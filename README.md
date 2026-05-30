# bongo

bongo 是一个 AI 辅助学习助手，帮助你阅读文档、记录笔记、整理错题并进行针对性练习。它有两套并行的核心链路：

- **`/ask`** — 基于 ReAct 的智能问答，大模型自主调用工具读写文件
- **`/practice`** — 基于 Plan-and-Execute 的练习系统，出题、判分、记录错题全自动

数据全部存在本地 `~/.bongo/` 目录，不依赖外部服务（除模型 API）。

## 快速开始

需要 Python 3.10+。

```bash
# 安装
pip install -e .

# 启动交互模式
bongo

# 直接跑一次性任务
bongo "帮我分析这个项目的测试覆盖情况"

# 指定工作目录
bongo --cwd /path/to/project
```

## 支持的模型后端

| Provider | 默认模型 | 启动方式 |
|---|---|---|
| Ollama | mimo-v2.5-pro | `ollama serve` 后 `bongo --provider ollama` |
| OpenAI 兼容 | mimo-v2.5-pro | `bongo --provider openai --base-url ... --model ...` |
| Anthropic 兼容 | mimo-v2.5-pro | `bongo --provider anthropic --base-url ... --model ...` |

模型优先级：CLI `--model` > 环境变量 > 持久化配置 > 代码默认值。

```bash
# 保存配置，以后不用重复传参
bongo config --provider openai --api-key sk-xxx --base-url https://api.example.com/v1 --model gpt-4
bongo config --show
```

## 核心功能

### /ask — 智能问答（ReAct 链路）

`/ask` 让大模型在受限环境中自主调用工具完成任务。支持三种文档类型：

| 模式 | 说明 | agent 工作范围 |
|---|---|---|
| 信任路径 | 对指定目录下的文件进行读写操作 | 选定的目录 |
| 笔记 | 查看、修改、补充学习笔记 | `~/.bongo/notes/` |
| 错题 | 分析、整理错题本 | `~/.bongo/mistakes/` |

```bash
# 进入 /ask 后选择文档类型
/ask 帮我总结最近的错题
/ask 在 CC/README.md 末尾添加总结
/ask 装饰器和闭包有什么区别
```

交互流程：
```
/ask <问题>

请选择文档类型：
  1. 信任路径（文件操作）
  2. 笔记（学习笔记）
  3. 错题（错题本）

选择编号: 2

笔记列表（共 15 条）：
  1. [2026-05-29] 装饰器和闭包
  2. [2026-05-28] Python GIL 机制
  ...

工作路径: ~/.bongo/notes
/ask> 帮我补充装饰器的实际应用场景
```

### /practice — 练习模式（Plan-and-Execute 链路）

练习模式走独立的上下文设计，不依赖 ReAct 的 memory/history，专注于出题、判分、记录错题。

| 模式 | 说明 |
|---|---|
| 快问快答 | 从最近笔记中抽取 10 个问题 |
| 深度求索 | 选择信任路径下的 md 文档，出 10 道题 |
| 朝花夕拾 | 错题复习，答对移除，答错累加错误次数 |

```bash
/practice

=== 练习模式 ===
1. 快问快答（从最近笔记中抽取 10 个问题）
2. 深度求索（选择 md 文档，10 道题）
3. 朝花夕拾（错题复习）
0. 退出练习
```

得分低于 60 分的题目自动记入错题本，并关联来源和标签。

### 笔记与错题管理

```bash
# 笔记
/note -7              # 查看最近 7 天的笔记（默认）
/note -1              # 查看最近 1 天
/note del <关键词>    # 按关键词删除笔记

# 错题
/mistake -7           # 查看最近 7 天的错题（默认）
/mistake -1           # 查看最近 1 天

# 学习档案
/profile              # 显示技能水平、连续学习天数、任务统计
/errors               # 显示按类型分组的错误历史
/progress             # 显示过去 7 天的学习进度
```

### 用户管理

支持多用户，每个用户有独立的笔记、错题和学习档案。

```bash
/user                 # 显示当前用户和所有用户列表
/user <name>          # 切换到另一个用户
/user new <name>      # 创建新用户并切换
```

### 模型层级路由

根据任务难度自动选择模型，简单任务用轻量模型，复杂任务用强力模型。

