"""多步 agent 运行时使用的轻量工作记忆。

session history 负责保存完整事件流；这个模块只保存更小的一层工作集：
当前任务摘要、最近接触的文件、文件短摘要，以及少量跨轮笔记。
这样下一轮 prompt 还能接上上一轮，但不会被整段历史塞满。
"""

import hashlib
from datetime import datetime
import re
from pathlib import Path

from .utils import clip, now

WORKING_FILE_LIMIT = 8      # 工作文件列表上限：Agent 最近关注/操作过的文件路径最多保留 8 个，防止上下文过载
EPISODIC_NOTE_LIMIT = 12    #  episodic notes（情景笔记）上限：从工具执行结果中提炼出的关键知识点/摘要最多保留 12 条（已弃用）
FILE_SUMMARY_LIMIT = 6      # 文件摘要上限：为单个文件生成的内容摘要最大字符数限制，确保每个文件的记忆片段足够精简
ASK_MODE_LOADED_LIMIT = 5   # /ask 模式下已加载文档上限
ASK_MODE_INDEX_LINES = 3    # 生成摘要时读取文件的前 N 行


def default_memory_state():
    # 用一个小而结构化的状态，而不是一大段自由文本摘要。
    return {
        "working": {
            "task_summary": "",
            "recent_files": [],
        },
        "file_summaries": {},
        "mistakes_index": [],
        "task": "",
        "files": [],
    }


def default_ask_mode_state():
    """/ask 模式的动态 memory 结构。

    mode: 文档类型（trust_path / notes / mistakes）
    original_request: 用户的原始请求（跨轮保持不变）
    index: 全量文档索引（轻量，id + label + summary）
    loaded: 已加载的完整文档内容（最多 ASK_MODE_LOADED_LIMIT 个）
    """
    return {
        "mode": "",
        "original_request": "",
        "index": [],
        "loaded": {},
    }


def populate_index(state, items, workspace_root=None):
    """用文件列表填充 index。items 为 dict 列表，需含 label，可选 summary/offset/length/file_path。"""
    ask = state.setdefault("ask_mode", default_ask_mode_state())
    ask["index"] = []
    for i, item in enumerate(items, 1):
        entry = {"id": i, "label": str(item.get("label", ""))}
        if item.get("summary"):
            entry["summary"] = clip(str(item["summary"]).strip(), 200)
        if item.get("file_path"):
            entry["file_path"] = str(item["file_path"])
        if item.get("offset") is not None:
            entry["offset"] = int(item["offset"])
            entry["length"] = int(item["length"])
        ask["index"].append(entry)
    return state


def load_document(state, doc_id, path, content, workspace_root=None):
    """将完整文档内容加载到 loaded，超出上限时淘汰最旧的。"""
    ask = state.setdefault("ask_mode", default_ask_mode_state())
    loaded = ask.setdefault("loaded", {})

    # 淘汰最旧的
    while len(loaded) >= ASK_MODE_LOADED_LIMIT:
        oldest_key = min(loaded.keys(), key=lambda k: loaded[k].get("loaded_at", ""))
        loaded.pop(oldest_key, None)

    loaded[str(doc_id)] = {
        "path": canonicalize_path(path, workspace_root) if workspace_root else str(path),
        "content": clip(str(content), 50000),
        "loaded_at": now(),
    }
    return state


def load_document_by_offset(state, doc_id, file_path, offset, length, workspace_root=None):
    """用 offset/length 从文件中 seek 读取单条文档，加载到 loaded。"""
    try:
        with open(file_path, "rb") as f:
            f.seek(offset)
            content = f.read(length).decode("utf-8")
    except (OSError, ValueError):
        return state
    return load_document(state, doc_id, file_path, content, workspace_root)


def unload_document(state, doc_id):
    """从 loaded 中移除指定文档。"""
    ask = state.get("ask_mode", {})
    loaded = ask.get("loaded", {})
    loaded.pop(str(doc_id), None)
    return state


