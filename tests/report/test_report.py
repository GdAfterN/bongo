"""统一测试报告。

串行运行 4 个模块，生成 tests/test_report.md。
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bongo.runtime import bongo, SessionStore, REDACTED_VALUE
from bongo.run_store import RunStore
from bongo.trace import TraceStore

PROJECT_ROOT = Path(__file__).parent.parent.parent
BENCH_CONTEXT = PROJECT_ROOT / "benchmarks" / "context"
BENCH_MEMORY = PROJECT_ROOT / "benchmarks" / "memory"
BENCH_RESUME = PROJECT_ROOT / "benchmarks" / "resume"
BENCH_TOOLS = PROJECT_ROOT / "benchmarks" / "tools"
REPORT_PATH = PROJECT_ROOT / "tests" / "test_report.md"


def _load_jsonl(path):
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _load_real_model_client():
    config_path = Path.home() / ".bongo" / "config.json"
    if not config_path.exists():
        return None
    config = json.loads(config_path.read_text(encoding="utf-8"))
    provider = config.get("provider", "openai")
    if provider == "anthropic":
        from bongo.models import AnthropicCompatibleModelClient
        return AnthropicCompatibleModelClient(
            model=config.get("model", "mimo-v2.5-pro"),
            base_url=config["base_url"], api_key=config["api_key"],
            temperature=0.7, timeout=120,
        )
    else:
        from bongo.models import OpenAICompatibleModelClient
        return OpenAICompatibleModelClient(
            model=config.get("model", "mimo-v2.5-pro"),
            base_url=config["base_url"], api_key=config["api_key"],
            temperature=0.7, timeout=120,
        )


class TestUnifiedReport:
    """生成统一测试报告。"""

    def test_generate_report(self, tmp_path):
        """运行 4 个模块，输出 tests/test_report.md。"""
        start_time = time.time()
        sections = []

        # ── 一、上下文管理 ─────────────────────────────────────
        client = _load_real_model_client()
        context_results = []
        if client and BENCH_CONTEXT.exists():
            work_dir = tmp_path / "ctx_ws"
            work_dir.mkdir()
            (work_dir / "README.md").write_text("# test\n")
            (work_dir / "src").mkdir()
            (work_dir / "src" / "main.py").write_text("def main(): print('hello')\n")

            for jf in sorted(BENCH_CONTEXT.glob("*.jsonl")):
                rounds = _load_jsonl(jf)
                ss = SessionStore(tmp_path / f"ctx_{jf.stem}" / "sessions")
                rs = RunStore(tmp_path / f"ctx_{jf.stem}" / "reports")
                ts = TraceStore(tmp_path / f"ctx_{jf.stem}" / "traces")
                agent = bongo(model_client=client, work_dir=str(work_dir),
                              session_store=ss, run_store=rs, trace_store=ts,
                              max_steps=10, approval_policy="auto")

                prompts, reacts, tokens = [], [], []
                t0 = time.time()
                for rd in rounds:
                    try:
                        agent.ask(rd["user"])
                    except Exception:
                        pass
                    pm = agent.last_prompt_metadata or {}
                    cm = agent.last_completion_metadata or {}
                    task = getattr(agent, "current_task_status", None)
                    prompts.append(pm.get("prompt_chars", 0))
                    reacts.append(task.attempts if task else 0)
                    tokens.append(cm.get("total_tokens", 0) or 0)

                elapsed = time.time() - t0
                context_results.append({
                    "name": jf.stem, "rounds": len(rounds),
                    "avg_prompt": round(sum(prompts) / len(prompts)) if prompts else 0,
                    "avg_react": round(sum(reacts) / len(reacts), 1) if reacts else 0,
                    "avg_tokens": round(sum(tokens) / len(tokens)) if tokens else 0,
                    "elapsed": round(elapsed, 1),
                })

        if context_results:
            lines = ["## 一、上下文管理（真实 LLM）", "",
                     "| 场景 | 轮次 | avg_prompt | avg_react | avg_tokens | 耗时 |",
                     "|------|------|-----------|-----------|-----------|------|"]
            for r in context_results:
                lines.append(f"| {r['name']} | {r['rounds']} | {r['avg_prompt']} | {r['avg_react']} | {r['avg_tokens']} | {r['elapsed']}s |")
            sections.append("\n".join(lines))
        else:
            sections.append("## 一、上下文管理\n\n*跳过：无基准数据或无模型配置*")

        # ── 二、记忆管理 ─────────────────────────────────────
        memory_results = []
        if client and BENCH_MEMORY.exists():
            work_dir = tmp_path / "mem_ws"
            work_dir.mkdir()
            (work_dir / "README.md").write_text("# test\n")
            (work_dir / "src").mkdir()
            (work_dir / "src" / "main.py").write_text("def main(): print('hello')\nif __name__ == '__main__':\n    main()\n")
            (work_dir / "src" / "utils.py").write_text("def add(a, b): return a + b\ndef multiply(a, b): return a * b\n")
            (work_dir / "docs").mkdir()
            (work_dir / "docs" / "guide.md").write_text("# Guide\n\n1. Install\n2. Run\n")

            for jf in sorted(BENCH_MEMORY.glob("*.jsonl")):
                rounds = _load_jsonl(jf)
                ss = SessionStore(tmp_path / f"mem_{jf.stem}" / "sessions")
                rs = RunStore(tmp_path / f"mem_{jf.stem}" / "reports")
                ts = TraceStore(tmp_path / f"mem_{jf.stem}" / "traces")
                agent = bongo(model_client=client, work_dir=str(work_dir),
                              session_store=ss, run_store=rs, trace_store=ts,
                              max_steps=10, approval_policy="auto")

                mem_sizes, seen_files, rereads = [], set(), 0
                max_round = 0
                t0 = time.time()
                for rd in rounds:
                    tool_args = rd.get("tool_args") or {}
                    if rd.get("tool_name") == "read_file":
                        fp = tool_args.get("path", "")
                        if fp:
                            if fp in seen_files:
                                rereads += 1
                            seen_files.add(fp)
                    try:
                        agent.ask(rd["user"])
                    except Exception:
                        pass
                    mem_sizes.append(len(agent.memory.render_memory_text()))
                    if agent.memory.state["working"].get("task_summary"):
                        max_round = rd["round"]

                elapsed = time.time() - t0
                memory_results.append({
                    "name": jf.stem, "rounds": len(rounds),
                    "avg_memory": round(sum(mem_sizes) / len(mem_sizes)) if mem_sizes else 0,
                    "max_round": max_round, "rereads": rereads,
                    "elapsed": round(elapsed, 1),
                })

        if memory_results:
            lines = ["## 二、记忆管理（真实 LLM）", "",
                     "| 场景 | 轮次 | avg_memory | max_round | 重读 | 耗时 |",
                     "|------|------|-----------|-----------|------|------|"]
            for r in memory_results:
                lines.append(f"| {r['name']} | {r['rounds']} | {r['avg_memory']} | {r['max_round']} | {r['rereads']} | {r['elapsed']}s |")
            sections.append("\n".join(lines))
        else:
            sections.append("## 二、记忆管理\n\n*跳过：无基准数据或无模型配置*")

        # ── 三、会话恢复 ─────────────────────────────────────
        resume_results = []
        if BENCH_RESUME.exists():
            index_path = BENCH_RESUME / "index.json"
            if index_path.exists():
                with open(index_path, encoding="utf-8") as f:
                    scenarios = json.load(f)["scenarios"]

                for sc in scenarios:
                    sc_dir = BENCH_RESUME / sc["scenario"]
                    sess_files = list((sc_dir / "sessions").glob("*.json"))
                    trace_files = list((sc_dir / "traces").glob("*.jsonl"))

                    if not sess_files:
                        continue

                    with open(sess_files[0], encoding="utf-8") as f:
                        session = json.load(f)
                    session["work_dir"] = str(tmp_path / "resume_ws")

                    ss = SessionStore(tmp_path / f"res_{sc['scenario']}" / "sessions")
                    rs = RunStore(tmp_path / f"res_{sc['scenario']}" / "reports")
                    tstore = TraceStore(tmp_path / f"res_{sc['scenario']}" / "traces")
                    (tmp_path / "resume_ws").mkdir(exist_ok=True)

                    dest = ss.root / f"{session['id']}.json"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, "w", encoding="utf-8") as f:
                        json.dump(session, f, ensure_ascii=False, indent=2)

                    # 复制 task_status.json 到 run_store
                    run_id = session.get("active_run_id", "")
                    ts_src = sc_dir / "reports" / run_id / "task_status.json"
                    if ts_src.exists():
                        import shutil
                        ts_dest = rs.root / run_id / "task_status.json"
                        ts_dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(ts_src, ts_dest)

                    traces = []
                    if trace_files:
                        import shutil
                        dt = tstore.root / f"{session['id']}.jsonl"
                        dt.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(trace_files[0], dt)
                        with open(trace_files[0], encoding="utf-8") as f:
                            for line in f:
                                if line.strip():
                                    traces.append(json.loads(line))

                    ws = tmp_path / "resume_ws"
                    ws.mkdir(exist_ok=True)

                    restored = bongo.from_session(
                        model_client=client, session_store=ss, session_id=session["id"],
                        work_dir=str(ws), run_store=rs, trace_store=tstore, approval_policy="auto",
                    )
                    rc = restored._recovery_context
                    ok = rc is not None
                    earliest = min((t["round"] for t in rc["trace_entries"]), default=0) if rc and rc["trace_entries"] else 0
                    chain = bool(rc and rc["tools_called"])

                    # 真实 LLM 继续对话
                    llm_ok = False
                    llm_time = 0
                    if ok:
                        try:
                            t0 = time.time()
                            answer = restored.ask("继续")
                            llm_time = round(time.time() - t0, 1)
                            llm_ok = bool(answer)
                        except Exception:
                            pass

                    resume_results.append({
                        "name": sc["scenario"], "recovery": ok,
                        "earliest": earliest, "chain": chain,
                        "tools": sc["tool_steps"], "llm_ok": llm_ok, "llm_time": llm_time,
                    })

        if resume_results:
            lines = ["## 三、会话恢复（10轮中断，真实 LLM）", "",
                     "| 场景 | 工具步数 | 恢复 | 最早轮次 | 工具链 | LLM | 耗时 |",
                     "|------|---------|------|---------|--------|-----|------|"]
            for r in resume_results:
                lines.append(f"| {r['name']} | {r['tools']} | {'PASS' if r['recovery'] else 'FAIL'} | {r['earliest']} | {'YES' if r['chain'] else 'NO'} | {'PASS' if r.get('llm_ok') else 'FAIL'} | {r.get('llm_time', 0)}s |")
            sections.append("\n".join(lines))
        else:
            sections.append("## 三、会话恢复\n\n*跳过：无基准数据*")

        # ── 四、工具安全 ─────────────────────────────────────
        tools_results = []
        scenarios_path = BENCH_TOOLS / "scenarios.jsonl"
        if scenarios_path.exists():
            work_dir = tmp_path / "tool_ws"
            work_dir.mkdir()
            (work_dir / "README.md").write_text("# test\n")
            (work_dir / "src").mkdir()
            (work_dir / "src" / "main.py").write_text("def main(): pass\n")
            (work_dir / "sample.txt").write_text("alpha\nbeta\ngamma\n")

            ss = SessionStore(tmp_path / "tool_sessions")
            mc = MagicMock()
            mc.get_provider_name.return_value = "test"
            agent = bongo(model_client=mc, work_dir=str(work_dir),
                          session_store=ss, max_steps=5, approval_policy="auto")

            for sc in _load_jsonl(scenarios_path):
                name = sc["name"]
                cat = sc["category"]
                setup = sc.get("setup")

                agent.approval_policy = sc.get("approval_policy", "auto")
                agent.read_only = sc.get("read_only", False)

                try:
                    if setup == "call_twice_same_args":
                        agent.run_tool(sc["tool"], sc["args"])
                        agent.session["history"].append({
                            "role": "assistant",
                            "content": [{"type": "tool_use", "id": "tu_t1", "name": sc["tool"], "input": sc["args"]}],
                        })
                        result = agent.run_tool(sc["tool"], sc["args"])
                    elif setup == "call_twice_different_args":
                        agent.session["history"] = []
                        agent.run_tool(sc["tool"], sc["args_first"])
                        agent.session["history"].append({
                            "role": "assistant",
                            "content": [{"type": "tool_use", "id": "tu_t2", "name": sc["tool"], "input": sc["args_first"]}],
                        })
                        result = agent.run_tool(sc["tool"], sc["args_second"])
                    elif setup == "set_env_and_redact":
                        os.environ[sc["env_key"]] = sc["env_value"]
                        try:
                            redacted = agent.redact_text(sc["test_text"])
                            result = "ok" if sc["env_value"] not in redacted and REDACTED_VALUE in redacted else "fail"
                        finally:
                            del os.environ[sc["env_key"]]
                    else:
                        result = agent.run_tool(sc["tool"], sc["args"])

                    if sc.get("expect_error"):
                        passed = "error" in str(result).lower() or "denied" in str(result).lower()
                    elif setup == "set_env_and_redact":
                        passed = result == "ok"
                    elif setup == "call_twice_different_args":
                        passed = "error" not in str(result).lower()
                    else:
                        passed = "error" not in str(result).lower()
                except Exception:
                    passed = sc.get("expect_error", False)

                tools_results.append({"name": name, "category": cat, "passed": passed})

        if tools_results:
            lines = ["## 四、工具安全（本地测试）", "",
                     "| 场景 | 类别 | 结果 |",
                     "|------|------|------|"]
            for r in tools_results:
                lines.append(f"| {r['name']} | {r['category']} | {'PASS' if r['passed'] else 'FAIL'} |")
            sections.append("\n".join(lines))
        else:
            sections.append("## 四、工具安全\n\n*跳过：无基准数据*")

        # ── 生成报告 ─────────────────────────────────────────
        total_elapsed = time.time() - start_time
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        report_lines = [
            "# Bongo 测试报告",
            "",
            f"> 日期: {now}",
            f"> 总耗时: {total_elapsed:.0f}s",
            "",
        ]
        report_lines.extend(sections)
        report_lines.append("")

        report = "\n".join(report_lines)
        REPORT_PATH.write_text(report, encoding="utf-8")

        print("\n" + report)
        print(f"\n报告已写入: {REPORT_PATH}")
