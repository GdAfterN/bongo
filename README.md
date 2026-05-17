# bongo

`bongo` 是一个面向代码仓库的轻量本地 coding agent。它直接跑在终端里，先看当前工作区，再用一组受约束的工具去读文件、改文件、跑命令，并把会话状态保存在本地 `.bongo/` 目录里。

它更像一个能在仓库里持续工作的命令行助手，不是纯聊天窗口。你可以拿它做代码排查、测试修复、仓库分析，或者让它在当前项目里执行一次性的工程任务。

## 适合做什么

- 在本地仓库里排查测试失败
- 读取当前代码结构并给出修改建议
- 基于现有文件做小步迭代，而不是脱离仓库空想
- 在会话中保留上下文，支持继续上一次工作

## 主要特性

- 包名是 `bongo`
- CLI 命令是 `bongo`
- 模块入口是 `python -m bongo`
- 会话保存在 `.bongo/sessions/`
- 每次运行的工件保存在 `.bongo/runs/<run_id>/`
- 使用千问百炼大模型

## 使用截图

CLI 帮助信息：

![bongo help](assets/screenshots/bongo-help.png)

启动界面：

![bongo start](assets/screenshots/bongo-start.png)

REPL 内置命令与会话路径：

![bongo repl](assets/screenshots/bongo-repl.png)

## 安装

需要 Python 3.10+。

如果你用 `uv`，直接安装依赖：

```bash
uv sync
```

如果你已经在自己的 Python 环境里工作，也可以直接装成可编辑模式：

```bash
pip install -e .
```

## 快速开始

在当前仓库里启动交互模式：

```bash
uv run bongo
```

指定另一个工作目录：

```bash
uv run bongo --cwd /path/to/repo
```

直接跑一次性任务：

```bash
uv run bongo "inspect the test failures and propose a fix"
```

如果当前环境已经安装过包，也可以直接这样启动：

```bash
python -m bongo
```

## 模型后端

### Ollama

```bash
ollama serve
ollama pull qwen3.5:4b
uv run bongo --provider ollama --model qwen3.5:4b
```

### OpenAI 兼容接口

```bash
export OPENAI_API_BASE="https://your-api.example/v1"
export OPENAI_API_KEY="your-api-key"
export OPENAI_MODEL="gpt-5.4"
uv run bongo --provider openai
```

### Anthropic 兼容接口

```bash
export ANTHROPIC_API_BASE="https://www.right.codes/claude/v1"
export ANTHROPIC_API_KEY="your-api-key"
export ANTHROPIC_MODEL="claude-sonnet-4-6"
uv run bongo --provider anthropic
```

如果你的服务端对多个兼容接口复用了同一套密钥，`bongo` 也支持从 `ANTHROPIC_API_KEY` 回退到 `RIGHT_CODES_API_KEY` 或 `OPENAI_API_KEY`。

## 常用交互命令

- `/help`：查看内置命令
- `/memory`：查看提炼后的工作记忆
- `/session`：查看当前会话文件路径
- `/reset`：清空当前会话状态
- `/exit` 或 `/quit`：退出 REPL

## 安全与持久化

`bongo` 不会默认把所有动作都放开。像 shell 执行、文件写入这类高风险操作，会受审批模式控制：

- `--approval ask`
- `--approval auto`
- `--approval never`

每次运行结束后，都会在 `.bongo/runs/<run_id>/` 下写出这些文件：

- `task_state.json`
- `trace.jsonl`
- `report.json`

这些内容默认只保存在本地，不需要跟仓库一起提交。

## 开发

如果装了 Ruff，可以这样检查：

```bash
uv run ruff check .
```


## 启动服务

有两种方式启动 Web 服务，推荐使用一键启动脚本。

### 方式一：使用启动脚本（推荐）

在项目根目录执行：

```powershell
cd D:\aiAgentStudy\bongo-main
python scripts\start_web.py
```


这个脚本会自动完成以下操作：
1. 检查 API 密钥是否已配置
2. 启动 Flask 后端服务器（默认端口 5000）
3. 等待 2 秒让服务器完全启动
4. 自动在默认浏览器中打开前端页面

启动成功后，终端会显示类似以下信息：
```
🚀 Starting Bongo Web Interface...
📡 Starting backend server on http://localhost:5000
🌐 Opening web interface...

✅ Bongo Web Interface is running!
   Backend: http://localhost:5000
   Frontend: file:///D:/aiAgentStudy/bongo-main/bongo/web/index.html

Press Ctrl+C to stop the server.
```


### 方式二：手动分别启动

