from __future__ import annotations

import os
from pathlib import Path

from .database import StudyDatabase
from .exporter import export_learning_skill
from .ingestion import KnowledgeIngestor
from .memory import ConversationContext
from .providers import ProviderConfig, available_providers, build_provider


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
            base_url=self.database.get_setting("base_url", ""),
        )

    def set_provider(self, name: str, model: str = "", base_url: str = "") -> None:
        self.database.set_setting("provider", name)
        self.database.set_setting("model", model)
        self.database.set_setting("base_url", base_url)

    def _provider(self):
        return build_provider(self.provider_config(), cwd=self.data_dir)

    def ingest(self, path: str | Path) -> dict:
        return KnowledgeIngestor(self.database, self._provider()).ingest(path)

    def start_conversation(self, title: str = "新对话") -> int:
        return self.database.create_conversation(title, self.provider_config().name)

    def chat(self, conversation_id: int | None, user_message: str) -> dict:
        message = user_message.strip()
        if not message:
            raise ValueError("消息不能为空")
        if conversation_id is None:
            conversation_id = self.start_conversation(message[:40])
        messages, citations, system = self.context.build(conversation_id, message)
        self.database.add_message(conversation_id, "user", message)
        answer = str(self._provider().complete(messages, system)).strip()
        if not answer:
            raise RuntimeError("模型返回了空回答")
        self.database.add_message(conversation_id, "assistant", answer, citations)
        self._compact_if_needed(conversation_id)
        return {"conversation_id": conversation_id, "answer": answer, "citations": citations}

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
