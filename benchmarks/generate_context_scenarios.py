"""生成 12 组 × 50 轮上下文管理基准对话。

每组 JSONL 文件包含 50 轮完整对话记录（user → tool_use → tool_result → assistant）。
覆盖：短问答、长文件读取、搜索密集、写文件、混合工具、超长结果、错误密集、
渐进增长、真实开发、代码审查、重构、调试调查。
"""

import json
import os
import random
import string
from pathlib import Path

random.seed(42)

OUT_DIR = Path(__file__).parent / "context"
OUT_DIR.mkdir(exist_ok=True)

ROUNDS = 50


def _rand_code_lines(n):
    """生成 n 行随机 Python 代码。"""
    lines = []
    for i in range(n):
        indent = random.choice(["", "    ", "        "])
        line = random.choice([
            f"def func_{i}():",
            f"    return {random.randint(0, 999)}",
            f"x_{i} = {random.randint(0, 999)}",
            f"# comment {i}",
            f"class Class{i}:",
            f"    pass",
            f"for i_{i} in range({random.randint(1, 100)}):",
            f"    print(i_{i})",
            f"if x_{i} > {random.randint(0, 100)}:",
            f"    x_{i} += 1",
        ])
        lines.append(indent + line)
    return "\n".join(lines)


def _rand_log_lines(n):
    """生成 n 行随机日志。"""
    levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    modules = ["auth", "db", "api", "cache", "scheduler"]
    msgs = [
        "Connection established",
        "Query executed in 12ms",
        "Timeout waiting for response",
        "Cache miss for key",
        "User login successful",
        "Rate limit exceeded",
        "Health check passed",
        "Memory usage: 45%",
        "Request processed",
        "Background job started",
    ]
    lines = []
    for i in range(n):
        lvl = random.choice(levels)
        mod = random.choice(mods := modules)
        msg = random.choice(msgs)
        lines.append(f"[2026-01-01 12:{i:02d}:00] {lvl} [{mod}] {msg}")
    return "\n".join(lines)


def _write_scenario(name, rounds_data):
    """写入单个场景 JSONL 文件。"""
    path = OUT_DIR / f"{name}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for rd in rounds_data:
            f.write(json.dumps(rd, ensure_ascii=False) + "\n")
    print(f"  {name}.jsonl — {len(rounds_data)} 轮")


