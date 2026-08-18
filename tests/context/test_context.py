"""上下文管理基准测试（12×50轮 真实 LLM）。

指标：avg_prompt_chars、avg_react_rounds、avg_tokens_per_round
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

BENCHMARK_DIR = Path(__file__).parent.parent.parent / "benchmarks" / "context"


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
    (work_dir / "src" / "main.py").write_text("def main(): print('hello')\n", encoding="utf-8")
    (work_dir / "src" / "utils.py").write_text("def add(a, b): return a + b\ndef mul(a, b): return a * b\n", encoding="utf-8")
    (work_dir / "docs").mkdir()
    (work_dir / "docs" / "guide.md").write_text("# Guide\n\n1. Install\n2. Run\n", encoding="utf-8")

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


class TestContextBenchmark:
    """上下文管理基准测试。"""

    def test_run_all_scenarios(self, real_env):
        """运行 12 个场景 × 50 轮，收集上下文指标。"""
        agent = real_env["agent"]
        scenarios = _load_scenarios()
        results = []

        for sc in scenarios:
            name = sc["name"]
            rounds = sc["rounds"]
            prompt_chars_list = []
            react_rounds_list = []
            tokens_list = []
            t0 = time.time()

            for rd in rounds:
                user_msg = rd["user"]
                try:
                    answer = agent.ask(user_msg)
                except Exception as e:
                    answer = f"ERROR: {e}"

                pm = agent.last_prompt_metadata or {}
                cm = agent.last_completion_metadata or {}
                ts = getattr(agent, "current_task_status", None)

                prompt_chars_list.append(pm.get("prompt_chars", 0))
                react_rounds_list.append(ts.attempts if ts else 0)
                tokens_list.append(cm.get("total_tokens", 0) or 0)

            elapsed = time.time() - t0
            avg_prompt = sum(prompt_chars_list) / len(prompt_chars_list) if prompt_chars_list else 0
            avg_react = sum(react_rounds_list) / len(react_rounds_list) if react_rounds_list else 0
            avg_tokens = sum(tokens_list) / len(tokens_list) if tokens_list else 0

            results.append({
                "scenario": name,
                "rounds": len(rounds),
                "avg_prompt_chars": round(avg_prompt),
                "avg_react_rounds": round(avg_react, 1),
                "avg_tokens": round(avg_tokens),
                "elapsed": round(elapsed, 1),
            })
            print(f"  {name}: avg_prompt={avg_prompt:.0f}, avg_react={avg_react:.1f}, avg_tokens={avg_tokens:.0f}, {elapsed:.0f}s")

        # 输出汇总
        print("\n" + "=" * 70)
        print(f"{'场景':<25} {'轮次':>5} {'avg_prompt':>10} {'avg_react':>10} {'avg_tokens':>10} {'耗时':>8}")
        print("-" * 70)
        for r in results:
            print(f"{r['scenario']:<25} {r['rounds']:>5} {r['avg_prompt_chars']:>10} {r['avg_react_rounds']:>10} {r['avg_tokens']:>10} {r['elapsed']:>7.0f}s")
        print("=" * 70)

        assert len(results) == 12