```bash
/model                # 显示当前模型和层级状态
/model 1              # 锁定到 Tier 1（简单任务模型）
/model 2              # 锁定到 Tier 2（中等任务模型）
/model 3              # 锁定到 Tier 3（复杂任务模型）
/model unlock         # 解锁，恢复自动路由
```

任务分类规则：
- **写任务**：< 200 行 → L1，200-500 行 → L2，> 500 行 → L3
- **读任务**：< 500 行 → L1，500-1000 行 → L2，> 1000 行 → L3
- **无行数**：读任务默认 L1，写任务默认 L2

## 工具

bongo 提供 9 个工具，模型只能调用白名单中的工具：

| 工具 | 参数 | 危险 | 说明 |
|---|---|---|---|
| `list_files` | `path='.'` | 否 | 列出目录文件 |
| `read_file` | `path, start=1, end=200` | 否 | 按行号范围读文件 |
| `search` | `pattern, path='.'` | 否 | 搜索关键词（优先用 rg） |
| `run_shell` | `command, timeout=20` | 是 | 执行 shell 命令 |
| `write_file` | `path, content` | 是 | 写文件 |
| `patch_file` | `path, old_text, new_text` | 是 | 精确文本替换（old_text 必须恰好出现一次） |
| `delegate` | `task, max_steps=3` | 否 | 启动只读子 agent 做调查 |
| `search_mistakes` | `query, limit=3` | 否 | 搜索错题索引 |
| `get_mistake_detail` | `title` | 否 | 获取错题详情 |
| `read_notes` | `limit=10` | 否 | 读取最近学习笔记 |

所有文件类工具的路径被锚定在 workspace root 下，`../` 逃逸会被直接拦截。

## 常用参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--cwd` | `.` | 工作目录 |
| `--approval` | `ask` | 审批策略：`ask` / `auto` / `never` |
| `--max-steps` | `20` | 每轮请求最大工具调用次数 |
| `--max-new-tokens` | `2048` | 每步模型最大输出 token |
| `--temperature` | `0.2` | 采样温度 |
| `--resume` | 无 | 恢复会话：指定 ID 或 `latest` |
| `--read-only` | false | 只读模式，禁止所有写操作 |

## REPL 命令一览

| 命令 | 作用 |
|---|---|
| `/ask <问题>` | 选择文档类型后进入智能问答 |
| `/practice` | 进入练习模式 |
| `/note [-天数]` | 查询笔记 |
| `/note del <关键词>` | 删除笔记 |
| `/mistake [-天数]` | 查询错题 |
| `/profile` | 显示学习档案 |
| `/errors` | 显示错误历史 |
| `/progress` | 显示学习进度 |
| `/user` | 用户管理 |
| `/model` | 模型层级管理 |
| `/memory` | 查看 agent 工作记忆 |
| `/session` | 查看会话文件路径 |
| `/reset` | 清空当前会话 |
| `/level` | 查看/切换审批策略 |
| `/help` | 查看帮助 |
| `/exit` | 退出 |

## 设计思想

### 1. 双链路架构

`/ask` 和 `/practice` 有完全独立的上下文设计：

| | /ask | /practice |
|---|---|---|
| 范式 | ReAct（观察→思考→行动→循环） | Plan-and-Execute（出题→判分→总结） |
| 上下文 | prefix + memory + history + request | 阶段独立，无 history，无 memory |
| 工具调用 | 有，模型决定调什么 | 无，流程固定 |
| 状态 | 跨轮保持 | 无状态 |

### 2. /ask Memory：渐进式披露

`/ask` 的 memory 采用静态 + 动态分离设计：

- **静态部分**（prefix）：身份、行为规则、工具定义，只在启动时渲染一次
- **动态部分**（ask_mode）：根据文档类型动态切换

ask_mode 结构：
```
mode:        "notes" / "mistakes" / "trust_path"
original_request: 用户首次提问
index:       [{id, label, summary}, ...]  # 轻量索引，始终存在
loaded:      {doc_id: {path, content, loaded_at}}  # 完整内容，最多 5 个
```

工作流程：用户选择模式 → 填充索引 → agent 看到索引 → 调用 read_file → 自动加载到 loaded + fork 子线程生成摘要 → agent 看到完整内容 → 读写操作。超出 loaded 上限时淘汰最旧文档。

