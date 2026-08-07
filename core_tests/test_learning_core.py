from __future__ import annotations

import json

import pytest

from bongo.database import StudyDatabase
from bongo.exporter import export_learning_skill
from bongo.ingestion import KnowledgeIngestor, split_knowledge
from bongo.providers import ConversationProvider, ProviderError
from bongo.service import LearningService


class FakeProvider(ConversationProvider):
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def complete(self, messages, system, response_schema=None):
        self.calls += 1
        if self.fail:
            raise ProviderError("planned failure")
        if response_schema:
            return {
                "questions": [
                    {
                        "question": "TCP 建立连接时为什么需要三次握手？",
                        "options": ["同步双方状态", "压缩报文", "加密文件", "关闭端口"],
                        "correct_index": 0,
                        "explanation": "三次握手用于确认双方的发送和接收能力。",
                        "evidence": "TCP 使用三次握手建立可靠连接。",
                        "topic": "TCP",
                    }
                ]
            }
        return "三次握手用于同步连接双方的初始状态。[来源1]"


def test_ingestion_can_retry_after_model_failure(tmp_path):
    knowledge = tmp_path / "network.md"
    knowledge.write_text(
        "# TCP 连接\n\nTCP 使用三次握手建立可靠连接，并同步连接双方的初始状态。",
        encoding="utf-8",
    )
    database = StudyDatabase(tmp_path / "study.db")
    try:
        with pytest.raises(ProviderError):
            KnowledgeIngestor(database, FakeProvider(fail=True)).ingest(knowledge)
        assert database.list_sources()[0]["status"] == "failed"

        provider = FakeProvider()
        result = KnowledgeIngestor(database, provider).ingest(knowledge)
        assert result == {"source_id": 1, "created": False, "reprocessed": True, "questions": 1}
        assert database.list_sources()[0]["status"] == "ready"

        duplicate = KnowledgeIngestor(database, provider).ingest(knowledge)
        assert duplicate["created"] is False
        assert duplicate["questions"] == 1
        assert provider.calls == 1
    finally:
        database.close()


def test_database_search_practice_and_conversations(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    try:
        source_id, _ = database.add_source(
            tmp_path / "network.md",
            "TCP 使用三次握手建立可靠连接，并同步双方状态。",
        )
        chunk_ids = database.replace_chunks(
            source_id,
            [{"heading": "连接建立", "content": "TCP 使用三次握手建立可靠连接，并同步双方状态。"}],
        )
        assert database.search_chunks("为什么需要三次握手")[0]["source_name"] == "network.md"

        question_id = database.add_questions(
            source_id,
            [
                {
                    "chunk_id": chunk_ids[0],
                    "question": "哪项描述正确？",
                    "options": ["三次握手", "一次握手", "无需握手", "五次握手"],
                    "correct_index": 0,
                    "explanation": "材料明确说明是三次握手。",
                    "evidence": "TCP 使用三次握手。",
                    "topic": "TCP",
                }
            ],
        )[0]
        assert database.answer_question(question_id, 0)["correct"] is True
        assert database.get_question(question_id)["ask_count"] == 1

        conversation_id = database.create_conversation("网络复习", "fake")
        database.add_message(conversation_id, "user", "解释三次握手")
        database.add_message(conversation_id, "assistant", "用于建立可靠连接")
        assert [item["role"] for item in database.get_messages(conversation_id)] == ["user", "assistant"]
    finally:
        database.close()


def test_service_chat_resume_and_skill_export(tmp_path):
    service = LearningService(tmp_path / "data")
    provider = FakeProvider()
    service._provider = lambda: provider
    try:
        source_id, _ = service.database.add_source(
            tmp_path / "network.md",
            "TCP 使用三次握手建立可靠连接，并同步连接双方的初始状态。",
        )
        service.database.replace_chunks(
            source_id,
            [{"heading": "TCP", "content": "TCP 使用三次握手建立可靠连接，并同步连接双方的初始状态。"}],
        )
        result = service.chat(None, "请解释三次握手")
        assert result["conversation_id"] > 0
        assert result["citations"][0]["source"] == "network.md"
        assert len(service.database.get_messages(result["conversation_id"])) == 2

        target = export_learning_skill(service.database, tmp_path / "exported-skill")
        knowledge = (target / "references" / "knowledge.md").read_text(encoding="utf-8")
        conversations = json.loads(
            (target / "references" / "conversations.json").read_text(encoding="utf-8")
        )
        assert "TCP 使用三次握手" in knowledge
        assert conversations[0]["messages"][0]["content"] == "请解释三次握手"
    finally:
        service.close()


def test_split_knowledge_preserves_all_text():
    content = "# 第一节\n\n" + "A" * 80 + "\n\n# 第二节\n\n" + "B" * 80
    chunks = split_knowledge(content, ".md", max_chars=100)
    merged = "\n".join(chunk["content"] for chunk in chunks)
    assert "# 第一节" in merged
    assert "# 第二节" in merged
    assert "A" * 80 in merged
    assert "B" * 80 in merged
