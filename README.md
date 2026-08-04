# bongo - 本地智能助学 Agent

bongo 是一个以本地资料和个人学习数据为中心的智能助学 Agent。它可以阅读你授权的文档、整理学习笔记、维护错题本、生成针对性练习，并根据长期使用记录形成个人学习画像。

核心目标是让学习资料、练习反馈和长期记忆形成闭环。

使用 Ollama 时，模型推理和学习数据都可以保留在本机；使用 OpenAI 或 Anthropic 兼容接口时，只有模型请求会发送到所配置的服务端。笔记、错题、画像、会话和运行记录始终保存在本地文件中。

## 核心能力

### `/ask`：基于资料的智能问答

`/ask` 使用 ReAct 控制循环。模型可以根据问题自主选择工具，但只能在用户选定的范围内操作。

| 模式 | 用途 | 工作范围 |
|---|---|---|
| 信任路径 | 阅读课程资料、分析文档、整理知识 | 用户选择的本地文件或目录 |
| 笔记 | 查询、补充和整理学习笔记 | `~/.bongo/notes/` |
| 错题 | 分析错误原因、归纳薄弱知识点 | `~/.bongo/mistakes/` |

```text
/ask 帮我总结最近的错题，并给出复习顺序
/ask 对比装饰器和闭包的适用场景
/ask 从这份课程讲义中整理一份知识提纲
```

文档模式采用渐进式加载：Agent 先获得轻量索引，只在需要时读取完整内容，最多在工作记忆中保留 5 份已加载文档。

### `/practice`：针对性练习

`/practice` 使用独立的 Plan-and-Execute 流程，不共享 `/ask` 的对话历史和工具上下文。

| 模式 | 说明 |
|---|---|
| 快问快答 | 根据最近笔记生成问题 |
| 深度求索 | 根据选定的 Markdown 学习资料生成问题 |
| 朝花夕拾 | 复习错题；答对移除，答错累计错误次数 |

每题由模型评分。低于 60 分的回答会写入错题本，同时保存题目、用户回答、参考答案、反馈、来源和标签。

### 本地学习档案

bongo 支持多个本地用户，每个用户拥有独立的：

- 学习笔记与笔记索引
- 错题本与错题索引
- 信任路径
- 常聊话题和薄弱领域
- 学习偏好、连续学习天数和每日统计
- 会话摘要与历史记录

`/skills export` 可以将个人画像、笔记、错题和会话导出为一个可复用的本地 skill 目录。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
pip install -e .

# 启动交互模式
bongo

# 执行一次问答后退出
bongo "解释一下 TCP 三次握手，并给我一道检查理解的问题"

# 指定学习资料所在目录
bongo --cwd /path/to/learning-materials
```

## 模型配置

支持以下模型后端：

| Provider | 使用方式 |
|---|---|
| Ollama | 启动 `ollama serve`，然后使用 `--provider ollama` |
| OpenAI 兼容接口 | 使用 `--provider openai --base-url ... --model ...` |
| Anthropic 兼容接口 | 使用 `--provider anthropic --base-url ... --model ...` |

配置优先级为：命令行参数、环境变量、持久化配置、代码默认值。

```bash
bongo config --provider openai \
  --api-key sk-xxx \
  --base-url https://api.example.com/v1 \
  --model your-model

bongo config --show
```

## REPL 命令

| 命令 | 作用 |
|---|---|
| `/ask <问题>` | 进入资料、笔记或错题问答 |
| `/practice` | 进入针对性练习 |
| `/note [-天数]` | 查看最近的学习笔记 |
| `/note del <关键词>` | 删除匹配的笔记 |
| `/mistake [-天数]` | 查看最近的错题 |
| `/profile` | 查看学习档案摘要 |
| `/skills` | 查看画像三要素 |
| `/skills export` | 导出个人学习 skill |
| `/errors` | 按类型查看错误历史 |
| `/progress` | 查看最近 7 天的学习进度 |
| `/user` | 查看、创建或切换用户 |
| `/memory` | 查看当前 Agent 工作记忆 |
| `/session` | 查看当前会话文件 |
| `/resume [id]` | 列出或恢复历史会话 |
| `/reset` | 清空当前会话 |
| `/level [ask\|auto\|never]` | 查看或修改工具审批策略 |
| `/help` | 查看帮助 |
| `/exit` | 退出 |

## 常用启动参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--cwd` | `.` | 本地资料或工作目录 |
| `--provider` | `openai` | 模型后端 |
| `--model` | 配置或代码默认值 | 模型名称 |
| `--approval` | `ask` | 风险工具审批策略 |
| `--max-steps` | `20` | 单次请求最大 Agent 轮数 |
| `--max-new-tokens` | `2048` | 每轮模型最大输出 token |
| `--temperature` | `0.2` | 采样温度 |
| `--resume` | 无 | 恢复指定会话或 `latest` |

