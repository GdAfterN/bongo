from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

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


class GeneratedAlgorithmQuestion(GeneratedQuestion):
    explanation: str = Field(min_length=40, max_length=3000)
    focus: Literal["main_approach", "data_structure", "boundary"]


class AlgorithmStudySet(BaseModel):
    problem_title: str = Field(min_length=1, max_length=200)
    problem_statement: str = Field(min_length=10, max_length=5000)
    solution_approach: str = Field(min_length=20, max_length=5000)
    questions: list[GeneratedAlgorithmQuestion] = Field(min_length=3, max_length=3)


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


ALGORITHM_STUDY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "problem_title": {"type": "string"},
        "problem_statement": {"type": "string"},
        "solution_approach": {"type": "string"},
        "questions": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
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
                    "explanation": {"type": "string", "minLength": 40},
                    "evidence": {"type": "string"},
                    "topic": {"type": "string"},
                    "focus": {
                        "type": "string",
                        "enum": ["main_approach", "data_structure", "boundary"],
                    },
                },
                "required": [
                    "question", "options", "correct_index", "explanation", "evidence", "topic", "focus"
                ],
            },
        },
    },
    "required": ["problem_title", "problem_statement", "solution_approach", "questions"],
}


DOCUMENT_EXTENSIONS = {".md", ".markdown", ".txt", ".rst"}
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".swift",
}
WEB_STYLE_EXTENSIONS = {".html", ".css", ".scss"}
DATA_EXTENSIONS = {".json", ".yaml", ".yml"}
SCRIPT_EXTENSIONS = {".sh", ".ps1"}


def knowledge_profile(suffix: str) -> tuple[str, str]:
    suffix = suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS:
        return (
            "结构化文档",
            "按概念、论点、步骤、条件和因果关系拆解。题目应检验理解与应用，"
            "避免只考标题、措辞或孤立数字。",
        )
    if suffix in CODE_EXTENSIONS:
        return (
            "程序代码",
            "按模块职责、调用关系、数据流、分支条件、状态变化、异常与边界条件拆解。"
            "题目应要求阅读代码推断行为，不考无意义的语法记忆。",
        )
    if suffix == ".sql":
        return (
            "SQL",
            "按表结构、字段约束、连接关系、过滤聚合、事务和查询结果拆解。"
            "题目应检验数据关系和语句实际效果。",
        )
    if suffix in DATA_EXTENSIONS:
        return (
            "配置或结构化数据",
            "按层级、字段语义、取值约束、引用关系和配置影响拆解。"
            "题目应检验修改某字段会产生的结果。",
        )
    if suffix in WEB_STYLE_EXTENSIONS:
        return (
            "界面结构与样式",
            "按页面结构、组件职责、选择器作用、布局规则、层叠关系和交互状态拆解。"
            "题目应检验渲染或交互结果。",
        )
    if suffix in SCRIPT_EXTENSIONS:
        return (
            "自动化脚本",
            "按执行顺序、输入输出、环境依赖、失败条件和有副作用的操作拆解。"
            "题目应检验执行效果与安全边界。",
        )
    return (
        "通用文本",
        "按主题、事实、关系、步骤和约束拆解，题目应检验理解而非表面记忆。",
    )


def question_system_prompt(suffix: str) -> str:
    profile, guidance = knowledge_profile(suffix)
    return (
        "你是严谨的助学题目设计器。只能依据用户提供的材料出题。"
        f"当前材料类型是：{profile}。{guidance}"
        "生成 3 到 5 道单选题，每题恰好四个选项且只有一个正确答案。"
        "题目之间应覆盖不同知识点；错误选项应当合理但能被材料排除。"
        "evidence 必须引用或紧贴原文，explanation 要说明判断依据。"
        "所有内容使用与材料一致的主要语言。"
    )


def algorithm_study_system_prompt() -> str:
    return (
        "你是严谨的算法题助学设计器。输入是一道算法题的题干、题解或实现代码，"
        "只能依据材料整理，不得调用你记忆中的同名题目来补全材料。题名可依据文件名或类名归纳；"
        "题干、约束和示例只能记录材料明确给出的内容，代码行为可以标注为由实现归纳。材料没有"
        "提供的信息必须明确说明未提供，不得虚构常见题目的默认条件。先提取 problem_title、"
        "problem_statement 和 solution_approach：problem_title 必须是简洁的中文算法题名；"
        "problem_statement 只写算法题的简要摘要，用几句话说明材料能够确认的任务目标、输入和"
        "输出，不要复述代码结构，也不要扩写材料未提供的约束；解题思路应说明所选算法或数据"
        "结构、核心不变量、执行步骤、时间与空间复杂度以及边界处理。然后固定生成 3 道便于快速"
        "复习的四选一题，且每题只有一个正确答案。第 1 题的 focus 必须是 main_approach，直接"
        "检验整道题的主要实现思路及关键执行过程；第 2 题的 focus 必须是 data_structure，检验"
        "实现使用的数据结构、该数据结构的作用及选择原因；第 3 题的 focus 必须是 boundary，"
        "检验最重要的边界条件或易错处理。每道 question 的题干都必须自然包含中文题名，例如"
        "“在《层序遍历》中，……”，不能只写“该实现”而省略题名。不要把次要"
        "细节拆成大量题目。explanation 必须详细说明正确选项为什么成立，并逐项解释其余"
        "选项为什么不适合当前题目；例如两数之和使用哈希表时，应说明它通过 O(1) 平均查找"
        "补数把双重循环降为一次遍历 O(n)，代价是 O(n) 额外空间。evidence 必须紧贴题干、"
        "题解或代码。所有内容使用与材料一致的主要语言。"
    )


