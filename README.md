# bongo

bongo 是一个面向代码仓库的轻量本地 coding agent。它跑在终端里，先读取当前工作区状态，再用一组受约束的工具去读文件、改文件、跑命令，会话状态保存在本地 `.bongo/` 目录里。

## 快速开始

需要 Python 3.10+。

```bash
# 安装
pip install -e .

# 在当前仓库启动交互模式
bongo

# 直接跑一次性任务
bongo "inspect the test failures and propose a fix"

# 指定工作目录
bongo --cwd /path/to/repo
```

## 支持的模型后端

| Provider | 默认模型 | 启动方式 |
|---|---|---|
| Ollama | qwen3.5:4b | `ollama serve` 后 `bongo --provider ollama` |
| OpenAI 兼容 | qwen3.5-plus-2026-02-15 | `bongo --provider openai --base-url ... --model ...` |
| Anthropic 兼容 | claude-sonnet-4-6 | `bongo --provider anthropic --base-url ... --model ...` |

模型优先级：CLI `--model` > 环境变量 > 持久化配置 > 代码默认值。

```bash
# 保存配置，以后不用重复传参
bongo config --provider openai --api-key sk-xxx --base-url https://api.example.com/v1 --model gpt-4
bongo config --show
```

## 常用参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--cwd` | `.` | 工作目录 |
| `--approval` | `ask` | 审批策略：`ask` / `auto` / `never` |
| `--max-steps` | `6` | 每轮请求最大工具调用次数 |
| `--max-new-tokens` | `512` | 每步模型最大输出 token |
| `--temperature` | `0.2` | 采样温度 |
| `--resume` | 无 | 恢复会话：指定 ID 或 `latest` |
| `--read-only` | false | 只读模式，禁止所有写操作 |

## REPL 内置命令

| 命令 | 作用 |
|---|---|
| `/help` | 查看帮助 |
| `/memory` | 查看当前工作记忆 |
| `/session` | 查看会话文件路径 |
| `/reset` | 清空当前会话状态 |
| `/exit` | 退出 |

```bash
# 查看最新一次运行的实时状态（当前动作、工具调用次数、模型轮次等）
bongo status
```

## 工具

bongo 提供 7 个工具，模型只能调用白名单中的工具：

| 工具 | 参数 | 危险 | 说明 |
|---|---|---|---|
| `list_files` | `path='.'` | 否 | 列出目录文件 |
| `read_file` | `path, start=1, end=200` | 否 | 按行号范围读文件 |
| `search` | `pattern, path='.'` | 否 | 搜索关键词（优先用 rg） |
| `run_shell` | `command, timeout=20` | 是 | 执行 shell 命令 |
| `write_file` | `path, content` | 是 | 写文件 |
| `patch_file` | `path, old_text, new_text` | 是 | 精确文本替换（old_text 必须恰好出现一次） |
| `delegate` | `task, max_steps=3` | 否 | 启动只读子 agent 做调查 |

所有文件类工具的路径被锚定在 workspace root 下，`../` 逃逸会被直接拦截。

## 设计思想

### 1. ReAct 控制循环

bongo 的核心是一个 sense → decide → act 循环：

```
用户请求
  → 构建 prompt（5 段，带预算控制）
  → 模型输出 → 解析动作
  → 工具调用（校验 → 重复检测 → 审批 → 执行 → 更新记忆）
  → 循环直到模型给出最终答案
```

每轮都重新构建完整 prompt，保证状态一致性。双停止条件：`max_steps` 限制工具调用，`max_attempts` 限制总模型调用（含重试）。

### 2. 分层 Prompt 与预算裁剪

Prompt 按固定顺序组装为 5 段，总预算 12000 字符：

