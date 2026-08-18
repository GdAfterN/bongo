"""轻量级 Trace 日志。

每轮 ReAct 循环的工具调用追加写入 JSONL 文件，
用于中断恢复时定位"执行到哪一步"。
存储路径：.bongo/traces/{session_id}.jsonl
"""

import json
from datetime import datetime
from pathlib import Path


def _now():
    return datetime.now().isoformat(timespec="seconds")


class TraceStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _trace_path(self, session_id):
        return self.root / f"{session_id}.jsonl"

    def append(self, session_id, entry):
        """追加一条 trace 记录。entry 是 dict，自动加 timestamp。"""
        entry["timestamp"] = _now()
        path = self._trace_path(session_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True, ensure_ascii=False))
            f.write("\n")

    def read_last(self, session_id, n=10):
        """读取最后 N 条 trace 记录。"""
        path = self._trace_path(session_id)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        entries = []
        for line in lines[-n:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def read_all(self, session_id):
        """读取全部 trace 记录。"""
        path = self._trace_path(session_id)
        if not path.exists():
            return []
        entries = []
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries
