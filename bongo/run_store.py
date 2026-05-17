# 运行记录保存。
#
# session.json 负责保存"可恢复的会话状态"；RunStore 负责保存"单次运行的日志"，
# 例如 task_status、trace 和 report。两者分开后，恢复现场和复盘证据不会混在一起。

import json
import tempfile
from pathlib import Path

# value是一个对象，if查看其中有没有叫"run_id"的字段
def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # 返回指定run_id的路径
    def run_dir(self, run_id):
        return self.root / _run_id(run_id)

    def task_status_path(self, run_id):
        return self.run_dir(run_id) / "task_status.json"

    def trace_path(self, run_id):
        return self.run_dir(run_id) / "trace.jsonl"

    def report_path(self, run_id):
        return self.run_dir(run_id) / "report.json"

    def start_run(self, task_status):
        # 每次 ask() 都会生成一个 run 目录。
        # NOTE 这样一次用户请求对应一组独立记录，后续排查更容易。
        run_dir = self.run_dir(task_status)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_status(task_status)
        return run_dir

    def write_task_status(self, task_status):
        path = self.task_status_path(task_status)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, task_status.to_dict())
        return path

    def append_trace(self, task_status, event):
        path = self.trace_path(task_status)
        path.parent.mkdir(parents=True, exist_ok=True)
        # trace 采用 jsonl 追加写入，原因是 agent 运行过程是流式事件序列，
        # 逐条保存比"最后一次性写整份 trace"更稳，也更适合调试。
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
        return path

    def write_report(self, task_status, report):
        path = self.report_path(task_status)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, report)
        return path

    def load_task_status(self, task_id):
        return json.loads(self.task_status_path(task_id).read_text(encoding="utf-8"))

    def load_report(self, task_id):
        return json.loads(self.report_path(task_id).read_text(encoding="utf-8"))

    def _write_json_atomic(self, path, payload):
        # 原子写：先写临时文件，再 replace。
        # 这样即使中途异常，也不容易留下半截 JSON。
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(path)
