from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
)

from .database import StudyDatabase


class SkillEditorDialog(QDialog):
    def __init__(self, database: StudyDatabase, skill: dict | None = None, parent=None):
        super().__init__(parent)
        self.database = database
        self.skill = skill or {}
        self.setWindowTitle("编辑 Learning Skill" if skill else "创建 Learning Skill")
        self.resize(620, 620)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "选择要沉淀的知识来源。原始知识始终包含，题库、错题、对话洞察和成长画像可以独立控制。"
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        layout.addWidget(intro)

        form = QFormLayout()
        self.title_input = QLineEdit(str(self.skill.get("title", "")))
        self.title_input.setPlaceholderText("例如：Hot 100 算法复习")
        self.name_input = QLineEdit(str(self.skill.get("name", "")))
        self.name_input.setPlaceholderText("例如：review-hot100")
        self.description_input = QLineEdit(str(self.skill.get("description", "")))
        self.description_input.setPlaceholderText("说明这个 Skill 能做什么，以及何时使用")
        form.addRow("显示名称", self.title_input)
        form.addRow("Skill 标识", self.name_input)
        form.addRow("用途描述", self.description_input)
        layout.addLayout(form)

        layout.addWidget(QLabel("知识来源"))
        self.source_list = QListWidget()
        selected = {int(value) for value in self.skill.get("source_ids", [])}
        for source in database.list_sources():
            if source["status"] != "ready":
                continue
            label = source.get("problem_title") or source["name"]
            item = QListWidgetItem(f"{label}  ·  {source['knowledge_type']}")
            item.setData(Qt.ItemDataRole.UserRole, int(source["id"]))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if int(source["id"]) in selected else Qt.CheckState.Unchecked
            )
            self.source_list.addItem(item)
        layout.addWidget(self.source_list, 1)

        self.include_questions = QCheckBox("包含完整题库与解析")
        self.include_mistakes = QCheckBox("包含历史错题与纠正状态")
        self.include_conversations = QCheckBox("包含对话结论与未解决问题")
        self.include_growth = QCheckBox("包含学习画像、复习计划与成长成果")
        options = (
            (self.include_questions, "include_questions"),
            (self.include_mistakes, "include_mistakes"),
            (self.include_conversations, "include_conversations"),
            (self.include_growth, "include_growth"),
        )
        for checkbox, key in options:
            checkbox.setChecked(bool(self.skill.get(key, 1)))
            layout.addWidget(checkbox)

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color:#b94a48;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        if not self.title_input.text().strip():
            self.error_label.setText("请填写显示名称。")
            return
        if not self.name_input.text().strip():
            self.error_label.setText("请填写由小写字母、数字和连字符组成的 Skill 标识。")
            return
        if not self.description_input.text().strip():
            self.error_label.setText("请填写 Skill 的用途描述。")
            return
        if not self.selected_source_ids():
            self.error_label.setText("至少选择一个知识来源。")
            return
        self.accept()

    def selected_source_ids(self) -> list[int]:
        return [
            int(item.data(Qt.ItemDataRole.UserRole))
            for row in range(self.source_list.count())
            if (item := self.source_list.item(row)).checkState() == Qt.CheckState.Checked
        ]

    def values(self) -> dict:
        return {
            "name": self.name_input.text().strip(),
            "title": self.title_input.text().strip(),
            "description": self.description_input.text().strip(),
            "source_ids": self.selected_source_ids(),
            "include_questions": self.include_questions.isChecked(),
            "include_mistakes": self.include_mistakes.isChecked(),
            "include_conversations": self.include_conversations.isChecked(),
            "include_growth": self.include_growth.isChecked(),
        }


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
