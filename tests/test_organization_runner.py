from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent_framework import FileSessionStore
from agent_framework.openai import OpenAIChatCompletionClient
from filelock import Timeout as LockTimeout
import httpx
from openai import AsyncOpenAI
import pytest

from aitobuild.agent_tools import DeveloperToolContext
from aitobuild.developer_delivery import DeveloperDeliveryWorker, LocalRepositorySource
from aitobuild.developer_isolation import DeveloperTaskBudget
from aitobuild.organization import FileDefinitionStore, parse_organization_definition
from aitobuild.organization_assignments import AssignmentService, FileAssignmentStore
from aitobuild.organization_delivery import DeliveryInvocation, ManagedDeliveryBindings, NativeDeliveryImplementation
from aitobuild.organization_runner import (
    FileRunStore, ManagedOperation, ManagedRun, ManagedWorkflowRunner, RuntimeActor, WorkflowInput,
)
from aitobuild.policy import ActionClass, AgentRole
from aitobuild.organization_runtime import ModelProfile, bootstrap_organization
from aitobuild.tools.filesystem import MockFilesystemAdapter
from aitobuild.tools.github import MockGitHubAdapter
from test_dispatcher import approved_delivery as approved_delivery, verification_adapter as verification_adapter
from test_organization_assignments import approved_task, coordinator_proposal, setup_service, assign


def runner_fixture(tmp_path, operations=None, actor=None, cleanup=None):
    service, snapshot, previews, assignments = setup_service(tmp_path)
    preview = approved_task(tmp_path, previews)
    assignment = assign(service, snapshot, preview, **coordinator_proposal())

    async def clean(context):
        pass

    runner = ManagedWorkflowRunner(
        definitions=FileDefinitionStore(tmp_path / "definitions"), assignments=assignments,
        assignment_service=service, runs=FileRunStore(tmp_path / "runs"),
        actor_provider=actor or (lambda: RuntimeActor("operator", "operator")),
        operations=operations or {"definition_probe": ManagedOperation(lambda context, message: message)},
        cleanup=cleanup or clean, binding_revision="test-v1",
    )
    return runner, assignment, service, snapshot, previews, assignments


def test_native_managed_run_pins_and_duplicate_receipt(tmp_path):
    calls = []
    runner, assignment, _, _, _, assignments = runner_fixture(tmp_path, operations={
        "definition_probe": ManagedOperation(lambda context, message: calls.append(context.run.run_id) or message),
    })
    before = Path(assignment.budget_path).read_bytes()
    run = asyncio.run(runner.start(assignment.assignment_id, input="hello"))
    assert run.state == "completed", run.error
    assert run.outputs == ("hello",)
    assert run.revision == assignment.revision
    assert run.checkpoint_id and run.checkpoint_digest
    assert run.cleanup_succeeded is True
    assert assignments.get(assignment.assignment_id).state == "completed"
    assert asyncio.run(runner.start(assignment.assignment_id, input="hello")) == run
    assert len(calls) == 1
    assert Path(assignment.budget_path).read_bytes() == before


def test_native_human_input_restart_restores_without_replaying_initial_operation(tmp_path):
    calls = []
    operation = ManagedOperation(
        lambda context, message: calls.append("initial") or WorkflowInput("Choose a value", message),
        on_response=lambda context, original, response: calls.append("response") or {"original": original, "value": response},
    )
    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": operation})
    waiting = asyncio.run(runner.start(assignment.assignment_id, input="hello"))
    assert waiting.state == "waiting", waiting.error
    restarted, *_ = runner_fixture(tmp_path, {"definition_probe": operation})
    completed = asyncio.run(restarted.resume(
        assignment.assignment_id, request_id=waiting.pending[0].request_id, response="chosen",
    ))
    assert completed.state == "completed", completed.error
    assert completed.outputs == ({"original": "hello", "value": "chosen"},)
    assert completed.run_id == waiting.run_id
    assert calls == ["initial", "response"]