| 段 | 预算 | 最低保底 | 裁剪优先级 |
|---|---|---|---|
| prefix（系统规则 + 工具定义 + 工作区快照） | 3600 | 1200 | 最后裁 |
| memory（任务摘要 + 最近文件 + 文件摘要） | 1600 | 400 | 第三 |
| relevant_notes（关键词召回的笔记，最多 3 条） | 1200 | 300 | 第一裁 |
| history（完整对话记录） | 5200 | 1500 | 第二 |
| current_request（当前用户请求） | 不参与裁剪 | - | 永远不裁 |

当 prompt 超过总预算时，按 `relevant_notes → history → memory → prefix` 的顺序依次压缩，当前用户请求始终完整保留。

### 3. 三层结构化记忆

记忆不是简单的历史拼接，而是三层独立维护的状态：

- **工作集**：当前任务摘要（300 字符）+ 最近访问文件（最多 8 个）
- **文件摘要**：每个读过的文件存摘要 + SHA-256 新鲜度哈希。文件在磁盘上变了，摘要自动失效，不会给模型过期信息
- **相关笔记**：从工具结果中提取的事件笔记（最多 12 条），通过关键词重叠 + 标签匹配 + 时间衰减召回，最多 3 条注入 prompt

### 4. 工作区指纹与漂移检测

每轮 prompt 构建前，bongo 对工作区做一次轻量快照（git status、最近提交、项目文档内容），计算 SHA-256 指纹。指纹变了就重建 prefix，没变就复用。这保证了外部修改（别人提交了代码、手动改了 README）能被 agent 及时感知。

### 5. 安全护栏

工具调用不是"直接调函数"，而是一条带护栏的流水线：

```
工具是否存在 → 参数是否合法 → 是否重复调用 → 是否通过审批 → 真正执行
```

关键安全机制：
- **路径隔离**：所有文件操作锚定在 workspace root，符号链接和 `../` 解析后仍需在 root 下
- **审批策略**：`ask`（每次询问）、`auto`（自动放行）、`never`（全部拒绝）
- **只读模式**：`--read-only` 禁止所有写操作
- **重复调用拦截**：连续两次相同参数的工具调用直接拦
- **Shell 环境过滤**：只传递白名单环境变量
- **敏感信息脱敏**：trace 和 report 中自动替换 API_KEY、TOKEN 等值

### 6. 确定性测试

bongo 的模型客户端可以替换为 `FakeModelClient`——一个脚本播放器，返回预设输出。这让整个控制循环在不调用任何 API 的情况下就能完整运行，实现零成本的确定性测试。

评测覆盖 4 个独立维度：
- **工具安全**（15 个场景）：路径逃逸、参数校验、审批拦截、重复调用
- **上下文压缩**（12 组配置）：不同历史/笔记/请求长度下的 prompt 压缩效果
- **记忆收益**（8 任务 × 2 变体）：记忆开/关对重复读文件的消除率
- **漂移检测**（12 个场景）：工作区外部变更的指纹识别能力

### 7. 检查点与恢复

会话状态（对话历史、工作记忆、相关笔记）以 JSON 保存在 `.bongo/sessions/`。每次运行的执行工件（状态机快照、事件流 trace.jsonl、报告 report.json）保存在 `.bongo/runs/<run_id>/`。两层持久化分离：会话层用于跨轮次恢复，运行层用于单次执行审计。

```bash
# 恢复上一次会话
bongo --resume latest

# 恢复指定会话
bongo --resume abc123
```

## 项目结构

```
bongo/
├── __init__.py          # 公共 API 导出
├── cli.py               # CLI 入口与参数解析
├── runtime.py           # 核心控制循环（ask / run_tool / parse）
├── tools.py             # 工具定义、校验、实现
├── context_manager.py   # 5 段 prompt 组装与预算裁剪
├── memory.py            # 3 层结构化记忆
├── workspace.py         # 工作区快照与指纹
├── models.py            # 模型后端适配（Ollama / OpenAI / Anthropic / Fake）
├── run_store.py         # 运行工件持久化
├── task_status.py       # 执行状态机
├── config.py            # 持久化用户配置
├── metrics.py           # 评测实验
└── evaluator.py         # benchmark 评测器
```
