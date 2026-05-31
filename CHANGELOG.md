# 更新日志

## 2026-05-30

### CLI 命令优化
- `/note` 和 `/mistake` 交互界面新增 `/q` 返回主菜单
- 新增 `/note del <关键词>` 命令，支持按关键词直接删除笔记
- 更新 `/help` 帮助文本，补充新命令说明

### 笔记存储修复
- 修复 MCP `add_note` 工具保存笔记时内容含 `## ` 前缀被拆分为多条笔记的问题
- `add_note` 现在会转义内容中的 `## ` 前缀，防止被解析为新笔记标题
- 合并历史中被拆分的 JUC 锁升级机制笔记

### /ask 链路重构
- `/ask` 启动时先选择信任路径，临时限制 `agent.root` 为选定路径
- 所有文件操作不能超出选定路径范围
- 保留原 agent 的用户画像、历史、记忆、完整 prefix
- `/ask` 默认使用 `auto` 审批策略（信任路径已限制范围）
- 非危险工具（read_file、list_files、search 等）自动通过审批
- 危险工具（write_file、patch_file、run_shell）仍需人工确认

### 性能优化 v1

优化内容：
1. 审批策略：`/ask` 默认 `auto` 审批，消除人工确认等待
2. Prefix 提示：增加"先读后写"规则，减少无效工具调用
3. Prompt 重建：prefix 不变时复用，减少 hash 计算开销

Bug 修复：`approve()` 中 `self.tool_specs` 改为 `self.tools`

测试结果（mimo-v2.5-pro，本地 Ollama，任务：CC/README.md 末尾添加一行）：

| 场景 | 耗时 | 工具调用 | 模型调用 | 状态 |
|---|---|---|---|---|
| 优化前（ask审批） | 444秒 | N/A | N/A | 卡在审批循环 |
| 优化后（auto+提示） | 145秒 | 8次 | ~9轮 | 成功 |

问题诊断：
- 模型首次尝试直接写文件，违反"先读后写"规则
- 写入成功后读取 3 次验证，应只验证 1 次
- 被拦截后仍重试，浪费轮次

### Prompt 结构优化 v2（借鉴 CC 上下文工程）

优化内容：
1. **Prefix 稳定化**：`build_prefix()` 移除 `scoped_path`，prefix 只含静态内容（身份+规则+工具）。新增 `workspace` section 由 context_manager 动态注入 `agent.root`。`/ask` 不再重建 prefix。
2. **工具结果瘦身**：`CLEAN_TOOL_RESULT_AGE` 10→4，read_file 超 2000 字符裁剪为前 20 行预览。
3. **压缩后摘要保留**：`file_summaries` 独立于 history，`compact_history` 不影响 memory section。

Prompt 结构变化：`prefix → workspace → memory → history → current_request`

新增规则：`After write/patch, read once to verify, then return final.`

变更文件：`runtime.py`、`context_manager.py`、`cli.py`、`scripts/test_perf.py`

测试结果（mimo-v2.5-pro，Anthropic API，任务：CC/README.md 末尾添加一行）：

| 场景 | 耗时 | 工具调用 | 模型调用 |
|---|---|---|---|
| v1（auto+提示） | 145秒 | 8次 | ~9轮 |
| v2（prefix静态+裁剪） | 100秒 | 3次 | 4轮 |

改善：耗时 ↓31%，工具调用 ↓62.5%，模型调用 ↓55.6%

最佳流程：read_file(10ms) → write_file(6ms) → read_file(25ms) → final，共 3 轮工具 + 4 轮模型

观察：模型思考占 99.9% 耗时（99.9s/100s），工具执行仅 130ms。偶有中文编码异常（API 端问题）。

### 结构化 API 重构

背景：对比 Claude Code 和 bongo 调用同一模型的速度差异，发现 CC 使用结构化 API（system 字段分离），而 bongo 将所有内容拼接为单条 prompt。

