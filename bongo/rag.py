from __future__ import annotations

import hashlib
import json
import mimetypes
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RagError(RuntimeError):
    pass


@dataclass(frozen=True)
class RagConnection:
    id: int
    name: str
    base_url: str
    api_key: str = ""
    knowledge_id: str = ""
    upload_path: str = "/documents"
    retrieval_path: str = "/retrieval"
    delete_path: str = "/documents/{document_id}"

    @classmethod
    def from_row(cls, row: dict) -> "RagConnection":
        return cls(**{field: row.get(field, getattr(cls, field, "")) for field in cls.__dataclass_fields__})


class ExternalRagConnector:
    """HTTP connector compatible with Dify-style external knowledge retrieval."""

    def __init__(self, connection: RagConnection, timeout: int = 60):
        self.connection = connection
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.connection.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.connection.api_key:
            headers["Authorization"] = f"Bearer {self.connection.api_key}"
        return headers

    def _request(self, request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            raise RagError(f"RAG 服务返回 HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RagError(f"无法连接 RAG 服务: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RagError("RAG 服务返回了非 JSON 数据") from exc
        if not isinstance(value, dict):
            raise RagError("RAG 服务返回格式必须是 JSON 对象")
        return value

    def health_check(self) -> dict:
        return self.retrieve("Bongo 连接测试", top_k=1)

    def upload_document(self, path: str | Path) -> dict:
        file_path = Path(path).resolve()
        boundary = f"----Bongo{uuid.uuid4().hex}"
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        parts: list[bytes] = []
        fields = {"knowledge_id": self.connection.knowledge_id}
        for name, value in fields.items():
            if not value:
                continue
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_path.name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8")
        )
        parts.append(file_path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        headers = self._headers()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        payload = self._request(
            urllib.request.Request(self._url(self.connection.upload_path), data=b"".join(parts), headers=headers, method="POST")
        )
        remote_id = (
            payload.get("document_id") or payload.get("id") or
            (payload.get("document") or {}).get("id") or (payload.get("data") or {}).get("id")
        )
        if not remote_id:
            raise RagError("上传成功但响应中缺少 document_id")
        return {**payload, "document_id": str(remote_id)}

    def retrieve(self, query: str, top_k: int = 6, score_threshold: float = 0.2) -> dict:
        body = {
            "knowledge_id": self.connection.knowledge_id,
            "query": query,
            "retrieval_setting": {"top_k": top_k, "score_threshold": score_threshold},
        }
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        return self._request(
            urllib.request.Request(
                self._url(self.connection.retrieval_path),
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
        )

    def delete_document(self, remote_document_id: str) -> None:
        path = self.connection.delete_path.replace("{document_id}", remote_document_id)
        headers = self._headers()
        self._request(urllib.request.Request(self._url(path), headers=headers, method="DELETE"))


def file_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_records(payload: dict) -> list[dict[str, Any]]:
    records = payload.get("records")
    if records is None and isinstance(payload.get("data"), dict):
        records = payload["data"].get("records")
    if not isinstance(records, list):
        raise RagError("RAG 检索响应缺少 records 数组")
    normalized = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        content = str(record.get("content") or record.get("text") or "").strip()
        if not content:
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        normalized.append({
            "index": index,
            "content": content,
            "score": float(record.get("score") or 0),
            "title": str(record.get("title") or metadata.get("title") or metadata.get("document_name") or f"知识片段 {index}"),
            "metadata": metadata,
        })
    return normalized
