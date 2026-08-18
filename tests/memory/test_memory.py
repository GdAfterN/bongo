"""记忆管理基准测试（12×30轮 真实 LLM）。

指标：avg_memory_chars、max_memory_round、reread_count
"""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bongo.runtime import bongo, SessionStore
from bongo.run_store import RunStore
from bongo.trace import TraceStore

BENCHMARK_DIR = Path(__file__).parent.parent.parent / "benchmarks" / "memory"


def _load_real_model_client():
    config_path = Path.home() / ".bongo" / "config.json"
    if not config_path.exists():
        pytest.skip("~/.bongo/config.json 不存在")
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


def _load_scenarios():
    files = sorted(BENCHMARK_DIR.glob("*.jsonl"))
    if not files:
        pytest.skip("基准数据未生成")
    scenarios = []
    for f in files:
        rounds = []
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rounds.append(json.loads(line))
        scenarios.append({"name": f.stem, "rounds": rounds})
    return scenarios


@pytest.fixture
def real_env(tmp_path):
    client = _load_real_model_client()
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    (work_dir / "README.md").write_text("# test\n", encoding="utf-8")
    (work_dir / "src").mkdir()
    (work_dir / "src" / "main.py").write_text("def main(): print('hello')\nif __name__ == '__main__':\n    main()\n", encoding="utf-8")
    (work_dir / "src" / "utils.py").write_text("def add(a, b): return a + b\ndef multiply(a, b): return a * b\n", encoding="utf-8")
    (work_dir / "docs").mkdir()
    (work_dir / "docs" / "guide.md").write_text("# Guide\n\n1. Install deps\n2. Run server\n", encoding="utf-8")

    session_store = SessionStore(tmp_path / "sessions")
    run_store = RunStore(tmp_path / "reports")
    trace_store = TraceStore(tmp_path / "traces")

    agent = bongo(
        model_client=client,
        work_dir=str(work_dir),
        session_store=session_store,
        run_store=run_store,
        trace_store=trace_store,
        max_steps=10,
        approval_policy="auto",
    )
    return {"agent": agent}


class TestMemoryBenchmark:
    """记忆管理基准测试。"""

    def test_run_all_scenarios(self, real_env):
        """运行 12 个场景 × 30 轮，收集记忆指标。"""
        agent = real_env["agent"]
        scenarios = _load_scenarios()
        results = []

        for sc in scenarios:
            name = sc["name"]
            rounds = sc["rounds"]
            memory_chars_list = []
            reread_count = 0
            seen_files = set()
            max_memory_round = 0
            t0 = time.time()

            for rd in rounds:
                user_msg = rd["user"]
                tool_name = rd.get("tool_name")
                tool_args = rd.get("tool_args") or {}

                # 统计重读：同一文件被读取超过一次
                if tool_name == "read_file":
                    fpath = tool_args.get("path", "")
                    if fpath:
                        if fpath in seen_files:
                            reread_count += 1
                        seen_files.add(fpath)

                try:
                    answer = agent.ask(user_msg)
                except Exception:
                    pass

                mem_text = agent.memory.render_memory_text()
                mem_len = len(mem_text)
                memory_chars_list.append(mem_len)

                # 检测 memory 中是否保留了首轮信息
                task_summary = agent.memory.state["working"].get("task_summary", "")
                if task_summary and rd["round"] > max_memory_round:
                    max_memory_round = rd["round"]

            elapsed = time.time() - t0
            avg_mem = sum(memory_chars_list) / len(memory_chars_list) if memory_chars_list else 0

            results.append({
                "scenario": name,
                "rounds": len(rounds),
                "avg_memory_chars": round(avg_mem),
                "max_memory_round": max_memory_round,
                "reread_count": reread_count,
                "elapsed": round(elapsed, 1),
            })
            print(f"  {name}: avg_mem={avg_mem:.0f}, max_round={max_memory_round}, rereads={reread_count}, {elapsed:.0f}s")

        print("\n" + "=" * 70)
        print(f"{'场景':<25} {'轮次':>5} {'avg_memory':>10} {'max_round':>10} {'重读':>5} {'耗时':>8}")
        print("-" * 70)
        for r in results:
            print(f"{r['scenario']:<25} {r['rounds']:>5} {r['avg_memory_chars']:>10} {r['max_memory_round']:>10} {r['reread_count']:>5} {r['elapsed']:>7.0f}s")
        print("=" * 70)

        assert len(results) == 12