### 3. Prompt 结构与预算裁剪

Prompt 按固定顺序组装为 5 段，总预算 24000 字符：

```
prefix（身份 + 行为规则）→ workspace（工作目录）→ memory（任务摘要 + 文件索引）→ history（对话记录）→ current_request（当前请求）
```

当 prompt 超过预算时按优先级压缩，当前用户请求始终完整保留。

### 4. 安全护栏

工具调用是一条带护栏的流水线：

```
工具是否存在 → 参数是否合法 → 是否重复调用 → 是否通过审批 → 真正执行
```

关键安全机制：
- **路径隔离**：所有文件操作锚定在 workspace root，`../` 解析后仍需在 root 下
- **审批策略**：`ask`（每次询问）、`auto`（自动放行）、`never`（全部拒绝）
- **只读模式**：`--read-only` 禁止所有写操作
- **重复调用拦截**：连续两次相同参数的工具调用直接拦
- **Shell 环境过滤**：只传递白名单环境变量
- **敏感信息脱敏**：trace 和 report 中自动替换 API_KEY、TOKEN 等值

### 5. 用户画像系统

每个用户有独立的档案，记录学习轨迹：

- **技能追踪**：自动统计使用频率，每 5 次升级一级（最高 L5）
- **错题本**：按类型分组，支持标签检索，答对自动移除
- **学习笔记**：Markdown 格式，支持关联文件路径
- **信任路径**：笔记关联的文件自动加入信任路径，供 /ask 和 /practice 使用
- **每日统计**：任务数、技能使用、错误计数、连续学习天数

### 6. MCP Server

bongo 可以作为 MCP server 被 Claude Code 调用，暴露以下工具：

| 工具 | 说明 |
|---|---|
| `record_task` | 记录完成的任务、技能、易错点 |
| `add_note` | 记录学习笔记和信任的文件路径 |
| `get_profile` | 获取用户画像摘要 |
| `get_mistakes` | 查询历史易错点 |
| `get_mistakes_book` | 获取错题本内容 |
| `get_progress` | 获取学习进度统计 |
| `user` | 查看/切换/创建用户 |

### 7. 检查点与恢复

会话状态以 JSON 保存在 `.bongo/sessions/`，运行工件保存在 `.bongo/runs/<run_id>/`。两层持久化分离：会话层用于跨轮次恢复，运行层用于单次执行审计。

```bash
bongo --resume latest    # 恢复上一次会话
bongo --resume abc123    # 恢复指定会话
```

### 8. 确定性测试

模型客户端可替换为 `FakeModelClient`（脚本播放器），在不调用 API 的情况下完整运行控制循环，实现零成本确定性测试。

## 项目结构

```
bongo/
├── __init__.py          # 公共 API 导出
├── cli.py               # CLI 入口、REPL 循环、/ask 和 /practice 实现
├── runtime.py           # 核心控制循环（ReAct: ask / run_tool / parse）
├── tools.py             # 工具定义、校验、实现
├── context_manager.py   # 5 段 prompt 组装与预算裁剪
├── memory.py            # 结构化记忆（工作集、文件摘要、ask_mode 渐进式披露）
├── models.py            # 模型后端适配（Ollama / OpenAI / Anthropic / Fake）
├── profile.py           # 用户画像（技能、错题、笔记、信任路径）
├── tier_manager.py      # 任务难度分类与多模型层级路由
├── mcp_server.py        # MCP server，供 Claude Code 调用
├── config.py            # 持久化用户配置（~/.bongo/config.json）
├── utils.py             # 工具函数
├── run_store.py         # 运行工件持久化
├── task_status.py       # 执行状态机
├── metrics.py           # 评测实验
└── evaluator.py         # benchmark 评测器
```

## 数据存储

```
~/.bongo/
├── config.json              # 用户配置（provider、api_key、base_url、model）
├── current_user             # 当前活跃用户名
├── profiles/
│   └── {username}.json      # 用户画像（技能、错误、学习记录）
├── notes/
│   └── {username}.md        # 学习笔记（Markdown）
├── mistakes/
│   ├── {username}.md        # 错题本详情（Markdown）
│   └── {username}_index.md  # 错题索引（轻量，供快速检索）
└── sessions/
    └── {session_id}.json    # 会话状态
```
