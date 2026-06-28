"""命令行入口。

这个模块负责把"用户怎么启动 bongo"翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

from .config import _config_path, load_config, save_config
from .task_status import TaskStatus
from .models import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .profile import UserProfile, save_current_user, load_current_user, list_profiles
from .runtime import bongo, SessionStore
from .utils import Spinner, middle
from .theme import console as C, STYLE_BANNER, STYLE_BANNER_SUB, STYLE_MUTED, STYLE_WARNING, STYLE_ERROR, STYLE_PROMPT, STYLE_SUCCESS
from rich.text import Text

import re as _re


def _styled_input(prompt_text="/ask> "):
    """带语法高亮的输入框：输入 /command 时实时变蓝。prompt_text 可以是字符串或 (style, text) 元组列表。"""
    try:
        import os, sys
        from prompt_toolkit import PromptSession
        from prompt_toolkit.lexers import Lexer
        from prompt_toolkit.styles import Style

        class _CmdLexer(Lexer):
            def lex_document(self, document):
                lines = document.lines
                def get_line(lineno):
                    text = lines[lineno]
                    if text.startswith("/"):
                        return [("class:cmd", text)]
                    return [("class:default", text)]
                return get_line

        _style = Style.from_dict({
            "cmd": "#00afff bold",      # 亮蓝
            "default": "",
            "prompt": "bold #00afff",   # 提示符蓝色
            "username": "bold yellow",  # 用户名黄色
        })

        # 支持 (style, text) 元组列表或纯字符串
        if isinstance(prompt_text, str):
            message = [("class:prompt", prompt_text)]
        else:
            message = prompt_text

        extra = {}
        # 非原生 Windows 终端（Git Bash / MSYS2 / Cygwin）：用 VT100 输出
        if os.name == "nt" and os.environ.get("TERM", ""):
            from prompt_toolkit.output.vt100 import Vt100_Output
            from prompt_toolkit.data_structures import Size
            extra["output"] = Vt100_Output(sys.stdout, get_size=lambda: Size(rows=40, columns=80))

        session = PromptSession(
            lexer=_CmdLexer(),
            style=_style,
            message=message,
            **extra,
        )
        return session.prompt()
    except Exception:
        if isinstance(prompt_text, str):
            return input(prompt_text)
        # 元组列表降级：拼成纯文本
        return input("".join(t[1] for t in prompt_text))


def _style_answer(text):
    """将模型输出中的 markdown 格式转为 ANSI 颜色，兼容 cmd.exe。"""
    cyan = "\033[36m"
    bold = "\033[1m"
    dim = "\033[2m"
    reset = "\033[0m"

    lines = text.split("\n")
    result = []
    for line in lines:
        # 标题行: ### / ## / #
        if _re.match(r'^#{1,3}\s', line):
            heading = _re.sub(r'^#{1,3}\s+', '', line)
            # 标题中的 **text** 也标色
            heading = _re.sub(r'\*\*(.+?)\*\*', f'{bold}{cyan}\\1{reset}{bold}', heading)
            result.append(f"{bold}{cyan}{heading}{reset}")
            continue
        # 分隔线
        if _re.match(r'^---+\s*$', line):
            result.append(f"{dim}{line}{reset}")
            continue
        # 普通行中的 **text** → 青色加粗
        styled = _re.sub(r'\*\*(.+?)\*\*', f'{bold}{cyan}\\1{reset}', line)
        result.append(styled)
    return "\n".join(result)

DEFAULT_SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

# ... existing code ...
WELCOME_ART = (
    " ██████╗  ██████╗ ███╗   ██╗ ██████╗  ██████╗ ",
    "██╔══██╗██╔═══██╗████╗  ██║██╔════╝ ██╔═══██╗",
    "██████╔╝██║   ██║██╔██╗ ██║██║  ███╗██║   ██║",
    "██╔══██╗██║   ██║██║╚██╗██║██║   ██║██║   ██║",
    "██║  ██║╚██████╔╝██║ ╚████║╚██████╔╝╚██████╔╝",
    "╚══════╝╚═════╝ ╚═╝  ╚═══╝ ╚═════╝  ╚═════╝ ",
)
WELCOME_SUBTITLE = "AI 辅助学习助手 · 记笔记 · 练错题 · 读写文档"
HELP_DETAILS = textwrap.dedent(
    """\
    命令（支持 / 或 - 前缀）：

    问答功能（ReAct 链路）：
    [cyan]/ask[/] <问题>      选择文档类型后进入完整问答。
                    支持三种文档：信任路径、笔记、错题。
                    大模型在选定范围内自主调用工具。
                    示例：[cyan]/ask[/] 帮我总结最近的错题
                    示例：[cyan]/ask[/] 在 CC/README.md 末尾添加总结
                    示例：[cyan]/ask[/] 装饰器和闭包有什么区别

    查询功能：
    [cyan]/note[/] -天数      查询笔记（如 [cyan]/note[/] -1 最近1天，默认 -7）。
    [cyan]/note del[/] <关键词> 按关键词删除笔记。
    [cyan]/mistake[/] -天数   查询错题本（如 [cyan]/mistake[/] -1 最近1天，默认 -7）。
    [cyan]/profile[/]         显示学习档案摘要。
    [cyan]/errors[/]          显示按类型分组的错误历史。
    [cyan]/progress[/]        显示过去 7 天的学习进度。

    学习功能：
    [cyan]/practice[/]        进入练习模式（得分<60自动记入错题本）。
                    1. 快问快答：从最近笔记中出题
                    2. 深度求索：从信任路径中选择文档出题
                    3. 朝花夕拾：错题复习（答对移除，答错累加）

    视频功能：
    [cyan]/video[/]           从信任路径选择技术文档，转换为视频演示。
                    工作流：文档→口播稿→大纲→React演示项目→音频→录制。

    用户管理：
    [cyan]/user[/]            显示当前用户和所有用户列表。
    [cyan]/user[/] <name>     切换到另一个用户。
    [cyan]/user new[/] <name> 创建新用户并切换。
    [cyan]/skills[/]          查看用户画像三要素并导出 skill。

    系统命令：
    [cyan]/memory[/]          显示代理的工作记忆。
    [cyan]/session[/]         显示会话文件路径。
    [cyan]/resume[/]          列出最近 10 条会话，选择恢复。
    [cyan]/resume[/] <id>     恢复指定会话。
    [cyan]/reset[/]           清空当前会话历史和记忆。
    [cyan]/level[/]           显示当前审批策略。
    [cyan]/level[/] [ask|auto|never]   切换审批策略。
    [cyan]/help[/]            显示此帮助信息。
    [cyan]/exit[/]            退出代理。
    """
).strip()

PRACTICE_HELP = textwrap.dedent(
    """\
    === /practice 用法说明 ===

    练习模式基于 Plan-and-Execute 链路，专注于出题、判分、记录错题。
    与 /ask 不同，练习模式不调用工具，走独立的上下文设计。

    三种模式：

    1. 快问快答
       从你最近保存的笔记中自动抽取内容，生成 10 道面试题。
       适合日常复习，检验对笔记内容的掌握程度。
       前提：至少有 1 条笔记（通过 /note 或 MCP 添加）。

    2. 深度求索
       从信任路径中选择一篇 md 文档，围绕其内容生成 10 道题。
       适合深入学习某个主题。
       前提：至少有 1 条信任路径（添加笔记时关联文件会自动建立）。

    3. 朝花夕拾
       从错题本中抽题复习。
       答对的题目自动从错题本移除，答错的累加错误次数。
       适合巩固薄弱知识点。

    评分规则：
    - 每题满分 100 分，低于 60 分自动记入错题本
    - 错题会关联来源（快问快答/深度求索）和标签
    - 练习结束后生成总评

    退出：输入 /q 可提前退出当前练习。
    """
).strip()

ASK_HELP = textwrap.dedent(
    """\
    === /ask 用法说明 ===

    /ask 基于 ReAct（观察→思考→行动→循环）链路，大模型自主调用工具完成任务。
    与 /practice 不同，/ask 可以读写文件、执行命令，是完整的 agent 交互。

    三种文档类型：

    1. 信任路径（文件操作）
       选择一个本地目录，agent 在该目录范围内进行文件读写操作。
       适合：修改代码、分析项目、批量处理文件。
       示例：/ask 帮我给所有 py 文件加上 type hints

    2. 笔记（学习笔记）
       agent 工作在 ~/.bongo/notes/ 下，可以查看、修改、补充笔记。
       适合：整理笔记、补充知识点、合并重复内容。
       示例：/ask 帮我补充装饰器的实际应用场景

    3. 错题（错题本）
       agent 工作在 ~/.bongo/mistakes/ 下，可以分析、整理错题。
       适合：分析错因、补充解题思路、清理已掌握的错题。
       示例：/ask 分析我最近的错题，找出薄弱知识点

    交互方式：
    - 选择文档类型后进入交互循环（/ask> 提示符）
    - 用自然语言描述需求，agent 自主决定调用什么工具
    - 可以用编号引用文档，如「读一下3号」「修改第5条」
    - 输入 /q 返回文档类型选择，再输入 /q 返回主菜单

    工具列表：
    - list_files: 列出目录文件
    - read_file: 按行号读文件
    - search: 搜索关键词
    - write_file: 写文件（需确认）
    - patch_file: 精确替换文本（需确认）
    - run_shell: 执行命令（需确认）
    - search_mistakes: 搜索错题索引
    - get_mistake_detail: 获取错题详情
    - read_notes: 读取学习笔记
    """
).strip()

DEFAULT_OLLAMA_MODEL = "mimo-v2.5-pro"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "mimo-v2.5-pro"
DEFAULT_OPENAI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_ANTHROPIC_MODEL = "mimo-v2.5-pro"
DEFAULT_ANTHROPIC_BASE_URL = "https://token-plan-cn.xiaomimimo.com/anthropic"


def _effective_model(args, provider, config=None):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 持久化配置文件
    # 4. 代码里的默认值
    if config is None:
        config = {}
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = os.environ.get("OPENAI_MODEL")
        if model:
            return model
        if config.get("model"):
            return config["model"]
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":  # Claude家的
        model = os.environ.get("ANTHROPIC_MODEL")
        if model:
            return model
        if config.get("model"):
            return config["model"]
        return DEFAULT_ANTHROPIC_MODEL
    model = config.get("model")
    if model:
        return model
    return DEFAULT_OLLAMA_MODEL


# *可以将多个参数传入，打包成一个元组
#查找有没有对应名字的环境
def _first_env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _build_model_client(args):
    provider = getattr(args, "provider", "openai")
    config = load_config()
    # 如果命令行没指定 provider，使用配置文件里的 provider
    if not getattr(args, "provider_set", False) and config.get("provider"):
        provider = config["provider"]
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "openai":
        model = _effective_model(args, provider, config)
        base_url = (
            getattr(args, "base_url", None)
            or os.environ.get("OPENAI_API_BASE")
            or config.get("base_url")
            or DEFAULT_OPENAI_BASE_URL
        )
        api_key = os.environ.get("OPENAI_API_KEY", "") or config.get("api_key", "")
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "anthropic":
        model = _effective_model(args, provider, config)
        base_url = (
            getattr(args, "base_url", None)
            or os.environ.get("ANTHROPIC_API_BASE")
            or config.get("base_url")
            or DEFAULT_ANTHROPIC_BASE_URL
        )
        api_key = _first_env("ANTHROPIC_API_KEY", "RIGHT_CODES_API_KEY", "OPENAI_API_KEY") or config.get("api_key", "")
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )

    model = _effective_model(args, provider, config)
    host = getattr(args, "host", None) or config.get("base_url") or DEFAULT_OLLAMA_HOST
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )



def _handle_config(args):
    """处理 bongo config 子命令：--show 查看，其他参数保存。"""
    if args.show:
        config = load_config()
        if not config:
            print("暂无保存的配置。")
            return 0
        print("已保存的配置：")
        for key, value in config.items():
            if key == "api_key" and value:
                masked = value[:4] + "****" + value[-4:] if len(value) > 8 else "****"
                print(f"  {key}: {masked}")
            else:
                print(f"  {key}: {value}")
        return 0

    # 收集要保存的字段
    updates = {}
    if args.provider:
        updates["provider"] = args.provider
    if args.api_key:
        updates["api_key"] = args.api_key
    if args.base_url:
        updates["base_url"] = args.base_url
    if args.model:
        updates["model"] = args.model

    if not updates:
        print("无可保存内容。使用 --show 查看，或传入 --provider/--api-key/--base-url/--model 设置。")
        return 0

    config = load_config()
    config.update(updates)
    save_config(config)
    print("配置已保存到", _config_path())
    return 0


def _handle_status(args):
    """处理 bongo status 子命令：读取最新运行的状态。"""
    from pathlib import Path
    from .run_store import RunStore

    work_dir = os.path.abspath(getattr(args, "cwd", "."))
    runs_root = Path(work_dir) / ".bongo" / "reports"
    if not runs_root.is_dir():
        print("未找到运行记录。")
        return 0

    run_dirs = sorted(p for p in runs_root.iterdir() if p.is_dir())
    if not run_dirs:
        print("未找到运行记录。")
        return 0

    latest_run = run_dirs[-1]
    status_path = latest_run / "task_status.json"
    if not status_path.is_file():
        print(f"最新运行中未找到 task_status.json: {latest_run.name}")
        return 0

    status = TaskStatus.from_dict(json.loads(status_path.read_text(encoding="utf-8")))
    print(f"运行ID:   {status.run_id}")
    print(f"请求:     {status.user_request[:80]}")
    print(f"状态:     {status.status}")
    print(f"动作:     {status.current_action}")
    print(f"轮次:     模型={status.attempts}  工具={status.tool_steps}")
    if status.tools_called:
        print(f"工具:     {', '.join(status.tools_called)}")
    if status.final_answer:
        print(f"回答:     {status.final_answer[:120]}")
    if status.stop_reason:
        print(f"停止原因: {status.stop_reason}")
    return 0


# 构建一个漂亮的，居中的欢迎面板
def build_welcome(agent, model, host):
    import io
    from rich.panel import Panel
    from rich.text import Text
    from rich.table import Table
    from rich import box

    # ASCII art
    art_text = "\n".join(WELCOME_ART)

    # 描述区
    desc = (
        "bongo 是一个本地 AI 学习助手，帮你记笔记、练错题、读写文档。\n"
        "数据全部存在本地 ~/.bongo/，两套独立链路：\n"
        "  /ask       ReAct 链路 — 大模型自主调用工具读写文件\n"
        "  /practice  Plan-and-Execute 链路 — 自动出题、判分、记错题"
    )

    # 系统信息表格
    info_table = Table(show_header=False, box=None, padding=(0, 2))
    info_table.add_column(style="dim")
    info_table.add_column(style="bold white")
    info_table.add_row("模型", str(model))
    info_table.add_row("工作目录", str(agent.work_dir))

    # 快速引导表格
    guide_table = Table(show_header=False, box=None, padding=(0, 2))
    guide_table.add_column(style="bold cyan")
    guide_table.add_column(style="white")
    guide_table.add_row("/ask <问题>", "向大模型提问，自主调用工具")
    guide_table.add_row("/practice", "进入练习模式（快问快答 / 深度求索 / 朝花夕拾）")
    guide_table.add_row("/note", "查看和管理学习笔记")
    guide_table.add_row("/mistake", "查看错题本")
    guide_table.add_row("/skills", "查看画像三要素，导出 skill")
    guide_table.add_row("/resume", "恢复上次会话")
    guide_table.add_row("/help", "查看全部命令")

    # 组装面板内容
    content = Text()
    content.append(art_text, style="bold cyan")
    content.append("\n\n")
    content.append(WELCOME_SUBTITLE, style="dim white")
    content.append("\n\n")
    content.append(desc, style="white")

    # 把表格渲染到字符串
    buf = io.StringIO()
    tmp = C.__class__(file=buf, force_terminal=True, color_system="truecolor")
    tmp.print()
    tmp.print(info_table)
    tmp.print()
    tmp.print(guide_table)
    content.append(buf.getvalue())

    panel = Panel(
        content,
        border_style="cyan",
        box=box.DOUBLE,
        width=min(max(68, shutil.get_terminal_size((80, 20)).columns), 84),
    )
    buf2 = io.StringIO()
    tmp2 = C.__class__(file=buf2, force_terminal=True, color_system="truecolor")
    tmp2.print(panel)
    return buf2.getvalue()


import re as _re
import json as _json

# === Practice 上下文：独立于 /ask 的 ReAct 链路 ===

GRADE_PROMPT_TEMPLATE = (
    "评分任务。输出格式：先写简短分析（不超过3行），然后写---，然后写四行结果。\n\n"
    "题目：{question}\n"
    "参考依据：{reference}\n"
    "回答：{user_answer}\n\n"
    "评分标准（严格执行）：\n"
    "- 0-39分：重大事实性错误或完全答非所问\n"
    "- 40-59分：明显事实性偏差，或多要点题目只覆盖了少数要点（如3点只答1点）\n"
    "- 60-69分：无事实错误，覆盖了大部分要点但不完整\n"
    "- 70-79分：无事实错误，覆盖全部要点且较完整\n"
    "- 80-89分：准确完整有条理\n"
    "- 90-100分：深入有见解\n\n"
    "关键规则：\n"
    "1. 多要点题目（如'分别解释A、B、C'）只回答了少数要点（不到一半），即使无事实错误也必须<=59\n"
    "2. 只有覆盖了大部分要点（超过一半）才能>=60\n"
    "3. 存在事实性错误必须<=59\n\n"
    "输出示例：\n"
    "回答未涉及题目内容，属于答非所问。\n"
    "---\n"
    "分数：10\n"
    "依据：回答完全未描述相关内容\n"
    "错误：答非所问，未提供有效信息\n"
    "答案：根据参考资料，正确答案是..."
)


def _clean_model_output(text):
    """剥离模型输出中的思考标签和代码块。"""
    text = _re.sub(r'<(think|reasoning|thought)[^>]*>.*?</\1>', '', text, flags=_re.DOTALL)
    text = _re.sub(r'<[^>]+>.*?</[^>]+>', '', text, flags=_re.DOTALL)
    text = _re.sub(r'^```\w*\n?', '', text, flags=_re.MULTILINE)
    text = _re.sub(r'^```\s*$', '', text, flags=_re.MULTILINE)
    return text.strip()


def _parse_grade_result(raw):
    """从评分输出中提取分数、依据、错误原因、正确答案。"""
    score = 50
    basis = "未提供"
    error_reason = "未提供"
    correct_answer = ""

    after_delim = raw
    if "---" in raw:
        parts = raw.split("---", 1)
        after_delim = parts[1] if len(parts) > 1 else raw
    lines = [l.strip() for l in after_delim.split("\n") if l.strip()]
    for line in lines:
        if _re.match(r'^分数[：:]', line):
            val = _re.sub(r'^分数[：:]\s*', '', line)
            nums = _re.findall(r'\d+', val)
            if nums:
                score = int(nums[0])
        elif _re.match(r'^依据[：:]', line):
            basis = _re.sub(r'^依据[：:]\s*', '', line).strip()
        elif _re.match(r'^错误[：:]', line):
            error_reason = _re.sub(r'^错误[：:]\s*', '', line).strip()
        elif _re.match(r'^答案[：:]', line):
            correct_answer = _re.sub(r'^答案[：:]\s*', '', line).strip()
    if score == 50:
        m = _re.search(r'分数[：:]\s*(\d{1,3})', raw)
        if m:
            score = int(m.group(1))
    return score, basis, error_reason, correct_answer


class PracticeContext:
    """练习模式的独立上下文，不依赖 agent 的 memory/session/history。

    只持有 model_client，每个阶段（出题/判分/总结）各自构建 prompt，
    直接调用 complete()，无状态、无工具调用。
    """

    def __init__(self, model_client, work_dir=None):
        self.model_client = model_client
        self.work_dir = work_dir

    def complete(self, prompt, max_tokens=4000, spinner_message=""):
        """调用模型并清理输出（剥离思考标签）。"""
        if spinner_message:
            with Spinner(spinner_message):
                raw = self.model_client.complete(prompt, max_tokens)
        else:
            raw = self.model_client.complete(prompt, max_tokens)
        return _clean_model_output(str(raw))

    def grade(self, question, user_answer, reference, spinner_message=""):
        """判分阶段：返回 (score, basis, error_reason, correct_answer)。"""
        prompt = GRADE_PROMPT_TEMPLATE.format(
            question=question,
            reference=reference[:2000],
            user_answer=user_answer,
        )
        try:
            raw = self.complete(prompt, 4000, spinner_message=spinner_message)
            return _parse_grade_result(raw)
        except Exception as exc:
            print(f"评分失败: {exc}")
            return 0, "评分异常", "", ""

    def summarize(self, source_label, scores, all_feedback):
        """总结阶段：生成总评。"""
        if not scores:
            return
        avg = sum(scores) / len(scores)
        print(f"\n{'='*50}")
        print(f"--- 平均分: {avg:.0f}/100 ---")
        print(f"{'='*50}")

        prompt = (
            f"你是面试官。根据以下表现给出总评。\n\n"
            f"测试类型：{source_label}\n"
            f"平均分：{avg:.0f}/100\n"
            f"各题得分：{', '.join(str(s) for s in scores)}\n"
            f"反馈：\n" + "\n".join(all_feedback) + "\n\n"
            f"用中文总评（150字内），包括：整体评价、主要优势、需加强方面。\n"
            f"只输出总评，不要前缀。"
        )
        try:
            result = self.complete(prompt, 2000, spinner_message="正在生成总评...")
            lines = [l.strip() for l in result.split("\n") if l.strip() and len(l.strip()) > 10]
            if lines:
                print(f"\n【总评】")
                print("\n".join(lines[-5:]))
            else:
                print(f"\n【总评】")
                print(result)
        except Exception as exc:
            print(f"\n总评生成失败: {exc}")
        print(f"{'='*50}")


# ── /video 工作流 ──────────────────────────────────────────────────────────────

VIDEO_SKILL_DIR = Path(__file__).parent / "skills" / "video-presentation"
VIDEO_THEMES_DIR = VIDEO_SKILL_DIR / "themes"


def _run_video_workflow(agent, user_profile, current_username):
    """从信任路径选择技术文档，转换为视频演示的工作流。"""
    from rich.panel import Panel
    from rich.table import Table

    # ── Step 1: 选择文档 ──
    trusted = user_profile.get_trusted_paths()
    if not trusted:
        print("暂无信任路径。请先使用 /ask 让大模型添加笔记时关联文件。")
        return

    C.print(Panel("[bold cyan]视频工作流[/] — 技术文档 → 视频演示", border_style="cyan"))
    print("\n信任路径：")
    for idx, path in enumerate(trusted, 1):
        print(f"  {idx}. {path}")

    try:
        path_choice = _styled_input("\n选择路径 [编号]: ").strip()
        path_idx = int(path_choice) - 1
        selected_path = trusted[path_idx]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        print("无效选择。")
        return

    p = Path(selected_path)
    md_files = []
    if p.is_file() and p.suffix.lower() == '.md':
        md_files.append(p)
    elif p.is_dir():
        md_files.extend(p.glob("**/*.md"))

    if not md_files:
        print("该路径下未找到 md 文件。")
        return

    print(f"\n{selected_path} 中的 md 文档：")
    for idx, f in enumerate(md_files, 1):
        print(f"  {idx}. {f.name}")

    try:
        file_choice = _styled_input("\n选择文档 [编号]: ").strip()
        file_idx = int(file_choice) - 1
        selected_file = md_files[file_idx]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        print("无效选择。")
        return

    try:
        article_content = selected_file.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"读取文件失败: {exc}")
        return

    # 创建工作目录
    work_dir = agent.work_dir / "my-video"
    work_dir.mkdir(exist_ok=True)
    (work_dir / "article.md").write_text(article_content, encoding="utf-8")
    print(f"\n工作目录: {work_dir}")
    print(f"文档: {selected_file.name} ({len(article_content)} 字)")

    # ── 状态管理（resume 机制）──
    state_file = work_dir / ".video-state.json"
    import json
    import subprocess

    # Windows 路径转为 Git Bash 兼容的 /d/ 前缀路径
    def _to_git_bash_path(p):
        """将 Windows 路径转为 Git Bash 的 /<drive>/ 前缀格式。"""
        s = str(p).replace("\\", "/")
        if len(s) >= 2 and s[1] == ':':
            return "/" + s[0].lower() + s[2:]
        return s

    def _find_git_bash():
        """查找 Git Bash 可执行文件路径。"""
        import shutil as _shutil
        for candidate in [
            "D:/Git/usr/bin/bash.exe",
            "C:/Program Files/Git/usr/bin/bash.exe",
        ]:
            if os.path.isfile(candidate):
                return candidate
        found = _shutil.which("bash")
        if found:
            return found
        raise FileNotFoundError("找不到 bash，请安装 Git for Windows")

    git_bash = _find_git_bash()

    def _load_state():
        if state_file.exists():
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_state(state):
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    state = _load_state()
    resuming = False

    # 检测是否有可恢复的状态
    if state.get("script_done"):
        completed = state.get("completed_chapters", [])
        total = state.get("total_chapters", 0)
        print(f"\n检测到未完成的工作流：")
        print(f"  script.md: ✓")
        print(f"  outline.md: {'✓' if state.get('outline_done') else '✗'}")
        print(f"  主题: {state.get('selected_theme', '未选择')}")
        print(f"  scaffold: {'✓' if state.get('scaffold_done') else '✗'}")
        print(f"  章节: {len(completed)}/{total} 已完成")
        resume_choice = _styled_input("\n从断点继续？[Y/n/取消]: ").strip().lower()
        if resume_choice == "n":
            state = {}
            state_file.unlink(missing_ok=True)
            print("  已清除旧状态，重新开始。")
        elif resume_choice in ("c", "取消"):
            print("已取消。")
            return
        else:
            resuming = True
            print("  从断点继续...")

    # 创建 LLM 调用上下文
    ctx = PracticeContext(agent.model_client, agent.work_dir)

    # 加载 reference 文档
    script_style = (VIDEO_SKILL_DIR / "references" / "SCRIPT-STYLE.md").read_text(encoding="utf-8")
    outline_format = (VIDEO_SKILL_DIR / "references" / "OUTLINE-FORMAT.md").read_text(encoding="utf-8")
    chapter_craft = (VIDEO_SKILL_DIR / "references" / "CHAPTER-CRAFT.md").read_text(encoding="utf-8")

    # ── Phase 1: 内容写作 ──
    if not resuming or not state.get("script_done"):
        print(f"\n{'='*50}")
        print("Phase 1: 内容写作")
        print(f"{'='*50}")

    # ── 生成 script.md ──
    if not resuming or not state.get("script_done"):
        # LLM 节点1: 生成 script.md
        script_prompt = f"""你是专业的视频口播稿作者。请根据以下风格指南，将用户的文章转换为口播稿。

## 风格指南
{script_style}

## 用户文章
{article_content[:8000]}

请直接输出口播稿内容（markdown 格式），不要添加任何前缀说明。"""
        print("\n[LLM 节点1] 正在生成口播稿...")
        script_content = ctx.complete(script_prompt, max_tokens=6000, spinner_message="生成 script.md...")
        (work_dir / "script.md").write_text(script_content, encoding="utf-8")
        print(f"✓ script.md 已生成 ({len(script_content)} 字)")

        # LLM 节点2: script 自检
        check_prompt = f"""请检查以下口播稿是否符合要求：
1. 信息保留度 ≥ 60%（与原文对比）
2. 口语化，无 AI 味
3. 短句为主（每句 ≤ 20 字）
4. 第二人称
5. 开头有钩子

口播稿：
{script_content[:4000]}

原文：
{article_content[:4000]}

如果存在问题，请指出并给出修改建议。如果合格，回复"合格"。"""
        print("[LLM 节点2] 正在自检口播稿...")
        check_result = ctx.complete(check_prompt, max_tokens=2000, spinner_message="自检 script.md...")
        if "合格" not in check_result:
            print(f"⚠ 口播稿需要修改：\n{check_result[:200]}")
            revise_prompt = f"""请根据以下修改建议，重新生成口播稿。

