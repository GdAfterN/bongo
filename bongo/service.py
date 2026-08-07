from __future__ import annotations

import os
from pathlib import Path

from .database import StudyDatabase
from .exporter import export_learning_skill
from .ingestion import KnowledgeIngestor
from .memory import ConversationContext
from .providers import (
    ClaudeCodeProvider,
    CodexCliProvider,
    ProviderConfig,
    available_providers,
    chat_backend_available,
    resolve_chat_backend,
    build_provider,
)


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
        return KnowledgeIngestor(self.database, self._provider()).ingest(path, knowledge_type)

    def start_conversation(
        self,
        source_id: int,
        title: str = "新对话",
        backend: str | None = None,
    ) -> int:
        if not self.database.get_source(source_id):
            raise ValueError("请选择有效的知识文档")
        return self.database.create_conversation(
            title,
            backend or resolve_chat_backend(self.chat_backend()),
            source_id,
        )

    def chat(self, source_id: int | None, conversation_id: int | None, user_message: str) -> dict:
        message = user_message.strip()
        if not message:
            raise ValueError("消息不能为空")
        if conversation_id is None:
            if source_id is None:
                raise ValueError("请先选择要对话的知识文档")
            source = self.database.get_source(source_id)
            if not source:
                raise ValueError("所选知识文档不存在")
            backend = resolve_chat_backend(self.chat_backend())
            conversation_id = self.start_conversation(
                source_id,
                f"{source['name']} · {message[:36]}",
                backend,
            )
        conversation = self.database.get_conversation(conversation_id)
        if not conversation:
            raise ValueError("对话不存在")
        source_id = conversation.get("source_id")
        if source_id is None:
            raise ValueError("旧对话未绑定知识文档，请新建文档对话")
        messages, citations, system = self.context.build(conversation_id, message)
        self.database.add_message(conversation_id, "user", message)
        configured_backend = self.chat_backend()
        resolved_backend = resolve_chat_backend(configured_backend)
        if resolved_backend == "cc":
            provider = ClaudeCodeProvider(ProviderConfig(name="cc"), cwd=self.data_dir)
        elif resolved_backend == "codex":
            provider = CodexCliProvider(ProviderConfig(name="codex"), cwd=self.data_dir)
        else:
            provider = self._provider()
        answer = str(provider.complete(messages, system)).strip()
        if not answer:
            raise RuntimeError("模型返回了空回答")
        self.database.add_message(conversation_id, "assistant", answer, citations)
        self._compact_if_needed(conversation_id)
        return {
            "conversation_id": conversation_id,
            "source_id": source_id,
            "backend": resolved_backend,
            "answer": answer,
            "citations": citations,
        }

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

    def export_skill(self, target: str | Path) -> Path:
        return export_learning_skill(self.database, target)

    def close(self) -> None:
        self.database.close()