优化内容：
1. **三个 ModelClient 统一支持结构化调用**：`complete()` 新增 `system`/`tools`/`messages` kwargs
2. **Anthropic**：使用 `system` 参数 + structured messages（tool_use/tool_result 块）
3. **OpenAI**：使用 Responses API 的 `input` 数组，支持 system role
4. **Ollama**：优先使用 `/api/chat` 结构化消息，回退到 `/api/generate`
5. **runtime._build_structured_params**：将 prompt 拆为 system=prefix + user=其余部分

设计决策：不传 `tools` 数组——工具定义已在 prefix 文本中，避免模型在原生工具格式和 bongo `<tool>` 标签间混淆。

测试结果（mimo-v2.5-pro，Anthropic API，同一任务）：

| 方案 | 耗时 | 工具调用 | 结果 |
|---|---|---|---|
| system+单user消息 | 135-150s | 10次 | 模型反复读文件不写入 |
| system+结构化messages | 65-112s | 0次 | 模型卡在读循环 |
| system+tools数组 | 3.6s | 0次 | 模型返回文本计划而非工具调用 |
| 原方案（全拼prompt） | 100s | 3次 | 正常完成 |

结论：mimo-v2.5-pro 对 system 字段分离的响应不稳定，原方案（全拼 prompt）反而最可靠。结构化 API 代码已保留，向下兼容——无 kwargs 时自动回退到原逻辑。后续可针对不同模型测试最优策略。

变更文件：`models.py`、`runtime.py`

### 工具协议重构：纯文本 → 原生 tool_use

背景：bongo 的工具调用是自定义纯文本协议（`<tool>` 标签），与 API 原生 tool_use 不兼容，导致结构化 API 调用后模型行为不稳定。重构为 API 原生 tool_use 协议，与 Claude Code 调用方式对齐。

改动：
1. **`build_prefix()`**：移除工具定义和 `<tool>`/`<final>` 格式说明，prefix 只含身份+行为规则
2. **`_build_structured_params()`**：返回真实 `tools` 数组 + 多轮结构化 messages（不再拼接单条文本）
3. **ModelClient `complete()`**：三个客户端在有 tools 参数时，从响应中提取 `tool_use` 块返回 dict；无 tool_use 则返回文本
4. **`parse()`**：改为接收结构化 dict（tool_use）或纯文本（final），不再解析 `<tool>` 标签
5. **`ask()`**：记录结构化 history——assistant 消息为 `tool_use` 块列表，tool 消息含 `tool_use_id`
6. **`context_manager._render_history_item()`**：支持 content 为 list 格式
7. **`convert_history_to_messages()`**：结构化 history 直接透传，不再需要 `_split_assistant_content()`
8. **清理**：删除 `TOOL_EXAMPLES`、`parse_xml_tool`、`parse_attrs`、`extract`、`extract_raw` 等旧代码
9. **工具参数描述**：所有工具 spec 新增 `param_descriptions` 字段，注入 JSON Schema 的 `description` 属性

协议闭环：`tools数组 → 模型返回tool_use → 执行 → tool_result → 模型继续 → ... → 纯文本输出 = 结束`（与 Claude Code 一致）

关键修复：单条 user 消息会导致模型只读不写（read loop）；改为多轮结构化 messages 后模型行为正常。

变更文件：`runtime.py`、`models.py`、`context_manager.py`、`tools.py`、`evaluator.py`、`metrics.py`、`tests/test_agent.py`、`tests/test_tier_integration.py`

测试：19/19 通过

性能测试（mimo-v2.5-pro，Anthropic API，任务：CC/README.md 末尾添加一行）：

| 方案 | 耗时 | 工具调用 | 模型调用 |
|---|---|---|---|
| v2（prefix静态+裁剪+纯文本协议） | 100s | 3次 | 4轮 |
| v3（原生tool_use+多轮messages） | 21s | 5次 | 6轮 |

改善：耗时 ↓79%，工具执行仅占 ~130ms，模型思考占 99% 耗时。

### /ask 三种文档模式
- `/ask` 新增文档类型选择菜单：信任路径、笔记、错题
- 笔记模式：列出所有笔记，进入交互循环，支持用编号引用文档进行读写
- 错题模式：列出所有错题，进入交互循环，支持用编号引用文档进行读写
- 信任路径模式：选择路径后列出文件，进入交互循环
- 交互循环（`/ask>` 提示符）：agent 通过 tool_use 链路处理用户请求，`/q` 返回文档类型选择
- 文档列表注入 agent memory，agent 知道有哪些文件可用
- 两层 `/q`：交互循环 → 文档类型选择 → 主菜单
- 修复 Windows subprocess GBK 编码错误（`run_shell` 和 `search` 加 `encoding="utf-8"`）
- 更新 `/help` 帮助文本

变更文件：`cli.py`、`tools.py`

## 2026-05-31

### /practice 上下文解耦

背景：`/practice` 和 `/ask` 共用 agent 的 memory/session，但 practice 是无状态批处理管道（出题→判分→总结），不需要 ReAct 的 memory/history/prefix。

改动：
1. **`PracticeContext` 类**：独立上下文，只持有 `model_client`，提供 `complete()`、`grade()`、`summarize()` 方法
2. **`GRADE_PROMPT_TEMPLATE`**：提取判分 prompt 为常量，消除 `_run_practice_plan_execute` 和 `_run_practice_review` 之间的重复
3. **`_clean_model_output()`**：提取思考标签/代码块清理逻辑为公共函数
4. **`_parse_grade_result()`**：提取评分结果解析为公共函数
5. **移除 `agent.memory` 依赖**：practice 中的关联错题检索改为直接查 `user_profile.get_mistakes_index()`，不再经过 agent.memory
6. **函数签名变更**：`_run_practice_plan_execute(agent, ...)` → `_run_practice_plan_execute(ctx, ...)`，`_run_practice_review(agent, ...)` → `_run_practice_review(ctx, ...)`

上下文对比：

| | /ask | /practice |
|---|---|---|
| 范式 | ReAct（观察→思考→行动→循环） | Plan-and-Execute（出题→判分→总结） |
| 上下文 | prefix + memory + history + request | 阶段独立，无 history，无 memory |
| 工具调用 | 有，模型决定调什么 | 无，流程固定 |
| 状态 | 跨轮保持 | 无状态 |

变更文件：`cli.py`

测试：19/19 通过

### /ask Memory 渐进式披露重构

背景：旧设计中 `relevant_notes` 写入后从未被读取（死数据），`recent_files` 只存路径不含内容，history 压缩后文件内容丢失导致模型重复读取同一文件。

设计思路：静态 + 动态分离。prefix 只渲染一次（静态），memory 根据 `/ask` 模式动态切换。渐进式披露：轻量索引始终存在，完整内容按需加载到 `loaded`（最多 5 个），淘汰最旧。

改动：
1. **`memory.py`**：
   - 新增 `ASK_MODE_LOADED_LIMIT = 5`、`ASK_MODE_INDEX_LINES = 3`
   - 新增 `default_ask_mode_state()`：`mode` / `original_request` / `index` / `loaded`
   - 新增 `populate_index()`：用文档列表填充轻量索引
   - 新增 `load_document()`：加载完整内容到 `loaded`，超限淘汰最旧
   - 新增 `update_index_summary()`：更新索引摘要
   - 新增 `generate_file_summary()`：读前 N 行生成摘要
   - 新增 `render_ask_memory()`：渲染 ask_mode 专用 memory 文本
   - `render_memory_text()` 优先检查 `ask_mode`，有则委托 `render_ask_memory()`
   - `LayeredMemory` 新增 `populate_index()`、`load_document()`、`update_index_summary()` 方法
   - `EPISODIC_NOTE_LIMIT` 标记为已弃用

