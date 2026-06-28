"""bongo MCP Server.

作为 MCP server 被 Claude Code 调用，暴露学习伙伴工具：
- record_task: 记录完成的任务、知识话题、易错点
- add_note: 记录学习笔记和信任的文件路径
- get_profile: 获取用户画像摘要
- get_mistakes: 查询历史易错点
- get_progress: 获取学习进度统计
- user: 查看/切换/创建用户
"""

import json
from mcp.server.fastmcp import FastMCP
from .profile import UserProfile, load_current_user, save_current_user, list_profiles

mcp = FastMCP("bongo-learning-partner")

current_username = load_current_user()
profile = UserProfile(current_username)


@mcp.tool()
def record_task(
    task: str,
    topics: list[str] = None,
    mistakes: list[dict] = None,
    learnings: list[str] = None,
    files: list[str] = None,
    difficulty: int = 3,
) -> str:
    """Record a completed task and update user profile.

    Args:
        task: Description of the task completed
        topics: List of knowledge topics explored (e.g. ["binary_search", "recursion"])
        mistakes: List of mistakes made, each with type/desc/fix fields
        learnings: List of new things learned
        files: List of files modified
        difficulty: Task difficulty 1-5
    """
    profile.record_task(
        task=task,
        skills=topics or [],
        mistakes=mistakes or [],
        learnings=learnings or [],
        files=files or [],
        difficulty=difficulty,
    )

    # 记录话题到 frequent_topics
    if topics:
        profile.record_ask_topics(topics)

    similar_past = []
    if mistakes:
        past_mistakes = profile.data.get("mistakes", [])
        for m in mistakes:
            mtype = m.get("type", "")
            similar = [pm for pm in past_mistakes if pm.get("type") == mtype]
            if len(similar) > 1:
                similar_past.append(f"You've made '{mtype}' mistakes {len(similar)} times before. Watch out!")

    response = f"Task recorded: {task}"
    if topics:
        response += f"\nTopics: {', '.join(topics)}"
    if similar_past:
        response += "\n\nWarnings:\n" + "\n".join(similar_past)
    return response


@mcp.tool()
def get_profile() -> str:
    """获取用户档案摘要，包括技能水平和最近活动。"""
    return profile.get_profile_summary()


@mcp.tool()
def get_mistakes(limit: int = 20) -> str:
    """获取按类型分组的最近错误记录。

    Args:
        limit: 返回的最大错误组数
    """
    groups = profile.get_mistakes(limit=limit, group_by_type=True)
    if not groups:
        return "暂无错误记录。"

    lines = ["错误历史（按类型分组）："]
    for g in groups[:limit]:
        lines.append(f"\n[{g['type']}] x{g['count']}")
        for m in g.get("recent", []):
            lines.append(f"  - {m.get('desc', 'N/A')}")
            if m.get("fix"):
                lines.append(f"    修复: {m['fix']}")
    return "\n".join(lines)


@mcp.tool()
def get_progress(days: int = 7) -> str:
    """获取过去 N 天的学习进度。

    Args:
        days: 回溯的天数
    """
    progress = profile.get_progress(days=days)
    lines = [f"学习进度（过去 {days} 天）："]
    for entry in progress:
        d = entry["date"]
        tasks = entry.get("tasks", 0)
        mistakes = entry.get("mistakes_count", 0)
        bar = "+" * tasks
        line = f"  {d}: {tasks} 个任务 {bar}"
        if mistakes:
            line += f" ({mistakes} 个错误)"
        lines.append(line)

    total_tasks = sum(e.get("tasks", 0) for e in progress)
    total_mistakes = sum(e.get("mistakes_count", 0) for e in progress)
    lines.append(f"\n总计: {total_tasks} 个任务, {total_mistakes} 个错误")
    return "\n".join(lines)


@mcp.tool()
def get_mistakes_book(days: int = 7, limit: int = 20) -> str:
    """获取错题本内容（按时间倒序）。

    Args:
        days: 回溯的天数（默认7天）
        limit: 返回的最大错题数
    """
    mistakes = profile.get_mistakes_from_file(limit=limit, days=days)
    if not mistakes:
        return f"过去 {days} 天内无错题。"

    lines = [f"错题本（过去 {days} 天，共 {len(mistakes)} 题）："]
    for m in mistakes:
        ts = m.get("timestamp", "")[:10]
        src = f" [{m['source']}]" if m.get("source") else ""
        lines.append(f"\n  {ts} 得分:{m['score']}{src}")
        lines.append(f"    题目: {m.get('question', '')[:80]}")
        lines.append(f"    回答: {m.get('user_answer', '')[:80]}")
        if m.get("feedback"):
            lines.append(f"    反馈: {m['feedback'][:80]}")
    return "\n".join(lines)


@mcp.tool()
def add_note(content: str, file_path: str = "", title: str = "") -> str:
    """Record a learning note and optionally register a trusted file path.

    Call this when the user asks to save content to a markdown document.
    The note is saved to ~/.bongo/notes/{username}.md file.
    If file_path is provided, it will be added to trusted paths.

    Args:
        content: The note content to save
        file_path: Optional path to associate with this note (will be added to trusted paths)
        title: Optional title for the note (defaults to first 50 chars of content)
    """
    entry = profile.add_note(content, file_path=file_path, title=title)
    response = f"笔记已保存: {entry['title']}"
    response += f"\n存储位置: {profile.notes_file}"
    if file_path:
        response += f"\n关联文件: {file_path}"
        response += f"\n信任路径数量: {len(profile.get_trusted_paths())}"
    return response


@mcp.tool()
def user(action: str = "show", username: str = "") -> str:
    """Manage bongo learning partner users.

    Args:
        action: "show" to list users, "switch" to change user, "new" to create user
        username: Username for switch/new actions
    """
    global current_username, profile

    if action == "show":
        users = list_profiles()
        lines = [f"当前用户: {current_username}"]
        if users:
            lines.append("可用用户:")
            for u in users:
                marker = " *" if u == current_username else ""
                lines.append(f"  - {u}{marker}")
        return "\n".join(lines)

    if action == "new":
        if not username or not username.strip():
            return "错误: 需要用户名。用法: user(action='new', username='alice')"
        username = username.strip()
        save_current_user(username)
        current_username = username
        profile = UserProfile(username)
        return f"已创建并切换到用户: {username}\n\n{profile.get_profile_summary()}"

    if action == "switch":
        if not username or not username.strip():
            return "错误: 需要用户名。用法: user(action='switch', username='alice')"
        username = username.strip()
        users = list_profiles()
        if username not in users:
            return f"用户 '{username}' 不存在。使用 action='new' 创建。"
        save_current_user(username)
        current_username = username
        profile = UserProfile(username)
        return f"已切换到用户: {username}\n\n{profile.get_profile_summary()}"

    return "未知操作。可用操作: show, switch, new"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