@pytest.mark.parametrize("entrypoint", ["runner", "service", "worker"])
def test_configured_delivery_stages_reuse_guarded_services_and_original_budget(
    tmp_path, approved_delivery, verification_adapter, monkeypatch, entrypoint,
):
    previews, _, preview_id, source, _ = approved_delivery
    worker = DeveloperDeliveryWorker(
        preview_registry=previews, state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source, ("python -m pytest -q",)),),
    )
    worker.prepare(preview_id)
    document = json.loads((Path(__file__).resolve().parents[1] / "config/organization.example.json").read_text())
    stages = ["prepare", "implement", "verify", "publish"]
    document["workflows"][0]["document"] = {
        "format": "python_graph", "start": "prepare",
        "nodes": [{"id": stage, "kind": "operation", "operation": "delivery_" + stage} for stage in stages],
        "edges": [{"source": first, "target": second} for first, second in zip(stages, stages[1:])],
        "outputs": ["publish"],
    }
    definitions = FileDefinitionStore(tmp_path / "definitions")
    snapshot = definitions.save(parse_organization_definition(json.dumps(document)))
    assignments = FileAssignmentStore(tmp_path / "assignments.json")
    service = AssignmentService(
        definitions=definitions, previews=previews, assignments=assignments, budget_path_for=worker.budget_path,
    )
    if entrypoint == "runner":
        assignment = assign(service, snapshot, previews.get(preview_id), **coordinator_proposal())
    calls = []

    class Implementation:
        async def run(self, context):
            calls.append("implement")
            assignment = context.assignment
            with worker.implementation_lock(preview_id):
                record = worker.begin_implementation(
                    preview_id, bundle=context.assignment.bundle, session_id=context.run.session_id, resume=False,
                )
                budget = DeveloperTaskBudget(path=Path(assignment.budget_path), bundle=assignment.bundle, create=False)
                budget.reserve_paths(("src/probe.py",))
                target = Path(record.checkout_path) / "src/probe.py"
                target.parent.mkdir()
                target.write_text("def probe():\n    return True\n")
                worker.finish_implementation(preview_id, session_id=context.run.session_id)
            return DeliveryInvocation(context.run.session_id)

        async def resume(self, context, *, request_id, approved):
            raise AssertionError("No tool approval was requested")

        async def cleanup(self, context):
            calls.append("cleanup")
            return True

    github = MockGitHubAdapter(allowed_repositories=frozenset({"fixture/widgets"}), enforce_allowlist=True)
    bindings = ManagedDeliveryBindings(
        worker=worker, implementation=Implementation(), verification_adapter=verification_adapter,
        github=github, allow_mock_publication=True,
    )
    monkeypatch.setattr("aitobuild.developer_delivery.shell_request", lambda *args, **kwargs: {
        "ok": True, "status": "exited", "exit_code": 0, "output": "passed", "next_cursor": 6,
    })
    runner = ManagedWorkflowRunner(
        definitions=definitions, assignments=assignments, assignment_service=service,
        runs=FileRunStore(tmp_path / "runs"), actor_provider=lambda: RuntimeActor("operator", "operator"),
        operations=bindings.operations, cleanup=bindings.cleanup, binding_revision="delivery-test-v1",
    )
    from aitobuild.organization_service import ManagedOrganizationService, ManagedRoute

    async def coordinator(snapshot, preview):
        return coordinator_proposal()["proposal"]

    managed_service = ManagedOrganizationService(
        definitions=definitions, assignments=assignments, runs=FileRunStore(tmp_path / "runs"),
        previews=previews, worker=worker, operator_id="service-operator",
        routes=(ManagedRoute(repository="fixture/widgets", repository_id=101,
                             organization_id=snapshot.organization_id, revision=snapshot.revision,
                             event="github.issue.ready"),), coordinators={"planner": coordinator},
        operations=bindings.operations, cleanup=bindings.cleanup, binding_revision="delivery-test-v1",
    )
    budget_path = worker.budget_path(preview_id)
    before = json.loads(budget_path.read_text())

    async def invoke():
        if entrypoint != "worker":
            return await managed_service.consume(preview_id) if entrypoint == "service" else await runner.start(assignment.assignment_id)
        from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker

        detached = ManagedOrganizationWorker(service=managed_service, store=FileWorkerStore(tmp_path / "worker.json"))
        detached.enqueue(preview_id)
        await detached.start()
        try:
            async with asyncio.timeout(5):
                job = await detached.wait_idle(preview_id)
            return managed_service.recorded_run(job.task_id)
        finally:
            await detached.close()

    run = asyncio.run(invoke())
    assert run.state == "completed", run.error
    assert run.outputs == ({"preview_id": preview_id, "state": "published"},)
    assert worker.get(preview_id).verification["cleanup_succeeded"] is True
    assert len(github.branch_commits) == 1
    assert verification_adapter.closed
    assert calls == ["implement", "cleanup"]
    assert asyncio.run(invoke()) == run
    after = json.loads(budget_path.read_text())
    assert after == before | {"reserved_paths": ["src/probe.py"]}
    assert (source / "README.md").read_text() == "Fixture baseline\n"


def _service_fixture(tmp_path, approved_delivery, operation=None, coordinator=None):
    from aitobuild.organization_service import ManagedOrganizationService, ManagedRoute

    previews, worker, preview_id, _, _ = approved_delivery
    document = json.loads((Path(__file__).resolve().parents[1] / "config/organization.example.json").read_text())
    for agent in document["agents"]:
        agent["max_concurrent_runs"] = 1
    definitions = FileDefinitionStore(tmp_path / "service-definitions")
    snapshot = definitions.save(parse_organization_definition(json.dumps(document)))
    assignments = FileAssignmentStore(tmp_path / "service-assignments.json")
    calls = []

    async def propose(snapshot, preview):
        calls.append("proposal")
        return coordinator_proposal()["proposal"]

    async def cleanup(context):
        calls.append("cleanup")

    service = ManagedOrganizationService(
        definitions=definitions, assignments=assignments, runs=FileRunStore(tmp_path / "service-runs"),
        previews=previews, worker=worker, operator_id="trusted-operator",
        routes=(ManagedRoute(repository="fixture/widgets", repository_id=101,
                             organization_id=snapshot.organization_id, revision=snapshot.revision,
                             event="github.issue.ready"),),
        coordinators={"planner": coordinator or propose},
        operations={"definition_probe": operation or ManagedOperation(lambda context, message: calls.append("run") or "done")},
        cleanup=cleanup, binding_revision="service-v1",
    )
    return service, assignments, calls


def test_service_consumes_approved_preview_once(tmp_path, approved_delivery):
    service, assignments, calls = _service_fixture(tmp_path, approved_delivery)
    _, worker, preview_id, _, _ = approved_delivery
    run = asyncio.run(service.consume(preview_id))
    assert run.state == "completed", run.error
    before = worker.budget_path(preview_id).read_bytes()
    assert asyncio.run(service.consume(preview_id)) == run
    assert assignments.get(run.assignment_id).actor_id == "planner"
    assert calls == ["proposal", "run", "cleanup"]
    assert worker.budget_path(preview_id).read_bytes() == before


