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
    "file_info": {
        "schema": {"path": "str"},
        "param_descriptions": {"path": "File path relative to workspace root"},
        "risky": False,
        "description": "Get file metadata (line count, size) without reading content. Use before reading large files.",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": "int=200", "tail": "int=0", "grep": "str=''"},
        "param_descriptions": {"path": "File path relative to workspace root", "start": "Starting line number (1-based)", "end": "Ending line number (inclusive)", "tail": "Read last N lines (overrides start/end)", "grep": "Only return lines containing this keyword (case-insensitive)"},
        "risky": False,
        "description": "Read a UTF-8 file. Modes: tail=N (last N lines), grep=pattern (filter), default=line range. Max 2000 lines.",
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
        "schema": {"path": "str", "old_text": "str", "new_text": "str", "nth": "int=0"},
        "param_descriptions": {"path": "File path relative to workspace root", "old_text": "Exact text to find", "new_text": "Replacement text", "nth": "Replace Nth occurrence (0=must be unique, >0=replace that occurrence)"},
        "risky": True,
        "description": "Replace exact text in a file. Default: old_text must occur exactly once. Use nth=N to replace the Nth occurrence.",
    },
    "append_file": {
        "schema": {"path": "str", "content": "str"},
        "param_descriptions": {"path": "File path relative to workspace root", "content": "Content to append to end of file"},
        "risky": True,
        "description": "Append content to the end of an existing file. No read needed. Use for adding lines.",
    },
    "insert_at_line": {
        "schema": {"path": "str", "line": "int", "content": "str"},
        "param_descriptions": {"path": "File path relative to workspace root", "line": "Line number to insert before (1-based)", "content": "Content to insert"},
        "risky": True,
        "description": "Insert content before a specific line number. Use file_info or read_file to find the target line.",
    },
    "delete_line": {
        "schema": {"path": "str", "line": "int"},
        "param_descriptions": {"path": "File path relative to workspace root", "line": "Line number to delete (1-based)"},
        "risky": True,
        "description": "Delete a specific line from a file by line number. Use file_info or read_file to find the target line.",
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

    if name == "file_info":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        return

    if name == "read_file":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        tail = int(args.get("tail", 0))
        if tail < 0:
            raise ValueError("tail must be >= 0")
        if tail == 0:
            start = int(args.get("start", 1))
            end = int(args.get("end", 200))
            if start < 1 or end < start:
                raise ValueError("invalid line range")
            if end - start + 1 > 2000:
                raise ValueError("max 2000 lines per call")
        elif tail > 2000:
            raise ValueError("max 2000 lines per call")
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
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        nth = int(args.get("nth", 0))
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if nth == 0:
            if count != 1:
                raise ValueError(f"old_text must occur exactly once, found {count}")
        else:
            if nth < 1 or nth > count:
                raise ValueError(f"nth={nth} out of range, found {count} occurrences")
        return

    if name == "append_file":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file — use write_file to create")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "insert_at_line":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        line_no = int(args.get("line", 0))
        if line_no < 1:
            raise ValueError("line must be >= 1")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "delete_line":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        line_no = int(args.get("line", 0))
        if line_no < 1:
            raise ValueError("line must be >= 1")
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
    tail = int(args.get("tail", 0))
    grep_pattern = str(args.get("grep", "")).strip()
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)

    if tail > 0:
        start_idx = max(0, total - tail)
        selected = list(enumerate(lines[start_idx:], start=start_idx + 1))
    elif grep_pattern:
        selected = [(i + 1, line) for i, line in enumerate(lines) if grep_pattern.lower() in line.lower()]
        if not selected:
            return f"# {path.relative_to(agent.root)} ({total} lines)\n(no matches for '{grep_pattern}')"
        if len(selected) > 2000:
            selected = selected[:2000]
    else:
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        if end - start + 1 > 2000:
            raise ValueError("max 2000 lines per call")
        selected = list(enumerate(lines[start - 1:end], start=start))

    body = "\n".join(f"{num:>4}: {line}" for num, line in selected)
    return f"# {path.relative_to(agent.root)} ({total} lines)\n{body}"


def tool_file_info(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    size = path.stat().st_size
    return f"lines: {len(lines)}, size: {size} bytes, path: {path.relative_to(agent.root)}"


def tool_append_file(agent, args):
    path = agent.path(args["path"])
    content = str(args["content"])
    if not path.is_file():
        raise ValueError("path is not a file — use write_file to create")
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)
    # Show last 5 lines so the model can see the appended content
    all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total_lines = len(all_lines)
    appended_lines = len(content.splitlines())
    tail_count = min(5, total_lines)
    tail = all_lines[-tail_count:]
    tail_preview = "\n".join(f"  {total_lines - tail_count + 1 + i}: {l}" for i, l in enumerate(tail))
    return f"SUCCESS: appended {appended_lines} lines to {path.relative_to(agent.root)} (now {total_lines} lines total)\nlast {tail_count} lines:\n{tail_preview}"


def tool_insert_at_line(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    line_no = int(args["line"])
    content = str(args["content"])
    if line_no < 1:
        raise ValueError("line must be >= 1")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if line_no > len(lines) + 1:
        raise ValueError(f"line {line_no} out of range (file has {len(lines)} lines)")
    insert_lines = content.split("\n")
    new_lines = lines[:line_no - 1] + insert_lines + lines[line_no - 1:]
    path.write_text("\n".join(new_lines), encoding="utf-8")
    # Show the inserted lines in context
    end_line = line_no + len(insert_lines)
    context_start = max(0, line_no - 2)
    context_end = min(len(new_lines), end_line + 1)
    context = "\n".join(f"  {context_start + 1 + i}: {l}" for i, l in enumerate(new_lines[context_start:context_end]))
    return f"SUCCESS: inserted {len(insert_lines)} lines at line {line_no} in {path.relative_to(agent.root)}\ncontext:\n{context}"


def tool_delete_line(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    line_no = int(args["line"])
    if line_no < 1:
        raise ValueError("line must be >= 1")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if line_no > len(lines):
        raise ValueError(f"line {line_no} out of range (file has {len(lines)} lines)")
    deleted = lines[line_no - 1]
    new_lines = lines[:line_no - 1] + lines[line_no:]
    path.write_text("\n".join(new_lines), encoding="utf-8")
    # Show before/after to prove the delete worked
    ctx_start = max(0, line_no - 2)
    before_ctx = lines[ctx_start:line_no + 1]
    after_ctx = new_lines[ctx_start:line_no + 1]
    before_str = "\n".join(f"  {ctx_start + 1 + i}: {l}" for i, l in enumerate(before_ctx))
    after_str = "\n".join(f"  {ctx_start + 1 + i}: {l}" for i, l in enumerate(after_ctx))
    return (
        f"SUCCESS: deleted line {line_no} from {path.relative_to(agent.root)}\n"
        f"deleted content: {deleted[:120]}\n"
        f"before ({len(lines)} lines):\n{before_str}\n"
        f"after ({len(new_lines)} lines):\n{after_str}\n"
        f"The file now has {len(new_lines)} lines. Line {line_no} has changed."
    )


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
    line_count = len(content.splitlines())
    return f"wrote {path.relative_to(agent.root)} ({line_count} lines, {len(content)} chars)"


def tool_patch_file(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    new_text = str(args["new_text"])
    nth = int(args.get("nth", 0))

    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)

    if nth == 0:
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    else:
        if nth < 1 or nth > count:
            raise ValueError(f"nth={nth} out of range, found {count} occurrences")
        idx = -1
        for _ in range(nth):
            idx = text.find(old_text, idx + 1)
        result = text[:idx] + new_text + text[idx + len(old_text):]
        path.write_text(result, encoding="utf-8")
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
            return f"未找到 {path.name} 对应的索引文件。read_entry 仅适用于笔记/错题文件，请改用 read_file(path={path.name}) 读取全文。"

    if not index_path.exists():
        return f"索引文件 {index_path.name} 不存在。请改用 read_file 读取全文。"

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
    try:
        raw = UserProfile._read_entry_at(path, target["offset"], target["length"])
    except (UnicodeDecodeError, ValueError):
        # 索引 offset 可能因 patch_file 等操作失效，降级为全文解析
        import re as _re
        content = path.read_text(encoding="utf-8", errors="replace")
        sections = _re.split(r'\n(?=## )', content)
        real_entries = [s for s in sections if s.strip().startswith("## ")]
        if entry_num > len(real_entries):
            return f"条目编号 {entry_num} 超出范围，共 {len(real_entries)} 条。"
        raw = real_entries[entry_num - 1]
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
            return f"未找到 {path.name} 对应的索引文件。delete_entry 仅适用于笔记/错题文件。"

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
    "file_info": tool_file_info,
    "read_file": tool_read_file,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "append_file": tool_append_file,
    "insert_at_line": tool_insert_at_line,
    "delete_line": tool_delete_line,
    "delete_file": tool_delete_file,
    "search_mistakes": tool_search_mistakes,
    "get_mistake_detail": tool_get_mistake_detail,
    "read_notes": tool_read_notes,
    "write_note": tool_write_note,
    "read_entry": tool_read_entry,
    "delete_entry": tool_delete_entry,
    "read_cache": tool_read_cache,
}
