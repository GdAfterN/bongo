import hashlib
import json
import locale as locale_module
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import memory as memorylib
from .models import FakeModelClient
from .runtime import bongo, SessionStore
from .run_store import RunStore
from .task_status import STOP_REASON_FINAL_ANSWER_RETURNED
# WorkspaceContext removed - using work_dir instead

# ... existing code ...
BENCHMARK_SCHEMA_VERSION = 1  # 基准测试配置文件的版本号，用于确保代码与 JSON 格式兼容
DEFAULT_BENCHMARK_PATH = Path("benchmarks/coding_tasks.json")  # 默认的任务定义文件路径（即“试卷”的位置）
DEFAULT_ARTIFACT_PATH = Path("benchmarks/benchmarks-v1.json")   # 默认的运行结果输出路径（即“成绩单”的保存位置）
DEFAULT_MODEL_NAME = "FakeModelClient"      # 默认使用的模型客户端名称（通常用于测试或模拟环境）
DEFAULT_MODEL_VERSION = "scripted-deterministic"  # 默认模型版本：脚本化确定性模型（即不依赖真实 AI，而是按预设剧本回复）
DEFAULT_TEMPERATURE = 0.0                   # 默认温度参数：设为 0 保证模型输出完全确定，没有随机性
DEFAULT_TOP_P = 1.0                         # 默认 Top-p 采样参数：保留所有概率质量的 token
DEFAULT_MAX_NEW_TOKENS = 64                 # 默认最大生成 Token 数：限制模型单次回复的长度
DEFAULT_TIMEZONE = "Asia/Shanghai"          # 默认时区：用于记录任务执行的时间戳

# 校验 Benchmark JSON 文件时必须包含的顶层键
REQUIRED_BENCHMARK_KEYS = ("schema_version", "tasks")

# 校验每一个具体任务（Task）时必须包含的字段
REQUIRED_TASK_KEYS = (
    "id",              # 任务唯一标识符
    "prompt",          # 给 Agent 的指令
    "fixture_repo",    # 任务运行的隔离环境（文件夹）
    "allowed_tools",   # 允许 Agent 使用的工具列表
    "step_budget",     # 允许的最大执行步数
    "expected_artifact", # 预期产生的文件或修改描述
    "verifier",        # 自动验证脚本（Python 代码字符串）
    "category",        # 任务分类（如 documentation, text-edit）
)

# 定义不同测试环境（Fixture）中需要关注的核心产物文件
TASK_FIXTURE_ARTIFACTS = {
    "bench_repo_readme": "README.md",  # 在 readme 测试环境中，主要检查 README.md 的变化
    "bench_repo_patch": "sample.txt",  # 在 patch 测试环境中，主要检查 sample.txt 的变化
}

# 脚本化模型的预设输出剧本。
# 当使用 "FakeModelClient" 时，它不会调用真实 AI，而是根据任务 ID 直接返回这些预定义的字符串。
# 这用于在不消耗 API 费用的情况下测试 bongo 的执行逻辑和验证器是否正确。
SCRIPTED_MODEL_OUTPUTS = {
    # 任务：修改 README 引言
    "readme_intro_locked": [
        {"type": "tool_use", "id": "toolu_bench_001", "name": "patch_file", "input": {"path": "README.md", "old_text": "This is a placeholder benchmarks fixture.", "new_text": "This fixture is a locked benchmarks workspace."}},
        "Done.",
    ],
    # 任务：修改 README 关于 schema 的说明
    "readme_schema_note": [
        {"type": "tool_use", "id": "toolu_bench_002", "name": "patch_file", "input": {"path": "README.md", "old_text": "- Placeholder note about the repo.", "new_text": "- The benchmarks schema and baseline are fixed."}},
        "Done.",
    ],
    # 任务：修改 README 关于文件排序的说明
    "readme_ordering_note": [
        {"type": "tool_use", "id": "toolu_bench_003", "name": "patch_file", "input": {"path": "README.md", "old_text": "- Placeholder note about the file layout.", "new_text": "- Deterministic file ordering keeps benchmarks diffs stable."}},
        "Done.",
    ],
    # 任务：将 sample.txt 中的 beta 替换为 beta-locked
    "sample_beta_locked": [
        {"type": "tool_use", "id": "toolu_bench_004", "name": "patch_file", "input": {"path": "sample.txt", "old_text": "beta", "new_text": "beta-locked"}},
        "Done.",
    ],
    # 任务：将 sample.txt 中的 gamma 替换为 gamma-locked
    "sample_gamma_locked": [
        {"type": "tool_use", "id": "toolu_bench_005", "name": "patch_file", "input": {"path": "sample.txt", "old_text": "gamma", "new_text": "gamma-locked"}},
        "Done.",
    ],
    # 任务：将 sample.txt 中的 placeholder 替换为 delta
    "sample_placeholder_delta": [
        {"type": "tool_use", "id": "toolu_bench_006", "name": "patch_file", "input": {"path": "sample.txt", "old_text": "placeholder", "new_text": "delta"}},
        "Done.",
    ],
}
# ... existing code ...