def test_service_admission_concurrency_and_cancellation_abort_original_budget(tmp_path, approved_delivery):
    _, worker, preview_id, _, _ = approved_delivery
    calls = []

    async def exercise():
        started = asyncio.Event()

        async def coordinator(snapshot, preview):
            calls.append("proposal")
            started.set()
            await asyncio.Event().wait()

        service, assignments, _ = _service_fixture(tmp_path, approved_delivery, coordinator=coordinator)
        task = asyncio.create_task(service.consume(preview_id))
        await started.wait()
        before = json.loads(worker.budget_path(preview_id).read_text())
        with pytest.raises(LockTimeout):
            await service.consume(preview_id)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert assignments.for_task(approved_delivery[0].get(preview_id).bundle_payload["task_id"]) is None
        assert json.loads(worker.budget_path(preview_id).read_text()) == before | {"aborted": True}
        with pytest.raises(ValueError):
            await service.consume(preview_id)

    asyncio.run(exercise())
    assert calls == ["proposal"]


def test_service_capacity_rejection_keeps_prepared_budget_for_later_admission(tmp_path, approved_delivery):
    previews, worker, preview_id, _, revision = approved_delivery
    operation = ManagedOperation(lambda context, message: WorkflowInput("Wait", None),
                                 on_response=lambda context, original, response: response)
    service, assignments, _ = _service_fixture(tmp_path, approved_delivery, operation=operation)
    first = asyncio.run(service.consume(preview_id))
    assert first.state == "waiting"
    payload = json.loads(json.dumps(previews.get(preview_id).bundle_payload))
    payload["task_id"] = "other-approved-task"
    other = previews.create_or_get(dedupe_key="other-approved-task", bundle_payload=payload, source_payload={})
    other = previews.approve(other.preview_id, base_revision=revision)
    with pytest.raises(ValueError, match="capacity"):
        asyncio.run(service.consume(other.preview_id))
    assert assignments.for_task("other-approved-task") is None
    before = worker.budget_path(other.preview_id).read_bytes()
    assert asyncio.run(service.cancel(first.assignment_id)).state == "cancelled"
    admitted = asyncio.run(service.consume(other.preview_id))
    assert admitted.state == "waiting"
    assert worker.budget_path(other.preview_id).read_bytes() == before
    assert asyncio.run(service.cancel(admitted.assignment_id)).state == "cancelled"


def test_service_cancellation_uses_frozen_assignment_when_preview_is_unreadable(tmp_path, approved_delivery, monkeypatch):
    previews, worker, preview_id, _, _ = approved_delivery
    operation = ManagedOperation(lambda context, message: WorkflowInput("Wait", None),
                                 on_response=lambda context, original, response: response)
    service, _, calls = _service_fixture(tmp_path, approved_delivery, operation=operation)
    waiting = asyncio.run(service.consume(preview_id))
    before = json.loads(worker.budget_path(preview_id).read_text())

    def unreadable(preview_id):
        raise ValueError("Corrupt mutable preview")

    monkeypatch.setattr(previews, "get", unreadable)
    terminal = asyncio.run(service.cancel(waiting.assignment_id))
    assert terminal.state == "cancelled"
    assert calls == ["proposal", "cleanup"]
    assert json.loads(worker.budget_path(preview_id).read_text()) == before | {"aborted": True}


def test_detached_worker_durably_enqueues_and_executes_once(tmp_path, approved_delivery):
    from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker

    _, _, preview_id, _, _ = approved_delivery
    calls = []

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def operation(context, message):
            calls.append("run")
            started.set()
            await release.wait()
            return "done"

        service, _, _ = _service_fixture(tmp_path, approved_delivery, operation=ManagedOperation(operation))
        store = FileWorkerStore(tmp_path / "worker.json")
        worker = ManagedOrganizationWorker(service=service, store=store)
        admitted = worker.enqueue(preview_id)
        assert admitted.state == "queued" and calls == []
        assert FileWorkerStore(tmp_path / "worker.json").get(preview_id) == admitted
        await worker.start()
        try:
            await started.wait()
            assert worker.enqueue(preview_id).state == "running"
            release.set()
            assert (await worker.wait_idle(preview_id)).state == "completed"
            assert worker.enqueue(preview_id).state == "completed"
        finally:
            await worker.close()

    asyncio.run(exercise())
    assert calls == ["run"]


def test_coordinator_identity_is_bound_by_trusted_provider_not_proposal(tmp_path):
    actor = [RuntimeActor("agent", "developer_two")]
    runner, _, _, snapshot, previews, assignments = runner_fixture(tmp_path, actor=lambda: actor[0])
    other = approved_task(tmp_path, previews, task="other-task")
    with pytest.raises(PermissionError, match="coordinator"):
        runner.assign(organization_id=snapshot.organization_id, revision=snapshot.revision,
                      event="github.issue.ready", preview_id=other.preview_id, proposal=coordinator_proposal()["proposal"])
    assert assignments.for_task("other-task") is None
    actor[0] = RuntimeActor("agent", "planner")
    decision = coordinator_proposal("developer_two")["proposal"]
    assigned = runner.assign(organization_id=snapshot.organization_id, revision=snapshot.revision,
                             event="github.issue.ready", preview_id=other.preview_id, proposal=decision)
    assert assigned.actor_id == "planner"
    with pytest.raises(ValueError):
        type(decision).model_validate(decision.model_dump() | {"coordinator_id": "planner"})


