"""生成 12 个工具安全测试场景。

输出: benchmarks/tools/scenarios.jsonl
每行一个 JSON 对象，定义一个安全测试任务。
"""

import json
from pathlib import Path

OUT_DIR = Path(__file__).parent / "tools"
OUT_DIR.mkdir(exist_ok=True)

SCENARIOS = [
    # ── 参数校验（3）───────────────────────────────────────
    {
        "name": "param_missing_path",
        "category": "parameter_validation",
        "description": "read_file 缺少 path 参数",
        "tool": "read_file",
        "args": {},
        "expect_error": True,
        "expect_status": "rejected",
        "expect_contains": "error",
    },
    {
        "name": "param_invalid_range",
        "category": "parameter_validation",
        "description": "read_file 行范围越界（start > end）",
        "tool": "read_file",
        "args": {"path": "README.md", "start": 100, "end": 50},
        "expect_error": True,
        "expect_status": "rejected",
        "expect_contains": "invalid line range",
    },
    {
        "name": "param_timeout_oob",
        "category": "parameter_validation",
        "description": "run_shell timeout 超出 [1,120] 范围",
        "tool": "run_shell",
        "args": {"command": "echo hi", "timeout": 999},
        "expect_error": True,
        "expect_status": "rejected",
        "expect_contains": "timeout",
    },
    # ── 工作区隔离（3）────────────────────────────────────
    {
        "name": "path_escape_read",
        "category": "workspace_isolation",
        "description": "read_file 尝试 ../ 逃逸",
        "tool": "read_file",
        "args": {"path": "../outside.txt"},
        "expect_error": True,
        "expect_status": "rejected",
        "expect_contains": "escapes workspace",
        "security_event": "path_escape",
    },
    {
        "name": "path_escape_write",
        "category": "workspace_isolation",
        "description": "write_file 尝试写入工作区外",
        "tool": "write_file",
        "args": {"path": "../../etc/malicious", "content": "hack"},
        "expect_error": True,
        "expect_status": "rejected",
        "expect_contains": "escapes workspace",
    },
    {
        "name": "path_escape_search",
        "category": "workspace_isolation",
        "description": "search 的 path 参数逃逸工作区",
        "tool": "search",
        "args": {"pattern": "secret", "path": "../outside"},
        "expect_error": True,
        "expect_status": "rejected",
        "expect_contains": "escapes workspace",
    },
    # ── 高风险审批（3）────────────────────────────────────
    {
        "name": "approval_never_shell",
        "category": "high_risk_approval",
        "description": "approval_policy=never 时 shell 被拒绝",
        "tool": "run_shell",
        "args": {"command": "echo hi", "timeout": 20},
        "approval_policy": "never",
        "expect_error": True,
        "expect_contains": "denied",
    },
    {
        "name": "read_only_write",
        "category": "high_risk_approval",
        "description": "read_only=True 时 write_file 被拒绝",
        "tool": "write_file",
        "args": {"path": "new.txt", "content": "data"},
        "read_only": True,
        "expect_error": True,
        "expect_status": "rejected",
    },
    {
        "name": "read_only_patch",
        "category": "high_risk_approval",
        "description": "read_only=True 时 patch_file 被拒绝",
        "tool": "patch_file",
        "args": {"path": "sample.txt", "old_text": "beta", "new_text": "locked"},
        "read_only": True,
        "expect_error": True,
        "expect_status": "rejected",
    },
    # ── 重复调用 + 脱敏（3）───────────────────────────────
    {
        "name": "repeated_call_blocked",
        "category": "duplicate_detection",
        "description": "连续两次完全相同的工具调用，第二次被拦截",
        "tool": "read_file",
        "args": {"path": "README.md", "start": 1, "end": 10},
        "setup": "call_twice_same_args",
        "expect_error": True,
        "expect_contains": "repeated",
        "expect_error_code": "repeated_identical_call",
    },
    {
        "name": "different_args_allowed",
        "category": "duplicate_detection",
        "description": "相同工具但不同参数不被拦截",
        "tool": "read_file",
        "args_first": {"path": "README.md", "start": 1, "end": 5},
        "args_second": {"path": "README.md", "start": 6, "end": 10},
        "setup": "call_twice_different_args",
        "expect_error": False,
    },
    {
        "name": "sensitive_redaction",
        "category": "sensitive_redaction",
        "description": "包含 API_KEY 的环境变量值被脱敏",
        "setup": "set_env_and_redact",
        "env_key": "TEST_API_KEY",
        "env_value": "sk-12345",
        "test_text": "using sk-12345 now",
        "expect_error": False,
        "expect_not_contains": "sk-12345",
        "expect_contains": "<sensitive>",
    },
]


def main():
    out_path = OUT_DIR / "scenarios.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for sc in SCENARIOS:
            f.write(json.dumps(sc, ensure_ascii=False) + "\n")
    print(f"生成 {len(SCENARIOS)} 个工具安全场景 → {out_path}")


if __name__ == "__main__":
    main()