## 修改建议
{check_result}

## 风格指南
{script_style}

## 用户文章
{article_content[:8000]}

请直接输出修改后的口播稿内容（markdown 格式）。"""
            print("[LLM 节点1] 正在重新生成口播稿...")
            script_content = ctx.complete(revise_prompt, max_tokens=6000, spinner_message="重新生成 script.md...")
            (work_dir / "script.md").write_text(script_content, encoding="utf-8")
            print(f"✓ script.md 已重新生成 ({len(script_content)} 字)")

        state["script_done"] = True
        _save_state(state)
    else:
        script_content = (work_dir / "script.md").read_text(encoding="utf-8")
        print(f"\n[resume] script.md 已存在 ({len(script_content)} 字)，跳过")

    # ── 生成 outline.md ──
    if not resuming or not state.get("outline_done"):
        # LLM 节点3: 生成 outline.md
        outline_prompt = f"""你是专业的视频策划师。请根据以下格式规范，为口播稿生成章节大纲。

## 格式规范
{outline_format}

## 口播稿
{script_content[:6000]}

## 原文（用于信息池）
{article_content[:6000]}

请直接输出 outline.md 内容（markdown 格式），不要添加任何前缀说明。"""
        print("\n[LLM 节点3] 正在生成大纲...")
        outline_content = ctx.complete(outline_prompt, max_tokens=6000, spinner_message="生成 outline.md...")
        (work_dir / "outline.md").write_text(outline_content, encoding="utf-8")
        print(f"✓ outline.md 已生成 ({len(outline_content)} 字)")

        # LLM 节点4: outline 自检
        outline_check_prompt = f"""请检查以下大纲是否符合要求：
1. 包含 metadata block（主题、总时长、章节数）
2. 每章有信息池
3. 每章有开发计划（step 列表）
4. 每章有口播节选

大纲：
{outline_content[:4000]}

如果存在问题，请指出。如果合格，回复"合格"。"""
        print("[LLM 节点4] 正在自检大纲...")
        outline_check = ctx.complete(outline_check_prompt, max_tokens=2000, spinner_message="自检 outline.md...")
        if "合格" not in outline_check:
            print(f"⚠ 大纲需要修改：\n{outline_check[:200]}")
            revise_outline = f"""请根据以下修改建议，重新生成大纲。

## 修改建议
{outline_check}

## 格式规范
{outline_format}

## 口播稿
{script_content[:6000]}

## 原文（用于信息池）
{article_content[:6000]}

