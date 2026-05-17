from bongo.task_status import (
    STOP_REASON_FINAL_ANSWER_RETURNED,
    STOP_REASON_RETRY_LIMIT_REACHED,
    STOP_REASON_STEP_LIMIT_REACHED,
    TaskStatus,
)


def test_task_status_starts_running_with_empty_progress():
    status = TaskStatus.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    assert status.task_id == "task_001"
    assert status.run_id == "run_001"
    assert status.user_request == "Inspect the repo."
    assert status.status == "running"
    assert status.tool_steps == 0
    assert status.attempts == 0
    assert status.last_tool == ""
    assert status.stop_reason == ""
    assert status.final_answer == ""


def test_task_status_records_success_and_final_answer():
    status = TaskStatus.create(run_id="run_002", task_id="task_002", user_request="Fix the bug.")
    status.record_attempt()
    status.record_tool("read_file")
    status.finish_success("Done.")

    assert status.attempts == 1
    assert status.tool_steps == 1
    assert status.last_tool == "read_file"
    assert status.status == "completed"
    assert status.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
    assert status.final_answer == "Done."


def test_task_status_records_step_limit_stop_reason():
    status = TaskStatus.create(run_id="run_003", task_id="task_003", user_request="Try again.")

    status.stop_step_limit()

    assert status.status == "stopped"
    assert status.stop_reason == STOP_REASON_STEP_LIMIT_REACHED


def test_task_status_records_retry_limit_stop_reason():
    status = TaskStatus.create(run_id="run_004", task_id="task_004", user_request="Try again.")

    status.stop_retry_limit()

    assert status.status == "stopped"
    assert status.stop_reason == STOP_REASON_RETRY_LIMIT_REACHED


def test_task_status_snapshot_keeps_final_answer():
    status = TaskStatus.create(run_id="run_005", task_id="task_005", user_request="Return the answer.")
    status.finish_success("Final answer.")

    snapshot = status.to_dict()

    assert snapshot["final_answer"] == "Final answer."
    assert snapshot["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED
