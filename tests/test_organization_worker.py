from __future__ import annotations

import dataclasses
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import pytest
from filelock import Timeout as LockTimeout

import aitobuild.organization_worker as worker_module
from aitobuild.organization_delivery import _delivery_call
from aitobuild.organization import parse_organization_definition
from aitobuild.organization_assignments import AssignmentProposal
from aitobuild.organization_runner import FileRunStore, ManagedOperation, WorkflowInput
from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker
from test_dispatcher import approved_delivery as approved_delivery
from test_organization_runner import _service_fixture
from conftest import approve_preview


def make_worker(tmp_path, approved_delivery, operation=None, approval_policy=None, task_base_revision_for=None,
                **options):
    service, assignments, calls = _service_fixture(tmp_path, approved_delivery, operation=operation,
                                                   approval_policy=approval_policy,
                                                   task_base_revision_for=task_base_revision_for)
    options.setdefault("poll_seconds", 0.01)
    worker = ManagedOrganizationWorker(service=service, store=FileWorkerStore(tmp_path / "worker.json"), **options)
    return worker, service, assignments, calls


async def finish(worker, preview_id):
    async with asyncio.timeout(5):
        return await worker.wait_idle(preview_id)


def other_preview(approved_delivery, *, approve=True):
    previews, _, preview_id, _, revision = approved_delivery
    payload = json.loads(json.dumps(previews.get(preview_id).bundle_payload))
    payload["task_id"] = "other-worker-task"
    preview = previews.create_or_get(dedupe_key="other-worker-task", bundle_payload=payload, source_payload={})
    return approve_preview(previews, preview.preview_id, base_revision=revision) if approve else preview


@pytest.mark.parametrize("phase", ["open", "abort"])
def test_worker_abort_does_not_hide_budget_lock_timeout(tmp_path, approved_delivery, monkeypatch, phase):
    _, delivery, preview_id, _, _ = approved_delivery
    delivery.prepare(preview_id)
    worker, _, _, _ = make_worker(tmp_path, approved_delivery)
    job = worker.enqueue(preview_id)
    path = delivery.budget_path(preview_id)
    before = path.read_bytes()
    original = worker_module.DeveloperTaskBudget

    def locked_budget(**options):
        if phase == "open":
            raise LockTimeout(str(path) + ".lock")
        budget = original(**options)

        def locked_abort():
            raise LockTimeout(str(path) + ".lock")

        monkeypatch.setattr(budget, "abort", locked_abort)
        return budget

    monkeypatch.setattr(worker_module, "DeveloperTaskBudget", locked_budget)
    with pytest.raises(LockTimeout):
        asyncio.run(worker._abort(job))
    assert path.read_bytes() == before


def test_worker_abort_reopens_already_aborted_budget_without_reset(tmp_path, approved_delivery):
    _, delivery, preview_id, _, _ = approved_delivery
    delivery.prepare(preview_id)
    worker, _, _, _ = make_worker(tmp_path, approved_delivery)
    job = worker.enqueue(preview_id)
    path = delivery.budget_path(preview_id)
    before = json.loads(path.read_text())
    asyncio.run(worker._abort(job))
    asyncio.run(worker._abort(job))
    assert json.loads(path.read_text()) == before | {"aborted": True}


@pytest.mark.parametrize("kind,value", [("service_approval", True), ("human_input", "answer")])
def test_queued_decision_restart_consumes_once(tmp_path, approved_delivery, kind, value):
    _, delivery, preview_id, _, _ = approved_delivery
    calls = []
    operation = ManagedOperation(lambda context, message: calls.append("initial") or WorkflowInput("Decide", "saved", kind),
                                 on_response=lambda context, original, response: calls.append((original, response)) or response)

    async def exercise():
        first, service, _, _ = make_worker(tmp_path, approved_delivery, operation)
        first.enqueue(preview_id)
        await first.start()
        assert (await finish(first, preview_id)).state == "waiting"
        await first.close()
        run = service.recorded_run(first.store.get(preview_id).task_id)
        before = delivery.budget_path(preview_id).read_bytes()
        control = "approve" if kind == "service_approval" else "resume"
        command = first.enqueue_decision(run.assignment_id, request_id=run.pending[0].request_id, kind=control, value=value)
        assert command.state == "queued" and calls == ["initial"]
        with pytest.raises(ValueError):
            first.enqueue_decision(run.assignment_id, request_id=run.pending[0].request_id, kind=control, value=value)
        restarted, restored, _, _ = make_worker(tmp_path, approved_delivery, operation)
        await restarted.start()
        try:
            assert (await finish(restarted, preview_id)).state == "completed"
            assert len(restored.recorded_run(command.task_id).decisions) == 1
        finally:
            await restarted.close()
        assert delivery.budget_path(preview_id).read_bytes() == before

    asyncio.run(exercise())
    assert calls == ["initial", ("saved", value)]


@pytest.mark.parametrize("case", ["queued", "scope", "binding", "expired"])
def test_startup_queued_jobs_recheck_pins_and_original_budget(tmp_path, approved_delivery, case):
    _, delivery, preview_id, _, _ = approved_delivery
    delivery.prepare(preview_id)
    worker, service, _, calls = make_worker(tmp_path, approved_delivery)
    worker.enqueue(preview_id)
    before = json.loads(delivery.budget_path(preview_id).read_text())
    if case == "binding":
        service.binding_revision = "changed-binding"
    elif case == "scope":
        original = service.admission

        def changed(preview_id):
            preview, route = original(preview_id)
            preview.bundle_payload["objective"] = "Unapproved objective"
            return preview, route

        service.admission = changed
    elif case == "expired":
        before["deadline"] = 0
        delivery.budget_path(preview_id).write_text(json.dumps(before))

    async def exercise():
        await worker.start()
        try:
            receipt = await finish(worker, preview_id)
            assert receipt.state == ("completed" if case == "queued" else "failed"), receipt.error
        finally:
            await worker.close()

    asyncio.run(exercise())
    assert calls == (["proposal", "run", "cleanup"] if case == "queued" else [])
    after = json.loads(delivery.budget_path(preview_id).read_text())
    assert after["deadline"] == before["deadline"] and after["reserved_paths"] == before["reserved_paths"]
    assert bool(after.get("aborted")) == (case != "queued")


def test_interrupted_pre_assignment_job_aborts_without_replaying_proposal(tmp_path, approved_delivery):
    _, delivery, preview_id, _, _ = approved_delivery
    delivery.prepare(preview_id)
    worker, _, _, calls = make_worker(tmp_path, approved_delivery)
    job = worker.enqueue(preview_id)
    worker.store.save(job.model_copy(update={"state": "running"}))
    before = json.loads(delivery.budget_path(preview_id).read_text())

    async def exercise():
        await worker.start()
        try:
            failed = await finish(worker, preview_id)
            assert failed.state == "failed" and "Interrupted" in failed.error
        finally:
            await worker.close()

    asyncio.run(exercise())
    assert calls == []
    assert json.loads(delivery.budget_path(preview_id).read_text()) == before | {"aborted": True}


@pytest.mark.parametrize("case", ["decision", "native_running"])
def test_interrupted_decision_or_native_execution_never_replays(tmp_path, approved_delivery, case):
    _, delivery, preview_id, _, _ = approved_delivery
    calls = []
    operation = ManagedOperation(lambda context, message: calls.append("initial") or WorkflowInput("Decide", None, "service_approval"),
                                 on_response=lambda context, original, response: calls.append("resumed") or response)

    async def exercise():
        first, service, _, _ = make_worker(tmp_path, approved_delivery, operation)
        first.enqueue(preview_id)
        await first.start()
        await finish(first, preview_id)
        await first.close()
        job = first.store.get(preview_id)
        run = service.recorded_run(job.task_id)
        queued = first.enqueue_decision(run.assignment_id, request_id=run.pending[0].request_id, kind="approve", value=True)
        first.store.save(queued.model_copy(update={"state": "running"}))
        if case == "native_running":
            FileRunStore(tmp_path / "service-runs").save(run.model_copy(update={"state": "running", "pending": ()}))
        before = json.loads(delivery.budget_path(preview_id).read_text())
        restarted, restored, _, _ = make_worker(tmp_path, approved_delivery, operation)
        await restarted.start()
        try:
            failed = await finish(restarted, preview_id)
            assert failed.state == "failed" and "Interrupted" in failed.error
            assert restored.recorded_run(job.task_id).state in {"failed", "cancelled"}
        finally:
            await restarted.close()
        assert json.loads(delivery.budget_path(preview_id).read_text()) == before | {"aborted": True}

    asyncio.run(exercise())
    assert calls == ["initial"]


def test_worker_shutdown_drains_thread_before_cleanup_and_lock_release(tmp_path, approved_delivery):
    _, delivery, preview_id, _, _ = approved_delivery
    release = Event()
    events = []

    async def exercise():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocking_stage():
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
            events.append("thread-drained")

        async def operation(context, message):
            await _delivery_call(blocking_stage)
            return "done"

        worker, _, _, calls = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        worker.enqueue(preview_id)
        await worker.start()
        await started.wait()
        before = json.loads(delivery.budget_path(preview_id).read_text())
        close = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        assert not close.done()
        assert calls == ["proposal"]
        release.set()
        await close
        assert worker.store.get(preview_id).state == "cancelled"
        assert calls == ["proposal", "cleanup"]
        assert json.loads(delivery.budget_path(preview_id).read_text()) == before | {"aborted": True}

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert events == ["thread-drained"]


