from __future__ import annotations

import json
from pathlib import Path

from .database import StudyDatabase


def export_learning_skill(database: StudyDatabase, output_dir: str | Path) -> Path:
    target = Path(output_dir).resolve()
    references = target / "references"
    references.mkdir(parents=True, exist_ok=True)
    sources = database.list_sources()
    conversations = database.list_conversations(limit=20)
    conversation_history = []
    for conversation in conversations:
        conversation_history.append(
            {**conversation, "messages": database.get_messages(conversation["id"], limit=100)}
        )
    questions = []
    for source in sources:
        rows = database.conn.execute(
            "SELECT * FROM questions WHERE source_id = ? ORDER BY id", (source["id"],)
        ).fetchall()
        for row in rows:
            value = dict(row)
            value["options"] = json.loads(value.pop("options_json"))
            questions.append(value)

    skill = """---
name: bongo-learning-profile
description: Use this local learning profile to review imported knowledge and practice questions.
---

# Bongo Learning Profile

Read `references/knowledge.md` for imported sources and `references/questions.md` for practice material.
Use the evidence attached to each question and do not invent missing source content.
"""
    (target / "SKILL.md").write_text(skill, encoding="utf-8")
    knowledge_lines = ["# Knowledge Sources", ""]
    for source in sources:
        knowledge_lines.extend(
            [
                f"## {source['name']}",
                f"- Original path: {source['path']}",
                f"- Status: {source['status']}",
                "",
                source["content"],
                "",
            ]
        )
    (references / "knowledge.md").write_text("\n".join(knowledge_lines), encoding="utf-8")
    question_lines = ["# Practice Questions", ""]
    for question in questions:
        question_lines.append(f"## {question['prompt']}")
        for index, option in enumerate(question["options"]):
            marker = " (correct)" if index == question["correct_index"] else ""
            question_lines.append(f"- {option}{marker}")
        question_lines.extend(
            [f"- Explanation: {question['explanation']}", f"- Evidence: {question['evidence']}", ""]
        )
    (references / "questions.md").write_text("\n".join(question_lines), encoding="utf-8")
    (references / "conversations.json").write_text(
        json.dumps(conversation_history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target
