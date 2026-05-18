"""AgentTestSuite — 统一测试入口。

一个类跑三套测试：
  1. tool_security:   15个工具边界与调用安全性场景
  2. context:         12组长上下文压缩能力
  3. memory:          记忆依赖实验
  4. drift:           12个workspace漂移检测

用法：
    from tests.test_agent import AgentTestSuite
    t = AgentTestSuite()
    t.run_all()          # 跑全部，打印4张表
    t.run_tool_security() # 只跑工具安全，返回结果列表
    t.run_context()       # 只跑上下文压缩
    t.run_memory()        # 只跑记忆实验
    t.run_drift()         # 只跑漂移检测
"""

import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from bongo import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from bongo.memory import append_note, retrieval_candidates


# ════════════════════════════════════════════════
# 工具安全场景定义
# ════════════════════════════════════════════════

TOOL_SECURITY_SCENARIOS = [
    {
        "id": "path_traversal",
        "category": "path_security",
        "desc": "read_file 尝试 ../ 逃逸工作区",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"read_file","args":{"path":"../etc/passwd","start":1,"end":1}}</tool>', "<final>done</final>"],
        "check": lambda result: "path escapes workspace" in result,
    },
    {
        "id": "search_path_escape",
        "category": "path_security",
        "desc": "search 尝试搜索工作区外路径",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"search","args":{"pattern":"secret","path":"../outside"}}</tool>', "<final>done</final>"],
        "check": lambda result: "path escapes workspace" in result,
    },
    {
        "id": "write_path_traversal",
        "category": "path_security",
        "desc": "write_file 尝试写到工作区外",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"write_file","args":{"path":"../evil.txt","content":"hacked"}}</tool>', "<final>done</final>"],
        "check": lambda result: "path escapes workspace" in result,
    },
    {
        "id": "empty_read_args",
        "category": "param_validation",
        "desc": "read_file 不传 path 参数",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"read_file","args":{}}</tool>', "<final>done</final>"],
        "check": lambda result: "invalid arguments" in result and "path" in result,
    },
    {
        "id": "empty_write_args",
        "category": "param_validation",
        "desc": "write_file 不传 path 参数",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"write_file","args":{}}</tool>', "<final>done</final>"],
        "check": lambda result: "invalid arguments" in result and "path" in result,
    },
    {
        "id": "empty_shell_command",
        "category": "param_validation",
        "desc": "run_shell 传空命令",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"run_shell","args":{"command":"","timeout":20}}</tool>', "<final>done</final>"],
        "check": lambda result: "command must not be empty" in result,
    },
    {
        "id": "shell_timeout_out_of_range",
        "category": "param_validation",
        "desc": "run_shell 超时超过120秒",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"run_shell","args":{"command":"echo hi","timeout":999}}</tool>', "<final>done</final>"],
        "check": lambda result: "timeout must be in [1, 120]" in result,
    },
    {
        "id": "shell_timeout_zero",
        "category": "param_validation",
        "desc": "run_shell 超时为0",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"run_shell","args":{"command":"echo hi","timeout":0}}</tool>', "<final>done</final>"],
        "check": lambda result: "timeout must be in [1, 120]" in result,
    },
    {
        "id": "approval_denied",
        "category": "approval",
        "desc": "审批策略为 never 时拒绝高风险工具",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"run_shell","args":{"command":"echo hi","timeout":20}}</tool>', "<final>done</final>"],
        "check": lambda result: "approval denied" in result,
        "approval_policy": "never",
    },
    {
        "id": "read_only_write",
        "category": "approval",
        "desc": "只读模式下拒绝写文件",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"write_file","args":{"path":"x.txt","content":"nope"}}</tool>', "<final>done</final>"],
        "check": lambda result: "approval denied" in result,
        "read_only": True,
    },
    {
        "id": "read_only_patch",
        "category": "approval",
        "desc": "只读模式下拒绝 patch 文件",
        "setup": lambda d: None,
        "outputs": ['<tool>{"name":"patch_file","args":{"path":"README.md","old_text":"demo","new_text":"bye"}}</tool>', "<final>done</final>"],
        "check": lambda result: "approval denied" in result,
        "read_only": True,
    },
    {
        "id": "unknown_tool",
        "category": "param_validation",
        "desc": "调用不存在的工具",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"nonexistent_tool","args":{"x":1}}</tool>', "<final>done</final>"],
        "check": lambda result: "unknown tool" in result,
    },
    {
        "id": "repeated_identical_call",
        "category": "repeated_call",
        "desc": "连续两次相同参数调用同一工具",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": [
            '<tool>{"name":"list_files","args":{}}</tool>',
            '<tool>{"name":"list_files","args":{}}</tool>',
            '<tool>{"name":"list_files","args":{}}</tool>',
            "<final>done</final>",
        ],
        "check": lambda result: "repeated identical tool call" in result,
    },
    {
        "id": "patch_non_unique_match",
        "category": "param_validation",
        "desc": "patch_file 的 old_text 匹配多处",
        "setup": lambda d: (d / "sample.txt").write_text("dup\ndup\n"),
        "outputs": ['<tool>{"name":"patch_file","args":{"path":"sample.txt","old_text":"dup","new_text":"replaced"}}</tool>', "<final>done</final>"],
        "check": lambda result: "occur exactly once" in result,
    },
    {
        "id": "empty_delegate_task",
        "category": "param_validation",
        "desc": "delegate 传空 task",
        "setup": lambda d: (d / "README.md").write_text("ok\n"),
        "outputs": ['<tool>{"name":"delegate","args":{"task":"","max_steps":2}}</tool>', "<final>done</final>"],
        "check": lambda result: "task must not be empty" in result,
    },
]


# ════════════════════════════════════════════════
# AgentTestSuite 类
# ════════════════════════════════════════════════

class AgentTestSuite:
    def __init__(self):
        self.results = {}

    @staticmethod
    def _build_agent(tmp_path, outputs, approval_policy="auto", read_only=False):
        (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
        workspace = WorkspaceContext.build(tmp_path)
        store = SessionStore(tmp_path / ".bongo" / "sessions")
        return MiniAgent(
            model_client=FakeModelClient(outputs),
            workspace=workspace,
            session_store=store,
            approval_policy=approval_policy,
            read_only=read_only,
        )

    @staticmethod
    def _run_tool_direct(name, args, tmp_path):
        agent = AgentTestSuite._build_agent(tmp_path, [])
        return agent.run_tool(name, args)

    # ──────────────────────────────────────
    # 1. 工具安全测试
    # ──────────────────────────────────────
    def run_tool_security(self):
        rows = []
        for scenario in TOOL_SECURITY_SCENARIOS:
            passed = False
            reason = ""
            try:
                with tempfile.TemporaryDirectory(prefix="bongo-sec-") as tmp:
                    tmp_path = Path(tmp)
                    scenario["setup"](tmp_path)

                    approval = scenario.get("approval_policy", "auto")
                    read_only = scenario.get("read_only", False)
                    agent = self._build_agent(
                        tmp_path,
                        scenario["outputs"],
                        approval_policy=approval,
                        read_only=read_only,
                    )
                    agent.ask("test")
                    # 找到 tool 类型的 history 条目
                    tool_events = [i for i in agent.session["history"] if i["role"] == "tool"]
                    if tool_events:
                        # 拼接所有 tool 事件内容，以便检查重复调用等跨事件场景
                        result = "\n".join(e["content"] for e in tool_events)
                    else:
                        # 没有 tool 事件，说明在参数校验阶段就被拦了
                        # 检查 assistant 的 retry notice
                        assistant_msgs = [i["content"] for i in agent.session["history"] if i["role"] == "assistant"]
                        result = assistant_msgs[-1] if assistant_msgs else ""
                    passed = scenario["check"](result)
                    reason = str(result)[:60]
            except Exception as exc:
                reason = str(exc)[:60]

            rows.append({
                "id": scenario["id"],
                "category": scenario["category"],
                "desc": scenario["desc"],
                "passed": passed,
                "reason": reason,
            })

        self.results["tool_security"] = rows
        return rows

    # ──────────────────────────────────────
    # 2. 上下文压缩测试
    # ──────────────────────────────────────
    def run_context(self, repetitions=3):
        from bongo.metrics import measure_feature_ablation_metrics

        rows = []
        history_levels = [("short", 4), ("medium", 12), ("long", 24)]
        note_levels = [("low", 2), ("high", 10)]
        request_levels = [("short", "recall"), ("long", "recall the relevant benchmark fact without dropping details")]

        for h_label, h_count in history_levels:
            for n_label, n_count in note_levels:
                for r_label, r_text in request_levels:
                    full_chars_list = []
                    raw_chars_list = []
                    for _ in range(repetitions):
                        with tempfile.TemporaryDirectory(prefix="bongo-ctx-") as tmp:
                            tmp_path = Path(tmp)
                            (tmp_path / "README.md").write_text("demo\n")
                            agent = self._build_agent(tmp_path, [])
                            for idx in range(n_count):
                                agent.session["relevant_notes"] = append_note(
                                    agent.session.get("relevant_notes", []),
                                    f"note-{idx}-" + ("A" * 180),
                                    tags=("recall",),
                                    created_at=f"2026-04-08T10:{idx:02d}:00+00:00",
                                )
                            for idx in range(h_count):
                                ts = f"2026-04-08T11:{idx:02d}:00+00:00"
                                agent.record({
                                    "role": "user" if idx % 2 == 0 else "assistant",
                                    "content": f"history-{idx}-" + ("B" * 220),
                                    "created_at": ts,
                                })
                            metrics = measure_feature_ablation_metrics(agent, r_text)
                            full_chars_list.append(metrics["full"]["prompt_chars"])
                            raw_chars_list.append(metrics["no_context_reduction"]["prompt_chars"])

                    avg_full = sum(full_chars_list) / len(full_chars_list)
                    avg_raw = sum(raw_chars_list) / len(raw_chars_list)
                    ratio = (avg_raw - avg_full) / avg_raw if avg_raw > 0 else 0
                    rows.append({
                        "id": f"{h_label}-{n_label}-{r_label}",
                        "history": h_label,
                        "notes": n_label,
                        "request": r_label,
                        "avg_full": int(avg_full),
                        "avg_raw": int(avg_raw),
                        "compression": f"{ratio:.1%}",
                    })

        self.results["context"] = rows
        return rows

    # ──────────────────────────────────────
    # 3. 记忆依赖测试
    # ──────────────────────────────────────
    def run_memory(self, repetitions=3):
        tasks = [
            {"id": "fact_color", "category": "fact_lookup", "file": "facts.txt", "fact": "deploy key is red"},
            {"id": "fact_api", "category": "fact_lookup", "file": "settings.txt", "fact": "api base path is /v1/internal"},
            {"id": "fact_budget", "category": "fact_lookup", "file": "limits.txt", "fact": "default step budget is 6"},
            {"id": "fact_timeout", "category": "fact_lookup", "file": "runtime.txt", "fact": "timeout ceiling is 120 seconds"},
            {"id": "edit_intro", "category": "edit_dep", "file": "README.md", "fact": "first bullet is the locked intro line"},
            {"id": "edit_token", "category": "edit_dep", "file": "sample.txt", "fact": "second token is placeholder"},
            {"id": "edit_field", "category": "edit_dep", "file": "config.txt", "fact": "fixed field name is benchmark_schema"},
            {"id": "edit_line", "category": "edit_dep", "file": "notes.txt", "fact": "locked marker is on line three"},
        ]

        rows = []
        for task in tasks:
            for variant in ("memory_on", "memory_off"):
                repeated_reads_sum = 0
                for _ in range(repetitions):
                    with tempfile.TemporaryDirectory(prefix="bongo-mem-") as tmp:
                        tmp_path = Path(tmp)
                        (tmp_path / "README.md").write_text("demo\n")
                        (tmp_path / task["file"]).write_text(task["fact"] + "\n")
                        agent = self._build_agent(tmp_path, [
                            f'<tool>{{"name":"read_file","args":{{"path":"{task["file"]}","start":1,"end":20}}}}</tool>',
                            "<final>Done.</final>",
                            f"<final>{task['fact'].capitalize()}.</final>",
                        ])
                        agent.ask(f"Read {task['file']} and remember the fact.")

                        if variant == "memory_off":
                            agent.feature_flags["memory"] = False
                            agent.feature_flags["relevant_notes"] = False

                        agent.ask(f"What does {task['file']} say?")
                        tool_events = [i for i in agent.session["history"] if i["role"] == "tool"]
                        read_count = sum(1 for e in tool_events if e["name"] == "read_file")
                        repeated_reads_sum += max(0, read_count - 1)

                rows.append({
                    "task_id": task["id"],
                    "category": task["category"],
                    "variant": variant,
                    "repeated_reads": repeated_reads_sum,
                })

        # 汇总
        on_total = sum(r["repeated_reads"] for r in rows if r["variant"] == "memory_on")
        off_total = sum(r["repeated_reads"] for r in rows if r["variant"] == "memory_off")
        summary = {"memory_on_total": on_total, "memory_off_total": off_total}
        self.results["memory"] = {"rows": rows, "summary": summary}
        return rows, summary

    # ──────────────────────────────────────
    # 4. Workspace 漂移检测
    # ──────────────────────────────────────
    def run_drift(self):
        from bongo.workspace import WorkspaceContext

        def _git(cwd, *args):
            subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)

        def _init_git(d):
            _git(d, "init")
            _git(d, "config", "user.email", "t@t")
            _git(d, "config", "user.name", "t")

        scenarios = [
            {
                "id": "readme_changed",
                "desc": "README 内容被修改",
                "setup": lambda d: None,
                "drift": lambda d: (d / "README.md").write_text("changed\n"),
            },
            {
                "id": "new_untracked",
                "desc": "新未跟踪文件出现",
                "setup": lambda d: [_init_git(d), (d / "README.md").write_text("init\n"), _git(d, "add", "."), _git(d, "commit", "-m", "init")],
                "drift": lambda d: (d / "new.py").write_text("new\n"),
            },
            {
                "id": "tracked_modified",
                "desc": "已跟踪文件被修改",
                "setup": lambda d: [_init_git(d), (d / "app.py").write_text("v1\n"), _git(d, "add", "."), _git(d, "commit", "-m", "init")],
                "drift": lambda d: (d / "app.py").write_text("v2\n"),
            },
            {
                "id": "new_commit",
                "desc": "新提交出现",
                "setup": lambda d: [_init_git(d), (d / "README.md").write_text("init\n"), _git(d, "add", "."), _git(d, "commit", "-m", "init")],
                "drift": lambda d: [(d / "README.md").write_text("updated\n"), _git(d, "add", "."), _git(d, "commit", "-m", "update")],
            },
            {
                "id": "file_deleted",
                "desc": "文件被删除",
                "setup": lambda d: [_init_git(d), (d / "temp.txt").write_text("temp\n"), _git(d, "add", "."), _git(d, "commit", "-m", "add")],
                "drift": lambda d: (d / "temp.txt").unlink(),
            },
            {
                "id": "pyproject_changed",
                "desc": "pyproject.toml 被修改",
                "setup": lambda d: (d / "pyproject.toml").write_text('[project]\nname = "v1"\n'),
                "drift": lambda d: (d / "pyproject.toml").write_text('[project]\nname = "v2"\n'),
            },
            {
                "id": "multi_files",
                "desc": "多个文件同时变动",
                "setup": lambda d: [_init_git(d), (d / "a.py").write_text("a1\n"), (d / "b.py").write_text("b1\n"), _git(d, "add", "."), _git(d, "commit", "-m", "init")],
                "drift": lambda d: [(d / "a.py").write_text("a2\n"), (d / "b.py").write_text("b2\n")],
            },
            {
                "id": "agents_md_created",
                "desc": "AGENTS.md 被创建",
                "setup": lambda d: None,
                "drift": lambda d: (d / "AGENTS.md").write_text("rules\n"),
            },
            {
                "id": "package_json_changed",
                "desc": "package.json 被修改",
                "setup": lambda d: (d / "package.json").write_text('{"name":"v1"}\n'),
                "drift": lambda d: (d / "package.json").write_text('{"name":"v2"}\n'),
            },
            {
                "id": "file_renamed",
                "desc": "文件被重命名",
                "setup": lambda d: [_init_git(d), (d / "old.py").write_text("x\n"), _git(d, "add", "."), _git(d, "commit", "-m", "init")],
                "drift": lambda d: (d / "old.py").rename(d / "new.py"),
            },
            {
                "id": "file_appended",
                "desc": "文件被追加内容",
                "setup": lambda d: [_init_git(d), (d / "log.txt").write_text("line1\n"), _git(d, "add", "."), _git(d, "commit", "-m", "init")],
                "drift": lambda d: open(d / "log.txt", "a").write("line2\n"),
            },
            {
                "id": "no_change",
                "desc": "无变化时不重建（反向测试）",
                "setup": lambda d: [_init_git(d), (d / "README.md").write_text("stable\n"), _git(d, "add", "."), _git(d, "commit", "-m", "init")],
                "drift": lambda d: None,
            },
        ]

        rows = []
        for s in scenarios:
            detected = False
            try:
                with tempfile.TemporaryDirectory(prefix="bongo-drift-") as tmp:
                    tmp_path = Path(tmp)
                    (tmp_path / "README.md").write_text("demo\n")
                    s["setup"](tmp_path)
                    agent = self._build_agent(tmp_path, [])
                    agent.refresh_prefix()
                    old_hash = agent.prefix_state.hash

                    s["drift"](tmp_path)
                    result = agent.refresh_prefix()

                    if s["id"] == "no_change":
                        detected = not result["workspace_changed"]
                    else:
                        detected = result["workspace_changed"] and result["prefix_changed"]
            except Exception:
                pass

            rows.append({
                "id": s["id"],
                "desc": s["desc"],
                "detected": detected,
            })

        self.results["drift"] = rows
        return rows

    # ──────────────────────────────────────
    # 全部跑完 + 打印表格
    # ──────────────────────────────────────
    def run_all(self):
        print("Running tool security tests...")
        self.run_tool_security()
        self._print_table("Tool Security", self.results["tool_security"],
                          ["id", "category", "desc", "passed"])

        print("\nRunning context compression tests...")
        self.run_context()
        self._print_table("Context Compression", self.results["context"],
                          ["id", "history", "notes", "request", "avg_raw", "avg_full", "compression"])

        print("\nRunning memory dependency tests...")
        rows, summary = self.run_memory()
        self._print_table("Memory Dependency", rows,
                          ["task_id", "category", "variant", "repeated_reads"])
        print(f"\n  Summary: memory_on={summary['memory_on_total']} repeated reads, "
              f"memory_off={summary['memory_off_total']} repeated reads")

        print("\nRunning workspace drift tests...")
        self.run_drift()
        self._print_table("Workspace Drift", self.results["drift"],
                          ["id", "desc", "detected"])

        # 总览
        print("\n" + "=" * 50)
        sec_total = len(self.results["tool_security"])
        sec_passed = sum(1 for r in self.results["tool_security"] if r["passed"])
        ctx_total = len(self.results["context"])
        drt_total = len(self.results["drift"])
        drt_passed = sum(1 for r in self.results["drift"] if r["detected"])
        mem_on = summary["memory_on_total"]
        mem_off = summary["memory_off_total"]

        print(f"Tool Security:       {sec_passed}/{sec_total} passed")
        print(f"Context Compression: {ctx_total} configs tested")
        print(f"Memory Dependency:   {mem_on} vs {mem_off} repeated reads (on vs off)")
        print(f"Workspace Drift:     {drt_passed}/{drt_total} detected")

        self.save_results()

    @staticmethod
    def _print_table(title, rows, columns):
        if not rows:
            print(f"  (no data for {title})")
            return

        widths = {}
        for col in columns:
            widths[col] = max(len(col), max(len(str(r.get(col, ""))) for r in rows))

        header = " | ".join(col.ljust(widths[col]) for col in columns)
        sep = "-+-".join("-" * widths[col] for col in columns)
        print(f"\n  {title}")
        print(f"  {header}")
        print(f"  {sep}")
        for row in rows:
            line = " | ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns)
            print(f"  {line}")

    @staticmethod
    def _to_markdown(title, rows, columns):
        if not rows:
            return f"## {title}\n\n(no data)\n"
        header = "| " + " | ".join(columns) + " |"
        sep = "| " + " | ".join("---" for _ in columns) + " |"
        lines = [f"## {title}", "", header, sep]
        for row in rows:
            cells = " | ".join(str(row.get(col, "")) for col in columns)
            lines.append(f"| {cells} |")
        return "\n".join(lines) + "\n"

    def save_results(self, output_dir=None):
        if output_dir is None:
            output_dir = Path(__file__).parent / "results"
        else:
            output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        parts = [f"# bongo test results — {ts}\n"]

        if "tool_security" in self.results:
            parts.append(self._to_markdown(
                "Tool Security",
                self.results["tool_security"],
                ["id", "category", "desc", "passed"],
            ))

        if "context" in self.results:
            parts.append(self._to_markdown(
                "Context Compression",
                self.results["context"],
                ["id", "history", "notes", "request", "avg_raw", "avg_full", "compression"],
            ))

        if "memory" in self.results:
            mem = self.results["memory"]
            parts.append(self._to_markdown(
                "Memory Dependency",
                mem["rows"],
                ["task_id", "category", "variant", "repeated_reads"],
            ))
            s = mem["summary"]
            parts.append(f"- memory_on_total: {s['memory_on_total']} repeated reads\n")
            parts.append(f"- memory_off_total: {s['memory_off_total']} repeated reads\n")

        if "drift" in self.results:
            parts.append(self._to_markdown(
                "Workspace Drift",
                self.results["drift"],
                ["id", "desc", "detected"],
            ))

        # 总览
        summary_lines = ["## Summary\n"]
        if "tool_security" in self.results:
            sec = self.results["tool_security"]
            passed = sum(1 for r in sec if r["passed"])
            summary_lines.append(f"- Tool Security: {passed}/{len(sec)} passed")
        if "context" in self.results:
            summary_lines.append(f"- Context Compression: {len(self.results['context'])} configs tested")
        if "memory" in self.results:
            s = self.results["memory"]["summary"]
            summary_lines.append(f"- Memory Dependency: {s['memory_on_total']} vs {s['memory_off_total']} repeated reads (on vs off)")
        if "drift" in self.results:
            drt = self.results["drift"]
            detected = sum(1 for r in drt if r["detected"])
            summary_lines.append(f"- Workspace Drift: {detected}/{len(drt)} detected")
        parts.append("\n".join(summary_lines) + "\n")

        content = "\n".join(parts)
        path = output_dir / f"results-{ts}.md"
        path.write_text(content, encoding="utf-8")
        print(f"\nResults saved to {path}")
        return path


if __name__ == "__main__":
    t = AgentTestSuite()
    t.run_all()


# ════════════════════════════════════════════════
# pytest 入口
# ════════════════════════════════════════════════

def test_tool_security():
    rows = AgentTestSuite().run_tool_security()
    for r in rows:
        assert r["passed"], f"[FAIL] {r['id']}: {r['reason'][:80]}"


def test_context_compression():
    rows = AgentTestSuite().run_context(repetitions=1)
    assert len(rows) == 12
    for r in rows:
        assert r["avg_full"] <= r["avg_raw"]


def test_memory_dependency():
    rows, summary = AgentTestSuite().run_memory(repetitions=1)
    assert len(rows) == 16
    assert summary["memory_on_total"] <= summary["memory_off_total"]


def test_workspace_drift():
    rows = AgentTestSuite().run_drift()
    for r in rows:
        assert r["detected"], f"[FAIL] {r['id']}: {r['desc']}"


def test_save_report():
    t = AgentTestSuite()
    t.run_all()
    path = t.save_results()
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "Tool Security" in content
    assert "Context Compression" in content
    assert "Memory Dependency" in content
    assert "Workspace Drift" in content
    assert "Summary" in content
