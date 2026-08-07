from __future__ import annotations

import json

import pytest

from bongo.database import StudyDatabase
from bongo.exporter import export_learning_skill
from bongo.ingestion import KnowledgeIngestor, question_system_prompt, split_knowledge
from bongo.pet import GlobalInputMonitor
from bongo.providers import (
    ClaudeCodeProvider,
    ConversationProvider,
    OpenAIProvider,
    ProviderConfig,
    ProviderError,
    normalize_openai_base_url,
)
from bongo.service import LearningService


class FakeProvider(ConversationProvider):
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0
        self.messages = []
        self.systems = []

    def complete(self, messages, system, response_schema=None):
        self.calls += 1
        self.messages.append(messages)
        self.systems.append(system)
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
        service.set_chat_backend("builtin")
        result = service.chat(source_id, None, "请解释三次握手")
        assert result["conversation_id"] > 0
        assert result["source_id"] == source_id
        assert result["citations"][0]["source"] == "network.md"
        assert len(service.database.get_messages(result["conversation_id"])) == 2

        target = export_learning_skill(service.database, tmp_path / "exported-skill")
        source_index = (target / "references" / "source-index.md").read_text(encoding="utf-8")
        knowledge_file = next((target / "references" / "knowledge").glob("*.md"))
        knowledge = knowledge_file.read_text(encoding="utf-8")
        conversations = json.loads(
            (target / "references" / "conversations.json").read_text(encoding="utf-8")
        )
        assert "network.md" in source_index
        assert "TCP 使用三次握手" in knowledge
        assert conversations[0]["messages"][0]["content"] == "请解释三次握手"
        assert (target / "references" / "mistakes.md").exists()
        assert (target / "references" / "learning-profile.md").exists()
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


def test_provider_config_is_persisted_and_base_url_is_normalized(tmp_path):
    service = LearningService(tmp_path / "data")
    try:
        service.set_provider("openai", "test-model", "http://127.0.0.1:3000", "test-key")
        config = service.provider_config()
        assert config.name == "openai"
        assert config.model == "test-model"
        assert config.api_key == "test-key"
        assert config.base_url == "http://127.0.0.1:3000"
        assert normalize_openai_base_url(config.base_url) == "http://127.0.0.1:3000/v1"
        assert normalize_openai_base_url("https://example.com/openai/v1/") == "https://example.com/openai/v1"
    finally:
        service.close()


def test_bongocat_key_names_are_transiently_normalized():
    class CharacterKey:
        char = "a"

    class SpecialKey:
        char = None
        name = "space"

    assert GlobalInputMonitor._key_name(CharacterKey()) == "KeyA"
    assert GlobalInputMonitor._key_name(SpecialKey()) == "Space"


def test_question_rotation_and_wrong_answer_history(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    try:
        source_id, _ = database.add_source(tmp_path / "network.md", "TCP knowledge content for testing.")
        question_ids = database.add_questions(
            source_id,
            [
                {
                    "question": f"Question {index} about TCP?",
                    "options": ["A", "B", "C", "D"],
                    "correct_index": 0,
                    "explanation": "Because A.",
                    "evidence": "TCP evidence.",
                    "topic": "TCP",
                }
                for index in range(2)
            ],
        )
        first = database.next_question()
        second = database.next_question(exclude_id=first["id"])
        assert first["id"] != second["id"]

        result = database.answer_question(question_ids[0], 1)
        assert result["correct"] is False
        assert [item["id"] for item in database.list_wrong_questions()] == [question_ids[0]]
        assert database.list_attempts(question_ids[0])[0]["selected_index"] == 1
    finally:
        database.close()


def test_conversation_retrieval_is_scoped_to_selected_document(tmp_path):
    service = LearningService(tmp_path / "data")
    provider = FakeProvider()
    service._provider = lambda: provider
    service.set_chat_backend("builtin")
    try:
        first_id, _ = service.database.add_source(tmp_path / "first.md", "握手概念只属于第一份资料。")
        second_id, _ = service.database.add_source(tmp_path / "second.md", "握手概念只属于第二份资料。")
        service.database.replace_chunks(first_id, [{"heading": "第一", "content": "握手概念只属于第一份资料。"}])
        service.database.replace_chunks(second_id, [{"heading": "第二", "content": "握手概念只属于第二份资料。"}])

        result = service.chat(first_id, None, "解释握手概念")
        request = provider.messages[-1][-1]["content"]
        assert result["citations"][0]["source"] == "first.md"
        assert "第一份资料" in request
        assert "第二份资料" not in request
    finally:
        service.close()


def test_file_types_use_specialized_question_prompts():
    document_prompt = question_system_prompt(".md")
    code_prompt = question_system_prompt(".py")
    data_prompt = question_system_prompt(".json")
    assert "概念、论点、步骤" in document_prompt
    assert "调用关系、数据流" in code_prompt
    assert "字段语义、取值约束" in data_prompt
    assert len({document_prompt, code_prompt, data_prompt}) == 3


def test_openai_provider_retries_one_empty_structured_response():
    class Response:
        def __init__(self, text):
            self.output_text = text
            self.status = "completed"

    class Responses:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return Response("")
            return Response('{"value":"ok"}')

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = type("Client", (), {"responses": Responses()})()
    provider.model = "test-model"
    result = provider.complete(
        [{"role": "user", "content": "test"}],
        "test",
        {"type": "object", "properties": {"value": {"type": "string"}}},
    )
    assert result == {"value": "ok"}
    assert provider.client.responses.calls == 2


def test_claude_code_sends_prompt_over_stdin(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return type("Completed", (), {"returncode": 0, "stdout": "answer", "stderr": ""})()

    monkeypatch.setattr("bongo.providers.shutil.which", lambda _name: "claude")
    monkeypatch.setattr("bongo.providers.subprocess.run", fake_run)
    provider = ClaudeCodeProvider(ProviderConfig(name="claude-code"), cwd=tmp_path)
    result = provider.complete([{"role": "user", "content": "question"}], "system")
    assert result == "answer"
    assert captured["input"] == "用户: question"
    assert "用户: question" not in captured["command"]


def test_auto_chat_backend_falls_back_to_builtin(monkeypatch, tmp_path):
    service = LearningService(tmp_path / "data")
    builtin = FakeProvider()
    claude = FakeProvider(fail=True)
    service._provider = lambda: builtin
    service.set_chat_backend("auto")
    monkeypatch.setattr("bongo.providers.shutil.which", lambda _name: "claude")
    monkeypatch.setattr("bongo.service.ClaudeCodeProvider", lambda *_args, **_kwargs: claude)
    try:
        source_id, _ = service.database.add_source(tmp_path / "fallback.md", "自动回退测试材料。")
        service.database.replace_chunks(
            source_id,
            [{"heading": "回退", "content": "自动模式在外部 Agent 失败时使用内置对话后端。"}],
        )

        result = service.chat(source_id, None, "外部后端失败时怎么办？")

        assert claude.calls == 1
        assert builtin.calls == 1
        assert result["backend"] == "builtin"
        conversation = service.database.get_conversation(result["conversation_id"])
        assert conversation["provider"] == "builtin"
    finally:
        service.close()