def test_operator_cancellation_keeps_worker_available_for_other_tasks(tmp_path, approved_delivery):
    _, _, preview_id, _, _ = approved_delivery
    second = other_preview(approved_delivery)

    async def exercise():
        started = asyncio.Event()

        async def operation(context, message):
            if context.assignment.preview_id == preview_id:
                started.set()
                await asyncio.Event().wait()
            return "done"

        worker, _, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        worker.enqueue(preview_id)
        await worker.start()
        try:
            await asyncio.wait_for(started.wait(), timeout=30)
            assert (await worker.cancel(preview_id)).state == "cancelled"
            worker.enqueue(second.preview_id)
            assert (await finish(worker, second.preview_id)).state == "completed"
            assert worker.diagnostics()["workers"] == 1
        finally:
            await worker.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("recover", [False, True])
def test_startup_discovery_is_explicit_and_never_replays_terminal_receipts(tmp_path, approved_delivery, recover):
    _, _, preview_id, _, _ = approved_delivery
    worker, _, _, calls = make_worker(tmp_path, approved_delivery, recover_approved=recover)

    async def exercise():
        await worker.start()
        try:
            if recover:
                assert (await finish(worker, preview_id)).state == "completed"
            else:
                assert worker.store.get(preview_id) is None
        finally:
            await worker.close()
        if recover:
            restarted, _, _, _ = make_worker(tmp_path, approved_delivery, recover_approved=True)
            await restarted.start()
            await restarted.close()
            assert restarted.store.get(preview_id).state == "completed"

    asyncio.run(exercise())
    assert calls == (["proposal", "run", "cleanup"] if recover else [])


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("locked", [False, True])
def test_receipt_backed_followup_queueing_is_opt_in_and_idempotent(tmp_path, approved_delivery, monkeypatch, enabled, locked):
    _, _, preview_id, _, _ = approved_delivery
    child = other_preview(approved_delivery)
    worker, service, _, calls = make_worker(tmp_path, approved_delivery, queue_follow_ups=enabled)
    worker.enqueue(preview_id)
    contended = []

    def discover(source):
        if locked and not contended:
            from filelock import Timeout

            contended.append(source)
            raise Timeout("saved-followup.lock")
        return child.preview_id if source == preview_id else None

    monkeypatch.setattr(service, "follow_up_preview_id", discover)
    child_queued = asyncio.Event()
    enqueue = worker.enqueue

    def admit(preview_id):
        job = enqueue(preview_id)
        if preview_id == child.preview_id:
            child_queued.set()
        return job

    monkeypatch.setattr(worker, "enqueue", admit)

    async def exercise():
        await worker.start()
        try:
            assert (await finish(worker, preview_id)).state == "completed"
            if enabled:
                await asyncio.wait_for(child_queued.wait(), timeout=5)
                assert (await finish(worker, child.preview_id)).state == "completed"
            else:
                assert worker.store.get(child.preview_id) is None
        finally:
            await worker.close()
        restarted, restored, _, _ = make_worker(tmp_path, approved_delivery, queue_follow_ups=enabled)
        monkeypatch.setattr(restored, "follow_up_preview_id", lambda source: child.preview_id if source == preview_id else None)
        await restarted.start()
        await restarted.close()
        assert len(restarted.store.list()) == (2 if enabled else 1)

    asyncio.run(exercise())
    assert calls == ["proposal", "run", "cleanup"] * (2 if enabled else 1)
    assert bool(contended) is (enabled and locked)


@pytest.mark.parametrize("case", ["human", "invalid_child", "already_failed"])
def test_followup_queueing_preserves_approval_and_terminal_parent(tmp_path, approved_delivery, monkeypatch, case):
    _, delivery, preview_id, _, _ = approved_delivery
    child = other_preview(approved_delivery, approve=False)
    worker, service, _, calls = make_worker(tmp_path, approved_delivery, queue_follow_ups=True)
    worker.enqueue(preview_id)
    discovered = asyncio.Event()

    def discover(source):
        discovered.set()
        return child.preview_id if case != "invalid_child" else "unknown-child"

    monkeypatch.setattr(service, "follow_up_preview_id", discover)

    async def exercise():
        await worker.start()
        try:
            assert (await finish(worker, preview_id)).state == "completed"
            before = delivery.budget_path(preview_id).read_bytes()
            if case == "already_failed":
                approve_preview(approved_delivery[0], child.preview_id, base_revision=approved_delivery[4])
                child_job = worker.enqueue(child.preview_id)
                worker.store.save(child_job.model_copy(update={"state": "failed", "error": "Saved terminal failure"}))
            await asyncio.wait_for(discovered.wait(), timeout=5)
            if case == "invalid_child":
                # #69: the follow-up error is contained and reported once; the loop keeps running.
                async with asyncio.timeout(5):
                    while not worker.diagnostics()["job_errors"]:
                        await asyncio.sleep(0.01)
                diagnostics = worker.diagnostics()
                assert diagnostics["job_errors"] == (f"{preview_id}: ValueError: Preview not found",)
                assert diagnostics["workers"] == 1 and diagnostics["health"]["status"] == "ok"
                assert not diagnostics["errors"] and worker.store.get("unknown-child") is None
            elif case == "human":
                assert worker.store.get(child.preview_id) is None and not child.approved
            else:
                assert worker.store.get(child.preview_id).state == "failed"
            assert worker.store.get(preview_id).state == "completed"
            assert delivery.budget_path(preview_id).read_bytes() == before
            assert not delivery.budget_path(child.preview_id).exists()
        finally:
            await worker.close()

    asyncio.run(exercise())
    assert calls == ["proposal", "run", "cleanup"]


