"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import shutil
import subprocess
import textwrap
from functools import partial
from pathlib import Path

from .utils import IGNORED_PATH_NAMES, clip
# 定义 Agent 可用的基础工具规格说明。这些信息会被拼接进 System Prompt，告诉大模型有哪些工具可用、参数是什么以及是否危险。
BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'"},
        "param_descriptions": {"path": "Directory path relative to workspace root, defaults to '.'"},
        "risky": False,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": "int=500"},
        "param_descriptions": {"path": "File path relative to workspace root", "start": "Starting line number (1-based)", "end": "Ending line number (inclusive, max 1000)"},
        "risky": False,
        "description": "Read a UTF-8 file by line range. Max 1000 lines per call.",
    },
    "search": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "param_descriptions": {"pattern": "Regex or literal search pattern", "path": "Directory to search in, defaults to '.'"},
        "risky": False,
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": "int=20"},
        "param_descriptions": {"command": "Shell command to execute", "timeout": "Timeout in seconds (1-120)"},
        "risky": True,
        "description": "Run a shell command in the repo root.",
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "param_descriptions": {"path": "File path relative to workspace root", "content": "Complete file content to write"},
        "risky": True,
        "description": "Write a text file. Creates or overwrites the file.",
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "param_descriptions": {"path": "File path relative to workspace root", "old_text": "Exact text to find (must occur exactly once)", "new_text": "Replacement text"},
        "risky": True,
        "description": "Replace one exact text block in a file.",
    },
    "delete_file": {
        "schema": {"path": "str"},
        "param_descriptions": {"path": "File path relative to workspace root"},
        "risky": True,
        "description": "Delete a file from the workspace.",
    },
    "search_mistakes": {
        "schema": {"query": "str", "limit": "int=3"},
        "param_descriptions": {"query": "Keywords to search in mistake index", "limit": "Max results to return"},
        "risky": False,
        "description": "Search mistake index by keywords. Returns matching mistakes with score and summary.",
    },
    "get_mistake_detail": {
        "schema": {"title": "str"},
        "param_descriptions": {"title": "Title or partial title of the mistake"},
        "risky": False,
        "description": "Get full detail of a mistake by its title. Returns question, answer, feedback, etc.",
    },
    "read_notes": {
        "schema": {"limit": "int=10"},
        "param_descriptions": {"limit": "Max number of recent notes to return"},
        "risky": False,
        "description": "Read recent learning notes from the user's notes file.",
    },
    "write_note": {
        "schema": {"title": "str", "content": "str", "file_path": "str=''"},
        "param_descriptions": {"title": "Note title", "content": "Note content in markdown", "file_path": "Optional related file path to associate with this note"},
        "risky": False,
        "description": "Save a learning note to the user's notes file (~/.bongo/notes/). Use this instead of write_file when creating notes.",
    },
    "read_entry": {
        "schema": {"path": "str", "entry": "int"},
        "param_descriptions": {"path": "File path relative to workspace root", "entry": "1-based entry number from the document index"},
        "risky": False,
        "description": "Read a specific entry by number from a notes or mistakes file. Uses the index for O(1) lookup.",
    },
    "delete_entry": {
        "schema": {"path": "str", "entry": "int"},
        "param_descriptions": {"path": "File path relative to workspace root", "entry": "1-based entry number from the document index"},
        "risky": True,
        "description": "Delete a specific entry by number from a notes or mistakes file. Rebuilds the index after deletion.",
    },
    "read_cache": {
        "schema": {"path": "str"},
        "param_descriptions": {"path": "Cache file path returned by a previous tool call (e.g. 'Full output saved to: ...')"},
        "risky": False,
        "description": "Read a cached output file from ~/.bongo/cache/. Use this when a tool result says 'Full output saved to: ...'.",
    },
}

# 定义"委托"工具的规格。允许主 Agent 把任务分派给一个受限的子 Agent 去执行。
DELEGATE_TOOL_SPEC = {
    # 定义参数：task(子任务描述), max_steps(子 Agent 最大执行步数，限制其能力范围)
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,  # 委托本身被视为安全操作（因为子 Agent 通常是只读的）
    "description": "Ask a bounded read-only child agent to investigate.",
}


