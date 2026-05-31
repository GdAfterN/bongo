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
    /ask <问题>      选择文档类型后进入完整问答。
                    支持三种文档：信任路径、笔记、错题。
                    大模型在选定范围内自主调用工具。
                    示例：/ask 帮我总结最近的错题
                    示例：/ask 在 CC/README.md 末尾添加总结
                    示例：/ask 装饰器和闭包有什么区别

    查询功能：
    /note -天数      查询笔记（如 /note -1 最近1天，默认 -7）。
    /note del <关键词> 按关键词删除笔记。
    /mistake -天数   查询错题本（如 /mistake -1 最近1天，默认 -7）。
    /profile         显示学习档案摘要。
    /errors          显示按类型分组的错误历史。
    /progress        显示过去 7 天的学习进度。

    学习功能：
    /practice        进入练习模式（得分<60自动记入错题本）。
                    1. 快问快答：从最近笔记中出题
                    2. 深度求索：从信任路径中选择文档出题
                    3. 朝花夕拾：错题复习（答对移除，答错累加）

    用户管理：
    /user            显示当前用户和所有用户列表。
    /user <name>     切换到另一个用户。
    /user new <name> 创建新用户并切换。

    系统命令：
    /memory          显示代理的工作记忆。
    /session         显示会话文件路径。
    /reset           清空当前会话历史和记忆。
    /level           显示当前审批策略。
    /level [ask|auto|never]   切换审批策略。
    /help            显示此帮助信息。
    /exit            退出代理。
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
    runs_root = Path(work_dir) / ".bongo" / "runs"
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
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4

    def row(text):
        body = middle(text, inner)
        return f"| {body.ljust(inner)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    line = divider("=")
    rows = []
    # ASCII art
    for text in WELCOME_ART:
        rows.append(center(text))
    rows.append(center(""))
    rows.append(center(WELCOME_SUBTITLE))
    rows.append(divider("-"))
    rows.append(row(""))
    rows.append(row("bongo 是一个本地 AI 学习助手，帮你记笔记、练错题、读写文档。"))
    rows.append(row("数据全部存在本地 ~/.bongo/，两套独立链路："))
    rows.append(row("  /ask       ReAct 链路 — 大模型自主调用工具读写文件"))
    rows.append(row("  /practice  Plan-and-Execute 链路 — 自动出题、判分、记错题"))
    rows.append(row(""))
    rows.append(divider("-"))
    # 系统信息
    rows.append(row(""))
    rows.append(row(f"模型: {model}"))
    rows.append(row(f"工作目录: {middle(str(agent.work_dir), inner - 8)}"))
    rows.append(row(""))
    rows.append(divider("-"))
    # 快速引导
    rows.append(row(""))
    rows.append(center("快速开始"))
    rows.append(row("/ask <问题>    向大模型提问，自主调用工具"))
    rows.append(row("/practice      进入练习模式（快问快答 / 深度求索 / 朝花夕拾）"))
    rows.append(row("/note          查看和管理学习笔记"))
    rows.append(row("/mistake       查看错题本"))
    rows.append(row("/help          查看全部命令"))
    rows.append(row(""))
    rows.append(line)
    return "\n".join(rows)


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
            user_answer = input("你的答案 (输入 /q 提前退出): ").strip()
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
            user_answer = input("你的答案 (输入 /q 提前退出): ").strip()
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
                    question = input("/ask> ").strip()
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
                result = agent.ask(question)
                print(result)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
            question = None
            # 空输入展开上次 ReAct 过程
            has_steps = bool(getattr(agent, "_last_react_steps", []))
            while has_steps:
                try:
                    expand_input = input("/ask> ").strip()
                except (EOFError, KeyboardInterrupt):
                    expand_input = "/q"
                if expand_input == "":
                    agent.expand_last_steps()
                    has_steps = False
                else:
                    question = expand_input
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

    print(f"\n笔记列表（共 {len(index_entries)} 条）：")
    items = []
    for idx, entry in enumerate(index_entries, 1):
        ts = entry.get("timestamp", "")[:10]
        title = entry.get("title", "")
        item = {"label": title, "summary": ts, "file_path": str(user_profile.notes_file)}
        if entry.get("offset") is not None:
            item["offset"] = entry["offset"]
            item["length"] = entry["length"]
        items.append(item)
        print(f"  {idx}. [{ts}] {title}")

    agent.memory.populate_index(items)
    _ask_interactive_loop(agent, user_profile.notes_file.parent, "notes", first_question)


def _ask_with_mistakes(agent, user_profile, first_question=None):
    """错题模式：列出所有错题，填充 index（含 offset），进入交互循环。"""
    index_entries = user_profile.get_mistakes_index()
    if not index_entries:
        print("暂无错题。请先通过 /practice 或 MCP 记录错题。")
        return

    print(f"\n错题列表（共 {len(index_entries)} 条）：")
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
        print(f"  {idx}. [{ts}] 得分:{score} 次数:{count} {summary_text}")

    agent.memory.populate_index(items)
    _ask_interactive_loop(agent, user_profile.mistakes_file.parent, "mistakes", first_question)