def test_capped_followup_is_a_policy_refusal_left_for_a_human(tmp_path, approved_delivery, monkeypatch):
    """#69/#70 follow-up: a used-up max_automatic_admissions refuses the follow-up as policy (AdmissionRefused), so it
    lands in refused_follow_ups for a human, not in job_errors to be retried as an error every pass."""
    from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage
    from aitobuild.developer_preview import preview_approval_operation
    from aitobuild.organization_service import AdmissionRefused, Human, admit_task

    previews, delivery, preview_id, _, revision = approved_delivery
    policy = ApprovalPolicy(task_admission="automatic", max_automatic_admissions=1)
    worker, service, _, calls = make_worker(tmp_path, approved_delivery, approval_policy=policy, queue_follow_ups=True,
                                            task_base_revision_for=lambda preview: revision)
    payload = json.loads(json.dumps(previews.get(preview_id).bundle_payload))
    used = previews.create_or_get(dedupe_key="used-worker-task", bundle_payload=payload | {"task_id": "used-worker-task"},
                                  source_payload={})
    decision = policy.resolve(ApprovalStage.TASK_ADMISSION, actor_id="trusted-operator",
                              operation=preview_approval_operation(used.preview_id, used.bundle_payload),
                              automatic_supported=True, expected_revision=policy.revision)
    assert approve_preview(previews, used.preview_id, approval_policy=decision, max_automatic_admissions=1).approved
    child = other_preview(approved_delivery, approve=False)
    with pytest.raises(AdmissionRefused, match="admission limit reached"):  # the gate itself, before any worker
        service.admission(child.preview_id)
    worker.enqueue(preview_id)
    monkeypatch.setattr(service, "follow_up_preview_id", lambda source: child.preview_id if source == preview_id else None)

    async def exercise():
        await worker.start()
        try:
            assert (await finish(worker, preview_id)).state == "completed"
            async with asyncio.timeout(5):
                while child.preview_id not in worker.diagnostics()["refused_follow_ups"]:
                    await asyncio.sleep(0.01)
            diagnostics = worker.diagnostics()
            assert diagnostics["refused_follow_ups"] == (child.preview_id,)
            assert not diagnostics["job_errors"] and not diagnostics["errors"]
            assert diagnostics["health"]["status"] == "ok"
            assert worker.store.get(child.preview_id) is None and not previews.get(child.preview_id).approved
            # Over the limit a human admission still admits, and the loop runs it.
            assert admit_task(previews, child.preview_id, Human(base_revision=revision)).evidence_kind == "human"
            worker.enqueue(child.preview_id)
            assert (await finish(worker, child.preview_id)).state == "completed"
        finally:
            await worker.close()

    asyncio.run(exercise())
    assert calls == ["proposal", "run", "cleanup"] * 2


def test_persistent_followup_error_is_retried_at_most_once_per_poll(tmp_path, approved_delivery, monkeypatch):
    """#70 follow-up: a contained follow-up error is retried on the next pass, not in a tight loop. Each loop pass
    reaches the service at most once for it, and passes are paced by poll_seconds."""
    _, _, preview_id, _, _ = approved_delivery
    poll = 0.1
    worker, service, _, _ = make_worker(tmp_path, approved_delivery, queue_follow_ups=True, poll_seconds=poll)
    worker.enqueue(preview_id)
    monkeypatch.setattr(service, "follow_up_preview_id", lambda source: "unknown-child" if source == preview_id else None)
    attempts: list[float] = []
    passes: list[float] = []
    admission = service.admission
    journal = worker.store.list

    def counted_admission(identity):
        if identity == "unknown-child":
            attempts.append(time.monotonic())
        return admission(identity)

    def counted_list():
        passes.append(time.monotonic())
        return journal()

    monkeypatch.setattr(service, "admission", counted_admission)

    async def exercise():
        await worker.start()
        monkeypatch.setattr(worker.store, "list", counted_list)  # from here on, one call per loop pass
        try:
            assert (await finish(worker, preview_id)).state == "completed"
            async with asyncio.timeout(5):
                while not attempts:
                    await asyncio.sleep(0.01)
            attempts.clear()
            passes.clear()
            began = time.monotonic()
            await asyncio.sleep(1.0)
            elapsed = time.monotonic() - began
            window_attempts, window_passes = len(attempts), len(passes)
            assert worker.diagnostics()["job_errors"] == (f"{preview_id}: ValueError: Preview not found",)
            assert worker.health()["status"] == "ok"
        finally:
            await worker.close()
        return window_attempts, window_passes, elapsed

    window_attempts, window_passes, elapsed = asyncio.run(exercise())
    assert window_attempts >= 2  # still retried, so a transient error clears without a restart
    assert window_attempts <= window_passes + 1  # at most one service call per pass, no retry within a pass
    assert window_attempts <= elapsed / poll + 2  # passes are paced by the poll, not a tight loop