def build_tool_registry(agent):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        # name是工具名，spec是对应的配置字典
        # 提前绑定好了agent，只需要输入
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], agent)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if agent.depth < agent.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, agent)}
    return tools



def validate_tool(agent, name, args):
    args = args or {}

    if name == "list_files":
        path = agent.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 500))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        if end - start + 1 > 1000:
            raise ValueError("max 1000 lines per call")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        agent.path(args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "write_file":
        path = agent.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "delete_file":
        path = agent.path(args["path"])
        if not path.exists():
            raise ValueError("path does not exist")
        if path.is_dir():
            raise ValueError("path is a directory, cannot delete")
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        return

    if name == "search_mistakes":
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        return

    if name == "get_mistake_detail":
        title = str(args.get("title", "")).strip()
        if not title:
            raise ValueError("title must not be empty")
        return

    if name == "read_notes":
        return

    if name == "write_note":
        title = str(args.get("title", "")).strip()
        if not title:
            raise ValueError("title must not be empty")
        content = str(args.get("content", "")).strip()
        if not content:
            raise ValueError("content must not be empty")
        return

    if name == "read_entry":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        entry = int(args.get("entry", 0))
        if entry < 1:
            raise ValueError("entry must be >= 1")
        return

    if name == "delete_entry":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        entry = int(args.get("entry", 0))
        if entry < 1:
            raise ValueError("entry must be >= 1")
        return

    if name == "read_cache":
        cache_path = Path(args["path"])
        cache_dir = Path.home() / ".bongo" / "cache"
        if not str(cache_path.resolve()).startswith(str(cache_dir.resolve())):
            raise ValueError("path must be in ~/.bongo/cache/")
        if not cache_path.is_file():
            raise ValueError("cache file not found")
        return

# 工具的具体实现
def tool_list_files(agent, args):
    path = agent.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(agent.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 500))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    if end - start + 1 > 1000:
        raise ValueError("max 1000 lines per call")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(agent.root)}\n{body}"


