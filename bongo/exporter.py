from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .database import StudyDatabase


SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _safe_name(source_id: int, name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "source"
    return f"{source_id}-{stem[:80]}.md"


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _selected_data(database: StudyDatabase, source_ids: list[int]) -> tuple[list[dict], list[dict], list[dict]]:
    selected = set(source_ids)
    sources = [source for source in database.list_sources() if source["id"] in selected]
    questions = [
        question
        for source_id in source_ids
        for question in database.list_questions(source_id=source_id)
    ]
    attempts = [attempt for attempt in database.list_attempts() if attempt["source_id"] in selected]
    return sources, questions, attempts


def _question_states(questions: list[dict], attempts: list[dict]) -> dict[int, dict]:
    by_question: dict[int, list[dict]] = defaultdict(list)
    for attempt in attempts:
        by_question[int(attempt["question_id"])].append(attempt)
    result = {}
    for question in questions:
        history = by_question[int(question["id"])]
        wrong_count = sum(not bool(item["is_correct"]) for item in history)
        if not history:
            state = "未练习"
        elif not bool(history[-1]["is_correct"]):
            state = "薄弱"
        elif wrong_count:
            state = "已纠正"
        elif len(history) >= 2:
            state = "已掌握"
        else:
            state = "学习中"
        result[int(question["id"])] = {
            "state": state,
            "wrong_count": wrong_count,
            "last_correct": bool(history[-1]["is_correct"]) if history else None,
        }
    return result


def _conversation_insights(database: StudyDatabase, source_ids: list[int]) -> list[dict]:
    persisted = database.list_conversation_insights(source_ids)
    by_user_message = {int(item["user_message_id"]): item for item in persisted}
    selected = set(source_ids)
    for conversation in database.list_conversations(limit=1000):
        if conversation.get("source_id") not in selected:
            continue
        messages = database.get_messages(int(conversation["id"]), limit=1000)
        for index, message in enumerate(messages):
            if message["role"] != "user" or int(message["id"]) in by_user_message:
                continue
            answer = messages[index + 1] if index + 1 < len(messages) and messages[index + 1]["role"] == "assistant" else None
            item = {
                "conversation_id": conversation["id"],
                "conversation_title": conversation["title"],
                "conversation_summary": conversation.get("summary", ""),
                "source_id": conversation["source_id"],
                "source_name": conversation.get("source_name") or "未知来源",
                "user_message_id": message["id"],
                "question": message["content"],
                "conclusion": answer["content"] if answer else "",
                "citations": answer.get("citations", []) if answer else [],
                "resolved": int(answer is not None),
                "created_at": message["created_at"],
            }
            by_user_message[int(message["id"])] = item
    return sorted(by_user_message.values(), key=lambda item: int(item["user_message_id"]))


def _growth_summary(
    database: StudyDatabase,
    source_ids: list[int],
    sources: list[dict],
    questions: list[dict],
    attempts: list[dict],
    insights: list[dict],
    states: dict[int, dict],
) -> dict:
    events = database.list_learning_events(source_ids)
    correct_attempts = sum(bool(item["is_correct"]) for item in attempts)
    recovered_ids = {
        int(event["question_id"])
        for event in events
        if event["event_type"] == "mistake_recovered" and event.get("question_id") is not None
    }
    if not recovered_ids:
        by_question: dict[int, list[dict]] = defaultdict(list)
        for attempt in attempts:
            by_question[int(attempt["question_id"])].append(attempt)
        recovered_ids = {
            question_id
            for question_id, history in by_question.items()
            if any(not bool(item["is_correct"]) for item in history)
            and bool(history[-1]["is_correct"])
        }
    resolved_insights = sum(bool(item["resolved"]) for item in insights)
    mastered = sum(item["state"] in {"已掌握", "已纠正"} for item in states.values())
    activity_days = sorted(
        {
            datetime.fromisoformat(item["created_at"]).date()
            for item in events
            if item.get("created_at")
        },
        reverse=True,
    )
    streak = 0
    cursor = date.today()
    for active_day in activity_days:
        if active_day == cursor:
            streak += 1
            cursor = date.fromordinal(cursor.toordinal() - 1)
        elif active_day < cursor:
            break
    growth_score = len(sources) * 20 + correct_attempts * 5 + len(recovered_ids) * 12 + min(resolved_insights, 20) * 3
    return {
        "growth_score": growth_score,
        "learned_sources": len(sources),
        "questions_total": len(questions),
        "questions_attempted": len({int(item["question_id"]) for item in attempts}),
        "correct_attempts": correct_attempts,
        "mastered_questions": mastered,
        "recovered_mistakes": len(recovered_ids),
        "conversation_conclusions": resolved_insights,
        "review_streak_days": streak,
        "events": events,
    }


def _write_bundle(database: StudyDatabase, skill: dict, root: Path, version: int) -> dict:
    references = root / "references"
    knowledge_dir = references / "knowledge"
    agents_dir = root / "agents"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    source_ids = [int(value) for value in skill["source_ids"]]
    sources, questions, attempts = _selected_data(database, source_ids)
    if len(sources) != len(set(source_ids)):
        raise ValueError("Skill 中包含已经删除的知识来源，请重新编辑后再导出")
    states = _question_states(questions, attempts)
    insights = _conversation_insights(database, source_ids) if skill["include_conversations"] else []
    growth = _growth_summary(database, source_ids, sources, questions, attempts, insights, states)

    skill_md = f"""---
name: {skill['name']}
description: {_yaml_string(skill['description'].replace(chr(10), ' '))}
---

# {skill['title']}

1. Read `references/source-index.md` and choose only the sources relevant to the learner's request.
2. Read the matching files under `references/knowledge/` before explaining source-specific material.
3. For practice, read `references/questions.json`, ask one question at a time, and hide `correct_index` until the learner answers.
4. For remediation, read `references/mistakes.md` and address unresolved misconceptions before general review.
5. Read `references/conversation-insights.md` when resuming prior reasoning or unresolved questions.
6. Use `references/learning-profile.md` and `references/review-plan.md` to prioritize weak knowledge.
7. Read `references/growth-profile.md` only when reporting progress or choosing an encouraging next step.

Ground answers in the selected source material and cite the source filename. Clearly label knowledge added beyond the exported sources.
"""
    (root / "SKILL.md").write_text(skill_md, encoding="utf-8")
    default_prompt = f"Use ${skill['name']} to review my weakest knowledge and quiz me one question at a time."
    (agents_dir / "openai.yaml").write_text(
        "interface:\n"
        f"  display_name: {_yaml_string(skill['title'])}\n"
        f"  short_description: {_yaml_string(skill['description'][:64])}\n"
        f"  default_prompt: {_yaml_string(default_prompt)}\n",
        encoding="utf-8",
    )

    question_count = Counter(int(question["source_id"]) for question in questions)
    wrong_count = Counter(
        int(question["source_id"])
        for question in questions
        if states[int(question["id"])]["wrong_count"]
    )
    source_index = ["# Source Index", ""]
    source_files = {}
    for source in sorted(sources, key=lambda item: int(item["id"])):
        filename = _safe_name(int(source["id"]), source["name"])
        source_files[int(source["id"])] = filename
        source_index.extend(
            [
                f"## {source.get('problem_title') or source['name']}",
                f"- Knowledge file: `knowledge/{filename}`",
                f"- Original filename: `{source['name']}`",
                f"- Knowledge type: {source['knowledge_type']}",
                f"- Imported: {source['created_at']}",
                f"- Questions: {question_count[int(source['id'])]}",
                f"- Historical mistakes: {wrong_count[int(source['id'])]}",
                "",
            ]
        )
        algorithm_metadata = ""
        if source["knowledge_type"] == "code":
            algorithm_metadata = (
                f"## Problem summary\n\n{source['problem_statement']}\n\n"
                f"## Solution approach\n\n{source['solution_approach']}\n\n"
            )
        content = (
            f"# {source.get('problem_title') or source['name']}\n\n"
            f"- Original filename: `{source['name']}`\n"
            f"- Knowledge type: {source['knowledge_type']}\n"
            f"- File type: {source['kind'] or 'text'}\n\n"
            f"{algorithm_metadata}"
            f"## Original material\n\n{source['content']}\n"
        )
        (knowledge_dir / filename).write_text(content, encoding="utf-8")
    (references / "source-index.md").write_text("\n".join(source_index), encoding="utf-8")

    topics_by_source: dict[int, set[str]] = defaultdict(set)
    question_payload = []
    if skill["include_questions"]:
        for question in questions:
            topic = question["topic"] or "未分类"
            topics_by_source[int(question["source_id"])].add(topic)
            state = states[int(question["id"])]
            question_payload.append(
                {
                    "id": question["id"],
                    "source_id": question["source_id"],
                    "source": question["source_name"],
                    "topic": topic,
                    "question": question["prompt"],
                    "options": question["options"],
                    "correct_index": question["correct_index"],
                    "explanation": question["explanation"],
                    "evidence": question["evidence"],
                    "attempts": question["ask_count"],
                    "correct_attempts": question["correct_count"],
                    "learning_state": state["state"],
                }
            )
    (references / "questions.json").write_text(
        json.dumps(question_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    knowledge_map = {
        "nodes": [
            {"id": f"source:{source['id']}", "type": "source", "label": source.get("problem_title") or source["name"]}
            for source in sources
        ] + [
            {"id": f"topic:{source_id}:{topic}", "type": "topic", "label": topic}
            for source_id, topics in topics_by_source.items()
            for topic in sorted(topics)
        ],
        "relations": [
            {"from": f"source:{source_id}", "to": f"topic:{source_id}:{topic}", "type": "contains"}
            for source_id, topics in topics_by_source.items()
            for topic in sorted(topics)
        ],
    }
    (references / "knowledge-map.json").write_text(
        json.dumps(knowledge_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    mistake_lines = ["# Misconceptions and Corrections", ""]
    historical_mistakes = [
        question for question in questions if states[int(question["id"])]["wrong_count"]
    ] if skill["include_mistakes"] else []
    if not historical_mistakes:
        mistake_lines.append("No mistakes have been recorded for the selected sources.")
    for question in historical_mistakes:
        state = states[int(question["id"])]
        mistake_lines.extend(
            [
                f"## {question['prompt']}",
                f"- Source: {question['source_name']}",
                f"- Topic: {question['topic'] or '未分类'}",
                f"- Learning state: {state['state']}",
                f"- Wrong attempts: {state['wrong_count']}",
                f"- Correct answer: {chr(65 + question['correct_index'])}. {question['options'][question['correct_index']]}",
                f"- Correction: {question['explanation']}",
                f"- Evidence: {question['evidence']}",
                "",
            ]
        )
    (references / "mistakes.md").write_text("\n".join(mistake_lines), encoding="utf-8")

    insight_lines = ["# Conversation Insights", ""]
    if not insights:
        insight_lines.append("No conversation insights have been recorded for the selected sources.")
    for item in insights:
        status = "已形成结论" if item["resolved"] else "尚未解决"
        citations = ", ".join(citation.get("source", "") for citation in item.get("citations", [])) or item["source_name"]
        insight_lines.extend(
            [
                f"## {item['question']}",
                f"- Source: {item['source_name']}",
                f"- Status: {status}",
                f"- Evidence references: {citations}",
                f"- Conversation summary: {item['conversation_summary']}" if item.get("conversation_summary") else "",
                f"- Conclusion: {item['conclusion'] or '需要在后续学习中继续解决。'}",
                "",
            ]
        )
    (references / "conversation-insights.md").write_text("\n".join(insight_lines), encoding="utf-8")

    weak_questions = [question for question in questions if states[int(question["id"])]["state"] == "薄弱"]
    unpracticed = [question for question in questions if states[int(question["id"])]["state"] == "未练习"]
    unresolved = [item for item in insights if not item["resolved"]]
    if skill["include_growth"]:
        topic_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for question in questions:
            topic = question["topic"] or "未分类"
            topic_stats[topic][0] += int(question["ask_count"])
            topic_stats[topic][1] += int(question["correct_count"])
            topic_stats[topic][2] += states[int(question["id"])]["state"] == "薄弱"
        profile_lines = ["# Learning Profile", ""]
        total_attempts = len(attempts)
        total_correct = sum(bool(item["is_correct"]) for item in attempts)
        profile_lines.extend(
            [
                f"- Selected sources: {len(sources)}",
                f"- Total questions: {len(questions)}",
                f"- Total attempts: {total_attempts}",
                f"- Accuracy: {total_correct / total_attempts:.0%}" if total_attempts else "- Accuracy: Not enough data",
                "",
                "## Topic Mastery",
                "",
            ]
        )
        for topic, (asked, correct, weak) in sorted(
            topic_stats.items(), key=lambda item: (-item[1][2], item[1][1] / max(item[1][0], 1), item[0])
        ):
            accuracy = f"{correct / asked:.0%}" if asked else "未练习"
            profile_lines.append(f"- {topic}: {correct}/{asked} correct ({accuracy}), unresolved weak questions: {weak}")
        review_lines = ["# Review Plan", "", "Review in this order:", ""]
        review_lines.append(f"1. Resolve {len(weak_questions)} currently weak questions from `mistakes.md`.")
        review_lines.append(f"2. Continue {len(unresolved)} unresolved conversation questions from `conversation-insights.md`.")
        review_lines.append(f"3. Practice {len(unpracticed)} unattempted questions from `questions.json`.")
        review_lines.append("4. Recheck corrected mistakes before reviewing already-mastered topics.")
        growth_lines = [
            "# Growth Profile",
            "",
            "The growth score reflects learning activity, not keyboard or mouse activity.",
            "",
            f"- Growth score: {growth['growth_score']}",
            f"- Knowledge sources learned: {growth['learned_sources']}",
            f"- Questions attempted: {growth['questions_attempted']}/{growth['questions_total']}",
            f"- Correct attempts: {growth['correct_attempts']}",
            f"- Mastered or corrected questions: {growth['mastered_questions']}",
            f"- Recovered mistakes: {growth['recovered_mistakes']}",
            f"- Grounded conversation conclusions: {growth['conversation_conclusions']}",
            f"- Current review streak: {growth['review_streak_days']} day(s)",
            "",
            "Score rule: 20 per source, 5 per correct attempt, 12 per recovered mistake, and 3 per grounded conversation conclusion (conversation contribution capped at 20).",
        ]
    else:
        profile_lines = ["# Learning Profile", "", "Learning profile was excluded when this Skill was created."]
        review_lines = ["# Review Plan", "", "Review plan was excluded when this Skill was created."]
        growth_lines = ["# Growth Profile", "", "Growth profile was excluded when this Skill was created."]
    (references / "learning-profile.md").write_text("\n".join(profile_lines), encoding="utf-8")
    (references / "review-plan.md").write_text("\n".join(review_lines), encoding="utf-8")
    (references / "growth-profile.md").write_text("\n".join(growth_lines), encoding="utf-8")

    manifest = {
        "generator": "Bongo Study",
        "format_version": 2,
        "skill_id": skill.get("id"),
        "name": skill["name"],
        "title": skill["title"],
        "version": version,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [
            {
                "id": source["id"],
                "name": source["name"],
                "content_hash": source["content_hash"],
                "knowledge_file": f"references/knowledge/{source_files[int(source['id'])]}",
            }
            for source in sorted(sources, key=lambda item: int(item["id"]))
        ],
        "include": {
            "questions": bool(skill["include_questions"]),
            "mistakes": bool(skill["include_mistakes"]),
            "conversations": bool(skill["include_conversations"]),
            "growth": bool(skill["include_growth"]),
        },
        "counts": {
            "sources": len(sources),
            "questions": len(question_payload),
            "historical_mistakes": len(historical_mistakes),
            "conversation_insights": len(insights),
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    validate_learning_skill(root, database.get_setting("api_key", ""))
    return {"manifest": manifest, "growth": growth, "states": states}


def validate_learning_skill(path: str | Path, secret: str = "") -> None:
    root = Path(path)
    required = [
        "SKILL.md", "agents/openai.yaml", "manifest.json", "references/source-index.md",
        "references/questions.json", "references/knowledge-map.json", "references/mistakes.md",
        "references/conversation-insights.md", "references/learning-profile.md",
        "references/review-plan.md", "references/growth-profile.md",
    ]
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise ValueError(f"Skill 缺少必要文件：{', '.join(missing)}")
    skill_text = (root / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = re.match(r"^---\nname: ([^\n]+)\ndescription: ([^\n]+)\n---\n", skill_text)
    if not frontmatter or not SKILL_NAME_PATTERN.fullmatch(frontmatter.group(1)):
        raise ValueError("SKILL.md frontmatter 无效")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    questions = json.loads((root / "references/questions.json").read_text(encoding="utf-8"))
    json.loads((root / "references/knowledge-map.json").read_text(encoding="utf-8"))
    source_ids = {int(source["id"]) for source in manifest["sources"]}
    if any(int(question["source_id"]) not in source_ids for question in questions):
        raise ValueError("题库包含未选择知识来源的数据")
    for source in manifest["sources"]:
        if not (root / source["knowledge_file"]).is_file():
            raise ValueError(f"知识文件不存在：{source['knowledge_file']}")
    if secret:
        for file in root.rglob("*"):
            if file.is_file() and secret in file.read_text(encoding="utf-8"):
                raise ValueError("Skill 中检测到模型密钥，已停止导出")


def export_saved_learning_skill(database: StudyDatabase, skill_id: int, output_dir: str | Path) -> Path:
    skill = database.get_learning_skill(skill_id)
    if not skill:
        raise ValueError("Skill 不存在")
    version = int(skill["version"]) + 1
    target = Path(output_dir).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{skill['name']}-", dir=target.parent))
    backup: Path | None = None
    try:
        _write_bundle(database, skill, temporary, version)
        if target.exists():
            manifest_path = target / "manifest.json"
            if not manifest_path.is_file():
                raise ValueError("目标目录已存在且不是 Bongo Study 导出的 Skill")
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("generator") != "Bongo Study" or existing.get("name") != skill["name"]:
                raise ValueError("目标目录属于另一个 Skill，请选择其他导出位置")
            backup = target.parent / f".{target.name}-backup-{uuid.uuid4().hex}"
            os.replace(target, backup)
        os.replace(temporary, target)
        if backup:
            shutil.rmtree(backup)
        database.mark_learning_skill_exported(skill_id, version)
        return target
    except Exception:
        if backup and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def learning_skill_preview(database: StudyDatabase, skill_id: int) -> dict:
    skill = database.get_learning_skill(skill_id)
    if not skill:
        raise ValueError("Skill 不存在")
    sources, questions, attempts = _selected_data(database, skill["source_ids"])
    states = _question_states(questions, attempts)
    insights = _conversation_insights(database, skill["source_ids"]) if skill["include_conversations"] else []
    growth = _growth_summary(database, skill["source_ids"], sources, questions, attempts, insights, states)
    return {
        "skill": skill,
        "sources": sources,
        "questions": questions,
        "states": states,
        "insights": insights,
        "growth": growth,
        "historical_mistakes": sum(item["wrong_count"] > 0 for item in states.values()),
        "weak_questions": sum(item["state"] == "薄弱" for item in states.values()),
    }


def export_learning_skill(database: StudyDatabase, output_dir: str | Path) -> Path:
    """Export all sources for callers that do not yet persist a Skill definition."""
    source_ids = [int(source["id"]) for source in database.list_sources()]
    if not source_ids:
        raise ValueError("没有可导出的知识来源")
    skill = {
        "id": None,
        "name": "review-bongo-study",
        "title": "Bongo Study Review",
        "description": "Review, explain, and practice knowledge exported from Bongo Study with source evidence, mistakes, conversation insights, and learning progress.",
        "source_ids": source_ids,
        "include_questions": 1,
        "include_mistakes": 1,
        "include_conversations": 1,
        "include_growth": 1,
    }
    target = Path(output_dir).resolve()
    if target.exists():
        shutil.rmtree(target)
    _write_bundle(database, skill, target, 1)
    return target