# ── 1. short_qa ──────────────────────────────────────────────────
def gen_short_qa():
    """短问答：简短问题，工具返回也很短。"""
    files = ["README.md", "config.json", "setup.py", "Makefile", ".env.example",
             "pyproject.toml", "requirements.txt", "Dockerfile", ".gitignore", "CHANGELOG.md"]
    rounds = []
    for i in range(ROUNDS):
        fname = files[i % len(files)]
        user_msg = f"请告诉我 {fname} 里写了什么内容？"
        tool_args = {"path": fname}
        tool_result = f"# {fname}\n\n这是一个示例文件的第 {i+1} 行内容。配置项 = 值{i+1}"
        rounds.append({
            "round": i + 1,
            "user": user_msg,
            "tool_name": "read_file",
            "tool_args": tool_args,
            "tool_result": tool_result,
            "assistant": f"`{fname}` 的内容是：\n\n```\n{tool_result}\n```\n\n这是一个简单的配置文件。",
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 2. long_file_read ─────────────────────────────────────────────
def gen_long_file_read():
    """长文件读取：每次读一个大文件（~80 行代码）。"""
    modules = ["models", "views", "serializers", "urls", "middleware",
               "permissions", "filters", "pagination", "signals", "tasks",
               "utils", "constants"]
    rounds = []
    for i in range(ROUNDS):
        mod = modules[i % len(modules)]
        fname = f"api/{mod}.py"
        code = _rand_code_lines(80)
        user_msg = f"帮我看看 {fname} 的代码实现"
        rounds.append({
            "round": i + 1,
            "user": user_msg,
            "tool_name": "read_file",
            "tool_args": {"path": fname},
            "tool_result": f"# {fname}\n\n{code}",
            "assistant": f"已读取 `{fname}`，该文件包含 {80} 行代码，主要实现了 {mod} 相关功能。",
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 3. search_heavy ───────────────────────────────────────────────
def gen_search_heavy():
    """搜索密集：大量 search 调用，结果含多个匹配项。"""
    keywords = ["import", "class", "def", "return", "if", "for", "try", "except",
                "async", "await", "yield", "lambda", "with", "raise", "assert"]
    rounds = []
    for i in range(ROUNDS):
        kw = keywords[i % len(keywords)]
        user_msg = f"搜索项目中所有使用了 `{kw}` 的地方"
        matches = [f"src/file_{j}.py:{random.randint(1,200)}: {kw} something" for j in range(random.randint(5, 25))]
        tool_result = "\n".join(matches)
        rounds.append({
            "round": i + 1,
            "user": user_msg,
            "tool_name": "search",
            "tool_args": {"pattern": kw, "path": "."},
            "tool_result": tool_result,
            "assistant": f"搜索 `{kw}` 找到 {len(matches)} 个匹配项，分布在多个文件中。",
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 4. write_files ────────────────────────────────────────────────
def gen_write_files():
    """写文件：每次创建一个新文件。"""
    templates = [
        ("src/api/{name}.py", "from rest_framework import views\n\nclass {Name}View(views.APIView):\n    def get(self, request):\n        return Response({{'ok': True}})"),
        ("tests/test_{name}.py", "import pytest\n\ndef test_{name}():\n    assert True"),
        ("src/models/{name}.py", "from django.db import models\n\nclass {Name}(models.Model):\n    name = models.CharField(max_length=100)"),
        ("docs/{name}.md", "# {Name}\n\n## 概述\n\n这是 {name} 模块的文档。"),
        ("scripts/{name}.py", "#!/usr/bin/env python\n\ndef main():\n    print('running {name}')\n\nif __name__ == '__main__':\n    main()"),
    ]
    rounds = []
    for i in range(ROUNDS):
        tpl = templates[i % len(templates)]
        name = f"module_{i+1}"
        path = tpl[0].format(name=name, Name=name.title().replace("_", ""))
        content = tpl[1].format(name=name, Name=name.title().replace("_", ""))
        user_msg = f"创建文件 {path}"
        rounds.append({
            "round": i + 1,
            "user": user_msg,
            "tool_name": "write_file",
            "tool_args": {"path": path, "content": content},
            "tool_result": f"wrote {path} ({len(content)} chars)",
            "assistant": f"已创建 `{path}`，包含 {len(content)} 字符的代码。",
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 5. mixed_tools ────────────────────────────────────────────────
def gen_mixed_tools():
    """混合工具：read → patch → search → shell 循环。"""
    tool_cycle = ["read_file", "patch_file", "search", "run_shell"]
    rounds = []
    for i in range(ROUNDS):
        tool = tool_cycle[i % 4]
        if tool == "read_file":
            fname = f"src/app_{i//4 + 1}.py"
            user_msg = f"读取 {fname}"
            tool_args = {"path": fname}
            tool_result = f"# {fname}\ndef func():\n    return {random.randint(0, 999)}"
        elif tool == "patch_file":
            fname = f"src/app_{i//4 + 1}.py"
            user_msg = f"修改 {fname} 中的函数"
            tool_args = {"path": fname, "old_text": "return 123", "new_text": "return 456"}
            tool_result = f"patched {fname}"
        elif tool == "search":
            user_msg = "搜索 TODO 注释"
            tool_args = {"pattern": "TODO", "path": "."}
            tool_result = f"src/app_{i//4+1}.py:10: TODO: fix this\nsrc/app_{i//4+1}.py:25: TODO: refactor"
        else:
            user_msg = "运行测试"
            tool_args = {"command": "pytest tests/", "timeout": 30}
            tool_result = f"exit_code: 0\nstdout: {random.randint(5, 50)} passed"
        rounds.append({
            "round": i + 1,
            "user": user_msg,
            "tool_name": tool,
            "tool_args": tool_args,
            "tool_result": tool_result,
            "assistant": f"已完成 {tool} 操作。",
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 6. huge_result ────────────────────────────────────────────────
def gen_huge_result():
    """超长结果：单次工具返回 3000-5000 字符。"""
    rounds = []
    for i in range(ROUNDS):
        size = 3000 + i * 40  # 渐进增长
        fname = f"logs/app_{i}.log"
        user_msg = f"分析日志文件 {fname}"
        tool_result = _rand_log_lines(size // 50)  # 每行约50字符
        rounds.append({
            "round": i + 1,
            "user": user_msg,
            "tool_name": "read_file",
            "tool_args": {"path": fname},
            "tool_result": tool_result,
            "assistant": f"已分析 `{fname}`，日志包含 {len(tool_result)} 字符，发现若干关键事件。",
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 7. error_heavy ────────────────────────────────────────────────
def gen_error_heavy():
    """错误密集：每 3 轮中 1 轮返回错误。"""
    rounds = []
    for i in range(ROUNDS):
        is_error = (i % 3 == 0)
        fname = f"src/missing_{i}.py"
        user_msg = f"读取文件 {fname}"
        if is_error:
            tool_result = f"error: file not found: {fname}"
            assistant = f"抱歉，`{fname}` 文件不存在。请检查路径是否正确。"
        else:
            tool_result = f"# {fname}\ndef func_{i}():\n    return {i}"
            assistant = f"已读取 `{fname}`，文件内容正常。"
        rounds.append({
            "round": i + 1,
            "user": user_msg,
            "tool_name": "read_file",
            "tool_args": {"path": fname},
            "tool_result": tool_result,
            "assistant": assistant,
            "expected_tool_count": i + 1,
            "is_error": is_error,
        })
    return rounds


# ── 8. growing_result ─────────────────────────────────────────────
def gen_growing_result():
    """渐进增长：工具结果随轮次线性增长。"""
    rounds = []
    for i in range(ROUNDS):
        size = 100 * (i + 1)  # 100, 200, 300, ... 5000
        fname = f"data/dataset_{i}.csv"
        user_msg = f"读取数据集 {fname}"
        tool_result = ",".join([f"col_{j}" for j in range(10)]) + "\n" + \
                      "\n".join([",".join([str(random.randint(0, 999)) for _ in range(10)])
                                 for _ in range(size // 50)])
        rounds.append({
            "round": i + 1,
            "user": user_msg,
            "tool_name": "read_file",
            "tool_args": {"path": fname},
            "tool_result": tool_result,
            "assistant": f"已读取 `{fname}`，数据集大小 {size} 字节。",
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 9. realistic_dev ──────────────────────────────────────────────
def gen_realistic_dev():
    """真实开发：模拟完整开发流程。"""
    dev_cycle = [
        ("read_file", lambda i: {"path": f"src/feature_{i//5+1}.py"},
         lambda i: f"# feature_{i//5+1}.py\ndef process():\n    data = load()\n    return transform(data)"),
        ("search", lambda i: {"pattern": "TODO", "path": "."},
         lambda i: f"src/feature_{i//5+1}.py:5: TODO: add validation"),
        ("patch_file", lambda i: {"path": f"src/feature_{i//5+1}.py", "old_text": "return transform(data)", "new_text": "validated = validate(data)\n    return transform(validated)"},
         lambda i: f"patched src/feature_{i//5+1}.py"),
        ("run_shell", lambda i: {"command": "pytest tests/", "timeout": 30},
         lambda i: f"exit_code: 0\nstdout: {random.randint(10, 100)} passed, 0 failed"),
        ("write_file", lambda i: {"path": f"tests/test_feature_{i//5+1}.py", "content": f"def test_process():\n    assert process() is not None"},
         lambda i: f"wrote tests/test_feature_{i//5+1}.py"),
    ]
    user_msgs = [
        "帮我看看这段代码",
        "有没有 TODO 需要处理",
        "加上验证逻辑",
        "运行测试确认一下",
        "补个单元测试",
    ]
    rounds = []
    for i in range(ROUNDS):
        idx = i % 5
        tool_name, args_fn, result_fn = dev_cycle[idx]
        user_msg = user_msgs[idx]
        tool_args = args_fn(i)
        tool_result = result_fn(i)
        rounds.append({
            "round": i + 1,
            "user": user_msg,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "tool_result": tool_result,
            "assistant": f"已完成：{tool_name}。{['查看代码', '找到TODO', '代码已修改', '测试通过', '测试已创建'][idx]}。",
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 10. code_review ───────────────────────────────────────────────
def gen_code_review():
    """代码审查：读取 → 搜索问题 → 修复 → 验证。"""
    bugs = ["未处理空值", "SQL 注入风险", "未关闭连接", "类型错误", "逻辑错误",
            "竞态条件", "内存泄漏", "未验证输入", "硬编码密钥", "异常吞没"]
    rounds = []
    for i in range(ROUNDS):
        cycle = i % 4
        fname = f"src/service_{i//4+1}.py"
        if cycle == 0:
            user_msg = f"审查 {fname} 的代码质量"
            tool_result = f"# {fname}\ndef query(user_id):\n    sql = f'SELECT * FROM users WHERE id={{user_id}}'\n    return db.execute(sql)"
            tool_name, tool_args = "read_file", {"path": fname}
            assistant = f"已读取 `{fname}`，发现潜在问题：{bugs[i % len(bugs)]}。"
        elif cycle == 1:
            user_msg = f"搜索 {fname} 中的安全问题"
            tool_result = f"{fname}:3: SQL injection risk\n{fname}:5: no input validation"
            tool_name, tool_args = "search", {"pattern": "sql|inject|exec", "path": fname}
            assistant = f"发现 {random.randint(2, 5)} 个安全问题。"
        elif cycle == 2:
            user_msg = f"修复 {fname} 中的问题"
            tool_result = f"patched {fname}"
            tool_name, tool_args = "patch_file", {"path": fname, "old_text": "f'SELECT", "new_text": "parameterized query"}
            assistant = "已使用参数化查询修复 SQL 注入风险。"
        else:
            user_msg = "验证修复后的代码"
            tool_result = f"exit_code: 0\nstdout: {random.randint(5, 20)} passed"
            tool_name, tool_args = "run_shell", {"command": "pytest tests/", "timeout": 30}
            assistant = "所有测试通过，修复有效。"
        rounds.append({
            "round": i + 1, "user": user_msg,
            "tool_name": tool_name, "tool_args": tool_args,
            "tool_result": tool_result, "assistant": assistant,
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 11. refactor ──────────────────────────────────────────────────
def gen_refactor():
    """重构：跨多个文件的系统性重构。"""
    files = ["models.py", "views.py", "serializers.py", "urls.py", "tests.py",
             "utils.py", "constants.py", "permissions.py", "middleware.py", "signals.py"]
    rounds = []
    for i in range(ROUNDS):
        cycle = i % 5
        fname = f"app/{files[i // 5 % len(files)]}"
        if cycle < 2:
            user_msg = f"读取 {fname} 了解当前结构"
            tool_name = "read_file"
            tool_args = {"path": fname}
            tool_result = f"# {fname}\nclass OldName:\n    def method(self):\n        pass"
            assistant = f"已读取 `{fname}`，确认需要重构的代码结构。"
        elif cycle == 2:
            user_msg = f"重构 {fname} 中的类名"
            tool_name = "patch_file"
            tool_args = {"path": fname, "old_text": "class OldName:", "new_text": "class NewName:"}
            tool_result = f"patched {fname}"
            assistant = f"已将 `OldName` 重命名为 `NewName`。"
        elif cycle == 3:
            user_msg = "搜索项目中所有引用旧类名的地方"
            tool_name = "search"
            tool_args = {"pattern": "OldName", "path": "."}
            matches = [f"{files[j]}.py:10: OldName" for j in range(random.randint(2, 8))]
            tool_result = "\n".join(matches)
            assistant = f"找到 {len(matches)} 处引用需要更新。"
        else:
            user_msg = "运行测试确认重构正确"
            tool_name = "run_shell"
            tool_args = {"command": "pytest", "timeout": 30}
            tool_result = f"exit_code: 0\nstdout: {random.randint(20, 100)} passed"
            assistant = "测试全部通过，重构无破坏性变更。"
        rounds.append({
            "round": i + 1, "user": user_msg,
            "tool_name": tool_name, "tool_args": tool_args,
            "tool_result": tool_result, "assistant": assistant,
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 12. debug_investigate ─────────────────────────────────────────
def gen_debug_investigate():
    """调试调查：追踪问题根因。"""
    symptoms = ["接口超时", "内存溢出", "数据不一致", "并发冲突", "缓存失效",
                "认证失败", "文件损坏", "网络超时", "死锁", "性能下降"]
    rounds = []
    for i in range(ROUNDS):
        cycle = i % 6
        symptom = symptoms[i // 6 % len(symptoms)]
        if cycle == 0:
            user_msg = f"用户报告了{symptom}问题，帮我排查"
            tool_name = "read_file"
            tool_args = {"path": "logs/error.log"}
            tool_result = _rand_log_lines(30)
            assistant = f"查看错误日志，发现与{symptom}相关的异常。"
        elif cycle == 1:
            user_msg = "看看相关代码"
            tool_name = "read_file"
            tool_args = {"path": f"src/handler_{i//6+1}.py"}
            tool_result = f"def handle(req):\n    result = process(req)\n    return result"
            assistant = "已读取处理代码，需要检查依赖。"
        elif cycle == 2:
            user_msg = "搜索相关错误码"
            tool_name = "search"
            tool_args = {"pattern": "error|exception|timeout", "path": "src/"}
            tool_result = f"src/handler_{i//6+1}.py:5: TimeoutError\nsrc/handler_{i//6+1}.py:12: ConnectionError"
            assistant = f"找到 {random.randint(3, 10)} 处异常处理代码。"
        elif cycle == 3:
            user_msg = "查看配置"
            tool_name = "read_file"
            tool_args = {"path": "config/production.yaml"}
            tool_result = "timeout: 30\nmax_connections: 100\nretry: 3"
            assistant = "配置看起来合理，需要加日志定位。"
        elif cycle == 4:
            user_msg = "加调试日志"
            tool_name = "patch_file"
            tool_args = {"path": f"src/handler_{i//6+1}.py", "old_text": "result = process(req)", "new_text": "logger.debug(f'processing {req}')\n    result = process(req)\n    logger.debug(f'result: {result}')"}
            tool_result = f"patched src/handler_{i//6+1}.py"
            assistant = "已添加调试日志，可以复现问题。"
        else:
            user_msg = "运行复现测试"
            tool_name = "run_shell"
            tool_args = {"command": f"python -m pytest tests/test_debug_{i//6+1}.py -v", "timeout": 60}
            tool_result = f"exit_code: 1\nFAILED: test_{symptom.replace(' ', '_')}\nAssertionError: expected 200 but got 500"
            assistant = f"已复现{symptom}问题，根因定位完成。"
        rounds.append({
            "round": i + 1, "user": user_msg,
            "tool_name": tool_name, "tool_args": tool_args,
            "tool_result": tool_result, "assistant": assistant,
            "expected_tool_count": i + 1,
        })
    return rounds


# ── 主函数 ────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("生成 12 组 × 50 轮上下文管理基准对话")
    print("=" * 60)

    generators = [
        ("short_qa", gen_short_qa),
        ("long_file_read", gen_long_file_read),
        ("search_heavy", gen_search_heavy),
        ("write_files", gen_write_files),
        ("mixed_tools", gen_mixed_tools),
        ("huge_result", gen_huge_result),
        ("error_heavy", gen_error_heavy),
        ("growing_result", gen_growing_result),
        ("realistic_dev", gen_realistic_dev),
        ("code_review", gen_code_review),
        ("refactor", gen_refactor),
        ("debug_investigate", gen_debug_investigate),
    ]

    for name, gen_fn in generators:
        rounds = gen_fn()
        _write_scenario(name, rounds)

    print("\n" + "=" * 60)
    print(f"生成完成！共 {len(generators)} 个场景 × {ROUNDS} 轮 = {len(generators) * ROUNDS} 轮对话")
    print(f"输出目录: {OUT_DIR}")
    print("=" * 60)
