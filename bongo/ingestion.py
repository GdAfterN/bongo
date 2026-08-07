from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .database import StudyDatabase
from .providers import ConversationProvider, ProviderError


SUPPORTED_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt",
    ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift",
    ".html", ".css", ".scss", ".sql", ".sh", ".ps1", ".yaml", ".yml", ".json",
}


class GeneratedQuestion(BaseModel):
    question: str = Field(min_length=6, max_length=500)
    options: list[str]
    correct_index: int = Field(ge=0, le=3)
    explanation: str = Field(min_length=2, max_length=1000)
    evidence: str = Field(min_length=1, max_length=1000)
    topic: str = Field(min_length=1, max_length=100)


class QuestionSet(BaseModel):
    questions: list[GeneratedQuestion] = Field(min_length=1, max_length=8)


QUESTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "minItems": 4,
                        "maxItems": 4,
                        "items": {"type": "string"},
                    },
                    "correct_index": {"type": "integer", "minimum": 0, "maximum": 3},
                    "explanation": {"type": "string"},
                    "evidence": {"type": "string"},
                    "topic": {"type": "string"},
                },
                "required": [
                    "question", "options", "correct_index", "explanation", "evidence", "topic"
                ],
            },
        }
    },
    "required": ["questions"],
}


def read_knowledge_file(path: str | Path) -> str:
    file_path = Path(path)
    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"暂不支持 {file_path.suffix or '无扩展名'} 文件")
    if file_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("单个知识文件不能超过 2 MB")
    raw = file_path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("无法识别文件编码")
    text = text.replace("\x00", "").strip()
    if len(text) < 20:
        raise ValueError("文件内容太少，无法生成有效练习")
    return text


def split_knowledge(content: str, suffix: str, max_chars: int = 3200) -> list[dict]:
    if suffix in {".md", ".markdown", ".rst"}:
        boundaries = re.split(r"(?m)(?=^#{1,4}\s+|^[^\n]+\n[=-]{3,}\s*$)", content)
    elif suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs"}:
        boundaries = re.split(
            r"(?m)(?=^(?:async\s+def|def|class|function|export\s+(?:default\s+)?(?:class|function)|"
            r"(?:public|private|protected|static|final|async)\s+.*(?:class|\w+\s*\())\b)",
            content,
        )
    else:
        boundaries = re.split(r"\n\s*\n", content)

    chunks: list[dict] = []
    buffer = ""
    heading = ""
    for part in boundaries:
        part = part.strip()
        if not part:
            continue
        first_line = part.splitlines()[0].strip()
        if first_line.startswith("#") or len(first_line) < 100:
            next_heading = first_line.lstrip("# ")[:120]
        else:
            next_heading = heading
        if buffer and len(buffer) + len(part) + 2 > max_chars:
            chunks.append({"heading": heading, "content": buffer.strip()})
            buffer = ""
        if len(part) > max_chars:
            if buffer:
                chunks.append({"heading": heading, "content": buffer.strip()})
                buffer = ""
            for start in range(0, len(part), max_chars):
                piece = part[start : start + max_chars].strip()
                if piece:
                    chunks.append({"heading": next_heading, "content": piece})
            continue
        heading = next_heading or heading
        buffer = f"{buffer}\n\n{part}".strip()
    if buffer:
        chunks.append({"heading": heading, "content": buffer.strip()})
    return chunks[:80]


class KnowledgeIngestor:
    def __init__(self, database: StudyDatabase, provider: ConversationProvider):
        self.database = database
        self.provider = provider

    def ingest(self, path: str | Path) -> dict:
        file_path = Path(path)
        content = read_knowledge_file(file_path)
        source_id, created = self.database.add_source(file_path, content)
        if not created:
            existing = next(item for item in self.database.list_sources() if item["id"] == source_id)
            if existing["status"] == "ready":
                return {"source_id": source_id, "created": False, "questions": existing["question_count"]}

        try:
            self.database.set_source_status(source_id, "processing")
            chunks = split_knowledge(content, file_path.suffix.lower())
            chunk_ids = self.database.replace_chunks(source_id, chunks)
            questions = self._generate_questions(file_path.name, chunks, chunk_ids)
            question_ids = self.database.add_questions(source_id, questions)
            self.database.set_source_status(source_id, "ready")
            return {
                "source_id": source_id,
                "created": created,
                "reprocessed": not created,
                "questions": len(question_ids),
            }
        except Exception as exc:
            self.database.set_source_status(source_id, "failed", str(exc)[:800])
            raise

    def _generate_questions(self, source_name: str, chunks: list[dict], chunk_ids: list[int]) -> list[dict]:
        selected = chunks[:6]
        material_parts = []
        for index, chunk in enumerate(selected):
            material_parts.append(
                f"[片段 {index + 1} | {chunk.get('heading') or '未命名'}]\n{chunk['content']}"
            )
        material = "\n\n".join(material_parts)
        system = (
            "你是严谨的助学题目设计器。只能依据用户提供的材料出题。"
            "生成 3 到 5 道单选题，每题恰好四个选项且只有一个正确答案。"
            "避免文字游戏，错误选项应当合理但能被材料排除。evidence 必须引用或紧贴原文，"
            "explanation 要说明正确原因。所有内容使用与材料一致的主要语言。"
        )
        try:
            raw = self.provider.complete(
                [{"role": "user", "content": f"资料名：{source_name}\n\n{material}"}],
                system,
                QUESTION_SCHEMA,
            )
            parsed = QuestionSet.model_validate(raw)
        except (ValidationError, ProviderError, TypeError, ValueError) as exc:
            raise ProviderError(f"选择题生成失败：{exc}") from exc

        result = []
        for position, question in enumerate(parsed.questions):
            if len(question.options) != 4 or len(set(question.options)) != 4:
                continue
            value = question.model_dump()
            value["chunk_id"] = chunk_ids[min(position, len(chunk_ids) - 1)] if chunk_ids else None
            result.append(value)
        if not result:
            raise ProviderError("模型没有生成可用的四选一题目")
        return result
