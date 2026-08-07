from __future__ import annotations

import json

import pytest

from bongo.database import StudyDatabase
from bongo.exporter import export_learning_skill
from bongo.ingestion import (
    KnowledgeIngestor,
    algorithm_study_system_prompt,
    question_system_prompt,
    split_knowledge,
)
from bongo.pet import GlobalInputMonitor
from bongo.providers import (
    ClaudeCodeProvider,
    CodexCliProvider,
    ConversationProvider,
    OpenAIProvider,
    ProviderConfig,
    ProviderError,
    chat_backend_available,
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


class FakeAlgorithmProvider(ConversationProvider):
    def __init__(self):
        self.system = ""
        self.messages = []

    def complete(self, messages, system, response_schema=None):
        self.system = system
        self.messages = messages
        questions = []
        topics = ["主要实现思路", "复杂度与边界"]
        for index, topic in enumerate(topics):
            questions.append(
                {
                    "question": f"两数之和专项问题 {index + 1} 应如何判断？",
                    "options": ["使用哈希表", "始终排序", "只比较相邻元素", "忽略补数"],
                    "correct_index": 0,
                    "explanation": (
                        "哈希表能够在遍历当前元素时以平均 O(1) 时间检查补数是否已经出现，"
                        "所以整体时间复杂度是 O(n)，代价是 O(n) 额外空间。排序会改变索引且需要"
                        " O(n log n)，只比较相邻元素和忽略补数都不能覆盖一般输入。"
                    ),
                    "evidence": "题解使用哈希表保存已经遍历的值及其索引。",
                    "topic": topic,
                }
            )
        return {
            "problem_title": "两数之和",
            "problem_statement": "给定整数数组和目标值，返回和为目标值的两个元素下标。",
            "solution_approach": (
                "一次遍历数组，用哈希表记录值到索引的映射。对每个元素计算补数，"
                "若补数已存在就返回两个下标；时间 O(n)，空间 O(n)。"
            ),
            "questions": questions,
        }


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
        service.set_chat_backend("default")
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

        assert database.mark_question_unanswered(question_ids[1]) is True
        assert database.mark_question_unanswered(question_ids[1]) is False
        assert [item["id"] for item in database.list_unanswered_questions()] == [question_ids[1]]
        assert database.next_question(unanswered_only=True)["id"] == question_ids[1]
        database.answer_question(question_ids[1], 0)
        assert database.list_unanswered_questions() == []
    finally:
        database.close()


def test_conversation_retrieval_is_scoped_to_selected_document(tmp_path):
    service = LearningService(tmp_path / "data")
    provider = FakeProvider()
    service._provider = lambda: provider
    service.set_chat_backend("default")
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


def test_algorithm_knowledge_records_problem_and_generates_detailed_questions(tmp_path):
    solution = tmp_path / "two-sum.md"
    solution.write_text(
        """# 两数之和

给定整数数组 nums 和目标值 target，返回两个和为 target 的元素下标。

## 题解

遍历 nums。对当前值 x 计算补数 target - x，先检查补数是否已经在哈希表中；
如果存在就返回补数的索引和当前索引，否则把 x 和当前索引加入哈希表。
这样只需一次遍历，时间复杂度 O(n)，空间复杂度 O(n)。
""",
        encoding="utf-8",
    )
    database = StudyDatabase(tmp_path / "study.db")
    provider = FakeAlgorithmProvider()
    try:
        result = KnowledgeIngestor(database, provider).ingest(solution, "code")
        source = database.get_source(result["source_id"])
        questions = database.list_questions(result["source_id"])

        assert result["questions"] == 2
        assert source["knowledge_type"] == "code"
        assert source["problem_title"] == "两数之和"
        assert "返回和为目标值" in source["problem_statement"]
        assert "时间 O(n)" in source["solution_approach"]
        assert len(questions) == 2
        assert all(len(question["explanation"]) >= 40 for question in questions)
        assert "主要实现思路" in provider.system
        assert "1 到 2 道" in provider.system
        assert "哈希表" in provider.system
        assert "题解文件：two-sum.md" in provider.messages[0]["content"]
    finally:
        database.close()


def test_same_material_can_be_imported_into_both_knowledge_modules(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    material = "同一份材料可以按文档知识或算法题解使用，去重范围必须包含知识类型。"
    try:
        document_id, document_created = database.add_source(
            tmp_path / "shared.md",
            material,
            "document",
        )
        code_id, code_created = database.add_source(
            tmp_path / "shared.md",
            material,
            "code",
        )
        duplicate_id, duplicate_created = database.add_source(
            tmp_path / "shared-copy.md",
            material,
            "code",
        )

        assert document_created is True
        assert code_created is True
        assert document_id != code_id
        assert duplicate_created is False
        assert duplicate_id == code_id
    finally:
        database.close()


def test_algorithm_prompt_requires_rationale_and_alternative_analysis():
    prompt = algorithm_study_system_prompt()
    assert "为什么不适合" in prompt
    assert "时间与空间复杂度" in prompt
    assert "两数之和" in prompt
    assert "第 1 题必须直接检验整道题的主要实现思路" in prompt
    assert "不得调用你记忆中的同名题目" in prompt
    assert "材料没有提供的信息必须明确说明未提供" in prompt


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
    provider = ClaudeCodeProvider(ProviderConfig(name="cc"), cwd=tmp_path)
    result = provider.complete([{"role": "user", "content": "question"}], "system")
    assert result == "answer"
    assert captured["input"] == "用户: question"
    assert "用户: question" not in captured["command"]


def test_default_chat_backend_uses_internal_conversation(tmp_path):
    service = LearningService(tmp_path / "data")
    internal = FakeProvider()
    service._provider = lambda: internal
    service.set_chat_backend("default")
    try:
        source_id, _ = service.database.add_source(tmp_path / "default.md", "默认后端测试材料。")
        service.database.replace_chunks(
            source_id,
            [{"heading": "默认", "content": "默认模式使用内部简易对话系统。"}],
        )

        result = service.chat(source_id, None, "默认后端如何工作？")

        assert internal.calls == 1
        assert result["backend"] == "default"
        conversation = service.database.get_conversation(result["conversation_id"])
        assert conversation["provider"] == "default"
    finally:
        service.close()


@pytest.mark.parametrize(
    ("legacy_name", "expected"),
    [("auto", "default"), ("builtin", "default"), ("claude-code", "cc")],
)
def test_legacy_chat_backend_names_are_migrated(tmp_path, legacy_name, expected):
    service = LearningService(tmp_path / legacy_name)
    try:
        service.database.set_setting("chat_backend", legacy_name)
        assert service.chat_backend() == expected
    finally:
        service.close()


def test_cli_backend_requires_executable_version_command(monkeypatch):
    monkeypatch.setattr("bongo.providers.shutil.which", lambda _name: "codex.exe")

    def denied(*_args, **_kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr("bongo.providers.subprocess.run", denied)
    assert chat_backend_available("codex") is False


def test_unavailable_codex_backend_is_rejected(monkeypatch, tmp_path):
    service = LearningService(tmp_path / "data")
    monkeypatch.setattr("bongo.service.chat_backend_available", lambda _name: False)
    try:
        with pytest.raises(ValueError, match="没有找到可执行的 Codex CLI"):
            service.set_chat_backend("codex")
        assert service.chat_backend() == "default"
    finally:
        service.close()


def test_codex_cli_sends_prompt_over_stdin_in_read_only_mode(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return type("Completed", (), {"returncode": 0, "stdout": "answer", "stderr": ""})()

    monkeypatch.setattr("bongo.providers.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("bongo.providers.subprocess.run", fake_run)
    provider = CodexCliProvider(ProviderConfig(name="codex"), cwd=tmp_path)
    result = provider.complete([{"role": "user", "content": "question"}], "system")

    assert result == "answer"
    assert captured["command"][1:4] == ["exec", "--sandbox", "read-only"]
    assert "--skip-git-repo-check" in captured["command"]
    assert captured["command"][-1] == "-"
    assert "用户: question" in captured["input"]
    assert "用户: question" not in captured["command"]


def test_pet_click_through_is_suspended_while_answering(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    pet = pet_module.PetWindow()
    pet.apply_settings(
        pet_module.PetSettings(pass_through=True),
        update_visibility=False,
    )
    transparent_flag = Qt.WindowType.WindowTransparentForInput
    assert bool(pet.windowFlags() & transparent_flag) is True

    answers = []
    pet.answer_selected.connect(lambda question_id, choice: answers.append((question_id, choice)))
    pet.show_question(
        {
            "id": 7,
            "prompt": "测试题目",
            "options": ["A", "B", "C", "D"],
        }
    )
    assert bool(pet.windowFlags() & transparent_flag) is False

    pet.option_buttons[2].click()
    assert answers == [(7, 2)]
    assert bool(pet.windowFlags() & transparent_flag) is True

    unanswered = []
    pet.question_unanswered.connect(unanswered.append)
    pet.show_question(
        {
            "id": 8,
            "prompt": "超时题目",
            "options": ["A", "B", "C", "D"],
        }
    )
    assert bool(pet.windowFlags() & transparent_flag) is False
    pet._expire_question()
    assert unanswered == [8]
    assert bool(pet.windowFlags() & transparent_flag) is True
    pet.close()
