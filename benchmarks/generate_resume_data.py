"""为 12 组上下文场景生成 10 轮中断的会话恢复测试数据。

从 benchmarks/context/*.jsonl 读取前 10 轮对话，
生成 session JSON 和 trace JSONL，用于测试会话恢复机制。

输出: benchmarks/resume/{scenario_name}/
  - sessions/{session_id}.json   — 完整会话快照（active_run_id 非空）
  - traces/{session_id}.jsonl    — 轻量追踪（10 条）
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bongo.memory import default_memory_state

CONTEXT_DIR = Path(__file__).parent / "context"
OUT_DIR = Path(__file__).parent / "resume"
INTERRUPT_AT = 10


def _ts(round_num):
    base = datetime(2026, 6, 3, 10, 0, 0)
    t = base + timedelta(seconds=round_num * 30)
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def _extract_target(tool_name, tool_args):
    if not tool_args:
        return ""
    for key in ("path", "file_path", "filename"):
        if key in tool_args:
            return tool_args[key]
    if tool_name == "search":
        return tool_args.get("pattern", "")[:80]
    return tool_args.get("command", "")[:80]


def generate_resume_data(scenario_name, rounds_data):
    session_id = f"20260603-100000-{scenario_name[:6]:0>6}"
    run_id = f"run_{session_id}"

    scenario_dir = OUT_DIR / scenario_name
    traces_dir = scenario_dir / "traces"
    sessions_dir = scenario_dir / "sessions"
    reports_dir = scenario_dir / "reports" / run_id
    for d in [traces_dir, sessions_dir, reports_dir]:
        d.mkdir(parents=True, exist_ok=True)

    history = []
    trace_entries = []
    tools_called = []

    first_user = rounds_data[0]["user"]
    history.append({"role": "user", "content": first_user, "created_at": _ts(0)})

    interrupted_rounds = rounds_data[:INTERRUPT_AT]
    for i, rd in enumerate(interrupted_rounds):
        tool_name = rd.get("tool_name")
        tool_args = rd.get("tool_args") or {}
        tool_result = rd.get("tool_result", "")

        if tool_name:
            tu_id = f"tu_{i}"
            history.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": tu_id, "name": tool_name, "input": tool_args}],
                "created_at": _ts(i * 2 + 1),
            })
            history.append({
                "role": "tool", "name": tool_name, "tool_use_id": tu_id,
                "content": tool_result, "created_at": _ts(i * 2 + 2),
            })
            target = _extract_target(tool_name, tool_args)
            trace_entries.append({
                "round": i + 1, "tool": tool_name, "target": target, "timestamp": _ts(i * 2 + 1),
            })
            tools_called.append(tool_name)

        assistant_text = rd.get("assistant", "")
        if assistant_text:
            history.append({"role": "assistant", "content": assistant_text, "created_at": _ts(i * 2 + 2)})

        if (i + 1) % 5 == 0 and i < INTERRUPT_AT - 1:
            next_user = rounds_data[i + 1]["user"] if i + 1 < len(rounds_data) else "继续"
            history.append({"role": "user", "content": next_user, "created_at": _ts(i * 2 + 3)})

    # trace JSONL
    trace_path = traces_dir / f"{session_id}.jsonl"
    with open(trace_path, "w", encoding="utf-8") as f:
        for entry in trace_entries:
            f.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")

    # session JSON
    memory = default_memory_state()
    memory["working"]["task_summary"] = first_user
    seen_files = []
    for rd in interrupted_rounds:
        if rd.get("tool_name") == "read_file":
            path = (rd.get("tool_args") or {}).get("path", "")
            if path and path not in seen_files:
                seen_files.append(path)
    memory["working"]["recent_files"] = seen_files[-8:]

    session = {
        "id": session_id,
        "created_at": _ts(0),
        "work_dir": "__PLACEHOLDER__",  # 测试时动态修正
        "history": history,
        "memory": memory,
        "active_run_id": run_id,
        "checkpoints": [],
    }
    sess_path = sessions_dir / f"{session_id}.json"
    with open(sess_path, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)

    # task_status.json (status=running, 模拟中断)
    task_status = {
        "run_id": run_id,
        "task_id": session_id,
        "user_request": first_user,
        "status": "running",
        "current_action": "executing",
        "tool_steps": len(tools_called),
        "attempts": len(tools_called),
        "last_tool": tools_called[-1] if tools_called else "",
        "tools_called": tools_called,
        "stop_reason": "",
        "final_answer": "",
        "checkpoint_snapshot": {},
        "interrupted_at_step": len(tools_called),
        "last_tool_result": "",
        "checkpoint_created_at": "",
    }
    ts_path = reports_dir / "task_status.json"
    with open(ts_path, "w", encoding="utf-8") as f:
        json.dump(task_status, f, ensure_ascii=False, indent=2)

    return {
        "scenario": scenario_name,
        "session_id": session_id,
        "run_id": run_id,
        "interrupt_at": INTERRUPT_AT,
        "tool_steps": len(tools_called),
        "history_entries": len(history),
        "trace_entries": len(trace_entries),
        "tools_called": tools_called,
    }


def main():
    print("=" * 60)
    print("生成 12 组 10 轮中断的会话恢复数据")
    print("=" * 60)

    jsonl_files = sorted(CONTEXT_DIR.glob("*.jsonl"))
    if not jsonl_files:
        print("错误: benchmarks/context/ 下没有 JSONL 文件")
        return

    results = []
    for jf in jsonl_files:
        rounds = []
        with open(jf, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rounds.append(json.loads(line))
        name = jf.stem
        info = generate_resume_data(name, rounds)
        results.append(info)
        print(f"  {name}: {info['tool_steps']} tools, {info['history_entries']} history, {info['trace_entries']} trace")

    index_path = OUT_DIR / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({"description": "会话恢复基准数据", "interrupt_at": INTERRUPT_AT, "scenarios": results}, f, ensure_ascii=False, indent=2)

    print(f"\n生成完成！共 {len(results)} 个场景 → {OUT_DIR}")


if __name__ == "__main__":
    main()