def update_index_summary(state, doc_id, new_summary):
    """更新 index 中指定文档的摘要。"""
    ask = state.get("ask_mode", {})
    for entry in ask.get("index", []):
        if str(entry["id"]) == str(doc_id):
            entry["summary"] = clip(str(new_summary).strip(), 200)
            break
    return state


def generate_file_summary(path, limit=180):
    """读取文件前几行生成摘要。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and lines[0].startswith("# "):
            lines = lines[1:]
        if not lines:
            return "(empty)"
        summary = " | ".join(lines[:ASK_MODE_INDEX_LINES])
        return clip(summary, limit)
    except Exception:
        return ""


def render_ask_memory(state, workspace_root=None):
    """渲染 /ask 模式的 memory 为文本。"""
    ask = state.get("ask_mode", {})
    if not ask.get("mode"):
        return ""

    lines = [f"Mode: {ask['mode']}"]
    if ask.get("original_request"):
        lines.append(f"Original request: {ask['original_request']}")

    index = ask.get("index", [])
    if index:
        # 检测文件路径（所有条目通常指向同一个文件）
        file_path = index[0].get("file_path", "") if index else ""
        if file_path:
            lines.append(f"Source file: {file_path}")
        lines.append(f"Document index ({len(index)} items):")
        for entry in index:
            summary = f" - {entry['summary']}" if entry.get("summary") else ""
            lines.append(f"  [{entry['id']}] {entry['label']}{summary}")
        lines.append("User references documents by number, e.g. 'read #3', 'modify #5'. Use read_file to access these files, not read_entry.")

    loaded = ask.get("loaded", {})
    if loaded:
        lines.append(f"\nLoaded documents ({len(loaded)}):")
        for doc_id, doc in sorted(loaded.items()):
            lines.append(f"  #{doc_id} {doc['path']}:")
            content_preview = clip(doc["content"], 3000)
            lines.append(f"  {content_preview}")

    return "\n".join(lines)

# 确保传入的值是列表，如果不是则转为列表
def _ensure_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if value in (None, ""):
        return []
    return [value]

# 去重并保留原始顺序
def _dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result

# 判断路径是否逃逸(软过滤，不会报错，只会返回None)
def resolve_workspace_path(raw_path, workspace_root=None):
    path = Path(str(raw_path))
    if workspace_root is None:
        return path

    root = Path(workspace_root).resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved

# 这个函数的主要目的是"去绝对化"。
# 在 AI 智能体或沙箱环境中，内部处理通常使用绝对路径（为了安全），但在向用户或 AI 展示时，
# 使用相对路径（如 src/main.py 而不是 /home/user/project/src/main.py）更清晰、更安全，也更具可移植性。
# 成功时：返回干净的相对路径字符串（如 src/main.py）。
# 失败时：返回原始路径的 POSIX 格式字符串（如 ../etc/passwd），不抛出异常。
def canonicalize_path(raw_path, workspace_root=None):
    resolved = resolve_workspace_path(raw_path, workspace_root)
    # .as_posix() 的作用：将路径中的反斜杠 \（Windows 风格）转换为正斜杠 /（Unix/Linux 风格），但不改变路径本身的含义
    if resolved is None:
        return Path(str(raw_path)).as_posix()
    if workspace_root is None:
        return Path(str(raw_path)).as_posix()
    root = Path(workspace_root).resolve()
    return resolved.relative_to(root).as_posix()

# 文件指纹生成器
def file_freshness(raw_path, workspace_root=None):
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return None
    return hashlib.sha256(resolved.read_bytes()).hexdigest()

# 将文本拆解为标准化的小写词元集合
def _tokenize(text):
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_]+", str(text))}


# 将各种格式的时间字符串安全地转换为 Unix 时间戳
def _parse_timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return 0.0

# 对多种数据结构的笔记进行标准化，返回字典
def _normalize_note(note, index):
    if isinstance(note, str):
        text = clip(note.strip(), 500)
        return {
            "text": text,
            "tags": [],
            "source": "",
            "created_at": now(),
            "note_index": index,
        }

    if not isinstance(note, dict):
        text = clip(str(note).strip(), 500)
        return {
            "text": text,
            "tags": [],
            "source": "",
            "created_at": now(),
            "note_index": index,
        }

    text = clip(str(note.get("text", "")).strip(), 500)
    tags = [str(tag).strip() for tag in _ensure_list(note.get("tags", [])) if str(tag).strip()]
    source = str(note.get("source", "")).strip()
    created_at = str(note.get("created_at", "")).strip() or now()
    note_index = int(note.get("note_index", index))
    return {
        "text": text,
        "tags": _dedupe_preserve_order(tags),
        "source": source,
        "created_at": created_at,
        "note_index": note_index,
    }

# 它的核心任务是接收一个可能来自旧版本程序、格式混乱或不完整的"原始状态字典"，
# 将其清洗、补全、去重、截断，并统一字段命名，最终输出一个结构严谨、可直接供当前运行时使用的"标准化状态"。
def normalize_memory_state(state, workspace_root=None):
    if state is None:
        state = default_memory_state()
    elif not isinstance(state, dict):
        raise TypeError("memory state must be a mapping")

    # 规范化层的作用，是把"磁盘里可能长得不太一样的旧状态"
    # 统一整理成当前 runtime 可直接使用的紧凑结构。
    working = state.get("working")
    if not isinstance(working, dict):
        working = {}
    working.setdefault("task_summary", "")
    working.setdefault("recent_files", [])
    working["task_summary"] = clip(str(working.get("task_summary", "")).strip(), 300)
    working["recent_files"] = _dedupe_preserve_order(
        [
            canonicalize_path(path, workspace_root)
            for path in _ensure_list(working.get("recent_files", []))
            if str(path).strip()
        ]
    )[-WORKING_FILE_LIMIT:]
    state["working"] = working

    if not str(working["task_summary"]).strip() and state.get("task"):
        working["task_summary"] = clip(str(state.get("task", "")).strip(), 300)
    if not working["recent_files"] and state.get("files"):
        working["recent_files"] = _dedupe_preserve_order(
            [
                canonicalize_path(path, workspace_root)
                for path in _ensure_list(state.get("files", []))
                if str(path).strip()
            ]
        )[-WORKING_FILE_LIMIT:]

    file_summaries = state.get("file_summaries")
    if not isinstance(file_summaries, dict):
        file_summaries = {}
    normalized_file_summaries = {}
    for path, summary in file_summaries.items():
        path = canonicalize_path(path, workspace_root)
        if isinstance(summary, dict):
            text = clip(str(summary.get("summary", "")).strip(), 500)
            created_at = str(summary.get("created_at", "")).strip() or now()
            freshness = summary.get("freshness")
            freshness = None if freshness in (None, "") else str(freshness).strip() or None
        else:
            text = clip(str(summary).strip(), 500)
            created_at = now()
            freshness = None
        if not path or not text:
            continue
        normalized_file_summaries[path] = {
            "summary": text,
            "created_at": created_at,
            "freshness": freshness,
        }
    state["file_summaries"] = normalized_file_summaries

    state["task"] = working["task_summary"]
    state["files"] = list(working["recent_files"])
    return state

# 更新当前任务摘要
def set_task_summary(state, summary, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    state["working"]["task_summary"] = clip(str(summary).strip(), 300)
    state["task"] = state["working"]["task_summary"]
    return state

# 保存文件至工作文件列表
def remember_file(state, path, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    if not path:
        return state
    files = [item for item in state["working"]["recent_files"] if item != path]
    files.append(path)
    state["working"]["recent_files"] = files[-WORKING_FILE_LIMIT:]
    state["files"] = list(state["working"]["recent_files"])
    return state

# 添加笔记到 notes 列表（独立于 memory state，直接操作 session["relevant_notes"]）
def append_note(notes, text, tags=(), source="", created_at=None):
    text = clip(str(text).strip(), 500)
    if not text:
        return notes

    normalized_tags = _dedupe_preserve_order(
        [str(tag).strip() for tag in _ensure_list(tags) if str(tag).strip()]
    )
    note = {
        "text": text,
        "tags": normalized_tags,
        "source": str(source).strip(),
        "created_at": str(created_at).strip() if created_at else now(),
        "note_index": len(notes),
    }

    notes = [item for item in notes if item["text"] != note["text"]]
    notes.append(note)
    return notes[-EPISODIC_NOTE_LIMIT:]

# 设定文件摘要
def set_file_summary(state, path, summary, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    summary = clip(str(summary).strip(), 500)
    if not path or not summary:
        return state
    state["file_summaries"][path] = {
        "summary": summary,
        "created_at": now(),
        "freshness": file_freshness(path, workspace_root),
    }
    return state

# 无效化一个文件摘要
def invalidate_file_summary(state, path, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    if not path:
        return state
    state["file_summaries"].pop(path, None)
    return state

# 从读取文件的原始结果中生成摘要（用模型压缩）
def summarize_read_result(result, model_client=None, limit=180):
    if model_client:
        from . import compressor
        return compressor.compress_document(str(result), model_client)
    # 降级：无模型时取前 3 行
    lines = [line.strip() for line in str(result).splitlines() if line.strip()]
    if not lines:
        return "(empty)"
    if lines[0].startswith("# "):
        lines = lines[1:]
    if not lines:
        return "(empty)"
    summary = " | ".join(lines[:3])
    return clip(summary, limit)

# 根据你当前的问题（Query），从笔记列表中找出最相关的几条。
# 根据笔记的tags和query的tokens的相关度来rank
def retrieval_candidates(notes, query, limit=3):
    query_tokens = _tokenize(query)
    ranked = []
    for note in notes:
        # 召回逻辑故意保持简单透明：先看 tag 精确命中，
        # 再看关键词重叠，最后看新旧程度。这里不引入 embedding。
        note_tags = {tag.lower() for tag in note.get("tags", [])}
        note_tokens = _tokenize(note.get("text", "")) | _tokenize(note.get("source", "")) | note_tags
        exact_tag_match = int(bool(query_tokens & note_tags))
        keyword_overlap = len(query_tokens & note_tokens)
        if exact_tag_match == 0 and keyword_overlap == 0:
            continue
        recency = _parse_timestamp(note.get("created_at"))
        note_index = int(note.get("note_index", 0))
        ranked.append(((exact_tag_match, keyword_overlap, recency, note_index), note))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [note for _, note in ranked[:limit]]

# 将检索到的相关笔记格式化成一段文本，准备塞进发给大模型的 Prompt 中
def retrieval_view(notes, query, limit=3):
    candidates = retrieval_candidates(notes, query, limit=limit)
    lines = ["Relevant notes:"]
    if not candidates:
        lines.append("- none")
        return "\n".join(lines)
    for note in candidates:
        lines.append(f"- {note['text']}")
    return "\n".join(lines)

# 提取最关键的上下文信息——当前任务、最近文件、有效的文件摘要、笔记数量，
# 用简洁的格式呈现，帮助模型快速理解当前状态，同时避免上下文窗口被撑爆。
def render_memory_text(state, workspace_root=None):
    # /ask 模式：渲染 ask_mode 专用结构
    ask = state.get("ask_mode", {})
    if ask.get("mode"):
        return render_ask_memory(state, workspace_root)

    state = normalize_memory_state(state, workspace_root)
    # 这里渲染的是给模型看的紧凑"仪表盘"，不是完整回放。
    # 笔记正文默认不展开，只有在相关召回时才按需拿出来。
    lines = [
        "Memory:",
        f"- task: {state['working']['task_summary'] or '-'}",
        f"- recent_files: {', '.join(state['working']['recent_files']) or '-'}",
    ]

    summaries = []
    for path in state["working"]["recent_files"][:FILE_SUMMARY_LIMIT]:
        summary = state["file_summaries"].get(path, {})
        current_freshness = file_freshness(path, workspace_root)
        # 比较"保存摘要时的文件哈希"和"当前文件的哈希"。
        # 如果两者相等，说明文件自生成摘要后没有被修改过，摘要是有效的。
        # 如果两者不等，说明文件被修改了，摘要是过期的，不显示。
        if summary.get("summary", "") and summary.get("freshness") == current_freshness:
            summaries.append(f"- {path}: {summary['summary']}")
    if summaries:
        lines.append("- file_summaries:")
        lines.extend(f"  {line}" for line in summaries)
    else:
        lines.append("- file_summaries: -")

    return "\n".join(lines)


def load_mistakes_index(state, index_entries):
    """将错题索引加载到 memory state 中。"""
    state.setdefault("mistakes_index", [])
    state["mistakes_index"] = index_entries
    return state


def search_mistakes(state, query, limit=3):
    """从错题索引中检索与 query 相关的错题。

    使用与 retrieval_candidates 相同的 tag + keyword 机制。
    """
    index = state.get("mistakes_index", [])
    if not index:
        return []

    query_tokens = _tokenize(query)
    ranked = []
    for entry in index:
        entry_tags = {t.lower() for t in entry.get("tags", [])}
        entry_tokens = _tokenize(entry.get("summary", "")) | entry_tags
        exact_tag_match = int(bool(query_tokens & entry_tags))
        keyword_overlap = len(query_tokens & entry_tokens)
        if exact_tag_match == 0 and keyword_overlap == 0:
            continue
        recency = _parse_timestamp(entry.get("timestamp", ""))
        ranked.append(((exact_tag_match, keyword_overlap, recency), entry))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in ranked[:limit]]


class LayeredMemory:
    def __init__(self, state=None, workspace_root=None):
        self.workspace_root = workspace_root
        self.state = normalize_memory_state(state, workspace_root)

    def to_dict(self):
        self.state = normalize_memory_state(self.state, self.workspace_root)
        return self.state

    def canonical_path(self, path):
        return canonicalize_path(path, self.workspace_root)

    def set_task_summary(self, summary):
        self.state = set_task_summary(self.state, summary, self.workspace_root)
        return self

    def remember_file(self, path):
        self.state = remember_file(self.state, path, self.workspace_root)
        return self

    def set_file_summary(self, path, summary):
        self.state = set_file_summary(self.state, path, summary, self.workspace_root)
        return self

    def invalidate_file_summary(self, path):
        self.state = invalidate_file_summary(self.state, path, self.workspace_root)
        return self

    def render_memory_text(self):
        return render_memory_text(self.state, self.workspace_root)

    def load_mistakes_index(self, index_entries):
        load_mistakes_index(self.state, index_entries)
        return self

    def search_mistakes(self, query, limit=3):
        return search_mistakes(self.state, query, limit)

    # /ask 模式专用
    def populate_index(self, items):
        self.state = populate_index(self.state, items, self.workspace_root)
        return self

    def load_document(self, doc_id, path, content):
        self.state = load_document(self.state, doc_id, path, content, self.workspace_root)
        return self

    def load_document_by_offset(self, doc_id, file_path, offset, length):
        self.state = load_document_by_offset(self.state, doc_id, file_path, offset, length, self.workspace_root)
        return self

    def update_index_summary(self, doc_id, new_summary):
        self.state = update_index_summary(self.state, doc_id, new_summary)
        return self