# 安全地获取当前环境
def _git_value(args, fallback="", cwd=None):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or fallback
    except Exception:
        return fallback


def _current_locale():
    try:
        return locale_module.setlocale(locale_module.LC_CTYPE)
    except Exception:
        return locale_module.getdefaultlocale()[0] or "C"


def _now_in_timezone(timezone_name):
    return datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _artifact_path_for_task(task):
    fixture_repo_name = Path(str(task["fixture_repo"])).name
    if fixture_repo_name not in TASK_FIXTURE_ARTIFACTS:
        raise ValueError(f"unsupported fixture repo for artifact lookup: {fixture_repo_name}")
    return TASK_FIXTURE_ARTIFACTS[fixture_repo_name]


def _workspace_relative(path, workspace_root):
    return str(Path(path).resolve().relative_to(Path(workspace_root).resolve()))


def _scripted_outputs_for_task(task):
    outputs = SCRIPTED_MODEL_OUTPUTS.get(task["id"])
    if outputs is None:
        raise ValueError(f"no scripted model outputs for benchmarks task: {task['id']}")
    return list(outputs)


def _fixture_snapshot_id(fixture_paths):
    sha = hashlib.sha256()
    for fixture_path in sorted({Path(path).resolve() for path in fixture_paths}, key=lambda path: str(path)):
        for path in sorted((item for item in fixture_path.rglob("*") if item.is_file()), key=lambda item: str(item.relative_to(fixture_path))):
            sha.update(str(fixture_path.name).encode("utf-8"))
            sha.update(b"\0")
            sha.update(str(path.relative_to(fixture_path)).encode("utf-8"))
            sha.update(b"\0")
            sha.update(path.read_bytes())
            sha.update(b"\0")
    return "sha256:" + sha.hexdigest()


def validate_benchmark(data, repo_root=None):
    if not isinstance(data, dict):
        raise ValueError("benchmarks must be a mapping")

    missing = [key for key in REQUIRED_BENCHMARK_KEYS if key not in data]
    if missing:
        raise ValueError(f"benchmarks is missing required keys: {', '.join(missing)}")

    if int(data.get("schema_version", 0)) != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported benchmarks schema_version")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("benchmarks tasks must be a non-empty list")

    repo_root = Path(repo_root or Path.cwd()).resolve()
    seen_ids = set()
    normalized_tasks = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"benchmarks task at index {index} must be a mapping")

        missing_task_keys = [key for key in REQUIRED_TASK_KEYS if key not in task]
        if missing_task_keys:
            raise ValueError(
                f"benchmarks task {task.get('id', index)!r} is missing required keys: {', '.join(missing_task_keys)}"
            )

        task_id = str(task["id"]).strip()
        if not task_id:
            raise ValueError(f"benchmarks task at index {index} has an empty id")
        if task_id in seen_ids:
            raise ValueError(f"duplicate benchmarks task id: {task_id}")
        seen_ids.add(task_id)

        fixture_repo = repo_root / str(task["fixture_repo"])
        if not fixture_repo.is_dir():
            raise ValueError(f"benchmarks task {task_id} fixture repo does not exist: {task['fixture_repo']}")

        allowed_tools = task["allowed_tools"]
        if not isinstance(allowed_tools, list) or not allowed_tools:
            raise ValueError(f"benchmarks task {task_id} allowed_tools must be a non-empty list")
        normalized_allowed_tools = []
        for tool in allowed_tools:
            tool_name = str(tool).strip()
            if not tool_name:
                raise ValueError(f"benchmarks task {task_id} has an empty allowed_tools entry")
            normalized_allowed_tools.append(tool_name)

        step_budget = int(task["step_budget"])
        if step_budget < 1:
            raise ValueError(f"benchmarks task {task_id} step_budget must be positive")

        normalized_task = dict(task)
        normalized_task["id"] = task_id
        normalized_task["prompt"] = str(task["prompt"]).strip()
        normalized_task["fixture_repo"] = str(task["fixture_repo"]).strip()
        normalized_task["allowed_tools"] = normalized_allowed_tools
        normalized_task["step_budget"] = step_budget
        normalized_task["expected_artifact"] = str(task["expected_artifact"]).strip()
        normalized_task["verifier"] = str(task["verifier"]).strip()
        normalized_task["category"] = str(task["category"]).strip()
        normalized_tasks.append(normalized_task)

    normalized = dict(data)
    normalized["schema_version"] = BENCHMARK_SCHEMA_VERSION
    normalized["tasks"] = normalized_tasks
    return normalized


