"""工具安全测试（12 个场景，不调 LLM）。

从 benchmarks/tools/scenarios.jsonl 加载测试定义，
验证参数校验、工作区隔离、高风险审批、重复调用拦截、敏感信息脱敏。
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bongo.runtime import bongo, SessionStore, REDACTED_VALUE

SCENARIOS_PATH = Path(__file__).parent.parent.parent / "benchmarks" / "tools" / "scenarios.jsonl"


def _load_scenarios():
    if not SCENARIOS_PATH.exists():
        pytest.skip("基准数据未生成，请先运行 benchmarks/generate_tools_scenarios.py")
    scenarios = []
    with open(SCENARIOS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                scenarios.append(json.loads(line))
    return scenarios


SCENARIOS = _load_scenarios()


@pytest.fixture
def env(tmp_path):
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    (work_dir / "README.md").write_text("# test project\n", encoding="utf-8")
    (work_dir / "src").mkdir()
    (work_dir / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (work_dir / "sample.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    session_store = SessionStore(tmp_path / "sessions")
    mock_client = MagicMock()
    mock_client.get_provider_name.return_value = "test"

    agent = bongo(
        model_client=mock_client,
        work_dir=str(work_dir),
        session_store=session_store,
        max_steps=5,
        approval_policy="auto",
    )
    return {"agent": agent, "work_dir": work_dir}


class TestToolSafety:
    """12 个工具安全测试场景。"""

    def test_01_param_missing_path(self, env):
        """read_file 缺少 path → rejected。"""
        agent = env["agent"]
        result = agent.run_tool("read_file", {})
        assert "error" in result.lower()
        assert agent._last_tool_result_metadata["tool_status"] == "rejected"

    def test_02_param_invalid_range(self, env):
        """read_file start > end → rejected。"""
        agent = env["agent"]
        result = agent.run_tool("read_file", {"path": "README.md", "start": 100, "end": 50})
        assert "error" in result.lower()
        assert "invalid line range" in result

    def test_03_param_timeout_oob(self, env):
        """run_shell timeout=999 → rejected。"""
        agent = env["agent"]
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 999})
        assert "error" in result.lower()
        assert "timeout" in result

    def test_04_path_escape_read(self, env):
        """read_file ../ 逃逸 → rejected + security_event。"""
        agent = env["agent"]
        result = agent.run_tool("read_file", {"path": "../outside.txt"})
        assert "error" in result.lower()
        assert "escapes workspace" in result
        assert agent._last_tool_result_metadata.get("security_event_type") == "path_escape"

    def test_05_path_escape_write(self, env):
        """write_file 写入工作区外 → rejected。"""
        agent = env["agent"]
        result = agent.run_tool("write_file", {"path": "../../etc/malicious", "content": "hack"})
        assert "error" in result.lower()
        assert "escapes workspace" in result

    def test_06_path_escape_search(self, env):
        """search path 逃逸 → rejected。"""
        agent = env["agent"]
        result = agent.run_tool("search", {"pattern": "secret", "path": "../outside"})
        assert "error" in result.lower()
        assert "escapes workspace" in result

    def test_07_approval_never_blocks_shell(self, env):
        """approval_policy=never 时 shell 被拒绝。"""
        agent = env["agent"]
        agent.approval_policy = "never"
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})
        assert "error" in result.lower() or "denied" in result.lower()

    def test_08_read_only_blocks_write(self, env):
        """read_only=True 时 write_file 被拒绝。"""
        agent = env["agent"]
        agent.read_only = True
        result = agent.run_tool("write_file", {"path": "new.txt", "content": "data"})
        assert "error" in result.lower()

    def test_09_read_only_blocks_patch(self, env):
        """read_only=True 时 patch_file 被拒绝。"""
        agent = env["agent"]
        agent.read_only = True
        result = agent.run_tool("patch_file", {"path": "sample.txt", "old_text": "beta", "new_text": "locked"})
        assert "error" in result.lower()

    def test_10_repeated_call_blocked(self, env):
        """连续两次相同调用，第二次被拦截（读和写都拦截连续重复）。"""
        agent = env["agent"]
        # 连续读重复拦截
        result1 = agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 10})
        assert "error" not in result1.lower()
        agent.session["history"].append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_rep", "name": "read_file",
                         "input": {"path": "README.md", "start": 1, "end": 10}}],
        })
        agent.session["history"].append({
            "role": "tool", "name": "read_file", "tool_use_id": "tu_rep", "content": result1,
        })
        result2 = agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 10})
        assert "error" in result2.lower()
        assert "repeated" in result2.lower()

        # 写操作重复拦截
        write_result1 = agent.run_tool("append_file", {"path": "README.md", "content": "test_line"})
        assert "error" not in write_result1.lower()
        agent.session["history"].append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_w1", "name": "append_file",
                         "input": {"path": "README.md", "content": "test_line"}}],
        })
        agent.session["history"].append({
            "role": "tool", "name": "append_file", "tool_use_id": "tu_w1", "content": write_result1,
        })
        write_result2 = agent.run_tool("append_file", {"path": "README.md", "content": "test_line"})
        assert "error" in write_result2.lower()
        assert "repeated" in write_result2.lower()

    def test_11_different_args_allowed(self, env):
        """相同工具不同参数不被拦截。"""
        agent = env["agent"]
        agent.session["history"] = []
        result1 = agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 5})
        agent.session["history"].append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_d1", "name": "read_file",
                         "input": {"path": "README.md", "start": 1, "end": 5}}],
        })
        agent.session["history"].append({
            "role": "tool", "name": "read_file", "tool_use_id": "tu_d1", "content": result1,
        })
        result2 = agent.run_tool("read_file", {"path": "README.md", "start": 6, "end": 10})
        assert "error" not in result2.lower()

    def test_12_sensitive_redaction(self, env):
        """API_KEY 环境变量值被脱敏。"""
        agent = env["agent"]
        os.environ["TEST_API_KEY"] = "sk-12345"
        try:
            redacted = agent.redact_text("using sk-12345 now")
            assert "sk-12345" not in redacted
            assert REDACTED_VALUE in redacted
        finally:
            del os.environ["TEST_API_KEY"]