def test_worker_journal_refuses_corruption_and_symlinks(tmp_path, approved_delivery):
    worker, _, _, calls = make_worker(tmp_path, approved_delivery)
    path = tmp_path / "worker.json"
    path.write_text('{"schema_version":1,"jobs":[')
    original = path.read_bytes()
    with pytest.raises(ValueError):
        asyncio.run(worker.start())
    assert path.read_bytes() == original and calls == []
    linked = tmp_path / "linked-worker.json"
    linked.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        FileWorkerStore(linked).list()


def test_separate_worker_cancellation_observes_durable_intent(tmp_path, approved_delivery):
    _, _, preview_id, _, _ = approved_delivery

    async def exercise():
        started = asyncio.Event()

        async def operation(context, message):
            started.set()
            await asyncio.Event().wait()

        owner, _, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        other, _, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        owner.enqueue(preview_id)
        await owner.start()
        try:
            await started.wait()
            from filelock import Timeout

            with pytest.raises(Timeout):
                await other.cancel(preview_id)
            assert other.store.get(preview_id).cancel_requested
            assert (await finish(owner, preview_id)).state == "cancelled"
            assert owner.diagnostics()["workers"] == 1
        finally:
            await owner.close()

    asyncio.run(exercise())


def test_capacity_defers_without_aborting_or_resetting_original_budget(tmp_path, approved_delivery):
    _, delivery, preview_id, _, _ = approved_delivery
    second = other_preview(approved_delivery)

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        async def operation(context, message):
            if context.assignment.preview_id == preview_id:
                started.set()
                await release.wait()
            return "done"

        owner, _, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        other, _, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        owner.enqueue(preview_id)
        await owner.start()
        await started.wait()
        job = other.enqueue(second.preview_id)
        try:
            await other._execute(job)
            deferred = other.store.get(second.preview_id)
            assert deferred.state == "queued" and "capacity" in deferred.error
            before = json.loads(delivery.budget_path(second.preview_id).read_text())
            assert not before.get("aborted", False)
            release.set()
            assert (await finish(owner, preview_id)).state == "completed"
            assert (await finish(owner, second.preview_id)).state == "completed"
            assert json.loads(delivery.budget_path(second.preview_id).read_text()) == before
        finally:
            release.set()
            await owner.close()

    asyncio.run(exercise())


def test_two_worker_instances_execute_one_shared_receipt(tmp_path, approved_delivery):
    _, _, preview_id, _, _ = approved_delivery
    calls = []

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        async def operation(context, message):
            calls.append("run")
            started.set()
            await release.wait()
            return "done"

        first, _, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        second, _, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        first.enqueue(preview_id)
        await first.start()
        await second.start()
        try:
            await started.wait()
            assert second.enqueue(preview_id).state == "running"
            release.set()
            assert (await finish(first, preview_id)).state == "completed"
        finally:
            await first.close()
            await second.close()

    asyncio.run(exercise())
    assert calls == ["run"]


def test_journal_concurrent_admission_and_immutable_history(tmp_path, approved_delivery):
    _, _, preview_id, _, _ = approved_delivery
    worker, _, _, _ = make_worker(tmp_path, approved_delivery)
    job = worker.enqueue(preview_id)
    path = tmp_path / "worker.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: FileWorkerStore(path).admit(job), range(8)))
    assert all(receipt == job for receipt in receipts)
    assert worker.store.list() == (job,)
    for changed in (job.model_copy(update={"binding_revision": "replacement"}),
                    job.model_copy(update={"commands": (job.commands[0].model_copy(update={"actor_id": "model"}),)})):
        before = path.read_bytes()
        with pytest.raises(ValueError):
            worker.store.save(changed)
        assert path.read_bytes() == before


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_journal_schema_version_requires_exact_integer(tmp_path, version):
    path = tmp_path / "worker.json"
    path.write_text(json.dumps({"schema_version": version, "jobs": []}))
    with pytest.raises(ValueError):
        FileWorkerStore(path).list()


