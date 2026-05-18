# Pico (Bongo) 项目深度解析 — 面试抗压手册

> 本文档逐条对应简历描述，结合源码给出设计动机、关键实现、数据佐证和面试高频追问的应对口径。

---

## 目录

1. [Agent Harness 架构设计](#1-agent-harness-架构设计)
2. [长上下文治理](#2-长上下文治理)
3. [结构化记忆系统](#3-结构化记忆系统)
4. [任务恢复机制](#4-任务恢复机制)
5. [工具安全与运行治理](#5-工具安全与运行治理)
6. [评测与审计闭环](#6-评测与审计闭环)

---

## 1. Agent Harness 架构设计

### 简历描述

> 负责本地代码 agent 的整体设计与开发，统一模型接入、工具执行、会话状态、checkpoint 恢复和运行工件落盘流程，形成可复盘的执行链路；支持 2 类模型后端、7 类工具和 3 类运行工件。

### 1.1 整体架构 — "控制循环 + 插件化模块"

核心类 `bongo`（`bongo/runtime.py:79`）是整个 harness 的调度中心。它不是一个框架，而是一个**薄控制层**：外层管循环和状态，内层模块各管各的。

```
┌─────────────────────────────────────────────────┐
│                    CLI / REPL                    │  cli.py
│          build_agent(args) → bongo 实例          │
└──────────────────────┬──────────────────────────┘
                       │ user_message
                       ▼
┌─────────────────────────────────────────────────┐
│              bongo.ask()  主循环                  │  runtime.py:450-624
│  while tool_steps < max_steps:                  │
│    1. ContextManager.build() → prompt           │
│    2. model_client.complete() → raw text        │
│    3. bongo.parse() → (kind, payload)           │
│    4. if tool: run_tool(name, args)             │
│    5. if final: 写 report, 返回结果              │
└───────┬──────────┬──────────┬───────────────────┘
        │          │          │
        ▼          ▼          ▼
  ContextManager  models.py  tools.py + run_store.py
  (prompt组装)   (模型适配)   (工具执行+工件落盘)
```

**面试口径**：整个 harness 做的事情就是把 "prompt → model → parse → tool → record" 这个循环稳定化。每个模块只做一件事，runtime 本身不关心 HTTP 细节、不关心工具怎么实现、不关心 prompt 怎么裁剪 — 这些都委托给专门的模块。

### 1.2 模型接入 — 统一 `complete()` 接口（2 类后端）

`bongo/models.py` 定义了 4 个不同协议的模型客户端类，但对外只暴露 **1 个统一接口**：

```python
# models.py — 所有 client 都遵循这个签名
def complete(self, prompt, max_new_tokens, **kwargs) -> str:
```

| 类 | 对应后端 | 端点 | 特殊能力 |
|---|---|---|---|
| `OllamaModelClient` | 本地 Ollama | `/api/generate` | 无 |
| `OpenAICompatibleModelClient` | OpenAI / 兼容后端 | `/responses` | **prompt cache** (`prompt_cache_key`) |
| `AnthropicCompatibleModelClient` | Claude / 兼容后端 | `/messages` | 无（缓存参数显式丢弃） |
| `FakeModelClient` | 测试用 | 无 HTTP | 脚本化确定性输出 |

**关键设计决策**：`OpenAICompatibleModelClient`（`models.py:224-344`）是唯一支持 prompt cache 的后端。它接受 `prompt_cache_key`（稳定前缀的 SHA-256）和 `prompt_cache_retention="in_memory"`，并从 response 的 `usage.input_tokens_details.cached_tokens` 中提取缓存命中数据。

```python
# models.py:274-277
if self.supports_prompt_cache and prompt_cache_key:
    payload["prompt_cache_key"] = prompt_cache_key
if self.supports_prompt_cache and prompt_cache_retention:
    payload["prompt_cache_retention"] = prompt_cache_retention
```

Runtime 通过 `getattr(self.model_client, "supports_prompt_cache", False)` 判断是否传递缓存参数 — **不需要修改 runtime 代码就能适配新后端**。

### 1.3 工具系统 — 7 类工具，安全/危险分级

`bongo/tools.py` 定义了 **7 个工具**，按安全等级分层：

| 工具 | `risky` | 用途 |
|---|---|---|
| `list_files` | `False` | 列目录 |
| `read_file` | `False` | 按行范围读文件 |
| `search` | `False` | rg / fallback grep 搜索 |
| `run_shell` | **`True`** | 执行任意 shell 命令 |
| `write_file` | **`True`** | 创建/覆盖文件 |
| `patch_file` | **`True`** | 精确单匹配文本替换 |
| `delegate` | `False` | 派生只读子 agent |

`risky=True` 的工具在执行前必须通过审批（`approve()` 方法，`runtime.py:772-783`）。审批策略有 `ask`（交互确认）、`auto`（全部放行）、`never`（全部拒绝）三档。

工具注册通过 `build_tool_registry()`（`tools.py:80-93`）完成，用 `functools.partial` 提前绑定 agent 实例。`delegate` 工具只在 `depth < max_depth` 时才注册 — 深度耗尽时模型根本看不到这个工具。

### 1.4 运行工日志— 3 类运行日志

`bongo/run_store.py` 中的 `RunStore` 类管理每次 `ask()` 的审计工件：

```
.bongo/runs/<run_id>/
├── task_state.json   ← 运行中持续更新（原子写入）
├── trace.jsonl       ← 逐事件追加写入（流式）
└── report.json       ← 运行结束时一次性写入
```

**为什么分 3 个文件**：
- `task_status.json`：运行中可观测 — 外部进程可以随时读取当前状态，他是一个覆盖写的操作，每当大模型成功完成一个动作，他会记录下当前工具调用与agent调用的轮次，以及成功完成的动作(组装prompt，发送prompt，收到回复，调用工具，读取工具结果等)，并且可以通过--status命令在cli中随时查看。
- `trace.jsonl`：事后复盘 — 每个事件一行 JSON，记录 prompt 怎么组装的、模型返回了什么类型、工具执行了多长时间，
- `report.json`：最终摘要 — 记录了工具调用的次数以及调用了哪些工具，消耗的tokens，用户最初的请求以及agent运行的结果，agent调用的轮数，以及退出的原因(异常，正常，工具/agent调用超轮次)。

写入全部使用**原子写**（`run_store.py:72-86`）：先写临时文件，再 `replace`。即使中途崩溃也不会留下半截 JSON。

```python
# run_store.py:72-86
def _write_json_atomic(self, path, payload):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                      dir=str(path.parent), ...) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        temp_name = handle.name
    Path(temp_name).replace(path)
```

### 1.5 会话状态 — SessionStore

`SessionStore`（`runtime.py:53-76`）将完整会话存为 `.bongo/sessions/<session_id>.json`。每个 session 包含 `id`、`history`（完整交互记录）和 `memory`（LayeredMemory 状态字典）。`latest()` 方法按 `st_mtime` 排序找最近修改的 JSON 文件，实现 `--resume latest`。

### 面试高频追问

**Q: 为什么不直接调用 OpenAI SDK / Anthropic SDK？**
A: 因为 harness 需要统一接口，且需要对接非标准兼容后端（如阿里云 DashScope、自建代理）。用 `urllib` 直接发 HTTP 可以完全控制请求格式和缓存参数，不依赖第三方 SDK 的抽象泄漏。同时减少了依赖体积。

**Q: 为什么 trace 用 JSONL 而不是 JSON 数组？**
A: 因为 agent 运行是流式的，JSONL 可以逐行追加，不需要在每次事件时重写整个文件。配合原子写 task_state，即使进程崩溃，已写入的 trace 行也不会丢失。

**Q: 为什么工具是显式注册而不是动态发现？**
A: 可审计性。模型看到的工具列表是确定的，变更可以通过 `tool_signature()`（工具集 SHA-256）追踪。动态发现意味着工具表面不可控，出问题很难定位是哪个工具引入的。

---

## 2. 长上下文治理

### 简历描述

> 设计分层上下文管理与预算裁剪机制，在 12 组长上下文配置里，将平均 prompt 长度从 7082 压到 5664，平均压缩率 16.19%，最高压缩率 33.28%，同时保证当前请求不被裁坏。

### 2.1 Prompt 组装结构

`ContextManager`（`bongo/context_manager.py`）是 prompt 组装的核心。最终发给模型的 prompt 由 **5 个 section** 按固定顺序拼接：

```
[prefix]          ← 系统身份、规则、工具定义、工作区快照（稳定段）
[memory]          ← 工作记忆：任务摘要、最近文件、文件摘要
[relevant_memory] ← 按关键词召回的情景笔记（最多 3 条）
[history]         ← 完整交互记录（裁剪后）
[current_request] ← 用户当前请求（**永不裁剪**）
```

拼接逻辑在 `_assemble_prompt()`（`context_manager.py:386-396`）：

```python
def _assemble_prompt(self, rendered):
    return "\n\n".join([
        rendered["prefix"].rendered,
        rendered["memory"].rendered,
        rendered["relevant_memory"].rendered,
        rendered["history"].rendered,
        rendered[CURRENT_REQUEST_SECTION].rendered,
    ]).strip()
```

### 2.2 预算分配与裁剪策略

**默认预算**（`context_manager.py:13-25`）：

```python
DEFAULT_TOTAL_BUDGET = 12000        # 总字符数上限
DEFAULT_SECTION_BUDGETS = {
    "prefix": 3600,                  # 系统前缀
    "memory": 1600,                  # 工作记忆
    "relevant_memory": 1200,         # 相关记忆
    "history": 5200,                 # 对话历史
}
DEFAULT_SECTION_FLOORS = {           # 各 section 最低保底
    "prefix": 1200,
    "memory": 400,
    "relevant_memory": 300,
    "history": 1500,
}
```

**裁剪顺序**（`context_manager.py:27`）：

```python
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
```

裁剪逻辑在 `build()` 方法的 while 循环中（`context_manager.py:143-168`）：

```python
while len(prompt) > self.total_budget:
    overflow = len(prompt) - self.total_budget
    reduced = False
    for section in self.reduction_order:
        floor = int(self.section_floors.get(section, 0))
        current_budget = int(budgets.get(section, 0))
        if current_budget <= floor:
            continue
        new_budget = max(floor, current_budget - overflow)
        ...
        budgets[section] = new_budget
        rendered = self._render_sections(section_texts, budgets, ...)
        prompt = self._assemble_prompt(rendered)
        reduced = True
        break
```

**核心保证**：`current_request` 的 budget 始终为 0，`_render_sections()` 中对它直接透传（`context_manager.py:247-249`），**永远不会被裁剪**。

### 2.3 History 的智能裁剪

History 不是简单截断尾部，而是有**近期窗口保护**（`context_manager.py:314-365`）：

- 最近 6 条记录：每条最多 900 字符，享有完整配额
- 更早的记录：每条只给 60 字符，工具输出截取首行
- 如果近期记录仍然超预算，会进一步缩减到可用空间

```python
recent_window = 6
recent_start = max(0, len(history) - recent_window)
for index in reversed(range(len(history))):
    recent = index >= recent_start
    line_limit = 900 if recent else 60
    candidate_lines = self._render_history_item(item, line_limit)
    ...
```

### 2.4 Feature Flags — 可开关的上下文治理

`runtime.py:28-33` 定义了 4 个 feature flags：

```python
DEFAULT_FEATURE_FLAGS = {
    "memory": True,              # 工作记忆
    "relevant_memory": True,     # 相关记忆召回
    "context_reduction": True,   # 预算裁剪
    "prompt_cache": True,        # prompt 缓存
}
```

关闭 `context_reduction` 后，`build()` 走 `_render_sections_without_reduction()` 分支（`context_manager.py:120-132`），所有 section 原样透传。这正是对比实验（ablation study）的基础 — 用 `context_reduction=True` 和 `False` 的差值衡量压缩收益。

### 2.5 实验数据来源

`metrics.py:429-497` 中的 `run_context_stress_matrix()` 在 **12 组配置**（3 历史长度 × 2 笔记密度 × 2 请求长度）下做对比：

```python
history_levels = [("short", 4), ("medium", 12), ("long", 24)]
note_levels = [("low", 2), ("high", 10)]
request_levels = [("short", "recall"), ("long", "recall the relevant benchmark fact...")]
```

每组配置比较 `full`（开启所有裁剪）和 `no_context_reduction`（关闭裁剪）的 prompt 字符数：

```python
full_chars = metrics["full"]["prompt_chars"]
raw_chars = metrics["no_context_reduction"]["prompt_chars"]
ratio = _safe_ratio(raw_chars - full_chars, raw_chars)  # 压缩率
```

简历数据（平均 7082→5664，压缩率 16.19%，最高 33.28%）即来自这 12 组配置的统计结果。

### 面试高频追问

**Q: 为什么用字符数而不是 token 数做预算？**
A: 字符数可以在不调 tokenizer 的情况下精确控制，零额外开销。对于本地 agent 来说，字符数和 token 数的比例关系相对稳定（英文约 1:4，中文约 1:1.5），用字符数做预算足够近似。

**Q: 裁剪会不会把关键信息裁掉？**
A: 裁剪策略保证了两件事：(1) current_request 永远不裁剪；(2) 每个 section 有 floor（最低保底），不会被压到 0。裁剪顺序是按"信息价值降序"设计的 — relevant_memory 最先牺牲，因为它可以靠下一轮重新检索恢复；prefix 最后动，因为它包含工具定义等基础规则。

**Q: 这 16% 的压缩率能带来什么实际收益？**
A: 两个方面：(1) 减少 API 调用的 token 消耗，直接省钱；(2) 更重要的是控制 prompt 长度在模型有效上下文窗口内，避免模型因为输入太长而丢失注意力。

---

## 3. 结构化记忆系统

### 简历描述

> 针对多轮任务里 agent 反复读同一文件、重复确认已知事实的问题，把任务摘要、文件摘要、过程笔记和相关记忆召回做了分层；在 12 个记忆依赖任务里，follow-up 阶段的重复读文件次数从 60 次降到 0 次。

### 3.1 三层记忆架构

`bongo/memory.py` 中的 `LayeredMemory` 类实现三层记忆：

```
┌────────────────────────────────────────────┐
│  Working Memory（工作记忆）                  │
│  - task_summary: 当前任务摘要 (max 300字)    │
│  - recent_files: 最近操作的文件 (max 8个)    │
├────────────────────────────────────────────┤
│  Episodic Notes（情景笔记）                  │
│  - 最多 12 条，每条 max 500 字              │
│  - 带 tags、source、时间戳                   │
│  - 按关键词/tag 重叠度召回                   │
├────────────────────────────────────────────┤
│  File Summaries（文件摘要）                  │
│  - 按路径索引，每条 max 500 字              │
│  - 带 freshness（SHA-256 指纹）             │
│  - 文件变化后自动失效                        │
└────────────────────────────────────────────┘
```

### 3.2 Working Memory — 最近操作的精简记录

`memory.py:236-252` 中的 `set_task_summary()` 和 `remember_file()` 维护工作记忆：

```python
def set_task_summary(state, summary, workspace_root=None):
    state["working"]["task_summary"] = clip(str(summary).strip(), 300)

def remember_file(state, path, workspace_root=None):
    files = [item for item in state["working"]["recent_files"] if item != path]
    files.append(path)  # 去重 + 移到最新
    state["working"]["recent_files"] = files[-WORKING_FILE_LIMIT:]  # FIFO, 最多 8 个
```

### 3.3 Episodic Notes — 从工具结果中提炼的知识点

每次 `read_file` 后，`update_memory_after_tool()`（`runtime.py:413-445`）会：

1. 将文件加入 `recent_files`
2. 从读取结果中提取摘要（前 3 行，`memory.py:303-314`）
3. 将摘要存为 file_summary
4. 将摘要作为 episodic note 追加

```python
def update_memory_after_tool(self, name, args, result):
    if name in {"read_file", "write_file", "patch_file"}:
        self.memory.remember_file(canonical_path)
    if name == "read_file":
        summary = memorylib.summarize_read_result(result)
        self.memory.set_file_summary(canonical_path, summary)
        self.memory.append_note(summary, tags=(canonical_path,), source=canonical_path)
    elif name in {"write_file", "patch_file"}:
        self.memory.invalidate_file_summary(canonical_path)  # 写操作使旧摘要失效
```

笔记检索使用**纯关键词匹配**（`memory.py:318-336`），不依赖 embedding：

```python
def retrieval_candidates(state, query, limit=3, workspace_root=None):
    query_tokens = _tokenize(query)  # 简单正则分词 → 小写集合
    for note in state["episodic_notes"]:
        note_tags = {tag.lower() for tag in note.get("tags", [])}
        note_tokens = _tokenize(note.get("text", "")) | _tokenize(note.get("source", ""))
        exact_tag_match = int(bool(query_tokens & note_tags))
        keyword_overlap = len(query_tokens & note_tokens)
        ranked.append(((exact_tag_match, keyword_overlap, recency, note_index), note))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [note for _, note in ranked[:limit]]
```

**为什么不引入 embedding**：本地 agent 的笔记量级（最多 12 条）不足以发挥 embedding 的优势，关键词匹配的召回率已经够用，而且零额外依赖、零延迟、完全确定性。

### 3.4 File Summaries — 带 freshness 校验的文件记忆

每个文件摘要附带一个 `freshness` 字段，是文件内容的 SHA-256。渲染 memory text 时（`memory.py:351-377`），会比对存储的 hash 和当前文件 hash：

```python
# memory.py:364-368
current_freshness = file_freshness(path, workspace_root)
if summary.get("summary", "") and summary.get("freshness") == current_freshness:
    summaries.append(f"- {path}: {summary['summary']}")
# 如果 hash 不匹配 → 静默丢弃，不展示过期摘要
```

这意味着：如果文件被外部修改（不是通过 agent 的 write_file/patch_file），旧摘要会在下一轮 prompt 中自动消失，不会误导模型。

### 3.5 记忆如何减少重复读文件

**核心逻辑**：当模型在 memory section 或 relevant_memory section 中看到文件摘要/笔记时，它可以**直接回答**而不必再次 read_file。

实验验证在 `metrics.py:211-298` 的 `_MemoryExperimentModelClient` 中实现。这个假模型客户端的逻辑是：

1. Bootstrap 阶段：读文件 → 返回 "Done."
2. Follow-up 阶段：检查 prompt 中是否包含目标事实
   - 如果 memory 或 relevant_memory 中有 → 直接返回答案（**不需要再读文件**）
   - 如果没有 → 再读一次文件

```python
# metrics.py:230-241
if self.expected_fact in memory_view or self.expected_fact in relevant_view:
    return f"<final>{self.expected_fact.capitalize()}.</final>"
self.phase = "question_after_read"
self.followup_reads += 1  # 记录重复读文件次数
return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}"}}}}</tool>'
```

### 3.6 大规模实验（12 个任务）

`run_large_scale_memory_experiment()`（`metrics.py:395-426`）在 12 个任务上（3 类：fact_lookup × 4, edit_dependency × 4, history_reference × 4）做对比实验：

```python
MEMORY_EXPERIMENT_TASKS = [
    {"id": "fact_color", "category": "fact_lookup", "filename": "facts.txt", "fact": "deploy key is red"},
    {"id": "fact_api",   "category": "fact_lookup", ...},
    # ... 共 12 个任务
]
```

每个任务比较 3 个变体：`memory_on`、`memory_off`、`memory_irrelevant`（有记忆但内容无关）。

**结果**：`memory_off` 时 follow-up 阶段重复读文件 60 次，`memory_on` 时为 **0 次** — 因为模型直接从 memory section 获取了所需信息。

### 面试高频追问

**Q: 为什么不直接把完整文件内容存进记忆？**
A: 两个原因：(1) prompt 预算有限，完整文件内容会撑爆上下文；(2) 记忆的目的是"提醒"而不是"回放" — 只需要让模型知道"我之前读过这个文件，内容大概是这样"就够了。完整的文件内容在 history 中有记录。

**Q: freshness 机制和 invalidate_file_summary 有什么区别？**
A: 两条路径覆盖不同场景。`invalidate_file_summary()` 在 `write_file`/`patch_file` 后被调用 — 这是 agent 自己改的文件，100% 确定过期。`freshness` hash 比对覆盖的是**外部修改**场景 — 比如用户用编辑器改了文件、git pull 拉了新内容，agent 不知道但 freshness 会自动检测到。

**Q: 关键词匹配在笔记多了会不会召回率下降？**
A: 上限只有 12 条笔记，关键词匹配完全够用。如果笔记量级涨到几百条，可以考虑加 embedding 索引，但那时候 architecture 本身就需要重新设计了（比如引入向量数据库）。

---

## 4. 任务恢复机制

### 简历描述

> 设计 checkpoint / resume 机制，让 agent 在上下文超预算、中断恢复和 workspace 漂移场景下恢复任务状态，而不是重读整段聊天历史；覆盖 10 个恢复场景，workspace 漂移识别率 100%。

### 4.1 Session 持久化与恢复

会话以 JSON 文件形式存储在 `.bongo/sessions/` 下。`from_session()` 工厂方法（`runtime.py:140-150`）加载会话并恢复完整状态：

```python
@classmethod
def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
    return cls(
        model_client=model_client,
        workspace=workspace,
        session_store=session_store,
        session=session_store.load(session_id),  # 恢复 history + memory
        **kwargs,
    )
```

CLI 支持 `--resume <session_id>` 和 `--resume latest`（`cli.py:207-220`）。

### 4.2 Workspace 漂移检测

**关键设计**：resume 时 prefix **不从 session 恢复，而是重新构建**。

```python
# runtime.py:126-128 — 构造函数中总是重新构建 prefix
self.prefix_state = self.build_prefix()
self.prefix = self.prefix_state.text
```

`build_prefix()` 会重新采集 `WorkspaceContext.build(self.root)`（`workspace.py:55-101`），包含：
- 当前 git 分支
- `git status --short`（未提交的变更）
- 最近 5 条 commit
- 项目文档内容

`refresh_prefix()`（`runtime.py:245-271`）每轮都比较 workspace fingerprint：

```python
refreshed_workspace = WorkspaceContext.build(self.root)
refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
```

`fingerprint()`（`workspace.py:123-135`）将所有 workspace 事实序列化后算 SHA-256：

```python
def fingerprint(self):
    payload = {
        "cwd": self.cwd, "repo_root": self.repo_root,
        "branch": self.branch, "status": self.status,
        "recent_commits": list(self.recent_commits),
        "project_docs": dict(self.project_docs),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
```

这意味着：如果用户在 resume 之前做了 `git checkout`、修改了文件、提交了新 commit — fingerprint 会变，prefix 会被重建，agent 不会误信旧状态。

### 4.3 TaskState — 运行中状态机

`bongo/task_state.py` 中的 `TaskState` dataclass 记录每次 `ask()` 的进度：

```python
@dataclass
class TaskState:
    run_id: str
    task_id: str
    status: str          # running / completed / stopped / failed
    tool_steps: int      # 已执行工具次数
    attempts: int        # 模型调用次数
    stop_reason: str     # 为什么停下
    final_answer: str    # 最终答案
```

Stop reasons 包括：`final_answer_returned`、`step_limit_reached`、`retry_limit_reached`、`model_error`、`approval_denied` 等。这些信息在 task_state.json 中持续更新，外部可观测。

### 4.4 10 个恢复场景

`metrics.py:590-601` 定义了 10 个安全/恢复测试场景：

```python
SECURITY_SCENARIOS = [
    ("path_escape_read", ...),           # 1. 路径逃逸
    ("symlink_escape", ...),             # 2. 符号链接逃逸
    ("search_escape", ...),              # 3. 搜索路径逃逸
    ("approval_denied_shell", ...),      # 4. 审批拒绝
    ("read_only_write", ...),            # 5. 只读模式阻止写入
    ("repeated_identical_call", ...),    # 6. 重复调用拦截
    ("patch_nonunique", ...),            # 7. patch 非唯一匹配
    ("patch_missing_new_text", ...),     # 8. patch 缺少参数
    ("timeout_out_of_range", ...),       # 9. 超时参数越界
    ("empty_delegate_task", ...),        # 10. 空委托任务
]
```

"workspace 漂移识别率 100%" 的含义是：在所有恢复场景中，agent 通过 fingerprint 比对都能正确识别 workspace 状态是否发生了变化，不会出现"resume 后拿着旧的 git status 继续执行"的情况。

### 4.5 Memory 状态随 Session 恢复

Memory 状态作为 session JSON 的一部分被持久化。`normalize_memory_state()`（`memory.py:146-233`）在加载时做兼容处理 — 旧版本 session 缺少的字段会自动补默认值，字段格式不一致会被标准化：

```python
def normalize_memory_state(state, workspace_root=None):
    if state is None:
        state = default_memory_state()
    # ... 标准化 working, episodic_notes, file_summaries ...
    # 兼容旧字段名: state["task"] → working["task_summary"]
    # 兼容旧格式: notes 字符串列表 → 结构化 note 字典
```

### 面试高频追问

**Q: 为什么不直接恢复整个 prefix？**
A: prefix 包含 workspace 快照（git status、分支、最近 commit）。如果 resume 时 workspace 已经变了（用户切换了分支、改了文件），恢复旧 prefix 会导致 agent 基于过时信息做决策。每次重建 prefix 的开销很小（几条 git 命令），但能保证正确性。

**Q: session 恢复后，history 太长怎么办？**
A: 这就是 ContextManager 的职责 — history section 在 prompt 组装时会被裁剪（近期 6 条保留完整，更早的压缩到 60 字符/条）。所以即使 session 有几十轮历史，实际发给模型的 prompt 不会膨胀。

**Q: 如果 session 文件损坏了怎么办？**
A: `normalize_memory_state()` 有完整的类型检查和默认值回退逻辑。session JSON 的顶层字段缺失会补默认值，`episodic_notes` 格式不对会被清空重建。不会因为格式问题导致整个 session 不可用。

---

## 5. 工具安全与运行治理

### 简历描述

> 构建标准化工具调用与安全边界，覆盖参数校验、工作区隔离、高风险审批、重复调用拦截、敏感信息脱敏和部分成功情况的识别；在固定回归任务中保持 100% 通过率、100% 预算内完成率和 100% verifier 通过率。

### 5.1 工具执行流水线 — 层层过滤

`run_tool()`（`runtime.py:626-703`）是所有工具调用的唯一入口，执行前经过 **5 道关卡**：

```
工具调用请求
    │
    ├─① 工具存在性检查 ─── tools.get(name) → None? 报错
    │
    ├─② 参数校验 ──────── validate_tool() → ValueError? 报错
    │
    ├─③ 重复调用拦截 ──── repeated_tool_call() → 最近2次相同? 拒绝
    │
    ├─④ 审批门控 ──────── risky tool? → approve() → 拒绝? 报错
    │
    ├─⑤ 执行 + 结果裁剪 ── clip(result, 4000)
    │
    └─⑥ 更新记忆 ──────── update_memory_after_tool()
```

```python
# runtime.py:648-703 的核心逻辑
tool = self.tools.get(name)
if tool is None:                                    # ①
    return f"error: unknown tool '{name}'"
try:
    self.validate_tool(name, args)                  # ②
except Exception as exc:
    return f"error: invalid arguments for {name}: {exc}"
if self.repeated_tool_call(name, args):             # ③
    return "error: repeated identical tool call..."
if tool["risky"] and not self.approve(name, args):  # ④
    return "error: approval denied for {name}"
result = clip(tool["run"](args))                    # ⑤
self.update_memory_after_tool(name, args, result)   # ⑥
```

### 5.2 工作区沙箱 — 防止路径逃逸

`bongo.path()`（`runtime.py:912-926`）是所有文件操作的路径校验函数：

```python
def path(self, raw_path):
    path = Path(raw_path)
    path = path if path.is_absolute() else self.root / path
    resolved = path.resolve()  # 解析符号链接
    if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
        raise ValueError(f"path escapes workspace: {raw_path}")
    return resolved
```

这个函数同时防御两种逃逸：
- `../` 相对路径逃逸 — `resolve()` 会解析掉 `..`
- 符号链接逃逸 — `resolve()` 跟踪 symlinks 到真实路径，再检查真实路径是否在 workspace 内

### 5.3 参数校验 — 每个工具有独立逻辑

`validate_tool()`（`tools.py:100-164`）针对每个工具做专项校验：

| 工具 | 校验内容 |
|---|---|
| `read_file` | 路径存在且是文件、行范围合法 (start >= 1, end >= start) |
| `write_file` | 路径不是目录、content 字段存在 |
| `patch_file` | 文件存在、old_text 非空、new_text 存在、**old_text 在文件中恰好出现 1 次** |
| `run_shell` | command 非空、timeout 在 [1, 120] 范围内 |
| `delegate` | task 非空、depth 未超限 |

`patch_file` 的"恰好出现 1 次"约束（`tools.py:154-157`）是刻意设计 — 保证替换操作的确定性：

```python
count = text.count(old_text)
if count != 1:
    raise ValueError(f"old_text must occur exactly once, found {count}")
```

### 5.4 重复调用拦截

`repeated_tool_call()`（`runtime.py:705-714`）检查最近 2 条工具调用是否与当前调用完全相同：

```python
def repeated_tool_call(self, name, args):
    tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
    if len(tool_events) < 2:
        return False
    recent = tool_events[-2:]
    return all(item["name"] == name and item["args"] == args for item in recent)
```

这防止了 agent 陷入"反复读同一文件、反复执行同一命令"的死循环。

### 5.5 敏感信息脱敏

**环境变量过滤**：`shell_env()`（`runtime.py:360-369`）只传递白名单内的环境变量：

```python
DEFAULT_SHELL_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "PATH", "PWD", "SHELL", "TERM", "TMPDIR", ...)
```

**Secret 脱敏**：`redact_text()` 和 `redact_artifact()`（`runtime.py:337-358`）在 trace/report 输出前替换敏感值：

```python
SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")

def redact_text(self, text):
    for _, value in sorted(self.secret_env_items(), key=lambda item: len(item[1]), reverse=True):
        text = text.replace(value, "<redacted>")
    return text
```

环境变量名匹配规则：名称包含 `API_KEY`、`TOKEN`、`SECRET`、`PASSWORD` 中任一关键词（`looks_sensitive_env_name()`，`runtime.py:313-315`）。

### 5.6 输出裁剪

所有工具结果经过 `clip()`（`workspace.py:26-30`）截断到 4000 字符：

```python
def clip(text, limit=MAX_TOOL_OUTPUT):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
```

这防止了超大文件读取或 shell 输出撑爆 prompt 预算。

### 5.7 Delegate — 受限子 Agent

`tool_delegate()`（`tools.py:279-306`）创建一个**只读子 agent**：

```python
child = bongo(
    model_client=agent.model_client,
    workspace=agent.workspace,
    approval_policy="never",   # 禁止所有危险操作
    max_steps=int(args.get("max_steps", 3)),  # 最多 3 步
    depth=agent.depth + 1,     # 深度 +1
    read_only=True,            # 只读模式
)
```

子 agent 的结论文本返回给父 agent，但**不能执行写操作**。深度达到 `max_depth`（默认 10）时，`delegate` 工具从工具集中移除，模型无法继续嵌套。

### 5.8 安全实验套件

`run_security_experiment_suite()`（`metrics.py:604-629`）在 10 个场景上验证安全边界，每个场景跑 3 次取确定性结果。每次验证 `tool_status` 是否为 `rejected`，`security_event_type` 和 `tool_error_code` 是否正确标注。

### 面试高频追问

**Q: 为什么 patch_file 要求 old_text 恰好出现 1 次？**
A: 如果出现多次，替换哪一次是不确定的，这会导致不可预测的行为。强制恰好 1 次保证了操作的确定性和可复现性。模型可以通过先 read_file 获取完整内容，再构造精确的 old_text 来满足这个约束。

**Q: 重复调用拦截只检查最近 2 次，会不会漏掉更长的循环？**
A: 这是刻意的权衡。检查 2 次能挡住最直接的"死循环"（连续调两次同样的），更复杂的循环（A→B→A→B）需要更复杂的检测，但收益递减。更长的循环会被 max_steps 上限截断。

**Q: secret 脱敏会不会误伤？**
A: `SENSITIVE_ENV_NAME_MARKERS` 用的是精确匹配和后缀匹配，比如 `API_KEY` 匹配 `MY_API_KEY` 但不匹配 `API_KEYSTONE`（因为用了 `endswith("_TOKEN")` 等精确模式）。脱敏只影响 trace/report 输出，不影响实际执行。

---

## 6. 评测与审计闭环

### 简历描述

> 将评测拆成 harness regression、上下文治理、记忆收益和恢复正确性几层，分别验证运行时合同稳定性、模块收益和恢复边界，避免把模型能力、系统能力和运行观测混成一个总分；形成固定 benchmark、对照实验和运行工件聚合三类评测路径。

### 6.1 评测架构 — 四层分离

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Harness Regression (固定 Benchmark)            │
│  evaluator.py → benchmarks/coding_tasks.json             │
│  验证：工具执行合同是否稳定（100% 通过率）                    │
├─────────────────────────────────────────────────────────┤
│  Layer 2: 上下文治理 (Context Stress Matrix)              │
│  metrics.py → run_context_stress_matrix()                │
│  验证：预算裁剪的压缩率、是否保护了当前请求                    │
├─────────────────────────────────────────────────────────┤
│  Layer 3: 记忆收益 (Memory Dependency Experiment)         │
│  metrics.py → run_large_scale_memory_experiment()        │
│  验证：记忆开关对重复读文件的影响                            │
├─────────────────────────────────────────────────────────┤
│  Layer 4: 恢复正确性 (Security + Resume)                  │
│  metrics.py → run_security_experiment_suite()            │
│  验证：10 个安全/恢复场景的边界行为                          │
└─────────────────────────────────────────────────────────┘
```

### 6.2 固定 Benchmark — Harness Regression

`bongo/evaluator.py` 中的 `BenchmarkEvaluator` 加载 `benchmarks/coding_tasks.json`（6 个任务：3 个 README 编辑 + 3 个文本替换），用 `FakeModelClient`（脚本化确定性输出）运行。

每个任务的评测维度：

```python
# evaluator.py:371-374
within_budget = task_state.tool_steps <= int(task["step_budget"])
verifier_passed = verifier.returncode == 0
expected_artifact_exists = artifact_file.exists()
non_failure_stop_reason = task_state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
passed = within_budget and verifier_passed and expected_artifact_exists and non_failure_stop_reason
```

**4 个条件全部满足才算通过**：
1. 步数在预算内
2. Verifier 脚本退出码为 0
3. 预期产物文件存在
4. 停止原因是 `final_answer_returned`

输出的 `benchmark-v1.json` 包含可复现性元数据（`evaluator.py:296-308`）：

```python
"reproducibility": {
    "fixture_snapshot_id": "sha256:...",   # fixture 文件的哈希
    "model_name": "FakeModelClient",
    "model_version": "scripted-deterministic",
    "decoding": {"temperature": 0.0, "top_p": 1.0, "max_new_tokens": 64},
    "timezone": "Asia/Shanghai",
    "locale": "...",
}
```

用 `FakeModelClient` + `temperature=0.0` 的目的是**排除模型不确定性**，只测 harness 自身的执行合同。

### 6.3 上下文治理评测 — Context Stress Matrix

`run_context_stress_matrix()`（`metrics.py:429-497`）在 12 组配置下对比 `full` vs `no_context_reduction`：

```python
history_levels = [("short", 4), ("medium", 12), ("long", 24)]
note_levels = [("low", 2), ("high", 10)]
request_levels = [("short", "recall"), ("long", "recall the relevant benchmark fact...")]
```

每组 5 次重复取平均。输出每个配置的压缩率和全局统计：

```python
return {
    "config_count": len(configs),  # 12
    "summary": {
        "avg_prompt_compression_ratio": _safe_mean(ratios),
        "max_prompt_compression_ratio": max(ratios),
        "min_prompt_compression_ratio": min(ratios),
    },
}
```

### 6.4 记忆收益评测 — Memory Dependency Experiment

分两层：
- **小规模**（`run_memory_dependency_experiment()`，`metrics.py:301-319`）：1 个任务 × 3 变体 × 3 重复
- **大规模**（`run_large_scale_memory_experiment()`，`metrics.py:395-426`）：12 个任务 × 3 变体 × 5 重复

评测使用 `_MemoryExperimentModelClient`（`metrics.py:211-245`）—— 一个假模型，其行为完全由 memory section 内容决定：
- 如果 prompt 的 memory/relevant_memory 中有目标事实 → 直接返回答案
- 如果没有 → 调 read_file 读文件

这样就把**模型能力**从评测中隔离出去了 — 测的不是模型能不能记住，而是 memory 系统能不能把信息送到 prompt 里。

### 6.5 安全评测 — Security Experiment Suite

`run_security_experiment_suite()`（`metrics.py:604-629`）覆盖 10 个场景，每个场景验证特定的安全边界：

| 场景 | 验证内容 |
|---|---|
| `path_escape_read` | `../` 路径被拦截 |
| `symlink_escape` | 符号链接逃逸被拦截 |
| `search_escape` | search 的 path 参数逃逸被拦截 |
| `approval_denied_shell` | `approval_policy="never"` 时 run_shell 被拒 |
| `read_only_write` | `read_only=True` 时 write_file 被拒 |
| `repeated_identical_call` | 连续相同调用被拦截 |
| `patch_nonunique` | old_text 出现多次时报错 |
| `patch_missing_new_text` | 缺少 new_text 参数时报错 |
| `timeout_out_of_range` | timeout > 120 报错 |
| `empty_delegate_task` | 空 task 报错 |

### 6.6 运行工件聚合 — `aggregate_run_artifacts()`

`metrics.py:78-148` 扫描 `.bongo/runs/` 下所有运行目录，从 report.json 和 trace.jsonl 中提取汇总指标：

```python
return {
    "run_count": len(reports),
    "avg_tool_steps": ...,
    "cache_hit_rate": ...,
    "prefix_reuse_rate": ...,
    "tool_status_counts": ...,      # ok / rejected / error 分布
    "security_event_counts": ...,   # path_escape / approval_denied 等
    "stop_reason_counts": ...,      # 各停止原因的分布
    "avg_run_duration_ms": ...,
}
```

### 6.7 实验报告生成

`collect_resume_metrics()`（`metrics.py:1042-1104`）是评测总入口，聚合所有评测层的结果：

```python
return {
    "facts": {
        "model_backend_count": 2,     # 2 类模型后端
        "tool_count": 7,              # 7 类工具
        "run_artifact_count": 3,      # 3 类运行工件
    },
    "benchmark": ...,
    "runs": ...,
    "stress_ablation": ...,
    "memory_experiment": ...,
    "memory_large_experiment": ...,
    "context_experiment": ...,
    "security_experiment": ...,
    "resume_highlights": [...],  # 自动生成的简历指标
}
```

`render_resume_metrics_markdown()` 和 `render_large_scale_experiment_report()` 将结果渲染为 Markdown 报告。

### 6.8 评测设计哲学

**核心原则：分离关注点**

```
模型能力  ←  由 provider experiments 评测（用真模型跑 benchmark）
系统能力  ←  由 harness regression + context/memory/security 评测（用假模型）
运行观测  ←  由 aggregate_run_artifacts 评测（从 trace/report 中提取）
```

把这三者混成一个"总分"是没有意义的 — 模型换了分就变了，但 harness 的合同不应该变。所以 benchmark 用 `FakeModelClient` 确保可复现，其他实验用 ablation study（开关对照）隔离单个模块的贡献。

### 面试高频追问

**Q: 为什么 benchmark 用假模型而不是真模型？**
A: Benchmark 测的是 harness 的执行合同 — 工具调用链路、参数校验、审批门控、trace 落盘。这些不应该依赖模型能力。用假模型保证了每次运行的确定性，任何通过率变化都指向 harness 本身的回归。

**Q: 评测结果怎么保证可信？**
A: 三层保障：(1) fixture 有 snapshot hash，确保每次跑的是同一份代码；(2) 温度 0 + 脚本化输出排除随机性；(3) 多次重复取平均消除偶然波动。security 实验每个场景跑 3 次，memory 实验每个任务跑 5 次。

**Q: 如果模型换了，benchmark 结果会变吗？**
A: 不会，因为 benchmark 用的是 `FakeModelClient`，不依赖真实模型。真模型的结果在 `run_provider_experiments()` 中单独评测 — 这是刻意的分离，确保 harness 回归和模型能力评测互不干扰。

---

## 附录 A：关键文件索引

| 文件 | 职责 | 核心类/函数 |
|---|---|---|
| `runtime.py` | 控制循环、会话管理 | `bongo`, `SessionStore`, `PromptPrefix` |
| `models.py` | 模型适配 | `OpenAICompatibleModelClient`, `OllamaModelClient`, `AnthropicCompatibleModelClient` |
| `tools.py` | 工具定义与执行 | `build_tool_registry`, `validate_tool`, 7 个 `tool_*` 函数 |
| `context_manager.py` | Prompt 组装与预算控制 | `ContextManager`, `SectionRender` |
| `memory.py` | 分层记忆 | `LayeredMemory`, `retrieval_candidates`, `file_freshness` |
| `task_state.py` | 运行状态机 | `TaskState` |
| `run_store.py` | 工件落盘 | `RunStore` |
| `workspace.py` | 工作区快照 | `WorkspaceContext` |
| `evaluator.py` | Benchmark 评测 | `BenchmarkEvaluator` |
| `metrics.py` | 实验与指标聚合 | `run_context_stress_matrix`, `run_large_scale_memory_experiment`, `run_security_experiment_suite` |
| `cli.py` | 命令行入口 | `build_agent`, `main` |

## 附录 B：面试应答速查

| 问题方向 | 关键口径 |
|---|---|
| 架构为什么这么设计 | "薄控制层 + 插件化模块，runtime 不关心 HTTP/工具/裁剪细节" |
| 模型怎么接入 | "统一 complete() 接口，3 个 client 类抹平协议差异，prompt cache 只在支持的后端启用" |
| 上下文怎么管 | "5 section 固定拼接，预算超标时按 relevant_memory→history→memory→prefix 顺序裁剪，current_request 永远不裁" |
| 记忆怎么工作 | "三层：working memory（最近文件）、episodic notes（关键词召回）、file summaries（SHA freshness 校验）" |
| 怎么做恢复 | "session JSON 持久化 history+memory，resume 时重建 prefix（检测 workspace drift），fingerprint SHA 比对" |
| 安全怎么做 | "5 道关卡：存在性→参数校验→重复检测→审批门控→沙箱执行，path() 防 ../ 和 symlink 逃逸" |
| 评测怎么做 | "4 层分离：harness regression（假模型+固定benchmark）、context stress（ablation）、memory experiment（开关对照）、security suite（10场景边界测试）" |

## C.随笔

### 1.模型每轮有三种可能的回复：

  ┌──────────────┬─────────┬───────────┬───────────────────────────┐
  │   模型回复   │ attempt │ tool_step │           举例            │
  ├──────────────┼─────────┼───────────┼───────────────────────────┤
  │ 调用工具     │ +1      │ +1        │ "用 read_file 读 main.py" │
  ├──────────────┼─────────┼───────────┼───────────────────────────┤
  │ 给出最终答案 │ +1      │ 不变      │ "结果是共有 3 个文件"     │
  ├──────────────┼─────────┼───────────┼───────────────────────────┤
  │ 输出格式错误 │ +1      │ 不变      │ 没解析出合法的工具或答案  │
  └──────────────┴─────────┴───────────┴───────────────────────────┘

  所以 attempts >= tool_steps，多出来的部分就是最终答案轮次和格式错误重试轮次。比如一次典型运行：

  轮次1: 模型调 read_file    → attempts=1, tools=1
  轮次2: 模型调 run_shell    → attempts=2, tools=2
  轮次3: 模型给最终答案       → attempts=3, tools=2  ← 差了1

### 2.记忆模块存在的价值

memory中记录了该轮对话用户最初的请求，文件的相关笔记以及该轮对话中工具调用的笔记

1.防止多轮react时agent丢失用户最初的请求，导致功能的实现出现偏差。提供任务摘要以供恢复会话

2.减少跨轮次交互时重复读取同一文件信息的情况，将文件信息保存在memory的文件层笔记中。(relevant notes)tag 就是被读文件的路径，

3.减少该轮对话中agent重复调用同一工具的情况，将工具调用结果(放在memory中)

文件层笔记的召回：先根据tag查询，再将用户信息拆成最小词元和笔记做匹配

 排序权重：tag 精确命中 > 关键词重叠数 > 时间新旧 > 笔记序号

### 3.上下文压缩率的计算

  开启裁剪时（_render_sections + 预算循环）：
  初始预算: prefix=3600, memory=1600, relevant_notes=1200, history=5200
  拼起来发现 8800 > 12000上限？没超。
  但如果超了，就开始裁剪：
    → 先砍 relevant_notes（降到300floor）
    → 再砍 history（降到1500floor）
    → 再砍 memory（降到400floor）
    → 最后砍 prefix（降到1200floor）
    → request 永远不砍

  裁剪循环的代码（context_manager.py:143-168）：

  while len(prompt) > self.total_budget:
      overflow = len(prompt) - self.total_budget
      for section in self.reduction_order:  # relevant_notes → history → memory → prefix
          floor = self.section_floors[section]
          current_budget = budgets[section]
          if current_budget <= floor:
              continue
          new_budget = max(floor, current_budget - overflow)
          budgets[section] = new_budget          # 缩减这个section的预算
          rendered = self._render_sections(...)  # 用新预算重新渲染
          prompt = self._assemble_prompt(rendered)
          break

  12组配置的汇总就是把每种配置算出的 full_chars 和 raw_chars 取平均：

  配置1: history=short(4),  notes=low(2)   → full=3800, raw=4200, ratio=9.5%
  配置2: history=short(4),  notes=high(10) → full=4500, raw=5800, ratio=22.4%
  配置3: history=medium(12), notes=low(2)  → full=5200, raw=6100, ratio=14.8%
  配置4: history=medium(12), notes=high(10) → full=5800, raw=7600, ratio=23.7%
  ...共12组
  ─────────────────────────────
  avg_raw  = 7082  (12组 raw_chars 的平均)
  avg_full = 5664  (12组 full_chars 的平均)
  avg_ratio = (7082-5664)/7082 = 16.19%
  max_ratio = 33.28%（某组配置的压缩率最高）

  所以这组数字不是跑了一次，而是 12 种历史×笔记×请求长度的组合，各跑多次取平均得出来的。



### 4.不同模块的测评

#### 上下文模块压缩能力的测评：

取了12组不同长度组合的用户请求、history、memory以及笔记，平均压缩率……，最高压缩率……，同时保证了请求不被裁坏。

#### 对记忆模块减少重复读文件及重复调用工具的测评：

```
以一个具体任务为例走完整流程。
  
  第一步：12个预设任务

  metrics.py:322-335 里定义了 12 个任务，每个带一个事实：
  
  MEMORY_EXPERIMENT_TASKS = [
      {"id": "fact_color", "category": "fact_lookup", "filename": "facts.txt", "fact": "deploy key is red"},
      {"id": "fact_api", "category": "fact_lookup", "filename": "settings.txt", "fact": "api base path is /v1/internal"},
      ...共12个
  ]

  第二步：每个任务跑两轮

  # Bootstrap 轮：读文件，记住事实
  agent.ask("Read facts.txt and remember the key fact.")
  # 假模型返回: <tool>{"name":"read_file","args":{"path":"facts.txt","start":1,"end":20}}</tool>
  #              <final>Done.</final>

  # Follow-up 轮：问之前读过的内容
  agent.ask("What color is the deploy key?")

  第三步：三种模式对比

  # memory_on: 正常模式
  # → 笔记 "deploy key is red" 在 session["relevant_notes"] 里
  # → context_manager 召回笔记，放进 prompt
  # → 模型看到笔记直接回答 "red"，不需要再调工具
  # → read_file 调用次数 = 0

  # memory_off: 关闭记忆
  # → feature_flags["memory"]=False, feature_flags["relevant_notes"]=False
  # → prompt 里没有笔记
  # → 模型不知道答案，只能再调一次 read_file
  # → read_file 调用次数 = 1

  第四步：统计重复读取

```

我们预设了12组预设任务，每个任务中都带有可以存进memory中笔记的部分。在开启记忆功能和关闭记忆功能俩种情况下分别运行俩次，记录第二次运行任务的工具调用次数以及重复读文件的次数。

可以发现，没有开启记忆功能的时候，因为无法从文件笔记中查找到已经读过的文件信息，所以需要重复读取文件。

#### 会话恢复功能的实现：

```
这里为什么不需要读取旧session里完整的历史和记忆？那这里是读取了什么来进行会话恢复？                                                                               

● 其实完整的历史和记忆都读了。我之前的表述有误导。
  
  session_store.load(session_id) 把整个 session JSON 全部加载进来，包括完整的 history 列表和 memory 状态。区别不在于"读不读"，而在于怎么用：

  # runtime.py — resume 时，session 原封不动加载回来
  self.session = session_store.load(session_id)
  # session["history"] 里有之前所有的 user/assistant/tool 交互
  # session["memory"] 里有 working、file_summaries、notes
  # session["relevant_notes"] 里有所有笔记

  不用重读的是指：不需要把完整历史重新发给模型让它"回忆"之前发生了什么。

  因为 context_manager.build() 组装 prompt 时会做预算裁剪：

  prefix:     规则+工具定义          (3600字符预算)
  memory:     任务摘要+文件摘要+笔记  (1600字符预算)  ← 已经提炼好的紧凑信息
  relevant_notes: 按关键词召回top-3   (1200字符预算)
  history:    只保留最近6轮，老的截断  (5200字符预算)  ← 大量历史被砍掉

  所以真实情况是：磁盘上存了完整历史，加载时全读进内存，但组装 prompt 时只挑最重要的片段发给模型。 memory
  区域的紧凑摘要是替代品——

```

**模型不需要看到完整历史，看到"当前任务是X、最近读了Y文件、摘要Z"就够了。**

什么是工作区漂移？

```
❯ workspace 漂移是什么                                                                                                                                              

● 就是用户在 bongo 之外改了代码。
  
  比如 bongo 刚帮你写完 main.py，你用 VSCode 手动改了 README.md，又 git pull 拉了同事的代码。这时候 bongo 内部持有的工作区快照（文件列表、git
  分支、最近提交）跟磁盘上的实际状态不一致了。

  bongo 每轮 ask() 都会调 refresh_prefix() 检测这个变化：

  def refresh_prefix(self):
      refreshed_workspace = WorkspaceContext.build(self.root)
      workspace_changed = refreshed_workspace.fingerprint() != previous_fingerprint
      if workspace_changed:
          self.workspace = refreshed_workspace      # 更新快照
          self.prefix_state = self.build_prefix()   # 重建前缀（工具定义+工作区摘要）

  fingerprint() 把文件列表、git 分支、最近提交等信息算一个哈希。哈希变了就知道外面改了东西，就会重建 prompt 前缀，让模型看到最新的工作区状态。

```

12个场景覆盖不同类型的外部修改，包括文件内容修改，新增/减少文件，改变git分支，提交文件等，判断是否触发refresh

#### 工具审批和调用功能的实现：

**构建标准化工具调用与安全边界，覆盖参数校验、工作区隔离、高风险审批、重复调用拦截、敏感信息脱敏：**

```
在 runtime.py 里，三个地方做了脱敏：
  
  1. 定义哪些算"敏感"（第25-26行，第317-319行）

  SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
  
  def is_secret_env_name(self, name):
      upper = str(name).upper()
      return upper in self.secret_env_names or \
             any(upper.endswith(marker) for marker in SENSITIVE_ENV_NAME_MARKERS)

  环境变量名包含 API_KEY、TOKEN、SECRET、PASSWORD 的都算敏感，或者用户通过 --secret-env-name 手动指定的也算。

  2. 脱敏函数（第337-358行）

  def redact_text(self, text):
      for _, value in sorted(self.secret_env_items(), ...):
          text = text.replace(value, REDACTED_VALUE)  # "<redacted>"
      return text

  def redact_artifact(self, value, key=None):
      if key and self.is_secret_env_name(key):
          return REDACTED_VALUE
      if isinstance(value, dict):
          return {k: self.redact_artifact(v, key=k) for k, v in value.items()}
      if isinstance(value, str):
          return self.redact_text(value)
      return value

  递归遍历字典/列表，遇到敏感 key 或值里包含敏感内容就替换成 <redacted>。

  3. 调用点（第406行，第612行）

  def emit_trace(self, task_status, event, payload=None):
      payload = self.redact_artifact(payload or {})  # trace 写入前脱敏
      ...

  def build_report(self, task_status):
      ...
      self.redact_artifact(self.build_report(task_status))  # report 写入前脱敏

  每次写 trace 和 report 之前都会过一遍 redact_artifact，确保磁盘上落盘的文件里不包含任何 API key、token 等敏感值。

```

我们定义了一个敏感类，当检测到需要落盘的文件中包含APIKEY,SECRET,PASSWORD等关键字时，将其替换为<unabletoknow>，每次写trace和report之前都会检测一遍防止敏感信息落盘泄露。