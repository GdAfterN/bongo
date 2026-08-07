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

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
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
from .dialogs import QuestionBankDialog
from .pet import PetSettings, PetWindow
from .providers import available_chat_backends, chat_backend_available, available_providers
from .service import LearningService
from .styles import APP_STYLE
from .widgets import WrappedRadioButton


APP_ICON_PATH = Path(__file__).parent / "assets" / "app-icon.ico"


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
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
        start_hidden: bool = False,
        pet_enabled: bool = True,
    ):
        super().__init__()
        self.service = service
        self.pet = pet
        self.pet_enabled = pet_enabled
        self.thread_pool = QThreadPool.globalInstance()
        self.active_workers: set[Worker] = set()
        self.current_conversation_id: int | None = None
        self.current_source_id: int | None = None
        self.current_practice_question: dict | None = None
        self.import_queue: deque[tuple[str, str]] = deque()
        self.force_exit = False
        self.setWindowTitle("Bongo Study")
        self.setMinimumSize(980, 680)
        self.resize(1180, 760)
        self._build_ui()
        self._build_tray()
        self._connect_pet()
        self.refresh_all()
        self.pet.position_changed.connect(self._save_pet_position)

        self.bubble_timer = QTimer(self)
        self.bubble_timer.timeout.connect(self.pet.show_next_question)
        interval = int(self.service.database.get_setting("bubble_interval", "180"))
        self.bubble_timer.start(max(30, interval) * 1000)
        if not start_hidden:
            self.show()

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
        nav_items = [("对话", 0), ("知识库", 1), ("练习", 2), ("设置", 3)]
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
        show_pet.clicked.connect(self.pet.show)
        side_layout.addWidget(show_pet)
        outer.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 18, 24, 20)
        self.page_title = QLabel("对话")
        self.page_title.setObjectName("pageTitle")
        content_layout.addWidget(self.page_title)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._chat_page())
        self.pages.addWidget(self._knowledge_page())
        self.pages.addWidget(self._practice_page())
        self.pages.addWidget(self._settings_page())
        content_layout.addWidget(self.pages, 1)
        outer.addWidget(content, 1)
        self.statusBar().showMessage("就绪")

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

        export_tab = QWidget()
        export_form = QVBoxLayout(export_tab)
        export_form.addWidget(QLabel("学习 Skill"))
        export_help = QLabel("将知识来源、练习题和会话索引导出为可复用的本地 skill。")
        export_help.setObjectName("muted")
        export_help.setWordWrap(True)
        export_form.addWidget(export_help)
        export_button = QPushButton("导出学习 Skill")
        export_button.setProperty("secondary", True)
        export_button.clicked.connect(self.export_skill)
        export_form.addWidget(export_button, 0, Qt.AlignmentFlag.AlignLeft)
        export_form.addStretch()
        tabs.addTab(export_tab, "导出")
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
        menu = QMenu()
        show_action = QAction("打开学习面板", self)
        show_action.triggered.connect(self.show_and_raise)
        pet_action = QAction("显示桌宠", self)
        pet_action.triggered.connect(self.pet.show)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.exit_application)
        menu.addAction(show_action)
        menu.addAction(pet_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_and_raise() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _connect_pet(self):
        self.pet.answer_selected.connect(self.answer_from_pet)
        self.pet.question_unanswered.connect(self.question_unanswered_from_pet)

    def show_page(self, index: int, title: str):
        self.pages.setCurrentIndex(index)
        self.page_title.setText(title)
        if index == 1:
            self.refresh_sources()
        elif index == 2:
            self.load_next_practice()

    def _start_worker(self, worker: Worker) -> None:
        self.active_workers.add(worker)
        worker.signals.finished.connect(lambda current=worker: self.active_workers.discard(current))
        self.thread_pool.start(worker)

    def refresh_all(self):
        self.refresh_sources()
        self.refresh_conversations()
        self.load_settings()
        self.load_next_practice()

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
        QTimer.singleShot(4500, self.pet.show_next_question)

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
        self.load_next_practice(advance=True)

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
        previous_id = (
            int(self.current_practice_question["id"])
            if advance and self.current_practice_question
            else None
        )
        source_id = self.practice_source_combo.currentData()
        mode = self._practice_mode()
        wrong_only = mode == "wrong"
        unanswered_only = mode == "unanswered"
        self._refresh_practice_mode_labels(source_id)
        question = self.service.database.next_question(
            exclude_id=previous_id,
            source_id=source_id,
            wrong_only=wrong_only,
            unanswered_only=unanswered_only,
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
        self.pet_opacity_slider.setValue(int(database.get_setting("pet_opacity", "100")))
        self.pet_scale_spin.setValue(int(database.get_setting("pet_scale", "100")))
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
        }
        for key, value in values.items():
            self.service.database.set_setting(key, str(int(value)) if isinstance(value, bool) else str(value))
        self.pet.apply_settings(settings)
        self.statusBar().showMessage("桌宠设置已应用", 5000)

    def _save_pet_position(self, x: int, y: int) -> None:
        self.service.database.set_setting("pet_x", str(x))
        self.service.database.set_setting("pet_y", str(y))

    def export_skill(self):
        directory = QFileDialog.getExistingDirectory(self, "选择 Skill 导出目录")
        if not directory:
            return
        target = Path(directory) / "bongo-learning-profile"
        try:
            result = self.service.export_skill(target)
            QMessageBox.information(self, "导出完成", f"Skill 已导出到：\n{result}")
        except Exception as exc:
            self._show_error(str(exc))

    def show_and_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()

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
            self.tray.showMessage("Bongo Study", "学习伙伴仍在桌面上运行。", QSystemTrayIcon.MessageIcon.Information, 2500)

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
        os.environ["BONGO_DISABLE_GLOBAL_INPUT"] = "1"
    if os.name == "nt":
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
    pet = PetWindow(lambda: service.database.next_question(bubble_only=True))
    window = MainWindow(
        service,
        pet,
        start_hidden=args.smoke_test,
        pet_enabled=not args.no_pet and not args.smoke_test,
    )

    def activate_existing_window() -> None:
        while instance_server.hasPendingConnections():
            connection = instance_server.nextPendingConnection()
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
        QTimer.singleShot(800, app.quit)
    try:
        return app.exec()
    finally:
        pet.stop_input_monitor()
        service.close()
        crash_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