@pytest.mark.parametrize("stage", ["worker", "assignment"])
def test_interrupted_terminal_receipt_retries_metadata_only(tmp_path, approved_delivery, monkeypatch, stage):
    _, delivery, preview_id, _, _ = approved_delivery
    worker, service, _, calls = make_worker(tmp_path, approved_delivery)
    worker.enqueue(preview_id)
    original_save = worker.store.save

    def interrupted(job):
        if job.state == "completed":
            raise OSError("Interrupted worker receipt")
        original_save(job)

    async def exercise():
        with monkeypatch.context() as patch:
            if stage == "worker":
                patch.setattr(worker.store, "save", interrupted)
            else:
                patch.setattr(service._runner._service, "finish",
                              lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Interrupted assignment receipt")))
                with pytest.raises(OSError, match="Interrupted assignment receipt"):
                    await service.consume(preview_id)
                assert worker.store.get(preview_id).state == "queued"
            await worker.start()
            try:
                async with asyncio.timeout(5):
                    with pytest.raises(RuntimeError, match="stopped"):
                        await worker.wait_idle(preview_id)
                await asyncio.sleep(0)
                assert "Interrupted" in worker.diagnostics()["errors"][0]
            finally:
                await worker.close()
        retained = worker.store.get(preview_id)
        assert retained.state == "running"
        before = delivery.budget_path(preview_id).read_bytes()
        assert not json.loads(before).get("aborted", False)
        restarted, restored, _, restarted_calls = make_worker(tmp_path, approved_delivery)
        await restarted.start()
        try:
            assert (await finish(restarted, preview_id)).state == "completed"
            assert restored.recorded_run(retained.task_id).state == "completed"
        finally:
            await restarted.close()
        assert delivery.budget_path(preview_id).read_bytes() == before
        assert restarted_calls == []

    asyncio.run(exercise())
    assert calls == ["proposal", "run", "cleanup"]


def test_restart_drains_waiting_durable_cancel_intent(tmp_path, approved_delivery):
    _, _, preview_id, _, _ = approved_delivery
    operation = ManagedOperation(lambda context, message: WorkflowInput("Decide", None, "service_approval"),
                                 on_response=lambda *args: "must not replay")

    async def exercise():
        first, _, _, _ = make_worker(tmp_path, approved_delivery, operation)
        first.enqueue(preview_id)
        await first.start()
        assert (await finish(first, preview_id)).state == "waiting"
        await first.close()
        first.store.request_cancel(preview_id)
        restarted, _, _, _ = make_worker(tmp_path, approved_delivery, operation)
        await restarted.start()
        try:
            async with asyncio.timeout(5):
                while restarted.store.get(preview_id).state == "waiting":
                    await restarted._changed.wait()
            assert restarted.store.get(preview_id).state == "cancelled"
        finally:
            await restarted.close()

    asyncio.run(exercise())


def test_queued_activation_revision_survives_service_configuration_update(tmp_path, approved_delivery):
    _, _, preview_id, _, _ = approved_delivery
    worker, service, assignments, calls = make_worker(tmp_path, approved_delivery)
    job = worker.enqueue(preview_id)
    activation = service._routes[0]
    original = service._definitions.get(activation.organization_id, activation.revision)
    document = original.definition.model_dump(mode="json")
    document["workflows"][0]["document"]["nodes"][0]["operation"] = "not_registered_new_revision"
    changed = service._definitions.save(parse_organization_definition(json.dumps(document)))
    service._routes = (activation.model_copy(update={"revision": changed.revision, "event": "new.event"}),)
    assert worker.enqueue(preview_id).activation == job.activation

    async def exercise():
        await worker.start()
        try:
            assert (await finish(worker, preview_id)).state == "completed"
        finally:
            await worker.close()

    asyncio.run(exercise())
    assert assignments.for_task(job.task_id).revision == activation.revision
    assert assignments.for_task(job.task_id).event == activation.event
    assert calls == ["proposal", "run", "cleanup"]


def test_waiting_human_selection_is_durable_and_uses_trusted_operator(tmp_path, approved_delivery):
    _, _, preview_id, _, _ = approved_delivery
    worker, service, assignments, calls = make_worker(tmp_path, approved_delivery)
    activation = service._routes[0]
    document = service._definitions.get(activation.organization_id, activation.revision).definition.model_dump(mode="json")
    document["routes"][0]["delegation"] = {"strategy": "human", "eligible_agents": ["developer_one"]}
    changed = service._definitions.save(parse_organization_definition(json.dumps(document)))
    service._routes = (activation.model_copy(update={"revision": changed.revision}),)
    worker.enqueue(preview_id)

    async def exercise():
        await worker.start()
        assert (await finish(worker, preview_id)).state == "waiting"
        await worker.close()
        assert calls == []
        proposal = AssignmentProposal(agent_id="developer_one", rationale="Explicit operator choice")
        queued = worker.enqueue(preview_id, proposal=proposal)
        assert queued.state == "queued" and len(queued.commands) == 2
        assert worker.enqueue(preview_id, proposal=proposal) == queued
        await worker.start()
        try:
            assert (await finish(worker, preview_id)).state == "completed"
        finally:
            await worker.close()
        assigned = assignments.for_task(queued.task_id)
        assert assigned.actor_id == service.operator_id == queued.commands[-1].actor_id

    asyncio.run(exercise())
    assert calls == ["run", "cleanup"]


def test_interrupted_admission_save_never_invokes_graph(tmp_path, approved_delivery, monkeypatch):
    _, _, preview_id, _, _ = approved_delivery
    worker, _, _, calls = make_worker(tmp_path, approved_delivery)
    with monkeypatch.context() as patch:
        patch.setattr("aitobuild.durable_files.os.fsync",
                      lambda *args: (_ for _ in ()).throw(OSError("Interrupted admission")))
        with pytest.raises(OSError, match="Interrupted admission"):
            worker.enqueue(preview_id)
    assert worker.store.get(preview_id) is None
    assert calls == []


def test_cancel_queued_job_does_not_prepare_checkout_or_create_budget(tmp_path, approved_delivery):
    _, delivery, preview_id, _, _ = approved_delivery
    worker, _, _, calls = make_worker(tmp_path, approved_delivery)
    worker.enqueue(preview_id)
    assert asyncio.run(worker.cancel(preview_id)).state == "cancelled"
    assert not delivery.budget_path(preview_id).exists()
    assert delivery.get(preview_id) is None and calls == []

def test_admission_refuses_a_preview_without_approval_time(tmp_path, approved_delivery, monkeypatch):
    _, _, preview_id, _, _ = approved_delivery
    worker, service, _, calls = make_worker(tmp_path, approved_delivery)
    preview, route = service.admission(preview_id)
    monkeypatch.setattr(service, "admission", lambda _: (dataclasses.replace(preview, approved_at=None), route))
    with pytest.raises(PermissionError, match="approved preview"):
        worker.enqueue(preview_id)
    assert worker.store.get(preview_id) is None
    assert calls == []


@pytest.mark.parametrize("child_state", ["queued", "completed"])
def test_followup_queueing_skips_a_child_that_already_has_a_receipt(tmp_path, approved_delivery, monkeypatch, child_state):
    _, _, preview_id, _, _ = approved_delivery
    child = other_preview(approved_delivery)
    worker, service, _, calls = make_worker(tmp_path, approved_delivery, queue_follow_ups=True)
    worker.enqueue(preview_id)
    child_job = worker.enqueue(child.preview_id)
    if child_state == "completed":
        running = child_job.model_copy(update={"state": "running"})
        worker.store.save(running)
        worker.store.save(running.model_copy(update={"state": "completed"}))
    scans = []

    def discover(source):
        if source == preview_id:
            scans.append(source)
            return child.preview_id
        return None

    monkeypatch.setattr(service, "follow_up_preview_id", discover)
    enqueued = []
    original = worker.enqueue
    monkeypatch.setattr(worker, "enqueue", lambda identity, **kwargs: enqueued.append(identity) or original(identity, **kwargs))

    async def exercise():
        await worker.start()
        try:
            assert (await finish(worker, preview_id)).state == "completed"
            async with asyncio.timeout(5):
                while len(scans) < 3:
                    await asyncio.sleep(0.01)
        finally:
            await worker.close()

    asyncio.run(exercise())
    assert child.preview_id not in enqueued



class _ContendedLock:
    """Invocation lock that reports contention until a deadline, like a dispatcher that has not woken yet."""

    def __init__(self, lock, until):
        self._lock = lock
        self._until = until

    def acquire(self, *args, **kwargs):
        from filelock import Timeout

        if time.monotonic() < self._until:
            raise Timeout(self._lock.lock_file)
        return self._lock.acquire(*args, **kwargs)

    def release(self, *args, **kwargs):
        return self._lock.release(*args, **kwargs)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _cancel_under_contention(tmp_path, approved_delivery, monkeypatch, *, held_seconds):
    _, _, preview_id, _, _ = approved_delivery

    async def exercise():
        started = asyncio.Event()

        async def operation(context, message):
            started.set()
            await asyncio.Event().wait()

        worker, _, _, _ = make_worker(tmp_path, approved_delivery, ManagedOperation(operation))
        worker.enqueue(preview_id)
        await worker.start()
        try:
            await asyncio.wait_for(started.wait(), timeout=30)
            real = worker.store.invocation_lock
            until = time.monotonic() + held_seconds
            monkeypatch.setattr(worker.store, "invocation_lock",
                                lambda pid: _ContendedLock(real(pid), until) if pid == preview_id else real(pid))
            began = time.monotonic()
            try:
                return await asyncio.wait_for(worker.cancel(preview_id), timeout=30), time.monotonic() - began
            finally:
                monkeypatch.setattr(worker.store, "invocation_lock", real)
        finally:
            await worker.close()

    return asyncio.run(exercise())


def test_cancel_waits_for_local_dispatcher_to_release_lock(tmp_path, approved_delivery, monkeypatch):
    # Deterministic form of #33: the dispatcher still holds the lock briefly after the cancelled task ends.
    job, _ = _cancel_under_contention(tmp_path, approved_delivery, monkeypatch, held_seconds=0.2)
    assert job.state == "cancelled"


def test_local_cancel_lock_wait_is_bounded_and_explicit(tmp_path, approved_delivery, monkeypatch):
    from filelock import Timeout

    import aitobuild.organization_worker as module

    monkeypatch.setattr(module, "_LOCAL_CANCEL_LOCK_SECONDS", 0.2)
    began = time.monotonic()
    with pytest.raises(Timeout):
        _cancel_under_contention(tmp_path, approved_delivery, monkeypatch, held_seconds=60)
    assert time.monotonic() - began < 10


def test_unexpected_dispatch_error_is_contained_and_the_job_is_retried(tmp_path, approved_delivery, monkeypatch, caplog):
    """#69: an unexpected per-job error is logged once with its traceback, the loop keeps serving other jobs, and
    the failing job's receipt is left as it was so the next pass retries it."""
    _, _, preview_id, _, _ = approved_delivery
    other = other_preview(approved_delivery)
    worker, service, _, calls = make_worker(tmp_path, approved_delivery)
    worker.enqueue(preview_id)
    worker.enqueue(other.preview_id)
    original_lock = worker.store.invocation_lock
    failures = {"left": 3}

    def flaky_lock(identity):
        if identity == preview_id and failures["left"]:
            failures["left"] -= 1
            raise OSError("transient lock directory failure")
        return original_lock(identity)

    monkeypatch.setattr(worker.store, "invocation_lock", flaky_lock)

    async def exercise():
        await worker.start()
        try:
            assert (await finish(worker, other.preview_id)).state == "completed"
            assert (await finish(worker, preview_id)).state == "completed"
            diagnostics = worker.diagnostics()
            assert diagnostics["health"] == {"status": "ok", "workers_alive": 1, "workers_expected": 1}
            assert diagnostics["job_errors"] == (f"{preview_id}: OSError: transient lock directory failure",)
            assert not diagnostics["errors"]
        finally:
            await worker.close()

    with caplog.at_level("ERROR", logger="aitobuild.organization_worker"):
        asyncio.run(exercise())
    contained = [record for record in caplog.records if "contained an error" in record.getMessage()]
    assert len(contained) == 1 and contained[0].exc_info is not None  # logged once, with traceback
    assert failures["left"] == 0 and calls.count("run") == 2


def test_unreadable_journal_pass_does_not_end_the_loop(tmp_path, approved_delivery, monkeypatch):
    _, _, preview_id, _, _ = approved_delivery
    worker, _, _, _ = make_worker(tmp_path, approved_delivery)
    worker.enqueue(preview_id)
    original_list = worker.store.list
    broken = {"left": 2}

    def flaky_list():
        if broken["left"]:
            broken["left"] -= 1
            raise ValueError("journal briefly unreadable")
        return original_list()

    async def exercise():
        await worker.start()  # start reads the journal itself; the loop's own passes then hit the failure
        monkeypatch.setattr(worker.store, "list", flaky_list)
        try:
            assert (await finish(worker, preview_id)).state == "completed"
            assert broken["left"] == 0
            assert worker.diagnostics()["job_errors"] == ("<worker journal>: ValueError: journal briefly unreadable",)
            assert worker.health()["status"] == "ok"
        finally:
            await worker.close()

    asyncio.run(exercise())


def test_a_dead_loop_shows_up_in_health(tmp_path, approved_delivery, monkeypatch, caplog):
    """#69: a lost worker receipt stays fail-stop (no safe record or replay), and the dead loop is reported."""
    _, _, preview_id, _, _ = approved_delivery
    worker, _, _, _ = make_worker(tmp_path, approved_delivery, max_workers=2)
    worker.enqueue(preview_id)
    original_save = worker.store.save

    def lost_receipt(job):
        if job.state == "completed":
            raise OSError("Interrupted worker receipt")
        original_save(job)

    monkeypatch.setattr(worker.store, "save", lost_receipt)

    async def exercise():
        assert worker.health() == {"status": "not_started", "workers_alive": 0, "workers_expected": 0}
        await worker.start()
        try:
            assert worker.health()["status"] == "ok"
            async with asyncio.timeout(5):
                while worker.health()["workers_alive"] == 2:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0)
            health = worker.health()
            assert health["workers_expected"] == 2 and health["workers_alive"] in {0, 1}
            assert health["status"] == ("stopped" if health["workers_alive"] == 0 else "degraded")
            assert worker.diagnostics()["health"] == health
            assert "Interrupted worker receipt" in worker.diagnostics()["errors"][0]
        finally:
            await worker.close()

    with caplog.at_level("ERROR", logger="aitobuild.organization_worker"):
        asyncio.run(exercise())
    assert any(record.getMessage() == "Managed worker loop stopped" and record.exc_info for record in caplog.records)
