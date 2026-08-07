# Bongo Study

Bongo Study 是一个 Windows 优先、本地数据优先的桌面智能助学 Agent。它把 Markdown、文本和代码文件作为桌宠的知识来源：导入资料后按文件类型拆解并生成四选一练习题，支持文档级知识对话、错题复习、历史会话恢复和针对性学习 Skill 导出。

项目当前聚焦可完整运行的核心闭环：

```text
导入本地资料 -> 切分并保存 -> 模型生成选择题 -> 桌面气泡复习
                         \-> 检索资料 -> 知识对话 -> 保存会话
```

## 已实现功能

- PySide6 原生桌面界面，包含文档对话、知识库、题库、练习和设置页面
- 透明、置顶、可拖动的 BongoCat Live2D 桌宠窗口
- 全局键盘、鼠标左右键和鼠标移动映射为桌宠动作
- 桌宠显示、透明度、尺寸、置顶、点击穿透、镜像、键鼠响应和答题等待时间设置
- 导入 Markdown、纯文本及常见代码文件，单文件上限 2 MB
- 导入后调用模型生成 3 到 5 道严格四选一题目
- Markdown、代码、SQL、配置数据、前端结构和脚本使用不同的拆解与出题 Prompt
- 桌宠每隔一段时间弹出已保存题目，可直接作答；超时后自动收起并加入未回答列表
- 按文档查看题库，保留逐次答题记录并支持错题与未回答题目复习
- SQLite 本地数据库与 FTS5/关键词检索
- 对话必须绑定一份导入资料，检索和引用不会混入其他文档
- 对话上下文、长期摘要、历史会话保存与恢复
- OpenAI、Anthropic 官方 Python SDK
- 对话后端可选默认、CC 或 Codex；CLI 仅在实际可执行时可选
- 将逐文档知识、题库、错题、学习画像和会话导出为本地 skill

当前版本不包含工具调用、Shell/文件编辑 Agent、ReAct、MCP、评测系统、视频生成，也暂不实现宠物升级、亲密度和装备系统。

## 技术栈

| 模块 | 实现 |
|---|---|
| 桌面 UI | Python 3.11+、PySide6 / Qt 6 |
| 桌宠渲染 | Qt WebEngine、PixiJS、`easy-live2d`、ayangweb/BongoCat 模型 |
| 全局输入 | `pynput` |
| 本地存储 | SQLite、FTS5 |
| 数据校验 | Pydantic |
| 模型 | OpenAI SDK、Anthropic SDK、Claude Code CLI、Codex CLI |

## 快速开始

需要 Python 3.11 或更高版本。Windows PowerShell：

```powershell
python -m pip install -e .
python -m bongo
```

也可以使用安装后的命令：

```powershell
bongo
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

在应用的“设置”页选择 `openai` 或 `anthropic`，用于资料导入出题和内置对话后端。OpenAI 后端使用 Responses API；当 Base URL 只有服务根地址时，应用会自动补全 `/v1`。

文档对话后端可以单独选择：

- `默认`：使用本地文档检索、历史消息和长期摘要，再调用上方 SDK 模型
- `CC`：使用系统 `PATH` 中可执行的 Claude Code CLI
- `Codex`：使用系统 `PATH` 中可执行的 Codex CLI

应用会实际执行 CLI 的 `--version` 检查，而不只判断文件是否存在。CC 或 Codex 未安装、未加入 `PATH` 或不可执行时，选择该项会立即提示并保留上一个可用后端；不会自动从默认后端切换到 CLI Agent。

Claude Code 每次请求均使用非持久化、无工具参数运行：

```text
--safe-mode --disable-slash-commands --no-session-persistence --tools ""
```

Claude Code 不会获得文件、Shell 或其他工具；应用自身的 SQLite 数据库是会话记录的唯一真源。

Codex 使用非交互 `exec` 模式并启用 `read-only` 沙箱；文档片段、历史消息和系统提示由应用通过标准输入传递。

## 使用流程

1. 打开“设置”，确认可用的模型后端。
2. 打开“知识库”，导入一个或多个资料文件。
3. 等待模型完成题目生成，资料和题目会保存在本地数据库。
4. 在知识库点击“查看题库”，检查该文档生成的题目与作答情况。
5. 在“对话”中先选择一份资料，再针对该文档提问并查看来源。
6. 在“练习”页按文档练习，或切换到“错题复习”“未回答”；桌宠气泡也可直接答题。
7. 在“设置”页调整 BongoCat，或导出包含学习画像和错题复盘流程的 skill。

支持的主要格式包括 `.md`、`.txt`、`.rst`、`.py`、`.js`、`.ts`、`.java`、`.go`、`.rs`、`.c`、`.cpp`、`.cs`、`.html`、`.css`、`.sql`、`.yaml` 和 `.json`。

## 本地数据与隐私

Windows 默认数据位置：

```text
%APPDATA%\BongoStudy\bongo.db
```

知识正文、切分结果、题目、逐次作答记录、文档绑定对话和设置均保存在本机。API Key 当前以明文保存在本机 SQLite，请勿提交该数据库。使用云模型、Claude Code 或 Codex 时，请求所需的资料片段会发送给对应模型服务。

全局输入监听只把事件即时映射为动画信号：不记录具体按键，不保存鼠标坐标，也不把键鼠事件发送给模型。可使用 `--no-pet` 禁用桌宠及全局监听。

桌宠模型及交互设计来源于 MIT 许可的 [ayangweb/BongoCat](https://github.com/ayangweb/BongoCat)，许可证和第三方说明见 `THIRD_PARTY_NOTICES.md`。

## 项目结构

```text
bongo/
├── app.py          # PySide6 主窗口、后台任务和系统托盘
├── dialogs.py      # 文档题库查看与错题筛选
├── pet.py          # 桌宠绘制、气泡答题和全局输入映射
├── database.py     # SQLite schema、检索、会话和答题记录
├── ingestion.py    # 文件读取、切分和选择题生成
├── providers.py    # OpenAI、Anthropic、Claude Code、Codex 后端
├── memory.py       # 对话上下文、知识检索和来源
├── service.py      # 导入、对话、摘要与恢复流程
├── exporter.py     # 学习 skill 导出
└── styles.py       # 桌面 UI 样式
```

## 验证

```powershell
python -m compileall -q bongo
python -m pytest core_tests
python -m bongo --smoke-test --data-dir .bongo-smoke
```
