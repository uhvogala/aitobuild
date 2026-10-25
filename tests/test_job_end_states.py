"""#78: a worker job's end state comes from its run's recorded end state, and only an operator cancel command records
"Operator cancelled workflow".

Evidence: pilot trial4-20261019-034521 (04:20:33 EEST): a shutdown landed while ``service.decide`` was staging the
follow-up of the Architect review, after its run and assignment had completed; the job was stored ``cancelled``
("Worker invocation cancelled") and the completed run's budget was aborted. Pilot trial4-20261019-052917 (05:42:19
EEST): a re-check failing on a GitHub TLS handshake timeout stored the run ``cancelled`` with "Operator cancelled
workflow" though no cancel was sent (that case is pinned in test_worker_transient_recheck.py, at the deadline)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json

import pytest

from aitobuild.organization_runner import OPERATOR_CANCELLED, FileRunStore, ManagedOperation, WorkflowInput
from aitobuild.organization_worker import FileWorkerStore, WorkerJournal, job_end_state
from test_dispatcher import approved_delivery as approved_delivery
from test_organization_worker import finish, make_worker

APPROVAL = ManagedOperation(lambda context, message: WorkflowInput("Review", None, "service_approval"),
                            on_response=lambda context, original, approved: "reviewed")


def _waiting(tmp_path, approved_delivery, operation=APPROVAL):
    """A job whose run waits on a service approval; the first worker is closed."""
    _, _, preview_id, _, _ = approved_delivery

    async def prepare():
        worker, service, _, _ = make_worker(tmp_path, approved_delivery, operation)
        worker.enqueue(preview_id)
        await worker.start()
        try:
            assert (await finish(worker, preview_id)).state == "waiting"
        finally:
            await worker.close()
        return service, service.recorded_run(worker.store.get(preview_id).task_id)

    service, run = asyncio.run(prepare())
    return preview_id, service, run


def _budget(approved_delivery, preview_id) -> dict:
    return json.loads(approved_delivery[1].budget_path(preview_id).read_text())


def test_shutdown_after_the_run_completed_keeps_the_job_completed(tmp_path, approved_delivery, monkeypatch):
    """trial4-20261019-034521: the approval completes the run, then the shutdown lands in the follow-up step."""
    preview_id, _, waiting = _waiting(tmp_path, approved_delivery)
    worker, service, assignments, _ = make_worker(tmp_path, approved_delivery, APPROVAL)
    worker.enqueue_decision(waiting.assignment_id, request_id=waiting.pending[0].request_id, kind="approve", value=True)
    budget_before = _budget(approved_delivery, preview_id)
    in_follow_up = asyncio.Event()

    async def staging_follow_up(preview, run):
        assert run.state == "completed"
        in_follow_up.set()
        await asyncio.Event().wait()  # the shutdown lands here

    monkeypatch.setattr(service, "_follow_up", staging_follow_up)

    async def exercise():
        await worker.start()
        await asyncio.wait_for(in_follow_up.wait(), 5)
        await worker.close()

    asyncio.run(exercise())
    job = worker.store.get(preview_id)
    run = service.recorded_run(job.task_id)
    assert run.state == "completed" and run.error is None
    assert assignments.get(run.assignment_id).state == "completed"
    assert job.state == "completed" and job.error is None  # was: cancelled, "Worker invocation cancelled"
    assert _budget(approved_delivery, preview_id) == budget_before  # a completed run's budget is not aborted


def test_failure_after_the_run_completed_keeps_the_job_completed_and_reports_the_error(
    tmp_path, approved_delivery, monkeypatch,
):
    preview_id, _, waiting = _waiting(tmp_path, approved_delivery)
    worker, service, _, _ = make_worker(tmp_path, approved_delivery, APPROVAL)
    worker.enqueue_decision(waiting.assignment_id, request_id=waiting.pending[0].request_id, kind="approve", value=True)

    async def failing_follow_up(preview, run):
        raise RuntimeError("follow-up staging failed")

    monkeypatch.setattr(service, "_follow_up", failing_follow_up)

    async def exercise():
        await worker.start()
        try:
            return await finish(worker, preview_id)
        finally:
            await worker.close()

    job = asyncio.run(exercise())
    assert job.state == "completed" and job.error is None
    assert service.recorded_run(job.task_id).state == "completed"
    assert any("follow-up staging failed" in error for error in worker.diagnostics()["job_errors"])
    assert set(worker.diagnostics()["model_content_filter"]) == {"refusals", "retries", "recovered", "failed"}
    assert worker.diagnostics()["model_content_filter_scope"] == "process"


def test_operator_cancel_of_a_waiting_run_records_the_operator_label(tmp_path, approved_delivery):
    preview_id, service, waiting = _waiting(tmp_path, approved_delivery)
    worker, _, assignments, _ = make_worker(tmp_path, approved_delivery, APPROVAL)
    job = asyncio.run(worker.cancel(preview_id))
    run = service.recorded_run(job.task_id)
    assert job.cancel_requested and job.state == "cancelled" and job.error == "Operator cancelled worker admission"
    assert run.state == "cancelled" and run.error == OPERATOR_CANCELLED == "Operator cancelled workflow"
    assert assignments.get(run.assignment_id).state == "cancelled"
    assert _budget(approved_delivery, preview_id).get("aborted") is True


def test_operator_cancel_of_an_active_invocation_is_recorded_as_an_operator_cancel(tmp_path, approved_delivery):
    _, _, preview_id, _, _ = approved_delivery

    async def exercise():
        started = asyncio.Event()

        async def operation(context, message):
            started.set()
            await asyncio.Event().wait()

        worker, service, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        worker.enqueue(preview_id)
        await worker.start()
        try:
            await asyncio.wait_for(started.wait(), 5)
            job = await worker.cancel(preview_id)
        finally:
            await worker.close()
        return job, service.recorded_run(job.task_id)

    job, run = asyncio.run(exercise())
    assert job.state == "cancelled" and job.error == "Operator cancelled worker admission"  # was "Worker invocation cancelled"
    assert run.state == "cancelled"


def test_shutdown_of_an_active_invocation_is_not_recorded_as_an_operator_cancel(tmp_path, approved_delivery):
    _, _, preview_id, _, _ = approved_delivery

    async def exercise():
        started = asyncio.Event()

        async def operation(context, message):
            started.set()
            await asyncio.Event().wait()

        worker, service, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        worker.enqueue(preview_id)
        await worker.start()
        await asyncio.wait_for(started.wait(), 5)
        await worker.close()
        job = worker.store.get(preview_id)
        return job, service.recorded_run(job.task_id)

    job, run = asyncio.run(exercise())
    assert job.state == "cancelled" and job.error == "Worker invocation cancelled" and not job.cancel_requested
    assert run.state == "cancelled" and run.error != OPERATOR_CANCELLED


def test_worker_failure_on_a_waiting_run_fails_the_run_with_the_real_error(tmp_path, approved_delivery, monkeypatch):
    preview_id, service, waiting = _waiting(tmp_path, approved_delivery)
    worker, _, assignments, _ = make_worker(tmp_path, approved_delivery, APPROVAL)
    worker.enqueue_decision(waiting.assignment_id, request_id=waiting.pending[0].request_id, kind="approve", value=True)

    def scope_changed(job):
        raise PermissionError("Worker approved scope changed")

    monkeypatch.setattr(worker, "_validate", scope_changed)

    async def exercise():
        await worker.start()
        try:
            return await finish(worker, preview_id)
        finally:
            await worker.close()

    job = asyncio.run(exercise())
    run = service.recorded_run(job.task_id)
    assert job.state == "failed" and job.error == "Worker approved scope changed" and not job.cancel_requested
    assert run.state == "failed" and run.error == "Worker approved scope changed"  # was: cancelled, operator label
    assert assignments.get(run.assignment_id).state == "failed"
    assert _budget(approved_delivery, preview_id).get("aborted") is True


@pytest.mark.parametrize("state", ["cancelled", "failed", "completed"])
def test_terminate_refuses_the_operator_label_for_any_state(tmp_path, approved_delivery, state):
    _, service, waiting = _waiting(tmp_path, approved_delivery)
    with pytest.raises(ValueError, match="operator-cancel label"):
        asyncio.run(service.terminate(waiting.assignment_id, state=state, error=OPERATOR_CANCELLED))  # type: ignore[arg-type]
    run = FileRunStore(tmp_path / "service-runs").get(waiting.assignment_id)
    assert run.state == "waiting" and run.error is None


def test_only_an_operator_cancel_can_record_the_operator_label(tmp_path, approved_delivery):
    _, service, waiting = _waiting(tmp_path, approved_delivery)
    with pytest.raises(ValueError, match="operator-cancel label"):
        asyncio.run(service.terminate(waiting.assignment_id, state="cancelled", error=OPERATOR_CANCELLED))
    with pytest.raises(ValueError):
        asyncio.run(service.terminate(waiting.assignment_id, state="completed", error="x"))  # type: ignore[arg-type]
    assert FileRunStore(tmp_path / "service-runs").get(waiting.assignment_id).state == "waiting"


@pytest.mark.parametrize("run_state,expected", [
    (None, ("failed", "worker error")),
    ("waiting", ("failed", "worker error")),
    ("completed", ("completed", None)),
    ("failed", ("failed", "worker error")),
    ("cancelled", ("cancelled", "run label")),
])
def test_one_rule_for_job_end_states(tmp_path, approved_delivery, run_state, expected):
    _, _, run = _waiting(tmp_path, approved_delivery)
    if run_state is not None:
        run = run.model_copy(update={"state": run_state, "pending": (),
                                     "error": None if run_state in {"waiting", "completed"} else "run label"})
    assert job_end_state(None if run_state is None else run, "failed", "worker error") == expected
    if run_state == "completed":
        assert job_end_state(run, "cancelled", "Worker invocation cancelled") == ("completed", None)


def test_older_records_load_as_they_are(tmp_path, approved_delivery):
    """No new state and no record-format change: a journal and a run receipt written before #78, including the
    034521 contradiction itself (a cancelled job whose run completed), load and stay byte-for-byte unchanged."""
    preview_id, service, waiting = _waiting(tmp_path, approved_delivery)
    store_path = tmp_path / "worker.json"
    journal = json.loads(store_path.read_text())
    job = journal["jobs"][0]
    assert set(job) == {"preview_id", "task_id", "activation", "binding_revision", "bundle_content", "scope_digest",
                        "approved_at", "commands", "created_at", "updated_at", "state", "error", "cancel_requested",
                        "refusal"}
    job.update(state="cancelled", error="Worker invocation cancelled",
               updated_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"))
    store_path.write_text(json.dumps(journal))
    runs = FileRunStore(tmp_path / "service-runs")
    asyncio.run(service.cancel(waiting.assignment_id))  # a run receipt with the operator label, as stored before
    run_path = next((tmp_path / "service-runs").glob("*/run.json"))
    journal_bytes, run_bytes = store_path.read_bytes(), run_path.read_bytes()

    assert WorkerJournal.model_validate_json(journal_bytes).jobs[0].state == "cancelled"
    assert runs.get(waiting.assignment_id).error == "Operator cancelled workflow"
    worker, _, _, _ = make_worker(tmp_path, approved_delivery, APPROVAL)

    async def exercise():
        await worker.start()
        try:
            await asyncio.sleep(0.1)  # several loop passes over the terminal receipt
        finally:
            await worker.close()

    asyncio.run(exercise())
    assert FileWorkerStore(store_path).get(preview_id).error == "Worker invocation cancelled"
    assert store_path.read_bytes() == journal_bytes and run_path.read_bytes() == run_bytes  # no migration, no rewrite
