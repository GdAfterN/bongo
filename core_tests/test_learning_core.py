from __future__ import annotations

import json
import ssl
from datetime import datetime, timedelta, timezone

import pytest

from bongo.activity import ActivityRecorder, WorkSessionTool
from bongo.database import StudyDatabase
from bongo.exporter import export_learning_skill, export_saved_learning_skill
from bongo.focus_agent import WorkBreakAgent
from bongo.news import HackerNewsClient, HackerNewsDigestGenerator, HackerNewsTool
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
        topics = ["主要实现思路", "数据结构", "边界条件"]
        focuses = ["main_approach", "data_structure", "boundary"]
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
                    "focus": focuses[index],
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


class FakeWorkBreakProvider(ConversationProvider):
    def __init__(self, action: str = "get_current_work_session"):
        self.action = action
        self.calls = []

    def complete(self, messages, system, response_schema=None):
        self.calls.append({"messages": messages, "system": system, "schema": response_schema})
        if len(self.calls) == 1:
            return {"action": self.action, "reason": "获取匿名活动事实"}
        return {
            "summary": "你已连续工作40分钟，共敲击120次键盘。",
            "activity": "活动主要集中在 code.exe，可能在进行编码工作。",
            "suggestion": "建议离开屏幕休息几分钟。",
        }


class FakeNewsProvider(ConversationProvider):
    def __init__(self):
        self.calls = []

    def complete(self, messages, system, response_schema=None):
        self.calls.append({"messages": messages, "system": system, "schema": response_schema})
        payload = json.loads(messages[0]["content"].split("：\n", 1)[1])
        index = int(payload["source_id"])
        return {
            "source_id": index,
            "title": f"中文 AI 简讯 {index}",
            "summary": (
                f"第{index}条简讯讨论人工智能系统需要解决的核心问题，并概括作者提出的主要结论。"
                "技术部分介绍模型结构、推理流程、数据处理和部署条件，说明相关设计对效率、准确性与"
                "使用体验的影响。文章同时指出方案的资源开销、适用边界及潜在应用，落地前仍需结合"
                "实际数据、硬件环境和安全要求验证。"
            ),
        }


def _fake_hn_tool(now=2_000_000):
    items = {
        index: {
            "id": index,
            "type": "story",
            "time": now - index * 600,
            "title": f"AI agent update {index}",
            "text": f"Original <b>HN</b> text {index}",
            "by": f"author{index}",
            "score": 200 - index,
            "descendants": 20 + index,
            "url": f"https://example.com/ai-{index}",
        }
        for index in range(1, 23)
    }
    items[30] = {
        "id": 30, "type": "story", "time": now - 100, "title": "Gardening tips",
        "by": "non_ai", "score": 999, "descendants": 999,
    }
    items[31] = {
        "id": 31, "type": "story", "time": now - 100,
        "title": "Deleted AI post", "deleted": True,
    }

    def fetch(path):
        if path in {"/topstories.json", "/beststories.json"}:
            return list(items)
        item_id = int(path.split("/")[-1].split(".")[0])
        return items[item_id]

    article_text = (
        "This article explains the technical architecture, implementation workflow, "
        "model inference design, data processing choices, deployment constraints, "
        "evaluation observations, limitations, and practical application scenarios. "
    ) * 5
    return HackerNewsTool(
        HackerNewsClient(
            fetch_json=fetch,
            fetch_article_text=lambda _url: article_text,
        ),
        now=lambda: now,
    )


def test_hacker_news_tool_filters_and_preserves_source_data():
    result = _fake_hn_tool().execute()
    assert len(result["items"]) == 20
    selected = next(item for item in result["items"] if item["id"] == 1)
    assert selected["title"] == "AI agent update 1"
    assert selected["text"] == "Original HN text 1"
    assert selected["discussion_url"] == "https://news.ycombinator.com/item?id=1"
    assert selected["original_url"] == "https://example.com/ai-1"


def test_article_fetch_rejects_non_public_network_addresses(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
        ],
    )
    with pytest.raises(ValueError, match="非公网"):
        HackerNewsClient._validate_public_url("http://localhost/private")


def test_hacker_news_client_retries_ssl_handshake_timeout(monkeypatch):
    import bongo.news as news_module

    calls = []
    delays = []

    class FakeResponse:
        def read(self, _limit):
            return b"[1, 2, 3]"

        def close(self):
            pass

    def flaky_urlopen(_request, timeout):
        calls.append(timeout)
        if len(calls) < 5:
            raise news_module.URLError(ssl.SSLError("handshake operation timed out"))
        return FakeResponse()

    monkeypatch.setattr(news_module, "urlopen", flaky_urlopen)
    client = HackerNewsClient(timeout=4.0, sleep=delays.append)
    assert client._fetch_json("/topstories.json") == [1, 2, 3]
    assert calls == [4.0, 4.0, 4.0, 4.0, 4.0]
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_hacker_news_client_raises_after_network_retries(monkeypatch):
    import bongo.news as news_module

    calls = []

    def failed_urlopen(_request, timeout):
        calls.append(timeout)
        raise news_module.URLError(TimeoutError("handshake operation timed out"))

    monkeypatch.setattr(news_module, "urlopen", failed_urlopen)
    client = HackerNewsClient(timeout=4.0, sleep=lambda _delay: None)
    with pytest.raises(news_module.URLError, match="handshake operation timed out"):
        client._fetch_json("/topstories.json")
    assert len(calls) == 5


def test_hacker_news_digest_directly_generates_and_backfills_source_fields():
    provider = FakeNewsProvider()
    completed = []
    result = HackerNewsDigestGenerator(provider, _fake_hn_tool()).run(
        item_completed=completed.append
    )
    assert len(provider.calls) == 20
    assert [len(digest["items"]) for digest in completed] == list(range(1, 21))
    assert result["mode"] == "direct"
    assert result["complete"] is True
    assert len(result["items"]) == 20
    first = result["items"][0]
    assert first["title"] == "中文 AI 简讯 1"
    assert first["author"] == "author1"
    assert first["original_url"] == "https://example.com/ai-1"
    assert first["original_title"] == "AI agent update 1"


def test_hacker_news_digest_sends_local_observation_to_model():
    provider = FakeNewsProvider()
    HackerNewsDigestGenerator(provider, _fake_hn_tool()).run()
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "Hacker News 单条来源事实" in prompt
    assert "AI agent update 1" in prompt
    assert "AI agent update 2" not in prompt


def test_hacker_news_digest_reports_fetch_and_model_progress():
    updates = []
    HackerNewsDigestGenerator(
        FakeNewsProvider(),
        _fake_hn_tool(),
    ).run(progress=updates.append)
    percentages = [update["percent"] for update in updates]
    assert percentages == sorted(percentages)
    assert percentages[0] == 5
    assert percentages[-1] == 100
    assert any(update["stage"] == "正在读取帖子详情" for update in updates)
    assert any(update["stage"] == "正在生成第 1/20 条" for update in updates)
    assert updates[-1]["detail"] == "已生成 20 条中文简讯，失败 0 条"


def test_hacker_news_digest_retries_model_without_refetching_sources():
    class FlakyProvider(FakeNewsProvider):
        def complete(self, messages, system, response_schema=None):
            payload = json.loads(messages[0]["content"].split("：\n", 1)[1])
            self.calls.append({"messages": messages, "system": system, "schema": response_schema})
            if payload["source_id"] == 3 and len([
                call for call in self.calls
                if json.loads(call["messages"][0]["content"].split("：\n", 1)[1])["source_id"] == 3
            ]) < 3:
                raise ProviderError("Claude Code request timed out after 180 seconds")
            return FakeNewsProvider().complete(messages, system, response_schema)

    class CountingTool:
        def __init__(self):
            self.calls = 0

        def execute(self, progress=None):
            self.calls += 1
            return _fake_hn_tool().execute(progress=progress)

    provider = FlakyProvider()
    tool = CountingTool()
    updates = []
    result = HackerNewsDigestGenerator(
        provider,
        tool,
        sleep=lambda _delay: None,
    ).run(progress=updates.append)
    assert tool.calls == 1
    assert len(provider.calls) == 22
    assert len(result["items"]) == 20
    assert sum("第 3/20 条生成失败" in update["stage"] for update in updates) == 2
    source_ids = [
        json.loads(call["messages"][0]["content"].split("：\n", 1)[1])["source_id"]
        for call in provider.calls
    ]
    assert source_ids == [1, 2, 3, 3, 3, *range(4, 21)]