def algorithm_title_hint(source_name: str) -> str:
    stem = Path(source_name).stem.strip()
    stem = re.sub(r"[_\-\s]*\d+$", "", stem).strip(" _-")
    return stem if re.search(r"[\u4e00-\u9fff]", stem) else ""


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
    if suffix in DOCUMENT_EXTENSIONS:
        boundaries = re.split(r"(?m)(?=^#{1,4}\s+|^[^\n]+\n[=-]{3,}\s*$)", content)
    elif suffix in CODE_EXTENSIONS:
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

    def ingest(self, path: str | Path, knowledge_type: str = "document") -> dict:
        if knowledge_type not in {"document", "code"}:
            raise ValueError("知识类型必须是 document 或 code")
        file_path = Path(path)
        content = read_knowledge_file(file_path)
        source_id, created = self.database.add_source(file_path, content, knowledge_type)
        if not created:
            existing = next(item for item in self.database.list_sources() if item["id"] == source_id)
            algorithm_questions_current = (
                knowledge_type == "code" and existing["question_count"] == 3
            )
            if existing["status"] == "ready" and (
                knowledge_type != "code" or algorithm_questions_current
            ):
                return {
                    "source_id": source_id,
                    "created": False,
                    "questions": existing["question_count"],
                    "problem_title": existing.get("problem_title", ""),
                }

        try:
            self.database.set_source_status(source_id, "processing")
            chunks = split_knowledge(content, file_path.suffix.lower())
            chunk_ids = self.database.replace_chunks(source_id, chunks)
            if knowledge_type == "code":
                study = self._generate_algorithm_study(
                    file_path.name,
                    chunks,
                    chunk_ids,
                )
                self.database.set_source_algorithm_metadata(
                    source_id,
                    study["problem_title"],
                    study["problem_statement"],
                    study["solution_approach"],
                )
                questions = study["questions"]
            else:
                questions = self._generate_questions(
                    file_path.name,
                    file_path.suffix.lower(),
                    chunks,
                    chunk_ids,
                )
            question_ids = self.database.add_questions(source_id, questions)
            self.database.set_source_status(source_id, "ready")
            result = {
                "source_id": source_id,
                "created": created,
                "reprocessed": not created,
                "questions": len(question_ids),
            }
            if knowledge_type == "code":
                result["problem_title"] = study["problem_title"]
            return result
        except Exception as exc:
            self.database.set_source_status(source_id, "failed", str(exc)[:800])
            raise

    def _generate_questions(
        self,
        source_name: str,
        suffix: str,
        chunks: list[dict],
        chunk_ids: list[int],
    ) -> list[dict]:
        selected = chunks[:6]
        material_parts = []
        for index, chunk in enumerate(selected):
            material_parts.append(
                f"[片段 {index + 1} | {chunk.get('heading') or '未命名'}]\n{chunk['content']}"
            )
        material = "\n\n".join(material_parts)
        profile, _ = knowledge_profile(suffix)
        system = question_system_prompt(suffix)
        try:
            raw = self.provider.complete(
                [{
                    "role": "user",
                    "content": (
                        f"资料名：{source_name}\n资料类型：{profile}\n"
                        "请先在内部识别最值得练习的知识单元，再直接返回题目结构。\n\n"
                        f"{material}"
                    ),
                }],
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

    def _generate_algorithm_study(
        self,
        source_name: str,
        chunks: list[dict],
        chunk_ids: list[int],
    ) -> dict:
        selected = chunks[:6]
        material = "\n\n".join(
            f"[片段 {index + 1} | {chunk.get('heading') or '未命名'}]\n{chunk['content']}"
            for index, chunk in enumerate(selected)
        )
        title_hint = algorithm_title_hint(source_name)
        try:
            raw = self.provider.complete(
                [{
                    "role": "user",
                    "content": (
                        f"题解文件：{source_name}\n"
                        f"中文题名提示：{title_hint or '请根据材料归纳并翻译为中文'}\n"
                        "请提取题名、题干和完整解题思路，再生成针对这道算法题的练习。\n\n"
                        f"{material}"
                    ),
                }],
                algorithm_study_system_prompt(),
                ALGORITHM_STUDY_SCHEMA,
            )
            parsed = AlgorithmStudySet.model_validate(raw)
        except (ValidationError, ProviderError, TypeError, ValueError) as exc:
            raise ProviderError(f"算法题题解拆解失败：{exc}") from exc

        questions = []
        for position, question in enumerate(parsed.questions):
            if len(question.options) != 4 or len(set(question.options)) != 4:
                continue
            expected_focus = ("main_approach", "data_structure", "boundary")[position]
            if question.focus != expected_focus:
                raise ProviderError(
                    "算法题练习结构不正确：三题必须依次复习主要思路、数据结构和边界条件"
                )
            value = question.model_dump()
            value.pop("focus", None)
            value["chunk_id"] = chunk_ids[min(position, len(chunk_ids) - 1)] if chunk_ids else None
            questions.append(value)
        if len(questions) != 3:
            raise ProviderError("模型没有生成完整的主思路、数据结构和边界条件三道题")
        problem_title = title_hint or parsed.problem_title.strip()
        problem_statement = (
            f"题目名称：{problem_title}\n\n"
            f"算法题简要摘要：{parsed.problem_statement.strip()}"
        )
        return {
            "problem_title": problem_title,
            "problem_statement": problem_statement,
            "solution_approach": parsed.solution_approach,
            "questions": questions,
        }