def load_benchmark(path=DEFAULT_BENCHMARK_PATH, repo_root=None):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if repo_root is None:
        repo_root = path.resolve().parent.parent
    return validate_benchmark(data, repo_root=repo_root)


def summarize_rows(rows):
    rows = list(rows)
    passed = sum(1 for row in rows if row.get("passed") or row.get("status") == "pass")
    failed = len(rows) - passed
    failure_category_counts = {}
    for row in rows:
        if row.get("passed") or row.get("status") == "pass":
            continue
        category = str(row.get("failure_category") or "unknown")
        failure_category_counts[category] = failure_category_counts.get(category, 0) + 1

    total_tasks = len(rows)
    within_budget = sum(1 for row in rows if row.get("within_budget"))
    verifier_passes = sum(1 for row in rows if row.get("verifier_passed"))
    return {
        "total_tasks": total_tasks,
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / total_tasks) if total_tasks else 0.0,
        "within_budget": within_budget,
        "verifier_passes": verifier_passes,
        "failure_category_counts": failure_category_counts,
    }


class BenchmarkEvaluator:
    def __init__(
        self,
        benchmark_path=DEFAULT_BENCHMARK_PATH,
        artifact_path=DEFAULT_ARTIFACT_PATH,
        workspace_root=None,
        model_name=DEFAULT_MODEL_NAME,
        model_version=DEFAULT_MODEL_VERSION,
        temperature=DEFAULT_TEMPERATURE,
        top_p=DEFAULT_TOP_P,
        max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        timezone_name=DEFAULT_TIMEZONE,
        model_client_factory=None,
    ):
        self.benchmark_path = Path(benchmark_path)
        self.artifact_path = Path(artifact_path)
        self.workspace_root = Path(workspace_root) if workspace_root is not None else Path(
            tempfile.mkdtemp(prefix="bongo-benchmarks-")
        )
        self.model_name = model_name
        self.model_version = model_version
        self.temperature = temperature
        self.top_p = top_p
        self.max_new_tokens = max_new_tokens
        self.timezone_name = timezone_name
        self.model_client_factory = model_client_factory
        self.repo_root = self.benchmark_path.resolve().parent.parent

    def load(self):
        return load_benchmark(self.benchmark_path, repo_root=self.repo_root)

    def run(self):
        benchmark = self.load()
        rows = [self.run_task(task) for task in benchmark["tasks"]]
        summary = summarize_rows(rows)
        artifact = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "captured_at": _now_in_timezone(self.timezone_name),
            "runtime": {
                "commit_sha": _git_value(["rev-parse", "HEAD"], cwd=self.repo_root),
                "branch": _git_value(["branch", "--show-current"], cwd=self.repo_root),
            },
            "benchmarks": {
                "source": str(self.benchmark_path.resolve().relative_to(self.repo_root)),
                "task_count": len(benchmark["tasks"]),
            },
            "reproducibility": {
                "fixture_snapshot_id": _fixture_snapshot_id(
                    self.repo_root / str(task["fixture_repo"]) for task in benchmark["tasks"]
                ),
                "model_name": self.model_name,
                "model_version": self.model_version,
                "decoding": {
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "max_new_tokens": self.max_new_tokens,
                },
                "timezone": self.timezone_name,
                "locale": _current_locale(),
            },
            "summary": summary,
            "failure_category_counts": summary["failure_category_counts"],
            "rows": rows,
        }
        self._write_artifact(artifact)
        return artifact

    def run_task(self, task):
        task = dict(task)
        fixture_source = self.repo_root / task["fixture_repo"]
        fixture_copy_root = self.workspace_root / task["id"] / fixture_source.name
        if fixture_copy_root.exists():
            shutil.rmtree(fixture_copy_root)
        fixture_copy_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture_source, fixture_copy_root)

        session_store = SessionStore(fixture_copy_root / ".bongo" / "sessions")
        run_store = RunStore(fixture_copy_root / ".bongo" / "reports")
        if self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, work_dir=fixture_copy_root)
        else:
            model_client = FakeModelClient(_scripted_outputs_for_task(task))
        agent = bongo(
            model_client=model_client,
            work_dir=fixture_copy_root,
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
        )

        initial_history_empty = len(agent.session["history"]) == 0
        initial_memory_state = agent.memory.to_dict()
        initial_memory_empty = initial_memory_state == memorylib.default_memory_state()
        initial_task_summary_empty = not str(initial_memory_state["working"]["task_summary"]).strip()
        initial_relevant_notes_empty = not agent.session.get("relevant_notes", [])

        final_answer = agent.ask(task["prompt"])
        task_status = agent.current_task_status
        run_dir = Path(agent.current_run_dir)
        task_status_path = agent.run_store.task_status_path(task_status)
        report_path = agent.run_store.report_path(task_status)
        report = agent.run_store.load_report(task_status.run_id)

        artifact_path = _artifact_path_for_task(task)
        artifact_file = fixture_copy_root / artifact_path
        expected_artifact_exists = artifact_file.exists()
        artifact_digest = _digest_file(artifact_file) if expected_artifact_exists else ""

        verifier = subprocess.run(
            task["verifier"],
            cwd=fixture_copy_root,
            shell=True,
            capture_output=True,
            text=True,
        )

        within_budget = task_status.tool_steps <= int(task["step_budget"])
        verifier_passed = verifier.returncode == 0
        non_failure_stop_reason = task_status.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
        passed = within_budget and verifier_passed and expected_artifact_exists and non_failure_stop_reason
        failure_category = None if passed else self._failure_category(
            within_budget=within_budget,
            verifier_passed=verifier_passed,
            expected_artifact_exists=expected_artifact_exists,
            non_failure_stop_reason=non_failure_stop_reason,
        )

        return {
            "id": task["id"],
            "prompt": task["prompt"],
            "fixture_repo": task["fixture_repo"],
            "fixture_copy_relpath": _workspace_relative(fixture_copy_root, self.workspace_root),
            "run_id": task_status.run_id,
            "run_dir_relpath": _workspace_relative(run_dir, self.workspace_root),
            "task_status_relpath": _workspace_relative(task_status_path, self.workspace_root),
            "report_relpath": _workspace_relative(report_path, self.workspace_root),
            "allowed_tools": list(task["allowed_tools"]),
            "step_budget": int(task["step_budget"]),
            "expected_artifact": task["expected_artifact"],
            "artifact_path": artifact_path,
            "artifact_exists": expected_artifact_exists,
            "artifact_digest": artifact_digest,
            "verifier": task["verifier"],
            "verifier_exit_code": verifier.returncode,
            "verifier_stdout": verifier.stdout,
            "verifier_stderr": verifier.stderr,
            "category": task["category"],
            "status": "pass" if passed else "fail",
            "passed": passed,
            "failure_category": failure_category,
            "within_budget": within_budget,
            "verifier_passed": verifier_passed,
            "expected_artifact_exists": expected_artifact_exists,
            "non_failure_stop_reason": non_failure_stop_reason,
            "tool_steps": task_status.tool_steps,
            "attempts": task_status.attempts,
            "final_answer": final_answer,
            "stop_reason": task_status.stop_reason,
            "initial_history_empty": initial_history_empty,
            "initial_memory_empty": initial_memory_empty,
            "initial_task_summary_empty": initial_task_summary_empty,
            "initial_relevant_notes_empty": initial_relevant_notes_empty,
            "task_status": task_status.to_dict(),
            "report": report,
        }

    def _failure_category(
        self,
        within_budget,
        verifier_passed,
        expected_artifact_exists,
        non_failure_stop_reason,
    ):
        if not expected_artifact_exists:
            return "missing_artifact"
        if not within_budget:
            return "budget_exceeded"
        if not verifier_passed:
            return "verifier_failed"
        if not non_failure_stop_reason:
            return "failure_stop_reason"
        return "unknown"

    def _write_artifact(self, artifact):
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _digest_file(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_fixed_benchmark(
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=DEFAULT_ARTIFACT_PATH,
    workspace_root=None,
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_VERSION,
    temperature=DEFAULT_TEMPERATURE,
    top_p=DEFAULT_TOP_P,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    timezone_name=DEFAULT_TIMEZONE,
    model_client_factory=None,
):
    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=workspace_root,
        model_name=model_name,
        model_version=model_version,
        temperature=temperature,
        top_p=top_p,
        max_new_tokens=max_new_tokens,
        timezone_name=timezone_name,
        model_client_factory=model_client_factory,
    )
    return evaluator.run()