def test_hacker_news_digest_does_not_retry_permanent_provider_configuration_error():
    class MissingProvider(FakeNewsProvider):
        def complete(self, messages, system, response_schema=None):
            self.calls.append({"messages": messages, "system": system, "schema": response_schema})
            raise ProviderError("Claude Code was not found in PATH")

    provider = MissingProvider()
    with pytest.raises(RuntimeError, match="已尝试 1 次"):
        HackerNewsDigestGenerator(
            provider,
            _fake_hn_tool(),
            sleep=lambda _delay: None,
        ).run()
    assert len(provider.calls) == 1


def test_ai_news_cache_refreshes_every_eight_hours(tmp_path):
    service = LearningService(tmp_path)
    try:
        digest = HackerNewsDigestGenerator(
            FakeNewsProvider(),
            _fake_hn_tool(),
        ).run()
        service.database.set_setting("ai_news_digest", json.dumps(digest, ensure_ascii=False))
        assert service.cached_ai_news()["items"][0]["author"] == "author1"
        assert service.ai_news_due(now=digest["fetched_at"] + 8 * 3600 - 1) is False
        assert service.ai_news_due(now=digest["fetched_at"] + 8 * 3600) is True
    finally:
        service.close()


def test_partial_ai_news_cache_is_readable_and_due_immediately(tmp_path):
    service = LearningService(tmp_path)
    try:
        digest = HackerNewsDigestGenerator(
            FakeNewsProvider(),
            _fake_hn_tool(),
        ).run()
        partial = {**digest, "items": digest["items"][:2], "complete": False}
        service.database.set_setting("ai_news_digest", json.dumps(partial, ensure_ascii=False))
        assert len(service.cached_ai_news()["items"]) == 2
        assert service.ai_news_due(now=digest["fetched_at"] + 1) is True
    finally:
        service.close()


def test_ai_news_read_state_is_scoped_to_current_digest(tmp_path):
    service = LearningService(tmp_path)
    try:
        digest = HackerNewsDigestGenerator(
            FakeNewsProvider(),
            _fake_hn_tool(),
        ).run()
        service.database.set_setting("ai_news_digest", json.dumps(digest, ensure_ascii=False))
        service.mark_ai_news_read(1)
        service.mark_ai_news_read(2)
        assert service.read_ai_news_ids() == {1, 2}

        next_digest = {**digest, "fetched_at": digest["fetched_at"] + 1}
        service.database.set_setting(
            "ai_news_digest",
            json.dumps(next_digest, ensure_ascii=False),
        )
        assert service.read_ai_news_ids() == set()
        assert len(service.cached_ai_news()["items"]) == 20
    finally:
        service.close()


def test_ai_news_uses_claude_code_when_cc_backend_is_selected(tmp_path, monkeypatch):
    service = LearningService(tmp_path)
    created = []

    class FakeCliProvider:
        def __init__(self, config, cwd=None):
            created.append((config.name, cwd))

    class FakeDigestGenerator:
        def __init__(self, provider, tool):
            self.provider = provider
            self.tool = tool

        def run(self, progress=None, item_completed=None):
            if progress:
                progress({"percent": 100, "stage": "抓取完成"})
            digest = {
                "source": "Hacker News",
                "fetched_at": 2_000_000,
                "mode": "direct",
                "items": [
                    {
                        "id": index,
                        "title": f"简讯 {index}",
                        "summary": f"摘要 {index}",
                        "published_at": "2026-08-13T00:00:00+00:00",
                        "author": f"author{index}",
                        "original_url": f"https://example.com/{index}",
                        "discussion_url": f"https://news.ycombinator.com/item?id={index}",
                        "original_title": f"Original {index}",
                    }
                    for index in range(1, 21)
                ],
            }
            if item_completed:
                item_completed({**digest, "items": digest["items"][:1]})
            return digest

    try:
        service.database.set_setting("chat_backend", "cc")
        monkeypatch.setattr("bongo.service.ClaudeCodeProvider", FakeCliProvider)
        monkeypatch.setattr("bongo.service.HackerNewsDigestGenerator", FakeDigestGenerator)
        result = service.fetch_ai_news(force=True)
        assert created == [("cc", service.data_dir)]
        assert len(result["items"]) == 20
        assert service.cached_ai_news()["mode"] == "direct"
    finally:
        service.close()


def test_forced_ai_news_refresh_does_not_hide_failure_with_cache(tmp_path, monkeypatch):
    service = LearningService(tmp_path)
    try:
        digest = HackerNewsDigestGenerator(FakeNewsProvider(), _fake_hn_tool()).run()
        service.database.set_setting("ai_news_digest", json.dumps(digest, ensure_ascii=False))

        class FailedGenerator:
            def __init__(self, provider, tool):
                pass

            def run(self, progress=None, item_completed=None):
                raise RuntimeError("planned fetch failure")

        monkeypatch.setattr(service, "_provider", lambda: FakeNewsProvider())
        monkeypatch.setattr("bongo.service.HackerNewsDigestGenerator", FailedGenerator)
        with pytest.raises(RuntimeError, match="planned fetch failure"):
            service.fetch_ai_news(force=True)
        assert service.cached_ai_news()["items"][0]["title"] == "中文 AI 简讯 1"
    finally:
        service.close()


def test_ai_news_persists_and_emits_each_completed_item(tmp_path, monkeypatch):
    service = LearningService(tmp_path)
    emitted = []

    class IncrementalGenerator:
        def __init__(self, provider, tool):
            pass

        def run(self, progress=None, item_completed=None):
            items = []
            for index in range(1, 4):
                items.append({
                    "id": index,
                    "title": f"简讯 {index}",
                    "summary": f"摘要 {index}",
                    "published_at": "2026-08-13T00:00:00+00:00",
                    "author": f"author{index}",
                    "original_url": f"https://example.com/{index}",
                    "discussion_url": f"https://news.ycombinator.com/item?id={index}",
                    "original_title": f"Original {index}",
                })
                digest = {
                    "source": "Hacker News",
                    "fetched_at": 2_000_000,
                    "mode": "direct",
                    "items": list(items),
                    "processed": index,
                    "total": 20,
                    "failures": [],
                    "complete": False,
                }
                item_completed(digest)
                assert len(service.cached_ai_news()["items"]) == index
            return {**digest, "complete": True}

    try:
        monkeypatch.setattr(service, "_provider", lambda: FakeNewsProvider())
        monkeypatch.setattr("bongo.service.HackerNewsDigestGenerator", IncrementalGenerator)
        result = service.fetch_ai_news(force=True, item_completed=emitted.append)
        assert [len(digest["items"]) for digest in emitted] == [1, 2, 3]
        assert result["complete"] is True
        assert len(service.cached_ai_news()["items"]) == 3
    finally:
        service.close()


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