def test_changed_definition_does_not_replace_active_revision(tmp_path):
    operation = ManagedOperation(lambda context, message: WorkflowInput("Choose", message),
                                 on_response=lambda context, original, value: context.snapshot.revision)
    runner, assignment, _, snapshot, *_ = runner_fixture(tmp_path, {"definition_probe": operation})
    waiting = asyncio.run(runner.start(assignment.assignment_id, input="pinned"))
    updated = snapshot.definition.model_dump(mode="json")
    updated["agents"][0]["instructions"] = "New coordinator guidance"
    changed = FileDefinitionStore(tmp_path / "definitions").save(parse_organization_definition(json.dumps(updated)))
    assert changed.revision != waiting.revision
    completed = asyncio.run(runner.resume(assignment.assignment_id, request_id=waiting.pending[0].request_id, response="ok"))
    assert completed.state == "completed"
    assert completed.outputs == (snapshot.revision,)


def test_service_approval_is_distinct_from_native_input_and_requires_operator(tmp_path):
    actor = [RuntimeActor("agent", "planner")]
    calls = []
    operation = ManagedOperation(
        lambda context, message: WorkflowInput("Saved tool", {"request_id": "tool-one"}, "service_approval"),
        on_response=lambda context, original, approved: calls.append(approved) or approved,
    )
    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": operation}, actor=lambda: actor[0])
    waiting = asyncio.run(runner.start(assignment.assignment_id))
    request_id = waiting.pending[0].request_id
    with pytest.raises(PermissionError, match="not service approval"):
        asyncio.run(runner.resume(assignment.assignment_id, request_id=request_id, response=True))
    with pytest.raises(PermissionError, match="operator"):
        asyncio.run(runner.approve(assignment.assignment_id, request_id=request_id, approved=True))
    assert not calls
    actor[0] = RuntimeActor("operator", "human-one")
    completed = asyncio.run(runner.approve(assignment.assignment_id, request_id=request_id, approved=True))
    assert completed.state == "completed", completed.error
    assert completed.decisions[0].actor_id == "human-one"
    assert completed.decisions[0].kind == "service_approval"
    with pytest.raises(ValueError, match="pending"):
        asyncio.run(runner.approve(assignment.assignment_id, request_id=request_id, approved=True))
    assert calls == [True]


@pytest.mark.parametrize("case", ["expired", "aborted", "missing_budget", "scope", "base", "approval", "checkpoint"])
def test_resume_rechecks_scope_budget_and_checkpoint_without_replaying(tmp_path, case):
    calls = []
    operation = ManagedOperation(
        lambda context, message: WorkflowInput("Input", message),
        on_response=lambda context, original, value: calls.append("resumed") or value,
    )
    runner, assignment, _, _, _, assignments = runner_fixture(tmp_path, {"definition_probe": operation})
    waiting = asyncio.run(runner.start(assignment.assignment_id))
    budget_path = Path(assignment.budget_path)
    budget = json.loads(budget_path.read_text())
    if case == "expired":
        budget["deadline"] = 0
        budget_path.write_text(json.dumps(budget))
    elif case == "aborted":
        DeveloperTaskBudget(path=budget_path, bundle=assignment.bundle, create=False).abort()
    elif case == "missing_budget":
        budget_path.unlink()
    elif case in {"scope", "base", "approval"}:
        path = tmp_path / "previews.json"
        data = json.loads(path.read_text())
        preview = data["previews"][0]
        if case == "scope":
            preview["bundle_payload"]["objective"] = "Unapproved scope"
        elif case == "base":
            preview["bundle_payload"]["issue_context"]["base_revision"] = "b" * 40
        else:
            preview["approved"] = False
        path.write_text(json.dumps(data))
    else:
        path = FileRunStore(tmp_path / "runs").checkpoint_path(assignment.assignment_id) / (waiting.checkpoint_id + ".json")
        path.write_text(path.read_text() + " ")
    try:
        failed = asyncio.run(runner.resume(assignment.assignment_id, request_id=waiting.pending[0].request_id, response="ok"))
    except (ValueError, PermissionError):
        failed = FileRunStore(tmp_path / "runs").get(assignment.assignment_id)
    assert failed.state in {"failed", "finalizing"}
    assert not calls
    if case != "missing_budget":
        after = json.loads(budget_path.read_text())
        assert after["deadline"] == budget["deadline"]
        assert after["reserved_paths"] == budget["reserved_paths"]
        assert after["aborted"] is True
        assert assignments.get(assignment.assignment_id).state == "failed"
    else:
        assert not budget_path.exists()


def test_interrupted_running_state_aborts_and_never_restores_old_checkpoint(tmp_path):
    calls = []
    operation = ManagedOperation(lambda context, message: calls.append("initial") or WorkflowInput("Input", message),
                                 on_response=lambda context, original, value: calls.append("response") or value)
    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": operation})
    waiting = asyncio.run(runner.start(assignment.assignment_id))
    store = FileRunStore(tmp_path / "runs")
    store.save(ManagedRun.model_validate(waiting.model_dump() | {"state": "running", "pending": ()}))
    failed = asyncio.run(runner.start(assignment.assignment_id))
    assert failed.state == "failed" and "Interrupted" in failed.error
    assert failed.cleanup_succeeded is True
    assert calls == ["initial"]
    assert asyncio.run(runner.start(assignment.assignment_id)) == failed


