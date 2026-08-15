from __future__ import annotations

import json
import os
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QIcon, QWindow
from PySide6.QtWidgets import QFileDialog, QMessageBox, QSystemTrayIcon

from .activity import ActivityRecorder
from .application_names import display_application_name
from .ingestion import SUPPORTED_EXTENSIONS
from .pet import PetSettings, PetWindow
from .providers import available_chat_backends, available_providers, chat_backend_available
from .service import LearningService


class TaskSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    item_completed = Signal(object)
    finished = Signal()


class BackgroundTask(QRunnable):
    def __init__(self, function: Callable, *args, **kwargs):
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = TaskSignals()

    def run(self) -> None:
        try:
            self.signals.result.emit(self.function(*self.args, **self.kwargs))
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            if os.environ.get("BONGO_DEBUG") == "1":
                detail += "\n\n" + traceback.format_exc()
            self.signals.error.emit(detail)
        finally:
            self.signals.finished.emit()


class BongoBridge(QObject):
    dashboardChanged = Signal()
    sourcesChanged = Signal()
    practiceChanged = Signal()
    skillsChanged = Signal()
    conversationsChanged = Signal()
    messagesChanged = Signal()
    newsChanged = Signal()
    settingsChanged = Signal()
    statusChanged = Signal(str, str)
    busyChanged = Signal(str, bool)
    newsProgressChanged = Signal(int, str, str)
    showWindowRequested = Signal()
    navigateRequested = Signal(int)
    chatCompleted = Signal()

    def __init__(
        self,
        service: LearningService,
        pet: PetWindow,
        activity_recorder: ActivityRecorder,
        app_icon: Path,
        *,
        pet_enabled: bool = True,
        start_background_tasks: bool = True,
    ):
        super().__init__()
        self.service = service
        self.pet = pet
        self.activity_recorder = activity_recorder
        self.pet_enabled = pet_enabled
        self.thread_pool = QThreadPool.globalInstance()
        self.active_tasks: set[BackgroundTask] = set()
        self.window: QWindow | None = None
        self.force_exit = False
        self._current_question: dict[str, Any] | None = None
        self._current_conversation_id: int | None = None
        self._current_source_id: int | None = None
        self._recent_question_ids: deque[int] = deque(maxlen=12)
        self._recent_source_ids: deque[int] = deque(maxlen=2)
        self._news_cursor = 0
        self._news_refresh_active = False
        self._import_queue: deque[tuple[str, str]] = deque()
        self._work_break_active = False
        self._work_break_started_at: str | None = None
        self._build_tray(app_icon)
        self._connect_pet()
        self._load_pet_settings()
        self._start_timers(start_background_tasks)

    def _build_tray(self, app_icon: Path) -> None:
        self.tray = QSystemTrayIcon(QIcon(str(app_icon)), self)
        self.tray.setToolTip("Bongo Study")
        menu = self.tray.contextMenu()
        if menu is None:
            from PySide6.QtWidgets import QMenu

            menu = QMenu()
            self.tray.setContextMenu(menu)
        open_action = menu.addAction("打开学习面板")
        show_pet_action = menu.addAction("显示桌宠")
        menu.addSeparator()
        exit_action = menu.addAction("退出")
        open_action.triggered.connect(self.show_window)
        show_pet_action.triggered.connect(self.pet.show)
        exit_action.triggered.connect(self.quit_application)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _connect_pet(self) -> None:
        self.pet.answer_selected.connect(self._answer_from_pet)
        self.pet.question_unanswered.connect(self._question_unanswered)
        self.pet.open_panel_requested.connect(self.show_window)
        self.pet.open_dashboard_requested.connect(self._open_dashboard)
        self.pet.show_statistics_requested.connect(self._show_pet_statistics)
        self.pet.show_ai_news_requested.connect(self._show_pet_news)
        self.pet.news_detail_requested.connect(self._show_news_detail)
        self.pet.news_read_requested.connect(self._mark_news_read)
        self.pet.position_changed.connect(self._save_pet_position)

    def _open_dashboard(self) -> None:
        self.show_window()
        self.navigateRequested.emit(0)

    def _start_timers(self, start_background_tasks: bool) -> None:
        self.activity_flush_timer = QTimer(self)
        self.activity_flush_timer.setInterval(30_000)
        self.activity_flush_timer.timeout.connect(self.activity_recorder.flush)
        self.activity_flush_timer.start()
        self.session_timer = QTimer(self)
        self.session_timer.setInterval(5_000)
        self.session_timer.timeout.connect(self._check_work_session)
        self.session_timer.start()
        self.news_timer = QTimer(self)
        self.news_timer.setInterval(5 * 60 * 1000)
        self.news_timer.timeout.connect(self.refresh_news_if_due)
        self.news_timer.start()
        if start_background_tasks:
            QTimer.singleShot(0, self.refresh_news_if_due)

    def _start_task(self, task: BackgroundTask) -> None:
        self.active_tasks.add(task)
        task.signals.finished.connect(lambda current=task: self.active_tasks.discard(current))
        self.thread_pool.start(task)

    @Slot(QObject)
    def attachWindow(self, window: QObject) -> None:
        self.window = window if isinstance(window, QWindow) else None

    @staticmethod
    def _activate_window(window: QWindow) -> None:
        window.setVisibility(QWindow.Visibility.Windowed)
        window.setVisible(True)
        window.raise_()
        window.requestActivate()

    @Slot()
    def show_window(self) -> None:
        self.showWindowRequested.emit()
        if self.window is not None:
            QTimer.singleShot(0, lambda window=self.window: self._activate_window(window))

    @Slot()
    def quit_application(self) -> None:
        if self.active_tasks:
            self.statusChanged.emit("任务仍在进行", "请等待当前模型请求完成后再退出")
            return
        from PySide6.QtWidgets import QApplication

        self.force_exit = True
        if self.window is not None:
            self.window.setProperty("allowClose", True)
        self.pet.stop_input_monitor()
        self.pet.close()
        self.tray.hide()
        QApplication.instance().quit()

    def shutdown(self) -> None:
        self.pet.stop_input_monitor()
        self.activity_recorder.flush()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_window()

    @staticmethod
    def _format_duration(seconds: int) -> str:
        minutes = max(0, int(seconds)) // 60
        hours, minutes = divmod(minutes, 60)
        return f"{hours}小时{minutes}分钟" if hours else f"{minutes}分钟"

    @staticmethod
    def _daily_work_seconds(rows: list[dict]) -> int:
        intervals = []
        for row in rows:
            try:
                started_at = datetime.fromisoformat(str(row["first_activity_at"]))
                ended_at = datetime.fromisoformat(str(row["last_activity_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if ended_at >= started_at:
                intervals.append((started_at, ended_at))
        if not intervals:
            return 0
        intervals.sort(key=lambda item: item[0])
        session_start, session_end = intervals[0]
        total = 0.0
        for started_at, ended_at in intervals[1:]:
            if (started_at - session_end).total_seconds() < 10 * 60:
                session_end = max(session_end, ended_at)
            else:
                total += max(0.0, (session_end - session_start).total_seconds())
                session_start, session_end = started_at, ended_at
        return int(total + max(0.0, (session_end - session_start).total_seconds()))

    @Slot(result="QVariantMap")
    def dashboard(self) -> dict:
        try:
            self.activity_recorder.flush()
        except Exception:
            pass
        today = datetime.now().astimezone().date().isoformat()
        sources = self.service.database.list_code_sources()
        activity_rows = self.service.database.list_activity_buckets(today)
        activity_summary = self.service.database.get_daily_activity_summary(today)
        current_session = self.activity_recorder.get_current_work_session()
        documents = len(self.service.database.list_rag_documents())
        algorithms = len(sources)
        keys = sum(int(item["key_press_count"]) for item in activity_summary)
        work_seconds = self._daily_work_seconds(activity_rows)
        session_seconds = int((current_session or {}).get("duration_seconds", 0))
        stats = [
            {"label": "导入资料", "value": str(documents), "suffix": "份", "accent": "#df7845", "icon": "knowledge.svg"},
            {"label": "算法题", "value": str(algorithms), "suffix": "道", "accent": "#a18d9e", "icon": "practice.svg"},
            {"label": "当日工作", "value": self._format_duration(work_seconds), "suffix": "", "accent": "#829789", "icon": "clock.svg"},
            {"label": "连续工作", "value": self._format_duration(session_seconds), "suffix": "", "accent": "#d2aa65", "icon": "focus.svg"},
            {"label": "键盘敲击", "value": f"{keys:,}", "suffix": "次", "accent": "#8299ab", "icon": "keyboard.svg"},
        ]
        activity = self._activity_series(activity_rows)
        trend = self._weekly_activity_series(self.service.database, today)
        application_usage = self._application_usage_series(activity_summary)
        application_keystrokes = self._application_keystroke_series(activity_summary)
        top_application = display_application_name(
            activity_summary[0]["application"] if activity_summary else "暂无活动"
        )
        return {
            "stats": stats,
            "activity": activity,
            "trend": trend,
            "applicationUsage": application_usage,
            "applicationKeystrokes": application_keystrokes,
            "topApplication": str(top_application),
            "trackingEnabled": self.activity_recorder.enabled,
            "dateText": datetime.now().astimezone().strftime("%Y年%m月%d日 · %A"),
        }

    @staticmethod
    def _activity_series(rows: list[dict]) -> list[dict]:
        totals: dict[int, int] = {hour: 0 for hour in range(0, 24, 2)}
        for row in rows:
            try:
                started_at = datetime.fromisoformat(str(row["bucket_start"]))
            except (KeyError, TypeError, ValueError):
                continue
            hour = started_at.hour - started_at.hour % 2
            totals[hour] += int(row.get("key_press_count", 0))
        return [{"label": f"{hour:02d}:00", "value": totals[hour]} for hour in sorted(totals)]

    @staticmethod
    def _weekly_activity_series(database, end_date: str) -> list[dict]:
        last_day = datetime.fromisoformat(end_date).date()
        weekday_labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        sample_keys = [1680, 2350, 1940, 3120, 2760]
        sample_work_seconds = [2 * 3600 + 25 * 60, 3 * 3600 + 10 * 60, 2 * 3600 + 50 * 60, 4 * 3600 + 5 * 60, 3 * 3600 + 35 * 60]
        result = []
        for offset in range(6, -1, -1):
            current_day = last_day - timedelta(days=offset)
            rows = database.list_activity_buckets(current_day.isoformat())
            key_count = sum(int(row.get("key_press_count", 0)) for row in rows)
            work_seconds = BongoBridge._daily_work_seconds(rows)
            simulated = not rows and offset >= 2
            if simulated:
                sample_index = 6 - offset
                key_count = sample_keys[sample_index]
                work_seconds = sample_work_seconds[sample_index]
            result.append(
                {
                    "label": current_day.strftime("%m/%d"),
                    "dateLabel": f"{current_day.month}月{current_day.day}日 · {weekday_labels[current_day.weekday()]}",
                    "keys": key_count,
                    "workSeconds": work_seconds,
                    "workLabel": BongoBridge._format_duration(work_seconds),
                    "simulated": simulated,
                }
            )
        return result

    @staticmethod
    def _application_usage_series(summary: list[dict]) -> list[dict]:
        grouped: dict[str, int] = {}
        for item in summary:
            name = display_application_name(item.get("application", ""))
            foreground_seconds = int(item.get("foreground_seconds", 0) or 0)
            if foreground_seconds > 0:
                grouped[name] = grouped.get(name, 0) + foreground_seconds
        ordered = sorted(grouped.items(), key=lambda item: (-item[1], item[0]))
        visible = ordered[:6]
        remaining = sum(seconds for _name, seconds in ordered[6:])
        if remaining:
            visible.append(("其他应用", remaining))
        return [
            {
                "label": name,
                "seconds": seconds,
                "duration": BongoBridge._format_duration(seconds),
            }
            for name, seconds in visible
        ]

    @staticmethod
    def _application_keystroke_series(summary: list[dict]) -> list[dict]:
        grouped: dict[str, int] = {}
        for item in summary:
            name = display_application_name(item.get("application", ""))
            count = int(item.get("key_press_count", 0) or 0)
            if count > 0:
                grouped[name] = grouped.get(name, 0) + count
        return [
            {"label": name, "value": count, "valueLabel": f"{count:,}"}
            for name, count in sorted(grouped.items(), key=lambda item: (-item[1], item[0]))
        ]

    @Slot(result="QVariantList")
    def sources(self) -> list[dict]:
        return [
            {
                "id": int(item["id"]),
                "name": item["name"],
                "title": item.get("problem_title") or Path(item["name"]).stem,
                "type": item["knowledge_type"],
                "createdAt": item["created_at"],
                "questionCount": int(item["question_count"]),
                "status": item["status"],
                "bubbleEnabled": bool(item.get("bubble_enabled", 1)),
            }
            for item in self.service.database.list_code_sources()
        ]

    @Slot(result="QVariantList")
    def ragDocuments(self) -> list[dict]:
        return [
            {
                "id": int(item["id"]),
                "name": item["name"],
                "connectionId": int(item["connection_id"]),
                "connectionName": item["connection_name"],
                "status": item["status"],
                "error": item.get("error", ""),
                "createdAt": item["created_at"],
                "syncedAt": item.get("synced_at") or "",
            }
            for item in self.service.database.list_rag_documents()
        ]

    @Slot(result="QVariantList")
    def ragConnections(self) -> list[dict]:
        return [
            {
                "id": int(item["id"]), "name": item["name"], "baseUrl": item["base_url"],
                "apiKey": item["api_key"], "knowledgeId": item["knowledge_id"],
                "uploadPath": item["upload_path"], "retrievalPath": item["retrieval_path"],
                "deletePath": item["delete_path"], "active": bool(item["active"]),
                "documentCount": int(item["document_count"]),
            }
            for item in self.service.database.list_rag_connections()
        ]

    @Slot(int, str, str, str, str, str, str, str)
    def saveRagConnection(self, connection_id: int, name: str, base_url: str, api_key: str,
                          knowledge_id: str, upload_path: str, retrieval_path: str, delete_path: str) -> None:
        try:
            self.service.save_rag_connection(
                connection_id=connection_id if connection_id > 0 else None,
                name=name.strip(), base_url=base_url.strip(), api_key=api_key.strip(),
                knowledge_id=knowledge_id.strip(), upload_path=upload_path.strip() or "/documents",
                retrieval_path=retrieval_path.strip() or "/retrieval",
                delete_path=delete_path.strip() or "/documents/{document_id}",
            )
        except Exception as exc:
            self.statusChanged.emit("RAG 配置保存失败", str(exc))
            return
        self.sourcesChanged.emit()
        self.statusChanged.emit("RAG 配置已保存", "已设为 Chat 当前连接")

    @Slot(int)
    def activateRagConnection(self, connection_id: int) -> None:
        try:
            self.service.database.activate_rag_connection(connection_id)
        except Exception as exc:
            self.statusChanged.emit("切换失败", str(exc))
            return
        self.sourcesChanged.emit()

    @Slot(int)
    def testRagConnection(self, connection_id: int) -> None:
        self.busyChanged.emit("ragTest", True)
        task = BackgroundTask(self.service.test_rag_connection, connection_id)
        task.task_type = "ragTest"
        task.signals.result.connect(lambda _result: self.statusChanged.emit("连接成功", "外部 RAG 检索接口可用"))
        task.signals.error.connect(lambda error: self.statusChanged.emit("连接失败", error))
        task.signals.finished.connect(lambda: self.busyChanged.emit("ragTest", False))
        self._start_task(task)

    @Slot(str)
    def importKnowledge(self, knowledge_type: str) -> None:
        if knowledge_type not in {"document", "code"}:
            return
        patterns = " ".join(f"*{suffix}" for suffix in sorted(SUPPORTED_EXTENSIONS))
        title = "导入算法题题解" if knowledge_type == "code" else "导入文档知识"
        files, _ = QFileDialog.getOpenFileNames(None, title, "", f"知识文件 ({patterns});;所有文件 (*)")
        self._import_queue.extend((path, knowledge_type) for path in files)
        if files and not any(getattr(task, "task_type", "") == "ingest" for task in self.active_tasks):
            self._ingest_next()

    def _ingest_next(self) -> None:
        if not self._import_queue:
            self.busyChanged.emit("ingest", False)
            self.sourcesChanged.emit()
            self.practiceChanged.emit()
            self.dashboardChanged.emit()
            return
        path, knowledge_type = self._import_queue.popleft()
        self.busyChanged.emit("ingest", True)
        self.statusChanged.emit("正在学习", Path(path).name)
        self.pet.show_message(f"正在学习 {Path(path).name} ...", 180000)
        task = BackgroundTask(self.service.ingest, path, knowledge_type)
        task.task_type = "ingest"
        task.signals.result.connect(
            lambda result, filename=Path(path).name: self._ingest_completed(filename, result)
        )
        task.signals.error.connect(lambda error: self.statusChanged.emit("导入失败", error))
        task.signals.finished.connect(self._ingest_next)
        self._start_task(task)

    def _ingest_completed(self, filename: str, result: dict) -> None:
        if "document_id" in result:
            message = f"{filename} 已上传到外部 RAG" if result.get("created") else f"{filename} 已在 RAG 知识库中"
            self.pet.show_message(message)
            self.statusChanged.emit("文档同步完成", message)
            self.sourcesChanged.emit()
            self.dashboardChanged.emit()
            return
        subject = f"《{result['problem_title']}》" if result.get("problem_title") else filename
        message = (
            f"吃完了 {subject}，整理出 {result['questions']} 道题"
            if result["created"] or result.get("reprocessed")
            else f"{filename} 已经学习过"
        )
        self.pet.show_message(message)
        self.statusChanged.emit("导入完成", message)
        self.sourcesChanged.emit()
        self.dashboardChanged.emit()

    @Slot(int)
    def retryRagDocument(self, document_id: int) -> None:
        self.busyChanged.emit("ingest", True)
        task = BackgroundTask(self.service.retry_document, document_id)
        task.task_type = "ingest"
        task.signals.result.connect(lambda _result: self.sourcesChanged.emit())
        task.signals.error.connect(lambda error: self.statusChanged.emit("重试失败", error))
        task.signals.finished.connect(lambda: self.busyChanged.emit("ingest", False))
        self._start_task(task)

    @Slot(int)
    def deleteRagDocument(self, document_id: int) -> None:
        document = self.service.database.get_rag_document(document_id)
        if not document:
            return
        if QMessageBox.question(None, "删除文档", f"同时从外部 RAG 删除 {document['name']}？") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.delete_document(document_id)
        except Exception as exc:
            self.statusChanged.emit("删除失败", str(exc))
            return
        self.sourcesChanged.emit()

    @Slot(int)
    def deleteSource(self, source_id: int) -> None:
        source = self.service.database.get_source(source_id)
        if not source:
            return
        if QMessageBox.question(None, "删除知识", f"删除 {source['name']} 及其生成的题目？") != QMessageBox.StandardButton.Yes:
            return
        self.service.database.delete_source(source_id)
        self.sourcesChanged.emit()
        self.practiceChanged.emit()
        self.dashboardChanged.emit()

    @Slot(int, bool)
    def setSourceBubbleEnabled(self, source_id: int, enabled: bool) -> None:
        self.service.database.set_source_bubble_enabled(source_id, enabled)
        self.sourcesChanged.emit()

    @Slot(int, result="QVariantList")
    def questions(self, source_id: int) -> list[dict]:
        return [self._question_for_qml(item) for item in self.service.database.list_questions(source_id)]

    @staticmethod
    def _question_for_qml(question: dict | None) -> dict:
        if not question:
            return {}
        return {
            "id": int(question["id"]),
            "sourceId": int(question["source_id"]),
            "sourceName": question.get("source_name", ""),
            "title": question.get("problem_title") or question.get("source_name", ""),
            "topic": question.get("topic") or "综合复习",
            "prompt": question["prompt"],
            "options": list(question["options"]),
            "correctIndex": int(question["correct_index"]),
            "explanation": question.get("explanation", ""),
            "knowledgeType": question.get("knowledge_type", "document"),
        }

    @Slot(int, str, bool, result="QVariantMap")
    def nextPractice(self, source_id: int, mode: str, advance: bool) -> dict:
        selected_source = source_id if source_id > 0 else None
        previous = self._current_question if advance else None
        previous_id = int(previous["id"]) if previous else None
        if previous:
            self._recent_question_ids.append(previous_id)
            self._recent_source_ids.append(int(previous["source_id"]))
        question = self.service.database.next_question(
            exclude_id=previous_id,
            exclude_ids=tuple(self._recent_question_ids),
            exclude_source_ids=tuple(self._recent_source_ids) if selected_source is None and advance else None,
            source_id=selected_source,
            wrong_only=mode == "wrong",
            unanswered_only=mode == "unanswered",
            randomize=True,
        )
        if question is None and previous_id is not None:
            self._recent_question_ids.clear()
            self._recent_source_ids.clear()
            question = self.service.database.next_question(
                exclude_id=previous_id,
                source_id=selected_source,
                wrong_only=mode == "wrong",
                unanswered_only=mode == "unanswered",
                randomize=True,
            )
        self._current_question = question
        return self._question_for_qml(question)

    @Slot(int, int, result="QVariantMap")
    def answerPractice(self, question_id: int, selected_index: int) -> dict:
        result = self.service.database.answer_question(question_id, selected_index)
        self.practiceChanged.emit()
        return {
            "correct": bool(result["correct"]),
            "correctIndex": int(result["question"]["correct_index"]),
            "explanation": result["question"].get("explanation", ""),
        }

    @Slot(int, result="QVariantMap")
    def practiceCounts(self, source_id: int) -> dict:
        selected = source_id if source_id > 0 else None
        return {
            "all": len(self.service.database.list_questions(source_id=selected)),
            "wrong": len(self.service.database.list_wrong_questions(selected)),
            "unanswered": len(self.service.database.list_unanswered_questions(selected)),
        }

    @Slot(result="QVariantList")
    def conversations(self) -> list[dict]:
        return [
            {
                "id": int(item["id"]),
                "title": item["title"],
                "sourceId": int(item["source_id"]) if item.get("source_id") is not None else 0,
                "sourceName": item.get("source_name") or "未绑定文档",
                "mode": item.get("mode") or "legacy",
                "ragName": item.get("rag_connection_name") or "",
                "workDir": item.get("work_dir") or "",
                "backend": item["provider"],
                "updatedAt": item["updated_at"],
            }
            for item in self.service.database.list_conversations()
        ]

    @Slot(int, result="QVariantList")
    def messages(self, conversation_id: int) -> list[dict]:
        if conversation_id <= 0:
            return []
        return [
            {
                "id": int(item["id"]),
                "role": item["role"],
                "content": item["content"],
                "citations": item.get("citations", []),
            }
            for item in self.service.database.get_messages(conversation_id, 200)
        ]

    @Slot(int, int, str)
    def sendChat(self, source_id: int, conversation_id: int, message: str) -> None:
        if not message.strip():
            self.statusChanged.emit("无法发送", "请输入问题")
            return
        self._current_conversation_id = conversation_id if conversation_id > 0 else None
        self.busyChanged.emit("chat", True)
        task = BackgroundTask(
            self.service.chat,
            self._current_conversation_id,
            message.strip(),
        )
        task.task_type = "chat"
        task.signals.result.connect(self._chat_completed)
        task.signals.error.connect(lambda error: self.statusChanged.emit("对话失败", error))
        task.signals.finished.connect(lambda: self.busyChanged.emit("chat", False))
        self._start_task(task)

    @Slot(result=str)
    def chooseWorkDirectory(self) -> str:
        return QFileDialog.getExistingDirectory(None, "选择 Work 工作目录") or ""

    @Slot(int, str, str, str)
    def sendWork(self, conversation_id: int, work_dir: str, backend: str, message: str) -> None:
        if not message.strip():
            self.statusChanged.emit("无法发送", "请输入工作任务")
            return
        self._current_conversation_id = conversation_id if conversation_id > 0 else None
        self.busyChanged.emit("chat", True)
        task = BackgroundTask(self.service.work, self._current_conversation_id, message.strip(), work_dir, backend)
        task.task_type = "chat"
        task.signals.result.connect(self._chat_completed)
        task.signals.error.connect(lambda error: self.statusChanged.emit("Work 执行失败", error))
        task.signals.finished.connect(lambda: self.busyChanged.emit("chat", False))
        self._start_task(task)

    def _chat_completed(self, result: dict) -> None:
        self._current_conversation_id = int(result["conversation_id"])
        self._current_source_id = int(result["source_id"])
        self.conversationsChanged.emit()
        self.messagesChanged.emit()
        self.chatCompleted.emit()
        self.statusChanged.emit("回答已保存", result["backend"])
        self.pet.canvas.react("left")

    @Slot(int)
    def selectConversation(self, conversation_id: int) -> None:
        conversation = self.service.database.get_conversation(conversation_id)
        if not conversation:
            return
        self._current_conversation_id = conversation_id
        self._current_source_id = int(conversation["source_id"] or 0)
        self.messagesChanged.emit()

    @Slot(result="QVariantList")
    def skills(self) -> list[dict]:
        result = []
        for item in self.service.database.list_learning_skills():
            if not item.get("last_exported_at"):
                status = "未导出"
            elif item.get("dirty"):
                status = "待更新"
            else:
                status = "最新"
            result.append({
                "id": int(item["id"]),
                "name": item["name"],
                "title": item["title"],
                "description": item["description"],
                "sourceCount": int(item["source_count"]),
                "questionCount": int(item["question_count"]),
                "version": int(item["version"]),
                "status": status,
            })
        return result

    @Slot(int, result="QVariantMap")
    def skillPreview(self, skill_id: int) -> dict:
        try:
            preview = self.service.preview_skill(skill_id)
        except Exception as exc:
            return {"error": str(exc)}
        skill = preview["skill"]
        return {
            "id": int(skill["id"]),
            "name": skill["name"],
            "title": skill["title"],
            "description": skill["description"],
            "sources": [item.get("problem_title") or item["name"] for item in preview["sources"]],
            "questionCount": len(preview["questions"]),
            "historicalMistakes": int(preview["historical_mistakes"]),
            "weakQuestions": int(preview["weak_questions"]),
            "growthScore": int(preview["growth"]["growth_score"]),
            "conversationConclusions": int(preview["growth"]["conversation_conclusions"]),
            "sourceIds": [int(value) for value in skill["source_ids"]],
            "includeQuestions": bool(skill["include_questions"]),
            "includeMistakes": bool(skill["include_mistakes"]),
            "includeConversations": bool(skill["include_conversations"]),
            "includeGrowth": bool(skill["include_growth"]),
        }

    @Slot(str, str, str, "QVariantList", bool, bool, bool, bool)
    def createSkill(
        self,
        name: str,
        title: str,
        description: str,
        source_ids: list,
        questions: bool,
        mistakes: bool,
        conversations: bool,
        growth: bool,
    ) -> None:
        try:
            self.service.create_skill(
                name=name,
                title=title,
                description=description,
                source_ids=[int(value) for value in source_ids],
                include_questions=questions,
                include_mistakes=mistakes,
                include_conversations=conversations,
                include_growth=growth,
            )
        except Exception as exc:
            self.statusChanged.emit("创建失败", str(exc))
            return
        self.skillsChanged.emit()
        self.statusChanged.emit("创建成功", title)

    @Slot(int, str, str, str, "QVariantList", bool, bool, bool, bool)
    def updateSkill(
        self,
        skill_id: int,
        name: str,
        title: str,
        description: str,
        source_ids: list,
        questions: bool,
        mistakes: bool,
        conversations: bool,
        growth: bool,
    ) -> None:
        try:
            self.service.update_skill(
                skill_id,
                name=name,
                title=title,
                description=description,
                source_ids=[int(value) for value in source_ids],
                include_questions=questions,
                include_mistakes=mistakes,
                include_conversations=conversations,
                include_growth=growth,
            )
        except Exception as exc:
            self.statusChanged.emit("更新失败", str(exc))
            return
        self.skillsChanged.emit()
        self.statusChanged.emit("更新成功", title)

    @Slot(int)
    def deleteSkill(self, skill_id: int) -> None:
        skill = self.service.database.get_learning_skill(skill_id)
        if not skill:
            return
        if QMessageBox.question(None, "删除 Skill", f"删除 {skill['title']}？") != QMessageBox.StandardButton.Yes:
            return
        self.service.database.delete_learning_skill(skill_id)
        self.skillsChanged.emit()

    @Slot(int)
    def exportSkill(self, skill_id: int) -> None:
        skill = self.service.database.get_learning_skill(skill_id)
        if not skill:
            return
        directory = QFileDialog.getExistingDirectory(None, "选择 Skill 导出位置")
        if not directory:
            return
        try:
            target = self.service.export_skill(skill_id, Path(directory) / skill["name"])
        except Exception as exc:
            self.statusChanged.emit("导出失败", str(exc))
            return
        self.skillsChanged.emit()
        self.statusChanged.emit("导出完成", str(target))

    @Slot(result="QVariantList")
    def news(self) -> list[dict]:
        digest = self.service.cached_ai_news()
        if not digest:
            return []
        read_ids = self.service.read_ai_news_ids(digest)
        return [
            {
                **item,
                "isRead": int(item["id"]) in read_ids,
                "publishedDisplay": self._news_time(item["published_at"]),
            }
            for item in digest["items"]
        ]

    @staticmethod
    def _news_time(value: object) -> str:
        try:
            return datetime.fromisoformat(str(value)).astimezone().strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            return "时间未知"

    @Slot(bool)
    def refreshNews(self, force: bool = True) -> None:
        if self._news_refresh_active:
            return
        self._news_refresh_active = True
        self.busyChanged.emit("news", True)
        task = BackgroundTask(self.service.fetch_ai_news, force)
        task.task_type = "news"
        task.kwargs["progress"] = task.signals.progress.emit
        task.kwargs["item_completed"] = task.signals.item_completed.emit
        task.signals.progress.connect(self._news_progress)
        task.signals.item_completed.connect(lambda _digest: self.newsChanged.emit())
        task.signals.result.connect(lambda _digest: self._news_completed())
        task.signals.error.connect(lambda error: self.statusChanged.emit("简讯更新失败", error))
        task.signals.finished.connect(self._news_finished)
        self._start_task(task)

    @Slot()
    def refresh_news_if_due(self) -> None:
        if self.service.ai_news_due():
            self.refreshNews(False)

    def _news_progress(self, update: dict) -> None:
        self.newsProgressChanged.emit(
            max(0, min(100, int(update.get("percent", 0)))),
            str(update.get("stage") or "正在抓取"),
            str(update.get("detail") or ""),
        )

    def _news_completed(self) -> None:
        self.newsChanged.emit()
        self.statusChanged.emit("AI 简讯已更新", "最新 20 条内容已保存")

    def _news_finished(self) -> None:
        self._news_refresh_active = False
        self.busyChanged.emit("news", False)

    @Slot(int)
    def markNewsRead(self, news_id: int) -> None:
        self._mark_news_read(news_id)

    def _mark_news_read(self, news_id: int) -> None:
        self.service.mark_ai_news_read(news_id)
        self._news_cursor = 0
        self.newsChanged.emit()

    @Slot(str)
    def openUrl(self, url: str) -> None:
        if url.strip():
            QDesktopServices.openUrl(QUrl(url.strip()))

    @Slot(result="QVariantMap")
    def settings(self) -> dict:
        config = self.service.provider_config()
        database = self.service.database
        backend = self.service.chat_backend()
        if not chat_backend_available(backend):
            backend = "default"
        return {
            "providers": available_providers(),
            "chatBackends": [
                {"value": name, "label": {"default": "默认", "cc": "Claude Code", "codex": "Codex"}[name], "available": chat_backend_available(name)}
                for name in available_chat_backends()
            ],
            "provider": config.name,
            "model": config.model,
            "baseUrl": config.base_url,
            "apiKey": config.api_key,
            "chatBackend": backend,
            "petVisible": database.get_setting("pet_visible", "1") == "1",
            "petOpacity": int(database.get_setting("pet_opacity", "100")),
            "petScale": int(database.get_setting("pet_scale", "100")),
            "petAlwaysTop": database.get_setting("pet_always_on_top", "1") == "1",
            "petPassThrough": database.get_setting("pet_pass_through", "0") == "1",
            "petKeepScreen": database.get_setting("pet_keep_in_screen", "1") == "1",
            "petModelMirror": database.get_setting("pet_model_mirror", "0") == "1",
            "petMouseMirror": database.get_setting("pet_mouse_mirror", "0") == "1",
            "petKeyboard": database.get_setting("pet_keyboard_enabled", "1") == "1",
            "petMouse": database.get_setting("pet_mouse_enabled", "1") == "1",
            "questionTimeout": int(database.get_setting("pet_question_timeout", "45")),
            "displayProfile": database.get_setting("pet_display_profile", "laptop_2880_200"),
            "activityTracking": database.get_setting("activity_tracking_enabled", "0") == "1",
        }

    @Slot(str, str, str, str)
    def saveModelSettings(self, provider: str, model: str, base_url: str, api_key: str) -> None:
        self.service.set_provider(provider, model.strip(), base_url.strip(), api_key.strip())
        self.settingsChanged.emit()
        self.pet.show_message("新的大脑设置已经记住了。")
        self.statusChanged.emit("设置已保存", "模型设置已更新")

    @Slot(str)
    def saveChatBackend(self, backend: str) -> None:
        if not chat_backend_available(backend):
            self.statusChanged.emit("后端不可用", f"没有找到 {backend} CLI")
            self.settingsChanged.emit()
            return
        self.service.set_chat_backend(backend)
        self.settingsChanged.emit()
        labels = {"default": "默认", "cc": "Claude Code", "codex": "Codex"}
        self.statusChanged.emit("对话后端已切换", labels.get(backend, backend))

    @Slot("QVariantMap")
    def savePetSettings(self, values: dict) -> None:
        settings = PetSettings(
            visible=bool(values.get("visible", True)),
            opacity=int(values.get("opacity", 100)),
            scale=int(values.get("scale", 100)),
            always_on_top=bool(values.get("alwaysTop", True)),
            pass_through=bool(values.get("passThrough", False)),
            keep_in_screen=bool(values.get("keepScreen", True)),
            model_mirror=bool(values.get("modelMirror", False)),
            mouse_mirror=bool(values.get("mouseMirror", False)),
            keyboard_enabled=bool(values.get("keyboard", True)),
            mouse_enabled=bool(values.get("mouse", True)),
            question_timeout=int(values.get("questionTimeout", 45)),
            display_profile=str(values.get("displayProfile", "laptop_2880_200")),
        )
        mapping = {
            "pet_visible": settings.visible,
            "pet_opacity": settings.opacity,
            "pet_scale": settings.scale,
            "pet_always_on_top": settings.always_on_top,
            "pet_pass_through": settings.pass_through,
            "pet_keep_in_screen": settings.keep_in_screen,
            "pet_model_mirror": settings.model_mirror,
            "pet_mouse_mirror": settings.mouse_mirror,
            "pet_keyboard_enabled": settings.keyboard_enabled,
            "pet_mouse_enabled": settings.mouse_enabled,
            "pet_question_timeout": settings.question_timeout,
            "pet_display_profile": settings.display_profile,
        }
        for key, value in mapping.items():
            self.service.database.set_setting(key, str(int(value)) if isinstance(value, bool) else str(value))
        self.pet.apply_settings(settings)
        self.settingsChanged.emit()
        self.statusChanged.emit("桌宠设置已应用", "")

    @Slot(bool)
    def saveActivitySettings(self, enabled: bool) -> None:
        self.service.database.set_setting("activity_tracking_enabled", str(int(enabled)))
        self.activity_recorder.set_enabled(enabled)
        self.settingsChanged.emit()
        self.dashboardChanged.emit()

    @Slot()
    def clearActivityHistory(self) -> None:
        if QMessageBox.question(None, "清空活动历史", "删除所有匿名键鼠活动统计？") != QMessageBox.StandardButton.Yes:
            return
        self.activity_recorder.flush()
        self.service.database.clear_activity_history()
        self.dashboardChanged.emit()

    @Slot()
    def showPet(self) -> None:
        self.pet.show()
        self.pet.raise_()

    def _load_pet_settings(self) -> None:
        database = self.service.database
        settings = PetSettings(
            visible=database.get_setting("pet_visible", "1") == "1",
            opacity=int(database.get_setting("pet_opacity", "100")),
            scale=int(database.get_setting("pet_scale", "100")),
            always_on_top=database.get_setting("pet_always_on_top", "1") == "1",
            pass_through=database.get_setting("pet_pass_through", "0") == "1",
            keep_in_screen=database.get_setting("pet_keep_in_screen", "1") == "1",
            model_mirror=database.get_setting("pet_model_mirror", "0") == "1",
            mouse_mirror=database.get_setting("pet_mouse_mirror", "0") == "1",
            keyboard_enabled=database.get_setting("pet_keyboard_enabled", "1") == "1",
            mouse_enabled=database.get_setting("pet_mouse_enabled", "1") == "1",
            question_timeout=int(database.get_setting("pet_question_timeout", "45")),
            display_profile=database.get_setting("pet_display_profile", "laptop_2880_200"),
        )
        self.pet.apply_settings(settings, update_visibility=False)
        x = database.get_setting("pet_x", "")
        y = database.get_setting("pet_y", "")
        if x and y:
            self.pet.move(int(x), int(y))
            self.pet.keep_inside_screen()

    def _save_pet_position(self, x: int, y: int) -> None:
        self.service.database.set_setting("pet_x", str(x))
        self.service.database.set_setting("pet_y", str(y))

    def _answer_from_pet(self, question_id: int, selected_index: int) -> None:
        result = self.service.database.answer_question(question_id, selected_index)
        self.pet.set_answer_feedback(result["correct"], result["question"]["explanation"])
        self.practiceChanged.emit()

    def _question_unanswered(self, question_id: int) -> None:
        self.service.database.mark_question_unanswered(question_id)
        self.practiceChanged.emit()

    def _show_pet_statistics(self) -> None:
        today = datetime.now().astimezone().date().isoformat()
        rows = self.service.database.list_activity_buckets(today)
        summary = self.service.database.get_daily_activity_summary(today)
        total_keys = sum(int(item["key_press_count"]) for item in summary)
        top = display_application_name(summary[0]["application"]) if summary else "暂无"
        self.pet.show_statistics(self._format_duration(self._daily_work_seconds(rows)), total_keys, top)

    def _show_pet_news(self) -> None:
        digest = self.service.cached_ai_news()
        if not digest or not digest.get("items"):
            self.pet.show_message("正在生成最新 AI 简讯，请稍候……", 60_000)
            self.refreshNews(False)
            return
        read_ids = self.service.read_ai_news_ids(digest)
        items = [item for item in digest["items"] if int(item["id"]) not in read_ids]
        if not items:
            self.pet.show_message("本轮 AI 简讯已经全部阅完，可在学习面板中随时查看。", 8000)
            return
        item = dict(items[self._news_cursor % len(items)])
        self._news_cursor = (self._news_cursor + 1) % len(items)
        item["published_at_display"] = self._news_time(item["published_at"])
        self.pet.show_ai_news(item)

    def _show_news_detail(self, news_id: int) -> None:
        self.show_window()
        self.navigateRequested.emit(news_id)

    def _check_work_session(self) -> None:
        session = self.activity_recorder.get_current_work_session()
        self.pet.set_work_session_tooltip(session)
        if (
            session is None
            or session["duration_seconds"] < 40 * 60
            or session.get("reminder_sent")
            or self._work_break_active
            or not self.pet.can_show_break_reminder()
        ):
            return
        claimed = self.activity_recorder.claim_break_reminder(40)
        if claimed is None:
            return
        self._work_break_active = True
        self._work_break_started_at = str(claimed["started_at"])
        task = BackgroundTask(self.service.analyze_work_session, self.activity_recorder, claimed)
        task.task_type = "work_break"
        task.signals.result.connect(lambda result: self._work_break_completed(result, claimed))
        task.signals.error.connect(lambda _error: self._work_break_fallback(claimed))
        task.signals.finished.connect(self._work_break_finished)
        self._start_task(task)

    def _work_break_completed(self, result: dict, session: dict) -> None:
        current = self.activity_recorder.get_current_work_session()
        if not current or current.get("started_at") != session.get("started_at"):
            return
        report = result["report"]
        self.pet.show_message(
            f"{report['summary']}\n\n{report['activity']}\n\n{report['suggestion']}",
            20_000,
        )

    def _work_break_fallback(self, session: dict) -> None:
        current = self.activity_recorder.get_current_work_session()
        if not current or current.get("started_at") != session.get("started_at"):
            return
        self.pet.show_message(
            f"你已经连续工作 {self._format_duration(session['duration_seconds'])}，"
            f"键盘敲击 {int(session['key_press_count']):,} 次。\n\n起来走动一下，让眼睛和手腕休息几分钟吧。",
            20_000,
        )

    def _work_break_finished(self) -> None:
        self._work_break_active = False
        self._work_break_started_at = None
