这是一个 Windows 优先、本地数据优先的 AI 智能助学桌面应用（桌宠 BongoCat）。用户导入本地资料（Markdown / 文本 / 代码），应用自动生成四选一练习题，通过置顶桌宠定时弹出气泡复习；支持绑定单一文档的知识问答、错题与未答题复习，并把学习成果导出为可复用的 Learning Skill。

核心闭环：

```text
导入本地资料 -> 拆解并保存 -> 模型生成选择题 -> 桌面气泡复习
                     \-> 检索资料 -> 来源限定对话 -> 错题与洞察沉淀
                                     \-> Learning Skill 编译 -> 定向复习
```

技术要点：

- 单条模型调用链路：出题和问答都是单轮 LLM 调用 + JSON Schema / Pydantic 校验 + 瞬态错误重试（`bongo/ingestion.py`、`bongo/providers.py`）
- 对话必须绑定一份已导入资料，检索与引用不跨文档（`bongo/memory.py`）
- 桌面 UI：PySide6；桌宠渲染：Qt WebEngine + PixiJS + easy-live2d BongoCat 模型；全局输入：pynput（`bongo/pet.py`）
- 本地存储：SQLite + FTS5 关键词检索（`bongo/database.py`）
- Skill 导出为 SKILL.md / manifest.json / references/ 的渐进式披露结构，记录来源哈希、版本与 dirty 标记（`bongo/exporter.py`）
- 桌宠成长只反映真实学习行为（学习事件账本），不做亲密度 / 等级系统

当前版本不包含工具调用、Shell / 文件编辑 Agent、ReAct、MCP（历史 `/ask` 链路已移除，CLI 后端仅作无工具问答适配）。CLI 命令 `/note`、`/mistake`、`/ask` 等仅存在于 `CHANGELOG.md` 的历史记录中，不代表当前代码状态。