## Agent 工作方式

### 双链路

| | `/ask` | `/practice` |
|---|---|---|
| 范式 | ReAct | Plan-and-Execute |
| 输入 | 当前问题、工作记忆、历史和本地资料 | 笔记、文档或错题 |
| 工具 | 模型自主选择 | 不使用工具 |
| 状态 | 跨轮保存 | 单次练习流程 |
| 输出 | 基于资料的回答或笔记操作 | 题目、评分、错题和总结 |

### 上下文与记忆

每轮 `/ask` 的上下文按以下顺序组装：

```text
prefix -> workspace -> memory -> history -> current_request
```

总字符预算为 32,000。超出预算时依次压缩历史、工作区信息、记忆和稳定前缀，当前用户请求始终完整保留。历史超过阈值后会生成检查点摘要，并保留最近的完整交互。

### 工具与安全边界

Agent 当前提供 18 个基础工具，并可在深度允许时增加一个只读 `delegate` 工具。能力包括：

- 列出文件、读取元数据、分段读取和搜索
- 写入、追加、精确替换、按行插入或删除文件内容
- 执行经过审批的本地 Shell 命令
- 查询和维护笔记、错题及其索引
- 读取被截断后保存到本地缓存的大结果
- 启动受限、只读的调查子 Agent

安全检查顺序如下：

```text
工具存在 -> 参数合法 -> 非重复调用 -> 通过审批 -> 执行
```

所有文件路径都锚定在当前工作根目录；解析后的路径不能逃逸。Shell 进程只继承白名单环境变量，trace 和 report 会对 API Key、Token、Secret、Password 等敏感值进行脱敏。

## MCP Server

`bongo-mcp` 通过 stdio 向兼容 MCP 的客户端提供学习工具：

| 工具 | 作用 |
|---|---|
| `record_task` | 记录任务、话题、错误和学习收获 |
| `add_note` | 保存学习笔记和关联资料路径 |
| `get_profile` | 获取学习画像摘要 |
| `get_mistakes` | 按类型查询错误历史 |
| `get_mistakes_book` | 查询错题本内容 |
| `get_progress` | 获取学习进度 |
| `user` | 查看、创建或切换用户 |

```bash
bongo-mcp
```

## 本地数据

用户级数据保存在 `~/.bongo/`：

```text
~/.bongo/
├── config.json
├── current_user
├── profiles/{username}.json
├── notes/{username}.md
├── notes/{username}_index.md
├── mistakes/{username}.md
├── mistakes/{username}_index.md
└── skills/{username}/
```

工作区级运行数据保存在 `<cwd>/.bongo/`：

```text
<cwd>/.bongo/
├── sessions/{session_id}.json
├── traces/{session_id}.jsonl
└── reports/{run_id}/
    ├── task_status.json
    ├── trace.jsonl
    └── report.json
```

Session 用于继续对话，trace 用于还原执行过程，report 用于单次请求审计。恢复会话时会检测工作目录和文件列表变化，并使过期文件摘要失效。

## 项目结构

```text
bongo/
├── cli.py               # CLI、REPL、/ask 与 /practice
├── runtime.py           # ReAct 控制循环、审批、恢复和审计
├── tools.py             # Agent 工具定义、校验与实现
├── models.py            # Ollama、OpenAI、Anthropic 和 Fake 模型适配
├── context_manager.py   # Prompt 组装、裁剪和历史压缩
├── memory.py            # 工作记忆和文档渐进式加载
├── profile.py           # 用户、笔记、错题、画像和 skill 导出
├── mcp_server.py        # 学习能力 MCP Server
├── config.py            # 本地模型配置
├── run_store.py         # 单次运行工件
├── trace.py             # 会话事件记录
├── task_status.py       # 运行状态机
├── evaluator.py         # Benchmark 评估
└── metrics.py           # 指标聚合与实验
```

## 测试与评测

项目使用 pytest 和 `FakeModelClient` 进行无需模型费用的确定性测试，同时提供上下文、记忆、恢复、报告和工具安全 benchmark。涉及真实模型的实验需要提前配置对应的模型服务。

```bash
pytest
```