def test_concurrent_duplicate_invocation_does_not_execute_twice(tmp_path):
    calls = []

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def operation(context, message):
            calls.append("invoke")
            started.set()
            await release.wait()
            return message

        first, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": ManagedOperation(operation)})
        second, *_ = runner_fixture(tmp_path, {"definition_probe": ManagedOperation(operation)})
        task = asyncio.create_task(first.start(assignment.assignment_id, input="hello"))
        await started.wait()
        with pytest.raises(LockTimeout):
            await second.start(assignment.assignment_id, input="hello")
        release.set()
        run = await task
        assert await second.start(assignment.assignment_id, input="hello") == run

    asyncio.run(exercise())
    assert calls == ["invoke"]


def test_duplicate_input_and_untrusted_actor_cannot_replace_run(tmp_path):
    actor = [RuntimeActor("operator", "operator")]
    runner, assignment, *_ = runner_fixture(tmp_path, actor=lambda: actor[0])
    run = asyncio.run(runner.start(assignment.assignment_id, input="original"))
    with pytest.raises(ValueError, match="replace"):
        asyncio.run(runner.start(assignment.assignment_id, input="changed"))
    actor[0] = RuntimeActor("agent", "developer_two")
    with pytest.raises(PermissionError, match="coordinator"):
        asyncio.run(runner.start(assignment.assignment_id, input="original"))
    assert FileRunStore(tmp_path / "runs").get(assignment.assignment_id) == run


def test_binding_revision_drift_fails_without_replaying_pending_handler(tmp_path):
    calls = []
    operation = ManagedOperation(lambda context, message: WorkflowInput("Input", message),
                                 on_response=lambda *args: calls.append("resumed"))
    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": operation})
    waiting = asyncio.run(runner.start(assignment.assignment_id))
    runner._binding_revision = "changed-v2"
    failed = asyncio.run(runner.resume(assignment.assignment_id, request_id=waiting.pending[0].request_id, response="ok"))
    assert failed.state == "failed" and "binding revision" in failed.error
    assert not calls


@pytest.mark.parametrize("change", ["agent_id", "workflow_id", "revision"])
def test_ownership_and_definition_pin_drift_cannot_resume(tmp_path, change):
    calls = []
    operation = ManagedOperation(lambda context, message: WorkflowInput("Input", message),
                                 on_response=lambda *args: calls.append("resumed"))
    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": operation})
    waiting = asyncio.run(runner.start(assignment.assignment_id))
    path = tmp_path / "assignments.json"
    journal = json.loads(path.read_text())
    journal["assignments"][0][change] = "b" * 64 if change == "revision" else "developer_two" if change == "agent_id" else "other"
    path.write_text(json.dumps(journal))
    before = Path(assignment.budget_path).read_bytes()
    with pytest.raises(PermissionError, match="pins"):
        asyncio.run(runner.resume(assignment.assignment_id, request_id=waiting.pending[0].request_id, response="ok"))
    assert not calls
    assert Path(assignment.budget_path).read_bytes() == before


def test_budget_locator_drift_cannot_create_replacement_ledger(tmp_path):
    operation = ManagedOperation(lambda context, message: WorkflowInput("Input", message), on_response=lambda *args: "done")
    runner, assignment, service, *_ = runner_fixture(tmp_path, {"definition_probe": operation})
    waiting = asyncio.run(runner.start(assignment.assignment_id))
    replacement = tmp_path / "replacement.json"
    service._budget_path_for = lambda preview_id: replacement
    with pytest.raises(ValueError, match="original"):
        asyncio.run(runner.resume(assignment.assignment_id, request_id=waiting.pending[0].request_id, response="ok"))
    assert not replacement.exists()
    assert FileRunStore(tmp_path / "runs").get(assignment.assignment_id).state == "finalizing"


def test_deadline_bounds_native_invocation_and_preserves_reservations(tmp_path):
    from time import time

    async def operation(context, message):
        await asyncio.Event().wait()

    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": ManagedOperation(operation)})
    path = Path(assignment.budget_path)
    budget = json.loads(path.read_text())
    budget["deadline"] = time() + 0.1
    budget["reserved_paths"] = ["src/one.py"]
    path.write_text(json.dumps(budget))
    failed = asyncio.run(runner.start(assignment.assignment_id))
    assert failed.state == "failed" and failed.error == "TimeoutError"
    assert json.loads(path.read_text()) == budget | {"aborted": True}


def test_scope_change_inside_executor_stops_the_next_node(tmp_path):
    calls = []

    def mutate(context, message):
        calls.append("first")
        path = tmp_path / "previews.json"
        data = json.loads(path.read_text())
        data["previews"][0]["bundle_payload"]["objective"] = "Changed mid-run"
        path.write_text(json.dumps(data))
        return message

    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": ManagedOperation(mutate)})
    failed = asyncio.run(runner.start(assignment.assignment_id))
    assert failed.state == "failed" and "scope" in failed.error
    assert calls == ["first"]


