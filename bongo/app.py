from __future__ import annotations

import argparse
import ctypes
import hashlib
import html
import faulthandler
import os
import sys
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .ingestion import SUPPORTED_EXTENSIONS
from .dialogs import QuestionBankDialog, SkillEditorDialog
from .activity import ActivityRecorder
from .application_names import display_application_name
from .pet import PetSettings, PetWindow
from .providers import available_chat_backends, chat_backend_available, available_providers
from .service import LearningService
from .styles import APP_STYLE
from .widgets import ActivityTimelineWidget, WrappedRadioButton
from .qml_bridge import BongoBridge


APP_ICON_PATH = Path(__file__).parent / "assets" / "app-icon.ico"
WINDOW_BACKGROUND_COLOR = 0x00E5EDF1  # COLORREF for #f1ede5 (BGR byte order)
WINDOW_TEXT_COLOR = 0x001F2220  # COLORREF for #20221f (BGR byte order)


def _apply_windows_title_bar_theme(window) -> None:
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.c_void_p(int(window.winId()))
        dwm = ctypes.windll.dwmapi
        for attribute, color in (
            (34, WINDOW_BACKGROUND_COLOR),  # DWMWA_BORDER_COLOR
            (35, WINDOW_BACKGROUND_COLOR),  # DWMWA_CAPTION_COLOR
            (36, WINDOW_TEXT_COLOR),  # DWMWA_TEXT_COLOR
        ):
            value = ctypes.c_uint(color)
            dwm.DwmSetWindowAttribute(
                hwnd,
                ctypes.c_uint(attribute),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
    except (AttributeError, OSError, TypeError, ValueError):
        pass


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    item_completed = Signal(object)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function, *args, **kwargs):
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.result.emit(self.function(*self.args, **self.kwargs))
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            if os.environ.get("BONGO_DEBUG") == "1":
                detail += "\n\n" + traceback.format_exc()
            self.signals.error.emit(detail)
        finally:
            self.signals.finished.emit()


def panel() -> QFrame:
    frame = QFrame()
    frame.setObjectName("panel")
    return frame


