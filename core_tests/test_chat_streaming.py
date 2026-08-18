from __future__ import annotations

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
os.environ["BONGO_DISABLE_GLOBAL_INPUT"] = "1"

from PySide6.QtCore import QObject

from bongo.qml_bridge import BongoBridge
from bongo.service import LearningService


class _RagStub:
    def retrieve(self, _query: str) -> dict:
        return {
            "records": [
                {
                    "content": "流式回答应在生成时逐步展示。",
                    "score": 0.93,
                    "title": "流式交互说明",
                }
            ]
        }


class _StreamingChatProvider:
    def __init__(self):
        self.messages = []

    def stream_text(self, messages, _system, on_delta):
        self.messages = messages
        on_delta("先显示")
        on_delta("，再保存。")
        return "先显示，再保存。"


class _StreamingWorkProvider:
    def __init__(self):
        self.decision_calls = 0

    def complete(self, _messages, _system, _schema=None):
        self.decision_calls += 1
        return {"action": "final", "tool": "", "arguments": {}, "answer": "任务已经完成。"}

    def stream_text(self, _messages, _system, on_delta):
        on_delta("任务已经")
        on_delta("完成。")
        return "任务已经完成。"


def test_chat_notifies_started_before_rag_and_streams_answer(tmp_path):
    service = LearningService(tmp_path / "data")
    provider = _StreamingChatProvider()
    service._provider = lambda: provider
    connection_id = service.save_rag_connection(name="测试 RAG", base_url="https://rag.example.com")
    service._rag_connector = lambda current_id=None: (
        _RagStub() if current_id == connection_id else None
    )
    events = []

    def on_started(conversation_id: int) -> None:
        events.append(("started", conversation_id))
        assert [item["role"] for item in service.database.get_messages(conversation_id)] == ["user"]

    try:
        result = service.chat(
            None,
            "流式输出如何工作？",
            on_started=on_started,
            on_delta=lambda delta: events.append(("delta", delta)),
        )

        assert events == [
            ("started", result["conversation_id"]),
            ("delta", "先显示"),
            ("delta", "，再保存。"),
        ]
        assert [item["role"] for item in service.database.get_messages(result["conversation_id"])] == [
            "user",
            "assistant",
        ]
        assert service.database.get_messages(result["conversation_id"])[-1]["content"] == "先显示，再保存。"
        assert sum(item["content"].count("流式输出如何工作？") for item in provider.messages) == 1
    finally:
        service.close()


def test_default_work_streams_final_answer_and_persists_once(tmp_path):
    service = LearningService(tmp_path / "data")
    provider = _StreamingWorkProvider()
    service._provider = lambda: provider
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events = []

    try:
        result = service.work(
            None,
            "检查工作目录",
            str(workspace),
            "default",
            on_started=lambda conversation_id: events.append(("started", conversation_id)),
            on_delta=lambda delta: events.append(("delta", delta)),
        )

        assert events == [
            ("started", result["conversation_id"]),
            ("delta", "任务已经"),
            ("delta", "完成。"),
        ]
        messages = service.database.get_messages(result["conversation_id"])
        assert [(item["role"], item["content"]) for item in messages] == [
            ("user", "检查工作目录"),
            ("assistant", "任务已经完成。"),
        ]
        assert provider.decision_calls == 1
    finally:
        service.close()


def test_bridge_coalesces_deltas_and_finishes_the_same_request():
    class _Service:
        @staticmethod
        def chat(_conversation_id, _message, *, on_started, on_delta):
            on_started(42)
            on_delta("你")
            on_delta("好")
            return {
                "conversation_id": 42,
                "source_id": 0,
                "backend": "default",
            }

    class _Canvas:
        @staticmethod
        def react(_action):
            return None

    class _Pet:
        canvas = _Canvas()

    bridge = BongoBridge.__new__(BongoBridge)
    QObject.__init__(bridge)
    bridge.service = _Service()
    bridge.pet = _Pet()
    bridge._current_conversation_id = None
    bridge._current_source_id = None
    bridge._pending_chat_deltas = {}
    bridge._scheduled_chat_delta_flushes = set()
    bridge._start_task = lambda task: task.run()
    events = []
    bridge.chatStarted.connect(lambda request_id, conversation_id: events.append(("started", request_id, conversation_id)))
    bridge.chatDelta.connect(lambda request_id, delta: events.append(("delta", request_id, delta)))
    bridge.chatStreamCompleted.connect(
        lambda request_id, conversation_id: events.append(("completed", request_id, conversation_id))
    )

    bridge.sendChat("request-1", 0, 0, "你好")

    assert events == [
        ("started", "request-1", 42),
        ("delta", "request-1", "你好"),
        ("completed", "request-1", 42),
    ]


