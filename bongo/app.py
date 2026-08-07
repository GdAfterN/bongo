from __future__ import annotations

import argparse
import html
import os
import sys
import traceback
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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
    QRadioButton,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .ingestion import SUPPORTED_EXTENSIONS
from .pet import PetWindow
from .providers import ProviderError, available_providers
from .service import LearningService
from .styles import APP_STYLE


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
    def __init__(self, service: LearningService, pet: PetWindow, start_hidden: bool = False):
        super().__init__()
        self.service = service
        self.pet = pet
        self.thread_pool = QThreadPool.globalInstance()
        self.current_conversation_id: int | None = None
        self.current_practice_question: dict | None = None
        self.import_queue: deque[str] = deque()
        self.force_exit = False
        self.setWindowTitle("Bongo Study")
        self.setMinimumSize(980, 680)
        self.resize(1180, 760)
        self._build_ui()
        self._build_tray()
        self._connect_pet()
        self.refresh_all()

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
        history_layout.addWidget(QLabel("历史对话"))
        new_button = QPushButton("新建对话")
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
        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.setHtml(self._welcome_html())
        chat_layout.addWidget(self.chat_view, 1)
        self.chat_input = QPlainTextEdit()
        self.chat_input.setPlaceholderText("针对已喂食的知识提问，Ctrl+Enter 发送")
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
        bar = QHBoxLayout()
        description = QLabel("导入 Markdown、文本或代码文件，Bongo 会立即生成练习题。")
        description.setObjectName("muted")
        self.import_button = QPushButton("导入知识")
        self.import_button.clicked.connect(self.choose_knowledge_files)
        self.delete_source_button = QPushButton("删除")
        self.delete_source_button.setProperty("danger", True)
        self.delete_source_button.clicked.connect(self.delete_selected_source)
        bar.addWidget(description)
        bar.addStretch()
        bar.addWidget(self.delete_source_button)
        bar.addWidget(self.import_button)
        layout.addLayout(bar)
        table_panel = panel()
        table_layout = QVBoxLayout(table_panel)
        self.source_table = QTableWidget(0, 5)
        self.source_table.setHorizontalHeaderLabels(["文件", "类型", "片段", "题目", "状态"])
        self.source_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.source_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.source_table.verticalHeader().setVisible(False)
        self.source_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.source_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        table_layout.addWidget(self.source_table)
        layout.addWidget(table_panel, 1)
        return page

    def _practice_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        card = panel()
        card.setMaximumWidth(820)
        card_layout = QVBoxLayout(card)
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
            option = QRadioButton()
            option.setStyleSheet("QRadioButton{padding:8px;} QRadioButton::indicator{width:16px;height:16px;}")
            self.practice_group.addButton(option, index)
            self.practice_options.append(option)
            card_layout.addWidget(option)
        self.practice_feedback = QLabel()
        self.practice_feedback.setWordWrap(True)
        card_layout.addWidget(self.practice_feedback)
        buttons = QHBoxLayout()
        self.submit_answer_button = QPushButton("提交答案")
        self.submit_answer_button.clicked.connect(self.submit_practice_answer)
        next_button = QPushButton("换一题")
        next_button.setProperty("secondary", True)
        next_button.clicked.connect(self.load_next_practice)
        buttons.addStretch()
        buttons.addWidget(next_button)
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
        settings.setMaximumWidth(760)
        form = QVBoxLayout(settings)
        form.addWidget(QLabel("对话与出题后端"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(available_providers())
        form.addWidget(self.provider_combo)
        form.addWidget(QLabel("模型（留空使用后端默认值）"))
        self.model_input = QLineEdit()
        form.addWidget(self.model_input)
        form.addWidget(QLabel("兼容接口 Base URL（可选）"))
        self.base_url_input = QLineEdit()
        form.addWidget(self.base_url_input)
        env_status = QLabel(
            "API Key 从环境变量 OPENAI_API_KEY / ANTHROPIC_API_KEY 读取，不写入本地数据库。\n"
            "Claude Code 后端复用系统中的 claude 登录状态，并强制禁用全部工具。"
        )
        env_status.setObjectName("muted")
        env_status.setWordWrap(True)
        form.addWidget(env_status)
        save = QPushButton("保存设置")
        save.clicked.connect(self.save_settings)
        form.addWidget(save, 0, Qt.AlignmentFlag.AlignRight)
        form.addSpacing(18)
        form.addWidget(QLabel("导出"))
        export_help = QLabel("将知识来源、练习题和会话索引导出为可复用的本地 skill。")
        export_help.setObjectName("muted")
        form.addWidget(export_help)
        export_button = QPushButton("导出学习 Skill")
        export_button.setProperty("secondary", True)
        export_button.clicked.connect(self.export_skill)
        form.addWidget(export_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(settings, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        return page

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip("Bongo Study")
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

    def show_page(self, index: int, title: str):
        self.pages.setCurrentIndex(index)
        self.page_title.setText(title)
        if index == 1:
            self.refresh_sources()
        elif index == 2:
            self.load_next_practice()

    def refresh_all(self):
        self.refresh_sources()
        self.refresh_conversations()
        self.load_settings()
        self.load_next_practice()

    def refresh_sources(self):
        sources = self.service.database.list_sources()
        self.source_table.setRowCount(len(sources))
        for row, source in enumerate(sources):
            name = QTableWidgetItem(source["name"])
            name.setData(Qt.ItemDataRole.UserRole, source["id"])
            name.setToolTip(source["path"] + (f"\n{source['error']}" if source["error"] else ""))
            values = [name, QTableWidgetItem(source["kind"] or "text"), QTableWidgetItem(str(source["chunk_count"])),
                      QTableWidgetItem(str(source["question_count"])), QTableWidgetItem(source["status"])]
            for column, value in enumerate(values):
                self.source_table.setItem(row, column, value)

    def refresh_conversations(self):
        selected = self.current_conversation_id
        self.conversation_list.clear()
        for conversation in self.service.database.list_conversations():
            item = QListWidgetItem(conversation["title"])
            item.setData(Qt.ItemDataRole.UserRole, conversation["id"])
            item.setToolTip(conversation["updated_at"])
            self.conversation_list.addItem(item)
            if conversation["id"] == selected:
                self.conversation_list.setCurrentItem(item)

    def new_conversation(self):
        self.current_conversation_id = None
        self.conversation_list.clearSelection()
        self.chat_view.setHtml(self._welcome_html())
        self.chat_input.setFocus()

    def load_conversation_item(self, item: QListWidgetItem):
        self.current_conversation_id = int(item.data(Qt.ItemDataRole.UserRole))
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
        self.chat_input.clear()
        self.chat_view.append(self._message_html("user", text))
        self.chat_status.setText("Bongo 正在查找知识并思考...")
        self.send_button.setEnabled(False)
        self.pet.canvas.react("thinking")
        worker = Worker(self.service.chat, self.current_conversation_id, text)
        worker.signals.result.connect(self._chat_completed)
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(lambda: self.send_button.setEnabled(True))
        self.thread_pool.start(worker)

    def _chat_completed(self, result: dict):
        self.current_conversation_id = int(result["conversation_id"])
        self.render_conversation()
        self.refresh_conversations()
        self.chat_status.setText("回答已保存，可随时恢复")
        self.pet.canvas.react("left")

    def choose_knowledge_files(self):
        patterns = " ".join(f"*{suffix}" for suffix in sorted(SUPPORTED_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "向 Bongo 喂食知识", "", f"知识文件 ({patterns});;所有文件 (*)")
        if not files:
            return
        self.import_queue.extend(files)
        if self.import_button.isEnabled():
            self._ingest_next()

    def _ingest_next(self):
        if not self.import_queue:
            self.import_button.setEnabled(True)
            self.import_button.setText("导入知识")
            self.refresh_sources()
            self.load_next_practice()
            return
        path = self.import_queue.popleft()
        self.import_button.setEnabled(False)
        self.import_button.setText("正在喂食...")
        self.statusBar().showMessage(f"正在解析并生成题目：{Path(path).name}")
        self.pet.show_message(f"正在学习 {Path(path).name} ...", 180000)
        worker = Worker(self.service.ingest, path)
        worker.signals.result.connect(lambda result, filename=Path(path).name: self._ingest_completed(filename, result))
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(self._ingest_next)
        self.thread_pool.start(worker)

    def _ingest_completed(self, filename: str, result: dict):
        if result["created"] or result.get("reprocessed"):
            message = f"吃完了 {filename}，我整理出了 {result['questions']} 道题。"
        else:
            message = f"{filename} 已经吃过了，知识没有重复保存。"
        self.pet.show_message(message)
        self.statusBar().showMessage(message, 8000)
        self.refresh_sources()
        QTimer.singleShot(4500, self.pet.show_next_question)

    def delete_selected_source(self):
        row = self.source_table.currentRow()
        if row < 0:
            return
        item = self.source_table.item(row, 0)
        if QMessageBox.question(self, "删除知识", f"删除 {item.text()} 及其生成的题目？") != QMessageBox.StandardButton.Yes:
            return
        self.service.database.delete_source(int(item.data(Qt.ItemDataRole.UserRole)))
        self.refresh_sources()
        self.load_next_practice()

    def load_next_practice(self):
        question = self.service.database.next_question()
        self.current_practice_question = question
        self.practice_group.setExclusive(False)
        for option in self.practice_options:
            option.setChecked(False)
            option.setVisible(bool(question))
        self.practice_group.setExclusive(True)
        self.practice_feedback.clear()
        if not question:
            self.practice_source.setText("知识库中还没有练习题")
            self.practice_prompt.setText("导入一个 Markdown 或代码文件开始练习。")
            self.submit_answer_button.setEnabled(False)
            return
        self.practice_source.setText(f"来源：{question['source_name']}  ·  {question.get('topic', '')}")
        self.practice_prompt.setText(question["prompt"])
        for index, text in enumerate(question["options"]):
            self.practice_options[index].setText(f"{chr(65 + index)}. {text}")
        self.submit_answer_button.setEnabled(True)

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

    def answer_from_pet(self, question_id: int, selected_index: int):
        result = self.service.database.answer_question(question_id, selected_index)
        self.pet.set_answer_feedback(result["correct"], result["question"]["explanation"])

    def load_settings(self):
        config = self.service.provider_config()
        index = self.provider_combo.findText(config.name)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)
        self.model_input.setText(config.model)
        self.base_url_input.setText(config.base_url)

    def save_settings(self):
        self.service.set_provider(
            self.provider_combo.currentText(), self.model_input.text().strip(), self.base_url_input.text().strip()
        )
        self.statusBar().showMessage("模型设置已保存", 5000)
        self.pet.show_message("新的大脑设置已经记住了。")

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


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ["BONGO_DISABLE_GLOBAL_INPUT"] = "1"
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Bongo Study")
    app.setOrganizationName("Bongo Study")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(APP_STYLE)
    service = LearningService(args.data_dir)
    pet = PetWindow(service.database.next_question)
    window = MainWindow(service, pet, start_hidden=args.smoke_test)
    if not args.no_pet and not args.smoke_test:
        screen = app.primaryScreen().availableGeometry()
        pet.move(screen.right() - pet.width() - 24, screen.bottom() - pet.height() - 20)
        pet.show()
        pet.start_input_monitor()
    if args.smoke_test:
        QTimer.singleShot(800, app.quit)
    try:
        return app.exec()
    finally:
        pet.stop_input_monitor()
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