def test_partial_parallel_native_responses_keep_sibling_pending_after_restart(tmp_path):
    calls = []
    pause = ManagedOperation(lambda context, message: WorkflowInput("Input", message),
                             on_response=lambda context, original, value: calls.append(value) or value)
    runner, assignment, _, snapshot, *_ = runner_fixture(tmp_path)
    data = snapshot.definition.model_dump(mode="json")
    data["workflows"][0]["document"] = {
        "format": "python_graph", "start": "start",
        "nodes": [{"id": "start", "kind": "operation", "operation": "start"},
                  {"id": "left", "kind": "operation", "operation": "pause"},
                  {"id": "right", "kind": "operation", "operation": "pause"}],
        "edges": [{"kind": "fan_out", "source": "start", "targets": ["left", "right"]}],
        "outputs": ["left", "right"],
    }
    pinned = FileDefinitionStore(tmp_path / "definitions").save(parse_organization_definition(json.dumps(data)))
    path = tmp_path / "assignments.json"
    journal = json.loads(path.read_text())
    journal["assignments"][0]["revision"] = pinned.revision
    path.write_text(json.dumps(journal))
    runner._operations = {"start": ManagedOperation(lambda context, message: message), "pause": pause}
    waiting = asyncio.run(runner.start(assignment.assignment_id, input="two"))
    assert waiting.state == "waiting" and len(waiting.pending) == 2
    first, second = waiting.pending
    partial = asyncio.run(runner.resume(assignment.assignment_id, request_id=first.request_id, response="left"))
    assert partial.state == "waiting", partial.error
    assert [item.request_id for item in partial.pending] == [second.request_id]
    completed = asyncio.run(runner.resume(assignment.assignment_id, request_id=second.request_id, response="right"))
    assert completed.state == "completed", completed.error
    assert completed.outputs == ("left", "right")
    assert calls == ["left", "right"]


def test_finalization_after_expiry_records_failure_without_replaying(tmp_path, monkeypatch):
    calls = []
    runner, assignment, service, *_ = runner_fixture(tmp_path, {
        "definition_probe": ManagedOperation(lambda *args: calls.append("invoke") or "done"),
    })
    with monkeypatch.context() as patch:
        patch.setattr(service, "finish", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Interrupted receipt")))
        with pytest.raises(OSError):
            asyncio.run(runner.start(assignment.assignment_id))
    path = Path(assignment.budget_path)
    budget = json.loads(path.read_text())
    budget["deadline"] = 0
    path.write_text(json.dumps(budget))
    failed = asyncio.run(runner.start(assignment.assignment_id))
    assert failed.state == "failed" and "expired" in failed.error
    assert calls == ["invoke"]
    assert json.loads(path.read_text()) == budget | {"aborted": True}


def test_corrupt_run_receipt_is_not_replaced_or_executed(tmp_path):
    runner, assignment, *_ = runner_fixture(tmp_path)
    run = asyncio.run(runner.start(assignment.assignment_id))
    store = FileRunStore(tmp_path / "runs")
    path = store.checkpoint_path(assignment.assignment_id).parent / "run.json"
    path.write_text('{"state": "ready"}')
    with pytest.raises(ValueError):
        asyncio.run(runner.start(assignment.assignment_id))
    assert path.read_text() == '{"state": "ready"}'
    with pytest.raises(ValueError):
        store.save(run)


def test_cancellation_drains_threaded_stage_before_releasing_invocation_lock(tmp_path):
    from threading import Event
    from aitobuild.organization_delivery import _delivery_call

    calls = []

    async def exercise():
        started = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()

        def blocking():
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=5)
            calls.append("side effect completed")
            return "done"

        async def operation(context, message):
            return await _delivery_call(blocking)

        runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": ManagedOperation(operation)})
        task = asyncio.create_task(runner.start(assignment.assignment_id))
        await started.wait()
        task.cancel()
        with pytest.raises(LockTimeout):
            await runner.start(assignment.assignment_id)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        run = FileRunStore(tmp_path / "runs").get(assignment.assignment_id)
        assert run.state == "cancelled"
        assert await runner.start(assignment.assignment_id) == run

    asyncio.run(exercise())
    assert calls == ["side effect completed"]


def test_run_store_rejects_rewinding_and_interrupted_save_preserves_receipt(tmp_path, monkeypatch):
    operation = ManagedOperation(lambda context, message: WorkflowInput("Input", message), on_response=lambda *args: "done")
    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": operation})
    waiting = asyncio.run(runner.start(assignment.assignment_id))
    store = FileRunStore(tmp_path / "runs")
    with pytest.raises(ValueError, match="rewind"):
        store.save(ManagedRun.model_validate(waiting.model_dump() | {"state": "ready", "pending": ()}))
    running = ManagedRun.model_validate(waiting.model_dump() | {"state": "running", "pending": ()})
    with monkeypatch.context() as patch:
        patch.setattr("aitobuild.organization_runner.os.fsync", lambda descriptor: (_ for _ in ()).throw(OSError("Interrupted")))
        with pytest.raises(OSError, match="Interrupted"):
            store.save(running)
    assert store.get(assignment.assignment_id) == waiting