def tool_search(agent, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = agent.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        result = subprocess.run(
            ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
            cwd=agent.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip() or result.stderr.strip() or "(no matches)"

    matches = []
    files = [path] if path.is_file() else [
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(agent.root).parts)
    ]
    for file_path in files:
        for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(agent.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_run_shell(agent, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    result = subprocess.run(
        command,
        cwd=agent.root,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
        # 目的是减少敏感信息被意外带进命令执行环境的风险。
        env=agent.shell_env(),
    )
    return textwrap.dedent(
        f"""\
        exit_code: {result.returncode}
        stdout:
        {result.stdout.strip() or "(empty)"}
        stderr:
        {result.stderr.strip() or "(empty)"}
        """
    ).strip()


def tool_write_file(agent, args):
    path = agent.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(agent.root)} ({len(content)} chars)"


def tool_patch_file(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return f"patched {path.relative_to(agent.root)}"


def tool_delete_file(agent, args):
    path = agent.path(args["path"])
    if not path.exists():
        raise ValueError("path does not exist")
    if path.is_dir():
        raise ValueError("path is a directory, cannot delete")
    rel = path.relative_to(agent.root)
    path.unlink()
    return f"deleted {rel}"


def tool_delegate(agent, args):
    if agent.depth >= agent.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")

    from .runtime import bongo

    child = bongo(
        model_client=agent.model_client,
        work_dir=agent.root,
        session_store=agent.session_store,
        run_store=agent.run_store,
        approval_policy="never",
        max_steps=int(args.get("max_steps", 3)),
        max_new_tokens=agent.max_new_tokens,
        depth=agent.depth + 1,
        max_depth=agent.max_depth,
        read_only=True,
        secret_env_names=agent.secret_env_names,
        shell_env_allowlist=agent.shell_env_allowlist,
    )
    child.session["memory"]["task"] = task
    child.session["memory"]["notes"] = [clip(agent.history_text(), 300)]
    return "delegate_result:\n" + child.ask(task)


def tool_search_mistakes(agent, args):
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query must not be empty")
    limit = int(args.get("limit", 3))

    from .profile import UserProfile, load_current_user
    from .memory import search_mistakes, load_mistakes_index

    username = load_current_user()
    profile = UserProfile(username)
    index = profile.get_mistakes_index()
    state = {"mistakes_index": index}
    results = search_mistakes(state, query, limit=limit)

    if not results:
        return "未找到相关错题。"
    lines = [f"找到 {len(results)} 条相关错题："]
    for r in results:
        lines.append(f"- [{r['timestamp']}] 得分:{r['score']} | 来源:{r['source']} | {r['summary']}")
    return "\n".join(lines)


def tool_get_mistake_detail(agent, args):
    title = str(args.get("title", "")).strip()
    if not title:
        raise ValueError("title must not be empty")

    from .profile import UserProfile, load_current_user

    username = load_current_user()
    profile = UserProfile(username)
    mistakes = profile.get_mistakes_from_file(limit=100)

    for m in mistakes:
        if m.get("title") == title or title in m.get("title", ""):
            lines = [
                f"题目：{m.get('question', '')}",
                f"你的回答：{m.get('user_answer', '')}",
                f"得分：{m.get('score', 0)}",
                f"反馈：{m.get('feedback', '')}",
            ]
            if m.get("correct_answer"):
                lines.append(f"正确答案：{m['correct_answer']}")
            if m.get("source"):
                lines.append(f"来源：{m['source']}")
            return "\n".join(lines)
    return f"未找到标题包含 '{title}' 的错题。"


def tool_read_notes(agent, args):
    limit = int(args.get("limit", 10))

    from .profile import UserProfile, load_current_user

    username = load_current_user()
    profile = UserProfile(username)
    notes = profile.get_notes(limit=limit)

    if not notes:
        return "暂无笔记。"
    lines = [f"最近 {len(notes)} 条笔记："]
    for n in notes:
        ts = n.get("timestamp", "")[:10]
        fp = f" [{n['file_path']}]" if n.get("file_path") else ""
        lines.append(f"- {ts} {n.get('title', '')}{fp}")
        content = n.get("content", "")
        if content:
            lines.append(f"  {content[:200]}")
    return "\n".join(lines)


def tool_write_note(agent, args):
    title = str(args.get("title", "")).strip()
    if not title:
        raise ValueError("title must not be empty")
    content = str(args.get("content", "")).strip()
    if not content:
        raise ValueError("content must not be empty")
    file_path = str(args.get("file_path", "")).strip() or None

    from .profile import UserProfile, load_current_user

    username = load_current_user()
    profile = UserProfile(username)
    result = profile.add_note(content=content, file_path=file_path, title=title)
    return f"已保存笔记：{result.get('title', title)}"


def tool_read_entry(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    entry_num = int(args.get("entry", 0))
    if entry_num < 1:
        raise ValueError("entry must be >= 1")

    from .profile import UserProfile

    # 根据文件名推断索引文件位置
    parent = path.parent
    stem = path.stem
    # 索引文件 = 同目录下 {stem}_index.md
    index_path = parent / f"{stem}_index.md"
    if not index_path.exists():
        # 尝试 UserProfile 生成的默认索引路径
        username = stem
        profile = UserProfile(username)
        if path.resolve() == profile.notes_file.resolve():
            index_path = profile.notes_index_file
        elif path.resolve() == profile.mistakes_file.resolve():
            index_path = profile.index_file
        else:
            return f"未找到 {path.name} 对应的索引文件。"

    if not index_path.exists():
        return f"索引文件 {index_path.name} 不存在。"

    # 解析索引获取 offset/length
    index_text = index_path.read_text(encoding="utf-8")
    entries = []
    for line in index_text.split("\n"):
        if not line.startswith("- "):
            continue
        try:
            body = line[2:]
            parts = body.split(" | ")
            offset = None
            length = None
            label = parts[0].strip() if parts else ""
            for p in parts:
                p = p.strip()
                if p.startswith("offset:"):
                    rest = p[7:]
                    if ", len:" in rest:
                        o, l = rest.split(", len:", 1)
                        offset = int(o)
                        length = int(l)
            if offset is not None:
                entries.append({"label": label, "offset": offset, "length": length})
        except (ValueError, IndexError):
            continue

    if not entries:
        return "索引中没有可读取的条目。"
    if entry_num > len(entries):
        return f"条目编号 {entry_num} 超出范围，索引共 {len(entries)} 条。"

    target = entries[entry_num - 1]
    raw = UserProfile._read_entry_at(path, target["offset"], target["length"])
    header = f"# Entry {entry_num}/{len(entries)} from {path.name}"
    return f"{header}\n{raw}"


def tool_delete_entry(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    entry_num = int(args.get("entry", 0))
    if entry_num < 1:
        raise ValueError("entry must be >= 1")

    from .profile import UserProfile

    # 查找索引文件
    parent = path.parent
    stem = path.stem
    index_path = parent / f"{stem}_index.md"
    if not index_path.exists():
        username = stem
        profile = UserProfile(username)
        if path.resolve() == profile.notes_file.resolve():
            index_path = profile.notes_index_file
        elif path.resolve() == profile.mistakes_file.resolve():
            index_path = profile.index_file
        else:
            return f"未找到 {path.name} 对应的索引文件。"

    if not index_path.exists():
        return f"索引文件 {index_path.name} 不存在。"

    # 解析索引
    index_text = index_path.read_text(encoding="utf-8")
    entries = []
    for line in index_text.split("\n"):
        if not line.startswith("- "):
            continue
        try:
            body = line[2:]
            parts = body.split(" | ")
            offset = None
            length = None
            label = parts[0].strip() if parts else ""
            for p in parts:
                p = p.strip()
                if p.startswith("offset:"):
                    rest = p[7:]
                    if ", len:" in rest:
                        o, l = rest.split(", len:", 1)
                        offset = int(o)
                        length = int(l)
            if offset is not None:
                entries.append({"label": label, "offset": offset, "length": length})
        except (ValueError, IndexError):
            continue

    if not entries:
        return "索引中没有可删除的条目。"
    if entry_num > len(entries):
        return f"条目编号 {entry_num} 超出范围，索引共 {len(entries)} 条。"

    target = entries[entry_num - 1]
    raw_bytes = path.read_bytes()
    entry_bytes = raw_bytes[target["offset"]:target["offset"] + target["length"]]

    # 删除该条目：offset 之前 + (offset+length) 之后
    new_content = raw_bytes[:target["offset"]] + raw_bytes[target["offset"] + target["length"]:]
    path.write_bytes(new_content)

    # 重建索引
    username = path.stem
    profile = UserProfile(username)
    if path.resolve() == profile.notes_file.resolve():
        profile._rebuild_notes_index()
    elif path.resolve() == profile.mistakes_file.resolve():
        profile._rebuild_mistakes_index()

    label = target["label"][:50]
    return f"已删除第 {entry_num} 条：{label}（剩余 {len(entries) - 1} 条）"


def tool_read_cache(agent, args):
    cache_path = Path(args["path"])
    cache_dir = Path.home() / ".bongo" / "cache"
    if not str(cache_path.resolve()).startswith(str(cache_dir.resolve())):
        raise ValueError("path must be in ~/.bongo/cache/")
    if not cache_path.is_file():
        raise ValueError("cache file not found")
    content = cache_path.read_text(encoding="utf-8", errors="replace")
    return f"# {cache_path.name}\n{content}"


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "delete_file": tool_delete_file,
    "search_mistakes": tool_search_mistakes,
    "get_mistake_detail": tool_get_mistake_detail,
    "read_notes": tool_read_notes,
    "write_note": tool_write_note,
    "read_entry": tool_read_entry,
    "delete_entry": tool_delete_entry,
    "read_cache": tool_read_cache,
}
