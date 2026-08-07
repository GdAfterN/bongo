from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .database import StudyDatabase


def _safe_name(source_id: int, name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "source"
    return f"{source_id}-{stem[:80]}.md"


def export_learning_skill(database: StudyDatabase, output_dir: str | Path) -> Path:
    target = Path(output_dir).resolve()
    references = target / "references"
    knowledge_dir = references / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    sources = database.list_sources()
    questions = database.list_questions()
    wrong_questions = database.list_wrong_questions()
    attempts = database.list_attempts()
    conversations = database.list_conversations(limit=50)
    conversation_history = [
        {**conversation, "messages": database.get_messages(conversation["id"], limit=100)}
        for conversation in conversations
    ]

    skill = """---
name: review-bongo-study
description: Review and quiz the learner on their Bongo Study documents, explain imported material with source evidence, resume document-specific learning conversations, and remediate recorded wrong answers. Use when the learner asks to study, review, practice, explain, or assess knowledge exported from Bongo Study.
---

# Review Bongo Study

1. Read `references/source-index.md` to locate the requested document.
2. Read only the relevant file under `references/knowledge/` unless the request spans documents.
3. For practice, load `references/questions.json`. Ask one question at a time and do not reveal `correct_index` before the learner answers.
4. For remediation, read `references/mistakes.md`, ask the learner to explain their reasoning, then contrast it with the stored evidence and explanation.
5. For progress guidance, read `references/learning-profile.md` and prioritize low-accuracy topics with enough attempts.
6. Resume prior work from `references/conversations.json` only when the learner refers to an earlier discussion.

Ground explanations in the exported material. Cite the source filename and section. Clearly label any general knowledge added beyond the source.
"""
    (target / "SKILL.md").write_text(skill, encoding="utf-8")

    source_index = ["# Source Index", ""]
    question_count = Counter(question["source_id"] for question in questions)
    wrong_count = Counter(question["source_id"] for question in wrong_questions)
    for source in sources:
        filename = _safe_name(source["id"], source["name"])
        source_index.extend(
            [
                f"## {source['name']}",
                f"- Knowledge file: `knowledge/{filename}`",
                f"- Imported: {source['created_at']}",
                f"- Questions: {question_count[source['id']]}",
                f"- Current weak questions: {wrong_count[source['id']]}",
                "",
            ]
        )
        content = (
            f"# {source['name']}\n\n"
            f"- Imported: {source['created_at']}\n"
            f"- Type: {source['kind'] or 'text'}\n\n"
            f"{source['content']}\n"
        )
        (knowledge_dir / filename).write_text(content, encoding="utf-8")
    (references / "source-index.md").write_text("\n".join(source_index), encoding="utf-8")

    question_payload = []
    for question in questions:
        question_payload.append(
            {
                "id": question["id"],
                "source": question["source_name"],
                "topic": question["topic"],
                "question": question["prompt"],
                "options": question["options"],
                "correct_index": question["correct_index"],
                "explanation": question["explanation"],
                "evidence": question["evidence"],
                "attempts": question["ask_count"],
                "correct_attempts": question["correct_count"],
            }
        )
    (references / "questions.json").write_text(
        json.dumps(question_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    attempts_by_question = Counter(attempt["question_id"] for attempt in attempts if not attempt["is_correct"])
    mistake_lines = ["# Mistake Review", ""]
    if not wrong_questions:
        mistake_lines.append("No wrong answers have been recorded.")
    for question in wrong_questions:
        recorded_wrong = attempts_by_question[question["id"]]
        historical_wrong = int(question["ask_count"]) - int(question["correct_count"])
        mistake_lines.extend(
            [
                f"## {question['prompt']}",
                f"- Source: {question['source_name']}",
                f"- Topic: {question['topic'] or 'Uncategorized'}",
                f"- Wrong attempts: {max(recorded_wrong, historical_wrong)}",
                f"- Correct answer: {chr(65 + question['correct_index'])}. {question['options'][question['correct_index']]}",
                f"- Explanation: {question['explanation']}",
                f"- Evidence: {question['evidence']}",
                "- Remediation: Ask for the learner's original reasoning before explaining the distinction.",
                "",
            ]
        )
    (references / "mistakes.md").write_text("\n".join(mistake_lines), encoding="utf-8")

    topic_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for question in questions:
        topic = question["topic"] or "Uncategorized"
        topic_stats[topic][0] += int(question["ask_count"])
        topic_stats[topic][1] += int(question["correct_count"])
    profile_lines = ["# Learning Profile", ""]
    total_attempts = sum(value[0] for value in topic_stats.values())
    total_correct = sum(value[1] for value in topic_stats.values())
    accuracy = total_correct / total_attempts if total_attempts else 0
    profile_lines.extend(
        [
            f"- Total questions: {len(questions)}",
            f"- Total attempts: {total_attempts}",
            f"- Accuracy: {accuracy:.0%}" if total_attempts else "- Accuracy: Not enough data",
            "",
            "## Topic Priority",
            "",
        ]
    )
    for topic, (asked, correct) in sorted(
        topic_stats.items(), key=lambda item: (item[1][1] / max(item[1][0], 1), -item[1][0], item[0])
    ):
        result = f"{correct / asked:.0%}" if asked else "not practiced"
        profile_lines.append(f"- {topic}: {correct}/{asked} correct ({result})")
    (references / "learning-profile.md").write_text("\n".join(profile_lines), encoding="utf-8")
    (references / "conversations.json").write_text(
        json.dumps(conversation_history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target
