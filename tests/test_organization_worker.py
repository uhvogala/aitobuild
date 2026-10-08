from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import pytest

from aitobuild.organization_delivery import _delivery_call
from aitobuild.organization import parse_organization_definition
from aitobuild.organization_assignments import AssignmentProposal
from aitobuild.organization_runner import FileRunStore, ManagedOperation, WorkflowInput
from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker
from test_dispatcher import approved_delivery as approved_delivery
from test_organization_runner import _service_fixture


def make_worker(tmp_path, approved_delivery, operation=None, **options):
    service, assignments, calls = _service_fixture(tmp_path, approved_delivery, operation=operation)
    worker = ManagedOrganizationWorker(service=service, store=FileWorkerStore(tmp_path / "worker.json"),
                                       poll_seconds=0.01, **options)
    return worker, service, assignments, calls


async def finish(worker, preview_id):
    async with asyncio.timeout(5):
        return await worker.wait_idle(preview_id)


def other_preview(approved_delivery):
    previews, _, preview_id, _, revision = approved_delivery
    payload = json.loads(json.dumps(previews.get(preview_id).bundle_payload))
    payload["task_id"] = "other-worker-task"
    preview = previews.create_or_get(dedupe_key="other-worker-task", bundle_payload=payload, source_payload={})
    return previews.approve(preview.preview_id, base_revision=revision)


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
            await started.wait()
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