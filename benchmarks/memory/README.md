# 记忆管理基准数据

12 组 × 30 轮对话场景，用于测试 bongo 的记忆管理能力。

## 场景列表

| 场景 | 描述 | 测试重点 |
|------|------|----------|
| single_file_reread | 单文件反复询问 30 轮 | 记忆复用，避免重复读文件 |
| rotate_files | 10 个文件轮换读取 | recent_files 截断（最多 8 个） |
| stale_summary | 文件被修改，摘要过时 | file_freshness 检测，摘要刷新 |
| multi_file | 20 个文件依次读取 | 记忆容量截断 |
| no_summary | 用户从不说明任务目的 | 任务摘要推断能力 |
| task_change | 中途切换任务 | 记忆适应新任务 |
| note_heavy | 每轮添加笔记 | 笔记累积与截断 |
| file_deleted | 先读后删 | 记忆一致性 |
| large_codebase | 20+ 文件项目 | 记忆预算控制 |
| incremental_build | 逐步构建功能模块 | 渐进式记忆累积 |
| cross_file_ref | 跨文件依赖分析 | 多文件关联记忆 |
| realistic_dev | 真实开发会话 | 综合记忆能力 |

## 数据格式

每行一个 JSON 对象（JSONL），字段：

```json
{
  "round": 1,
  "user": "用户消息",
  "tool_name": "read_file",
  "tool_args": {"path": "src/app.py"},
  "tool_result": "# src/app.py\n...",
  "assistant": "助手回复",
  "memory_snapshot": {
    "recent_files": ["src/app.py"],
    "task_summary": "开发 Flask 应用"
  }
}
```

`tool_name` 为 `null` 表示该轮无需工具调用（直接从记忆回答）。

## 生成

```bash
python benchmarks/generate_memory_scenarios.py
```