class MainWindow(QMainWindow):
    def __init__(
        self,
        service: LearningService,
        pet: PetWindow,
        activity_recorder: ActivityRecorder,
        start_hidden: bool = False,
        pet_enabled: bool = True,
    ):
        super().__init__()
        self.service = service
        self.pet = pet
        self.activity_recorder = activity_recorder
        self.pet_enabled = pet_enabled
        self.thread_pool = QThreadPool.globalInstance()
        self.active_workers: set[Worker] = set()
        self.current_conversation_id: int | None = None
        self.current_source_id: int | None = None
        self.current_practice_question: dict | None = None
        self.recent_practice_question_ids: deque[int] = deque(maxlen=12)
        self.recent_practice_source_ids: deque[int] = deque(maxlen=2)
        self.current_skill_id: int | None = None
        self.import_queue: deque[tuple[str, str]] = deque()
        self.force_exit = False
        self.work_break_worker_active = False
        self.ai_news_worker_active = False
        self.ai_news_show_after_refresh = False
        self.ai_news_cursor = 0
        self.work_break_session_started_at: str | None = None
        self.setWindowTitle("Bongo Study")
        self.setMinimumSize(980, 680)
        self.resize(1180, 760)
        self._build_ui()
        self._build_tray()
        self._connect_pet()
        self.refresh_all()
        self.pet.position_changed.connect(self._save_pet_position)

        self.home_refresh_timer = QTimer(self)
        self.home_refresh_timer.setInterval(30_000)
        self.home_refresh_timer.timeout.connect(self.refresh_home)
        self.home_refresh_timer.start()

        self.work_session_timer = QTimer(self)
        self.work_session_timer.setInterval(5_000)
        self.work_session_timer.timeout.connect(self.check_work_session)
        self.work_session_timer.start()

        self.ai_news_timer = QTimer(self)
        self.ai_news_timer.setInterval(5 * 60 * 1000)
        self.ai_news_timer.timeout.connect(self.refresh_ai_news_if_due)
        self.ai_news_timer.start()

        if not start_hidden:
            self.show()
            QTimer.singleShot(0, self.refresh_ai_news_if_due)

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(198)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(14, 18, 14, 16)
        brand = QLabel("Bongo Study")
        brand.setObjectName("brand")
        sub = QLabel("桌面知识伙伴")
        sub.setObjectName("brandSub")
        side_layout.addWidget(brand)
        side_layout.addWidget(sub)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        nav_items = [
            ("首页", 0),
            ("对话", 1),
            ("知识库", 2),
            ("练习", 3),
            ("Skill", 4),
            ("AI 简讯", 5),
            ("设置", 6),
        ]
        for label, index in nav_items:
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, page=index, title=label: self.show_page(page, title))
            self.nav_group.addButton(button, index)
            side_layout.addWidget(button)
            if index == 0:
                button.setChecked(True)
        side_layout.addStretch()
        show_pet = QPushButton("显示桌宠")
        show_pet.setObjectName("navButton")
        show_pet.clicked.connect(self.pet.show_on_active_screen)
        side_layout.addWidget(show_pet)
        outer.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 18, 24, 20)
        self.page_title = QLabel("首页")
        self.page_title.setObjectName("pageTitle")
        content_layout.addWidget(self.page_title)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._home_page())
        self.pages.addWidget(self._chat_page())
        self.pages.addWidget(self._knowledge_page())
        self.pages.addWidget(self._practice_page())
        self.pages.addWidget(self._skill_page())
        self.pages.addWidget(self._news_page())
        self.pages.addWidget(self._settings_page())
        content_layout.addWidget(self.pages, 1)
        outer.addWidget(content, 1)
        self.statusBar().showMessage("就绪")

    def _home_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(12)

        summary = QHBoxLayout()
        summary.setSpacing(12)

        def metric(label: str) -> tuple[QFrame, QLabel]:
            frame = panel()
            frame.setMinimumHeight(76)
            metric_layout = QVBoxLayout(frame)
            title = QLabel(label)
            title.setObjectName("muted")
            value = QLabel("-")
            value.setWordWrap(True)
            value.setStyleSheet("font-size:19px;font-weight:700;color:#203d34;")
            metric_layout.addWidget(title)
            metric_layout.addWidget(value)
            summary.addWidget(frame, 1)
            return frame, value

        _, self.home_key_total = metric("今日键盘敲击")
        _, self.home_top_application = metric("最活跃应用")
        _, self.home_activity_range = metric("今日记录时段")
        _, self.home_work_session = metric("当前连续工作")
        layout.addLayout(summary)

        body = QHBoxLayout()
        body.setSpacing(12)
        ranking_panel = panel()
        ranking_layout = QVBoxLayout(ranking_panel)
        ranking_title = QLabel("应用敲击排行")
        ranking_title.setStyleSheet("font-size:15px;font-weight:600;")
        ranking_layout.addWidget(ranking_title)
        self.home_activity_table = QTableWidget(0, 3)
        self.home_activity_table.setHorizontalHeaderLabels(["应用", "敲击", "活动时段"])
        self.home_activity_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.home_activity_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.home_activity_table.verticalHeader().setVisible(False)
        self.home_activity_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.home_activity_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.home_activity_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        ranking_layout.addWidget(self.home_activity_table, 1)
        ranking_panel.setMinimumWidth(330)
        body.addWidget(ranking_panel, 4)

        timeline_panel = panel()
        timeline_layout = QVBoxLayout(timeline_panel)
        timeline_header = QHBoxLayout()
        timeline_title = QLabel("键盘时间分布")
        timeline_title.setStyleSheet("font-size:15px;font-weight:600;")
        self.home_activity_status = QLabel()
        self.home_activity_status.setObjectName("muted")
        timeline_header.addWidget(timeline_title)
        timeline_header.addStretch()
        timeline_header.addWidget(self.home_activity_status)
        timeline_layout.addLayout(timeline_header)
        self.home_timeline = ActivityTimelineWidget()
        timeline_layout.addWidget(self.home_timeline, 1)
        self.home_timeline_legend = QLabel()
        self.home_timeline_legend.setObjectName("muted")
        self.home_timeline_legend.setWordWrap(True)
        timeline_layout.addWidget(self.home_timeline_legend)
        body.addWidget(timeline_panel, 7)
        layout.addLayout(body, 1)
        return page

    def _chat_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(14)
        history_panel = panel()
        history_layout = QVBoxLayout(history_panel)
        history_layout.addWidget(QLabel("文档对话记录"))
        new_button = QPushButton("开始新对话")
        new_button.setProperty("secondary", True)
        new_button.clicked.connect(self.new_conversation)
        history_layout.addWidget(new_button)
        self.conversation_list = QListWidget()
        self.conversation_list.setFixedWidth(220)
        self.conversation_list.itemClicked.connect(self.load_conversation_item)
        history_layout.addWidget(self.conversation_list, 1)
        layout.addWidget(history_panel)

        chat_panel = panel()
        chat_layout = QVBoxLayout(chat_panel)
        source_bar = QHBoxLayout()
        source_bar.addWidget(QLabel("对话文档"))
        self.chat_source_combo = QComboBox()
        self.chat_source_combo.setMinimumWidth(260)
        self.chat_source_combo.currentIndexChanged.connect(self._chat_source_changed)
        source_bar.addWidget(self.chat_source_combo, 1)
        chat_layout.addLayout(source_bar)
        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.setHtml(self._welcome_html())
        chat_layout.addWidget(self.chat_view, 1)
        self.chat_input = QPlainTextEdit()
        self.chat_input.setPlaceholderText("先选择一份文档，再针对其内容提问，Ctrl+Enter 发送")
        self.chat_input.setMaximumHeight(90)
        self.send_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self.chat_input)
        self.send_shortcut.activated.connect(self.send_chat)
        self.send_keypad_shortcut = QShortcut(QKeySequence("Ctrl+Enter"), self.chat_input)
        self.send_keypad_shortcut.activated.connect(self.send_chat)
        chat_layout.addWidget(self.chat_input)
        actions = QHBoxLayout()
        self.chat_status = QLabel("回答会优先引用本地知识库")
        self.chat_status.setObjectName("muted")
        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self.send_chat)
        actions.addWidget(self.chat_status)
        actions.addStretch()
        actions.addWidget(self.send_button)
        chat_layout.addLayout(actions)
        layout.addWidget(chat_panel, 1)
        return page

    def _news_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(14)

        list_panel = panel()
        list_layout = QVBoxLayout(list_panel)
        header = QHBoxLayout()
        header.addWidget(QLabel("最新 AI 简讯"))
        self.news_refresh_button = QPushButton("主动抓取")
        self.news_refresh_button.setProperty("secondary", True)
        self.news_refresh_button.clicked.connect(
            lambda _checked=False: self.start_ai_news_refresh(force=True)
        )
        header.addStretch()
        header.addWidget(self.news_refresh_button)
        list_layout.addLayout(header)
        self.news_status = QLabel("启动后每 8 小时自动更新")
        self.news_status.setObjectName("muted")
        list_layout.addWidget(self.news_status)
        self.news_progress = QProgressBar()
        self.news_progress.setRange(0, 100)
        self.news_progress.setValue(0)
        self.news_progress.hide()
        list_layout.addWidget(self.news_progress)
        self.news_progress_detail = QLabel()
        self.news_progress_detail.setObjectName("muted")
        self.news_progress_detail.setWordWrap(True)
        self.news_progress_detail.hide()
        list_layout.addWidget(self.news_progress_detail)
        self.news_list = QListWidget()
        self.news_list.setMinimumWidth(360)
        self.news_list.itemClicked.connect(self.render_news_item)
        list_layout.addWidget(self.news_list, 1)
        layout.addWidget(list_panel, 4)

        detail_panel = panel()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.addWidget(QLabel("简讯详情"))
        self.news_detail = QTextBrowser()
        self.news_detail.setOpenExternalLinks(False)
        self.news_detail.setHtml(
            "<div style='color:#77817e;'>选择一条简讯查看标题、摘要、发布时间和作者。</div>"
        )
        detail_layout.addWidget(self.news_detail, 1)
        self.news_open_button = QPushButton("打开原文")
        self.news_open_button.setEnabled(False)
        self.news_open_button.clicked.connect(self.open_selected_news_url)
        detail_layout.addWidget(self.news_open_button)
        layout.addWidget(detail_panel, 6)
        return page

    def _knowledge_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        self.knowledge_tabs = QTabWidget()

        def create_module(
            knowledge_type: str,
            description_text: str,
            button_text: str,
            headers: list[str],
        ) -> tuple[QWidget, QTableWidget, QPushButton]:
            module = QWidget()
            module_layout = QVBoxLayout(module)
            module_layout.setContentsMargins(0, 10, 0, 0)
            bar = QHBoxLayout()
            description = QLabel(description_text)
            description.setObjectName("muted")
            import_button = QPushButton(button_text)
            import_button.clicked.connect(
                lambda _checked=False, current=knowledge_type: self.choose_knowledge_files(current)
            )
            delete_button = QPushButton("删除")
            delete_button.setProperty("danger", True)
            bar.addWidget(description)
            bar.addStretch()
            bar.addWidget(delete_button)
            bar.addWidget(import_button)
            module_layout.addLayout(bar)

            table = QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for column in range(1, len(headers)):
                table.horizontalHeader().setSectionResizeMode(
                    column,
                    QHeaderView.ResizeMode.ResizeToContents,
                )
            delete_button.clicked.connect(
                lambda _checked=False, current=table: self.delete_selected_source(current)
            )
            module_layout.addWidget(table, 1)
            return module, table, import_button

        document_module, self.document_source_table, self.document_import_button = create_module(
            "document",
            "导入文档资料，按概念、论点、步骤和因果关系生成练习题。",
            "导入文档知识",
            ["文件名", "上传时间", "题目数量", "气泡出题", "题库"],
        )
        code_module, self.code_source_table, self.code_import_button = create_module(
            "code",
            "导入算法题题解或代码，提取中文题名和摘要，生成主思路、数据结构、边界三道题。",
            "导入代码知识",
            ["题名", "文件名", "上传时间", "题目数量", "气泡出题", "题库"],
        )
        self.import_buttons = {
            "document": self.document_import_button,
            "code": self.code_import_button,
        }
        self.knowledge_tabs.addTab(document_module, "文档知识")
        self.knowledge_tabs.addTab(code_module, "代码知识")
        layout.addWidget(self.knowledge_tabs, 1)
        return page

    def _practice_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        card = panel()
        card.setMaximumWidth(820)
        card_layout = QVBoxLayout(card)
        filters = QHBoxLayout()
        self.practice_source_combo = QComboBox()
        self.practice_source_combo.addItem("全部文档", None)
        filters.addWidget(self.practice_source_combo, 1)
        self.practice_mode_group = QButtonGroup(self)
        self.practice_mode_group.setExclusive(True)
        self.practice_mode_buttons: dict[str, QPushButton] = {}
        for mode, label in (("all", "全部题目"), ("wrong", "错题复习"), ("unanswered", "未回答")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setMinimumWidth(112)
            button.setStyleSheet(
                "QPushButton{background:#e9edef;color:#30363d;}"
                "QPushButton:hover{background:#dce2e5;}"
                "QPushButton:checked{background:#268060;color:white;}"
            )
            self.practice_mode_group.addButton(button)
            self.practice_mode_buttons[mode] = button
            filters.addWidget(button)
        self.practice_mode_buttons["all"].setChecked(True)
        self.practice_source_combo.currentIndexChanged.connect(self._practice_filter_changed)
        self.practice_mode_group.buttonClicked.connect(self._practice_filter_changed)
        card_layout.addLayout(filters)
        self.practice_source = QLabel("尚未加载题目")
        self.practice_source.setObjectName("muted")
        self.practice_prompt = QLabel("先向 Bongo 喂食一个知识文件。")
        self.practice_prompt.setWordWrap(True)
        self.practice_prompt.setStyleSheet("font-size:18px;font-weight:600;padding:12px 0;")
        card_layout.addWidget(self.practice_source)
        card_layout.addWidget(self.practice_prompt)
        self.practice_group = QButtonGroup(self)
        self.practice_options = []
        for index in range(4):
            option = WrappedRadioButton()
            option.setStyleSheet(
                "QRadioButton{background:#eef3f1;border:1px solid #8eb6a7;border-radius:5px;}"
                "QRadioButton:hover{background:#d8e9e3;}"
                "QRadioButton:checked{background:#d8e9e3;border:2px solid #268060;}"
                "QRadioButton::indicator{width:16px;height:16px;margin-left:8px;}"
            )
            self.practice_group.addButton(option, index)
            self.practice_options.append(option)
            card_layout.addWidget(option)
        self.practice_feedback = QLabel()
        self.practice_feedback.setWordWrap(True)
        card_layout.addWidget(self.practice_feedback)
        buttons = QHBoxLayout()
        self.submit_answer_button = QPushButton("提交答案")
        self.submit_answer_button.clicked.connect(self.submit_practice_answer)
        self.next_question_button = QPushButton("换一题")
        self.next_question_button.setProperty("secondary", True)
        self.next_question_button.clicked.connect(lambda: self.load_next_practice(advance=True))
        buttons.addStretch()
        buttons.addWidget(self.next_question_button)
        buttons.addWidget(self.submit_answer_button)
        card_layout.addLayout(buttons)
        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        return page

    def _skill_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)

        toolbar = QHBoxLayout()
        intro = QLabel("把选定知识、题库、错题、对话洞察和学习成长编译为可复用的 Learning Skill。")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        toolbar.addWidget(intro, 1)
        create_button = QPushButton("创建 Skill")
        create_button.clicked.connect(self.create_skill)
        toolbar.addWidget(create_button)
        layout.addLayout(toolbar)

        body = QHBoxLayout()
        self.skill_table = QTableWidget(0, 5)
        self.skill_table.setHorizontalHeaderLabels(["名称", "知识", "题目", "版本", "状态"])
        self.skill_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.skill_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.skill_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.skill_table.verticalHeader().setVisible(False)
        self.skill_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.skill_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.skill_table.itemSelectionChanged.connect(self.show_selected_skill)
        body.addWidget(self.skill_table, 5)

        detail_layout = QVBoxLayout()
        self.skill_preview = QTextBrowser()
        self.skill_preview.setHtml("<p style='color:#687078'>创建或选择一个 Skill 查看沉淀内容。</p>")
        detail_layout.addWidget(self.skill_preview, 1)
        actions = QHBoxLayout()
        self.edit_skill_button = QPushButton("编辑")
        self.edit_skill_button.setProperty("secondary", True)
        self.edit_skill_button.clicked.connect(self.edit_selected_skill)
        self.delete_skill_button = QPushButton("删除")
        self.delete_skill_button.setProperty("danger", True)
        self.delete_skill_button.clicked.connect(self.delete_selected_skill)
        self.export_skill_button = QPushButton("导出")
        self.export_skill_button.clicked.connect(self.export_selected_skill)
        actions.addWidget(self.edit_skill_button)
        actions.addWidget(self.delete_skill_button)
        actions.addStretch()
        actions.addWidget(self.export_skill_button)
        detail_layout.addLayout(actions)
        body.addLayout(detail_layout, 6)
        layout.addLayout(body, 1)
        for button in (self.edit_skill_button, self.delete_skill_button, self.export_skill_button):
            button.setEnabled(False)
        return page

    def _settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        settings = panel()
        settings.setMaximumWidth(820)
        settings_layout = QVBoxLayout(settings)
        tabs = QTabWidget()
        settings_layout.addWidget(tabs)

        model_tab = QWidget()
        form = QVBoxLayout(model_tab)
        form.addWidget(QLabel("导入出题模型（官方 SDK）"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(available_providers())
        form.addWidget(self.provider_combo)
        form.addWidget(QLabel("模型（留空使用后端默认值）"))
        self.model_input = QLineEdit()
        form.addWidget(self.model_input)
        form.addWidget(QLabel("API Key（仅保存在本机 SQLite）"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("OpenAI 或 Anthropic API Key")
        form.addWidget(self.api_key_input)
        form.addWidget(QLabel("兼容接口 Base URL（可选）"))
        self.base_url_input = QLineEdit()
        form.addWidget(self.base_url_input)
        form.addSpacing(12)
        form.addWidget(QLabel("文档对话后端"))
        self.chat_backend_combo = QComboBox()
        backend_labels = {
            "default": "默认",
            "cc": "CC",
            "codex": "Codex",
        }
        for backend in available_chat_backends():
            self.chat_backend_combo.addItem(backend_labels[backend], backend)
        self._last_chat_backend = "default"
        self.chat_backend_combo.currentIndexChanged.connect(self._chat_backend_changed)
        form.addWidget(self.chat_backend_combo)
        env_status = QLabel(
            "默认使用内置上下文；CC 和 Codex 仅在对应 CLI 可执行时可选。"
            "导入和出题始终使用上方配置的官方 SDK。"
        )
        env_status.setObjectName("muted")
        env_status.setWordWrap(True)
        form.addWidget(env_status)
        save = QPushButton("保存设置")
        save.clicked.connect(self.save_settings)
        form.addWidget(save, 0, Qt.AlignmentFlag.AlignRight)
        form.addStretch()
        tabs.addTab(model_tab, "模型与对话")

        pet_tab = QWidget()
        pet_form = QVBoxLayout(pet_tab)
        self.pet_visible_check = QCheckBox("显示桌宠")
        self.pet_always_top_check = QCheckBox("窗口置顶")
        self.pet_pass_through_check = QCheckBox("点击穿透（答题时自动暂停）")
        self.pet_keep_screen_check = QCheckBox("保持在屏幕内")
        self.pet_model_mirror_check = QCheckBox("模型镜像")
        self.pet_mouse_mirror_check = QCheckBox("鼠标镜像")
        self.pet_keyboard_check = QCheckBox("响应键盘")
        self.pet_mouse_check = QCheckBox("响应鼠标")
        for control in (
            self.pet_visible_check,
            self.pet_always_top_check,
            self.pet_pass_through_check,
            self.pet_keep_screen_check,
            self.pet_model_mirror_check,
            self.pet_mouse_mirror_check,
            self.pet_keyboard_check,
            self.pet_mouse_check,
        ):
            pet_form.addWidget(control)
        pet_form.addWidget(QLabel("不透明度"))
        self.pet_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.pet_opacity_slider.setRange(10, 100)
        self.pet_opacity_slider.setTickInterval(10)
        pet_form.addWidget(self.pet_opacity_slider)
        pet_form.addWidget(QLabel("窗口尺寸（%）"))
        self.pet_scale_spin = QSpinBox()
        self.pet_scale_spin.setRange(50, 200)
        self.pet_scale_spin.setSuffix("%")
        pet_form.addWidget(self.pet_scale_spin)
        pet_form.addWidget(QLabel("显示器适配预设"))
        self.pet_display_profile_combo = QComboBox()
        self.pet_display_profile_combo.addItem(
            "笔记本 2880×1800（200% 缩放）",
            "laptop_2880_200",
        )
        self.pet_display_profile_combo.addItem(
            "2K 27 寸显示器（100% 缩放）",
            "desktop_2k_100",
        )
        pet_form.addWidget(self.pet_display_profile_combo)
        profile_hint = QLabel(
            "预设用于校准点击穿透状态下的右键命中区域，不改变上方桌宠尺寸。"
        )
        profile_hint.setObjectName("muted")
        profile_hint.setWordWrap(True)
        pet_form.addWidget(profile_hint)
        pet_form.addWidget(QLabel("题目气泡等待时间"))
        self.pet_question_timeout_spin = QSpinBox()
        self.pet_question_timeout_spin.setRange(10, 300)
        self.pet_question_timeout_spin.setSuffix(" 秒")
        pet_form.addWidget(self.pet_question_timeout_spin)
        pet_save = QPushButton("应用桌宠设置")
        pet_save.clicked.connect(self.save_pet_settings)
        pet_form.addWidget(pet_save, 0, Qt.AlignmentFlag.AlignRight)
        pet_form.addStretch()
        tabs.addTab(pet_tab, "桌宠")

        activity_tab = QWidget()
        activity_form = QVBoxLayout(activity_tab)
        self.activity_tracking_check = QCheckBox("记录匿名键鼠活动")
        activity_form.addWidget(self.activity_tracking_check)
        activity_note = QLabel(
            "仅按 5 分钟时间段保存前台应用进程名、键盘次数、鼠标活跃秒数和点击次数。"
            "不保存具体按键、输入内容、窗口标题、鼠标坐标或完整程序路径。"
            "连续工作 40 分钟后，会将本次会话的进程名和聚合计数发送给当前配置的模型，"
            "用于生成休息提醒。"
        )
        activity_note.setObjectName("muted")
        activity_note.setWordWrap(True)
        activity_form.addWidget(activity_note)
        activity_actions = QHBoxLayout()
        clear_activity = QPushButton("清空活动历史")
        clear_activity.setProperty("danger", True)
        clear_activity.clicked.connect(self.clear_activity_history)
        save_activity = QPushButton("应用活动记录设置")
        save_activity.clicked.connect(self.save_activity_settings)
        activity_actions.addWidget(clear_activity)
        activity_actions.addStretch()
        activity_actions.addWidget(save_activity)
        activity_form.addLayout(activity_actions)
        activity_form.addStretch()
        tabs.addTab(activity_tab, "活动记录")

        layout.addWidget(settings, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        return page

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip("Bongo Study")
        icon = QIcon(str(APP_ICON_PATH))
        if icon.isNull():
            icon = self.style().standardIcon(self.style().StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.tray.setIcon(icon)
        self.tray_menu = QMenu(self)
        self.tray_show_action = QAction("打开学习面板", self)
        self.tray_show_action.triggered.connect(
            lambda _checked=False: self.show_and_raise()
        )
        self.tray_pet_action = QAction("显示桌宠", self)
        self.tray_pet_action.triggered.connect(
            lambda _checked=False: self.pet.show_on_active_screen()
        )
        self.tray_quit_action = QAction("退出", self)
        self.tray_quit_action.triggered.connect(
            lambda _checked=False: self.exit_application()
        )
        self.tray_menu.addAction(self.tray_show_action)
        self.tray_menu.addAction(self.tray_pet_action)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.tray_quit_action)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_and_raise()

    def _connect_pet(self):
        self.pet.answer_selected.connect(self.answer_from_pet)
        self.pet.question_unanswered.connect(self.question_unanswered_from_pet)
        self.pet.open_panel_requested.connect(self.show_and_raise)
        self.pet.open_dashboard_requested.connect(self.show_and_raise)
        self.pet.show_statistics_requested.connect(self.show_statistics_page)
        self.pet.show_ai_news_requested.connect(self.show_ai_news)
        self.pet.news_detail_requested.connect(self.show_news_detail)
        self.pet.news_read_requested.connect(self.mark_ai_news_read)

    def show_ai_news(self) -> None:
        cached = self.service.cached_ai_news()
        if cached and cached.get("items"):
            read_ids = self.service.read_ai_news_ids(cached)
            items = [item for item in cached["items"] if int(item["id"]) not in read_ids]
            if not items:
                self.pet.show_message("本轮 AI 简讯已经全部阅完，可在学习面板中随时查看。", 8000)
                return
            item = dict(items[self.ai_news_cursor % len(items)])
            self.ai_news_cursor = (self.ai_news_cursor + 1) % len(items)
            item["published_at_display"] = self._news_time(item["published_at"])
            self.pet.show_ai_news(item)
            return
        self.ai_news_show_after_refresh = True
        self.pet.show_message("正在生成最新 AI 简讯，请稍候……", 60_000)
        self.start_ai_news_refresh()

    def mark_ai_news_read(self, news_id: int) -> None:
        self.service.mark_ai_news_read(news_id)
        self.ai_news_cursor = 0

    def refresh_ai_news_if_due(self) -> None:
        if self.service.ai_news_due():
            self.start_ai_news_refresh()

    def start_ai_news_refresh(self, force: bool = False) -> None:
        if self.ai_news_worker_active:
            return
        self.ai_news_worker_active = True
        self.news_refresh_button.setEnabled(False)
        self.news_refresh_button.setText("抓取中…")
        self.news_progress.setValue(0)
        self.news_progress.setFormat("0% · 准备抓取")
        self.news_progress.show()
        self.news_progress_detail.setText("正在启动抓取任务")
        self.news_progress_detail.show()
        self.news_status.setText("正在获取来源并生成 20 条中文简讯……")
        worker = Worker(self.service.fetch_ai_news, force)
        worker.kwargs["progress"] = worker.signals.progress.emit
        worker.kwargs["item_completed"] = worker.signals.item_completed.emit
        worker.signals.progress.connect(self._ai_news_progress)
        worker.signals.item_completed.connect(self._ai_news_item_completed)
        worker.signals.result.connect(self._ai_news_completed)
        worker.signals.error.connect(self._ai_news_failed)
        worker.signals.finished.connect(self._finish_ai_news_worker)
        self._start_worker(worker)

    def _ai_news_progress(self, update: dict) -> None:
        percent = max(0, min(100, int(update.get("percent", 0))))
        stage = str(update.get("stage") or "正在抓取")
        detail = str(update.get("detail") or "")
        self.news_progress.setValue(percent)
        self.news_progress.setFormat(f"{percent}% · {stage}")
        self.news_progress_detail.setText(detail or stage)
        self.news_status.setText(stage)

    def _ai_news_completed(self, digest: dict) -> None:
        self.refresh_news(digest)
        if self.ai_news_show_after_refresh:
            self.ai_news_show_after_refresh = False
            self.show_ai_news()

    def _ai_news_item_completed(self, digest: dict) -> None:
        self.refresh_news(digest)

    def _ai_news_failed(self, error: str) -> None:
        self.news_status.setText(f"更新失败：{error}")
        self.news_progress.setFormat("抓取失败")
        self.news_progress_detail.setText(error)
        if self.ai_news_show_after_refresh:
            self.ai_news_show_after_refresh = False
            self.pet.show_message(f"AI 简讯更新失败：{error}", 8000)

    def _finish_ai_news_worker(self) -> None:
        self.ai_news_worker_active = False
        self.news_refresh_button.setEnabled(True)
        self.news_refresh_button.setText("主动抓取")

    def refresh_news(self, digest: dict | None = None) -> None:
        digest = digest or self.service.cached_ai_news()
        self.news_list.clear()
        if not digest:
            self.news_status.setText("暂无简讯，等待首次更新")
            return
        for brief in digest["items"]:
            item = QListWidgetItem(
                f"{brief['title']}\n{self._news_time(brief['published_at'])} · {brief['author']}"
            )
            item.setData(Qt.ItemDataRole.UserRole, brief)
            self.news_list.addItem(item)
        updated = datetime.fromtimestamp(int(digest["fetched_at"])).astimezone()
        completed = len(digest["items"])
        failures = len(digest.get("failures") or [])
        if digest.get("complete") is False:
            processed = int(digest.get("processed") or completed + failures)
            status = f"已完成 {completed} 条 · 正在处理 {processed}/20"
        elif failures:
            status = f"已完成 {completed} 条 · 失败 {failures} 条"
        else:
            status = f"共 {completed} 条"
        self.news_status.setText(
            f"{status} · 更新于 {updated.strftime('%m-%d %H:%M')} · 每 8 小时刷新"
        )

    def render_news_item(self, item: QListWidgetItem) -> None:
        brief = item.data(Qt.ItemDataRole.UserRole) or {}
        title = html.escape(str(brief.get("title") or ""))
        summary = html.escape(str(brief.get("summary") or ""))
        author = html.escape(str(brief.get("author") or "未知作者"))
        original_title = html.escape(str(brief.get("original_title") or ""))
        self.news_detail.setHtml(
            f"<h2 style='color:#203d34;'>{title}</h2>"
            f"<p style='color:#77817e;'>{self._news_time(brief.get('published_at'))} · 作者：{author}</p>"
            f"<p style='font-size:15px;line-height:1.65;color:#303735;'>{summary}</p>"
            f"<hr><p style='color:#77817e;font-size:12px;'>原始标题：{original_title}</p>"
        )
        self.news_open_button.setProperty("url", str(brief.get("original_url") or ""))
        self.news_open_button.setEnabled(bool(brief.get("original_url")))

    def show_news_detail(self, news_id: int) -> None:
        self.show_page(5, "AI 简讯")
        self.show_and_raise()
        for row in range(self.news_list.count()):
            item = self.news_list.item(row)
            brief = item.data(Qt.ItemDataRole.UserRole) or {}
            if int(brief.get("id") or -1) == int(news_id):
                self.news_list.setCurrentItem(item)
                self.render_news_item(item)
                break

    def open_selected_news_url(self) -> None:
        url = str(self.news_open_button.property("url") or "").strip()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    @staticmethod
    def _news_time(value: object) -> str:
        try:
            return datetime.fromisoformat(str(value)).astimezone().strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return "时间未知"

    def show_statistics_page(self) -> None:
        try:
            self.activity_recorder.flush()
            activity_date = datetime.now().astimezone().date().isoformat()
            rows = self.service.database.list_activity_buckets(activity_date)
            summary = self.service.database.get_daily_activity_summary(activity_date)
        except Exception as exc:
            self.pet.show_message(f"今日统计读取失败：{exc}", 6000)
            return

        if not self.activity_recorder.enabled:
            self.pet.show_message(
                "匿名活动记录尚未开启。\n请在学习面板的设置 → 活动记录中开启。",
                7000,
            )
            return
        total_keys = sum(int(item["key_press_count"]) for item in summary)
        work_seconds = self._daily_work_seconds(rows)
        top_application = display_application_name(summary[0]["application"]) if summary else "暂无"
        self.pet.show_statistics(
            self._format_work_duration(work_seconds),
            total_keys,
            top_application,
        )

    @staticmethod
    def _daily_work_seconds(rows: list[dict]) -> int:
        intervals = []
        for row in rows:
            try:
                started_at = datetime.fromisoformat(str(row["first_activity_at"]))
                ended_at = datetime.fromisoformat(str(row["last_activity_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if ended_at < started_at:
                continue
            intervals.append((started_at, ended_at))
        if not intervals:
            return 0

        intervals.sort(key=lambda item: item[0])
        session_start, session_end = intervals[0]
        total_seconds = 0.0
        for started_at, ended_at in intervals[1:]:
            if (started_at - session_end).total_seconds() < 10 * 60:
                session_end = max(session_end, ended_at)
            else:
                total_seconds += max(0.0, (session_end - session_start).total_seconds())
                session_start, session_end = started_at, ended_at
        total_seconds += max(0.0, (session_end - session_start).total_seconds())
        return int(total_seconds)

    def show_page(self, index: int, title: str):
        self.pages.setCurrentIndex(index)
        self.page_title.setText(title)
        if index == 0:
            self.refresh_home()
        elif index == 2:
            self.refresh_sources()
        elif index == 3:
            self.load_next_practice()
        elif index == 4:
            self.refresh_skills()
        elif index == 5:
            self.refresh_news()

    def _start_worker(self, worker: Worker) -> None:
        self.active_workers.add(worker)
        worker.signals.finished.connect(lambda current=worker: self.active_workers.discard(current))
        self.thread_pool.start(worker)

    def refresh_all(self):
        self.refresh_home()
        self.refresh_sources()
        self.refresh_conversations()
        self.refresh_skills()
        self.load_settings()
        self.load_next_practice()

    @staticmethod
    def _activity_clock(value: str) -> str:
        try:
            return datetime.fromisoformat(value).astimezone().strftime("%H:%M")
        except (TypeError, ValueError):
            return "-"

    def refresh_home(self):
        try:
            self.activity_recorder.flush()
            activity_date = datetime.now().astimezone().date().isoformat()
            rows = self.service.database.list_activity_buckets(activity_date)
            summary = self.service.database.get_daily_activity_summary(activity_date)
        except Exception as exc:
            self.home_activity_status.setText("统计读取失败")
            self.statusBar().showMessage(f"活动统计读取失败：{exc}", 5000)
            return

        total_keys = sum(int(item["key_press_count"]) for item in summary)
        self.home_key_total.setText(f"{total_keys:,} 次")
        if summary:
            top = summary[0]
            top_name = display_application_name(top["application"])
            self.home_top_application.setText(top_name)
            self.home_top_application.setToolTip(top_name)
            first = min(str(item["first_activity_at"]) for item in summary)
            last = max(str(item["last_activity_at"]) for item in summary)
            self.home_activity_range.setText(
                f"{self._activity_clock(first)} - {self._activity_clock(last)}"
            )
        else:
            self.home_top_application.setText("暂无")
            self.home_top_application.setToolTip("")
            self.home_activity_range.setText("暂无")

        self.home_activity_table.setRowCount(len(summary))
        for row_index, item in enumerate(summary):
            values = (
                display_application_name(item["application"]),
                f"{int(item['key_press_count']):,}",
                f"{self._activity_clock(item['first_activity_at'])} - "
                f"{self._activity_clock(item['last_activity_at'])}",
            )
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                if column == 1:
                    table_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.home_activity_table.setItem(row_index, column, table_item)

        self.home_timeline.set_activity(rows)
        top_applications = summary[:8]
        legend = []
        for index, item in enumerate(top_applications):
            color = ActivityTimelineWidget.COLORS[index % len(ActivityTimelineWidget.COLORS)]
            legend.append(
                f"<span style='color:{color};font-size:16px;'>■</span> "
                f"{html.escape(display_application_name(item['application']))} {int(item['key_press_count']):,}"
            )
        self.home_timeline_legend.setText("　".join(legend))
        enabled = self.service.database.get_setting("activity_tracking_enabled", "0") == "1"
        if not enabled:
            self.home_activity_status.setText("记录未开启")
        elif not rows:
            self.home_activity_status.setText("等待活动数据")
        else:
            self.home_activity_status.setText("每 30 秒更新")
        self._update_home_work_session(
            self.activity_recorder.get_current_work_session()
        )

    def check_work_session(self) -> None:
        session = self.activity_recorder.get_current_work_session()
        self.pet.set_work_session_tooltip(session)
        self._update_home_work_session(session)
        if (
            session is None
            or session["duration_seconds"] < 40 * 60
            or self.work_break_worker_active
            or not self.pet.can_show_break_reminder()
        ):
            return
        claimed = self.activity_recorder.claim_break_reminder(40)
        if claimed is None:
            return
        self.work_break_worker_active = True
        session_started_at = str(claimed["started_at"])
        self.work_break_session_started_at = session_started_at
        worker = Worker(
            self.service.analyze_work_session,
            self.activity_recorder,
            claimed,
        )
        worker.signals.result.connect(
            lambda result, started_at=session_started_at: self._work_break_completed(
                result, started_at
            )
        )
        worker.signals.error.connect(
            lambda _message, current=claimed, started_at=session_started_at: (
                self._work_break_fallback(current, started_at)
            )
        )
        worker.signals.finished.connect(self._work_break_finished)
        self._start_worker(worker)

    @staticmethod
    def _format_work_duration(seconds: int) -> str:
        minutes = max(0, int(seconds)) // 60
        hours, minutes = divmod(minutes, 60)
        return f"{hours}小时{minutes}分钟" if hours else f"{minutes}分钟"

    def _update_home_work_session(self, session: dict | None) -> None:
        if not self.activity_recorder.enabled:
            self.home_work_session.setText("记录未开启")
            self.home_work_session.setToolTip("请在设置 → 活动记录中开启匿名键鼠活动")
            return
        if not session:
            self.home_work_session.setText("等待活动")
            self.home_work_session.setToolTip("键盘或鼠标活动后开始计时")
            return
        duration = self._format_work_duration(int(session.get("duration_seconds", 0)))
        key_count = int(session.get("key_press_count", 0))
        self.home_work_session.setText(duration)
        self.home_work_session.setToolTip(
            f"本次键盘敲击 {key_count:,} 次；连续 10 分钟无操作后结束计时"
        )

    def _is_current_work_session(self, started_at: str) -> bool:
        session = self.activity_recorder.get_current_work_session()
        return bool(session and session.get("started_at") == started_at)

    def _work_break_completed(self, result: dict, started_at: str) -> None:
        if not self._is_current_work_session(started_at):
            return
        report = result["report"]
        session = self.activity_recorder.get_current_work_session()
        if not session:
            return
        self.pet.show_break_reminder(
            self._format_work_duration(session["duration_seconds"]),
            int(session["key_press_count"]),
            report,
        )

    def _work_break_fallback(self, session: dict, started_at: str) -> None:
        if not self._is_current_work_session(started_at):
            return
        self.pet.show_break_reminder(
            self._format_work_duration(session["duration_seconds"]),
            int(session["key_press_count"]),
        )

    def _work_break_finished(self) -> None:
        self.work_break_worker_active = False
        self.work_break_session_started_at = None

    def refresh_sources(self):
        sources = self.service.database.list_sources()
        document_sources = [source for source in sources if source["knowledge_type"] == "document"]
        code_sources = [source for source in sources if source["knowledge_type"] == "code"]
        self._populate_source_table(self.document_source_table, document_sources, "document")
        self._populate_source_table(self.code_source_table, code_sources, "code")
        self._refresh_source_selectors(sources)

    def _populate_source_table(
        self,
        table: QTableWidget,
        sources: list[dict],
        knowledge_type: str,
    ) -> None:
        table.setRowCount(len(sources))
        for row, source in enumerate(sources):
            primary_text = (
                source.get("problem_title") or Path(source["name"]).stem
                if knowledge_type == "code"
                else source["name"]
            )
            primary = QTableWidgetItem(primary_text)
            primary.setData(Qt.ItemDataRole.UserRole, source["id"])
            primary.setToolTip(source["path"] + (f"\n{source['error']}" if source["error"] else ""))
            try:
                uploaded = datetime.fromisoformat(source["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                uploaded = source["created_at"]
            if knowledge_type == "code":
                values = [
                    primary,
                    QTableWidgetItem(source["name"]),
                    QTableWidgetItem(uploaded),
                    QTableWidgetItem(str(source["question_count"])),
                ]
                bubble_column = 4
                bank_column = 5
            else:
                values = [
                    primary,
                    QTableWidgetItem(uploaded),
                    QTableWidgetItem(str(source["question_count"])),
                ]
                bubble_column = 3
                bank_column = 4
            for column, value in enumerate(values):
                table.setItem(row, column, value)
            bubble_checkbox = QCheckBox()
            bubble_checkbox.setChecked(bool(source.get("bubble_enabled", 1)))
            bubble_checkbox.setToolTip("允许该知识来源的题目出现在桌宠气泡中")
            bubble_checkbox.toggled.connect(
                lambda enabled, source_id=source["id"]: self.service.database.set_source_bubble_enabled(
                    source_id,
                    enabled,
                )
            )
            bubble_cell = QWidget()
            bubble_layout = QHBoxLayout(bubble_cell)
            bubble_layout.setContentsMargins(8, 0, 8, 0)
            bubble_layout.addStretch()
            bubble_layout.addWidget(bubble_checkbox)
            bubble_layout.addStretch()
            table.setCellWidget(row, bubble_column, bubble_cell)
            bank_button = QPushButton("查看题库")
            bank_button.setProperty("secondary", True)
            bank_button.setEnabled(source["question_count"] > 0)
            bank_button.clicked.connect(
                lambda _checked=False, source_id=source["id"]: self.open_question_bank(source_id)
            )
            table.setCellWidget(row, bank_column, bank_button)

    def _refresh_source_selectors(self, sources: list[dict]) -> None:
        chat_selected = self.current_source_id
        practice_selected = self.practice_source_combo.currentData()
        self.chat_source_combo.blockSignals(True)
        self.practice_source_combo.blockSignals(True)
        self.chat_source_combo.clear()
        self.chat_source_combo.addItem("请选择知识文档", None)
        self.practice_source_combo.clear()
        self.practice_source_combo.addItem("全部文档", None)
        for source in sources:
            if source["status"] != "ready":
                continue
            label = source.get("problem_title") or source["name"]
            self.chat_source_combo.addItem(label, source["id"])
            self.practice_source_combo.addItem(label, source["id"])
        chat_index = self.chat_source_combo.findData(chat_selected)
        self.chat_source_combo.setCurrentIndex(max(0, chat_index))
        practice_index = self.practice_source_combo.findData(practice_selected)
        self.practice_source_combo.setCurrentIndex(max(0, practice_index))
        self.chat_source_combo.blockSignals(False)
        self.practice_source_combo.blockSignals(False)

    def open_question_bank(self, source_id: int) -> None:
        QuestionBankDialog(self.service.database, source_id, self).exec()

    def refresh_skills(self) -> None:
        skills = self.service.database.list_learning_skills()
        selected_id = self.current_skill_id
        self.skill_table.blockSignals(True)
        self.skill_table.setRowCount(len(skills))
        selected_row = -1
        for row, skill in enumerate(skills):
            if not skill.get("last_exported_at"):
                status = "未导出"
            elif bool(skill.get("dirty")):
                status = "待更新"
            else:
                status = "最新"
            values = [
                skill["title"],
                str(skill["source_count"]),
                str(skill["question_count"]),
                str(skill["version"] or "-") if skill["version"] else "-",
                status,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, int(skill["id"]))
                    item.setToolTip(f"{skill['name']}\n{skill['description']}")
                self.skill_table.setItem(row, column, item)
            if int(skill["id"]) == selected_id:
                selected_row = row
        self.skill_table.blockSignals(False)
        if skills:
            self.skill_table.selectRow(selected_row if selected_row >= 0 else 0)
        else:
            self.current_skill_id = None
            self.skill_preview.setHtml(
                "<h3>还没有 Learning Skill</h3>"
                "<p style='color:#687078'>创建 Skill 后，可以在这里查看、编辑、删除和导出。</p>"
            )
            for button in (self.edit_skill_button, self.delete_skill_button, self.export_skill_button):
                button.setEnabled(False)

    def show_selected_skill(self) -> None:
        row = self.skill_table.currentRow()
        item = self.skill_table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        self.current_skill_id = int(item.data(Qt.ItemDataRole.UserRole))
        for button in (self.edit_skill_button, self.delete_skill_button, self.export_skill_button):
            button.setEnabled(True)
        try:
            preview = self.service.preview_skill(self.current_skill_id)
        except Exception as exc:
            self.skill_preview.setHtml(f"<p style='color:#b94a48'>{html.escape(str(exc))}</p>")
            return
        skill = preview["skill"]
        source_items = "".join(
            f"<li>{html.escape(source.get('problem_title') or source['name'])}</li>"
            for source in preview["sources"]
        ) or "<li>没有有效知识来源</li>"
        included = ["原始知识"]
        if skill["include_questions"]:
            included.append("完整题库")
        if skill["include_mistakes"]:
            included.append("错题纠正")
        if skill["include_conversations"]:
            included.append("对话洞察")
        if skill["include_growth"]:
            included.append("学习与成长画像")
        growth = preview["growth"]
        unresolved = sum(not bool(item["resolved"]) for item in preview["insights"])
        last_export = skill.get("last_exported_at") or "尚未导出"
        weak_items = [
            f"<li>{html.escape(question['prompt'])}</li>"
            for question in preview["questions"]
            if preview["states"][int(question["id"])]["state"] == "薄弱"
        ][:6]
        insight_items = [
            f"<li>{html.escape(item['question'])}：{html.escape(item['conclusion'] or '尚未形成结论')}</li>"
            for item in preview["insights"]
        ][:6]
        weak_html = "".join(weak_items) or "<li>当前没有需要优先纠正的题目</li>"
        insight_html = "".join(insight_items) or "<li>当前没有对话洞察</li>"
        growth_text = (
            f"<p>成长值 {growth['growth_score']} · 已纠正错题 {growth['recovered_mistakes']} 道 · "
            f"对话结论 {growth['conversation_conclusions']} 个</p>"
            if skill["include_growth"]
            else "<p style='color:#687078'>成长画像未包含在此 Skill 中。</p>"
        )
        self.skill_preview.setHtml(
            f"<h2>{html.escape(skill['title'])}</h2>"
            f"<p><code>{html.escape(skill['name'])}</code></p>"
            f"<p>{html.escape(skill['description'])}</p>"
            f"<p><b>沉淀范围：</b>{'、'.join(included)}</p>"
            f"<h3>知识来源</h3><ul>{source_items}</ul>"
            "<h3>学习状态</h3>"
            f"<p>题目 {len(preview['questions'])} 道 · 历史错题 {preview['historical_mistakes']} 道 · "
            f"当前薄弱 {preview['weak_questions']} 道 · 未解决对话 {unresolved} 个</p>"
            f"<h3>优先复习</h3><ul>{weak_html}</ul>"
            f"<h3>对话洞察</h3><ul>{insight_html}</ul>"
            f"{growth_text}"
            f"<p style='color:#687078'>当前版本：{skill['version'] or '未导出'} · 最近导出：{html.escape(str(last_export))}</p>"
        )

    def create_skill(self) -> None:
        if not any(source["status"] == "ready" for source in self.service.database.list_sources()):
            QMessageBox.information(self, "创建 Skill", "请先导入至少一份可用知识。")
            return
        dialog = SkillEditorDialog(self.service.database, parent=self)
        if not dialog.exec():
            return
        try:
            self.current_skill_id = self.service.create_skill(**dialog.values())
            self.refresh_skills()
            self.statusBar().showMessage("Learning Skill 已创建", 5000)
        except Exception as exc:
            self._show_error(str(exc))

    def edit_selected_skill(self) -> None:
        if self.current_skill_id is None:
            return
        skill = self.service.database.get_learning_skill(self.current_skill_id)
        if not skill:
            self.refresh_skills()
            return
        dialog = SkillEditorDialog(self.service.database, skill, self)
        if not dialog.exec():
            return
        try:
            self.service.update_skill(self.current_skill_id, **dialog.values())
            self.refresh_skills()
            self.statusBar().showMessage("Learning Skill 已更新", 5000)
        except Exception as exc:
            self._show_error(str(exc))

    def delete_selected_skill(self) -> None:
        if self.current_skill_id is None:
            return
        skill = self.service.database.get_learning_skill(self.current_skill_id)
        if not skill:
            self.refresh_skills()
            return
        if QMessageBox.question(
            self,
            "删除 Skill",
            f"删除 {skill['title']} 的 Skill 定义？已经导出到磁盘的目录不会被删除。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.service.database.delete_learning_skill(self.current_skill_id)
        self.current_skill_id = None
        self.refresh_skills()
        self.statusBar().showMessage("Learning Skill 已删除", 5000)

    def export_selected_skill(self) -> None:
        if self.current_skill_id is None:
            return
        skill = self.service.database.get_learning_skill(self.current_skill_id)
        if not skill:
            self.refresh_skills()
            return
        directory = QFileDialog.getExistingDirectory(self, "选择 Skill 导出位置")
        if not directory:
            return
        target = Path(directory) / skill["name"]
        try:
            result = self.service.export_skill(self.current_skill_id, target)
            self.refresh_skills()
            QMessageBox.information(self, "导出完成", f"Skill 已通过结构校验并导出到：\n{result}")
        except Exception as exc:
            self._show_error(str(exc))

    def refresh_conversations(self):
        selected = self.current_conversation_id
        self.conversation_list.clear()
        for conversation in self.service.database.list_conversations():
            item = QListWidgetItem(conversation["title"])
            item.setData(Qt.ItemDataRole.UserRole, conversation["id"])
            source = conversation.get("source_name") or "未绑定文档"
            item.setToolTip(f"文档：{source}\n后端：{conversation['provider']}\n{conversation['updated_at']}")
            self.conversation_list.addItem(item)
            if conversation["id"] == selected:
                self.conversation_list.setCurrentItem(item)

    def new_conversation(self):
        self.current_conversation_id = None
        self.current_source_id = self.chat_source_combo.currentData()
        self.conversation_list.clearSelection()
        self.chat_view.setHtml(self._welcome_html())
        if self.current_source_id is None:
            self.chat_status.setText("请先选择一份知识文档")
        else:
            self.chat_status.setText("新对话将只使用所选文档")
        self.chat_input.setFocus()

    def _chat_source_changed(self) -> None:
        selected = self.chat_source_combo.currentData()
        if selected == self.current_source_id:
            return
        self.current_source_id = selected
        self.current_conversation_id = None
        self.conversation_list.clearSelection()
        self.chat_view.setHtml(self._welcome_html())
        self.chat_status.setText("可以开始文档对话" if selected is not None else "请先选择一份知识文档")

    def load_conversation_item(self, item: QListWidgetItem):
        self.current_conversation_id = int(item.data(Qt.ItemDataRole.UserRole))
        conversation = self.service.database.get_conversation(self.current_conversation_id) or {}
        self.current_source_id = conversation.get("source_id")
        self.chat_source_combo.blockSignals(True)
        index = self.chat_source_combo.findData(self.current_source_id)
        self.chat_source_combo.setCurrentIndex(max(0, index))
        self.chat_source_combo.blockSignals(False)
        self.render_conversation()

    def render_conversation(self):
        if self.current_conversation_id is None:
            self.chat_view.setHtml(self._welcome_html())
            return
        messages = self.service.database.get_messages(self.current_conversation_id, limit=200)
        blocks = []
        for message in messages:
            blocks.append(self._message_html(message["role"], message["content"], message.get("citations", [])))
        self.chat_view.setHtml("".join(blocks) or self._welcome_html())
        self.chat_view.verticalScrollBar().setValue(self.chat_view.verticalScrollBar().maximum())

    def send_chat(self):
        text = self.chat_input.toPlainText().strip()
        if not text or not self.send_button.isEnabled():
            return
        if self.current_source_id is None:
            self.chat_status.setText("请先选择要对话的知识文档")
            return
        self.chat_input.clear()
        self.chat_view.append(self._message_html("user", text))
        self.chat_status.setText("Bongo 正在查找知识并思考...")
        self.send_button.setEnabled(False)
        self.pet.canvas.react("thinking")
        worker = Worker(self.service.chat, self.current_source_id, self.current_conversation_id, text)
        worker.signals.result.connect(self._chat_completed)
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(lambda: self.send_button.setEnabled(True))
        self._start_worker(worker)

    def _chat_completed(self, result: dict):
        self.current_conversation_id = int(result["conversation_id"])
        self.current_source_id = int(result["source_id"])
        self.render_conversation()
        self.refresh_conversations()
        self.chat_status.setText(f"回答已保存 · {result['backend']}")
        self.pet.canvas.react("left")

    def choose_knowledge_files(self, knowledge_type: str):
        patterns = " ".join(f"*{suffix}" for suffix in sorted(SUPPORTED_EXTENSIONS))
        title = "导入算法题题解" if knowledge_type == "code" else "导入文档知识"
        files, _ = QFileDialog.getOpenFileNames(
            self,
            title,
            "",
            f"知识文件 ({patterns});;所有文件 (*)",
        )
        if not files:
            return
        self.import_queue.extend((path, knowledge_type) for path in files)
        if all(button.isEnabled() for button in self.import_buttons.values()):
            self._ingest_next()

    def _ingest_next(self):
        if not self.import_queue:
            self.document_import_button.setEnabled(True)
            self.document_import_button.setText("导入文档知识")
            self.code_import_button.setEnabled(True)
            self.code_import_button.setText("导入代码知识")
            self.refresh_sources()
            self.load_next_practice()
            return
        path, knowledge_type = self.import_queue.popleft()
        self.document_import_button.setText("导入文档知识")
        self.code_import_button.setText("导入代码知识")
        for button in self.import_buttons.values():
            button.setEnabled(False)
        active_button = self.import_buttons[knowledge_type]
        active_button.setText("正在拆解题解..." if knowledge_type == "code" else "正在解析文档...")
        self.statusBar().showMessage(f"正在解析并生成题目：{Path(path).name}")
        self.pet.show_message(f"正在学习 {Path(path).name} ...", 180000)
        worker = Worker(self.service.ingest, path, knowledge_type)
        worker.signals.result.connect(lambda result, filename=Path(path).name: self._ingest_completed(filename, result))
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(self._ingest_next)
        self._start_worker(worker)

    def _ingest_completed(self, filename: str, result: dict):
        if result["created"] or result.get("reprocessed"):
            subject = f"《{result['problem_title']}》" if result.get("problem_title") else filename
            message = f"吃完了 {subject}，我整理出了 {result['questions']} 道题。"
        else:
            message = f"{filename} 已经吃过了，知识没有重复保存。"
        self.pet.show_message(message)
        self.statusBar().showMessage(message, 8000)
        self.refresh_sources()

    def delete_selected_source(self, table: QTableWidget):
        row = table.currentRow()
        if row < 0:
            return
        item = table.item(row, 0)
        if QMessageBox.question(self, "删除知识", f"删除 {item.text()} 及其生成的题目？") != QMessageBox.StandardButton.Yes:
            return
        self.service.database.delete_source(int(item.data(Qt.ItemDataRole.UserRole)))
        self.refresh_sources()
        self.load_next_practice()

    def _practice_filter_changed(self, *_args) -> None:
        self.recent_practice_question_ids.clear()
        self.recent_practice_source_ids.clear()
        self.current_practice_question = None
        self.load_next_practice()

    def _practice_mode(self) -> str:
        for mode, button in self.practice_mode_buttons.items():
            if button.isChecked():
                return mode
        return "all"

    def _refresh_practice_mode_labels(self, source_id: int | None) -> None:
        counts = {
            "all": len(self.service.database.list_questions(source_id=source_id)),
            "wrong": len(self.service.database.list_wrong_questions(source_id=source_id)),
            "unanswered": len(self.service.database.list_unanswered_questions(source_id=source_id)),
        }
        labels = {"all": "全部题目", "wrong": "错题复习", "unanswered": "未回答"}
        for mode, button in self.practice_mode_buttons.items():
            button.setText(f"{labels[mode]} ({counts[mode]})")

    def load_next_practice(self, advance: bool = False):
        previous = self.current_practice_question if advance else None
        previous_id = int(previous["id"]) if previous else None
        if previous is not None:
            self.recent_practice_question_ids.append(previous_id)
            self.recent_practice_source_ids.append(int(previous["source_id"]))
        source_id = self.practice_source_combo.currentData()
        mode = self._practice_mode()
        wrong_only = mode == "wrong"
        unanswered_only = mode == "unanswered"
        self._refresh_practice_mode_labels(source_id)
        question = self.service.database.next_question(
            exclude_id=previous_id,
            exclude_ids=tuple(self.recent_practice_question_ids),
            exclude_source_ids=(
                tuple(self.recent_practice_source_ids)
                if source_id is None and advance
                else None
            ),
            source_id=source_id,
            wrong_only=wrong_only,
            unanswered_only=unanswered_only,
            randomize=True,
        )
        if question is None and source_id is None and advance:
            question = self.service.database.next_question(
                exclude_id=previous_id,
                exclude_ids=tuple(self.recent_practice_question_ids),
                source_id=source_id,
                wrong_only=wrong_only,
                unanswered_only=unanswered_only,
                randomize=True,
            )
        if question is None and previous_id is not None:
            self.recent_practice_question_ids.clear()
            self.recent_practice_source_ids.clear()
            question = self.service.database.next_question(
                exclude_id=previous_id,
                source_id=source_id,
                wrong_only=wrong_only,
                unanswered_only=unanswered_only,
                randomize=True,
            )
        no_alternative = False
        if question is None and previous_id is not None:
            previous = self.service.database.get_question(previous_id)
            if previous and (source_id is None or previous["source_id"] == source_id):
                matches_mode = mode == "all" or (
                    wrong_only and previous["ask_count"] > previous["correct_count"]
                )
                if unanswered_only:
                    matches_mode = any(
                        item["id"] == previous_id
                        for item in self.service.database.list_unanswered_questions(source_id)
                    )
                if matches_mode:
                    question = previous
                    no_alternative = True
        self.current_practice_question = question
        self.practice_group.setExclusive(False)
        for option in self.practice_options:
            option.setChecked(False)
            option.setVisible(bool(question))
        self.practice_group.setExclusive(True)
        self.practice_feedback.clear()
        if not question:
            self.practice_source.setText("当前范围没有可练习的题目")
            empty_messages = {
                "all": "请先导入知识文档并生成题目。",
                "wrong": "还没有错题。答错的题目会自动沉淀在这里。",
                "unanswered": "还没有未回答题目。桌宠题目气泡超时后会加入这里。",
            }
            self.practice_prompt.setText(empty_messages[mode])
            self.submit_answer_button.setEnabled(False)
            self.next_question_button.setEnabled(False)
            return
        self.practice_source.setText(f"来源：{question['source_name']}  ·  {question.get('topic', '')}")
        self.practice_prompt.setText(question["prompt"])
        for index, text in enumerate(question["options"]):
            self.practice_options[index].set_wrapped_text(f"{chr(65 + index)}. {text}")
        self.submit_answer_button.setEnabled(True)
        self.next_question_button.setEnabled(True)
        if no_alternative:
            self.practice_feedback.setText("当前范围只有这一题。")

    def submit_practice_answer(self):
        if not self.current_practice_question:
            return
        selected = self.practice_group.checkedId()
        if selected < 0:
            self.practice_feedback.setText("请先选择一个答案。")
            return
        result = self.service.database.answer_question(self.current_practice_question["id"], selected)
        question = result["question"]
        color = "#176b4d" if result["correct"] else "#a33d39"
        prefix = "回答正确。" if result["correct"] else f"回答错误，正确答案是 {chr(65 + question['correct_index'])}。"
        self.practice_feedback.setStyleSheet(f"color:{color};font-weight:600;padding:10px 0;")
        self.practice_feedback.setText(prefix + "\n" + question["explanation"])
        self.submit_answer_button.setEnabled(False)
        self._refresh_practice_mode_labels(self.practice_source_combo.currentData())

    def answer_from_pet(self, question_id: int, selected_index: int):
        result = self.service.database.answer_question(question_id, selected_index)
        self.pet.set_answer_feedback(result["correct"], result["question"]["explanation"])
        self._refresh_practice_mode_labels(self.practice_source_combo.currentData())

    def question_unanswered_from_pet(self, question_id: int) -> None:
        created = self.service.database.mark_question_unanswered(question_id)
        self._refresh_practice_mode_labels(self.practice_source_combo.currentData())
        if created:
            self.statusBar().showMessage("题目气泡已超时，已加入未回答列表", 6000)
        if self.pages.currentIndex() == 2 and self._practice_mode() == "unanswered":
            self.current_practice_question = None
            self.load_next_practice()

    def _chat_backend_changed(self, *_args) -> None:
        backend = str(self.chat_backend_combo.currentData())
        if chat_backend_available(backend):
            self._last_chat_backend = backend
            return
        cli_name = "Claude Code" if backend == "cc" else "Codex"
        QMessageBox.warning(
            self,
            "对话后端不可用",
            f"没有找到可执行的 {cli_name} CLI，请先安装并加入 PATH。",
        )
        fallback_index = self.chat_backend_combo.findData(self._last_chat_backend)
        self.chat_backend_combo.blockSignals(True)
        self.chat_backend_combo.setCurrentIndex(max(0, fallback_index))
        self.chat_backend_combo.blockSignals(False)

    def load_settings(self):
        config = self.service.provider_config()
        index = self.provider_combo.findText(config.name)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)
        self.model_input.setText(config.model)
        self.api_key_input.setText(config.api_key)
        self.base_url_input.setText(config.base_url)
        backend = self.service.chat_backend()
        if not chat_backend_available(backend):
            backend = "default"
            self.service.database.set_setting("chat_backend", backend)
        backend_index = self.chat_backend_combo.findData(backend)
        self.chat_backend_combo.blockSignals(True)
        self.chat_backend_combo.setCurrentIndex(max(0, backend_index))
        self.chat_backend_combo.blockSignals(False)
        self._last_chat_backend = backend

        database = self.service.database
        checks = {
            self.pet_visible_check: ("pet_visible", "1"),
            self.pet_always_top_check: ("pet_always_on_top", "1"),
            self.pet_pass_through_check: ("pet_pass_through", "0"),
            self.pet_keep_screen_check: ("pet_keep_in_screen", "1"),
            self.pet_model_mirror_check: ("pet_model_mirror", "0"),
            self.pet_mouse_mirror_check: ("pet_mouse_mirror", "0"),
            self.pet_keyboard_check: ("pet_keyboard_enabled", "1"),
            self.pet_mouse_check: ("pet_mouse_enabled", "1"),
        }
        for control, (key, default) in checks.items():
            control.setChecked(database.get_setting(key, default) == "1")
        self.activity_tracking_check.setChecked(
            database.get_setting("activity_tracking_enabled", "0") == "1"
        )
        self.pet_opacity_slider.setValue(int(database.get_setting("pet_opacity", "100")))
        self.pet_scale_spin.setValue(int(database.get_setting("pet_scale", "100")))
        display_profile = database.get_setting(
            "pet_display_profile",
            "laptop_2880_200",
        )
        profile_index = self.pet_display_profile_combo.findData(display_profile)
        self.pet_display_profile_combo.setCurrentIndex(max(0, profile_index))
        self.pet_question_timeout_spin.setValue(
            int(database.get_setting("pet_question_timeout", "45"))
        )
        self.pet.apply_settings(self._pet_settings_from_ui(), update_visibility=False)
        if not self.pet_enabled:
            self.pet.hide()
        x = database.get_setting("pet_x", "")
        y = database.get_setting("pet_y", "")
        if x and y:
            self.pet.move(int(x), int(y))
            self.pet.keep_inside_screen()

    def save_settings(self):
        self.service.set_provider(
            self.provider_combo.currentText(),
            self.model_input.text().strip(),
            self.base_url_input.text().strip(),
            self.api_key_input.text().strip(),
        )
        self.service.set_chat_backend(str(self.chat_backend_combo.currentData()))
        self.statusBar().showMessage("模型设置已保存", 5000)
        self.pet.show_message("新的大脑设置已经记住了。")

    def _pet_settings_from_ui(self) -> PetSettings:
        return PetSettings(
            visible=self.pet_visible_check.isChecked(),
            opacity=self.pet_opacity_slider.value(),
            scale=self.pet_scale_spin.value(),
            always_on_top=self.pet_always_top_check.isChecked(),
            pass_through=self.pet_pass_through_check.isChecked(),
            keep_in_screen=self.pet_keep_screen_check.isChecked(),
            model_mirror=self.pet_model_mirror_check.isChecked(),
            mouse_mirror=self.pet_mouse_mirror_check.isChecked(),
            keyboard_enabled=self.pet_keyboard_check.isChecked(),
            mouse_enabled=self.pet_mouse_check.isChecked(),
            question_timeout=self.pet_question_timeout_spin.value(),
            display_profile=str(self.pet_display_profile_combo.currentData()),
        )

    def save_pet_settings(self):
        settings = self._pet_settings_from_ui()
        values = {
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
        for key, value in values.items():
            self.service.database.set_setting(key, str(int(value)) if isinstance(value, bool) else str(value))
        self.pet.apply_settings(settings)
        self.statusBar().showMessage("桌宠设置已应用", 5000)

    def save_activity_settings(self):
        enabled = self.activity_tracking_check.isChecked()
        self.service.database.set_setting("activity_tracking_enabled", str(int(enabled)))
        self.activity_recorder.set_enabled(enabled)
        state = "已开始记录匿名活动" if enabled else "活动记录已暂停"
        self.statusBar().showMessage(state, 5000)
        self.refresh_home()

    def clear_activity_history(self):
        confirmed = QMessageBox.question(
            self,
            "清空活动历史",
            "确定删除本机保存的全部键鼠活动统计吗？此操作不可撤销。",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        was_enabled = self.activity_recorder.enabled
        self.activity_recorder.set_enabled(False)
        self.activity_recorder.flush()
        deleted = self.service.database.clear_activity_history()
        self.activity_recorder.set_enabled(was_enabled)
        self.statusBar().showMessage(f"已清空 {deleted} 条活动统计", 5000)
        self.refresh_home()

    def _save_pet_position(self, x: int, y: int) -> None:
        self.service.database.set_setting("pet_x", str(x))
        self.service.database.set_setting("pet_y", str(y))

    def show_and_raise(self):
        state = self.windowState()
        if state & Qt.WindowState.WindowMinimized:
            self.setWindowState(
                (state & ~Qt.WindowState.WindowMinimized)
                | Qt.WindowState.WindowActive
            )
        self.show()
        self._ensure_window_on_screen()
        QTimer.singleShot(0, self._activate_visible_window)

    def _ensure_window_on_screen(self) -> None:
        frame = self.frameGeometry()
        if any(screen.availableGeometry().intersects(frame) for screen in QApplication.screens()):
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        bounds = screen.availableGeometry()
        self.move(
            bounds.center().x() - self.width() // 2,
            bounds.center().y() - self.height() // 2,
        )

    def _activate_visible_window(self) -> None:
        self.raise_()
        self.activateWindow()
        if os.name == "nt":
            handle = int(self.winId())
            user32 = ctypes.windll.user32
            user32.ShowWindow(handle, 9)
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
        QApplication.alert(self, 0)

    def exit_application(self):
        if self.active_workers:
            QMessageBox.information(self, "任务进行中", "请等待当前模型请求完成后再退出应用。")
            return
        self.force_exit = True
        self.pet.stop_input_monitor()
        self.pet.close()
        self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event: QCloseEvent):
        if self.force_exit or not QSystemTrayIcon.isSystemTrayAvailable():
            event.accept()
        else:
            event.ignore()
            self.hide()

    def _show_error(self, message: str):
        self.statusBar().showMessage("操作失败", 8000)
        self.chat_status.setText("操作失败，请检查模型设置")
        self.pet.show_message("我没有学会这次内容，请检查模型设置。")
        QMessageBox.critical(self, "Bongo Study", message)

    @staticmethod
    def _welcome_html() -> str:
        return (
            "<div style='padding:28px;color:#4a5259'>"
            "<h2 style='color:#202429'>和 Bongo 一起学习</h2>"
            "<p>先在知识库导入 Markdown 或代码文件，然后针对这些资料提问。</p>"
            "<p>对话、问题和回答都保存在本机。</p></div>"
        )

    @staticmethod
    def _message_html(role: str, content: str, citations=None) -> str:
        is_user = role == "user"
        name = "你" if is_user else "Bongo"
        background = "#e5f1ec" if is_user else "#ffffff"
        border = "#8eb6a7" if is_user else "#d7dcdf"
        text = html.escape(content).replace("\n", "<br>")
        citation_text = ""
        if citations:
            labels = [f"[{item['index']}] {html.escape(item['source'])}" for item in citations]
            citation_text = f"<div style='color:#687078;font-size:11px;margin-top:8px'>来源：{' · '.join(labels)}</div>"
        return (
            f"<div style='margin:10px 6px;padding:12px;background:{background};border:1px solid {border};border-radius:6px'>"
            f"<div style='font-weight:700;margin-bottom:6px'>{name}</div><div>{text}</div>{citation_text}</div>"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bongo Study desktop learning companion")
    parser.add_argument("--data-dir", default=None, help="Override local application data directory")
    parser.add_argument("--no-pet", action="store_true", help="Start without showing the desktop pet")
    parser.add_argument("--smoke-test", action="store_true", help="Start offscreen and exit automatically")
    parser.add_argument("--legacy-ui", action="store_true", help="Use the legacy QWidget management panel")
    return parser


def _instance_name(data_dir: Path) -> str:
    digest = hashlib.sha256(str(data_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"BongoStudy-{digest}"


def _notify_existing_instance(name: str) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(300):
        return False
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
        os.environ["BONGO_DISABLE_GLOBAL_INPUT"] = "1"
    else:
        os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    if os.name == "nt":
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)
            )
        except (AttributeError, OSError):
            pass
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BongoStudy.Desktop")
        except OSError:
            pass
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Bongo Study")
    app.setOrganizationName("Bongo Study")
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(APP_STYLE)
    service = LearningService(args.data_dir)
    instance_name = _instance_name(service.data_dir)
    if _notify_existing_instance(instance_name):
        service.close()
        return 0
    instance_server = QLocalServer(app)
    if not instance_server.listen(instance_name):
        if _notify_existing_instance(instance_name):
            service.close()
            return 0
        QLocalServer.removeServer(instance_name)
        if not instance_server.listen(instance_name):
            service.close()
            raise RuntimeError(f"无法创建单实例服务：{instance_server.errorString()}")
    crash_log = (service.data_dir / "crash.log").open("a", encoding="utf-8")
    faulthandler.enable(crash_log)
    recent_pet_source_ids: deque[int] = deque(maxlen=2)

    def next_pet_question():
        question = service.database.next_question(
            bubble_only=True,
            exclude_source_ids=tuple(recent_pet_source_ids),
        )
        if question is None and recent_pet_source_ids:
            # A small selected scope may contain only one or two sources. In that
            # case keep producing questions instead of enforcing source diversity.
            question = service.database.next_question(bubble_only=True)
        if question:
            # Persist recency when the question is selected for an actual bubble.
            # This keeps repeated timer ticks from favoring one early-imported item.
            service.database.mark_question_bubbled(int(question["id"]))
            recent_pet_source_ids.append(int(question["source_id"]))
        return question

    activity_recorder = ActivityRecorder(
        service.database,
        enabled=service.database.get_setting("activity_tracking_enabled", "0") == "1",
    )
    activity_sample_timer = QTimer(app)
    activity_sample_timer.setInterval(1_000)
    activity_sample_timer.timeout.connect(activity_recorder.sample_foreground)
    if not args.smoke_test:
        activity_sample_timer.start()
    pet = PetWindow(next_pet_question, activity_recorder.record)
    bridge = None
    engine = None
    if args.legacy_ui:
        window = MainWindow(
            service,
            pet,
            activity_recorder,
            start_hidden=args.smoke_test,
            pet_enabled=not args.no_pet and not args.smoke_test,
        )
        activity_flush_timer = QTimer(app)
        activity_flush_timer.setInterval(30_000)
        activity_flush_timer.timeout.connect(activity_recorder.flush)
        activity_flush_timer.start()
    else:
        bridge = BongoBridge(
            service,
            pet,
            activity_recorder,
            APP_ICON_PATH,
            pet_enabled=not args.no_pet and not args.smoke_test,
            start_background_tasks=not args.smoke_test,
        )
        engine = QQmlApplicationEngine()
        engine.warnings.connect(
            lambda warnings: [print(error.toString(), file=sys.stderr) for error in warnings]
        )
        engine.rootContext().setContextProperty("bridge", bridge)
        qml_path = Path(__file__).parent / "qml" / "Main.qml"
        engine.load(QUrl.fromLocalFile(str(qml_path)))
        if not engine.rootObjects():
            bridge.shutdown()
            service.close()
            crash_log.close()
            raise RuntimeError(f"无法加载 QML 界面：{qml_path}")
        window = engine.rootObjects()[0]
        window.setIcon(QIcon(str(APP_ICON_PATH)))
        bridge.attachWindow(window)
        _apply_windows_title_bar_theme(window)
        if args.smoke_test:
            window.hide()

    def activate_existing_window() -> None:
        while instance_server.hasPendingConnections():
            connection = instance_server.nextPendingConnection()
            if bridge is not None:
                bridge.show_window()
            else:
                window.show_and_raise()
            connection.disconnectFromServer()

    instance_server.newConnection.connect(activate_existing_window)
    if not args.no_pet and not args.smoke_test:
        if not service.database.get_setting("pet_x", ""):
            screen = app.primaryScreen().availableGeometry()
            pet.move(screen.right() - pet.width() - 24, screen.bottom() - pet.height() - 20)
        pet.setVisible(pet.pet_settings.visible)
        pet.start_input_monitor()
    if args.smoke_test:
        if bridge is not None:
            for index in range(7):
                QTimer.singleShot(120 + index * 120, lambda page=index: window.setProperty("currentPage", page))
            QTimer.singleShot(1100, app.quit)
        else:
            QTimer.singleShot(800, app.quit)
    try:
        return app.exec()
    finally:
        if bridge is not None:
            bridge.shutdown()
        else:
            pet.stop_input_monitor()
            activity_recorder.flush()
        service.close()
        crash_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
