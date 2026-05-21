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

from .config import _config_path, load_config, save_config, load_tier_config
from .task_status import TaskStatus
from .models import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .runtime import bongo, SessionStore
from .tier_manager import TierManager
from .workspace import WorkspaceContext, middle

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
    " ___   ____  _   _  _____  ____ ",
    "| __| | ___|| \\ | ||  ___||  _ \\",
    "| _|  | ___||  \\| || |___ | |_| |",
    "|___| |____||_|\\_||\\_____||____/ ",
)
# ... existing code ...

WELCOME_NAME = "bongo"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help            Show this help message.
    /memory          Show the agent's distilled working memory.
    /session         Show the path to the saved session file.
    /reset           Clear the current session history and memory.
    /model           Show current model and tier status.
    /model [1|2|3]   Lock to a specific tier model.
    /model unlock    Unlock model, resume auto-routing.
    /exit            Exit the agent.
    """
).strip()

DEFAULT_OLLAMA_MODEL = "mimo"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "mimo"
DEFAULT_OPENAI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_ANTHROPIC_MODEL = "mimo"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"


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


def _build_tier_model_client(tier_config, default_args, global_config=None):
    """根据层级配置构建模型客户端。tier_config 是 {provider, model, base_url, api_key} 字典。"""
    if global_config is None:
        global_config = {}
    provider = tier_config.get("provider", "openai")
    model = tier_config.get("model", "") or global_config.get("model", "")
    base_url = tier_config.get("base_url", "")
    api_key = tier_config.get("api_key", "")
    temperature = getattr(default_args, "temperature", 0.2)
    timeout = getattr(default_args, "openai_timeout", getattr(default_args, "ollama_timeout", 300))

    if provider == "openai":
        if not base_url:
            base_url = DEFAULT_OPENAI_BASE_URL
        if not model:
            model = DEFAULT_OPENAI_MODEL
        return OpenAICompatibleModelClient(
            model=model, base_url=base_url, api_key=api_key,
            temperature=temperature, timeout=timeout,
        )
    if provider == "anthropic":
        if not base_url:
            base_url = DEFAULT_ANTHROPIC_BASE_URL
        if not model:
            model = DEFAULT_ANTHROPIC_MODEL
        return AnthropicCompatibleModelClient(
            model=model, base_url=base_url, api_key=api_key,
            temperature=temperature, timeout=timeout,
        )
    # ollama
    if not base_url:
        base_url = DEFAULT_OLLAMA_HOST
    if not model:
        model = DEFAULT_OLLAMA_MODEL
    return OllamaModelClient(
        model=model, host=base_url,
        temperature=temperature,
        top_p=getattr(default_args, "top_p", 0.9),
        timeout=getattr(default_args, "ollama_timeout", 300),
    )


def _build_tier_manager(args, default_client):
    """构建多层级模型管理器。优先使用 CLI 参数，其次使用持久化配置，最后回退到默认客户端。"""
    tier_configs = load_tier_config()
    global_config = load_config()
    tiers = {}
    for level in (1, 2, 3):
        # 检查 CLI 参数是否有该层级的配置
        cli_provider = getattr(args, f"tier{level}_provider", None)
        cli_model = getattr(args, f"tier{level}_model", None)
        if cli_provider or cli_model:
            tier_config = {
                "provider": cli_provider or "openai",
                "model": cli_model or "",
                "base_url": getattr(args, f"tier{level}_base_url", "") or "",
                "api_key": getattr(args, f"tier{level}_api_key", "") or "",
            }
        elif tier_configs.get(level):
            tier_config = tier_configs[level]
        else:
            # 没有配置，继承全局配置的 provider/api_key/base_url
            provider = getattr(args, "provider", "openai")
            if not getattr(args, "provider_set", False) and global_config.get("provider"):
                provider = global_config["provider"]
            tier_config = {
                "provider": provider,
                "model": "",
                "base_url": global_config.get("base_url", ""),
                "api_key": global_config.get("api_key", ""),
            }

        tiers[level] = _build_tier_model_client(tier_config, args, global_config)

    return TierManager(tiers[1], tiers[2], tiers[3])

def _handle_config(args):
    """处理 bongo config 子命令：--show 查看，其他参数保存。"""
    if args.show:
        config = load_config()
        if not config:
            print("No configuration saved yet.")
            return 0
        print("Saved configuration:")
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
        print("Nothing to save. Use --show to view, or pass --provider/--api-key/--base-url/--model to set.")
        return 0

    config = load_config()
    config.update(updates)
    save_config(config)
    print("Configuration saved to", _config_path())
    return 0


def _handle_status(args):
    """处理 bongo status 子命令：读取最新运行的状态。"""
    from pathlib import Path
    from .run_store import RunStore

    workspace = WorkspaceContext.build(getattr(args, "cwd", "."))
    runs_root = Path(workspace.repo_root) / ".bongo" / "runs"
    if not runs_root.is_dir():
        print("No runs found.")
        return 0

    run_dirs = sorted(p for p in runs_root.iterdir() if p.is_dir())
    if not run_dirs:
        print("No runs found.")
        return 0

    latest_run = run_dirs[-1]
    status_path = latest_run / "task_status.json"
    if not status_path.is_file():
        print(f"No task_status.json in latest run: {latest_run.name}")
        return 0

    status = TaskStatus.from_dict(json.loads(status_path.read_text(encoding="utf-8")))
    print(f"Run:      {status.run_id}")
    print(f"Request:  {status.user_request[:80]}")
    print(f"Status:   {status.status}")
    print(f"Action:   {status.current_action}")
    print(f"Rounds:   model={status.attempts}  tool={status.tool_steps}")
    if status.tools_called:
        print(f"Tools:    {', '.join(status.tools_called)}")
    if status.final_answer:
        print(f"Answer:   {status.final_answer[:120]}")
    if status.stop_reason:
        print(f"Stopped:  {status.stop_reason}")
    return 0


# 构建一个漂亮的，居中的欢迎面板
def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
        ]
    )
    # 显示多层级模型信息
    if agent.tier_manager is not None:
        rows.append(divider("-"))
        rows.append(center("MODEL TIERS"))
        for level in (1, 2, 3):
            tier_client = agent.tier_manager.get_model(level)
            tier_name = getattr(tier_client, "model", "unknown")
            tier_provider = type(tier_client).__name__.replace("ModelClient", "").replace("Compatible", "").lower()
            label = {1: "Tier1(easy)", 2: "Tier2(medium)", 3: "Tier3(hard)"}[level]
            rows.append(row(f"{label}: {tier_provider}/{tier_name}"))
    rows.append(row(""))
    return "\n".join([line, *rows, line])


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
    # 这里是 CLI 到 runtime 的装配点：
    # 先整理 secret 名单，再采集工作区快照，随后决定是恢复旧 session
    # 还是创建一个新的 bongo 实例。
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES) # 创建一个set集合对象，保存敏感环境变量信息
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    # 将命令行参数 --secret-env-name指定的额外环境变量名（通过 args.secret_env_names 传入）添加到集合中，并全部转为大写。
    extra_names = os.environ.get("bongo_SECRET_ENV_NAMES", "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    # 使用命令行参数 --cwd 指定的当前工作目录，调用 WorkspaceContext.build 方法创建一个 WorkspaceContext 对象。
    workspace = WorkspaceContext.build(args.cwd)
    store = SessionStore(workspace.repo_root + "/.bongo/sessions")
    model = _build_model_client(args)
    # 构建多层级模型管理器
    tier_manager = _build_tier_manager(args, model)
    # 获取命令行参数 --resume 指定的会话 ID。如果值为 "latest"，则调用 store.latest() 方法获取最近一次保存的会话 ID。
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return bongo.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=sorted(configured_secret_names),
            tier_manager=tier_manager,
        )
    return bongo(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=sorted(configured_secret_names),
        tier_manager=tier_manager,
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

    # 多层级模型配置参数
    for tier_level in (1, 2, 3):
        tier_group = parser.add_argument_group(f"tier{tier_level} model")
        tier_group.add_argument(f"--tier{tier_level}-provider", choices=("ollama", "openai", "anthropic"), default=None,
                                help=f"Provider for tier {tier_level} model.")
        tier_group.add_argument(f"--tier{tier_level}-model", default=None,
                                help=f"Model name for tier {tier_level}.")
        tier_group.add_argument(f"--tier{tier_level}-base-url", default=None,
                                help=f"API base URL for tier {tier_level}.")
        tier_group.add_argument(f"--tier{tier_level}-api-key", default=None,
                                help=f"API key for tier {tier_level}.")
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
    """
    什么是 REPL？
    REPL 是四个英文单词的缩写：
    Read（读取）- 读取用户输入
    Eval（求值）- 执行/计算输入的代码
    Print（打印）- 输出结果
    Loop（循环）- 继续等待下一次输入
    即后面while(true)里的操作
    """
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
            user_input = input("\nbongo> ").strip() # 等待用户持续输入。
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue # 如果用户只按了回车，跳过本次循环。
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue
        if user_input.startswith("/model"):
            parts = user_input.split()
            if len(parts) == 1:
                # /model - 显示当前状态
                print(agent.model_status())
            elif parts[1] == "unlock":
                print(agent.unlock_model())
            elif parts[1] in ("1", "2", "3"):
                level = int(parts[1])
                print(agent.lock_model(level))
            else:
                print("Usage: /model [1|2|3|unlock]")
            continue

        print()
        # 如果输入不是空的也不是内置命令，就在新行调用 agent.ask() 将用户输入发送给 AI，并打印返回结果。
        try:
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
