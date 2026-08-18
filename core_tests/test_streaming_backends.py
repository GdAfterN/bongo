from __future__ import annotations

from types import SimpleNamespace

import pytest

import bongo.providers as provider_module
from bongo.database import StudyDatabase
from bongo.providers import (
    AnthropicProvider,
    ClaudeCodeProvider,
    CodexCliProvider,
    OpenAIProvider,
    ProviderConfig,
    ProviderError,
)
from bongo.work_agent import DefaultWorkAgent, WORK_FINAL_SYSTEM_PROMPT


class _OpenAIStream:
    def __init__(self, events, error: Exception | None = None):
        self.events = events
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        yield from self.events
        if self.error is not None:
            raise self.error


def test_openai_stream_text_forwards_sdk_deltas():
    events = [
        SimpleNamespace(type="response.created"),
        SimpleNamespace(type="response.output_text.delta", delta="你好"),
        SimpleNamespace(type="response.output_text.delta", delta="，Bongo"),
        SimpleNamespace(type="response.completed"),
    ]

    class Responses:
        def stream(self, **kwargs):
            assert kwargs["instructions"] == "system"
            return _OpenAIStream(events)

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = SimpleNamespace(responses=Responses())
    provider.model = "test-model"
    deltas = []

    result = provider.stream_text(
        [{"role": "user", "content": "question"}],
        "system",
        deltas.append,
    )

    assert result == "你好，Bongo"
    assert deltas == ["你好", "，Bongo"]


def test_openai_stream_does_not_retry_after_first_delta(monkeypatch):
    class TransientError(Exception):
        status_code = 503

    class Responses:
        calls = 0

        def stream(self, **_kwargs):
            self.calls += 1
            return _OpenAIStream(
                [SimpleNamespace(type="response.output_text.delta", delta="partial")],
                TransientError("connection lost"),
            )

    responses = Responses()
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = SimpleNamespace(responses=responses)
    provider.model = "test-model"
    deltas = []
    monkeypatch.setattr(provider_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(ProviderError, match="after output started"):
        provider.stream_text([], "system", deltas.append)

    assert responses.calls == 1
    assert deltas == ["partial"]


def test_openai_stream_retries_transient_failure_before_first_delta(monkeypatch):
    class TransientError(Exception):
        status_code = 503

    class FailedStream:
        def __enter__(self):
            raise TransientError("temporarily unavailable")

        def __exit__(self, *_args):
            return False

    class Responses:
        calls = 0

        def stream(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return FailedStream()
            return _OpenAIStream([
                SimpleNamespace(type="response.output_text.delta", delta="recovered")
            ])

    responses = Responses()
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = SimpleNamespace(responses=responses)
    provider.model = "test-model"
    delays = []
    monkeypatch.setattr(provider_module.time, "sleep", delays.append)

    assert provider.stream_text([], "system", lambda _delta: None) == "recovered"
    assert responses.calls == 2
    assert delays == [1.0]


def test_anthropic_stream_text_forwards_text_stream():
    class Stream:
        text_stream = iter(["真实", "流式"])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Messages:
        def stream(self, **kwargs):
            assert kwargs["system"] == "system"
            return Stream()

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.client = SimpleNamespace(messages=Messages())
    provider.model = "test-model"
    deltas = []

    assert provider.stream_text([], "system", deltas.append) == "真实流式"
    assert deltas == ["真实", "流式"]


def test_claude_code_stream_forwards_only_text_delta_events(monkeypatch, tmp_path):
    monkeypatch.setattr(provider_module.shutil, "which", lambda _name: "claude")
    captured = {}

    def fake_process(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input_text"]
        callback = kwargs["on_payload"]
        callback({"type": "assistant", "message": {"content": "ignored"}})
        callback({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "第一段"},
            },
        })
        callback({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "ignored"},
            },
        })
        callback({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "第二段"},
            },
        })
        callback({"type": "result", "result": "第一段第二段"})

    monkeypatch.setattr(provider_module, "_run_jsonl_process", fake_process)
    provider = ClaudeCodeProvider(ProviderConfig(name="cc"), cwd=tmp_path)
    deltas = []

    result = provider.stream_text(
        [{"role": "user", "content": "question"}],
        "system",
        deltas.append,
    )

    assert result == "第一段第二段"
    assert deltas == ["第一段", "第二段"]
    assert "--output-format" in captured["command"]
    assert "stream-json" in captured["command"]
    assert "--include-partial-messages" in captured["command"]
    assert captured["input"] == "用户: question"


def test_codex_stream_forwards_completed_agent_message_without_fake_chunks(monkeypatch, tmp_path):
    monkeypatch.setattr(provider_module.shutil, "which", lambda _name: "codex")
    captured = {}

    def fake_process(command, **kwargs):
        captured["command"] = command
        callback = kwargs["on_payload"]
        callback({
            "type": "item.completed",
            "item": {"type": "command_execution", "text": "ignored"},
        })
        callback({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "完整的实际事件"},
        })
        callback({
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 4},
        })

    monkeypatch.setattr(provider_module, "_run_jsonl_process", fake_process)
    provider = CodexCliProvider(ProviderConfig(name="codex"), cwd=tmp_path, writable=True)
    deltas = []

    result = provider.stream_text(
        [{"role": "user", "content": "question"}],
        "system",
        deltas.append,
    )

    assert result == "完整的实际事件"
    assert deltas == ["完整的实际事件"]
    assert captured["command"][1:3] == ["exec", "--json"]
    assert "workspace-write" in captured["command"]


class _StreamingWorkProvider:
    def __init__(self):
        self.complete_calls = 0
        self.stream_calls = 0
        self.final_messages = []
        self.final_system = ""

    def complete(self, _messages, _system, _schema=None):
        self.complete_calls += 1
        if self.complete_calls == 1:
            return {
                "action": "tool",
                "tool": "write_file",
                "arguments": {"path": "result.txt", "content": "done"},
                "answer": "",
            }
        return {
            "action": "final",
            "tool": "",
            "arguments": {},
            "answer": "不应直接展示这段结构化答案",
        }

    def stream_text(self, messages, system, on_delta):
        self.stream_calls += 1
        self.final_messages = messages
        self.final_system = system
        on_delta("任务")
        on_delta("完成")
        return "任务完成"


def test_default_work_agent_streams_final_non_structured_answer(tmp_path):
    database = StudyDatabase(tmp_path / "study.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = _StreamingWorkProvider()
    try:
        conversation_id = database.create_conversation(
            "work",
            "default",
            mode="work",
            work_dir=str(workspace),
        )
        deltas = []

        result = DefaultWorkAgent(
            provider,
            database,
            conversation_id,
            workspace,
        ).run([], "创建结果", deltas.append)

        assert result["answer"] == "任务完成"
        assert deltas == ["任务", "完成"]
        assert provider.complete_calls == 2
        assert provider.stream_calls == 1
        assert provider.final_system == WORK_FINAL_SYSTEM_PROMPT
        assert '"action": "final"' in provider.final_messages[-1]["content"]
        assert "不应直接展示" not in provider.final_messages[-1]["content"]
        assert (workspace / "result.txt").read_text(encoding="utf-8") == "done"
    finally:
        database.close()
