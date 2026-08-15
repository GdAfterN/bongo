# Bongo Study

Bongo Study 是一个 Windows 优先、本地数据优先的桌面智能助学 Agent。它把算法题解转化为可重复练习的本地题库，把文档接入用户自有的外部 RAG 服务，并通过 Chat、Work、桌宠提醒和 Learning Skill 将知识输入、练习、纠错与沉淀串成完整闭环。

项目当前聚焦可完整运行的核心闭环：

```text
代码题解 -> 本地解析 -> 固定三类选择题 -> 练习 / 错题 / 桌宠气泡
文档资料 -> 外部 RAG 上传 -> Chat 检索 -> 模型回答与引用
本地目录 -> Work 会话 -> 默认 ReAct 工具 / Claude Code / Codex
学习记录 ---------------------------------> Learning Skill 编译与导出
```

## 核心设计

- **知识与代码分流**：文档只进入外部 RAG，用于 Chat 检索问答；代码题解在本地结构化为“主要思路、数据结构、边界条件”三类选择题，避免两类数据共用一套不准确的处理流程
- **有界 Work Agent**：默认后端使用结构化 Tool Decision 和有界 ReAct 循环，文件工具受所选工作目录沙箱约束；也可在本机 CLI 可用时切换到 Claude Code 或 Codex
- **可追溯的学习沉淀**：题目、作答、错题纠正、对话结论和来源版本进入学习事件账本，再编译为带 `manifest.json` 与 `references/` 的可导出 Learning Skill
- **确定性数据工具 + 模型加工**：Hacker News 抓取、排序和正文读取由本地代码完成，模型只负责逐条翻译与摘要；每成功一条立即持久化，单条失败不影响整批结果
- **隐私友好的活动统计**：只聚合应用级键鼠计数与活跃时间，不记录具体按键、输入文本、窗口标题、鼠标坐标或完整程序路径

## 已实现功能

- Qt Quick/QML 现代桌面管理面板，包含动态仪表盘、Chat/Work 会话、知识库、题库、练习、Skill、AI 简讯和设置页面
- 首页集中展示导入资料、算法题、当日工作时长、连续工作时长和键盘敲击次数，并提供带进入动画和数据过渡的折线图、柱状图及 AI 热讯排行
- 知识库分为文档知识与代码知识两个模块，使用独立导入入口和拆解协议
- 透明、置顶、可拖动的 BongoCat Live2D 桌宠窗口
- 全局键盘、鼠标左右键和鼠标移动映射为桌宠动作
- 可选的匿名活动记录：按前台应用和 5 分钟时间段聚合键盘次数、鼠标活跃秒数与点击次数
- 首页展示今日键盘总量、当日/连续工作时长，并按双小时时段绘制动态键盘活动图
- 鼠标悬停桌宠时显示当前连续工作时长和本次键盘敲击量，首页同步展示连续工作状态；连续 10 分钟没有键鼠活动后结束本次计时
- 连续工作达到 40 分钟时，由受限 ReAct 链路调用只读会话工具，生成结构化工作摘要和休息提醒
- 独立 AI 简讯模块保存并展示 20 条中文结构化简讯，字段包含标题、摘要、发布时间、作者和原文链接；启动时后台更新，之后每 8 小时重新获取
- AI 简讯页面提供“主动抓取”按钮和实时进度，展示榜单读取、帖子详情获取、本地筛选、模型生成与结构校验阶段，以及已读取帖子数量；主动抓取失败会明确报错并保留原缓存
- 本地一次选出 20 个来源，再拆成 20 个独立模型任务；每条成功后立即写入 SQLite 和刷新界面。网络正文抓取最多尝试 5 次，单条模型生成遇到超时、临时服务错误或结构校验失败时最多尝试 3 次
- 桌宠显示、透明度、尺寸、置顶、点击穿透、镜像、键鼠响应和答题等待时间设置
- 桌宠设置提供“笔记本 2880×1800 / 200%”和“2K 27 寸 / 100%”两套输入命中预设，用于校准点击穿透状态下的右键区域；猫外右键不会触发桌宠菜单
- 文档知识通过可配置 HTTP Connector 上传到外部 RAG，不在本地切分或生成题目
- 代码知识读取常见代码文件，单文件上限 2 MB，并使用算法题专用 Prompt 生成题目
- 代码知识面向算法题题解，保存中文题名、算法题简要摘要和解题思路，并固定生成主思路、数据结构、边界条件三道题
- 算法题解析覆盖数据结构选择原因、执行过程、复杂度、边界条件和错误方案对比
- 右键桌宠显示 2×2 快捷菜单，可手动“来一题”、查看“今日统计”、轮播“AI 资讯”或打开“仪表盘”；简讯气泡只显示限长的中文标题、摘要、发布时间和作者，点击后进入详情页查看原文链接
- 连续工作、题目、解析和统计统一使用位于桌宠上方、带指向尖角的气泡样式，透明度跟随桌宠设置
- 手动题目气泡可直接作答；超时后自动收起并加入未回答列表
- 可在知识库中按来源勾选允许进入桌宠气泡的题目范围，不影响题库和练习页
- 按算法题查看题库，保留逐次答题记录并支持错题与未回答题目复习
- SQLite 本地数据库保存代码题库、RAG 文档元数据、会话、Agent Trace 与答题历史
- Chat 无需选择单一文档，先调用当前外部 RAG 的 `/retrieval`，再由配置模型依据检索结果回答并标注来源
- Work 可选择本地目录与默认、Claude Code、Codex 后端；默认后端使用有界 ReAct 循环和目录沙箱文件工具
- 会话上下文、长期摘要、历史会话保存与恢复
- OpenAI、Anthropic 官方 Python SDK
- Work 后端可选默认、Claude Code 或 Codex；CLI 仅在实际可执行时可选
- 独立的 Learning Skill 管理页：可按来源创建、查看、编辑、删除和导出 Skill
- Skill 沉淀融合原始知识、完整题库、错题纠正状态、来源限定对话洞察、学习画像、复习计划和桌宠学习成果
- Skill 使用 `SKILL.md`、`manifest.json` 和 `references/` 渐进式披露结构，记录来源哈希、版本和待更新状态
- 导出前校验 frontmatter、JSON、来源隔离和模型密钥；默认不导出完整原始聊天和键鼠日志
- 基于学习事件账本聚合知识导入、答题、错题纠正和对话结论，桌宠成长只反映真实学习行为

当前版本不实现 MCP、评测系统、视频生成，也暂不实现宠物升级、亲密度和装备系统。外部知识只通过 HTTP RAG Connector 接入。Work 默认后端提供受工作目录约束的文件列表、信息、读取、搜索、写入和单文件删除工具，不提供任意 Shell。工作提醒保留白名单只读工具与有界 ReAct 链路；AI 简讯由固定本地方法抓取、筛选 Hacker News 并受限读取公网原文，再把 20 个来源拆成 20 次独立模型请求。

## 技术栈

| 模块 | 实现 |
|---|---|
| 桌面 UI | Python 3.11+、PySide6、Qt Quick/QML；QWidget 保留透明桌宠窗口 |
| 桌宠渲染 | Qt WebEngine、PixiJS、`easy-live2d`、ayangweb/BongoCat 模型 |
| 全局输入 | `pynput` |
| 本地存储 | SQLite、FTS5 |
| 外部知识 | HTTP RAG Connector、Dify External Knowledge 风格 `/retrieval` 契约 |
| Agent | 有界 ReAct、结构化 Tool Decision、工作目录路径沙箱、执行 Trace |
| 数据校验 | Pydantic |
| 模型 | OpenAI SDK、Anthropic SDK、Claude Code CLI、Codex CLI |

## 快速开始

需要 Python 3.11 或更高版本。Windows PowerShell：

```powershell
python -m pip install -e .
python -m bongo
```

安装完成后，可以直接双击项目根目录的 `Bongo Study.pyw` 启动桌面端。该入口使用 `pythonw`，不会保留 PowerShell 或命令行窗口。

也可以使用安装后的命令；其中 `bongo-study` 是无控制台的 Windows GUI 入口：

```powershell
bongo
bongo-study
```

常用启动参数：

```powershell
# 指定测试数据目录
python -m bongo --data-dir .bongo-local

# 只启动学习面板，不显示桌宠
python -m bongo --no-pet

# 离屏启动检查，自动退出
python -m bongo --smoke-test --data-dir .bongo-smoke
```

## 模型配置

应用只使用官方 SDK。可以在“设置”页填写 Provider、模型、API Key 和 Base URL；这些配置只保存在本机 SQLite，不会写入项目文件。也可以通过环境变量提供 Key：

```powershell
# OpenAI
$env:OPENAI_API_KEY = "sk-..."
$env:OPENAI_MODEL = "gpt-4.1-mini"

# Anthropic
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:ANTHROPIC_MODEL = "claude-sonnet-4-5"
```

在应用的“设置”页选择 `openai` 或 `anthropic`，用于代码题生成、Chat 回答和默认 Work Agent。OpenAI 后端使用 Responses API；当 Base URL 只有服务根地址时，应用会自动补全 `/v1`。

Work 会话可以单独选择：

- `默认`：使用 Bongo 的上下文、摘要和有界 ReAct 文件工具
- `Claude Code`：使用系统 `PATH` 中可执行的 Claude Code CLI
- `Codex`：使用系统 `PATH` 中可执行的 Codex CLI

应用会实际执行 CLI 的 `--version` 检查，而不只判断文件是否存在。Claude Code 或 Codex 未安装、未加入 `PATH` 或不可执行时，选择该项会立即提示并保留上一个可用后端；不会自动从默认后端切换到 CLI Agent。