如果需要使用自定义配置或调试，可以分别启动前后端。

**步骤 1：启动后端服务器**

```powershell
cd D:\aiAgentStudy\bongo-main\bongo\web
python server.py
```


后端服务器会在 `http://0.0.0.0:5000` 上运行，监听所有网络接口。

**步骤 2：访问前端页面**

有两种方式访问前端：

- **直接打开文件**：用浏览器打开 `D:\aiAgentStudy\bongo-main\bongo\web\index.html`
- **通过本地服务器**（推荐，避免 CORS 问题）：
  ```powershell
  cd D:\aiAgentStudy\bongo-main\bongo\web
  python -m http.server 8080
  ```

  然后在浏览器访问 `http://localhost:8080`

## 访问服务

服务启动后，可以通过以下方式访问：

### 前端界面访问

- **文件方式**：直接在浏览器地址栏输入 `file:///D:/aiAgentStudy/bongo-main/bongo/web/index.html`
- **HTTP 方式**：如果使用 Python HTTP 服务器，访问 `http://localhost:8080`

前端界面包含两个主要区域：
- **左侧边栏**：显示所有会话历史列表，可以点击切换不同会话，顶部有"新建对话"按钮
- **右侧主区域**：聊天窗口，显示消息历史和输入框

### 后端 API 访问

后端提供 RESTful API，可以通过浏览器或工具（如 Postman、curl）访问：

**健康检查：**
```
GET http://localhost:5000/api/health
```


返回示例：
```json
{
  "status": "healthy",
  "workspace": "D:\\aiAgentStudy\\bongo-main",
  "model": "qwen3.5-plus-2026-02-15",
  "provider": "openai"
}
```


**获取会话列表：**
```
GET http://localhost:5000/api/sessions
```


**获取指定会话详情：**
```
GET http://localhost:5000/api/sessions/{session_id}
```


**发送消息：**
```
POST http://localhost:5000/api/chat
Content-Type: application/json

{
  "message": "你好，请帮我分析一下这个项目",
  "session_id": "可选的会话ID，不填则创建新会话"
}
```


**删除会话：**
```
DELETE http://localhost:5000/api/sessions/{session_id}
```


**查看工作记忆：**
```
GET http://localhost:5000/api/sessions/{session_id}/memory
```


## 使用流程

1. **首次使用**：打开前端页面后，左侧会话列表为空。点击"新建对话"按钮或直接在下方的输入框中输入消息并发送，系统会自动创建一个新会话。

2. **发送消息**：在底部输入框中输入你的问题或任务描述，按 Enter 键或点击"发送"按钮。AI 会处理你的请求并在右侧显示回复。支持 Markdown 格式和代码高亮。

3. **切换会话**：点击左侧会话列表中的任意会话，即可加载该会话的历史记录并继续对话。

4. **查看记忆**：在聊天窗口右上角点击"查看记忆"按钮，可以看到 AI 当前维护的工作记忆，包括任务摘要、最近访问的文件、文件摘要等信息。

5. **删除会话**：点击"删除会话"按钮可以删除当前会话及其所有历史记录。

6. **停止服务**：在运行启动脚本的终端中按 `Ctrl+C` 即可停止后端服务器。

## 常见问题

**Q: 启动时提示 "No API key found"**
A: 需要设置 `BONGO_API_KEY` 或 `OPENAI_API_KEY` 环境变量。在 PowerShell 中使用 `$env:BONGO_API_KEY="你的密钥"` 设置。

**Q: 前端页面无法连接到后端**
A: 检查后端服务器是否正常启动（访问 http://localhost:5000/api/health），确认没有防火墙阻止 5000 端口。

**Q: 会话数据保存在哪里**
A: 所有会话数据保存在 `D:\aiAgentStudy\bongo-main\.bongo\sessions\` 目录下，以 JSON 格式存储。每次运行的详细日志保存在 `.bongo\runs\` 目录。

**Q: 如何更改工作区路径**
A: 设置 `BONGO_WORKSPACE` 环境变量为你想要的工作目录路径，例如：`$env:BONGO_WORKSPACE="D:\your\project\path"`

**Q: 支持哪些模型**
A: 支持 OpenAI 兼容接口（默认阿里云千问）、Ollama 本地模型、Anthropic Claude 等。通过设置 `BONGO_PROVIDER` 环境变量切换。

**Q: 如何修改默认端口**
A: 编辑 `bongo\web\server.py` 文件最后一行，将 `app.run(host='0.0.0.0', port=5000, debug=True)` 中的 5000 改为其他端口号。