from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .database import StudyDatabase
from .exporter import export_saved_learning_skill, learning_skill_preview
from .ingestion import KnowledgeIngestor
from .memory import ConversationContext
from .activity import ActivityRecorder, WorkSessionTool
from .focus_agent import WorkBreakAgent
from .news import HackerNewsDigestGenerator, HackerNewsTool, validate_cached_digest
from .providers import (
    ClaudeCodeProvider,
    CodexCliProvider,
    ProviderConfig,
    available_providers,
    chat_backend_available,
    resolve_chat_backend,
    build_provider,
)
from .rag import ExternalRagConnector, RagConnection, file_digest, normalize_records
from .work_agent import DefaultWorkAgent


def default_data_dir() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "BongoStudy"
    return Path.home() / ".bongo-study"


class LearningService:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or default_data_dir()).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = StudyDatabase(self.data_dir / "bongo.db")
        self.context = ConversationContext(self.database)

    def provider_config(self) -> ProviderConfig:
        names = available_providers()
        default_name = names[0] if names else "openai"
        return ProviderConfig(
            name=self.database.get_setting("provider", default_name),
            model=self.database.get_setting("model", ""),
            api_key=self.database.get_setting("api_key", ""),
            base_url=self.database.get_setting("base_url", ""),
        )

    def set_provider(
        self,
        name: str,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
    ) -> None:
        self.database.set_setting("provider", name)
        self.database.set_setting("model", model)
        self.database.set_setting("base_url", base_url)
        self.database.set_setting("api_key", api_key)

    def chat_backend(self) -> str:
        return resolve_chat_backend(self.database.get_setting("chat_backend", "default"))

    def set_chat_backend(self, name: str) -> None:
        backend = resolve_chat_backend(name)
        if backend != "default" and not chat_backend_available(backend):
            cli_name = "Claude Code" if backend == "cc" else "Codex"
            raise ValueError(f"没有找到可执行的 {cli_name} CLI，请先安装并加入 PATH")
        self.database.set_setting("chat_backend", backend)

    def _provider(self):
        return build_provider(self.provider_config(), cwd=self.data_dir)

    def ingest(self, path: str | Path, knowledge_type: str = "document") -> dict:
        if knowledge_type == "document":
            return self.upload_document(path)
        result = KnowledgeIngestor(self.database, self._provider()).ingest(path, "code")
        self.database.record_learning_event(
            "knowledge_imported",
            f"source:{result['source_id']}:imported",
            source_id=int(result["source_id"]),
            value=1,
            metadata={"knowledge_type": knowledge_type},
        )
        return result

    def _rag_connector(self, connection_id: int | None = None) -> ExternalRagConnector:
        row = self.database.get_rag_connection(connection_id)
        if not row:
            raise ValueError("请先在会话的 Chat 页面配置并启用外部 RAG 服务")
        return ExternalRagConnector(RagConnection.from_row(row))

    def save_rag_connection(self, **values) -> int:
        if not str(values.get("name", "")).strip():
            raise ValueError("请输入 RAG 连接名称")
        if not str(values.get("base_url", "")).strip().startswith(("http://", "https://")):
            raise ValueError("RAG Base URL 必须以 http:// 或 https:// 开头")
        connection_id = self.database.save_rag_connection(**values)
        self.database.activate_rag_connection(connection_id)
        return connection_id

    def test_rag_connection(self, connection_id: int) -> dict:
        return self._rag_connector(connection_id).health_check()

    def upload_document(self, path: str | Path, connection_id: int | None = None) -> dict:
        file_path = Path(path).resolve()
        if not file_path.is_file():
            raise ValueError("文档不存在")
        connection = self.database.get_rag_connection(connection_id)
        if not connection:
            raise ValueError("请先配置并启用外部 RAG 服务")
        document_id, created = self.database.add_rag_document(
            int(connection["id"]), file_path, file_digest(file_path)
        )
        if not created:
            document = self.database.get_rag_document(document_id) or {}
            return {"document_id": document_id, "created": False, "status": document.get("status", "ready")}
        try:
            result = self._rag_connector(int(connection["id"])).upload_document(file_path)
            remote_id = result["document_id"]
            self.database.set_rag_document_status(document_id, "ready", remote_id)
        except Exception as exc:
            self.database.set_rag_document_status(document_id, "failed", error=str(exc))
            raise
        return {"document_id": document_id, "created": True, "status": "ready", "remote_document_id": remote_id}

    def retry_document(self, document_id: int) -> dict:
        document = self.database.get_rag_document(document_id)
        if not document:
            raise ValueError("文档记录不存在")
        self.database.delete_rag_document(document_id)
        return self.upload_document(document["local_path"], int(document["connection_id"]))

    def delete_document(self, document_id: int) -> None:
        document = self.database.get_rag_document(document_id)
        if not document:
            return
        if document.get("remote_document_id"):
            self._rag_connector(int(document["connection_id"])).delete_document(str(document["remote_document_id"]))
        self.database.delete_rag_document(document_id)

    def analyze_work_session(self, recorder: ActivityRecorder, snapshot: dict) -> dict:
        return WorkBreakAgent(
            self._provider(),
            WorkSessionTool(recorder, snapshot),
        ).run()

    def cached_ai_news(self) -> dict | None:
        raw = self.database.get_setting("ai_news_digest", "")
        if not raw:
            return None
        try:
            return validate_cached_digest(json.loads(raw))
        except json.JSONDecodeError:
            return None

    def read_ai_news_ids(self, digest: dict | None = None) -> set[int]:
        digest = digest or self.cached_ai_news()
        if not digest:
            return set()
        raw = self.database.get_setting("ai_news_read_state", "")
        if not raw:
            return set()
        try:
            state = json.loads(raw)
            if int(state.get("fetched_at")) != int(digest["fetched_at"]):
                return set()
            return {int(news_id) for news_id in state.get("ids", [])}
        except (json.JSONDecodeError, TypeError, ValueError):
            return set()

    def mark_ai_news_read(self, news_id: int) -> None:
        digest = self.cached_ai_news()
        if not digest:
            return
        valid_ids = {int(item["id"]) for item in digest["items"]}
        if int(news_id) not in valid_ids:
            return
        read_ids = self.read_ai_news_ids(digest)
        read_ids.add(int(news_id))
        self.database.set_setting(
            "ai_news_read_state",
            json.dumps({
                "fetched_at": int(digest["fetched_at"]),
                "ids": sorted(read_ids),
            }, ensure_ascii=False),
        )

    def ai_news_due(self, now: float | None = None) -> bool:
        cached = self.cached_ai_news()
        if cached is None:
            return True
        if cached.get("complete") is False:
            return True
        current_time = time.time() if now is None else now
        return current_time - int(cached["fetched_at"]) >= 8 * 3600

    def fetch_ai_news(self, force: bool = False, progress=None, item_completed=None) -> dict:
        cached = self.cached_ai_news()
        if not force and not self.ai_news_due():
            if progress:
                progress({"percent": 100, "stage": "使用八小时内的简讯缓存"})
            return cached
        backend = self.chat_backend()
        if backend == "cc":
            provider = ClaudeCodeProvider(ProviderConfig(name="cc"), cwd=self.data_dir)
        elif backend == "codex":
            provider = CodexCliProvider(ProviderConfig(name="codex"), cwd=self.data_dir)
        else:
            provider = self._provider()
        def persist_partial(digest: dict) -> None:
            self.database.set_setting(
                "ai_news_digest",
                json.dumps(digest, ensure_ascii=False),
            )
            if item_completed:
                item_completed(digest)

        try:
            digest = HackerNewsDigestGenerator(
                provider,
                HackerNewsTool(),
            ).run(progress=progress, item_completed=persist_partial)
        except Exception:
            if cached is not None and not force:
                return cached
            raise
        self.database.set_setting(
            "ai_news_digest",
            json.dumps(digest, ensure_ascii=False),
        )
        return digest

    def start_conversation(
        self,
        title: str = "新会话",
        backend: str = "default",
        *,
        mode: str = "chat",
        rag_connection_id: int | None = None,
        work_dir: str = "",
    ) -> int:
        return self.database.create_conversation(
            title,
            resolve_chat_backend(backend),
            None,
            mode=mode,
            rag_connection_id=rag_connection_id,
            work_dir=work_dir,
        )

    def chat(
        self,
        conversation_or_source_id: int | None,
        message_or_conversation_id: str | int | None,
        legacy_message: str | None = None,
    ) -> dict:
        if legacy_message is not None:
            return self._legacy_local_chat(
                conversation_or_source_id,
                message_or_conversation_id if isinstance(message_or_conversation_id, int) else None,
                legacy_message,
            )
        conversation_id = conversation_or_source_id
        user_message = str(message_or_conversation_id or "")
        message = user_message.strip()
        if not message:
            raise ValueError("消息不能为空")
        if conversation_id is None:
            connection = self.database.get_rag_connection()
            if not connection:
                raise ValueError("请先配置并启用外部 RAG 服务")
            conversation_id = self.start_conversation(
                message[:48],
                "default",
                mode="chat",
                rag_connection_id=int(connection["id"]),
            )
        conversation = self.database.get_conversation(conversation_id)
        if not conversation:
            raise ValueError("对话不存在")
        if conversation.get("mode") != "chat":
            raise ValueError("所选会话不是 Chat 会话")
        connection_id = conversation.get("rag_connection_id")
        payload = self._rag_connector(connection_id).retrieve(message)
        records = normalize_records(payload)
        citations = [
            {"index": item["index"], "source": item["title"], "score": item["score"], "metadata": item["metadata"]}
            for item in records
        ]
        history = self.database.get_messages(conversation_id, limit=16)
        messages = [{"role": item["role"], "content": item["content"]} for item in history if item["role"] in {"user", "assistant"}]
        context = "\n\n".join(f"[来源{item['index']}：{item['title']}]\n{item['content']}" for item in records)[:16000]
        messages.append({"role": "user", "content": message + (f"\n\n外部 RAG 检索结果：\n{context}" if context else "")})
        system = (
            "你是 Bongo 本地助学伙伴。优先依据外部 RAG 返回的资料回答，并以 [来源N] 标注依据。"
            "资料不足时明确说明，不要虚构。回答清晰、适合学习复盘。"
        )
        user_message_id = self.database.add_message(conversation_id, "user", message)
        resolved_backend = "default"
        provider = self._provider()
        answer = str(provider.complete(messages, system)).strip()
        if not answer:
            raise RuntimeError("模型返回了空回答")
        assistant_message_id = self.database.add_message(conversation_id, "assistant", answer, citations)
        self._compact_if_needed(conversation_id)
        return {
            "conversation_id": conversation_id,
            "source_id": 0,
            "backend": resolved_backend,
            "answer": answer,
            "citations": citations,
        }

    def _legacy_local_chat(self, source_id: int | None, conversation_id: int | None, user_message: str) -> dict:
        message = user_message.strip()
        if not message or source_id is None:
            raise ValueError("消息和旧知识来源不能为空")
        source = self.database.get_source(source_id)
        if not source:
            raise ValueError("旧知识来源不存在")
        backend = resolve_chat_backend(self.chat_backend())
        if conversation_id is None:
            conversation_id = self.database.create_conversation(
                f"{source['name']} · {message[:36]}", backend, source_id, mode="legacy"
            )
        messages, citations, system = self.context.build(conversation_id, message)
        user_message_id = self.database.add_message(conversation_id, "user", message)
        self.database.add_conversation_insight(conversation_id, source_id, user_message_id, message)
        if backend == "cc":
            provider = ClaudeCodeProvider(ProviderConfig(name="cc"), cwd=self.data_dir)
        elif backend == "codex":
            provider = CodexCliProvider(ProviderConfig(name="codex"), cwd=self.data_dir)
        else:
            provider = self._provider()
        answer = str(provider.complete(messages, system)).strip()
        assistant_id = self.database.add_message(conversation_id, "assistant", answer, citations)
        self.database.resolve_conversation_insight(user_message_id, assistant_id, answer, citations)
        self._compact_if_needed(conversation_id)
        return {"conversation_id": conversation_id, "source_id": source_id, "backend": backend, "answer": answer, "citations": citations}

    def work(self, conversation_id: int | None, user_message: str, work_dir: str = "", backend: str = "default") -> dict:
        message = user_message.strip()
        if not message:
            raise ValueError("消息不能为空")
        selected_backend = resolve_chat_backend(backend)
        if selected_backend != "default" and not chat_backend_available(selected_backend):
            raise ValueError("所选 CLI Agent 不可用")
        directory = Path(work_dir).resolve() if work_dir else None
        if conversation_id is None:
            if not directory or not directory.is_dir():
                raise ValueError("请选择有效的本地工作目录")
            conversation_id = self.start_conversation(
                message[:48], selected_backend, mode="work", work_dir=str(directory)
            )
        conversation = self.database.get_conversation(conversation_id)
        if not conversation or conversation.get("mode") != "work":
            raise ValueError("Work 会话不存在")
        directory = Path(conversation["work_dir"]).resolve()
        selected_backend = resolve_chat_backend(conversation["provider"])
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in self.database.get_messages(conversation_id, 16)
            if item["role"] in {"user", "assistant"}
        ]
        self.database.add_message(conversation_id, "user", message)
        if selected_backend == "cc":
            provider = ClaudeCodeProvider(ProviderConfig(name="cc"), cwd=directory)
            answer = str(provider.complete([*history, {"role": "user", "content": message}], "在当前目录完成用户任务并简要报告结果。"))
            run_id = 0
        elif selected_backend == "codex":
            provider = CodexCliProvider(ProviderConfig(name="codex"), cwd=directory, writable=True)
            answer = str(provider.complete([*history, {"role": "user", "content": message}], "在当前目录完成用户任务并简要报告结果。"))
            run_id = 0
        else:
            result = DefaultWorkAgent(self._provider(), self.database, conversation_id, directory).run(history, message)
            answer, run_id = result["answer"], result["run_id"]
        if not answer.strip():
            raise RuntimeError("Agent 返回了空结果")
        self.database.add_message(conversation_id, "assistant", answer)
        self._compact_if_needed(conversation_id)
        return {"conversation_id": conversation_id, "source_id": 0, "backend": selected_backend, "answer": answer, "citations": [], "run_id": run_id}

    def _compact_if_needed(self, conversation_id: int) -> None:
        messages = self.database.get_messages(conversation_id, limit=1000)
        if len(messages) < 24 or len(messages) % 8 != 0:
            return
        old = messages[:-12]
        transcript = "\n".join(f"{item['role']}: {item['content']}" for item in old)[-12000:]
        try:
            summary = self._provider().complete(
                [{"role": "user", "content": transcript}],
                "请将这段学习对话压缩为不超过300字的摘要，保留用户问题、关键结论和未解决事项。",
            )
        except Exception:
            return
        self.database.set_conversation_summary(conversation_id, str(summary).strip()[:2000])

    def create_skill(self, **values) -> int:
        return self.database.create_learning_skill(**values)

    def update_skill(self, skill_id: int, **values) -> None:
        self.database.update_learning_skill(skill_id, **values)

    def preview_skill(self, skill_id: int) -> dict:
        return learning_skill_preview(self.database, skill_id)

    def export_skill(self, skill_id: int, target: str | Path) -> Path:
        return export_saved_learning_skill(self.database, skill_id, target)

    def close(self) -> None:
        self.database.close()
