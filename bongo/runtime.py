"""Agent 运行时核心逻辑。

bongo 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import os
import re
import sys
import textwrap
import uuid
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import memory as memorylib
from .context_manager import ContextManager
from .models import convert_tools_to_api_schema, convert_history_to_messages
from .profile import UserProfile, load_current_user
from .run_store import RunStore
from .task_status import TaskStatus
from . import tools as toolkit
from .utils import MAX_HISTORY, clip, now, persist_large_output

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<sensitive>"
DEFAULT_SHELL_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PWD", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "USER")
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_notes": False,  # 已弃用：relevant_notes 不再注入 prompt
    "context_reduction": True,
    "prompt_cache": True,
}
# 历史超过此条数时触发自动摘要压缩
COMPACT_THRESHOLD = 50
# 压缩后保留的最近条数
COMPACT_KEEP_RECENT = 10


@dataclass # 类似@Data，省去构造器和toString()方法
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str  # ← 提示词的实际内容
    hash: str  # ← text 内容的哈希值，用于快速比对内容是否改变
    workspace_fingerprint: str  # ← 当前工作区状态的"指纹"，比如文件列表、git commit id 等
    tool_signature: str  # ← 可用工具集的"签名"，如果工具变了，提示词也需要变
    built_at: str  # ← 构建时间戳，记录这个 prefix 是什么时候生成的

"""
SessionStore 类是一个会话持久化管理器。它的主要作用是：
1.提供统一的存储接口：定义了会话数据的存储位置和文件格式（JSON）。
2.实现 CRUD 操作：提供 save（创建/更新）、load（读取）操作。
3.支持会话恢复：通过 latest 方法支持 --resume latest 功能。
4.确保数据安全：自动创建目录，使用 UTF-8 编码保证中文等字符不乱码。
"""
class SessionStore:
    def __init__(self, root):
        self.root = Path(root) # 将传入的 root 路径字符串转换为 pathlib.Path 对象，方便后续的路径操作。
        self.root.mkdir(parents=True, exist_ok=True) # 创建根目录。
        # parents=True 表示如果父目录不存在也会一并创建；exist_ok=True 表示如果目录已存在也不会报错。这样确保了会话存储目录始终存在。

    def path(self, session_id):
        return self.root / f"{session_id}.json" # 生成会话文件路径

    def save(self, session):
        path = self.path(session["id"])
        # path.write_text()写入文件
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        # json.dumps()将python对象转为json形式，indent=2是json输出的空格缩进数
        return path

    # 加载指定id的会话文件
    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    # 加载最新的会话文件
    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None


class bongo:
    def __init__(
        self,
        model_client,
        work_dir=None,
        session_store=None,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=20,
        max_new_tokens=2048,
        depth=0,
        max_depth=10,
        read_only=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
    ):
        self.model_client = model_client
        self.work_dir = Path(work_dir or ".").resolve()
        self.root = self.work_dir
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.session_store = session_store or SessionStore(self.work_dir / ".bongo" / "sessions")
        self.run_store = run_store or RunStore(self.work_dir / ".bongo" / "runs")
        try:
            self._user_profile = UserProfile(load_current_user())
        except Exception:
            self._user_profile = None
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "work_dir": str(self.work_dir),
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        self.memory = memorylib.LayeredMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=self.work_dir,
        )
        self.session["memory"] = self.memory.to_dict()
        self.tools = self.build_tools()
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self)
        self.session_path = self.session_store.save(self.session)
        self.current_task_status = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self._last_tool_result_metadata = {}
        self._last_react_steps = []
        self._delete_cooldown = {}  # path -> last delete timestamp
        self._delete_just_happened = None  # "notes" or "mistakes" or None

    @classmethod
    def from_session(cls, model_client, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    @staticmethod  # 静态方法
    def remember(bucket, item, limit):
        # 构建了一个FIFO的队列(bucket)，新的在后面
        if not item:
            return
        if item in bucket:
            bucket.remove(item)  # 已在队列里，更新到最新元素的位置
        bucket.append(item)  # 将新的元素加到队尾
        del bucket[:-limit]  # 删除限制外旧元素

    # TODO
    def build_tools(self):
        return toolkit.build_tool_registry(self)

    def tool_signature(self):
        payload = []
        for name in sorted(self.tools):
            tool = self.tools[name]
            payload.append(
                {
                    "name": name,
                    "schema": tool["schema"],
                    "risky": tool["risky"],
                    "description": tool["description"],
                }
            )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def build_prefix(self):
        # Prefix 只包含身份和行为规则。工具定义通过 API 的 tools 数组传递。
        import platform
        os_name = platform.system()
        text = textwrap.dedent(
            f"""\
            You are bongo, an intelligent code learning assistant.

            Your role:
            - Help the user learn programming concepts, algorithms, and software engineering.
            - Explain code, debug issues, and provide learning guidance.
            - Read and analyze files from the workspace to answer questions.

            Environment:
            - OS: {os_name}

            Rules:
            - Use tools instead of guessing about file contents.
            - *** CRITICAL: After delete_entry succeeds (result starts with '已删除'), your next message MUST be the final answer to the user. Do NOT call any more tools. ***
            - *** CRITICAL: After patch_file succeeds (result contains 'patched'), your next message MUST be the final answer. Do NOT read the file. Trust the tool result. ***
            - IMPORTANT: read_entry already gives you the full content of an entry. Do NOT also call read_file or read_entry again for the same content. Use what you already have.
            - IMPORTANT: After reading an entry, go straight to the action (patch/write). Do NOT read the file again, do NOT list files, do NOT call unrelated tools.
            - IMPORTANT: When the user asks to modify a file, do: 1) read_entry to get content, 2) patch_file to modify, 3) output final answer. That is 2 tool calls maximum. Do NOT add extra reads or verification steps.
            - IMPORTANT: Only call tools that are directly needed for the user's request. Do NOT call list_files, search, or read_file unless the user specifically asks for them.
            - IMPORTANT: To save learning notes, use the write_note tool. Do NOT use write_file for notes.
            - IMPORTANT: To read a specific entry by list number, use read_entry(path, entry).
            - IMPORTANT: To delete a specific entry by list number, use delete_entry(path, entry). Do NOT use patch_file for this.
            - IMPORTANT: When a tool result says 'Full output saved to: ...', use read_cache(path) to read the full content.
            - When you have the final answer, output it directly without calling any tool.
            - Never invent tool results.
            - Do not repeat the same tool call with the same arguments."""
        ).strip()

        return PromptPrefix(
            text=text,
            hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            workspace_fingerprint="",
            tool_signature=self.tool_signature(),
            built_at=now(),
        )

    # 结合from_session恢复会话，从会话中加载前缀状态
    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        prefix_state = self.build_prefix() if force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        return self.memory.render_memory_text()

    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"
        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            role = item.get("role", "")
            if role == "tool":
                name = item.get("name", "")
                # 从 assistant 的 tool_use 块中提取 args（如果有）
                args = item.get("args", {})
                if not args and index > 0:
                    prev = history[index - 1]
                    if prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
                        for block in prev["content"]:
                            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == name:
                                args = block.get("input", {})
                                break
                if name == "read_file" and not recent:
                    path = str(args.get("path", ""))
                    if path in seen_reads:
                        continue
                    seen_reads.add(path)
                limit = 900 if recent else 180
                lines.append(f"[tool:{name}] {json.dumps(args, sort_keys=True)}")
                lines.append(clip(item.get("content", ""), limit))
            else:
                limit = 900 if recent else 220
                content = item.get("content", "")
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                    tool_parts = [f"[call:{p.get('name', '')}]" for p in content if isinstance(p, dict) and p.get("type") == "tool_use"]
                    content = " ".join(text_parts + tool_parts)
                lines.append(f"[{role}] {clip(str(content), limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def compact_history(self):
        """当历史过长时，用模型生成摘要替换旧历史。

        借鉴 Claude Code 的 Autocompact 思想的简化版：
        - 不 fork 子 Agent，直接用当前模型生成摘要
        - 只替换旧历史，保留最近 COMPACT_KEEP_RECENT 条
        - 摘要作为 system 消息插入，标记为压缩边界
        """
        history = self.session.get("history", [])
        if len(history) <= COMPACT_THRESHOLD:
            return False

        keep_recent = COMPACT_KEEP_RECENT
        old_entries = history[:-keep_recent]
        recent_entries = history[-keep_recent:]

        # 构建摘要提示词
        summary_lines = []
        for item in old_entries:
            role = item.get("role", "")
            if role == "tool":
                name = item.get("name", "")
                args = item.get("args", {})
                summary_lines.append(
                    f"[tool:{name}] {json.dumps(args, sort_keys=True)} -> "
                    f"{len(str(item.get('content', '')))} chars"
                )
            else:
                content = item.get("content", "")
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                    content = " ".join(text_parts)[:200]
                else:
                    content = str(content)[:200]
                summary_lines.append(f"[{role}] {content}")

        compact_prompt = (
            "Summarize the following conversation history in 300 words or less. "
            "Focus on: what files were read/written, what tasks were attempted, "
            "what errors occurred, and what the current state is.\n\n"
            "History:\n" + "\n".join(summary_lines)
        )

        try:
            summary = self.model_client.complete(compact_prompt, 512)
            summary = str(summary).strip()
            if not summary:
                return False
        except Exception:
            return False

        # 用摘要替换旧历史
        compact_marker = {
            "role": "system",
            "content": f"[Conversation compacted. Summary of {len(old_entries)} earlier entries:]\n{summary}",
            "created_at": now(),
            "compacted": True,
        }
        self.session["history"] = [compact_marker] + recent_entries
        self.session_store.save(self.session)
        return True

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    @staticmethod
    def looks_sensitive_env_name(name):
        upper = str(name).upper()
        return any(upper == marker or upper.endswith(marker) or upper.endswith(f"_{marker}") for marker in SENSITIVE_ENV_NAME_MARKERS)

    def is_secret_env_name(self, name):
        upper = str(name).upper()
        return upper in self.secret_env_names or self.looks_sensitive_env_name(upper)

    def secret_env_items(self):
        items = [
            (name, value)
            for name, value in os.environ.items()
            if self.is_secret_env_name(name) and value
        ]
        items.sort(key=lambda item: item[0])
        return items

    def secret_env_summary(self):
        names = [name for name, _ in self.secret_env_items()]
        return {
            "secret_env_count": len(names),
            "secret_env_names": names,
        }

    def redact_text(self, text):
        text = str(text)
        for _, value in sorted(self.secret_env_items(), key=lambda item: len(item[1]), reverse=True):
            text = text.replace(value, REDACTED_VALUE)
        return text

    def redact_artifact(self, value, key=None):
        if key and self.is_secret_env_name(key):
            return REDACTED_VALUE
        if isinstance(value, dict):
            return {
                str(item_key): self.redact_artifact(item_value, key=item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, tuple):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, str):
            redacted = self.redact_text(value)
            return redacted
        return value

    def shell_env(self):
        env = {
            name: os.environ[name]
            for name in self.shell_env_allowlist
            if name in os.environ
        }
        env["PWD"] = str(self.root)
        if "PATH" not in env and os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]
        return env

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        prompt, metadata = self.context_manager.build(user_message)
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "tool_signature": self.prefix_state.tool_signature,
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
            }
        )
        metadata.update(self.secret_env_summary())
        return prompt, metadata

    def _build_structured_params(self, user_message):
        """构建结构化 API 参数。

        system: prefix（身份+规则，不含工具定义）
        tools: API 原生工具定义数组
        messages: 多轮结构化消息（context + history + request）
        """
        system = self.prefix
        api_tools = convert_tools_to_api_schema(self.tools)

        # 注入上下文（workspace + memory）到第一条消息
        context_parts = []
        if self.root:
            context_parts.append(f"Workspace root: {self.root}\nAll file paths are relative to this directory. To read/write README.md in the workspace, use path='README.md'.")
        memory_text = self.memory_text()
        if memory_text and memory_text != "(no memory)":
            context_parts.append(memory_text)
        context = "\n".join(context_parts) if context_parts else ""

        # 历史转为结构化消息
        history = self.session.get("history", [])
        history_messages = convert_history_to_messages(history)

        # 如果有 context，prepend 到第一条历史消息（或创建新消息）
        if context:
            if history_messages and history_messages[0]["role"] == "user":
                first = history_messages[0]
                if isinstance(first["content"], list):
                    first["content"].insert(0, {"type": "text", "text": context})
                else:
                    first["content"] = context + "\n\n" + str(first["content"])
            else:
                history_messages.insert(0, {"role": "user", "content": context})

        # 追加当前请求
        history_messages.append({"role": "user", "content": user_message})

        return system, api_tools, history_messages

    # 这个方法是"运行轨迹记录器"，
    # 它的作用是将 bongo 智能体在执行任务过程中的每一个关键事件（如思考、工具调用、输出等）都记录下来，
    # 形成一个完整的执行时间线，用于调试、监控和审计。
    def emit_trace(self, task_status, event, payload=None):
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答"这一轮 agent 到底做了什么"。
        self.run_store.append_trace(task_status, payload)
        return payload

    def update_memory_after_tool(self, name, args, result):
        """把少量高价值工具结果沉淀到 working memory。

        为什么存在：
        并不是每个工具结果都值得长期带进下一轮 prompt。完整结果已经进了
        `history`，这里只挑少量"下一轮大概率还会用到"的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出：
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果先进入完整历史，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        path = args.get("path")
        if not path:
            return

        canonical_path = self.memory.canonical_path(path)
        if name in {"read_file", "write_file", "patch_file"}:
            self.memory.remember_file(canonical_path)

        # /ask 模式：更新 ask_mode 索引和已加载文档
        ask_mode = self.session.get("memory", {}).get("ask_mode", {})
        if ask_mode.get("mode"):
            self._update_ask_mode_after_tool(name, args, result, canonical_path)
        else:
            # 非 /ask 模式：使用原有的 file_summaries
            if name == "read_file":
                summary = memorylib.summarize_read_result(result, model_client=self.model_client)
                self.memory.set_file_summary(canonical_path, summary)
            elif name in {"write_file", "patch_file"}:
                self.memory.invalidate_file_summary(canonical_path)

    def _update_ask_mode_after_tool(self, name, args, result, canonical_path):
        """/ask 模式下工具执行后的 memory 更新。"""
        path = args.get("path", "")
        ask_mode = self.session["memory"].get("ask_mode", {})
        index = ask_mode.get("index", [])

        # 找到 index 中匹配的文档
        doc_id = None
        for entry in index:
            entry_label = entry.get("label", "")
            label_base = entry_label.split(" (")[0].split(" [")[0]
            if label_base == path or path.endswith(label_base) or label_base.endswith(path):
                doc_id = str(entry["id"])
                break

        if name == "read_file":
            if doc_id:
                self.memory.load_document(doc_id, path, result)
                self._fork_summary(doc_id, path)
        elif name in {"write_file", "patch_file"}:
            if doc_id:
                self._fork_summary(doc_id, path)

    def _fork_summary(self, doc_id, path):
        """fork 子线程用模型生成文件摘要并更新 index。"""
        import threading
        from . import compressor

        def _summarize():
            try:
                full_path = self.root / path
                if not full_path.exists():
                    return
                content = full_path.read_text(encoding="utf-8", errors="replace")
                summary = compressor.compress_document(content, self.model_client)
                self.memory.update_index_summary(doc_id, summary)
                self.session_store.save(self.session)
            except Exception:
                pass
        threading.Thread(target=_summarize, daemon=True).start()

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args, result)

    def ask_direct(self, user_message):
        """直接问答链路：不走工具循环，直接调用模型回答。

        为什么存在：
        对于纯问答、解释概念等不需要工具的场景，直接调用模型更快。
        避免了 ReAct 循环的开销，响应更快。

        输入 / 输出：
        - 输入：`user_message`，用户的问题
        - 输出：字符串形式的回答

        在 agent 链路里的位置：
        CLI 收到普通问题时调用此方法，而不是 ask()。
        """
        # 记录用户消息
        self.record({"role": "user", "content": user_message, "created_at": now()})

        # 构建 prompt（包含上下文）
        prompt, _ = self._build_prompt_and_metadata(user_message)

        # 直接调用模型
        raw = self.model_client.complete(prompt, self.max_new_tokens)

        # 解析回答
        kind, payload = self.parse(raw)

        # 如果模型返回了工具调用，回退到 ask() 链路
        if kind == "tool":
            self.session["history"].pop()
            return self.ask(user_message)

        # 提取最终答案
        final = (payload or raw).strip()

        # 记录助手回答
        self.record({"role": "assistant", "content": final, "created_at": now()})

        return final

    def ask(self, user_message):
        """执行一次完整的 agent 回合，直到产出最终答案或命中停止条件。

        为什么存在：
        `ask()` 是整个 runtime 的总调度器。它把"用户提一个请求"扩展成一条
        可持续推进的控制循环：记录会话、组 prompt、调用模型、执行工具、
        写 trace/report、更新状态，直到模型给出最终答案或系统主动停下。

        输入 / 输出：
        - 输入：`user_message`，即用户这一次的任务描述
        - 输出：字符串形式的最终回答；如果中途达到步数上限或重试上限，
          返回的是一条停止原因说明

        在 agent 链路里的位置：
        它是 CLI 和底层工具/模型之间的核心桥梁。CLI 收到用户输入后基本只做
        一件事：调用 `agent.ask()`。而 `ask()` 内部再去驱动 `ContextManager`
        组 prompt、`model_client.complete()` 调模型、`run_tool()` 执行动作。
        如果新人想理解 bongo 是怎么"从一句话跑成一个 agent 流程"的，
        这里就是最关键的入口。
        """
        run_started_at = time.monotonic()  # 返回单调时间,便于计算所用时间

        self.memory.set_task_summary(user_message)
        self.record({"role": "user", "content": user_message, "created_at": now()})
        task_status = TaskStatus.create(
            run_id=self.new_run_id(), task_id=self.new_task_id(), user_request=user_message)
        self.current_task_status = task_status
        self.current_run_dir = self.run_store.start_run(task_status)
        # 记录运行开始的轨迹时间
        self.emit_trace(
            task_status,
            "run_started",
            {
                "task_id": task_status.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        tool_steps = 0  # 执行了多少次工具调用
        attempts = 0  # 尝试次数
        react_round = 0  # ReAct 循环轮次（用于显示）
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)  # 这是为了容忍一定次数的模型错误,同时设置合理上限:
        step_lines = []  # 记录中间步骤的输出行，用于最终折叠

        def _step_print(text):
            """打印并记录中间步骤，后续可折叠。"""
            step_lines.append(text)
            print(text)

        # 这是 agent 的主循环，可以按"感知 -> 决策 -> 行动 -> 记录"来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_status / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while tool_steps < self.max_steps and attempts < max_attempts:
            attempts += 1
            react_round += 1
            _step_print(f"\n  [Round {react_round}] Thinking...")
            task_status.record_attempt()
            prompt_started_at = time.monotonic()
            # 每个关键步骤都更新 current_action 并落盘，方便外部 --status 随时查看
            task_status.current_action = "building_prompt"
            self.run_store.write_task_status(task_status)
            # 历史过长时自动压缩
            if self.feature_enabled("context_reduction"):
                self.compact_history()
            # 关键，每轮都重新构建提示词和元数据
            prompt, prompt_metadata = self._build_prompt_and_metadata(user_message)
            self.emit_trace(
                task_status,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            self.emit_trace(
                task_status,
                "model_requested",
                {
                    "attempts": task_status.attempts,
                    "tool_steps": task_status.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            task_status.current_action = "sending_to_model"
            self.run_store.write_task_status(task_status)
            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(self.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            model_started_at = time.monotonic()
            # 构建结构化参数，让模型原生理解 system/tools/messages
            system, tools, messages = self._build_structured_params(user_message)
            raw = self.model_client.complete(
                prompt,
                self.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
                system=system,
                tools=tools,
                messages=messages,
            )
            completion_metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            self.last_completion_metadata = completion_metadata
            self.last_prompt_metadata = prompt_metadata
            task_status.current_action = "model_completed"
            self.run_store.write_task_status(task_status)
            kind, payload = self.parse(raw)
            self.emit_trace(
                task_status,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )

            if kind == "tool":
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                args_preview = ", ".join(f"{k}={str(v)[:30]}" for k, v in list(args.items())[:3])
                _step_print(f"  [Round {react_round}] Acting: {name}({args_preview})")
                tool_use_id = payload.get("id", f"toolu_{uuid.uuid4().hex[:12]}")
                task_status.record_tool(name)
                task_status.current_action = f"executing_tool:{name}"
                self.run_store.write_task_status(task_status)
                tool_started_at = time.monotonic()
                result = self.run_tool(name, args)
                result_preview = result[:80].replace("\n", " ") if result else "(empty)"
                _step_print(f"  [Round {react_round}] Observing: {result_preview}")
                task_status.current_action = f"tool_completed:{name}"
                # 记录 assistant 的 tool_use 块
                self.record({
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": args}],
                    "created_at": now(),
                })
                # 记录 tool_result
                self.record({
                    "role": "tool",
                    "tool_use_id": tool_use_id,
                    "name": name,
                    "content": result,
                    "created_at": now(),
                })
                self.run_store.write_task_status(task_status)
                self.emit_trace(
                    task_status,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(self._last_tool_result_metadata or {}),
                    },
                )
                continue

            if kind == "retry":
                self.record({"role": "assistant", "content": payload, "created_at": now()})
                self.run_store.write_task_status(task_status)
                continue

            final = (payload or raw).strip()
            _step_print(f"  [Round {react_round}] Done")

            # 折叠中间步骤：清掉已输出的步骤行，替换为一行摘要
            if step_lines:
                total_lines = sum(text.count("\n") + 1 for text in step_lines)
                # ANSI: 上移 total_lines 行，清掉从光标到屏幕末尾的内容
                sys.stdout.write(f"\033[{total_lines}A\033[J")
                sys.stdout.flush()
                detail = " | ".join(
                    line.strip() for line in step_lines
                    if line.strip().startswith("[Round")
                )
                print(f"  [ReAct: {react_round} rounds, {tool_steps} tools] {detail}")
                print(f"  (按 Enter 展开完整过程)")
            self._last_react_steps = list(step_lines)
            self.record({"role": "assistant", "content": final, "created_at": now()})
            task_status.current_action = "final_answer"
            task_status.finish_success(final)
            self.run_store.write_task_status(task_status)
            self.emit_trace(
                task_status,
                "run_finished",
                {
                    "status": task_status.status,
                    "stop_reason": task_status.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            self.run_store.write_report(task_status, self.redact_artifact(self.build_report(task_status)))
            return final

        if attempts >= max_attempts and tool_steps < self.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_status.current_action = "stopped:retry_limit"
            task_status.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_status.current_action = "stopped:step_limit"
            task_status.stop_step_limit(final)

        self.record({"role": "assistant", "content": final, "created_at": now()})
        self.run_store.write_task_status(task_status)
        self.emit_trace(
            task_status,
            "run_finished",
            {
                "status": task_status.status,
                "stop_reason": task_status.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        self.run_store.write_report(task_status, self.redact_artifact(self.build_report(task_status)))
        # 超限退出时也折叠中间步骤
        if step_lines:
            total_lines = sum(text.count("\n") + 1 for text in step_lines)
            sys.stdout.write(f"\033[{total_lines}A\033[J")
            sys.stdout.flush()
            print(f"  [ReAct: {react_round} rounds, {tool_steps} tools] (达到上限)")
            print(f"  (按 Enter 展开完整过程)")
        self._last_react_steps = list(step_lines)
        return final

    def run_tool(self, name, args):
        """执行一次工具调用，并在执行前后套上完整护栏。

        为什么存在：
        在 agent 系统里，真正危险的不是"模型会不会想调用工具"，而是
        "平台有没有在执行前把边界守住"。这个函数就是工具层的总闸口：
        所有工具调用都必须先经过它，不能让模型直接碰到底层函数。

        输入 / 输出：
        - 输入：工具名 `name`，参数字典 `args`
        - 输出：字符串结果。无论是成功结果还是错误信息，都会统一返回文本，
          这样模型下一轮都能继续消费这份反馈。

        在 agent 链路里的位置：
        它位于 `ask()` 的"模型决定要调用工具"之后，是控制循环里真正把模型
        意图落到外部世界的一步。因此这里串起了几乎所有安全与可控设计：
        工具是否存在、参数是否合法、是否重复、是否需要审批、执行结果是否裁剪、
        是否需要回写记忆。
        """
        # 工具执行不是"直接调函数"，而是一条带护栏的流水线：
        # 工具是否存在 -> 参数是否合法 -> 是否重复调用 -> 是否通过审批
        # -> 真正执行 -> 更新记忆。
        tool = self.tools.get(name)
        # 工具存在性查询
        if tool is None:
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "unknown_tool",
                "security_event_type": "",
            }
            return f"error: unknown tool '{name}'"
        # 参数合法性查询
        try:
            self.validate_tool(name, args)
        except Exception as exc:
            message = f"error: invalid arguments for {name}: {exc}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "invalid_arguments",
                "security_event_type": security_event_type,
            }
            return message
        # 是否重复调用
        if self.repeated_tool_call(name, args):
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "repeated_identical_call",
                "security_event_type": "",
            }
            return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
        # delete_entry cooldown: block rapid repeated deletes on the same file
        if name == "delete_entry":
            path_key = str(args.get("path", ""))
            now_ts = time.time()
            last_delete = self._delete_cooldown.get(path_key, 0)
            if now_ts - last_delete < 10:
                self._last_tool_result_metadata = {
                    "tool_status": "rejected",
                    "tool_error_code": "delete_cooldown",
                    "security_event_type": "",
                }
                return "error: delete_entry cooldown — you already deleted from this file. Output your final answer now. Do NOT delete again."
        # Post-delete read guard: block pointless reads on a file that was just deleted from
        now_ts = time.time()
        if name in ("read_file", "read_entry", "search"):
            path_key = str(args.get("path", ""))
            last_delete = self._delete_cooldown.get(path_key, 0)
            if now_ts - last_delete < 10:
                self._last_tool_result_metadata = {
                    "tool_status": "rejected",
                    "tool_error_code": "post_delete_read_blocked",
                    "security_event_type": "",
                }
                return "error: You already deleted from this file. The file has changed. Do NOT read or verify. Output your final answer now."
        # Block read_notes after notes delete, search_mistakes after mistakes delete
        if name == "read_notes" and self._delete_just_happened == "notes" and now_ts - max(self._delete_cooldown.values(), default=0) < 10:
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "post_delete_read_blocked",
                "security_event_type": "",
            }
            return "error: You already deleted a note. Do NOT read or verify. Output your final answer now."
        if name == "search_mistakes" and self._delete_just_happened == "mistakes" and now_ts - max(self._delete_cooldown.values(), default=0) < 10:
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "post_delete_read_blocked",
                "security_event_type": "",
            }
            return "error: You already deleted a mistake. Do NOT read or verify. Output your final answer now."
        if tool["risky"] and not self.approve(name, args):
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "approval_denied",
                "security_event_type": "read_only_block" if self.read_only else "approval_denied",
            }
            return f"error: approval denied for {name}"
        try:
            raw_result = str(tool["run"](args))
            tool_use_id = f"{name}_{hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:8]}"
            result, cache_path = persist_large_output(raw_result, tool_use_id)
            # Record delete cooldown on success and inject stop instruction
            if name == "delete_entry" and result.startswith("已删除"):
                path_key = str(args.get("path", ""))
                self._delete_cooldown[path_key] = time.time()
                # Track file type for read_notes/search_mistakes blocking
                lower_path = path_key.lower()
                if "note" in lower_path:
                    self._delete_just_happened = "notes"
                elif "mistake" in lower_path:
                    self._delete_just_happened = "mistakes"
                result += "\n\n[SYSTEM] Deletion complete. You MUST now output your final answer to the user. Do NOT call any more tools."
            self.update_memory_after_tool(name, args, result)
            self._last_tool_result_metadata = {
                "tool_status": "ok",
                "tool_error_code": "",
                "security_event_type": "",
                "persisted_to": cache_path or "",
                "original_chars": len(raw_result),
            }
            return result
        except Exception as exc:
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            self._last_tool_result_metadata = {
                "tool_status": "error",
                "tool_error_code": "tool_failed",
                "security_event_type": security_event_type,
            }
            return f"error: tool {name} failed: {exc}"

    def repeated_tool_call(self, name, args):
        # agent 很常见的一种坏循环，是在没有新信息的情况下反复发起同一调用。
        # 检查：上一次完全相同的调用（同名同参数）之后，是否有其他工具调用。
        # 如果没有（连续重复），则拦截；如果有（中间做了别的事），则放行。
        history = self.session["history"]
        last_match_idx = -1
        last_tool_idx = -1
        for i, item in enumerate(history):
            if item.get("role") != "tool":
                continue
            last_tool_idx = i
            if item.get("name") != name:
                continue
            if i > 0:
                prev = history[i - 1]
                if prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
                    for block in prev["content"]:
                        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == name:
                            if block.get("input", {}) == args:
                                last_match_idx = i
                            break
        # 没有历史匹配，或上次匹配之后有其他工具调用，放行
        if last_match_idx < 0:
            return False
        return last_match_idx == last_tool_idx

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    _EXIT_STATUS_MAP = {
        "final_answer_returned": "success",
        "step_limit_reached": "limit_reached",
        "retry_limit_reached": "limit_reached",
        "model_error": "error",
        "tool_timeout": "error",
        "approval_denied": "error",
        "delegate_failed": "error",
        "persistence_error": "error",
        "resume_load_error": "error",
    }

    def build_report(self, task_status):
        # report 是一次运行的最终摘要 — 谁请求的、跑了什么、结果怎样。
        # 具体过程细节在 trace 里，这里只记关键指标。
        stop_reason = task_status.stop_reason
        exit_status = self._EXIT_STATUS_MAP.get(stop_reason, "error")
        return {
            "run_id": task_status.run_id,
            "user_request": task_status.user_request,
            "final_answer": task_status.final_answer,
            "exit_status": exit_status,
            "stop_reason": stop_reason,
            "tools_called": task_status.tools_called,
            "tool_call_count": task_status.tool_steps,
            "model_call_count": task_status.attempts,
        }

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self, name, args)
        if name == "delegate":
            if self.depth >= self.max_depth:
                raise ValueError("delegate depth exceeded")

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self, args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self, args)

    def tool_search(self, args):
        return toolkit.tool_search(self, args)

    def tool_run_shell(self, args):
        return toolkit.tool_run_shell(self, args)

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self, args)

    def tool_patch_file(self, args):
        return toolkit.tool_patch_file(self, args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self, args)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        # 非危险工具自动通过
        tool_spec = self.tools.get(name, {})
        if not tool_spec.get("risky", False):
            return True
        try:
            display_args = {k: v for k, v in args.items() if k not in ("content", "old_text", "new_text")}
            answer = input(f"approve {name} {json.dumps(display_args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    @staticmethod
    def parse(raw):
        """把模型输出解析成 runtime 可执行的动作或最终答案。

        支持两种输入：
        - dict：ModelClient 返回的结构化 tool_use/text 块（原生工具调用）
        - str：纯文本（视为最终答案）
        """
        # 结构化响应（ModelClient 在有 tools 时返回 dict）
        if isinstance(raw, dict):
            if raw.get("type") == "tool_use":
                name = raw.get("name", "")
                args = raw.get("input", {})
                if not name:
                    return "retry", bongo.retry_notice("tool_use missing name")
                if not isinstance(args, dict):
                    args = {}
                return "tool", {"name": name, "args": args, "id": raw.get("id", "")}
            if raw.get("type") == "text":
                text = raw.get("text", "").strip()
                if text:
                    return "final", text
                return "retry", bongo.retry_notice("empty text response")
        # 纯文本 = 最终回答
        if isinstance(raw, str):
            text = raw.strip()
            if text:
                return "final", text
        return "retry", bongo.retry_notice("empty response")

    @staticmethod
    def retry_notice(problem=None):
        prefix = "Runtime notice"
        if problem:
            prefix += f": {problem}"
        else:
            prefix += ": model returned malformed output"
        return f"{prefix}. Call a tool or output your final answer directly."

    def expand_last_steps(self):
        """展开上一次 ask() 的完整 ReAct 过程。"""
        steps = getattr(self, "_last_react_steps", [])
        if not steps:
            print("(无可展开的步骤)")
            return
        print("\n  --- ReAct 完整过程 ---")
        for line in steps:
            print(line)
        print("  --- 结束 ---\n")

    def reset(self):
        self.session["history"] = []
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(self.session["memory"], workspace_root=self.root)
        self.session_store.save(self.session)

    def path(self, raw_path):
        # 将原始路径字符串转换为 Python 的 pathlib.Path 对象，便于后续的路径操作。
        path = Path(raw_path)
        # 如果 path 已经是绝对路径（如 /home/user/file.txt），则保持不变
        # 如果是相对路径（如 ./src/main.py 或 ../config.json），则将其拼接到工作区根目录下
        path = path if path.is_absolute() else self.root / path
        # 解析掉所有的 ../ 和符号链接 (symlinks)，拿到物理世界的真实路径
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        # 构建路径列表：[str(self.root), str(resolved)],计算公共前缀：os.path.commonpath(...)
        # 如果路径在工作区内：返回工作区根路径,如果路径在工作区外：返回更短的公共前缀（通常是根目录 /）
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):# 判断在不在工作区的根目录下
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved


MiniAgent = bongo