2. **`runtime.py`**：
   - 移除 `relevant_notes` 初始化和 `append_note()` 调用
   - `update_memory_after_tool()` 分支：ask_mode 走 `_update_ask_mode_after_tool()`
   - 新增 `_update_ask_mode_after_tool()`：匹配文件到索引条目，read_file 时加载到 loaded
   - 新增 `_fork_summary()`：fork 子线程生成文件摘要并更新索引

3. **`cli.py`**：
   - 删除 `_build_doc_list_context()`
   - `_ask_interactive_loop()`：设置 `ask_mode.mode` 和 `ask_mode.original_request`，退出时清理
   - `_ask_with_notes()`：调用 `agent.memory.populate_index(items)` 填充索引
   - `_ask_with_mistakes()`：调用 `agent.memory.populate_index(items)` 填充索引
   - `_ask_with_trusted_path()`：调用 `agent.memory.populate_index(items)` 填充索引

Memory 结构（`/ask` 模式）：
```
ask_mode:
  mode: "notes" / "mistakes" / "trust_path"
  original_request: 用户首次提问
  index: [{id, label, summary}, ...]  # 轻量，始终存在
  loaded: {doc_id: {path, content, loaded_at}}  # 完整内容，最多 5 个
```

工作流程：用户选择模式 → populate_index 填充索引 → agent 看到索引 → 调用 read_file → 自动加载到 loaded + fork 摘要 → agent 看到完整内容 → 读写操作

测试：19/19 通过

### 移除多模型层级路由（Tier Manager）

背景：多模型路由功能已不再使用，保留会增加代码复杂度和维护成本。

改动：
1. **删除 `bongo/tier_manager.py`**：任务难度分类（`classify_task`）和 `TierManager` 类
2. **删除 `tests/test_tier_integration.py`**、`tests/test_tier_manager.py`：相关测试
3. **`bongo/__init__.py`**：移除 `TierManager`、`classify_task` 导出
4. **`bongo/runtime.py`**：
   - 移除 `tier_manager` 参数、`locked_model`、`_original_model_client`
   - 移除 `ask()` 中的多层级路由和二级任务回退逻辑
   - 移除 `_ask_with_fallback()`、`lock_model()`、`unlock_model()`、`model_status()` 方法
5. **`bongo/cli.py`**：移除 `/model` 命令（REPL 和帮助文本）

测试：4/4 通过

### /ask ReAct 步骤展示 + /practice 出题动画

改动：
1. **`bongo/utils.py`**：新增 `Spinner` 类，终端 spinner 动画（`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`），支持 `with Spinner("message"):` 上下文管理器
2. **`bongo/runtime.py`**：
   - `ask()` 主循环新增 `react_round` 计数器
   - 每轮打印 `[Round N] Thinking...`（模型推理前）
   - 工具调用时打印 `[Round N] Acting: tool_name(arg1=val1, ...)` 
   - 工具执行后打印 `[Round N] Observing: result_preview`
   - 最终答案时打印 `[Round N] Done`
3. **`bongo/cli.py`**：
   - `PracticeContext.complete()` 和 `grade()` 新增 `spinner_message` 参数
   - 出题时显示 spinner `正在生成 N 道题目...`
   - 评分时显示 spinner `正在评分...`
   - 总评时显示 spinner `正在生成总评...`

测试：4/4 通过

### 欢迎界面优化 + 用户档案中文化

改动：
1. **`bongo/cli.py`**：
   - ASCII art 字体升级为大号方块字，B 的中间横杠和底杠正确连接
   - 欢迎界面新增项目简介和两套链路说明
   - 快速开始新增 `/note` 命令提示
2. **`bongo/profile.py`**：
   - `get_profile_summary()` 全部改为中文（用户/水平/连续学习/累计任务/擅长技能/近期错误）
   - `get_context_summary()` 全部改为中文
   - `get_daily_summary()` 全部改为中文
   - 新增 `LEVEL_NAMES` 映射：beginner→初学者, intermediate→进阶, advanced→熟练, expert→专家

### 新增 delete_file 工具 + 重复调用检测修复 + OS 感知