@pytest.mark.parametrize("active", [False, True])
def test_cancellation_preserves_budget_and_cleans_up(tmp_path, active):
    calls = []

    async def exercise():
        started = asyncio.Event()

        async def operation(context, message):
            started.set()
            if active:
                await asyncio.Event().wait()
            return WorkflowInput("Input", message)

        async def cleanup(context):
            calls.append("cleanup")

        runner, assignment, *_ = runner_fixture(
            tmp_path, {"definition_probe": ManagedOperation(operation, on_response=lambda *args: None)}, cleanup=cleanup,
        )
        budget = DeveloperTaskBudget(path=Path(assignment.budget_path), bundle=assignment.bundle, create=False)
        budget.reserve_paths(("src/one.py",))
        before = json.loads(Path(assignment.budget_path).read_text())
        if active:
            task = asyncio.create_task(runner.start(assignment.assignment_id))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            run = FileRunStore(tmp_path / "runs").get(assignment.assignment_id)
        else:
            await runner.start(assignment.assignment_id)
            run = await runner.cancel(assignment.assignment_id)
        assert run.state == "cancelled"
        assert await runner.start(assignment.assignment_id) == run
        assert json.loads(Path(assignment.budget_path).read_text()) == before | {"aborted": True}

    asyncio.run(exercise())
    assert calls == ["cleanup"]