def _ask_with_trusted_path(agent, user_profile, first_question=None):
    """信任路径模式：选择路径后列出文件，填充 index，进入交互循环。"""
    trusted = user_profile.get_trusted_paths()
    if not trusted:
        print("暂无信任路径。请先通过 /note 或 MCP 添加笔记关联文件。")
        return

    print("\n选择工作路径：")
    for idx, path in enumerate(trusted, 1):
        print(f"  {idx}. {path}")
    try:
        path_choice = input("\n选择编号: ").strip()
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

    print(f"\n{selected_path} 中的文件（共 {len(files)} 个）：")
    items = []
    for idx, f in enumerate(files, 1):
        rel = f.relative_to(scoped_root)
        size = f.stat().st_size
        label = str(rel)
        summary = f"{size}B"
        items.append({"label": label, "summary": summary})
        print(f"  {idx}. {rel} ({size}B)")

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
        return bongo.from_session(
            model_client=model,
            session_store=store,
            session_id=session_id,
            work_dir=work_dir,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=sorted(configured_secret_names),
        )
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


def main(argv=None):
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
                print(agent.ask(prompt))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    # 没有输入prompt，进入交互模式
    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        try:
            user_input = input(f"\n现在您要做什么？({current_username})> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue # 如果用户只按了回车，跳过本次循环。
        if user_input in {"/exit", "/quit", "-exit", "-quit"}:
            return 0
        if user_input in ("/help", "-help"):
            print(HELP_DETAILS)
            continue
        if user_input in ("/memory", "-memory"):
            print(agent.memory_text())
            continue
        if user_input in ("/session", "-session"):
            print(agent.session_path)
            continue
        if user_input in ("/reset", "-reset"):
            agent.reset()
            print("会话已重置")
            continue
        if user_input in ("/steps", "-steps"):
            agent.expand_last_steps()
            continue
        if user_input.startswith(("/level", "-level")):
            parts = user_input.split()
            if len(parts) == 1:
                print(f"审批策略: {agent.approval_policy}")
            elif parts[1] in ("ask", "auto", "never"):
                agent.approval_policy = parts[1]
                print(f"审批策略已设置为: {parts[1]}")
            else:
                print("用法: /level [ask|auto|never]")
            continue
        if user_input.startswith(("/user", "-user")):
            parts = user_input.split(maxsplit=2)
            if len(parts) == 1:
                # /user - 显示当前用户和所有用户列表
                users = list_profiles()
                print(f"当前用户: {current_username}")
                if users:
                    print("可用用户:")
                    for u in users:
                        marker = " *" if u == current_username else ""
                        print(f"  - {u}{marker}")
                else:
                    print("未找到用户档案。")
            elif parts[1] == "new":
                # /user new <name>
                if len(parts) < 3 or not parts[2].strip():
                    print("用法: /user new <name>")
                else:
                    new_name = parts[2].strip()
                    save_current_user(new_name)
                    current_username = new_name
                    user_profile = UserProfile(current_username)
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
                    print("用法: /note del <关键词>")
                    continue
                notes = user_profile.get_notes(limit=100)
                matched = [n for n in notes if keyword.lower() in n.get("title", "").lower()]
                if not matched:
                    print(f"未找到包含「{keyword}」的笔记。")
                elif len(matched) == 1:
                    if user_profile.delete_note(matched[0]["title"]):
                        print(f"已删除: {matched[0]['title']}")
                    else:
                        print("删除失败。")
                else:
                    print(f"找到 {len(matched)} 条匹配笔记：")
                    for idx, n in enumerate(matched, 1):
                        print(f"  {idx}. {n.get('title', '')}")
                    try:
                        choice = input("选择编号删除（回车取消）: ").strip()
                        if choice:
                            del_idx = int(choice) - 1
                            if user_profile.delete_note(matched[del_idx]["title"]):
                                print(f"已删除: {matched[del_idx]['title']}")
                            else:
                                print("删除失败。")
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
                    print("暂无笔记。")
                else:
                    from datetime import datetime, timedelta
                    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                    filtered = [n for n in notes if n.get("timestamp", "")[:10] >= cutoff]
                    if not filtered:
                        print(f"过去 {days} 天内无笔记。")
                    else:
                        print(f"笔记（过去 {days} 天，共 {len(filtered)} 条）：")
                        for idx, n in enumerate(filtered, 1):
                            ts = n.get("timestamp", "")[:10]
                            fp = f" [{n['file_path']}]" if n.get("file_path") else ""
                            print(f"  {idx}. {ts} {n.get('title', '')}{fp}")
                        # 交互：查看详情或删除
                        while True:
                            try:
                                cmd = input("\n输入编号查看详情，d <编号> 删除，/q 返回: ").strip()
                            except (EOFError, KeyboardInterrupt):
                                cmd = "/q"
                            if not cmd or cmd == "/q":
                                break
                            if cmd.startswith("d "):
                                try:
                                    del_idx = int(cmd[2:]) - 1
                                    del_note = filtered[del_idx]
                                    if user_profile.delete_note(del_note["title"]):
                                        print(f"已删除: {del_note['title']}")
                                        filtered.pop(del_idx)
                                    else:
                                        print("删除失败。")
                                except (ValueError, IndexError):
                                    print("无效编号。")
                            else:
                                try:
                                    view_idx = int(cmd) - 1
                                    n = filtered[view_idx]
                                    print(f"\n--- {n.get('title', '')} ---")
                                    print(f"时间: {n.get('timestamp', '')}")
                                    if n.get("file_path"):
                                        print(f"关联文件: {n['file_path']}")
                                    print(f"\n{n.get('content', '')}")
                                    print(f"---")
                                except (ValueError, IndexError):
                                    print("无效编号。")
            except Exception as exc:
                print(f"加载笔记失败: {exc}")
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
                    print(f"过去 {days} 天内无错题。")
                else:
                    print(f"错题本（过去 {days} 天，共 {len(mistakes)} 题）：")
                    for idx, m in enumerate(mistakes, 1):
                        ts = m.get("timestamp", "")[:10]
                        src = f" [{m['source']}]" if m.get("source") else ""
                        cnt = m.get("count", 1)
                        cnt_str = f" x{cnt}" if cnt > 1 else ""
                        print(f"  {idx}. {ts} 得分:{m['score']}{src}{cnt_str} {m.get('question', '')[:60]}")
                    # 交互：查看详情或删除
                    while True:
                        try:
                            cmd = input("\n输入编号查看详情，d <编号> 删除，/q 返回: ").strip()
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
                                print(f"已删除: {del_m.get('question', '')[:50]}")
                                mistakes.pop(del_idx)
                            except (ValueError, IndexError):
                                print("无效编号。")
                        else:
                            try:
                                view_idx = int(cmd) - 1
                                m = mistakes[view_idx]
                                print(f"\n--- {m.get('title', '')} ---")
                                print(f"时间: {m.get('timestamp', '')}")
                                if m.get("source"):
                                    print(f"来源: {m['source']}")
                                print(f"得分: {m.get('score', 0)}")
                                print(f"错误次数: {m.get('count', 1)}")
                                print(f"\n题目: {m.get('question', '')}")
                                print(f"回答: {m.get('user_answer', '')}")
                                if m.get("correct_answer"):
                                    print(f"正确答案: {m['correct_answer']}")
                                if m.get("feedback"):
                                    print(f"反馈: {m['feedback']}")
                                print(f"---")
                            except (ValueError, IndexError):
                                print("无效编号。")
            except Exception as exc:
                print(f"加载错题本失败: {exc}")
            continue
        if user_input in ("/progress", "-progress"):
            try:
                progress = user_profile.get_progress(days=7)
                print("学习进度（过去 7 天）：")
                for entry in progress:
                    d = entry["date"]
                    tasks = entry.get("tasks", 0)
                    mistakes = entry.get("mistakes_count", 0)
                    skills = entry.get("skills_used", [])
                    bar = "+" * tasks
                    line = f"  {d}: {tasks} 个任务 {bar}"
                    if mistakes:
                        line += f" ({mistakes} 个错误)"
                    if skills:
                        line += f" [{', '.join(skills)}]"
                    print(line)
                total = sum(e.get("tasks", 0) for e in progress)
                print(f"\n  总计: 7 天内 {total} 个任务")
            except Exception as exc:
                print(f"加载学习进度失败: {exc}")
            continue
        if user_input in ("/practice", "-practice"):
            # 练习模式
            print("\n=== 练习模式 ===")
            print("1. 快问快答（从最近笔记中抽取 10 个问题）")
            print("2. 深度求索（选择 md 文档，10 道题）")
            print("3. 朝花夕拾（错题复习）")
            print("h. 用法说明")
            print("0. 退出练习")
            try:
                choice = input("\n请选择模式 [1/2/3/h/0]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("")
                continue

            if choice == "1":
                # 快问快答 - Plan-and-Execute
                notes = user_profile.get_recent_notes_for_practice(limit=10)
                if not notes:
                    print("暂无可用笔记。请先保存一些笔记。")
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
                    path_choice = input("\n选择路径 [编号]: ").strip()
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
                    file_choice = input("\n选择文档 [编号]: ").strip()
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
                print("\n请选择文档类型：")
                print("  1. 信任路径（文件操作）")
                print("  2. 笔记（学习笔记）")
                print("  3. 错题（错题本）")
                print("  h. 用法说明")
                print("  /q 返回主菜单")
                try:
                    type_choice = input("\n选择编号: ").strip()
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