背景：agent 删除文件时使用 `run_shell` + `rm`（Windows 不支持），绕过了工具护栏。重复调用检测器只检查最近两次，中间插入其他工具后无法拦截连续重复。

改动：
1. **`bongo/tools.py`**：
   - 新增 `delete_file` 工具：`path` 参数，`risky: True`，校验路径存在且非目录，实现为 `path.unlink()`
   - 新增 `tool_delete_file` 函数和 `_TOOL_RUNNERS` 注册
2. **`bongo/runtime.py`**：
   - `build_prefix()` 通过 `platform.system()` 获取 OS 信息注入 prefix
   - prefix 新增规则：`To delete a file, use the delete_file tool. Do NOT use run_shell with rm/del.`
   - `repeated_tool_call()` 修复：从"检查最近两次是否相同"改为"检查最近一次相同调用之后是否有其他工具调用"。连续重复拦截，中间做了别的事则放行

测试：4/4 通过

### 新增 write_note 工具 + 缓存路径修复

背景：agent 创建笔记时使用 `write_file` 在工作目录下生成 md 文件，而非写入 `~/.bongo/notes/`。`persist_large_output` 缓存文件也保存在工作目录下，干扰用户文档。

改动：
1. **`bongo/tools.py`**：
   - 新增 `write_note` 工具：`title`/`content`/`file_path` 参数，`risky: False`
   - 调用 `UserProfile.add_note()` 将笔记写入 `~/.bongo/notes/{username}.md`
   - 注册到 `_TOOL_RUNNERS`
2. **`bongo/runtime.py`**：prefix 新增规则 `To save learning notes, use the write_note tool.`
3. **`bongo/utils.py`**：`persist_large_output()` 缓存路径从 `workspace/.bongo/cache/` 改为 `~/.bongo/cache/`
4. **`README.md`**：工具表新增 `write_note`，总数更新为 11

测试：4/4 通过

### 模型驱动的压缩器：文档摘要 + History 旧轮压缩

背景：文档摘要和 history 旧轮压缩都基于简单截取（前 3 行拼接 / 60 字符截断），信息损失大。改用模型调用生成有意义的压缩结果。

改动：
1. **`bongo/compressor.py`**（新建）：
   - `compress()`：通用模型压缩调用，带降级 fallback
   - `compress_document()`：文档摘要压缩，小文件（< 500 字符）直接返回
   - `compress_history()`：历史记录压缩，生成 150 字中文摘要
2. **`bongo/runtime.py`**：
   - `_fork_summary` 改用 `compress_document` 替代前 3 行截取
   - `summarize_read_result` 传入 `model_client` 使用模型压缩
   - 修复 `tool_delegate` 的 `workspace=` → `work_dir=` bug
3. **`bongo/memory.py`**：`summarize_read_result` 新增 `model_client` 参数，有模型时调用 compressor，无模型时降级到行截取
4. **`bongo/context_manager.py`**：
   - `_render_history_section` 旧轮（最近 6 条之前）用 `compress_history` 压缩为摘要
   - 压缩结果缓存在 `session.memory.compressed_history`，用 hash 做失效检测
   - 旧条目 < 4 条时直接截取，不调用模型（避免测试中消耗 fake model 输出）

测试：4/4 通过

### Prompt 预算扩展 + History 窗口调整

背景：recent_window = 6 只覆盖 1-2 轮工具调用，模型看不到足够的近期上下文。用户要求覆盖 6-7 轮对话。

改动：
1. **总预算**：24,000 → 32,000 字符（~8,000 tokens）
2. **history 预算**：16,800 → 24,000 字符
3. **history floor**：3,000 → 5,000 字符
4. **recent_window**：6 → 24 个 item（覆盖约 6 轮 × 4 item/轮）

预算分配：

| Section | 预算 | 说明 |
|---------|------|------|
| prefix | 5,000 | 身份+规则 |
| workspace | 200 | 工作目录 |
| memory | 2,000 | 文件摘要 |
| history | 24,000 | 对话历史（24 条近期 + 旧轮压缩） |
| request | ~800 | 当前请求（不设限） |

