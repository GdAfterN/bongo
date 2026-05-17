# Pico (Bongo) 模块详解 — 各模块做什么、怎么关联

> 本文档从"一个用户请求从进入到返回"的视角，逐模块拆解每个文件的职责、核心实现和模块间依赖关系。

---

## 目录

- [全局视角：一句话总结每个模块](#全局视角一句话总结每个模块)
- [模块依赖关系图](#模块依赖关系图)
- [启动链路：从 `python -m bongo` 到等待用户输入](#启动链路)
- [请求链路：从用户输入到返回答案](#请求链路)
- [逐模块详解](#逐模块详解)
  - [1. `__init__.py` — 公共 API 导出](#1-__init__py)
  - [2. `__main__.py` — 入口胶水](#2-__main__py)
  - [3. `cli.py` — 命令行解析与 Agent 装配](#3-clipy)
  - [4. `runtime.py` — 核心控制循环](#4-runtimepy)
  - [5. `models.py` — 模型后端适配层](#5-modelspy)
  - [6. `tools.py` — 工具定义与执行](#6-toolspy)
  - [7. `context_manager.py` — Prompt 组装与预算裁剪](#7-context_managerpy)
  - [8. `memory.py` — 分层记忆系统](#8-memorypy)
  - [9. `workspace.py` — 工作区快照](#9-workspacepy)
  - [10. `task_state.py` — 运行状态机](#10-task_statepy)
  - [11. `run_store.py` — 运行工件落盘](#11-run_storepy)
  - [12. `evaluator.py` — Benchmark 评测](#12-evaluatorpy)
  - [13. `metrics.py` — 实验与指标聚合](#13-metricspy)
- [模块间数据流全景](#模块间数据流全景)

---

## 全局视角：一句话总结每个模块

| 模块 | 一句话 |
|---|---|
| `__init__.py` | 对外暴露的 API 清单 |
| `__main__.py` | `python -m bongo` 的入口，转发到 cli.main |
| `cli.py` | 解析命令行参数，组装出一个可运行的 bongo 实例 |
| `runtime.py` | **核心**：控制循环（组 prompt → 调模型 → 执行工具 → 记录），管理会话状态 |
| `models.py` | 把 Ollama / OpenAI / Anthropic 三种 HTTP API 抹平成统一的 `complete()` |
| `tools.py` | 定义 7 个工具的参数 schema、校验逻辑和具体实现 |
| `context_manager.py` | 把 prefix + memory + history + request 拼成 prompt，超预算时裁剪 |
| `memory.py` | 三层记忆：工作记忆、情景笔记、文件摘要（带 freshness 校验） |
| `workspace.py` | 采集 git 事实、项目文档，生成工作区快照和指纹 |
| `task_state.py` | 单次 `ask()` 的状态机（running → completed/stopped/failed） |
| `run_store.py` | 把 task_state / trace / report 写到磁盘（原子写入） |
| `evaluator.py` | 用 FakeModelClient 跑固定 benchmark，验证 harness 合同 |
| `metrics.py` | 聚合运行工件、跑 ablation 实验、生成简历指标报告 |

---

## 模块依赖关系图

```
                        cli.py
                       ╱  │  ╲
                      ╱   │   ╲
            models.py  workspace.py  runtime.py
                          │        ╱  │  ╲  ╲
                          │       ╱   │   ╲  ╲
                     context_manager.py │  run_store.py
                          │        │   │      │
                       memory.py   │   │   task_state.py
                                   │   │
                               tools.py
                                   │
                               workspace.py  (path() 校验)


        evaluator.py ──→ runtime.py + models.py(Fake) + workspace.py + run_store.py

        metrics.py ──→ evaluator.py + runtime.py + models.py + memory.py + workspace.py
```

**读法**：箭头表示"依赖"。比如 `runtime.py` 依赖 `context_manager.py`、`memory.py`、`tools.py`、`run_store.py`、`task_state.py`、`workspace.py`。

---

## 启动链路

从终端敲 `python -m bongo` 到等待用户输入，经过 4 个文件：

```
__main__.py
  └─→ cli.main()
        ├─ build_arg_parser()        解析 --provider, --model, --cwd, --resume ...
        ├─ _build_model_client()     根据 provider 选 OllamaModelClient / OpenAI / Anthropic
        ├─ build_agent()
        │    ├─ WorkspaceContext.build(args.cwd)   采集 git 事实
        │    ├─ SessionStore(...)                   创建会话存储
        │    ├─ bongo.from_session()  或  bongo()   创建 agent 实例
        │    │    ├─ build_tool_registry()          注册 7 个工具
        │    │    ├─ build_prefix()                 组装系统提示词
        │    │    ├─ ContextManager(self)           创建 prompt 组装器
        │    │    └─ SessionStore.save()            持久化初始 session
        │    └─ return agent
        ├─ build_welcome()           打印欢迎面板
        └─ 进入 REPL 循环 (while True: input → agent.ask())
```

### 关键代码走读

**`__main__.py:4-5`** — 入口只做一件事：转发。

```python
from .cli import main
if __name__ == "__main__":
    raise SystemExit(main())
```

**`cli.py:268-270`** — main() 先解析参数，再装配 agent：

```python
def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)
```

**`cli.py:173-229`** — build_agent() 是 CLI 到 runtime 的唯一装配点：

```python
def build_agent(args):
    workspace = WorkspaceContext.build(args.cwd)        # ① 采集工作区
    store = SessionStore(workspace.repo_root + "/.bongo/sessions")
    model = _build_model_client(args)                   # ② 选模型后端
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return bongo.from_session(...)                  # ③a 恢复旧 session
    return bongo(...)                                   # ③b 创建新 session
```

---

## 请求链路

用户输入一句话到返回答案，核心路径只经过 `runtime.py` 的 `ask()` 方法，但它会调用几乎所有模块：

```
user_input
  │
  ▼
bongo.ask(user_message)                          runtime.py:450
  │
  ├─ memory.set_task_summary(user_message)       memory.py
  ├─ record({"role":"user", ...})                runtime.py:308 → SessionStore.save()
  ├─ TaskState.create(...)                       task_state.py
  ├─ run_store.start_run(task_state)             run_store.py → 创建 .bongo/runs/<id>/
  ├─ emit_trace("run_started")                   runtime.py:405
  │
  ├─ WHILE tool_steps < max_steps:               ── 主循环开始 ──
  │    │
  │    ├─ _build_prompt_and_metadata(user_message)
  │    │    ├─ refresh_prefix()                   检查 workspace 是否变化
  │    │    │    └─ WorkspaceContext.build()       workspace.py
  │    │    └─ ContextManager.build(user_message)  context_manager.py
  │    │         ├─ memory.retrieval_candidates()  memory.py — 召回相关笔记
  │    │         ├─ _render_sections()             按预算渲染各 section
  │    │         │    ├─ _render_relevant_memory() 裁剪相关记忆
  │    │         │    └─ _render_history_section() 裁剪历史（近期保护）
  │    │         └─ _assemble_prompt()             拼接 5 个 section
  │    │
  │    ├─ emit_trace("prompt_built")
  │    ├─ model_client.complete(prompt, ...)       models.py — 发 HTTP 请求
  │    ├─ bongo.parse(raw)                         runtime.py:786 — 解析 <tool>/<final>
  │    ├─ emit_trace("model_parsed")
  │    │
  │    ├─ IF kind == "tool":
  │    │    ├─ run_tool(name, args)                runtime.py:626
  │    │    │    ├─ tools.validate_tool()          tools.py:100 — 参数校验
  │    │    │    ├─ repeated_tool_call()           runtime.py:705 — 重复检测
  │    │    │    ├─ approve()                      runtime.py:772 — 审批门控
  │    │    │    ├─ tool["run"](args)              tools.py — 实际执行
  │    │    │    │    └─ agent.path()              runtime.py:912 — 路径沙箱校验
  │    │    │    ├─ clip(result)                   workspace.py:26 — 截断输出
  │    │    │    └─ update_memory_after_tool()     runtime.py:413 — 更新记忆
  │    │    │         ├─ memory.remember_file()    memory.py
  │    │    │         ├─ memory.set_file_summary() memory.py
  │    │    │         └─ memory.append_note()      memory.py
  │    │    ├─ record({"role":"tool", ...})
  │    │    ├─ emit_trace("tool_executed")
  │    │    └─ continue (回到循环顶部)
  │    │
  │    ├─ IF kind == "retry":
  │    │    ├─ record({"role":"assistant", "retry notice"})
  │    │    └─ continue
  │    │
  │    └─ IF kind == "final":
  │         ├─ record({"role":"assistant", final})
  │         ├─ task_state.finish_success()
  │         ├─ emit_trace("run_finished")
  │         ├─ write_report()
  │         └─ RETURN final
  │
  └─ (步数/重试耗尽 → 返回停止原因)
```

---

## 逐模块详解

---

### 1. `__init__.py`

**文件**：`bongo/__init__.py`

**做什么**：定义包的公共 API。外部代码 `from bongo import bongo` 走的就是这里。

**导出了什么**：

```python
__all__ = [
    "bongo", "MiniAgent",          # 核心 agent 类
    "SessionStore",                 # 会话存储
    "WorkspaceContext",             # 工作区快照
    "build_agent", "main",          # CLI 入口
    "OllamaModelClient",            # 3 类模型客户端
    "OpenAICompatibleModelClient",
    "AnthropicCompatibleModelClient",
    "FakeModelClient",
]
```

**和谁关联**：它只是 re-export，不包含逻辑。依赖 `cli.py`、`runtime.py`、`models.py`、`workspace.py`。

---

### 2. `__main__.py`

**文件**：`bongo/__main__.py`

**做什么**：让 `python -m bongo` 能跑。只有 3 行。

```python
from .cli import main
if __name__ == "__main__":
    raise SystemExit(main())
```

**和谁关联**：只依赖 `cli.py` 的 `main()`。

---

### 3. `cli.py`

**文件**：`bongo/cli.py`

**做什么**：命令行的"翻译层"。把用户输入的参数（`--provider openai`、`--resume latest`、`--max-steps 10`）翻译成 runtime 能直接使用的对象（model client、bongo 实例）。

#### 核心函数

**`build_arg_parser()`**（行 232-265）：定义所有 CLI 参数。

| 参数 | 默认值 | 作用 |
|---|---|---|
| `prompt` | 无（可选） | one-shot 模式的输入 |
| `--cwd` | `.` | 工作目录 |
| `--provider` | `openai` | 模型后端选择 |
| `--model` | None | 模型名覆盖 |
| `--resume` | None | 恢复的 session id 或 `latest` |
| `--approval` | `ask` | 审批策略 |
| `--max-steps` | 6 | 单请求最大步数 |

**`_build_model_client(args)`**（行 90-125）：根据 `--provider` 选择模型客户端。

```python
if provider == "openai":
    return OpenAICompatibleModelClient(...)
elif provider == "anthropic":
    return AnthropicCompatibleModelClient(...)
else:
    return OllamaModelClient(...)
```

**`build_agent(args)`**（行 173-229）：完整装配流程。

1. 采集 `WorkspaceContext`（git 分支、status、最近 commit、项目文档）
2. 创建 `SessionStore`
3. 根据 `--resume` 决定是恢复旧 session 还是新建
4. 返回装配好的 `bongo` 实例

**`main()`**（行 268-337）：程序主入口。

- 有 `prompt` 参数 → one-shot 模式，调一次 `agent.ask()` 就退出
- 没有 → REPL 模式，`while True` 循环等待输入，支持 `/help`、`/memory`、`/session`、`/reset`、`/exit` 命令

#### 和谁关联

```
cli.py
  ├── 依赖 models.py    （创建模型客户端）
  ├── 依赖 workspace.py  （创建工作区快照）
  └── 依赖 runtime.py   （创建 bongo 实例、SessionStore）
```

---

### 4. `runtime.py`

**文件**：`bongo/runtime.py`（~930 行）

**做什么**：这是整个 harness 的**核心**。它实现三件事：
1. **控制循环**：`ask()` 方法 — 组 prompt、调模型、执行工具、记录状态
2. **会话管理**：`SessionStore` 类 — session 的保存、加载、查找最新
3. **Prompt 前缀**：`build_prefix()` — 组装系统提示词（身份、规则、工具定义、工作区快照）

#### 关键类和方法

**`SessionStore`**（行 53-76）

```
.bongo/sessions/
├── 20260516-143021-a1b2c3.json    ← 每次启动一个 session
├── 20260516-150145-d4e5f6.json
└── ...
```

- `save(session)` → 写 JSON 文件
- `load(session_id)` → 读 JSON 文件
- `latest()` → 按修改时间找最新的 JSON 文件

**`bongo` 类**（行 79-929）— 核心类

构造函数（行 80-138）做了大量初始化：

```python
def __init__(self, model_client, workspace, session_store, ...):
    self.model_client = model_client          # 模型客户端
    self.workspace = workspace                # 工作区快照
    self.root = Path(workspace.repo_root)     # 仓库根目录
    self.session = session or {...}           # 会话数据（history + memory）
    self.memory = LayeredMemory(...)          # 分层记忆
    self.tools = self.build_tools()           # 工具注册表
    self.prefix_state = self.build_prefix()   # 系统提示词
    self.context_manager = ContextManager(self)  # prompt 组装器
    self.run_store = run_store or RunStore(...)  # 运行工件存储
```

**`ask(user_message)`**（行 450-624）— 主循环

每一轮做的事情：
1. 组 prompt：`_build_prompt_and_metadata()` → 调用 `ContextManager.build()`
2. 调模型：`model_client.complete(prompt, ...)`
3. 解析输出：`bongo.parse(raw)` → 返回 `("tool", payload)` 或 `("final", text)` 或 `("retry", notice)`
4. 如果是 tool → `run_tool(name, args)`
5. 如果是 final → 写 report，返回结果

**`run_tool(name, args)`**（行 626-703）— 工具执行流水线

5 道关卡：
1. `tools.get(name)` → 工具存在？
2. `validate_tool(name, args)` → 参数合法？
3. `repeated_tool_call(name, args)` → 不是重复调用？
4. `tool["risky"] and not approve()` → 危险操作通过审批？
5. `clip(tool["run"](args))` → 执行并截断输出

执行后调用 `update_memory_after_tool()` 更新记忆。

**`parse(raw)`**（行 786-835）— 模型输出解析

支持两种格式：
- JSON 风格：`<tool>{"name":"read_file","args":{"path":"x"}}</tool>`
- XML 风格：`<tool name="write_file" path="x"><content>...</content></tool>`

解析失败 → 返回 `("retry", notice)`，不消耗 tool step。

**`build_prefix()`**（行 181-238）— 系统提示词组装

包含：身份说明、调用规则、工具列表（含参数 schema 和风险等级）、few-shot 示例、工作区快照。返回一个 `PromptPrefix` 对象，附带 hash、workspace_fingerprint、tool_signature 三个元数据。

**`refresh_prefix()`**（行 245-271）— 工作区漂移检测

每轮调用前重新采集 workspace fingerprint，和上一轮比较。如果变了 → 重建 prefix。

**`path(raw_path)`**（行 912-926）— 路径沙箱

```python
resolved = (self.root / raw_path).resolve()   # 解析 .. 和 symlink
if os.path.commonpath([self.root, resolved]) != self.root:
    raise ValueError("path escapes workspace")
```

#### 和谁关联

```
runtime.py（bongo 类）
  ├── 依赖 context_manager.py  （ContextManager — prompt 组装）
  ├── 依赖 memory.py           （LayeredMemory — 记忆管理）
  ├── 依赖 tools.py            （工具定义、校验、执行函数）
  ├── 依赖 models.py           （model_client.complete()）
  ├── 依赖 task_state.py       （TaskState — 运行状态）
  ├── 依赖 run_store.py        （RunStore — 工件落盘）
  └── 依赖 workspace.py        （WorkspaceContext, clip, now）
```

它是所有模块的"胶水"，把各个专门模块串成完整的 agent 执行链路。

---

### 5. `models.py`

**文件**：`bongo/models.py`（~436 行）

**做什么**：把 3 种不同的模型 HTTP API 抹平成统一的 `complete(prompt, max_new_tokens) -> str` 接口。Runtime 不需要知道请求是发到 Ollama、OpenAI 还是 Anthropic。

#### 四个客户端类

**`FakeModelClient`**（行 15-28）— 测试用

```python
class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)  # 预设的回复队列
    def complete(self, prompt, max_new_tokens, **kwargs):
        return self.outputs.pop(0)    # 按顺序返回预设回复
```

用于 benchmark 和单元测试，保证结果确定性。

**`OllamaModelClient`**（行 31-79）— 本地 Ollama

- 端点：`POST /api/generate`
- 不支持 prompt cache
- 参数：`model`, `prompt`, `stream=False`, `options.num_predict/temperature/top_p`

**`OpenAICompatibleModelClient`**（行 224-344）— OpenAI / 兼容后端

- 端点：`POST /v1/responses`
- **唯一支持 prompt cache** 的客户端
- 关键逻辑（行 274-277）：

```python
if self.supports_prompt_cache and prompt_cache_key:
    payload["prompt_cache_key"] = prompt_cache_key
if self.supports_prompt_cache and prompt_cache_retention:
    payload["prompt_cache_retention"] = prompt_cache_retention
```

- 支持 SSE 流式响应解析（`_extract_openai_response_from_sse()`）
- 从 `usage.input_tokens_details.cached_tokens` 提取缓存命中数据
- 3 次重试（行 289-311），5xx 错误和连接错误都会重试

**`AnthropicCompatibleModelClient`**（行 356-435）— Claude / 兼容后端

- 端点：`POST /v1/messages`
- 缓存参数显式丢弃：`del prompt_cache_key, prompt_cache_retention`
- 请求头需要 `x-api-key` 和 `anthropic-version`

#### 辅助函数

| 函数 | 行号 | 作用 |
|---|---|---|
| `_normalize_versioned_base_url()` | 82-86 | 确保 base_url 以 `/v1` 结尾 |
| `_extract_openai_text()` | 89-113 | 从 OpenAI 响应中提取文本（兼容多种格式） |
| `_extract_openai_text_from_sse()` | 116-163 | 解析 SSE 流式响应 |
| `_extract_usage_cache_details()` | 207-221 | 统一提取 usage 和 cached_tokens |

#### 和谁关联

```
models.py
  └── 被 runtime.py 调用（model_client.complete()）
  └── 被 cli.py 创建（_build_model_client()）
  └── 被 evaluator.py 使用（FakeModelClient）
  └── 被 metrics.py 使用（FakeModelClient, OpenAI, Anthropic）
```

models.py **不依赖**任何其他 bongo 模块 — 它是纯 IO 层。

---

### 6. `tools.py`

**文件**：`bongo/tools.py`（~317 行）

**做什么**：定义 agent 的能力白名单 — 有哪些工具、参数是什么、怎么校验、怎么执行。

#### 工具规格定义

`BASE_TOOL_SPECS`（行 14-51）定义 6 个基础工具：

```python
BASE_TOOL_SPECS = {
    "list_files":  {"schema": {"path": "str='.'"},         "risky": False},
    "read_file":   {"schema": {"path":"str","start":"int=1","end":"int=200"}, "risky": False},
    "search":      {"schema": {"pattern":"str","path":"str='.'"}, "risky": False},
    "run_shell":   {"schema": {"command":"str","timeout":"int=20"}, "risky": True},
    "write_file":  {"schema": {"path":"str","content":"str"}, "risky": True},
    "patch_file":  {"schema": {"path":"str","old_text":"str","new_text":"str"}, "risky": True},
}
```

`DELEGATE_TOOL_SPEC`（行 54-59）定义第 7 个工具。

#### 核心函数

**`build_tool_registry(agent)`**（行 80-93）— 工具注册

```python
tools = {name: {**spec, "run": partial(runners[name], agent)}
         for name, spec in BASE_TOOL_SPECS.items()}
if agent.depth < agent.max_depth:
    tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, agent)}
```

用 `functools.partial` 提前绑定 agent 实例，执行时只需要传 args。

**`validate_tool(agent, name, args)`**（行 100-164）— 参数校验

每个工具有独立的校验逻辑。`patch_file` 的校验最严格 — 要求 `old_text` 在文件中恰好出现 1 次：

```python
count = text.count(old_text)
if count != 1:
    raise ValueError(f"old_text must occur exactly once, found {count}")
```

#### 7 个工具的实现

| 函数 | 行号 | 做什么 |
|---|---|---|
| `tool_list_files` | 167-179 | 列目录，隐藏 `.git`/`.bongo`/`__pycache__`，最多 200 项 |
| `tool_read_file` | 182-192 | 按行范围读 UTF-8 文件，带行号 |
| `tool_search` | 195-222 | 优先用 `rg`，不可用时 fallback 到纯 Python grep |
| `tool_run_shell` | 225-251 | 执行 shell 命令，用 `agent.shell_env()` 过滤环境变量 |
| `tool_write_file` | 254-259 | 写文件，自动创建父目录 |
| `tool_patch_file` | 262-276 | 精确单匹配替换 |
| `tool_delegate` | 279-306 | 创建只读子 agent，返回结论文本 |

#### 和谁关联

```
tools.py
  ├── 被 runtime.py 调用
  │    ├── build_tool_registry() → __init__ 时注册工具
  │    ├── validate_tool()      → run_tool() 执行前校验
  │    ├── tool_*()             → run_tool() 实际执行
  │    └── tool_example()       → build_prefix() 写入 few-shot 示例
  └── 依赖 workspace.py（IGNORED_PATH_NAMES, clip）
```

tools.py **不持有** agent 状态 — agent 实例通过 partial 绑定传入。

---

### 7. `context_manager.py`

**文件**：`bongo/context_manager.py`（~440 行）

**做什么**：每一轮 `ask()` 调模型之前，把 5 个 section（prefix, memory, relevant_memory, history, current_request）拼成最终 prompt。如果总长度超预算，按固定顺序裁剪。

#### 预算体系

```python
DEFAULT_TOTAL_BUDGET = 12000        # 总字符上限
DEFAULT_SECTION_BUDGETS = {          # 每个 section 的目标配额
    "prefix": 3600, "memory": 1600,
    "relevant_memory": 1200, "history": 5200,
}
DEFAULT_SECTION_FLOORS = {           # 每个 section 的最低保底
    "prefix": 1200, "memory": 400,
    "relevant_memory": 300, "history": 1500,
}
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
```

`current_request` 不参与预算分配 — 它的 budget 始终为 0，永远不被裁剪。

#### `ContextManager` 类

**`build(user_message)`**（行 79-179）— 核心方法

流程：
1. 检查 feature flags（memory、relevant_memory、context_reduction 是否启用）
2. 渲染各 section（`_render_sections()`）
3. 拼接 prompt（`_assemble_prompt()`）
4. 如果超预算 → while 循环按 `reduction_order` 逐个压缩 section，直到满足预算或全部压到 floor
5. 返回 `(prompt, metadata)` — metadata 记录每个 section 的原始长度、裁剪后长度、压缩记录

**`_render_history_section(budget)`**（行 314-365）— History 的智能裁剪

不是简单截断，而是：
- 最近 6 条：每条 900 字符配额
- 更早的：每条 60 字符
- 从后往前渲染，如果某条放不下就缩减到可用空间

**`_render_relevant_memory(selected_notes, budget)`**（行 260-305）— 相关记忆裁剪

多条笔记平均分配预算，避免一条超长笔记挤掉其他的：

```python
per_note_budget = self._per_note_budget(budget, len(note_texts), header)
rendered_notes = [_tail_clip(text, per_note_budget) for text in note_texts]
```

#### `SectionRender` 数据类

```python
@dataclass
class SectionRender:
    raw: str           # 原始文本
    budget: int        # 分配的字符预算
    rendered: str      # 裁剪后的最终文本
    details: dict      # 元数据（选中的笔记、渲染的条目等）
```

#### 和谁关联

```
context_manager.py
  ├── 被 runtime.py 调用（_build_prompt_and_metadata() → ContextManager.build()）
  ├── 调用 memory.py（retrieval_candidates() — 召回相关笔记）
  ├── 读取 runtime.py 的 agent.session["history"]（通过 self.agent）
  └── 读取 runtime.py 的 agent.memory_text()（通过 self.agent）
```

context_manager.py 和 runtime.py 之间是**双向依赖**：
- runtime 调 context_manager.build()
- context_manager 通过 `self.agent` 读取 runtime 的 session 和 memory

但 context_manager **只读** runtime 的状态，不修改 — 这是单向数据流。

---

### 8. `memory.py`

**文件**：`bongo/memory.py`（~420 行）

**做什么**：实现三层记忆系统，让 agent 跨轮记住"我读过什么文件、文件里大概有什么、当前任务是什么"。

#### 三层记忆

**Working Memory**（工作记忆）— 活跃状态

```python
"working": {
    "task_summary": "当前任务的一句话摘要",   # max 300 字符
    "recent_files": ["src/main.py", "README.md"],  # max 8 个，FIFO
}
```

`set_task_summary()`（行 236-240）每次 `ask()` 开头被调用。
`remember_file()`（行 243-252）每次工具涉及文件时被调用。

**Episodic Notes**（情景笔记）— 从工具结果中提炼的知识点

```python
"episodic_notes": [
    {
        "text": "文件摘要内容...",      # max 500 字符
        "tags": ["src/main.py"],       # 标签（通常是文件路径）
        "source": "src/main.py",       # 来源
        "created_at": "2026-05-16T...",
        "note_index": 0,
    },
    # ... max 12 条
]
```

`append_note()`（行 255-277）在 `read_file` 后被调用。
`retrieval_candidates()`（行 318-336）在 prompt 组装时被 ContextManager 调用，根据关键词/tag 重叠度排序。

**File Summaries**（文件摘要）— 带 freshness 校验

```python
"file_summaries": {
    "src/main.py": {
        "summary": "实现了 agent 的主循环...",  # max 500 字符
        "created_at": "2026-05-16T...",
        "freshness": "sha256:abc123...",         # 文件内容的 SHA-256
    }
}
```

`set_file_summary()`（行 280-291）在 `read_file` 后被调用，同时计算 `file_freshness()`。
`invalidate_file_summary()`（行 294-300）在 `write_file`/`patch_file` 后被调用。
渲染时（`render_memory_text()` 行 362-368）比对 freshness hash — 不匹配则静默丢弃。

#### 关键辅助函数

| 函数 | 行号 | 作用 |
|---|---|---|
| `canonicalize_path()` | 78-86 | 绝对路径 → workspace 相对路径（统一用 `/`） |
| `resolve_workspace_path()` | 59-71 | 路径逃逸检测（逃逸返回 None） |
| `file_freshness()` | 89-93 | 计算文件 SHA-256 |
| `summarize_read_result()` | 303-314 | 从 read_file 结果提取前 3 行作为摘要 |
| `retrieval_candidates()` | 318-336 | 按关键词/tag 匹配度召回 top-3 笔记 |
| `normalize_memory_state()` | 146-233 | 兼容旧版 session 格式的状态标准化 |

#### `LayeredMemory` 类

（行 380-419）— 包装所有函数，提供面向对象的接口。持有 `state` 字典和 `workspace_root`。

#### 和谁关联

```
memory.py
  ├── 被 runtime.py 调用
  │    ├── LayeredMemory()           → __init__ 创建记忆实例
  │    ├── render_memory_text()      → memory_text() → prompt 组装
  │    ├── update_memory_after_tool() → run_tool() 执行后更新记忆
  │    └── retrieval_candidates()    → context_manager.py 召回笔记
  ├── 被 context_manager.py 调用（retrieval_candidates()）
  ├── 被 evaluator.py 使用（default_memory_state()）
  └── 被 metrics.py 使用（default_memory_state()）
```

---

### 9. `workspace.py`

**文件**：`bongo/workspace.py`（~136 行）

**做什么**：在 agent 按需读文件之前，先给它一份"仓库第一印象"。快照刻意保持小而稳定：git 事实 + 少量项目文档。

#### `WorkspaceContext` 类

**`build(cwd)`**（行 55-101）— 采集工作区信息

```python
return cls(
    cwd=str(cwd),
    repo_root=git(["rev-parse", "--show-toplevel"]),
    branch=git(["branch", "--show-current"]),
    default_branch=...,
    status=git(["status", "--short"]),
    recent_commits=git(["log", "--oneline", "-5"]).splitlines(),
    project_docs=docs,  # 从白名单文件名中读取
)
```

白名单文件名（行 18）：`("AGENTS.md", "README.md", "pyproject.toml", "package.json")`。每个文件最多读 1200 字符。

**`text()`**（行 103-121）— 生成用于 prompt 的文本

格式化为结构化文本，嵌入到 `build_prefix()` 生成的系统提示词中。

**`fingerprint()`**（行 123-135）— 生成工作区指纹

将所有 workspace 事实序列化为 JSON，计算 SHA-256。用于 `refresh_prefix()` 检测 workspace 变化。

#### 工具函数

| 函数 | 行号 | 作用 |
|---|---|---|
| `clip(text, limit)` | 26-30 | 尾部截断，超长加 `...[truncated N chars]` |
| `middle(text, limit)` | 33-41 | 保留头尾，中间用 `...` |
| `now()` | 22-23 | 当前 UTC ISO 时间戳 |

#### 和谁关联

```
workspace.py
  ├── 被 cli.py 调用（WorkspaceContext.build()）
  ├── 被 runtime.py 调用
  │    ├── workspace.text()      → build_prefix() 嵌入 prompt
  │    ├── workspace.fingerprint() → refresh_prefix() 检测变化
  │    └── clip()                → 截断工具输出、历史记录
  ├── 被 tools.py 引用（IGNORED_PATH_NAMES — list_files 过滤）
  ├── 被 evaluator.py 调用（WorkspaceContext.build()）
  └── 被 metrics.py 调用（WorkspaceContext.build()）
```

workspace.py **不依赖**任何其他 bongo 模块 — 它是纯数据采集层。

---

### 10. `task_state.py`

**文件**：`bongo/task_state.py`（~111 行）

**做什么**：跟踪单次 `ask()` 运行的状态机 — 当前在跑还是停了、调了多少次工具、为什么停下。

#### `TaskState` 数据类

```python
@dataclass
class TaskState:
    run_id: str          # "run_20260516-143021-a1b2c3"
    task_id: str         # "task_20260516-143021-d4e5f6"
    user_request: str    # 用户的原始请求
    status: str          # running → completed / stopped / failed
    tool_steps: int      # 已执行的工具调用次数
    attempts: int        # 模型被调用次数
    last_tool: str       # 最后一次调用的工具名
    stop_reason: str     # 停止原因
    final_answer: str    # 最终答案
```

#### 状态转换

```
running ──→ completed   (finish_success() — 正常返回最终答案)
running ──→ stopped     (stop_step_limit() — 步数耗尽)
running ──→ stopped     (stop_retry_limit() — 重试次数耗尽)
running ──→ failed      (stop_model_error() — 模型报错)
```

#### 和谁关联

```
task_state.py
  ├── 被 runtime.py 创建和更新（ask() 中）
  └── 被 run_store.py 序列化（write_task_state() → to_dict()）
```

task_state.py 是纯数据类，**不依赖**任何其他模块。

---

### 11. `run_store.py`

**文件**：`bongo/run_store.py`（~87 行）

**做什么**：把运行过程中的 3 类工件写到磁盘。session.json 负责"可恢复的会话状态"，RunStore 负责"单次运行的审计工件"。

#### `RunStore` 类

目录结构：
```
.bongo/runs/
├── run_20260516-143021-a1b2c3/
│   ├── task_state.json   ← 持续更新（原子写入）
│   ├── trace.jsonl       ← 逐行追加
│   └── report.json       ← 运行结束时写入
├── run_20260516-150145-d4e5f6/
│   └── ...
```

| 方法 | 什么时候调用 | 写什么 |
|---|---|---|
| `start_run()` | `ask()` 开头 | 创建 run 目录，写初始 task_state |
| `write_task_state()` | 每次 attempt / tool 执行后 | 更新 task_state.json（原子写） |
| `append_trace()` | 每个事件（prompt_built, model_parsed, tool_executed...） | 追加一行到 trace.jsonl |
| `write_report()` | `ask()` 结束时 | 写最终 report.json |

**原子写**（`_write_json_atomic()` 行 72-86）：

```python
with tempfile.NamedTemporaryFile("w", ..., delete=False) as handle:
    json.dump(payload, handle, ...)
    temp_name = handle.name
Path(temp_name).replace(path)  # 原子替换
```

**trace.jsonl** — 一行一个 JSON 事件：

```jsonl
{"event":"run_started","task_id":"task_...","created_at":"..."}
{"event":"prompt_built","prompt_metadata":{...},"duration_ms":12}
{"event":"model_requested","attempts":1,"tool_steps":0}
{"event":"model_parsed","kind":"tool","duration_ms":340}
{"event":"tool_executed","name":"read_file","tool_status":"ok","duration_ms":5}
{"event":"run_finished","status":"completed","stop_reason":"final_answer_returned"}
```

#### 和谁关联

```
run_store.py
  ├── 被 runtime.py 调用（ask() 中的各阶段落盘）
  ├── 被 metrics.py 读取（aggregate_run_artifacts() 扫描 trace/report）
  └── 依赖 task_state.py（task_state.to_dict()）
```

---

### 12. `evaluator.py`

**文件**：`bongo/evaluator.py`（~472 行）

**做什么**：用 `FakeModelClient`（脚本化确定性输出）跑固定 benchmark，验证 harness 的执行合同是否稳定。

#### Benchmark 任务

`benchmarks/coding_tasks.json` 定义 6 个任务：

| ID | 类别 | 做什么 |
|---|---|---|
| `readme_intro_locked` | documentation | 替换 README 开头句子 |
| `readme_schema_note` | documentation | 替换 README 第一个 bullet |
| `readme_ordering_note` | documentation | 替换 README 第二个 bullet |
| `sample_beta_locked` | text-edit | 替换 sample.txt 中的 beta |
| `sample_gamma_locked` | text-edit | 替换 sample.txt 中的 gamma |
| `sample_placeholder_delta` | text-edit | 替换 sample.txt 中的 placeholder |

每个任务有 `step_budget`（步数预算）、`verifier`（验证脚本）、`allowed_tools`（允许的工具）。

#### `BenchmarkEvaluator` 类

**`run()`**（行 280-314）：跑全部任务，生成 benchmark-v1.json。

**`run_task(task)`**（行 316-419）：跑单个任务的完整流程：

1. 复制 fixture 仓库到临时目录（隔离）
2. 创建 `WorkspaceContext`、`SessionStore`、`RunStore`
3. 创建 `FakeModelClient`（脚本化输出）
4. 调用 `agent.ask(task["prompt"])`
5. 运行 verifier 脚本
6. 判定 pass/fail（4 个条件：在预算内 + verifier 通过 + 产物存在 + 正常停止）

脚本化输出示例（`SCRIPTED_MODEL_OUTPUTS` 行 53-84）：

```python
"readme_intro_locked": [
    '<tool name="patch_file" path="README.md"><old_text>...</old_text><new_text>...</new_text></tool>',
    "<final>Done.</final>",
]
```

#### 和谁关联

```
evaluator.py
  ├── 被 metrics.py 调用（run_fixed_benchmark()）
  ├── 被 scripts/ 调用
  ├── 使用 runtime.py（bongo 类、SessionStore）
  ├── 使用 models.py（FakeModelClient）
  ├── 使用 workspace.py（WorkspaceContext）
  └── 使用 run_store.py（RunStore）
```

---

### 13. `metrics.py`

**文件**：`bongo/metrics.py`（~1225 行）

**做什么**：整个评测体系的"总指挥"。聚合运行工件、跑 ablation 实验、生成简历指标报告。

#### 三层功能

**① 运行工件聚合** — `aggregate_run_artifacts()`（行 78-148）

扫描 `.bongo/runs/` 下所有目录，从 report.json 和 trace.jsonl 中提取：

- 平均 tool steps、attempts
- Cache hit rate、prefix reuse rate
- 工具调用状态分布（ok / rejected / error）
- 安全事件分布
- 停止原因分布
- 平均运行时长、工具执行时长、prompt 构建时长

**② 实验套件**

| 实验 | 函数 | 做什么 |
|---|---|---|
| 上下文压力矩阵 | `run_context_stress_matrix()` | 12 组配置 × full vs no_reduction |
| 小规模记忆实验 | `run_memory_dependency_experiment()` | 1 任务 × 3 变体 × 3 重复 |
| 大规模记忆实验 | `run_large_scale_memory_experiment()` | 12 任务 × 3 变体 × 5 重复 |
| 安全实验套件 | `run_security_experiment_suite()` | 10 场景 × 3 重复 |
| 真实模型实验 | `run_provider_experiments()` | 用真 GPT/Claude 跑 benchmark |

**`_MemoryExperimentModelClient`**（行 211-245）— 记忆实验专用假模型

行为逻辑：
1. Bootstrap 阶段：读文件 → 返回 "Done."
2. Follow-up 阶段：检查 prompt 的 memory/relevant_memory 中是否有目标事实
   - 有 → 直接返回答案（不读文件）
   - 没有 → 再读一次文件

这样就把"记忆系统能不能把信息送到 prompt 中"和"模型能不能记住"分离开了。

**③ 报告生成**

`collect_resume_metrics()`（行 1042-1104）— 评测总入口，聚合所有实验结果。

`render_resume_metrics_markdown()`（行 1107-1153）— 生成简历格式的 Markdown 报告。

`render_large_scale_experiment_report()`（行 1156-1224）— 生成详细实验报告。

#### 和谁关联

```
metrics.py
  ├── 调用 evaluator.py（run_fixed_benchmark()）
  ├── 调用 runtime.py（bongo, SessionStore）
  ├── 调用 models.py（FakeModelClient, OpenAI, Anthropic）
  ├── 调用 memory.py（default_memory_state()）
  ├── 调用 workspace.py（WorkspaceContext）
  ├── 读取 run_store.py 的产物（.bongo/runs/ 下的 trace/report）
  └── 被 scripts/collect_resume_metrics.py 调用
```

metrics.py 是**依赖最多模块的文件** — 因为它的职责就是把所有模块的能力串起来做评测。

---

## 模块间数据流全景

### 一条用户请求的完整数据流

```
用户输入 "修改 README 第一行"
    │
    ▼
cli.py: main() → agent.ask(user_message)
    │
    ▼
runtime.py: ask()
    │
    ├──→ memory.py: set_task_summary("修改 README 第一行")
    │         └── 更新 working.task_summary
    │
    ├──→ runtime.py: record() → SessionStore.save()
    │         └── .bongo/sessions/xxx.json
    │
    ├──→ task_state.py: TaskState.create()
    ├──→ run_store.py: start_run() → .bongo/runs/<run_id>/
    │
    ├──→ [LOOP] ──────────────────────────────────────────
    │    │
    │    ├──→ runtime.py: refresh_prefix()
    │    │    └──→ workspace.py: WorkspaceContext.build()
    │    │              └── git status, git log, 读白名单文件
    │    │
    │    ├──→ context_manager.py: ContextManager.build()
    │    │    ├──→ memory.py: retrieval_candidates("修改 README 第一行")
    │    │    │         └── 从 episodic_notes 中按关键词召回 top-3
    │    │    ├──→ runtime.py: agent.memory_text() → memory.py: render_memory_text()
    │    │    │         └── 渲染 working + file_summaries（freshness 校验）
    │    │    ├──→ runtime.py: agent.session["history"] → 裁剪历史
    │    │    └──→ 拼接 [prefix][memory][relevant][history][request]
    │    │
    │    ├──→ models.py: model_client.complete(prompt, ...)
    │    │         └── HTTP POST → 模型返回 "<tool>...</tool>"
    │    │
    │    ├──→ runtime.py: parse(raw) → ("tool", {"name":"read_file",...})
    │    │
    │    ├──→ runtime.py: run_tool("read_file", {"path":"README.md"})
    │    │    ├──→ tools.py: validate_tool() → 检查路径、行范围
    │    │    ├──→ runtime.py: path("README.md") → workspace.py 的路径校验
    │    │    ├──→ tools.py: tool_read_file() → 读文件内容
    │    │    ├──→ workspace.py: clip(result, 4000) → 截断
    │    │    └──→ runtime.py: update_memory_after_tool()
    │    │         ├──→ memory.py: remember_file("README.md")
    │    │         ├──→ memory.py: set_file_summary("README.md", summary)
    │    │         └──→ memory.py: append_note(summary, tags=("README.md",))
    │    │
    │    ├──→ run_store.py: append_trace("tool_executed", {...})
    │    │         └── .bongo/runs/<id>/trace.jsonl 追加一行
    │    │
    │    └──→ [回到 LOOP 顶部，组下一轮 prompt]
    │
    ├──→ [模型返回 <final>Done.</final>]
    │
    ├──→ task_state.py: finish_success("Done.")
    ├──→ run_store.py: write_report()
    │         └── .bongo/runs/<id>/report.json
    │
    └──→ 返回 "Done."
```

### 模块间调用关系速查

```
被谁调用              调用谁
──────────           ──────────
__main__.py    →     cli.py

cli.py         →     runtime.py, models.py, workspace.py

runtime.py     →     context_manager.py, memory.py, tools.py,
                     models.py, task_state.py, run_store.py, workspace.py

context_manager.py → memory.py（只读 agent 的 session/history）

tools.py       →     workspace.py（IGNORED_PATH_NAMES, clip）

memory.py      →     workspace.py（clip, now, canonicalize_path）

evaluator.py   →     runtime.py, models.py, workspace.py, run_store.py, memory.py

metrics.py     →     evaluator.py, runtime.py, models.py, memory.py, workspace.py

────────── 无外部依赖 ──────────
models.py      （纯 IO，不依赖其他模块）
task_state.py  （纯数据类）
workspace.py   （纯数据采集）
run_store.py   （只依赖 task_state.py）
```

### 数据流向总结

```
workspace.py ──→ workspace 快照数据 ──→ runtime.py（build_prefix 嵌入 prompt）
                                      ──→ context_manager.py（稳定前缀段）

memory.py    ──→ 记忆文本 ──→ runtime.py（memory_text() 嵌入 prompt）
                            ──→ context_manager.py（relevant_memory 召回）

models.py    ──→ 模型原始输出 ──→ runtime.py（parse 解析）

tools.py     ──→ 工具执行结果 ──→ runtime.py（record 写入 history, update_memory）

task_state.py ──→ 运行状态 ──→ run_store.py（write_task_state 落盘）

run_store.py  ──→ trace.jsonl / report.json ──→ metrics.py（聚合分析）

context_manager.py ──→ 最终 prompt + metadata ──→ runtime.py（发给模型，写入 trace）
```