请直接输出修改后的 outline.md 内容（markdown 格式）。"""
            print("[LLM 节点3] 正在重新生成大纲...")
            outline_content = ctx.complete(revise_outline, max_tokens=6000, spinner_message="重新生成 outline.md...")
            (work_dir / "outline.md").write_text(outline_content, encoding="utf-8")
            print(f"✓ outline.md 已重新生成 ({len(outline_content)} 字)")

        state["outline_done"] = True
        _save_state(state)
    else:
        outline_content = (work_dir / "outline.md").read_text(encoding="utf-8")
        print(f"[resume] outline.md 已存在 ({len(outline_content)} 字)，跳过")

    # ── Checkpoint Plan: 用户确认 ──
    if not resuming or not state.get("checkpoint_done"):
        print(f"\n{'='*50}")
        print("Checkpoint Plan")
        print(f"{'='*50}")

        # 展示 script 摘要
        print("\n【口播稿摘要】")
        print(script_content[:500] + "..." if len(script_content) > 500 else script_content)

        # 展示 outline
        print("\n【大纲】")
        print(outline_content[:800] + "..." if len(outline_content) > 800 else outline_content)

        # 主题选择
        themes = [d.name for d in VIDEO_THEMES_DIR.iterdir() if d.is_dir()]
        themes.sort()
        print("\n【可选主题】")
        for idx, theme in enumerate(themes, 1):
            print(f"  {idx}. {theme}")

        try:
            theme_choice = _styled_input("\n选择主题 [编号] (默认 12-midnight-press): ").strip()
            if theme_choice:
                theme_idx = int(theme_choice) - 1
                selected_theme = themes[theme_idx]
            else:
                selected_theme = "midnight-press"
        except (ValueError, IndexError):
            selected_theme = "midnight-press"

        print(f"\n已选择主题: {selected_theme}")

        confirm = _styled_input("\n确认继续？[Y/n]: ").strip().lower()
        if confirm == "n":
            print("已取消。")
            return

        state["checkpoint_done"] = True
        state["selected_theme"] = selected_theme
        _save_state(state)
    else:
        selected_theme = state.get("selected_theme", "midnight-press")
        print(f"[resume] checkpoint 已完成，主题: {selected_theme}")

    # ── Phase 2: Web 开发 ──
    print(f"\n{'='*50}")
    print("Phase 2: Web 开发")
    print(f"{'='*50}")

    # 运行 scaffold.sh
    scaffold_script = VIDEO_SKILL_DIR / "scripts" / "scaffold.sh"
    presentation_dir = work_dir / "presentation"
    scaffold_script_bash = _to_git_bash_path(scaffold_script)

    # resume 时 scaffold 已完成则跳过
    skip_scaffold = False
    if resuming and state.get("scaffold_done") and presentation_dir.exists():
        print(f"[resume] scaffold 已完成，跳过")
        skip_scaffold = True
    elif presentation_dir.exists() and any(presentation_dir.iterdir()):
        overwrite = _styled_input(f"\n  {presentation_dir} 已存在，是否删除并重新创建？[y/N]: ").strip().lower()
        if overwrite == "y":
            import shutil
            shutil.rmtree(presentation_dir)
        else:
            print("  跳过 scaffold，使用已有项目。")
            skip_scaffold = True

    if not skip_scaffold:
        print(f"\n[固定步骤] 正在创建 Vite 项目...")
        print(f"  脚本: {scaffold_script_bash}")
        print(f"  工作目录: {work_dir}")

        # Linter 选择（原版 create-vite 的交互，翻译成中文）
        lint_choice = _styled_input("\n  选择代码检查工具 [1-Oxlint / 2-ESLint] (默认 1): ").strip()
        lint_flag = "--eslint" if lint_choice == "2" else "--no-eslint"

        try:
            # cwd 用 Windows 原始路径；脚本路径用 /d/ 格式给 Git Bash
            # 使用 login shell 确保 PATH 完整（dirname 等工具可用）
            # 实时输出让用户看到进度
            proc = subprocess.Popen(
                [git_bash, "-l", scaffold_script_bash, "presentation", f"--theme={selected_theme}", lint_flag],
                cwd=str(work_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in proc.stdout:
                try:
                    print(f"  {line}", end="")
                except UnicodeEncodeError:
                    print(line.encode(sys.stdout.encoding, errors="replace").decode(sys.stdout.encoding, errors="replace"), end="")
            proc.wait(timeout=300)
            if proc.returncode != 0:
                print(f"✗ scaffold 失败 (exit {proc.returncode})")
                return
            print(f"✓ Vite 项目已创建: {presentation_dir}")
            state["scaffold_done"] = True
            _save_state(state)
        except Exception as exc:
            print(f"✗ scaffold 执行异常: {exc}")
            return

    # 解析 outline 获取章节列表
    import re
    chapters = []
    for line in outline_content.split("\n"):
        m = re.match(r'^##\s+(\d+)\.\s+(\S+)\s+—\s+(.+?)(?:（|$)', line)
        if m:
            chapters.append({
                "num": int(m.group(1)),
                "id": m.group(2),
                "title": m.group(3).strip(),
            })

    if not chapters:
        print("⚠ 无法从大纲中解析章节，跳过章节实现。")
        return

    print(f"\n共 {len(chapters)} 个章节：")
    for ch in chapters:
        print(f"  {ch['num']}. {ch['id']} — {ch['title']}")

    # 读取主题 tokens
    tokens_css = ""
    tokens_path = VIDEO_THEMES_DIR / selected_theme / "tokens.css"
    if tokens_path.exists():
        tokens_css = tokens_path.read_text(encoding="utf-8")

    # 初始化已完成章节列表
    completed_chapters = state.get("completed_chapters", [])
    state["total_chapters"] = len(chapters)
    _save_state(state)

    # ── 实现所有章节（支持 resume 跳过已完成的）──
    for ch in chapters:
        ch_num = ch['num']
        ch_id = ch['id']

        # resume 时跳过已完成章节（但验证文件确实存在）
        if ch_id in completed_chapters:
            ch_dir_check = presentation_dir / "src" / "chapters" / f"{ch_num:02d}-{ch_id}"
            if ch_dir_check.exists() and list(ch_dir_check.glob("*.tsx")):
                print(f"[resume] 章节 {ch_num} ({ch_id}) 已完成，跳过")
                continue
            else:
                print(f"[resume] 章节 {ch_num} ({ch_id}) state 标记完成但文件缺失，重新生成")
                completed_chapters.remove(ch_id)

        ch_dir = presentation_dir / "src" / "chapters" / f"{ch_num:02d}-{ch_id}"
        ch_dir.mkdir(parents=True, exist_ok=True)

        # 第 1 章作为 style anchor，使用完整 outline；其余章节提取对应部分
        if ch_num == 1:
            ch_outline = outline_content[:2000]
        else:
            ch_outline = _extract_chapter_outline(outline_content, ch_num)

        chapter_prompt_n = f"""你是专业的 React 前端开发者。请根据以下指引，实现视频演示的第 {ch_num} 个章节。

## 章节开发指引
{chapter_craft[:4000]}

## 主题 CSS Tokens
{tokens_css[:2000]}

## 大纲（第 {ch_num} 章节）
{ch_outline}

## 口播稿
{script_content[:4000]}

## 原文（用于信息池）
{article_content[:4000]}

请生成以下文件：
1. {ch_id}.tsx - React 组件（使用 step 属性驱动逐步揭示）
2. {ch_id}.css - 样式文件（使用主题 token）
3. narrations.ts - 旁白数组（每个 step 一句旁白）

输出格式：
```tsx
// {ch_id}.tsx
{{代码}}
```

```css
// {ch_id}.css
{{代码}}
```

```ts
// narrations.ts
{{代码}}
```"""

        print(f"\n[LLM 节点] 正在实现章节 {ch_num}: {ch_id}...")
        ch_code = ctx.complete(chapter_prompt_n, max_tokens=8000, spinner_message=f"实现章节 {ch_id}...")
        _save_chapter_files(ch_code, ch_dir, ch_id)
        print(f"✓ 章节 {ch_id} 已实现")

        # 更新状态
        completed_chapters.append(ch_id)
        state["completed_chapters"] = completed_chapters
        _save_state(state)

        # 第 1 章完成后用户确认
        if ch_num == 1:
            confirm_ch1 = _styled_input("\n章节 1 已完成，确认继续？[Y/n]: ").strip().lower()
            if confirm_ch1 == "n":
                print("已取消。")
                return

    # ── Checkpoint Audio ──
    print(f"\n{'='*50}")
    print("Checkpoint Audio")
    print(f"{'='*50}")

    audio_choice = _styled_input("\n是否合成音频？[y/N]: ").strip().lower()
    if audio_choice == "y":
        # ── Phase 3: 音频合成 ──
        print(f"\n{'='*50}")
        print("Phase 3: 音频合成")
        print(f"{'='*50}")

        # 提取旁白
        print("\n[固定步骤] 正在提取旁白...")
        try:
            result = subprocess.run(
                "npx tsx scripts/extract-narrations.ts",
                cwd=str(presentation_dir),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60
            )
            if result.returncode == 0:
                print("✓ 旁白已提取")
            else:
                print(f"⚠ 提取旁白失败: {result.stderr}")
        except Exception as exc:
            print(f"⚠ 提取旁白异常: {exc}")

        # 合成音频
        tts_choice = _styled_input("\n选择 TTS 提供商 [1-minimax / 2-openai] (默认 1): ").strip()
        tts_provider = "openai" if tts_choice == "2" else "minimax"
        print(f"\n[固定步骤] 正在合成音频 (provider: {tts_provider})...")
        try:
            result = subprocess.run(
                [git_bash, "-l", "scripts/synthesize-audio.sh", f"--provider={tts_provider}"],
                cwd=str(presentation_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600
            )
            if result.returncode == 0:
                print("✓ 音频已合成")
            else:
                print(f"⚠ 合成音频失败: {result.stderr}")
        except Exception as exc:
            print(f"⚠ 合成音频异常: {exc}")

    # ── Phase 4: 启动开发服务器 ──
    print(f"\n{'='*50}")
    print("Phase 4: 启动开发服务器")
    print(f"{'='*50}")

    print(f"\n项目目录: {presentation_dir}")
    print("启动开发服务器...\n")

    try:
        proc = subprocess.Popen(
            "npm run dev",
            cwd=str(presentation_dir),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        detected_port = None
        for line in proc.stdout:
            print(line, end="")
            # 从 Vite 输出中提取实际端口
            if not detected_port and "localhost:" in line:
                import re
                m = re.search(r'localhost:(\d+)', line)
                if m:
                    detected_port = m.group(1)

        proc.wait()
    except KeyboardInterrupt:
        print("\n\n开发服务器已停止。")

    # 显示录制指引
    port = detected_port or "5173"
    print(f"\n{'='*50}")
    print("录制指引")
    print(f"{'='*50}")
    print(f"""
  1. 浏览器打开 http://localhost:{port}/
     - 点击舞台任意位置推进 step
     - 按 M 键切换播放模式

  2. 自动播放模式（推荐录制用）：
     http://localhost:{port}/?auto=1
     - 按 SPACE 开始自动播放
     - 音频与 step 自动同步

  3. 录制工具推荐：
     - OBS Studio（免费，推荐）
     - 浏览器自带录屏（F12 → Performance）

  4. 音频文件位置：
     {presentation_dir / 'public' / 'audio'}
