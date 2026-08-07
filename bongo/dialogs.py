from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
)

from .database import StudyDatabase


class QuestionBankDialog(QDialog):
    def __init__(self, database: StudyDatabase, source_id: int, parent=None):
        super().__init__(parent)
        self.database = database
        self.source_id = source_id
        self.questions: list[dict] = []
        source = database.get_source(source_id) or {}
        display_name = source.get("problem_title") or source.get("name", "")
        self.setWindowTitle(f"题库 · {display_name}")
        self.resize(960, 760 if source.get("knowledge_type") == "code" else 620)

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel(display_name or "题库"))
        bar.addStretch()
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("全部题目", "all")
        self.filter_combo.addItem("错题", "wrong")
        self.filter_combo.addItem("未回答", "unanswered")
        self.filter_combo.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.filter_combo)
        layout.addLayout(bar)

        if source.get("knowledge_type") == "code":
            statement = html.escape(source.get("problem_statement") or "尚未提取题干").replace("\n", "<br>")
            approach = html.escape(source.get("solution_approach") or "尚未提取解题思路").replace("\n", "<br>")
            summary = QTextBrowser()
            summary.setMaximumHeight(220)
            summary.setHtml(
                f"<h3>{html.escape(display_name)}</h3>"
                f"<p><b>题干</b><br>{statement}</p>"
                f"<p><b>解题思路</b><br>{approach}</p>"
            )
            layout.addWidget(summary)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["题目", "主题", "作答", "正确率"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.show_selected)
        layout.addWidget(self.table, 1)

        self.detail = QTextBrowser()
        self.detail.setMaximumHeight(230)
        layout.addWidget(self.detail)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.refresh()

    def refresh(self, *_args) -> None:
        mode = str(self.filter_combo.currentData())
        self.questions = self.database.list_questions(
            self.source_id,
            wrong_only=mode == "wrong",
            unanswered_only=mode == "unanswered",
        )
        self.table.setRowCount(len(self.questions))
        for row, question in enumerate(self.questions):
            asked = int(question["ask_count"])
            correct = int(question["correct_count"])
            accuracy = f"{correct / asked:.0%}" if asked else "未作答"
            values = [question["prompt"], question.get("topic", ""), str(asked), accuracy]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row, column, item)
        if self.questions:
            self.table.selectRow(0)
        else:
            self.detail.setHtml("<p style='color:#687078'>当前筛选下没有题目。</p>")

    def show_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.questions):
            return
        question = self.questions[row]
        options = []
        for index, option in enumerate(question["options"]):
            marker = " <b>（正确答案）</b>" if index == question["correct_index"] else ""
            options.append(f"<li>{chr(65 + index)}. {html.escape(option)}{marker}</li>")
        self.detail.setHtml(
            f"<h3>{html.escape(question['prompt'])}</h3><ol>{''.join(options)}</ol>"
            f"<p><b>解析：</b>{html.escape(question['explanation']).replace(chr(10), '<br>')}</p>"
            f"<p><b>依据：</b>{html.escape(question['evidence']).replace(chr(10), '<br>')}</p>"
        )