def test_pet_question_scope_can_be_selected_by_source(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    try:
        first_id, _ = database.add_source(tmp_path / "first.md", "第一份气泡范围测试知识。")
        second_id, _ = database.add_source(tmp_path / "second.md", "第二份气泡范围测试知识。")
        first_question = database.add_questions(
            first_id,
            [{
                "question": "第一份知识应该如何回答这个测试问题？",
                "options": ["一", "二", "三", "四"],
                "correct_index": 0,
                "explanation": "选择第一项。",
                "evidence": "第一份知识。",
                "topic": "范围",
            }],
        )[0]
        second_question = database.add_questions(
            second_id,
            [{
                "question": "第二份知识应该如何回答这个测试问题？",
                "options": ["一", "二", "三", "四"],
                "correct_index": 0,
                "explanation": "选择第一项。",
                "evidence": "第二份知识。",
                "topic": "范围",
            }],
        )[0]

        database.set_source_bubble_enabled(first_id, False)
        assert database.next_question(bubble_only=True)["id"] == second_question
        assert database.next_question()["id"] == first_question

        database.set_source_bubble_enabled(second_id, False)
        assert database.next_question(bubble_only=True) is None

        database.set_source_bubble_enabled(first_id, True)
        assert database.next_question(bubble_only=True)["id"] == first_question
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
        assert "network.md" in source_index
        assert "TCP 使用三次握手" in knowledge
        insights = (target / "references" / "conversation-insights.md").read_text(encoding="utf-8")
        assert "请解释三次握手" in insights
        assert "用于同步连接双方" in insights
        assert (target / "references" / "mistakes.md").exists()
        assert (target / "references" / "learning-profile.md").exists()
        assert (target / "references" / "growth-profile.md").exists()
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


def test_global_input_monitor_deduplicates_held_keys():
    class Signals:
        pass

    monitor = GlobalInputMonitor(Signals())
    assert monitor._register_key_press("a") is True
    assert monitor._register_key_press("a") is False
    monitor._register_key_release("a")
    assert monitor._register_key_press("a") is True


def test_global_input_monitor_suppresses_only_pet_right_clicks():
    from types import SimpleNamespace

    class Signal:
        def __init__(self):
            self.values = []

        def emit(self, *values):
            self.values.append(values)

    class Signals:
        mouse_button_changed = Signal()
        global_click = Signal()
        context_requested = Signal()

    class Listener:
        def __init__(self):
            self.suppressed = 0

        def suppress_event(self):
            self.suppressed += 1

    monitor = GlobalInputMonitor(Signals())
    monitor.mouse_listener = Listener()
    monitor.set_right_click_interception(True, (100, 100, 300, 300))

    inside = SimpleNamespace(pt=SimpleNamespace(x=150, y=180))
    outside = SimpleNamespace(pt=SimpleNamespace(x=350, y=180))
    assert monitor._filter_windows_mouse_event(0x0204, inside) is False
    assert monitor._filter_windows_mouse_event(0x0205, outside) is False
    assert monitor.mouse_listener.suppressed == 2
    assert Signals.context_requested.values == [(150.0, 180.0)]
    assert Signals.mouse_button_changed.values == [
        ("right", True),
        ("right", False),
    ]

    assert monitor._filter_windows_mouse_event(0x0204, outside) is True
    assert monitor._filter_windows_mouse_event(0x0205, outside) is True
    assert monitor._filter_windows_mouse_event(0x0201, inside) is True
    monitor.set_right_click_interception(False, (100, 100, 300, 300))
    assert monitor._filter_windows_mouse_event(0x0204, inside) is True
    monitor.set_right_click_interception(True, (100, 100, 300, 300))
    assert monitor._filter_windows_mouse_event(0x0204, inside) is False
    monitor.set_right_click_interception(False, (100, 100, 300, 300))
    assert monitor._filter_windows_mouse_event(0x0205, outside) is False


def test_global_input_monitor_refreshes_native_rect_before_suppressing(monkeypatch):
    from types import SimpleNamespace

    from bongo import pet as pet_module

    class Signal:
        def __init__(self):
            self.values = []

        def emit(self, *values):
            self.values.append(values)

    class Signals:
        mouse_button_changed = Signal()
        global_click = Signal()
        context_requested = Signal()

    class Listener:
        def __init__(self):
            self.suppressed = 0

        def suppress_event(self):
            self.suppressed += 1

    monitor = GlobalInputMonitor(Signals())
    monitor.mouse_listener = Listener()
    monitor.set_right_click_interception(
        True,
        (100, 100, 300, 300),
        native_hwnd=1234,
    )
    monkeypatch.setattr(
        pet_module,
        "_windows_native_window_rect",
        lambda hwnd: (500, 500, 700, 700) if hwnd == 1234 else None,
    )

    current_position = SimpleNamespace(pt=SimpleNamespace(x=600, y=620))
    assert monitor._filter_windows_mouse_event(0x0204, current_position) is False
    assert monitor.mouse_listener.suppressed == 1
    assert monitor._pet_rect == (500, 500, 700, 700)
    assert Signals.context_requested.values == [(600.0, 620.0)]


def test_windows_listener_fallback_does_not_open_pet_context_menu(monkeypatch):
    from bongo import pet as pet_module

    monkeypatch.setattr(pet_module.os, "name", "nt")

    class Signal:
        def __init__(self):
            self.values = []

        def emit(self, *values):
            self.values.append(values)

    class Signals:
        mouse_button_changed = Signal()
        global_click = Signal()
        context_requested = Signal()

    monitor = GlobalInputMonitor(Signals())
    monitor._handle_listener_mouse_click(350, 180, "right", True)

    assert Signals.mouse_button_changed.values == [("right", True)]
    assert Signals.global_click.values == [("right", True, 350.0, 180.0)]
    assert Signals.context_requested.values == []


def test_activity_recorder_is_opt_in_and_aggregates_by_time_and_application(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    current_time = [datetime(2026, 8, 13, 9, 2, 10, tzinfo=timezone(timedelta(hours=8)))]
    applications = ["Code.exe"]
    recorder = ActivityRecorder(
        database,
        enabled=False,
        application_resolver=lambda: applications[0],
        now_provider=lambda: current_time[0],
    )
    try:
        recorder.record("keyboard")
        assert recorder.flush() == 0

        recorder.set_enabled(True)
        recorder.record("keyboard")
        recorder.record("keyboard")
        recorder.record("mouse_move")
        recorder.record("mouse_move")
        recorder.sample_foreground()
        current_time[0] += timedelta(seconds=1)
        recorder.record("mouse_move")
        recorder.record("mouse_click")
        recorder.sample_foreground()
        applications[0] = r"C:\Program Files\Google\Chrome\chrome.exe"
        current_time[0] = current_time[0].replace(minute=7)
        recorder.record("keyboard")
        recorder.sample_foreground()
        assert recorder.flush() == 2

        rows = database.list_activity_buckets("2026-08-13")
        assert len(rows) == 2
        by_application = {row["application"]: row for row in rows}
        assert by_application["code.exe"]["key_press_count"] == 2
        assert by_application["code.exe"]["mouse_active_seconds"] == 2
        assert by_application["code.exe"]["foreground_seconds"] == 2
        assert by_application["code.exe"]["mouse_click_count"] == 1
        assert by_application["chrome.exe"]["key_press_count"] == 1
        assert by_application["chrome.exe"]["foreground_seconds"] == 1
        assert "key" not in " ".join(rows[0]).replace("key_press_count", "")

        summary = database.get_daily_activity_summary("2026-08-13")
        assert [row["application"] for row in summary] == ["code.exe", "chrome.exe"]

        recorder.set_enabled(False)
        recorder.record("keyboard")
        assert recorder.flush() == 0
        assert database.clear_activity_history() == 2
        assert database.list_activity_buckets("2026-08-13") == []
    finally:
        database.close()


def test_work_session_tracks_apps_resets_after_idle_and_claims_once(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    current_time = [datetime(2026, 8, 13, 9, 0, tzinfo=timezone(timedelta(hours=8)))]
    application = ["code.exe"]
    recorder = ActivityRecorder(
        database,
        enabled=True,
        application_resolver=lambda: application[0],
        now_provider=lambda: current_time[0],
    )
    try:
        for index in range(9):
            application[0] = "code.exe" if index < 4 else "chrome.exe"
            recorder.record("keyboard" if index < 8 else "mouse_click")
            if index < 8:
                current_time[0] += timedelta(minutes=5)

        session = recorder.get_current_work_session()
        assert session["duration_seconds"] == 40 * 60
        assert session["key_press_count"] == 8
        assert session["mouse_click_count"] == 1
        assert {item["application"] for item in session["applications"]} == {
            "code.exe",
            "chrome.exe",
        }
        assert recorder.claim_break_reminder(40)["reminder_sent"] is True
        assert recorder.claim_break_reminder(40) is None

        current_time[0] += timedelta(minutes=10)
        assert recorder.get_current_work_session() is None
        recorder.record("keyboard")
        new_session = recorder.get_current_work_session()
        assert new_session["duration_seconds"] == 0
        assert new_session["key_press_count"] == 1
        assert new_session["reminder_sent"] is False
    finally:
        database.close()


def test_work_session_survives_restart_until_idle_timeout(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    current_time = [datetime(2026, 8, 13, 9, 0, tzinfo=timezone(timedelta(hours=8)))]
    application = ["code.exe"]
    try:
        recorder = ActivityRecorder(
            database,
            enabled=True,
            application_resolver=lambda: application[0],
            now_provider=lambda: current_time[0],
        )
        for index in range(9):
            application[0] = "code.exe" if index < 5 else "chrome.exe"
            recorder.record("keyboard" if index < 8 else "mouse_click")
            if index < 8:
                current_time[0] += timedelta(minutes=5)
        assert recorder.flush() == 9
        assert recorder.claim_break_reminder(40) is not None
        saved_started_at = recorder.get_current_work_session()["started_at"]

        current_time[0] += timedelta(minutes=5)
        restored = ActivityRecorder(
            database,
            enabled=True,
            application_resolver=lambda: application[0],
            now_provider=lambda: current_time[0],
        )
        session = restored.get_current_work_session()
        assert session["started_at"] == saved_started_at
        assert session["duration_seconds"] == 45 * 60
        assert session["key_press_count"] == 8
        assert session["mouse_click_count"] == 1
        assert session["reminder_sent"] is True
        assert restored.claim_break_reminder(40) is None
        assert {item["application"] for item in session["applications"]} == {
            "code.exe",
            "chrome.exe",
        }

        current_time[0] += timedelta(minutes=5)
        expired = ActivityRecorder(
            database,
            enabled=True,
            now_provider=lambda: current_time[0],
        )
        assert expired.get_current_work_session() is None
        assert database.get_setting(ActivityRecorder.SESSION_STATE_SETTING) == ""
    finally:
        database.close()


def test_work_session_ignores_invalid_saved_state_and_clears_when_disabled(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    current_time = [datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)]
    try:
        database.set_setting(ActivityRecorder.SESSION_STATE_SETTING, "{invalid json")
        recorder = ActivityRecorder(
            database,
            enabled=True,
            now_provider=lambda: current_time[0],
        )
        assert recorder.get_current_work_session() is None
        assert database.get_setting(ActivityRecorder.SESSION_STATE_SETTING) == ""

        recorder.record("keyboard")
        recorder.flush()
        assert database.get_setting(ActivityRecorder.SESSION_STATE_SETTING)
        recorder.set_enabled(False)
        assert recorder.get_current_work_session() is None
        assert database.get_setting(ActivityRecorder.SESSION_STATE_SETTING) == ""
    finally:
        database.close()


def test_work_session_tool_and_agent_use_trigger_snapshot(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    recorder = ActivityRecorder(database, enabled=True)
    snapshot = {
        "started_at": "2026-08-13T09:00:00+08:00",
        "last_activity_at": "2026-08-13T09:40:00+08:00",
        "duration_seconds": 2400,
        "idle_seconds": 0,
        "key_press_count": 120,
        "mouse_active_seconds": 300,
        "mouse_click_count": 20,
        "applications": [{"application": "code.exe", "key_press_count": 120}],
        "reminder_sent": True,
    }
    try:
        provider = FakeWorkBreakProvider()
        result = WorkBreakAgent(provider, WorkSessionTool(recorder, snapshot)).run()

        assert len(provider.calls) == 2
        assert result["observation"]["applications"] == [
            {"application": "Visual Studio Code", "key_press_count": 120}
        ]
        assert snapshot["applications"][0]["application"] == "code.exe"
        assert "Visual Studio Code" in provider.calls[1]["messages"][0]["content"]
        assert result["report"]["suggestion"] == "建议离开屏幕休息几分钟。"
    finally:
        database.close()


def test_work_break_agent_rejects_unregistered_tool(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    recorder = ActivityRecorder(database, enabled=True)
    try:
        with pytest.raises(ValueError, match="unregistered tool"):
            WorkBreakAgent(
                FakeWorkBreakProvider("read_keyboard_content"),
                WorkSessionTool(recorder),
            ).run()
    finally:
        database.close()


def test_activity_timeline_groups_five_minute_buckets_into_half_hours():
    from PySide6.QtWidgets import QApplication

    from bongo.widgets import ActivityTimelineWidget

    QApplication.instance() or QApplication([])
    widget = ActivityTimelineWidget()
    widget.set_activity(
        [
            {
                "bucket_start": "2026-08-13T09:05:00+08:00",
                "application": "code.exe",
                "key_press_count": 20,
            },
            {
                "bucket_start": "2026-08-13T09:20:00+08:00",
                "application": "chrome.exe",
                "key_press_count": 10,
            },
            {
                "bucket_start": "2026-08-13T09:35:00+08:00",
                "application": "code.exe",
                "key_press_count": 15,
            },
        ]
    )

    assert [item["total"] for item in widget._bins] == [30, 15]
    assert widget._bins[0]["counts"] == {
        "Visual Studio Code": 20,
        "Google Chrome": 10,
    }
    assert widget._applications == ["Visual Studio Code", "Google Chrome"]


def test_daily_work_seconds_merges_activity_until_ten_minute_break():
    from bongo.app import MainWindow

    rows = [
        {
            "first_activity_at": "2026-08-13T09:00:00+08:00",
            "last_activity_at": "2026-08-13T09:05:00+08:00",
        },
        {
            "first_activity_at": "2026-08-13T09:12:00+08:00",
            "last_activity_at": "2026-08-13T09:20:00+08:00",
        },
        {
            "first_activity_at": "2026-08-13T09:30:00+08:00",
            "last_activity_at": "2026-08-13T09:40:00+08:00",
        },
    ]

    assert MainWindow._daily_work_seconds(rows) == 30 * 60


def test_dashboard_weekly_activity_series_combines_keys_and_work_time(tmp_path):
    from bongo.qml_bridge import BongoBridge

    database = StudyDatabase(tmp_path / "study.db")
    database.add_activity_buckets(
        [
            {
                "activity_date": "2026-08-13",
                "bucket_start": "2026-08-13T09:00:00+08:00",
                "application": "code.exe",
                "key_press_count": 120,
                "mouse_active_seconds": 20,
                "mouse_click_count": 2,
                "first_activity_at": "2026-08-13T09:00:00+08:00",
                "last_activity_at": "2026-08-13T09:05:00+08:00",
            },
            {
                "activity_date": "2026-08-13",
                "bucket_start": "2026-08-13T09:10:00+08:00",
                "application": "chrome.exe",
                "key_press_count": 80,
                "mouse_active_seconds": 10,
                "mouse_click_count": 1,
                "first_activity_at": "2026-08-13T09:10:00+08:00",
                "last_activity_at": "2026-08-13T09:25:00+08:00",
            },
        ]
    )
    try:
        series = BongoBridge._weekly_activity_series(database, "2026-08-13")
        assert len(series) == 7
        assert series[-1] == {
            "label": "08/13",
            "dateLabel": "8月13日 · 周四",
            "keys": 200,
            "workSeconds": 25 * 60,
            "workLabel": "25分钟",
            "simulated": False,
        }
        assert [item["keys"] for item in series[:5]] == [1680, 2350, 1940, 3120, 2760]
        assert all(item["simulated"] for item in series[:5])
        assert series[-2]["keys"] == 0
        assert series[-2]["workSeconds"] == 0
        assert series[-2]["simulated"] is False
    finally:
        database.close()


def test_application_names_and_usage_series_are_consistent():
    from bongo.application_names import display_application_name
    from bongo.qml_bridge import BongoBridge

    assert display_application_name("chatgpt.exe") == "ChatGPT"
    assert display_application_name("chargpt.exe") == "ChatGPT"
    assert display_application_name("weixin.exe") == "微信"
    assert display_application_name("wechat.exe") == "微信"
    assert display_application_name(r"C:\Program Files\App\custom-tool.exe") == "custom-tool"

    series = BongoBridge._application_usage_series(
        [
            {
                "application": "chatgpt.exe",
                "foreground_seconds": 125,
                "mouse_active_seconds": 60,
            },
            {
                "application": "weixin.exe",
                "foreground_seconds": 0,
                "mouse_active_seconds": 60,
            },
            {
                "application": "wechat.exe",
                "foreground_seconds": 35,
                "mouse_active_seconds": 10,
            },
        ],
    )
    assert series == [
        {"label": "ChatGPT", "seconds": 125, "duration": "2分钟"},
        {"label": "微信", "seconds": 35, "duration": "0分钟"},
    ]
    assert sum(item["seconds"] for item in series) == 160

    ranking = BongoBridge._application_keystroke_series(
        [
            {"application": "chatgpt.exe", "key_press_count": 120},
            {"application": "chargpt.exe", "key_press_count": 30},
            {"application": "weixin.exe", "key_press_count": 240},
            {"application": "pythonw.exe", "key_press_count": 0},
        ]
    )
    assert ranking == [
        {"label": "微信", "value": 240, "valueLabel": "240"},
        {"label": "ChatGPT", "value": 150, "valueLabel": "150"},
    ]


def test_qml_window_restore_forces_windowed_visible_and_active():
    from PySide6.QtGui import QWindow

    from bongo.qml_bridge import BongoBridge

    class FakeWindow:
        def __init__(self):
            self.calls = []

        def setVisibility(self, value):
            self.calls.append(("visibility", value))

        def setVisible(self, value):
            self.calls.append(("visible", value))

        def raise_(self):
            self.calls.append(("raise", None))

        def requestActivate(self):
            self.calls.append(("activate", None))

    window = FakeWindow()
    BongoBridge._activate_window(window)

    assert window.calls == [
        ("visibility", QWindow.Visibility.Windowed),
        ("visible", True),
        ("raise", None),
        ("activate", None),
    ]


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


def test_learning_skill_selects_sources_and_tracks_dirty_state(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    try:
        first_id, _ = database.add_source(tmp_path / "first.md", "第一份知识。")
        second_id, _ = database.add_source(tmp_path / "second.md", "第二份知识。")
        question_id = database.add_questions(
            first_id,
            [{
                "question": "第一份知识的题目？",
                "options": ["正确", "错误", "未知", "无关"],
                "correct_index": 0,
                "explanation": "第一份知识明确说明了正确结论。",
                "evidence": "第一份知识。",
                "topic": "第一主题",
            }],
        )[0]
        skill_id = database.create_learning_skill(
            "review-first",
            "第一份复习",
            "复习第一份知识并纠正错题。",
            [first_id],
        )
        skill = database.get_learning_skill(skill_id)
        assert skill["source_ids"] == [first_id]
        assert skill["dirty"] == 1

        database.answer_question(question_id, 1)
        assert database.get_learning_skill(skill_id)["dirty"] == 1
        target = export_saved_learning_skill(database, skill_id, tmp_path / "review-first")
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["version"] == 1
        assert manifest["sources"][0]["id"] == first_id
        questions = json.loads((target / "references" / "questions.json").read_text(encoding="utf-8"))
        assert {item["source_id"] for item in questions} == {first_id}
        assert database.get_learning_skill(skill_id)["dirty"] == 0

        database.answer_question(question_id, 0)
        assert database.get_learning_skill(skill_id)["dirty"] == 1
        export_saved_learning_skill(database, skill_id, target)
        assert database.get_learning_skill(skill_id)["version"] == 2
        assert database.list_learning_events([first_id])
    finally:
        database.close()


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

        assert result["questions"] == 3
        assert source["knowledge_type"] == "code"
        assert source["problem_title"] == "两数之和"
        assert source["problem_statement"].startswith("题目名称：两数之和\n\n算法题简要摘要：")
        assert "返回和为目标值" in source["problem_statement"]
        assert "时间 O(n)" in source["solution_approach"]
        assert len(questions) == 3
        assert all(len(question["explanation"]) >= 40 for question in questions)
        assert "主要实现思路" in provider.system
        assert "固定生成 3 道" in provider.system
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


def test_existing_algorithm_source_with_old_question_count_is_reprocessed(tmp_path):
    solution = tmp_path / "two-sum.md"
    solution.write_text(
        "两数之和：遍历数组，用哈希表保存已经出现的值和下标，并查找目标值对应的补数。",
        encoding="utf-8",
    )
    database = StudyDatabase(tmp_path / "study.db")
    try:
        source_id, _ = database.add_source(solution, solution.read_text(encoding="utf-8"), "code")
        database.replace_chunks(source_id, [{"heading": "题解", "content": solution.read_text(encoding="utf-8")}])
        database.add_questions(
            source_id,
            [
                {
                    "question": "旧版本为什么使用哈希表完成补数查找？",
                    "options": ["快速查找", "排序", "递归", "回溯"],
                    "correct_index": 0,
                    "explanation": "旧版本题目。",
                    "evidence": "使用哈希表。",
                    "topic": "旧版本",
                }
            ],
        )
        database.set_source_status(source_id, "ready")

        result = KnowledgeIngestor(database, FakeAlgorithmProvider()).ingest(solution, "code")

        assert result["created"] is False
        assert result["reprocessed"] is True
        assert result["questions"] == 3
        assert len(database.list_questions(source_id)) == 3
    finally:
        database.close()


def test_algorithm_question_prompt_includes_title_for_existing_questions(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    try:
        source_id, _ = database.add_source(
            tmp_path / "symmetric-tree.java",
            "完整的对称二叉树递归实现代码，用于测试旧题目的显示兼容逻辑。",
            "code",
        )
        database.set_source_algorithm_metadata(
            source_id,
            "对称二叉树",
            "判断一棵二叉树是否轴对称。",
            "递归比较镜像位置的节点。",
        )
        question_id = database.add_questions(
            source_id,
            [
                {
                    "question": "该实现的主要递归过程是什么？",
                    "options": ["镜像比较", "层序求和", "中序排序", "统计节点"],
                    "correct_index": 0,
                    "explanation": "递归比较左右子树中处于镜像位置的节点。",
                    "evidence": "代码交叉比较左右孩子。",
                    "topic": "主要实现思路",
                }
            ],
        )[0]

        assert database.get_question(question_id)["prompt"].startswith(
            "在《对称二叉树》这道题中，"
        )
        assert database.list_questions(source_id)[0]["prompt"].startswith(
            "在《对称二叉树》这道题中，"
        )
    finally:
        database.close()


def test_algorithm_prompt_requires_rationale_and_alternative_analysis():
    prompt = algorithm_study_system_prompt()
    assert "为什么不适合" in prompt
    assert "时间与空间复杂度" in prompt
    assert "两数之和" in prompt
    assert "第 1 题的 focus 必须是 main_approach" in prompt
    assert "第 2 题的 focus 必须是 data_structure" in prompt
    assert "第 3 题的 focus 必须是 boundary" in prompt
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


def test_openai_provider_retries_transient_overload(monkeypatch):
    class OverloadedError(Exception):
        status_code = 400
        body = {"type": "upstream_error", "message": "servers are currently overloaded"}

    class Response:
        output_text = "answer"

    class Responses:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls < 3:
                raise OverloadedError("servers are currently overloaded")
            return Response()

    delays = []
    monkeypatch.setattr("bongo.providers.time.sleep", delays.append)
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = type("Client", (), {"responses": Responses()})()
    provider.model = "test-model"

    assert provider.complete([{"role": "user", "content": "test"}], "test") == "answer"
    assert provider.client.responses.calls == 3
    assert delays == [1.0, 2.0]


def test_openai_provider_shows_friendly_error_after_overload_retries(monkeypatch):
    class OverloadedError(Exception):
        status_code = 400
        body = {"type": "upstream_error"}

    class Responses:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            raise OverloadedError("servers are currently overloaded")

    monkeypatch.setattr("bongo.providers.time.sleep", lambda _seconds: None)
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = type("Client", (), {"responses": Responses()})()
    provider.model = "test-model"

    with pytest.raises(ProviderError, match="已自动重试 3 次，请稍后再试"):
        provider.complete([{"role": "user", "content": "test"}], "test")
    assert provider.client.responses.calls == 3


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

    application = QApplication.instance() or QApplication([])
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


def test_pet_action_bubble_actions_are_manual_and_emit_navigation(monkeypatch):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    loaded = []

    def load_question():
        loaded.append(True)
        return {"id": 9, "prompt": "手动题目", "options": ["A", "B", "C", "D"]}

    pet = pet_module.PetWindow(load_question)
    dashboard_requests = []
    statistics_requests = []
    news_requests = []
    pet.open_dashboard_requested.connect(lambda: dashboard_requests.append(True))
    pet.show_statistics_requested.connect(lambda: statistics_requests.append(True))
    pet.show_ai_news_requested.connect(lambda: news_requests.append(True))

    assert loaded == []
    pet.show()
    QApplication.processEvents()
    pet._popup_context_menu(pet.canvas.mapToGlobal(pet.canvas.rect().center()))
    assert pet.action_bubble.isVisible()
    assert [button.label for button in pet.action_buttons] == ["来一题", "今日统计", "AI资讯", "仪表盘"]
    pet.action_buttons[0]._animate(1.0)
    QTest.qWait(220)
    assert pet.action_buttons[0].hoverProgress > 0.95
    pet.action_buttons[0].click()
    assert loaded == [True]
    assert pet.current_question["id"] == 9
    assert isinstance(pet.bubble, pet_module.QuestionSpeechBubble)
    assert pet.bubble.layout().contentsMargins().bottom() == 25
    pet._popup_context_menu(pet.canvas.mapToGlobal(pet.canvas.rect().center()))
    pet.action_buttons[1].click()
    pet._popup_context_menu(pet.canvas.mapToGlobal(pet.canvas.rect().center()))
    pet.action_buttons[2].click()
    pet._popup_context_menu(pet.canvas.mapToGlobal(pet.canvas.rect().center()))
    pet.action_buttons[3].click()
    assert dashboard_requests == [True]
    assert statistics_requests == [True]
    assert news_requests == [True]
    pet.close()


def test_pet_action_bubble_opens_to_left_of_cat(monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    pet = pet_module.PetWindow()
    pet.move(500, 350)
    pet.show()
    application.processEvents()

    pet._popup_context_menu(pet.canvas.mapToGlobal(pet.canvas.rect().center()))
    application.processEvents()

    canvas_top_left = pet.canvas.mapToGlobal(QPoint(0, 0))
    cat_anchor_x = canvas_top_left.x() + round(pet.canvas.width() * 0.58)
    bubble_geometry = pet.action_bubble.frameGeometry()
    assert bubble_geometry.center().x() < cat_anchor_x
    assert abs(
        bubble_geometry.left()
        + round(pet.action_bubble.width() * 0.92)
        - cat_anchor_x
    ) <= 1
    available = pet.screen().availableGeometry()
    assert bubble_geometry.left() >= available.left()
    assert bubble_geometry.right() <= available.right()
    pet.close()


def test_pet_primary_and_work_bubbles_stay_left_at_right_screen_edge(monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

        def look_at(self, _x, _y):
            pass

    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    pet = pet_module.PetWindow()
    pet.apply_settings(
        pet_module.PetSettings(
            scale=50,
            keep_in_screen=False,
            pass_through=True,
            mouse_enabled=False,
        ),
        update_visibility=False,
    )
    pet.show()
    application.processEvents()

    bounds = pet.screen().availableGeometry()
    canvas_right_in_pet = pet.canvas.geometry().right()
    pet.move(
        bounds.right() - canvas_right_in_pet - 8,
        bounds.top() + min(300, max(20, bounds.height() // 3)),
    )
    application.processEvents()
    canvas_top_left = pet.canvas.mapToGlobal(QPoint(0, 0))
    canvas_right = canvas_top_left.x() + pet.canvas.width() - 1
    assert canvas_right <= bounds.right()

    def assert_left_overlay(overlay):
        geometry = overlay.frameGeometry()
        canvas_center_x = canvas_top_left.x() + pet.canvas.width() // 2
        assert overlay.isWindow()
        assert geometry.left() >= bounds.left()
        assert geometry.right() <= bounds.right()
        assert geometry.top() >= bounds.top()
        assert geometry.bottom() <= bounds.bottom()
        assert geometry.center().x() < canvas_center_x

    pet.show_question(
        {"id": 501, "prompt": "贴边题目", "options": ["A", "B", "C", "D"]}
    )
    application.processEvents()
    assert_left_overlay(pet.bubble)
    assert pet.bubble._tail_tip_ratio > 0.75
    assert pet.canvas.mapToGlobal(QPoint(0, 0)) == canvas_top_left
    pet._expire_question()

    pet.show_statistics("1小时", 1234, "code.exe", timeout_ms=60_000)
    application.processEvents()
    assert_left_overlay(pet.bubble)
    pet._hide_message_bubble()

    pet.show_ai_news(
        {
            "id": 502,
            "title": "贴边资讯",
            "summary": "摘要",
            "author": "作者",
            "published_at_display": "2026-08-15 09:00",
        },
        timeout_ms=60_000,
    )
    application.processEvents()
    assert_left_overlay(pet.bubble)
    pet._hide_message_bubble()

    canvas_center = pet.canvas.mapToGlobal(pet.canvas.rect().center())
    pet._follow_mouse(canvas_center.x(), canvas_center.y())
    application.processEvents()
    assert_left_overlay(pet.work_session_badge)
    assert pet.work_session_badge._tail_tip_ratio > 0.75
    pet.close()


def test_pet_news_bubble_hides_link_and_opens_detail(monkeypatch):
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
    requested = []
    read_requested = []
    pet.news_detail_requested.connect(requested.append)
    pet.news_read_requested.connect(read_requested.append)
    pet.show_ai_news(
        {
            "id": 42,
            "title": "中文简讯标题",
            "summary": "一段严格限制长度的中文简讯摘要。",
            "published_at_display": "2026-08-13 10:00",
            "author": "alice",
            "original_url": "https://example.com/source",
        }
    )
    assert "中文简讯标题" in pet.question_label.text()
    assert "一段严格限制长度" in pet.question_label.text()
    assert "https://example.com/source" not in pet.question_label.text()
    assert not pet.news_read_button.isHidden()
    pet.question_label.clicked.emit()
    assert requested == [42]
    pet.news_read_button.click()
    assert read_requested == [42]
    assert not pet.bubble.isVisible()
    pet.close()


def test_pet_news_bubble_shows_full_summary(monkeypatch):
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
    summary = "这是一段用于验证气泡完整展示的中文摘要。" * 8
    pet.show_ai_news({
        "id": 43,
        "title": "中文简讯标题",
        "summary": summary,
        "published_at_display": "2026-08-13 10:00",
        "author": "alice",
    })
    assert summary in pet.question_label.text()
    assert pet.bubble_scroll.height() == 184
    pet.close()


def test_pet_work_session_badge_follows_global_hover_with_pass_through(monkeypatch):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

        def look_at(self, _x, _y):
            pass

    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    pet = pet_module.PetWindow()
    pet.apply_settings(
        pet_module.PetSettings(pass_through=True, mouse_enabled=False),
        update_visibility=False,
    )
    pet.move(100, 100)
    pet.show()
    application.processEvents()
    pet.set_work_session_tooltip(
        {
            "duration_seconds": 25 * 60,
            "idle_seconds": 30,
            "key_press_count": 321,
        }
    )

    canvas_center = pet.canvas.mapToGlobal(pet.canvas.rect().center())
    pet._follow_mouse(canvas_center.x(), canvas_center.y())
    assert pet.work_session_badge.isVisible()
    assert "25分钟" in pet.work_session_badge.text()
    assert "321" in pet.work_session_badge.text()
    assert "空闲" not in pet.work_session_badge.text()
    assert "#167a55" in pet.work_session_badge.text()
    assert "#d56a1f" in pet.work_session_badge.text()
    canvas_top = pet.canvas.mapToGlobal(QPoint(0, 0)).y()
    assert pet.work_session_badge.frameGeometry().bottom() <= canvas_top + 8

    pet.apply_settings(
        pet_module.PetSettings(
            pass_through=True,
            mouse_enabled=False,
            opacity=55,
        ),
        update_visibility=False,
    )
    assert pet.windowOpacity() == pytest.approx(0.55, abs=0.005)
    assert pet.work_session_badge.parent() is pet

    outside = pet.mapToGlobal(QPoint(-20, -20))
    pet._follow_mouse(outside.x(), outside.y())
    assert not pet.work_session_badge.isVisible()
    assert bool(
        pet.windowFlags() & Qt.WindowType.WindowTransparentForInput
    ) is True
    pet.close()


def test_pet_hover_badge_stays_hidden_during_primary_bubble_window_rebuild(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

        def look_at(self, _x, _y):
            pass

    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    pet = pet_module.PetWindow()
    pet.apply_settings(
        pet_module.PetSettings(pass_through=True, mouse_enabled=False),
        update_visibility=False,
    )
    pet.move(100, 100)
    pet.show()
    application.processEvents()
    assert pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert pet.bubble.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert pet.work_session_badge.testAttribute(
        Qt.WidgetAttribute.WA_TranslucentBackground
    )

    canvas_center = pet.canvas.mapToGlobal(pet.canvas.rect().center())
    pet._follow_mouse(canvas_center.x(), canvas_center.y())
    assert pet.work_session_badge.isVisible()

    pet.show_message("主气泡", timeout_ms=60_000)
    assert pet.bubble.isVisible()
    assert pet.work_session_badge.isHidden()

    pet.hide()
    assert not pet.bubble.isVisible()
    assert pet.bubble.isHidden()
    pet._follow_mouse(canvas_center.x(), canvas_center.y())
    assert pet.work_session_badge.isHidden()

    pet.show()
    application.processEvents()
    assert pet.bubble.isVisible()
    assert pet.work_session_badge.isHidden()
    assert not bool(
        pet.windowFlags() & Qt.WindowType.WindowTransparentForInput
    )
    pet.close()


def test_pet_message_rich_text_is_explicit(monkeypatch):
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
    pet.show_message("<b>普通文本</b>")
    assert pet.question_label.textFormat() == Qt.TextFormat.PlainText
    assert bool(pet.windowFlags() & transparent_flag) is False
    pet.show_message("<b>统计数据</b>", rich_text=True)
    assert pet.question_label.textFormat() == Qt.TextFormat.RichText
    assert bool(pet.windowFlags() & transparent_flag) is False
    pet._hide_message_bubble()
    assert bool(pet.windowFlags() & transparent_flag) is True
    pet.close()


def test_pet_statistics_uses_compact_layout_without_scrollbar(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    pet = pet_module.PetWindow()
    pet.apply_settings(
        pet_module.PetSettings(pass_through=True),
        update_visibility=False,
    )
    transparent_flag = Qt.WindowType.WindowTransparentForInput
    pet.show()
    application.processEvents()

    pet.show_statistics("2小时11分钟", 6602, "chatgpt.exe")
    application.processEvents()

    assert pet.statistics_panel.isVisible()
    assert not pet.question_label.isVisible()
    assert pet.statistics_duration.text() == "2小时11分钟"
    assert pet.statistics_keys.text() == "6,602"
    assert pet.statistics_application.text() == "ChatGPT"
    assert pet.statistics_application.toolTip() == "ChatGPT"
    assert pet._application_display_name("CODE.EXE") == "Visual Studio Code"
    assert pet._application_display_name("node.js") == "node.js"
    assert (
        pet.bubble_scroll.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert pet.bubble_scroll.height() == 132
    assert bool(pet.windowFlags() & transparent_flag) is False

    pet.show_question(
        {"id": 10, "prompt": "下一道题", "options": ["A", "B", "C", "D"]}
    )
    assert not pet.statistics_panel.isVisible()
    assert pet.question_label.isVisible()
    assert (
        pet.bubble_scroll.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    assert pet.bubble_scroll.height() == 184
    pet.close()


def test_algorithm_question_bubble_separates_title_and_removes_duplicate_prefixes():
    from bongo.pet import PetWindow

    question = {
        "knowledge_type": "code",
        "problem_title": "合并两个有序链表",
        "problem_statement": (
            "题目名称：合并两个有序链表\n\n"
            "算法题简要摘要：将两个升序链表合并为一个新的升序链表。"
        ),
        "topic": "边界条件",
        "prompt": (
            "在《合并两个有序链表》这道题中，"
            "在《合并两个有序链表》这道题中，当主循环结束时，哪种处理是正确的？"
        ),
    }

    assert PetWindow._algorithm_prompt(question) == "当主循环结束时，哪种处理是正确的？"
    assert PetWindow._algorithm_statement(question) == "将两个升序链表合并为一个新的升序链表。"
    rendered = PetWindow._algorithm_question_html(question)
    assert rendered.count("合并两个有序链表") == 1
    assert "算法题" in rendered
    assert "边界条件" in rendered
    assert "#315f7c" in rendered
    assert "#b85b1d" in rendered

    question["prompt"] = "在《合并两个有序链表》中，主循环应该如何推进？"
    assert PetWindow._algorithm_prompt(question) == "主循环应该如何推进？"
    question["prompt"] = "对于《合并两个有序链表》，应使用哪种数据结构？"
    assert PetWindow._algorithm_prompt(question) == "应使用哪种数据结构？"


def test_pet_canvas_passes_mouse_input_to_window_and_local_context_menu_works(monkeypatch):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
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
    pet.move(100, 100)
    pet.show()
    QApplication.processEvents()

    assert pet.canvas.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    popup_positions = []
    monkeypatch.setattr(pet, "_popup_context_menu", popup_positions.append)
    right_press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(200, 300),
        QPointF(300, 400),
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )
    pet.mousePressEvent(right_press)
    assert popup_positions == [right_press.globalPosition().toPoint()]

    left_press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(200, 300),
        QPointF(300, 400),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    pet.mousePressEvent(left_press)
    assert not pet._drag_offset.isNull()
    left_move = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(220, 320),
        QPointF(360, 460),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    pet.mouseMoveEvent(left_move)
    assert pet.pos() == QPoint(160, 160)
    pet.close()


def test_pet_global_drag_and_right_click_work_when_pass_through_is_disabled(monkeypatch):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

        def look_at(self, _x, _y):
            pass

    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    pet = pet_module.PetWindow()
    pet.apply_settings(pet_module.PetSettings(pass_through=False), update_visibility=False)
    pet.move(100, 100)
    pet.show()
    application.processEvents()

    assert not bool(pet.windowFlags() & Qt.WindowType.WindowTransparentForInput)
    pet._sync_right_click_interception()
    assert pet.monitor._intercept_right_click is True

    center = pet.canvas.mapToGlobal(pet.canvas.rect().center())
    pet._handle_global_click("left", True, center.x(), center.y())
    assert not pet._global_drag_offset.isNull()
    target = QPoint(center.x() + 45, center.y() + 30)
    monkeypatch.setattr(pet_module.QCursor, "pos", lambda: target)
    pet._follow_native_mouse(target.x(), target.y())
    assert pet.pos() == QPoint(145, 130)
    pet._handle_global_click("left", False, target.x(), target.y())
    assert pet._global_drag_offset.isNull()

    native_rect = (300, 300, 700, 700)
    monkeypatch.setattr(pet, "_native_canvas_rect", lambda: native_rect)
    monkeypatch.setattr(pet_module.QCursor, "pos", lambda: QPoint(450, 450))
    pet._show_context_menu_at_native(450, 450)
    application.processEvents()
    assert pet.action_bubble.isVisible()
    pet.close()


def test_pet_action_bubble_closes_on_global_left_click_outside(monkeypatch):
    from PySide6.QtCore import QPoint, Qt
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
    pet.show()
    pet._popup_context_menu(QPoint(200, 200))
    QApplication.processEvents()
    assert pet.action_bubble.isVisible()

    bubble_center = pet.action_bubble.mapToGlobal(pet.action_bubble.rect().center())
    pet._handle_global_click("left", True, bubble_center.x(), bubble_center.y())
    assert pet.action_bubble.isVisible()
    pet._handle_global_click("right", True, 0, 0)
    assert pet.action_bubble.isVisible()
    pet._handle_global_click("left", True, 0, 0)
    assert not pet.action_bubble.isVisible()
    pet.close()


def test_pet_global_click_outside_canvas_and_bubble_closes_each_bubble(monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

        def look_at(self, _x, _y):
            pass

    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    pet = pet_module.PetWindow()
    pet.apply_settings(
        pet_module.PetSettings(pass_through=True, mouse_enabled=False),
        update_visibility=False,
    )
    pet.move(300, 300)
    pet.show()
    application.processEvents()

    outside = pet.mapToGlobal(QPoint(-80, -80))
    unanswered = []
    pet.question_unanswered.connect(unanswered.append)
    pet.show_question(
        {"id": 91, "prompt": "外部点击测试", "options": ["A", "B", "C", "D"]}
    )
    bubble_center = pet.bubble.mapToGlobal(pet.bubble.rect().center())
    pet._handle_global_click("left", True, bubble_center.x(), bubble_center.y())
    assert pet.bubble.isVisible()
    assert pet._question_pending

    pet._handle_global_click("left", True, outside.x(), outside.y())
    assert pet.bubble.isHidden()
    assert not pet._question_pending
    assert unanswered == [91]

    pet.show_statistics("1小时", 1234, "code.exe", timeout_ms=60_000)
    assert pet.bubble.isVisible()
    pet._handle_global_click("left", True, outside.x(), outside.y())
    assert pet.bubble.isHidden()
    assert not pet._message_pending

    pet.show_ai_news(
        {
            "id": 92,
            "title": "AI 简讯",
            "summary": "摘要",
            "author": "作者",
            "published_at_display": "2026-08-14 14:00",
        },
        timeout_ms=60_000,
    )
    assert pet.bubble.isVisible()
    pet._handle_global_click("left", True, outside.x(), outside.y())
    assert pet.bubble.isHidden()

    canvas_center = pet.canvas.mapToGlobal(pet.canvas.rect().center())
    pet._follow_mouse(canvas_center.x(), canvas_center.y())
    assert pet.work_session_badge.isVisible()
    pet._handle_global_click("left", True, outside.x(), outside.y())
    assert pet.work_session_badge.isHidden()
    pet.close()


def test_pet_uses_native_canvas_rect_and_rejects_outside_right_click(monkeypatch):
    from PySide6.QtCore import QPoint, Qt
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
    pet.show()
    QApplication.processEvents()
    pet.apply_settings(
        pet_module.PetSettings(pass_through=True),
        update_visibility=False,
    )

    pet.move(100, 100)
    QApplication.processEvents()

    qt_center = pet.canvas.mapToGlobal(pet.canvas.rect().center())
    monkeypatch.setattr(pet_module.QCursor, "pos", lambda: qt_center)
    expected = (200, 300, 800, 700)
    monkeypatch.setattr(pet, "_native_canvas_rect", lambda: expected)
    pet._show_context_menu_at_native(9999, 9999)
    QApplication.processEvents()
    assert not pet.action_bubble.isVisible()

    pet._show_context_menu_at_native(400, 400)
    QApplication.processEvents()
    assert pet.action_bubble.isVisible()

    pet._sync_right_click_interception()
    assert pet.monitor._pet_rect == expected
    assert bool(
        pet.windowFlags() & Qt.WindowType.WindowTransparentForInput
    ) is True
    pet._hide_action_bubble()
    assert bool(
        pet.windowFlags() & Qt.WindowType.WindowTransparentForInput
    ) is True
    pet.close()


def test_pet_display_profiles_define_expected_native_scale():
    from bongo.pet import PetSettings

    laptop = PetSettings(display_profile="laptop_2880_200")
    desktop = PetSettings(display_profile="desktop_2k_100")
    assert laptop.display_profile == "laptop_2880_200"
    assert desktop.display_profile == "desktop_2k_100"


def test_pet_native_rect_uses_canvas_window_handle(monkeypatch):
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
    monkeypatch.setattr(pet, "_native_canvas_handle", lambda: 4321)
    monkeypatch.setattr(
        pet_module,
        "_windows_native_window_rect",
        lambda hwnd: (400, 500, 774, 687) if hwnd == 4321 else None,
    )
    assert pet._native_canvas_rect() == (400, 500, 774, 687)
    pet.close()


def test_pet_screen_change_synchronizes_live2d_viewport(monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget

    import bongo.pet as pet_module

    class FakeCatView(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.sync_count = 0

        def set_mirrored(self, _mirrored):
            pass

        def react(self, _action):
            pass

        def sync_display(self):
            self.sync_count += 1

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_module, "BongoCatView", FakeCatView)
    pet = pet_module.PetWindow()
    pet._sync_after_screen_change()
    assert pet.canvas.sync_count == 1

    scheduled = []
    monkeypatch.setattr(
        pet_module.QTimer,
        "singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )
    pet._schedule_display_sync()
    assert [delay for delay, _callback in scheduled] == [0, 100, 300]
    assert all(callback == pet._sync_after_screen_change for _delay, callback in scheduled)
    pet.close()


def test_bongocat_renderer_exposes_dynamic_viewport_sync():
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "pet_renderer" / "src" / "main.js").read_text(
        encoding="utf-8"
    )
    bundle = (
        project_root / "bongo" / "assets" / "bongocat" / "renderer.js"
    ).read_text(encoding="utf-8")
    assert "syncViewport(requestedResolution)" in source
    assert "renderer.resolution = resolution" in source
    assert "syncViewport" in bundle