""")


def _save_chapter_files(code_text, chapter_dir, chapter_id):
    """从 LLM 输出中解析并保存章节文件。"""
    import re

    saved = []

    # 解析 tsx 文件（只匹配 ```tsx，不匹配 ```ts）
    tsx_match = re.search(r'```tsx\s*\n(.*?)```', code_text, re.DOTALL)
    if tsx_match:
        (chapter_dir / f"{chapter_id}.tsx").write_text(tsx_match.group(1).strip(), encoding="utf-8")
        saved.append(f"{chapter_id}.tsx")

    # 解析 css 文件
    css_match = re.search(r'```css\s*\n(.*?)```', code_text, re.DOTALL)
    if css_match:
        (chapter_dir / f"{chapter_id}.css").write_text(css_match.group(1).strip(), encoding="utf-8")
        saved.append(f"{chapter_id}.css")

    # 解析 narrations.ts（匹配 ```ts 或 ```typescript）
    ts_match = re.search(r'```(?:ts|typescript)\s*\n(.*?)```', code_text, re.DOTALL)
    if ts_match:
        (chapter_dir / "narrations.ts").write_text(ts_match.group(1).strip(), encoding="utf-8")
        saved.append("narrations.ts")

    if not saved:
        print(f"  ⚠ 未能从 LLM 输出中解析出任何文件，尝试保存原始输出...")
        (chapter_dir / f"{chapter_id}_raw.txt").write_text(code_text, encoding="utf-8")
        print(f"  已保存原始输出到 {chapter_dir / f'{chapter_id}_raw.txt'}")
    elif len(saved) < 3:
        print(f"  ⚠ 只解析到: {', '.join(saved)}（期望 3 个文件）")


def _extract_chapter_outline(outline_content, chapter_num):
    """从 outline.md 中提取指定章节的内容。"""
    lines = outline_content.split("\n")
    in_chapter = False
    chapter_lines = []

    for line in lines:
        if line.startswith(f"## {chapter_num}."):
            in_chapter = True
            chapter_lines.append(line)
        elif in_chapter and line.startswith("## ") and not line.startswith(f"## {chapter_num}."):
            break
        elif in_chapter:
            chapter_lines.append(line)

    return "\n".join(chapter_lines) if chapter_lines else "（未找到该章节的 outline 内容）"


def _run_practice_review(ctx, user_profile):
    """朝花夕拾：从错题本中抽题复习。"""
    import random

    index = user_profile.get_mistakes_index()
    if not index:
        print("错题本为空，暂无题目。")
        return

    random.shuffle(index)
    index.sort(key=lambda e: e.get("count", 1), reverse=True)

    full_mistakes = user_profile.get_mistakes_from_file(limit=100)
    loaded = []
    for entry in index:
        summary = entry.get("summary", "")
        count = entry.get("count", 1)
        question_text = summary
        reference = ""
        for m in full_mistakes:
            m_title = m.get("title", "")
            if summary[:20] in m_title or m_title.startswith(summary[:20]):
                question_text = m.get("question", summary)
                if m.get("correct_answer"):
                    reference = m["correct_answer"]
                break
        loaded.append({
            "summary": summary,
            "count": count,
            "question": question_text,
            "reference": reference,
        })
    loaded = loaded[:10]

    if not loaded:
        print("错题本为空，暂无题目。")
        return

    print(f"\n--- 朝花夕拾（共 {len(loaded)} 题）---\n")
    print("输入 /q 可提前退出，请逐题作答，不要粘贴多行文字\n")

    scores = []
    all_feedback = []

    for i, item in enumerate(loaded, 1):
        summary = item["summary"]
        count = item["count"]
        question_text = item["question"]
        reference = item["reference"]

        print(f"问题 {i}/{len(loaded)} [错误次数:{count}]. {question_text}")
        try:
            user_answer = _styled_input("你的答案 (输入 /q 提前退出): ").strip()
        except (EOFError, KeyboardInterrupt):
            user_answer = "/q"

        if user_answer == "/q":
            ctx.summarize("朝花夕拾", scores, all_feedback)
            return

        score, basis, error_reason, correct_answer = ctx.grade(question_text, user_answer, reference, spinner_message="正在评分...")
        scores.append(score)
        all_feedback.append(f"Q{i}: {basis}")

        print(f"\n  得分: {score}/100")
        print(f"  打分依据: {basis}")
        print(f"  错误原因: {error_reason}")
        if correct_answer:
            print(f"  参考答案: {correct_answer}")

        if score >= 60:
            user_profile.delete_mistake(summary)
            print(f"  [已掌握，从错题本移除]")
        else:
            new_count = count + 1
            user_profile.update_mistake_count(summary, new_count)
            # 记录薄弱领域
            try:
                user_profile.record_practice_weak_area(summary[:20], score)
            except Exception:
                pass
            print(f"  [错误次数: {count} → {new_count}]")
        print()

    ctx.summarize("朝花夕拾", scores, all_feedback)


def _run_practice_plan_execute(ctx, user_profile, reference, source_label, num_questions=10, work_dir=None):
    """Plan-and-Execute 范式实现练习模式。

    Planner: 一次性生成所有题目（JSON 格式）
    Executor: 逐步展示题目、收集答案
    Grader: 每题严格评分
    Summarizer: 生成总评
    """

    # === Phase 1: Planner ===
    print(f"正在生成 {num_questions} 道题目...\n")
    plan_prompt = (
        f"你是一名技术面试官。请根据以下参考资料，一次性生成 {num_questions} 道面试题。\n\n"
        f"参考资料：\n{reference}\n\n"
        f"要求：\n"
        f"1. 所有题目用中文\n"
        f"2. 每道题干不超过150字\n"
        f"3. 题目要覆盖参考资料的不同部分\n"
        f"4. 测试对内容的深入理解\n\n"
        f"严格按以下 JSON 格式输出，不要输出任何其他内容：\n"
        f'{{"questions": ["题目1", "题目2", ...]}}'
    )
    try:
        raw = ctx.complete(plan_prompt, 8000, spinner_message=f"正在生成 {num_questions} 道题目...")
    except Exception as exc:
        print(f"生成题目失败：{type(exc).__name__}: {exc}")
        return

    # 解析 JSON（容错处理）
    questions = []
    try:
        data = _json.loads(raw.strip())
        questions = data.get("questions", [])
    except _json.JSONDecodeError:
        match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if match:
            try:
                data = _json.loads(match.group())
                questions = data.get("questions", [])
            except _json.JSONDecodeError:
                pass
    if not questions:
        questions = _re.findall(r'"([^"]{10,200})"', raw)
        questions = [q for q in questions if q not in ("questions",)]

    if not questions:
        print(f"题目生成失败（JSON解析错误），请重试。")
        print(f"  [DEBUG] 模型输出前200字: {raw[:200]}")
        if work_dir:
            try:
                debug_path = work_dir / ".bongo" / "debug_planner_output.txt"
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                debug_path.write_text(raw, encoding="utf-8")
                print(f"  [DEBUG] 完整输出已保存到: {debug_path}")
            except Exception:
                pass
        return

    # === Phase 2: Executor + Grader ===
    print("输入 /q 可提前退出，请逐题作答，不要粘贴多行文字\n")

    scores = []
    all_feedback = []

    for i, question in enumerate(questions, 1):
        print(f"问题 {i}/{len(questions)}. {question}")
        try:
            user_answer = _styled_input("你的答案 (输入 /q 提前退出): ").strip()
        except (EOFError, KeyboardInterrupt):
            user_answer = "/q"

        if user_answer == "/q":
            ctx.summarize(source_label, scores, all_feedback)
            return

        score, basis, error_reason, correct_answer = ctx.grade(question, user_answer, reference, spinner_message="正在评分...")
        scores.append(score)
        all_feedback.append(f"Q{i}: {basis}")

        print(f"\n  得分: {score}/100")
        print(f"  【评分与错误分析】")
        print(f"  打分依据: {basis}")
        print(f"  错误原因: {error_reason}")
        if correct_answer:
            print(f"  参考答案: {correct_answer}")

        # 检索关联错题（直接查 user_profile，不经过 agent.memory）
        related_index = user_profile.get_mistakes_index()
        if related_index:
            query_tokens = {t.lower() for t in _re.findall(r'[A-Za-z0-9_]+', question)}
            related = []
            for entry in related_index:
                entry_tokens = {t.lower() for t in _re.findall(r'[A-Za-z0-9_]+', entry.get("summary", ""))}
                entry_tokens |= {t.lower() for t in entry.get("tags", [])}
                if query_tokens & entry_tokens:
                    related.append(entry)
            related = related[:3]
            if related:
                print(f"  【关联错题】")
                for r in related:
                    print(f"  - [{r.get('timestamp', '')}] 得分:{r.get('score', 0)} {r.get('summary', '')}")

        if score < 60:
            user_profile.add_mistake(
                question=question,
                user_answer=user_answer,
                score=score,
                feedback=basis,
                correct_answer=correct_answer,
                source=source_label,
            )
            # 记录薄弱领域
            try:
                # 从问题中提取话题关键词
                import re as _re
                cn_words = _re.findall(r'[一-鿿]{2,6}', question)
                en_words = _re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', question)
                topic = (cn_words + en_words)[0] if (cn_words + en_words) else source_label
                user_profile.record_practice_weak_area(topic, score)
            except Exception:
                pass
            print(f"  [已记入错题本]")
        print()

    # === Phase 3: Summarizer ===
    ctx.summarize(source_label, scores, all_feedback)


def _ask_interactive_loop(agent, scoped_root, mode, first_question=None):
    """交互式 /ask 循环：用户可反复提问，agent 用 tool_use 链路处理。

    mode: "trust_path" / "notes" / "mistakes"
    """
    original_root = agent.root
    original_approval = agent.approval_policy
    agent.root = scoped_root
    agent.approval_policy = "auto"

    # 初始化 ask_mode 结构
    ask = agent.session["memory"].setdefault("ask_mode", {})
    ask["mode"] = mode
    ask["original_request"] = first_question or ""

    print(f"\n工作路径: {scoped_root}")
    print("输入问题引用文档编号，/q 返回主菜单\n")

    question = first_question
    try:
        while True:
            if not question:
                try:
                    question = _styled_input("/ask> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("")
                    break
                if not question:
                    continue
                if question == "/q":
                    break

            # 首次提问时记录 original_request
            if not ask.get("original_request"):
                ask["original_request"] = question

            try:
                tracker = getattr(agent, "token_tracker", None)
                if tracker:
                    tracker.reset()
                result = agent.ask(question)
                print(_style_answer(result))
                # 显示 token 用量（黄色）
                if tracker:
                    tok = tracker.display()
                    if tok:
                        print(f"\033[33mtokens: {tok}\033[0m")
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
            question = None
            # 空输入展开上次 ReAct 过程
            has_steps = bool(getattr(agent, "_last_react_steps", []))
            _quit = False
            while has_steps:
                try:
                    expand_input = _styled_input("/ask> ").strip()
                except (EOFError, KeyboardInterrupt):
                    expand_input = "/q"
                if expand_input == "/q":
                    _quit = True
                    break
                if expand_input == "":
                    agent.expand_last_steps()
                    has_steps = False
                else:
                    question = expand_input
                    break
            if _quit:
                break
    finally:
        agent.root = original_root
        agent.approval_policy = original_approval
        # 清理 ask_mode，避免影响后续非 /ask 操作
        agent.session["memory"].pop("ask_mode", None)


def _ask_with_notes(agent, user_profile, first_question=None):
    """笔记模式：列出所有笔记，填充 index（含 offset），进入交互循环。"""
    index_entries = user_profile._read_notes_index()
    if not index_entries:
        print("暂无笔记。请先通过 /note 或 MCP 添加笔记。")
        return

    C.print(f"\n笔记列表（共 [cyan]{len(index_entries)}[/] 条）：")
    items = []
    for idx, entry in enumerate(index_entries, 1):
        ts = entry.get("timestamp", "")[:10]
        title = entry.get("title", "")
        item = {"label": title, "summary": ts, "file_path": str(user_profile.notes_file)}
        if entry.get("offset") is not None:
            item["offset"] = entry["offset"]
            item["length"] = entry["length"]
        items.append(item)
        C.print(Text.assemble("  ", (str(idx), "cyan"), f". [{ts}] {title}"))

    agent.memory.populate_index(items)
    _ask_interactive_loop(agent, user_profile.notes_file.parent, "notes", first_question)


def _ask_with_mistakes(agent, user_profile, first_question=None):
    """错题模式：列出所有错题，填充 index（含 offset），进入交互循环。"""
    index_entries = user_profile.get_mistakes_index()
    if not index_entries:
        print("暂无错题。请先通过 /practice 或 MCP 记录错题。")
        return

    C.print(f"\n错题列表（共 [cyan]{len(index_entries)}[/] 条）：")
    items = []
    for idx, entry in enumerate(index_entries, 1):
        ts = entry.get("timestamp", "")
        score = entry.get("score", 0)
        summary_text = entry.get("summary", "")[:40]
        count = entry.get("count", 1)
        item = {
            "label": summary_text,
            "summary": f"{ts} 得分:{score} 次数:{count}",
            "file_path": str(user_profile.mistakes_file),
        }
        if entry.get("offset") is not None:
            item["offset"] = entry["offset"]
            item["length"] = entry["length"]
        items.append(item)
        C.print(Text.assemble("  ", (str(idx), "cyan"), f". [{ts}] 得分:{score} 次数:{count} {summary_text}"))

    agent.memory.populate_index(items)
    _ask_interactive_loop(agent, user_profile.mistakes_file.parent, "mistakes", first_question)


def _ask_with_trusted_path(agent, user_profile, first_question=None):
    """信任路径模式：选择路径后列出文件，填充 index，进入交互循环。"""
    trusted = user_profile.get_trusted_paths()
    if not trusted:
        print("暂无信任路径。请先通过 /note 或 MCP 添加笔记关联文件。")
        return

    C.print("\n选择工作路径：")
    for idx, path in enumerate(trusted, 1):
        C.print(f"  [cyan]{idx}[/]. {path}")
    try:
        path_choice = _styled_input("\n选择编号: ").strip()
        path_idx = int(path_choice) - 1
        selected_path = trusted[path_idx]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        print("无效选择。")
        return

    p = Path(selected_path)
    if not p.exists():
        print(f"路径不存在: {selected_path}")
        return

    scoped_root = p if p.is_dir() else p.parent

    # 列出目录下所有文件
    files = []
    if p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.is_file():
                files.append(f)
    elif p.is_file():
        files.append(p)

    if not files:
        print(f"{selected_path} 下未找到文件。")
        return

    C.print(f"\n{selected_path} 中的文件（共 [cyan]{len(files)}[/] 个）：")
    items = []
    for idx, f in enumerate(files, 1):
        rel = f.relative_to(scoped_root)
        size = f.stat().st_size
        label = str(rel)
        summary = f"{size}B"
        items.append({"label": label, "summary": summary})
        C.print(f"  [cyan]{idx}[/]. {rel} ({size}B)")

    agent.memory.populate_index(items)
    _ask_interactive_loop(agent, scoped_root, "trust_path", first_question)


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 bongo 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把"启动参数"翻译成"agent 运行现场"。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `bongo`，或一个从旧 session 恢复出来的 `bongo`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get("bongo_SECRET_ENV_NAMES", "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    model = _build_model_client(args)
    work_dir = os.path.abspath(args.cwd)
    session_id = args.resume
    if session_id == "latest":
        store = SessionStore(os.path.join(work_dir, ".bongo", "sessions"))
        session_id = store.latest()
    if session_id:
        store = SessionStore(os.path.join(work_dir, ".bongo", "sessions"))
        agent = bongo.from_session(
            model_client=model,
            session_store=store,
            session_id=session_id,
            work_dir=work_dir,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=sorted(configured_secret_names),
        )
        if getattr(agent, "_recovery_context", None):
            rc = agent._recovery_context
            from rich.panel import Panel
            from rich.text import Text
            t = Text()
            t.append("Previous run was interrupted\n", style="bold yellow")
            t.append(f"Request: ", style="dim")
            t.append(f"{rc['user_request'][:80]}\n", style="white")
            t.append(f"Steps completed: ", style="dim")
            t.append(f"{rc['tool_steps']}\n", style="white")
            t.append(f"Last action: ", style="dim")
            t.append(f"{rc['current_action']}", style="white")
            C.print(Panel(t, title="[bold yellow]Recovery[/]", border_style="yellow", expand=False))
        if getattr(agent, "_drift_detected", None):
            dd = agent._drift_detected
            from rich.panel import Panel
            from rich.text import Text
            t = Text()
            t.append("Workspace has changed since last session\n", style="bold yellow")
            if dd.get("work_dir_changed"):
                t.append(f"Directory: ", style="dim")
                t.append(f"{dd['stored_work_dir']} -> {dd['actual_work_dir']}\n", style="white")
            if dd.get("workspace_files_changed"):
                t.append("Files changed (stale summaries cleared)", style="white")
            C.print(Panel(t, title="[bold yellow]Drift[/]", border_style="yellow", expand=False))
        return agent
    return bongo(
        model_client=model,
        work_dir=work_dir,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=sorted(configured_secret_names),
    )


# 在命令行解析器的方法里，我们添加了很多的参数
def build_arg_parser():
    # 创建一个命令行解析器实例
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for Ollama, OpenAI-compatible, or Anthropic-compatible models.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # config 子命令
    config_parser = subparsers.add_parser("config", help="Save or show persistent configuration.")
    config_parser.add_argument("--provider", choices=("ollama", "openai", "anthropic"), default=None,
                               help="Model backend to save.")
    config_parser.add_argument("--api-key", default=None, help="API key to save.")
    config_parser.add_argument("--base-url", default=None, help="API base URL to save.")
    config_parser.add_argument("--model", default=None, help="Model name to save.")
    config_parser.add_argument("--show", action="store_true", help="Show current saved configuration.")

    # status 子命令
    subparsers.add_parser("status", help="Show current agent run status.")

    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic"), default="openai",
                        help="Model backend to use.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to mimo for all providers (Ollama, OpenAI, Anthropic) unless overridden by env vars or config.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for openai or anthropic.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask",
                        help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument("--max-steps", type=int, default=20, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=2048, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")

    return parser


def _enable_ansi_windows():
    """启用 Windows 终端的 VT100 ANSI 转义码支持。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(-11)
        # 获取当前控制台模式
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def main(argv=None):
    if sys.platform == "win32":
        _enable_ansi_windows()
    args = build_arg_parser().parse_args(argv) # 当 argv=None 时，argparse.parse_args() 的行为如下：自动获取 sys.argv[1:]

    # 处理 config 子命令
    if args.command == "config":
        return _handle_config(args)

    # 处理 status 子命令
    if args.command == "status":
        return _handle_status(args)

    # 标记用户是否显式传了 --provider（区别于默认值）
    args.provider_set = any(
        arg in ("--provider",) or arg.startswith("--provider=")
        for arg in (argv if argv is not None else sys.argv[1:])
    )

    agent = build_agent(args)
    """
    sys.argv 是一个全局列表，由 Python 解释器自动填充，它永远存在于 sys 模块中。
    sys.argv = [程序名, 参数1, 参数2, 参数3, ...]
    索引位置:   [0]      [1]     [2]     [3]     ...
    """
    # 获取model，host，构建欢迎界面
    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host",
                   getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))

    # 启动时检测模型连通性
    print("\n检查模型连接...")
    try:
        agent.model_client.complete("hi", 10)
        model_name = getattr(agent.model_client, "model", "unknown")
        print(f"  {model_name}: 连接成功")
    except Exception as exc:
        print(f"  连接失败 ({type(exc).__name__}: {exc})")

    # 加载当前用户并展示 profile
    current_username = load_current_user()
    try:
        user_profile = UserProfile(current_username)
        # 加载错题索引到 memory
        mistakes_index = user_profile.get_mistakes_index()
        if mistakes_index:
            agent.memory.load_mistakes_index(mistakes_index)
            print(f"\n已加载 {len(mistakes_index)} 条错题索引")
        print()
        print(user_profile.get_profile_summary())
        # 显示最近 5 条笔记
        recent_notes = user_profile.get_notes(limit=5)
        if recent_notes:
            print("\n最近笔记：")
            for n in reversed(recent_notes):
                ts = n.get("timestamp", "")[:10]
                fp = f" [{n['file_path']}]" if n.get("file_path") else ""
                print(f"  {ts} {n.get('title', '')}{fp}")
        if user_profile.should_show_daily_summary():
            print()
            print(user_profile.get_daily_summary())
            user_profile.mark_summary_shown()
    except Exception:
        user_profile = UserProfile(current_username)

    # 如果用户在命令行输入了 --prompt 参数（例如 python -m bongo --prompt "帮我写个程序"）
    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                tracker = getattr(agent, "token_tracker", None)
                if tracker:
                    tracker.reset()
                print(_style_answer(agent.ask(prompt)))
                if tracker:
                    tok = tracker.display()
                    if tok:
                        print(f"\033[33mtokens: {tok}\033[0m")
            except RuntimeError as exc:
                C.print(f"[error]{exc}[/]", file=sys.stderr)
                return 1
        return 0

    # 没有输入prompt，进入交互模式
    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        try:
            user_input = _styled_input([
                ("class:prompt", "\n现在您要做什么？("),
                ("class:username", current_username),
                ("class:prompt", ")> "),
            ]).strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue # 如果用户只按了回车，跳过本次循环。
        if user_input in {"/exit", "/quit", "-exit", "-quit"}:
            return 0
        if user_input in ("/help", "-help"):
            from rich.panel import Panel
            from rich.syntax import Syntax
            C.print(Panel(HELP_DETAILS, title="[bold cyan]Bongo 命令[/]", border_style="cyan", expand=False))
            continue
        if user_input in ("/memory", "-memory"):
            print(agent.memory_text())
            continue
        if user_input in ("/session", "-session"):
            print(agent.session_path)
            continue
        if user_input in ("/reset", "-reset"):
            agent.reset()
            C.print("[success]会话已重置[/]")
            continue
        if user_input.startswith(("/resume", "-resume")):
            parts = user_input.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else ""
            store = agent.session_store

            # /resume <id> — 直接恢复指定会话
            if target and target != "latest":
                sid = target
            else:
                # /resume 或 /resume latest — 列出最近 10 条，让用户选
                sessions = store.list_recent(limit=10)
                if not sessions:
                    C.print("[muted]没有可恢复的会话。[/]")
                    continue
                C.print("\n[bold]最近会话：[/]")
                for i, s in enumerate(sessions, 1):
                    title = s["title"]
                    ts = s["created_at"]
                    C.print(Text.assemble("  ", (str(i), "bold cyan"), f". [{ts}] {title}"))
                print()
                pick = _styled_input("选择编号（直接回车恢复最近一条）: ").strip()
                if not pick:
                    idx = 0
                elif pick.isdigit() and 1 <= int(pick) <= len(sessions):
                    idx = int(pick) - 1
                else:
                    C.print("[error]无效选择。[/]")
                    continue
                sid = sessions[idx]["id"]

            try:
                session_data = store.load(sid)
            except Exception:
                C.print(f"[error]会话不存在: {sid}[/]")
                continue
            hist_len = len(session_data.get("history", []))
            work_dir = session_data.get("work_dir", "?")
            created = session_data.get("created_at", "?")[:19]
            # 提取标题
            title = ""
            for item in session_data.get("history", []):
                if item.get("role") == "user":
                    content = item.get("content", "")
                    if isinstance(content, str):
                        title = content[:60].replace("\n", " ")
                    break
            C.print(f"\n[bold]会话:[/] {title or sid}")
            C.print(f"  [dim]创建时间:[/] {created}")
            C.print(f"  [dim]工作目录:[/] {work_dir}")
            C.print(f"  [dim]历史记录:[/] {hist_len} 条")
            confirm = _styled_input("确认恢复？(y/N) ").strip().lower()
            if confirm not in ("y", "yes"):
                C.print("[muted]已取消。[/]")
                continue
            new_agent = bongo.from_session(
                model_client=agent.model_client,
                session_store=store,
                session_id=sid,
                work_dir=str(agent.work_dir),
                run_store=agent.run_store,
                approval_policy=agent.approval_policy,
                max_steps=agent.max_steps,
                max_new_tokens=agent.max_new_tokens,
                secret_env_names=sorted(agent.secret_env_names),
            )
            agent.__dict__.update(new_agent.__dict__)
            # Update REPL local variables to match restored agent
            current_username = load_current_user()
            try:
                user_profile = UserProfile(current_username)
                mistakes_index = user_profile.get_mistakes_index()
                if mistakes_index:
                    agent.memory.load_mistakes_index(mistakes_index)
            except Exception:
                pass
            if getattr(agent, "_recovery_context", None):
                rc = agent._recovery_context
                print(f"\n  [Recovery] 上次运行被中断:")
                print(f"    请求: {rc['user_request'][:80]}")
                print(f"    已完成: {rc['tool_steps']} 步")
            if getattr(agent, "_drift_detected", None):
                dd = agent._drift_detected
                print(f"\n  [Drift] 工作区已变更:")
                if dd.get("work_dir_changed"):
                    print(f"    目录: {dd['stored_work_dir']} -> {dd['actual_work_dir']}")
                if dd.get("workspace_files_changed"):
                    print(f"    文件已变更（过期摘要已清除）")
            # Show restored session state
            history = agent.session.get("history", [])
            print(f"\n{'='*60}")
            print(f"会话已恢复: {title or sid}")
            print(f"{'='*60}")

            # Replay full conversation history
            for item in history:
                role = item.get("role", "")
                content = item.get("content", "")
                compacted = item.get("compacted", False)

                if role == "user":
                    if isinstance(content, str):
                        print(f"\n[用户] {content}")

                elif role == "assistant":
                    if compacted:
                        # Compacted summary
                        print(f"\n--- 上下文压缩 ---")
                        print(content[:500] if isinstance(content, str) else "")
                        print(f"---")
                    elif isinstance(content, list):
                        # Structured content: tool_use + text blocks
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    print(f"\n[助手] {text}")
                            elif block.get("type") == "tool_use":
                                tool_name = block.get("name", "?")
                                tool_args = block.get("input", {})
                                args_str = json.dumps(tool_args, ensure_ascii=False, sort_keys=True)
                                if len(args_str) > 200:
                                    args_str = args_str[:200] + "..."
                                print(f"  [调用工具] {tool_name}({args_str})")
                    elif isinstance(content, str) and content.strip():
                        print(f"\n[助手] {content}")

                elif role == "tool":
                    tool_name = item.get("name", "")
                    if isinstance(content, str):
                        preview = content[:300].replace("\n", "\n    ")
                        if len(content) > 300:
                            preview += f"\n    ...({len(content)} 字符)"
                        print(f"  [工具结果:{tool_name}] {preview}")

                elif role == "system":
                    if isinstance(content, str):
                        print(f"\n[系统] {content[:200]}")

            print(f"\n{'='*60}")
            print(f"输入问题继续对话，/q 返回主菜单\n")
            # Enter interactive loop for resumed session
            while True:
                try:
                    q = _styled_input("/ask> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("")
                    break
                if not q:
                    continue
                if q == "/q":
                    break
                if q in ("/exit", "/quit"):
                    return 0
                try:
                    tracker = getattr(agent, "token_tracker", None)
                    if tracker:
                        tracker.reset()
                    result = agent.ask(q)
                    print(_style_answer(result))
                    if tracker:
                        tok = tracker.display()
                        if tok:
                            print(f"\033[33mtokens: {tok}\033[0m")
                except RuntimeError as exc:
                    print(str(exc), file=sys.stderr)
            continue
        if user_input in ("/steps", "-steps"):
            agent.expand_last_steps()
            continue
        if user_input.startswith(("/level", "-level")):
            parts = user_input.split()
            if len(parts) == 1:
                C.print(f"审批策略: [bold]{agent.approval_policy}[/]")
            elif parts[1] in ("ask", "auto", "never"):
                agent.approval_policy = parts[1]
                C.print(f"[success]审批策略已设置为: {parts[1]}[/]")
            else:
                C.print("[muted]用法: /level [ask|auto|never][/]")
            continue
        if user_input.startswith(("/user", "-user")):
            parts = user_input.split(maxsplit=2)
            if len(parts) == 1:
                # /user - 显示当前用户和所有用户列表
                users = list_profiles()
                C.print(f"当前用户: [bold]{current_username}[/]")
                if users:
                    C.print("可用用户:")
                    for u in users:
                        marker = " [bold]*[/]" if u == current_username else ""
                        C.print(f"  - {u}{marker}")
                else:
                    C.print("[muted]未找到用户档案。[/]")
            elif parts[1] == "new":
                # /user new <name>
                if len(parts) < 3 or not parts[2].strip():
                    print("用法: /user new <name>")
                else:
                    new_name = parts[2].strip()
                    save_current_user(new_name)
                    current_username = new_name
                    user_profile = UserProfile(current_username)
                    # 询问用户定位
                    try:
                        positioning = _styled_input([
                            ("class:prompt", "请描述您的用户定位（如 '一个 Java 领域的求职者'，可跳过）: "),
                        ]).strip()
                        if positioning:
                            user_profile.set_positioning(positioning)
                    except (EOFError, KeyboardInterrupt):
                        pass
                    print(f"已创建并切换到用户: {current_username}")
                    print(user_profile.get_profile_summary())
            else:
                # /user <name>
                target = parts[1].strip()
                users = list_profiles()
                if target not in users:
                    print(f"用户 '{target}' 不存在。使用 '/user new {target}' 创建。")
                else:
                    save_current_user(target)
                    current_username = target
                    user_profile = UserProfile(current_username)
                    print(f"已切换到用户: {current_username}")
                    print(user_profile.get_profile_summary())
            continue
        if user_input in ("/profile", "-profile"):
            try:
                print(user_profile.get_profile_summary())
            except Exception as exc:
                print(f"加载档案失败: {exc}")
            continue
        if user_input.startswith(("/skills", "-skills")):
            try:
                parts = user_input.split(maxsplit=1)
                if len(parts) > 1 and parts[1] == "export":
                    # /skills export - 导出 skill
                    from rich.panel import Panel
                    from rich.table import Table
                    C.print("[bold]正在导出 skill...[/]")
                    result = user_profile.export_as_skill(session_store=agent.session_store)
                    C.print(Panel(
                        f"[bold green]导出成功！[/]\n\n"
                        f"输出目录: {result['output_dir']}\n"
                        f"文件数: {len(result['files'])}\n\n"
                        f"[bold]画像摘要:[/]\n"
                        f"  用户定位: {result['summary'].get('positioning', '未设置')}\n"
                        f"  常聊话题: {', '.join(result['summary'].get('frequent_topics', [])) or '暂无'}\n"
                        f"  薄弱领域: {', '.join(result['summary'].get('weak_areas', [])) or '暂无'}\n"
                        f"  用户偏好: {result['summary'].get('preference_type', '数据积累中')}\n"
                        f"  笔记数: {result['summary'].get('notes_count', 0)}\n"
                        f"  错题数: {result['summary'].get('mistakes_count', 0)}\n"
                        f"  Session数: {result['summary'].get('sessions_count', 0)}",
                        title="[bold cyan]Skill 导出结果[/]",
                        border_style="cyan",
                    ))
                else:
                    # /skills - 查看画像三要素
                    from rich.table import Table
                    from rich.panel import Panel

                    positioning = user_profile.data.get("user_positioning", "")
                    frequent_topics = user_profile.data.get("frequent_topics", [])
                    weak_areas = user_profile.data.get("weak_areas", [])
                    pref = user_profile.data.get("user_preference", {})
                    preference_type = pref.get("preference_type", "")
                    ask_count = pref.get("ask_count", 0)
                    practice_count = pref.get("practice_count", 0)

                    C.print(Panel(
                        f"[bold]{current_username}[/] 的学习画像\n"
                        f"用户定位: {positioning or '[muted]未设置[/]'}",
                        title="[bold cyan]用户画像[/]",
                        border_style="cyan",
                    ))

                    table = Table(title="画像三要素", show_header=True, header_style="bold")
                    table.add_column("维度", style="cyan")
                    table.add_column("内容")
                    table.add_row("常聊话题", ", ".join(frequent_topics) or "[muted]暂无[/]")
                    table.add_row("薄弱领域", ", ".join(weak_areas) or "[muted]暂无[/]")
                    table.add_row("用户偏好", preference_type or "[muted]数据积累中[/]")
                    table.add_row("使用统计", f"/ask {ask_count} 次, /practice {practice_count} 次")
                    C.print(table)

                    C.print("\n[dim]输入 [cyan]/skills export[/] 导出为可复用的 skill 目录[/]")
            except Exception as exc:
                print(f"操作失败: {exc}")
            continue
        if user_input in ("/mistakes", "-mistakes", "/errors", "-errors"):
            try:
                groups = user_profile.get_mistakes()
                if not groups:
                    print("暂无错误记录。")
                else:
                    print("错误历史（按类型分组）：")
                    for g in groups:
                        print(f"\n  [{g['type']}] x{g['count']}")
                        for m in g.get("recent", []):
                            print(f"    - {m.get('desc', 'N/A')}")
                            if m.get("fix"):
                                print(f"      修复: {m['fix']}")
            except Exception as exc:
                print(f"加载错误历史失败: {exc}")
            continue
        if user_input.startswith(("/note", "-note")):
            parts = user_input.split(maxsplit=2)
            days = 7
            # /note del <关键词> - 按关键词删除
            if len(parts) > 1 and parts[1] == "del":
                keyword = parts[2].strip() if len(parts) > 2 else ""
                if not keyword:
                    C.print("[muted]用法: /note del <关键词>[/]")
                    continue
                notes = user_profile.get_notes(limit=100)
                matched = [n for n in notes if keyword.lower() in n.get("title", "").lower()]
                if not matched:
                    C.print(f"[muted]未找到包含「{keyword}」的笔记。[/]")
                elif len(matched) == 1:
                    if user_profile.delete_note(matched[0]["title"]):
                        C.print(f"[success]已删除: {matched[0]['title']}[/]")
                    else:
                        C.print("[error]删除失败。[/]")
                else:
                    C.print(f"找到 [bold]{len(matched)}[/] 条匹配笔记：")
                    for idx, n in enumerate(matched, 1):
                        C.print(f"  [bold cyan]{idx}[/]. {n.get('title', '')}")
                    try:
                        choice = _styled_input("选择编号删除（回车取消）: ").strip()
                        if choice:
                            del_idx = int(choice) - 1
                            if user_profile.delete_note(matched[del_idx]["title"]):
                                C.print(f"[success]已删除: {matched[del_idx]['title']}[/]")
                            else:
                                C.print("[error]删除失败。[/]")
                    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
                        pass
                continue
            # /note -天数 - 按天数查询
            if len(parts) > 1:
                try:
                    val = int(parts[1])
                    if val < 0:
                        days = abs(val)
                    else:
                        print("用法: /note -天数（如 /note -1 表示最近1天）")
                        continue
                except ValueError:
                    print("用法: /note -天数（如 /note -7 表示最近7天）")
                    continue
            try:
                notes = user_profile.get_notes(limit=100)
                if not notes:
                    C.print("[muted]暂无笔记。[/]")
                else:
                    from datetime import datetime, timedelta
                    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                    filtered = [n for n in notes if n.get("timestamp", "")[:10] >= cutoff]
                    if not filtered:
                        C.print(f"[muted]过去 {days} 天内无笔记。[/]")
                    else:
                        C.print(f"[bold]笔记[/]（过去 {days} 天，共 [bold]{len(filtered)}[/] 条）：")
                        for idx, n in enumerate(filtered, 1):
                            ts = n.get("timestamp", "")[:10]
                            fp = f" [dim][{n['file_path']}][/]" if n.get("file_path") else ""
                            C.print(f"  [bold cyan]{idx}[/]. {ts} {n.get('title', '')}{fp}")
                        # 交互：查看详情或删除
                        while True:
                            try:
                                cmd = _styled_input("\n输入编号查看详情，d <编号> 删除，/q 返回: ").strip()
                            except (EOFError, KeyboardInterrupt):
                                cmd = "/q"
                            if not cmd or cmd == "/q":
                                break
                            if cmd.startswith("d "):
                                try:
                                    del_idx = int(cmd[2:]) - 1
                                    del_note = filtered[del_idx]
                                    if user_profile.delete_note(del_note["title"]):
                                        C.print(f"[success]已删除: {del_note['title']}[/]")
                                        filtered.pop(del_idx)
                                    else:
                                        C.print("[error]删除失败。[/]")
                                except (ValueError, IndexError):
                                    C.print("[error]无效编号。[/]")
                            else:
                                try:
                                    view_idx = int(cmd) - 1
                                    n = filtered[view_idx]
                                    from rich.panel import Panel
                                    detail = f"[dim]时间:[/] {n.get('timestamp', '')}\n"
                                    if n.get("file_path"):
                                        detail += f"[dim]关联文件:[/] {n['file_path']}\n"
                                    detail += f"\n{n.get('content', '')}"
                                    C.print(Panel(detail, title=f"[bold]{n.get('title', '')}[/]", border_style="cyan", expand=False))
                                except (ValueError, IndexError):
                                    C.print("[error]无效编号。[/]")
            except Exception as exc:
                C.print(f"[error]加载笔记失败: {exc}[/]")
            continue
        if user_input.startswith(("/mistake", "-mistake")):
            parts = user_input.split()
            days = 7
            if len(parts) > 1:
                try:
                    val = int(parts[1])
                    if val < 0:
                        days = abs(val)
                    else:
                        print("用法: /mistake -天数（如 /mistake -1 表示最近1天）")
                        continue
                except ValueError:
                    print("用法: /mistake -天数（如 /mistake -7 表示最近7天）")
                    continue
            try:
                mistakes = user_profile.get_mistakes_from_file(limit=50, days=days)
                if not mistakes:
                    C.print(f"[muted]过去 {days} 天内无错题。[/]")
                else:
                    C.print(f"[bold]错题本[/]（过去 {days} 天，共 [bold]{len(mistakes)}[/] 题）：")
                    for idx, m in enumerate(mistakes, 1):
                        ts = m.get("timestamp", "")[:10]
                        src = f" [dim][{m['source']}][/]" if m.get("source") else ""
                        cnt = m.get("count", 1)
                        cnt_str = f" [yellow]x{cnt}[/]" if cnt > 1 else ""
                        C.print(f"  [bold cyan]{idx}[/]. {ts} 得分:{m['score']}{src}{cnt_str} {m.get('question', '')[:60]}")
                    # 交互：查看详情或删除
                    while True:
                        try:
                            cmd = _styled_input("\n输入编号查看详情，d <编号> 删除，/q 返回: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            cmd = "/q"
                        if not cmd or cmd == "/q":
                            break
                        if cmd.startswith("d "):
                            try:
                                del_idx = int(cmd[2:]) - 1
                                del_m = mistakes[del_idx]
                                user_profile.delete_mistake(del_m.get("title", ""))
                                agent.memory.load_mistakes_index(user_profile.get_mistakes_index())
                                C.print(f"[success]已删除: {del_m.get('question', '')[:50]}[/]")
                                mistakes.pop(del_idx)
                            except (ValueError, IndexError):
                                C.print("[error]无效编号。[/]")
                        else:
                            try:
                                view_idx = int(cmd) - 1
                                m = mistakes[view_idx]
                                from rich.panel import Panel
                                detail = f"[dim]时间:[/] {m.get('timestamp', '')}\n"
                                if m.get("source"):
                                    detail += f"[dim]来源:[/] {m['source']}\n"
                                detail += f"[dim]得分:[/] {m.get('score', 0)}  [dim]错误次数:[/] {m.get('count', 1)}\n"
                                detail += f"\n[bold]题目:[/] {m.get('question', '')}\n"
                                detail += f"[bold]回答:[/] {m.get('user_answer', '')}\n"
                                if m.get("correct_answer"):
                                    detail += f"[green]正确答案:[/] {m['correct_answer']}\n"
                                if m.get("feedback"):
                                    detail += f"[yellow]反馈:[/] {m['feedback']}"
                                C.print(Panel(detail, title=f"[bold]{m.get('title', '')}[/]", border_style="cyan", expand=False))
                            except (ValueError, IndexError):
                                C.print("[error]无效编号。[/]")
            except Exception as exc:
                C.print(f"[error]加载错题本失败: {exc}[/]")
            continue
        if user_input in ("/progress", "-progress"):
            try:
                progress = user_profile.get_progress(days=7)
                print("学习进度（过去 7 天）：")
                for entry in progress:
                    d = entry["date"]
                    tasks = entry.get("tasks", 0)
                    mistakes = entry.get("mistakes_count", 0)
                    bar = "+" * tasks
                    line = f"  {d}: {tasks} 个任务 {bar}"
                    if mistakes:
                        line += f" ({mistakes} 个错误)"
                    print(line)
                total = sum(e.get("tasks", 0) for e in progress)
                print(f"\n  总计: 7 天内 {total} 个任务")
            except Exception as exc:
                print(f"加载学习进度失败: {exc}")
            continue
        if user_input.startswith(("/video", "-video")):
            try:
                _run_video_workflow(agent, user_profile, current_username)
            except Exception as exc:
                print(f"视频工作流失败: {exc}")
            continue
        if user_input in ("/practice", "-practice"):
            # 练习模式
            from rich.panel import Panel
            from rich.table import Table
            practice_menu = Table(show_header=False, box=None, padding=(0, 2))
            practice_menu.add_column(style="bold cyan", justify="right")
            practice_menu.add_column(style="white")
            practice_menu.add_row("1", "快问快答（从最近笔记中抽取 10 个问题）")
            practice_menu.add_row("2", "深度求索（选择 md 文档，10 道题）")
            practice_menu.add_row("3", "朝花夕拾（错题复习）")
            practice_menu.add_row("h", "用法说明")
            practice_menu.add_row("0", "退出练习")
            C.print(Panel(practice_menu, title="[bold cyan]练习模式[/]", border_style="cyan", expand=False))
            try:
                choice = _styled_input("\n请选择模式 [1/2/3/h/0]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("")
                continue

            if choice == "1":
                # 快问快答 - Plan-and-Execute
                notes = user_profile.get_recent_notes_for_practice(limit=10)
                if not notes:
                    C.print("[muted]暂无可用笔记。请先保存一些笔记。[/]")
                    continue
                ref_parts = []
                for n in notes:
                    ref_parts.append(f"【{n.get('title', '')}】\n{n.get('content', '')}")
                reference = "\n\n".join(ref_parts)
                ctx = PracticeContext(agent.model_client, agent.work_dir)
                _run_practice_plan_execute(
                    ctx, user_profile, reference, "快问快答", len(notes), agent.work_dir
                )

            elif choice == "2":
                # 深度求索 - Plan-and-Execute
                trusted = user_profile.get_trusted_paths()
                if not trusted:
                    print("暂无信任路径。请先使用 /ask 让大模型添加笔记时关联文件。")
                    continue

                print(f"\n信任路径：")
                for idx, path in enumerate(trusted, 1):
                    print(f"  {idx}. {path}")
                try:
                    path_choice = _styled_input("\n选择路径 [编号]: ").strip()
                    path_idx = int(path_choice) - 1
                    selected_path = trusted[path_idx]
                except (ValueError, IndexError, EOFError, KeyboardInterrupt):
                    print("无效选择。")
                    continue

                p = Path(selected_path)
                md_files = []
                if p.is_file() and p.suffix.lower() == '.md':
                    md_files.append(p)
                elif p.is_dir():
                    md_files.extend(p.glob("**/*.md"))

                if not md_files:
                    print("该路径下未找到 md 文件。")
                    continue

                print(f"\n{selected_path} 中的 md 文档：")
                for idx, f in enumerate(md_files, 1):
                    print(f"  {idx}. {f.name}")
                try:
                    file_choice = _styled_input("\n选择文档 [编号]: ").strip()
                    file_idx = int(file_choice) - 1
                    selected_file = md_files[file_idx]
                except (ValueError, IndexError, EOFError, KeyboardInterrupt):
                    print("无效选择。")
                    continue

                try:
                    content = selected_file.read_text(encoding="utf-8")
                except Exception as exc:
                    print(f"读取文件失败: {exc}")
                    continue

                print(f"\n--- 深度求索: {selected_file.name} ---\n")
                ctx = PracticeContext(agent.model_client, agent.work_dir)
                _run_practice_plan_execute(
                    ctx, user_profile, content[:3000], f"深度求索 - {selected_file.name}", 10, agent.work_dir
                )

            elif choice == "3":
                # 朝花夕拾 - 错题复习
                ctx = PracticeContext(agent.model_client, agent.work_dir)
                _run_practice_review(ctx, user_profile)
            elif choice in ("h", "H", "help"):
                print(PRACTICE_HELP)
            continue

        # /ask 命令：选择文档类型后进入交互式 ReAct 问答
        if user_input.startswith(("/ask", "-ask")):
            first_question = user_input[4:].strip() if user_input.startswith("/ask") else user_input[5:].strip()

            # 文档类型选择循环：/q 返回主菜单
            while True:
                C.print("\n请选择文档类型：")
                C.print("  [cyan]1[/]. 信任路径（文件操作）")
                C.print("  [cyan]2[/]. 笔记（学习笔记）")
                C.print("  [cyan]3[/]. 错题（错题本）")
                C.print("  [cyan]h[/]. 用法说明")
                C.print("  [cyan]/q[/] 返回主菜单")
                try:
                    type_choice = _styled_input("\n选择编号: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("")
                    type_choice = "/q"

                if type_choice == "/q":
                    break
                elif type_choice in ("h", "H", "help"):
                    print(ASK_HELP)
                elif type_choice == "1":
                    _ask_with_trusted_path(agent, user_profile, first_question or None)
                elif type_choice == "2":
                    _ask_with_notes(agent, user_profile, first_question or None)
                elif type_choice == "3":
                    _ask_with_mistakes(agent, user_profile, first_question or None)
                else:
                    print("无效选择。")
                first_question = None
            continue

        print("无当前命令，输入 /help 查看可用命令")