Claude Code 与 Codex 只在 Work 中使用所选目录作为工作目录，并调用各自原生 Agent 能力。Codex Work 使用 `workspace-write`；其他通用 Codex 调用仍保持 `read-only`。Bongo 本地数据库保存会话文本，默认 Work 额外保存每次工具调用的结构化 Trace。

### 外部 RAG 接口

在“会话 → Chat → RAG 配置”填写 Base URL、API Key、Knowledge ID，以及可配置的上传、检索、删除路径。检索请求兼容 Dify External Knowledge 风格：`POST /retrieval`，请求包含 `knowledge_id`、`query` 和 `retrieval_setting`，响应包含 `records`。上传路径接收 `multipart/form-data` 的 `file` 与 `knowledge_id` 字段，响应需返回 `document_id`；删除路径中的 `{document_id}` 会替换为远端文档 ID。

## 使用流程

1. 打开“设置”，确认可用的模型后端。
2. 打开“会话 → Chat → RAG 配置”，保存并启用外部 RAG 连接。
3. 在“知识库”上传文档到外部 RAG，或在“代码知识”导入算法题题解。
4. 等待代码题生成后，在知识库打开本地题库检查三类题目。
5. 在“会话 → Chat”直接询问整个外部知识库；无需选择单个文件。
6. 在“会话 → Work”选择本地目录和后端，再创建可恢复的 Agent 会话。
7. 在“练习”页按算法题练习，或切换到“错题复习”“未回答”；桌宠气泡也可直接答题。
8. 打开“Skill”页，选择代码知识来源和需要沉淀的题库、错题、对话及成长内容，然后查看、编辑或导出 Skill。

支持的主要格式包括 `.md`、`.txt`、`.rst`、`.py`、`.js`、`.ts`、`.java`、`.go`、`.rs`、`.c`、`.cpp`、`.cs`、`.html`、`.css`、`.sql`、`.yaml` 和 `.json`。

## 本地数据与隐私

Windows 默认数据位置：

```text
%APPDATA%\BongoStudy\bongo.db
```

代码正文、题目、逐次作答记录、RAG 文档元数据、会话、默认 Work 工具轨迹和设置均保存在本机；文档正文由所配置的外部 RAG 服务负责保存和索引。模型 API Key 与 RAG API Key 当前以明文保存在本机 SQLite，请勿提交该数据库。使用外部 RAG、云模型、Claude Code 或 Codex 时，相关请求会发送给对应服务。

全局输入监听默认只把事件即时映射为动画信号。设置页中的“匿名活动记录”默认关闭；用户主动开启后，应用仅按 5 分钟时间段保存前台应用进程名、键盘次数、鼠标活跃秒数、点击次数以及首次/最后活动时间。它不保存具体按键、输入文本、窗口标题、鼠标坐标或完整程序路径。连续工作达到 40 分钟时，这段会话的进程名和聚合计数会发送给当前配置的模型，用于生成休息提醒；模型只能据此谨慎推断工作类型，无法得知具体文件、网站、项目或输入内容。用户可以随时暂停记录或清空全部活动历史；`--no-pet` 会禁用桌宠及全局监听。

桌宠模型及交互设计来源于 MIT 许可的 [ayangweb/BongoCat](https://github.com/ayangweb/BongoCat)，许可证和第三方说明见 `THIRD_PARTY_NOTICES.md`。

## 项目结构

```text
bongo/
├── app.py          # Qt 应用入口、单实例与 QML 引擎
├── qml_bridge.py   # QML 与现有业务服务、SQLite、桌宠之间的桥接层
├── qml/            # QML 页面、设计系统和动态图表组件
├── activity.py     # 匿名活动聚合与 Windows 前台应用识别
├── focus_agent.py  # 受限 ReAct 工具调用与结构化休息报告
├── news.py         # Hacker News AI 资讯工具、确定性排序与受限选择 Agent
├── dialogs.py      # 文档题库查看与错题筛选
├── pet.py          # 桌宠绘制、气泡答题和全局输入映射
├── database.py     # SQLite schema、检索、会话和答题记录
├── ingestion.py    # 文件读取、切分和选择题生成
├── rag.py          # 外部 RAG 上传、检索、删除与响应标准化
├── work_agent.py   # 默认 Work ReAct 循环、目录沙箱工具与 Trace
├── providers.py    # OpenAI、Anthropic、Claude Code、Codex 后端
├── memory.py       # 对话上下文、知识检索和来源
├── service.py      # 导入、对话、摘要与恢复流程
├── exporter.py     # Learning Skill 编译、校验与导出
└── styles.py       # 旧 QWidget 面板兼容样式（可用 --legacy-ui 启动）
```

## 验证

```powershell
python -m compileall -q bongo
python -m pytest core_tests
python -m bongo --smoke-test --data-dir .bongo-smoke
```
