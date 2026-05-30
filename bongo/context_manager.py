"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# NOTE prompt=prefix+memory+history+current_request
# 设计原则：稳定部分在前（命中前缀缓存），不稳定部分在后
DEFAULT_TOTAL_BUDGET = 24000  # Prompt 总字符数预算上限（约 6000 tokens）
DEFAULT_SECTION_BUDGETS = {   # 各个组成部分（Section）的理想字符数配额
    "prefix": 5000,           # 系统前缀（身份+规则+工具）— 最稳定，完全静态
    "workspace": 200,         # 工作空间路径 — 动态注入，但字符数很少
    "memory": 2000,           # 工作记忆（当前任务/文件摘要）的配额 — 任务级稳定
    "history": 16800,         # 对话历史（之前的交互记录）的配额 — 每轮增长
}
DEFAULT_SECTION_FLOORS = {    # 各个组成部分的最小保底字符数，即使超预算也不能低于此值
    "prefix": 2000,           # 前缀的最小长度，确保核心规则和工具说明不丢失
    "workspace": 100,         # 工作空间最小长度
    "memory": 500,            # 工作记忆的最小长度
    "history": 3000,          # 对话历史的最小长度，保留最近的交互语境
}
# 当 prompt 超预算时，会优先压缩这些 section。顺序越靠前，越先被裁剪/压缩。
DEFAULT_REDUCTION_ORDER = ("history", "workspace", "memory", "prefix")
# Prompt 各部分的拼接顺序：稳定在前，不稳定在后（缓存友好）
SECTION_ORDER = ("prefix", "workspace", "memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
CLEAN_TOOL_RESULT_AGE = 4   # 超过 4 轮的工具结果内容替换为占位符（CC Microcompact 思想）
MAX_HISTORY_ITEM_CHARS = 2000  # 单条历史条目的最大字符数，超过则裁剪

# 如果文本太长，会对文本进行尾部截断
def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."

# 原始数据渲染器
@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    # 表示details可以是字典或空值类型，默认值为None
    details: dict | None = None

    @property  # 可以把方法当作属性来访问
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把"稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求"拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `bongo.ask()` 的每轮模型调用之前，是"真正发请求给模型"
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        # 工作空间路径：从 agent.root 动态获取，不在 prefix 中（prefix 保持静态以利缓存）
        workspace_root = getattr(self.agent, "root", None)
        workspace_text = f"Workspace:\n  - {workspace_root}" if workspace_root else ""

        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "workspace": workspace_text,
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        rendered = self._render_sections(section_texts, budgets)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 超出预算时按顺序压缩：history → workspace → memory → prefix
        # 当前请求永远不裁剪
        while len(prompt) > self.total_budget:
            overflow = len(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            user_message=user_message,
            section_texts=section_texts,
        )
        return prompt, metadata

    def _render_sections_without_reduction(self, section_texts):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]),
                                    rendered=section_texts["prefix"], details={}),
            "workspace": SectionRender(raw=section_texts["workspace"], budget=len(section_texts["workspace"]),
                                       rendered=section_texts["workspace"], details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]),
                                    rendered=section_texts["memory"], details={}),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw,
                                     details={"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    # 计算各部分最低配额
    def _compute_section_floors(self):
        floors = {
            section: max(20, int(budget) // 4)
            for section, budget in self.section_budgets.items()
        }
        floors.update(self._section_floor_overrides)
        return floors

    # 这个方法是"节内容渲染器"，它的作用是根据预设的预算（字符数限制）
    # 对不同部分的内容进行裁剪或特殊处理，生成适合发送给AI模型的最终文本。
    def _render_sections(self, section_texts, budgets):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_history_section(self, budget):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)
        if not history:
            rendered = "Transcript:\n- empty"
            return SectionRender(raw=raw, budget=budget, rendered=rendered, details={"rendered_entries": []})

        # 优先保留最近的历史，因为下一步决策通常最依赖刚刚发生的工具结果。
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)
        total = len(history)
        rendered_entries = []
        for index in reversed(range(total)):
            item = history[index]
            recent = index >= recent_start
            age = total - index  # 距今多少轮
            line_limit = 900 if recent else 60
            candidate_lines = self._render_history_item(item, line_limit, age=age)
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
            if len(candidate_rendered) <= budget:
                rendered_entries = candidate_entries
                continue
            if recent:
                available = budget - len("Transcript:")
                if rendered_entries:
                    available -= sum(len(line) + 1 for line in rendered_entries)
                available = max(20, available - 1)
                candidate_lines = self._render_history_item(item, available, age=age)
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
                if len(candidate_rendered) <= budget:
                    rendered_entries = candidate_entries
            else:
                smaller_lines = self._render_history_item(item, 20, age=age)
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["Transcript:", *smaller_entries])
                if len(smaller_rendered) <= budget:
                    rendered_entries = smaller_entries
        rendered = "\n".join(["Transcript:", *rendered_entries])

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
            },
        )

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            role = item.get("role", "")
            if role == "tool":
                name = item.get("name", "")
                if "tool_use_id" in item:
                    lines.append(f"[tool:{name}]")
                else:
                    lines.append(f"[tool:{name}] {json.dumps(item.get('args', {}), sort_keys=True)}")
                lines.append(str(item.get("content", "")))
            else:
                content = item.get("content", "")
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                    tool_parts = [f"[call:{p.get('name', '')}]" for p in content if isinstance(p, dict) and p.get("type") == "tool_use"]
                    content = " ".join(text_parts + tool_parts)
                lines.append(f"[{role}] {content}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit, age=0):
        role = item.get("role", "")
        if role == "tool":
            name = item.get("name", "")
            # 新格式（tool_use_id）或旧格式（args）
            if "tool_use_id" in item:
                prefix = f"[tool:{name}]"
            else:
                prefix = f"[tool:{name}] {json.dumps(item.get('args', {}), sort_keys=True)}"
            if age >= CLEAN_TOOL_RESULT_AGE:
                original_len = len(str(item.get("content", "")))
                content = f"[Old tool result cleared ({original_len} chars)]"
                return [prefix, content]
            content = str(item.get("content", ""))
            if name == "read_file" and len(content) > MAX_HISTORY_ITEM_CHARS:
                lines = content.split("\n")
                preview = "\n".join(lines[:20])
                content = f"[File read: {len(content)} chars, {len(lines)} lines]\n{preview}\n...[truncated]"
                content = _tail_clip(content, max(20, line_limit))
            else:
                content = _tail_clip(content, max(20, line_limit))
            return [prefix, content]
        # assistant 消息：支持结构化 content list
        content = item.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            tool_parts = [f"[call:{p.get('name', '')}]" for p in content if isinstance(p, dict) and p.get("type") == "tool_use"]
            content = " ".join(text_parts + tool_parts)
        return [f"[{role}] {_tail_clip(str(content), line_limit)}"]

    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        # prefix → workspace → memory → history → current_request
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["workspace"].rendered,
                rendered["memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

    # 元数据是和prompt紧密相关的，包含了prompt里的各种信息，便于调试
    def _metadata(self, prompt, rendered, budgets, reduction_log, user_message, section_texts):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }
