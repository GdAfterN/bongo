"""会话恢复基准测试（12 个场景，真实 LLM）。

从 benchmarks/resume/ 加载预生成的 10 轮中断 session + trace，
用真实 LLM 测试恢复机制：恢复成功率、最早记忆轮次、工具链记忆、LLM 继续回答。
"""

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bongo.runtime import bongo, SessionStore
from bongo.run_store import RunStore
from bongo.trace import TraceStore

BENCHMARK_DIR = Path(__file__).parent.parent.parent / "benchmarks" / "resume"


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
    index_path = BENCHMARK_DIR / "index.json"
    if not index_path.exists():
        pytest.skip("基准数据未生成，请先运行 benchmarks/generate_resume_data.py")
    with open(index_path, encoding="utf-8") as f:
        return json.load(f)["scenarios"]


SCENARIOS = _load_scenarios()


@pytest.fixture(autouse=True)
def _rate_limit():
    """避免 429 限流：每个测试之间等待 5 秒。"""
    time.sleep(5)
    yield


@pytest.fixture
def env(tmp_path):
    client = _load_real_model_client()
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    (work_dir / "README.md").write_text("# test\n", encoding="utf-8")
    (work_dir / "src").mkdir()
    (work_dir / "src" / "main.py").write_text("def main(): print('hello')\n", encoding="utf-8")

    session_store = SessionStore(tmp_path / "sessions")
    run_store = RunStore(tmp_path / "reports")
    trace_store = TraceStore(tmp_path / "traces")

    return {
        "work_dir": work_dir,
        "session_store": session_store,
        "run_store": run_store,
        "trace_store": trace_store,
        "client": client,
        "tmp_path": tmp_path,
    }


def _load_scenario(env, scenario_name):
    """加载场景数据到隔离环境，返回 (session_id, session, trace_entries)。"""
    scenario_dir = BENCHMARK_DIR / scenario_name
    if not scenario_dir.exists():
        pytest.skip(f"场景数据不存在: {scenario_name}")

    sess_files = list((scenario_dir / "sessions").glob("*.json"))
    if not sess_files:
        pytest.skip(f"session 文件不存在: {scenario_dir}")
    with open(sess_files[0], encoding="utf-8") as f:
        session = json.load(f)

    session_id = session["id"]
    run_id = session.get("active_run_id", "")
    session["work_dir"] = str(env["work_dir"])

    dest_sess = env["session_store"].root / f"{session_id}.json"
    dest_sess.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_sess, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)

    # 复制 task_status.json 到 run_store
    ts_src = scenario_dir / "reports" / run_id / "task_status.json"
    if ts_src.exists():
        ts_dest = env["run_store"].root / run_id / "task_status.json"
        ts_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ts_src, ts_dest)

    trace_files = list((scenario_dir / "traces").glob("*.jsonl"))
    trace_entries = []
    if trace_files:
        dest_trace = env["trace_store"].root / f"{session_id}.jsonl"
        dest_trace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(trace_files[0], dest_trace)
        with open(trace_files[0], encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    trace_entries.append(json.loads(line))

    return session_id, session, trace_entries


class TestResumeBenchmark:
    """12 个场景的会话恢复测试（真实 LLM）。"""

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["scenario"] for s in SCENARIOS])
    def test_recovery_success(self, env, scenario):
        """from_session 检测到中断并构建 _recovery_context。"""
        session_id, session, traces = _load_scenario(env, scenario["scenario"])

        restored = bongo.from_session(
            model_client=env["client"],
            session_store=env["session_store"],
            session_id=session_id,
            work_dir=str(env["work_dir"]),
            run_store=env["run_store"],
            trace_store=env["trace_store"],
            approval_policy="auto",
        )

        rc = restored._recovery_context
        assert rc is not None, f"场景 {scenario['scenario']}: 未检测到恢复上下文"
        assert rc["user_request"]
        assert rc["tool_steps"] == scenario["tool_steps"]

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["scenario"] for s in SCENARIOS])
    def test_earliest_memory_round(self, env, scenario):
        """trace 中最早记录应从第 1 轮开始。"""
        session_id, session, traces = _load_scenario(env, scenario["scenario"])

        restored = bongo.from_session(
            model_client=env["client"],
            session_store=env["session_store"],
            session_id=session_id,
            work_dir=str(env["work_dir"]),
            run_store=env["run_store"],
            trace_store=env["trace_store"],
            approval_policy="auto",
        )

        rc = restored._recovery_context
        assert rc is not None
        if rc["trace_entries"]:
            earliest = min(t["round"] for t in rc["trace_entries"])
            assert earliest == 1, f"场景 {scenario['scenario']}: 最早轮次为 {earliest}，期望 1"

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["scenario"] for s in SCENARIOS])
    def test_tool_chain_preserved(self, env, scenario):
        """恢复后工具调用链与 trace 一致。"""
        session_id, session, traces = _load_scenario(env, scenario["scenario"])

        restored = bongo.from_session(
            model_client=env["client"],
            session_store=env["session_store"],
            session_id=session_id,
            work_dir=str(env["work_dir"]),
            run_store=env["run_store"],
            trace_store=env["trace_store"],
            approval_policy="auto",
        )

        rc = restored._recovery_context
        assert rc is not None
        assert len(rc["tools_called"]) > 0
        assert rc["tools_called"] == scenario["tools_called"]

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["scenario"] for s in SCENARIOS])
    def test_llm_resume(self, env, scenario):
        """真实 LLM 恢复测试：从中断处继续对话。"""
        session_id, session, traces = _load_scenario(env, scenario["scenario"])

        restored = bongo.from_session(
            model_client=env["client"],
            session_store=env["session_store"],
            session_id=session_id,
            work_dir=str(env["work_dir"]),
            run_store=env["run_store"],
            trace_store=env["trace_store"],
            approval_policy="auto",
        )

        rc = restored._recovery_context
        assert rc is not None, f"场景 {scenario['scenario']}: 未检测到恢复上下文"

        # 用真实 LLM 继续对话
        t0 = time.time()
        try:
            answer = restored.ask("继续")
            elapsed = round(time.time() - t0, 1)
            assert answer, f"场景 {scenario['scenario']}: LLM 返回空答案"
            print(f"  {scenario['scenario']}: 恢复成功, {elapsed}s, answer={answer[:80]}...")
        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            pytest.fail(f"场景 {scenario['scenario']}: LLM 恢复调用失败 ({elapsed}s): {e}")

    def test_all_scenarios_summary(self, env):
        """汇总：12 个场景全部恢复结果（真实 LLM）。"""
        results = []
        for sc in SCENARIOS:
            env2 = {
                "work_dir": env["work_dir"],
                "session_store": SessionStore(env["tmp_path"] / f"s_{sc['scenario']}" / "sessions"),
                "run_store": RunStore(env["tmp_path"] / f"s_{sc['scenario']}" / "reports"),
                "trace_store": TraceStore(env["tmp_path"] / f"s_{sc['scenario']}" / "traces"),
                "client": env["client"],
                "tmp_path": env["tmp_path"],
            }
            try:
                session_id, session, traces = _load_scenario(env2, sc["scenario"])
                restored = bongo.from_session(
                    model_client=env2["client"],
                    session_store=env2["session_store"],
                    session_id=session_id,
                    work_dir=str(env2["work_dir"]),
                    run_store=env2["run_store"],
                    trace_store=env2["trace_store"],
                    approval_policy="auto",
                )
                rc = restored._recovery_context
                ok = rc is not None
                earliest = min((t["round"] for t in rc["trace_entries"]), default=0) if rc and rc["trace_entries"] else 0
                chain = bool(rc and rc["tools_called"])

                # 真实 LLM 继续对话
                if ok:
                    t0 = time.time()
                    answer = restored.ask("继续")
                    llm_ok = bool(answer)
                    llm_time = round(time.time() - t0, 1)
                else:
                    llm_ok = False
                    llm_time = 0
            except Exception as e:
                err_msg = repr(e).encode("ascii", errors="replace").decode("ascii")
                print(f"  {sc['scenario']}: EXCEPTION: {err_msg}")
                ok, earliest, chain = False, 0, False
                llm_ok = False
                llm_time = 0
            results.append({
                "scenario": sc["scenario"], "recovery": ok,
                "earliest": earliest, "chain": chain,
                "llm_ok": llm_ok, "llm_time": llm_time,
            })

        print("\n" + "=" * 75)
        print(f"{'场景':<22} {'恢复':>5} {'最早轮次':>8} {'工具链':>5} {'LLM':>5} {'耗时':>7}")
        print("-" * 75)
        for r in results:
            print(f"{r['scenario']:<22} {'PASS' if r['recovery'] else 'FAIL':>5} "
                  f"{r['earliest']:>8} {'YES' if r['chain'] else 'NO':>5} "
                  f"{'PASS' if r['llm_ok'] else 'FAIL':>5} {r['llm_time']:>6.1f}s")
        print("=" * 75)

        failed = [r for r in results if not r["recovery"] or not r["llm_ok"]]
        assert len(failed) == 0, f"以下场景恢复失败: {[r['scenario'] for r in failed]}"
