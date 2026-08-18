# 上下文管理基准数据

12 组 × 50 轮对话场景，用于测试 bongo 的上下文治理能力。

## 场景列表

| 场景 | 描述 | 测试重点 |
|------|------|----------|
| short_qa | 短问答，结果简短 | 基础上下文增长 |
| long_file_read | 读取大文件（~80 行代码） | 大结果对 prompt 的影响 |
| search_heavy | 大量搜索调用 | 搜索结果累积 |
| write_files | 创建 50 个新文件 | 写操作上下文 |
| mixed_tools | read/patch/search/shell 混合 | 多工具上下文管理 |
| huge_result | 单次返回 3000-5000 字符 | 超长结果压缩 |
| error_heavy | 每 3 轮 1 轮报错 | 错误上下文处理 |
| growing_result | 结果随轮次线性增长 | 渐进增长压缩 |
| realistic_dev | 真实开发流程 | 综合上下文能力 |
| code_review | 代码审查 → 修复 → 验证 | 审查上下文保留 |
| refactor | 跨文件重构 | 多文件重构上下文 |
| debug_investigate | 调试调查追踪 | 调查上下文累积 |

## 数据格式

每行一个 JSON 对象（JSONL），字段：

```json
{
  "round": 1,
  "user": "用户消息",
  "tool_name": "read_file",
  "tool_args": {"path": "README.md"},
  "tool_result": "# README.md\n...",
  "assistant": "助手回复",
  "expected_tool_count": 1,
  "is_error": false
}
```

## 生成

```bash
python benchmarks/generate_context_scenarios.py
```
