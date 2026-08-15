from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bongo.database import StudyDatabase
from bongo.rag import ExternalRagConnector, RagConnection, normalize_records
from bongo.work_agent import DefaultWorkAgent


class _RagHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path == "/retrieval":
            request = json.loads(body)
            assert request["knowledge_id"] == "kb-1"
            payload = {"records": [{"content": "外部知识内容", "score": 0.91, "title": "测试文档"}]}
        else:
            assert b'filename="notes.md"' in body
            payload = {"document_id": "remote-1"}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def test_external_rag_upload_and_dify_style_retrieval(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RagHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = RagConnection(1, "test", f"http://127.0.0.1:{server.server_port}", knowledge_id="kb-1")
        connector = ExternalRagConnector(connection)
        document = tmp_path / "notes.md"
        document.write_text("测试知识", encoding="utf-8")
        assert connector.upload_document(document)["document_id"] == "remote-1"
        records = normalize_records(connector.retrieve("测试"))
        assert records[0]["content"] == "外部知识内容"
        assert records[0]["title"] == "测试文档"
    finally:
        server.shutdown()
        server.server_close()


class _WorkProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, _messages, _system, _schema=None):
        self.calls += 1
        if self.calls == 1:
            return {"action": "tool", "tool": "write_file", "arguments": {"path": "result.txt", "content": "done"}, "answer": ""}
        return {"action": "final", "tool": "", "arguments": {}, "answer": "任务完成"}


def test_default_work_agent_writes_inside_workspace_and_records_trace(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        conversation_id = database.create_conversation("work", "default", mode="work", work_dir=str(workspace))
        result = DefaultWorkAgent(_WorkProvider(), database, conversation_id, workspace).run([], "创建结果")
        assert result["answer"] == "任务完成"
        assert (workspace / "result.txt").read_text(encoding="utf-8") == "done"
        step = database.conn.execute("SELECT name, status FROM agent_steps WHERE run_id=?", (result["run_id"],)).fetchone()
        assert dict(step) == {"name": "write_file", "status": "completed"}
    finally:
        database.close()