测试：4/4 通过

### 笔记/错题索引重设计：O(1) 定位 + /ask memory 加载

背景：笔记没有索引，每次读取需全文解析。错题索引只有摘要没有位置信息，也无法 O(1) 定位。`/ask` 加载文档时 memory 中只有标题列表，agent 无法高效访问内容。

改动：
1. **`bongo/profile.py`**：
   - 笔记索引 `~/.bongo/notes/{username}_index.md`（新建）：`- 标题 | 时间 | offset:N, len:M`
   - 错题索引追加 offset：`- [...] 得分:X | ... | offset:N, len:M`
   - `add_note`/`add_mistake`：用 `a+b` 模式写入，`f.tell()` 记录字节偏移
   - `get_notes`/`get_mistakes_from_file`：优先从索引读取，有 offset 时 `seek+read` 读单条，无 offset 时降级全文解析
   - `delete_note`/`delete_mistake`：删除后重建索引
   - `_rebuild_notes_index`/`_rebuild_mistakes_index`：从详情文件重建索引
   - `_read_entry_at`：seek 读取 helper
   - `_ensure_indexes`：init 时自动检查并重建缺失/旧格式索引（数据迁移）
2. **`bongo/cli.py`**：
   - `_ask_with_notes`：从索引读取条目，items 含 `file_path`/`offset`/`length`
   - `_ask_with_mistakes`：同上
3. **`bongo/memory.py`**：
   - `populate_index`：存储 `file_path`/`offset`/`length`
   - `load_document_by_offset`（新增）：用 seek 读取单条文档
   - `render_ask_memory`：显示源文件路径，agent 知道读哪个文件

测试：4/4 通过

### 新增 read_entry 工具：按编号读取笔记/错题条目

背景：`/ask` 加载笔记或错题列表后，agent 无法通过编号直接读取某条内容。需先用 `read_notes` 读列表，再用 `read_file` 读全文，效率低。

改动：
1. **`bongo/tools.py`**：
   - 新增 `read_entry` 工具：`path`（文件路径）+ `entry`（1-based 编号）参数，`risky: False`
   - 根据文件名推断对应 `_index.md` 索引文件，解析 offset/length，用 `seek` O(1) 读取单条内容
   - 新增 `tool_read_entry` 实现函数、validator、`_TOOL_RUNNERS` 注册
2. **`bongo/runtime.py`**：prefix 新增规则 `To read a specific entry from a notes/mistakes file by list number, use read_entry(path, entry).`
3. **`README.md`**：工具表新增 `read_entry`，总数更新为 13

工具链路：`/ask` 加载列表 → memory 显示索引 → agent 调用 `read_entry(path="username.md", entry=3)` → 索引定位 → seek 读取 → 返回第 3 条完整内容

测试：4/4 通过

### 新增 delete_entry 工具：按编号删除笔记/错题条目

背景：agent 按编号删除错题时，只能用 `patch_file` 猜测文本匹配，经常删错条目或陷入循环。需要一个和 `read_entry` 对称的"按编号删除"工具。

改动：
1. **`bongo/tools.py`**：
   - 新增 `delete_entry` 工具：`path`（文件路径）+ `entry`（1-based 编号）参数，`risky: True`
   - 从索引读取 offset/length → 精确删除该字节范围 → 重建索引
   - 新增 `tool_delete_entry` 实现函数、validator、`_TOOL_RUNNERS` 注册
2. **`bongo/runtime.py`**：prefix 新增规则 `To delete a specific entry by list number, use delete_entry(path, entry). Do NOT use patch_file for this.`
3. **`README.md`**：工具表新增 `delete_entry`，总数更新为 14

工具链路：`/ask` 加载列表 → 用户说"删除第 7 条" → agent 调用 `delete_entry(path, entry=7)` → 索引定位 → 字节级删除 → 重建索引 → 返回确认

测试：4/4 通过
