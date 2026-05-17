import json

from bongo.run_store import RunStore
from bongo.task_status import STOP_REASON_FINAL_ANSWER_RETURNED, TaskStatus


def test_run_store_creates_run_directory_and_status_file(tmp_path):
    store = RunStore(tmp_path / ".bongo" / "runs")
    status = TaskStatus.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    run_dir = store.start_run(status)

    assert run_dir == store.run_dir(status.run_id)
    assert run_dir.exists()
    persisted = json.loads((run_dir / "task_status.json").read_text(encoding="utf-8"))
    assert persisted["task_id"] == "task_001"
    assert persisted["run_id"] == "run_001"
    assert persisted["user_request"] == "Inspect the repo."


def test_run_store_appends_trace_jsonl(tmp_path):
    store = RunStore(tmp_path / ".bongo" / "runs")
    status = TaskStatus.create(run_id="run_002", task_id="task_002", user_request="Trace the run.")
    store.start_run(status)

    store.append_trace(status, {"event": "run_started", "created_at": "2026-04-07T00:00:00+00:00"})
    store.append_trace(
        status.run_id,
        {
            "event": "prompt_built",
            "created_at": "2026-04-07T00:00:01+00:00",
            "prompt_metadata": {"prompt_chars": 128, "secret_env_count": 1},
        },
    )
    store.append_trace(status.run_id, {"event": "run_finished", "created_at": "2026-04-07T00:00:02+00:00"})

    lines = (store.trace_path(status.run_id)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["event"] == "prompt_built"
    assert json.loads(lines[2])["event"] == "run_finished"


def test_run_store_writes_report_json(tmp_path):
    store = RunStore(tmp_path / ".bongo" / "runs")
    status = TaskStatus.create(run_id="run_003", task_id="task_003", user_request="Report the run.")
    store.start_run(status)
    status.finish_success("Done.")

    store.write_task_status(status)
    store.write_report(status, {"task_status": status.to_dict(), "stop_reason": status.stop_reason})

    report = json.loads(store.report_path(status.run_id).read_text(encoding="utf-8"))
    assert report["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED
    assert report["task_status"]["final_answer"] == "Done."


def test_run_store_tolerates_missing_final_report(tmp_path):
    store = RunStore(tmp_path / ".bongo" / "runs")
    status = TaskStatus.create(run_id="run_004", task_id="task_004", user_request="Crash before finalize.")

    store.start_run(status)
    store.append_trace(status, {"event": "run_started"})

    assert store.trace_path(status.run_id).exists()
    assert not store.report_path(status.run_id).exists()