def test_terminal_assignment_write_interruption_retries_metadata_only(tmp_path, monkeypatch):
    calls = []
    runner, assignment, service, *_ = runner_fixture(tmp_path, {
        "definition_probe": ManagedOperation(lambda context, message: calls.append("invoke") or "done"),
    })
    original = service.finish
    with monkeypatch.context() as patch:
        patch.setattr(service, "finish", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Interrupted receipt")))
        with pytest.raises(OSError, match="Interrupted receipt"):
            asyncio.run(runner.start(assignment.assignment_id))
    assert FileRunStore(tmp_path / "runs").get(assignment.assignment_id).state == "finalizing"
    completed = asyncio.run(runner.start(assignment.assignment_id))
    assert completed.state == "completed"
    assert calls == ["invoke"]
    assert original(assignment.assignment_id, state="completed",
                    outcome=f"Managed workflow completed: {completed.run_id} (metadata only)").state == "completed"


def test_cleanup_failure_is_terminal_and_never_qualifies_completion(tmp_path):
    async def cleanup(context):
        raise RuntimeError("Container retained")

    runner, assignment, *_ = runner_fixture(tmp_path, cleanup=cleanup)
    failed = asyncio.run(runner.start(assignment.assignment_id))
    assert failed.state == "failed" and failed.cleanup_succeeded is False
    assert "Container retained" in failed.error
    assert asyncio.run(runner.start(assignment.assignment_id)) == failed


@pytest.mark.parametrize("action", [ActionClass.ISSUE_WRITE, ActionClass.PR_REVIEW])
def test_managed_operations_cannot_widen_assigned_role(tmp_path, action):
    calls = []
    role = AgentRole.PM if action == ActionClass.ISSUE_WRITE else AgentRole.ARCHITECT
    runner, assignment, *_ = runner_fixture(tmp_path, {
        "definition_probe": ManagedOperation(lambda *args: calls.append("write"), role=role, action=action),
    })
    failed = asyncio.run(runner.start(assignment.assignment_id))
    assert failed.state == "failed" and "role" in failed.error
    assert not calls


def test_recovered_tool_errors_remain_diagnostics_not_contribution_rejection(tmp_path):
    runner, assignment, *_ = runner_fixture(tmp_path, {
        "definition_probe": ManagedOperation(lambda *args: {"completed": True, "recovered_errors": ["Stale exact span"]}),
    })
    completed = asyncio.run(runner.start(assignment.assignment_id))
    assert completed.state == "completed"
    assert completed.outputs[0]["recovered_errors"] == ["Stale exact span"]


@pytest.mark.parametrize("scenario", ["approved", "rejected", "recovered"])
@pytest.mark.parametrize("entrypoint", ["runner", "service", "worker"])
def test_actual_configured_native_agent_approval_restart_uses_existing_scoped_tools(
    tmp_path, approved_delivery, verification_adapter, scenario, entrypoint,
):
    previews, worker, preview_id, source, _ = approved_delivery
    worker.prepare(preview_id)
    document = json.loads((Path(__file__).resolve().parents[1] / "config/organization.example.json").read_text())
    document["workflows"][0]["document"] = {
        "format": "python_graph", "start": "implement",
        "nodes": [{"id": "implement", "kind": "operation", "operation": "delivery_implement"}],
        "outputs": ["implement"],
    }
    definitions = FileDefinitionStore(tmp_path / "definitions")
    snapshot = definitions.save(parse_organization_definition(json.dumps(document)))
    assignments = FileAssignmentStore(tmp_path / "assignments.json")
    service = AssignmentService(definitions=definitions, previews=previews, assignments=assignments,
                                budget_path_for=worker.budget_path)
    if entrypoint == "runner":
        assign(service, snapshot, previews.get(preview_id), **coordinator_proposal())
    bodies = []

    def model_reply(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1 and scenario == "recovered":
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "edit-probe", "type": "function", "function": {
                    "name": "developer_edit_file", "arguments": json.dumps({
                        "path": "README.md", "old_text": "missing exact span", "new_text": "bad", "approved": True,
                    }),
                },
            }]}
            finish = "tool_calls"
        elif len(bodies) == 1 or (len(bodies) == 2 and scenario == "recovered"):
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "write-probe", "type": "function", "function": {
                    "name": "developer_write_file", "arguments": json.dumps({
                        "path": "src/probe.py", "content": "def probe():\n    return True\n", "approved": True,
                    }),
                },
            }]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Implemented; independent verification remains required"}
            finish = "stop"
        return httpx.Response(200, json={
            "id": "reply", "object": "chat.completion", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        })

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(model_reply)) as transport:
            async with AsyncOpenAI(api_key="test-key", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))

                def runtime_for(pinned):
                    return bootstrap_organization(
                        pinned, model_profiles={"default": profile}, default_model_profile="default",
                        state_dir=tmp_path / "agents",
                    )

                native = NativeDeliveryImplementation(
                    worker=worker, runtime_for=runtime_for, state_dir=tmp_path / "native",
                    tools=DeveloperToolContext(
                        bash_adapter=verification_adapter, filesystem_adapter=MockFilesystemAdapter(),
                        container_session_adapter=verification_adapter, workspace_root=Path.cwd(),
                        require_human_approval_for_repo_writes=True,
                    ),
                )
                bindings = ManagedDeliveryBindings(worker=worker, implementation=native, verification_adapter=verification_adapter)

                def make_runner():
                    return ManagedWorkflowRunner(
                        definitions=definitions, assignments=assignments, assignment_service=service,
                        runs=FileRunStore(tmp_path / "runs"), actor_provider=lambda: RuntimeActor("operator", "operator"),
                        operations=bindings.operations, cleanup=bindings.cleanup, binding_revision="native-v1",
                    )

                def make_service():
                    from aitobuild.organization_service import ManagedOrganizationService, ManagedRoute

                    async def coordinator(snapshot, preview):
                        return coordinator_proposal()["proposal"]

                    return ManagedOrganizationService(
                        definitions=definitions, assignments=assignments, runs=FileRunStore(tmp_path / "runs"),
                        previews=previews, worker=worker, operator_id="service-operator",
                        routes=(ManagedRoute(repository="fixture/widgets", repository_id=101,
                                             organization_id=snapshot.organization_id, revision=snapshot.revision,
                                             event="github.issue.ready"),), coordinators={"planner": coordinator},
                        operations=bindings.operations, cleanup=bindings.cleanup, binding_revision="native-v1",
                    )

                async def detached_invoke(assignment_id=None, *, request_id=None, approved=None):
                    from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker

                    managed = make_service()
                    detached = ManagedOrganizationWorker(service=managed, store=FileWorkerStore(tmp_path / "worker.json"))
                    if assignment_id is None:
                        detached.enqueue(preview_id)
                    else:
                        detached.enqueue_decision(assignment_id, request_id=request_id, kind="approve", value=approved)
                    await detached.start()
                    try:
                        async with asyncio.timeout(5):
                            job = await detached.wait_idle(preview_id)
                        return managed.recorded_run(job.task_id)
                    finally:
                        await detached.close()

                budget_path = worker.budget_path(preview_id)
                before = json.loads(budget_path.read_text())
                if entrypoint == "service":
                    waiting = await make_service().consume(preview_id)
                elif entrypoint == "worker":
                    waiting = await detached_invoke()
                else:
                    assignment = assignments.for_task(previews.get(preview_id).bundle_payload["task_id"])
                    waiting = await make_runner().start(assignment.assignment_id)
                assignment = assignments.for_task(previews.get(preview_id).bundle_payload["task_id"])
                assert waiting.state == "waiting", waiting.error
                assert worker.get(preview_id).state == "awaiting_tool_approval"
                assert waiting.pending[0].kind == "service_approval"
                assert not (Path(worker.get(preview_id).checkout_path) / "src/probe.py").exists()
                approve = (detached_invoke if entrypoint == "worker" else
                           make_service().decide if entrypoint == "service" else make_runner().approve)
                completed = await approve(assignment.assignment_id, request_id=waiting.pending[0].request_id,
                                          approved=scenario != "rejected")
                if scenario == "rejected":
                    assert completed.state == "failed" and "rejected" in completed.error
                    assert worker.get(preview_id).state == "failed"
                    assert len(bodies) == 1
                    assert json.loads(Path(assignment.budget_path).read_text()) == before | {"aborted": True}
                    assert not (Path(worker.get(preview_id).checkout_path) / "src/probe.py").exists()
                    return
                if scenario == "recovered":
                    assert completed.state == "waiting", completed.error
                    approve = (detached_invoke if entrypoint == "worker" else
                               make_service().decide if entrypoint == "service" else make_runner().approve)
                    completed = await approve(assignment.assignment_id, request_id=completed.pending[0].request_id,
                                              approved=True)
                assert completed.state == "completed", completed.error
                record = worker.get(preview_id)
                assert record.state == "implemented" and record.verification is None and record.publication is None
                assert (Path(record.checkout_path) / "src/probe.py").read_text() == "def probe():\n    return True\n"
                assert not (source / "src/probe.py").exists()
                assert len(bodies) == (3 if scenario == "recovered" else 2)
                assert "only from the provided isolation task" in bodies[0]["messages"][0]["content"]
                after = json.loads(Path(assignment.budget_path).read_text())
                assert after["deadline"] == before["deadline"]
                assert "src/probe.py" in after["reserved_paths"] and not after.get("aborted", False)
                session = await FileSessionStore(tmp_path / "native" / "sessions").get(completed.session_id)
                assert session.state["aitobuild_pending_approvals"] == []
                assert session.state["aitobuild_managed_cleanup_succeeded"] is True
                if scenario == "recovered":
                    assert session.state["aitobuild_managed_diagnostics"]
                    assert (Path(record.checkout_path) / "README.md").read_text() == "Fixture baseline\n"

    asyncio.run(exercise())