def test_qml_chat_ctrl_enter_adds_newline_and_enter_sends_immediately(tmp_path):
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from bongo.activity import ActivityRecorder
    from bongo.app import APP_ICON_PATH
    from bongo.pet import PetWindow

    class _BlockingProvider:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def stream_text(self, _messages, _system, on_delta):
            self.started.set()
            if not self.release.wait(3):
                raise RuntimeError("test stream was not released")
            on_delta("流式")
            on_delta("回答")
            return "流式回答"

    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    service = LearningService(tmp_path / "data")
    provider = _BlockingProvider()
    service._provider = lambda: provider
    connection_id = service.save_rag_connection(name="测试 RAG", base_url="https://rag.example.com")
    service._rag_connector = lambda current_id=None: (
        _RagStub() if current_id == connection_id else None
    )
    work_conversation_id = service.start_conversation(
        "Work 后端显示",
        "cc",
        mode="work",
        work_dir=str(tmp_path),
    )
    recorder = ActivityRecorder(service.database, enabled=False)
    pet = PetWindow()
    bridge = BongoBridge(
        service,
        pet,
        recorder,
        APP_ICON_PATH,
        pet_enabled=False,
        start_background_tasks=False,
    )
    engine = QQmlApplicationEngine()
    qml_warnings = []
    engine.warnings.connect(lambda warnings: qml_warnings.extend(str(item) for item in warnings))
    engine.rootContext().setContextProperty("bridge", bridge)
    qml_path = Path(__file__).parents[1] / "bongo" / "qml" / "Main.qml"
    window = None

    try:
        engine.load(QUrl.fromLocalFile(str(qml_path)))
        assert engine.rootObjects(), "Main.qml did not create an ApplicationWindow: " + " | ".join(qml_warnings)
        window = engine.rootObjects()[0]
        bridge.attachWindow(window)
        window.setProperty("currentPage", 1)
        window.show()
        QTest.qWait(80)

        chat_input = window.findChild(QObject, "chatInput")
        messages = window.findChild(QObject, "chatMessages")
        chat_page = window.findChild(QObject, "chatPage")
        backend_control = window.findChild(QObject, "currentBackendControl")
        assert chat_input is not None
        assert messages is not None
        assert chat_page is not None
        assert backend_control is not None
        assert backend_control.property("displayText") == "当前后端 · 默认"
        assert backend_control.property("enabled") is False

        chat_page.setProperty("mode", "work")
        chat_page.setProperty("conversationId", work_conversation_id)
        QTest.qWait(20)
        assert backend_control.property("displayText") == "当前后端 · Claude Code"
        assert backend_control.property("enabled") is False
        assert backend_control.property("showIndicator") is False
        chat_page.setProperty("conversationId", 0)
        backend_control.setProperty("currentIndex", 2)
        QTest.qWait(20)
        assert backend_control.property("displayText") == "当前后端 · Codex"
        assert backend_control.property("enabled") is True
        assert backend_control.property("showIndicator") is True
        backend_control.setProperty("currentIndex", 0)
        chat_page.setProperty("mode", "chat")
        chat_page.setProperty("conversationId", 0)
        chat_input.forceActiveFocus()
        chat_input.setProperty("text", "第一行")
        chat_input.setProperty("cursorPosition", 3)

        QTest.keyClick(window, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        QTest.qWait(30)
        assert chat_input.property("text") == "第一行\n"
        assert messages.property("count") == 0

        chat_input.setProperty("text", "立即显示")
        chat_input.setProperty("cursorPosition", 4)
        QTest.keyClick(window, Qt.Key.Key_Return)
        QTest.qWait(40)

        assert chat_input.property("text") == ""
        assert messages.property("count") == 2
        assert provider.started.wait(2)

        provider.release.set()
        deadline = time.monotonic() + 3
        while bridge.active_tasks and time.monotonic() < deadline:
            QTest.qWait(25)
        assert not bridge.active_tasks
        assert messages.property("count") == 2
        conversation = next(
            item for item in service.database.list_conversations()
            if item["mode"] == "chat"
        )
        persisted = service.database.get_messages(int(conversation["id"]))
        assert [(item["role"], item["content"]) for item in persisted] == [
            ("user", "立即显示"),
            ("assistant", "流式回答"),
        ]
    finally:
        provider.release.set()
        bridge.tray.hide()
        bridge.shutdown()
        if window is not None:
            window.setProperty("allowClose", True)
            window.close()
        pet.close()
        service.close()
