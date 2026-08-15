from __future__ import annotations

import json
import re
from pathlib import Path


WORK_SYSTEM_PROMPT = """你是 Bongo 的本地工作 Agent。你只能通过提供的工具在指定工作目录内完成任务。
每轮只返回一个 JSON 决策：需要工具时 action=tool，并填写 tool 与 arguments；任务完成时 action=final，并填写 answer。
先检查再修改，避免无关改动。不要声称执行了未调用的操作。"""


DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["tool", "final"]},
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
        "answer": {"type": "string"},
    },
    "required": ["action", "tool", "arguments", "answer"],
    "additionalProperties": False,
}


class WorkspaceTools:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("工作目录不存在")

    def _path(self, value: str) -> Path:
        target = (self.root / value).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError("路径超出所选工作目录")
        return target

    def execute(self, name: str, arguments: dict) -> str:
        if name == "list_files":
            base = self._path(str(arguments.get("path", ".")))
            if not base.is_dir():
                raise ValueError("目标不是目录")
            rows = []
            for item in sorted(base.iterdir(), key=lambda value: (not value.is_dir(), value.name.lower()))[:300]:
                rows.append(f"{'DIR' if item.is_dir() else 'FILE'}\t{item.relative_to(self.root)}")
            return "\n".join(rows)
        if name == "file_info":
            target = self._path(str(arguments.get("path", "")))
            stat = target.stat()
            return json.dumps({"path": str(target.relative_to(self.root)), "size": stat.st_size, "is_dir": target.is_dir()}, ensure_ascii=False)
        if name == "read_file":
            target = self._path(str(arguments.get("path", "")))
            start = max(1, int(arguments.get("start_line", 1)))
            limit = min(1000, max(1, int(arguments.get("line_count", 300))))
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(f"{index}: {line}" for index, line in enumerate(lines[start - 1:start - 1 + limit], start))
        if name == "search_text":
            pattern = str(arguments.get("query", ""))
            suffix = str(arguments.get("suffix", ""))
            regex = re.compile(re.escape(pattern), re.IGNORECASE)
            matches = []
            for target in self.root.rglob(f"*{suffix}" if suffix else "*"):
                if not target.is_file() or target.stat().st_size > 2_000_000:
                    continue
                for number, line in enumerate(target.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if regex.search(line):
                        matches.append(f"{target.relative_to(self.root)}:{number}: {line[:300]}")
                        if len(matches) >= 200:
                            return "\n".join(matches)
            return "\n".join(matches)
        if name == "write_file":
            target = self._path(str(arguments.get("path", "")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(arguments.get("content", "")), encoding="utf-8")
            return f"已写入 {target.relative_to(self.root)}"
        if name == "delete_file":
            target = self._path(str(arguments.get("path", "")))
            if not target.is_file():
                raise ValueError("只允许删除工作目录内的单个文件")
            target.unlink()
            return f"已删除 {target.relative_to(self.root)}"
        raise ValueError(f"未知工具: {name}")


class DefaultWorkAgent:
    def __init__(self, provider, database, conversation_id: int, work_dir: str | Path):
        self.provider = provider
        self.database = database
        self.conversation_id = conversation_id
        self.tools = WorkspaceTools(work_dir)

    def run(self, history: list[dict], request: str) -> dict:
        run_id = self.database.create_agent_run(self.conversation_id, "default")
        messages = [*history, {"role": "user", "content": request}]
        try:
            for _ in range(10):
                decision = self.provider.complete(messages, WORK_SYSTEM_PROMPT, DECISION_SCHEMA)
                if not isinstance(decision, dict):
                    raise RuntimeError("模型未返回结构化 Agent 决策")
                if decision.get("action") == "final":
                    answer = str(decision.get("answer") or "").strip()
                    self.database.finish_agent_run(run_id)
                    return {"answer": answer, "run_id": run_id}
                tool = str(decision.get("tool") or "")
                arguments = decision.get("arguments") if isinstance(decision.get("arguments"), dict) else {}
                try:
                    output = self.tools.execute(tool, arguments)
                    status = "completed"
                except Exception as exc:
                    output = f"工具执行失败: {exc}"
                    status = "failed"
                self.database.add_agent_step(run_id, "tool", tool, arguments, output, status)
                messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
                messages.append({"role": "user", "content": f"工具 {tool} 返回：\n{output[:12000]}"})
            raise RuntimeError("Agent 超过最大工具调用轮数")
        except Exception as exc:
            self.database.finish_agent_run(run_id, "failed", str(exc))
            raise
