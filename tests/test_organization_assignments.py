from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier

import pytest

from aitobuild.developer_isolation import (
    DeveloperIssueContext, DeveloperTaskBudget, build_developer_task_bundle,
    developer_task_bundle_from_payload,
)
from aitobuild.developer_preview import DeveloperPreviewRegistry
from aitobuild.organization import FileDefinitionStore, parse_organization_definition
from aitobuild.organization_assignments import (
    AssignmentProposal, AssignmentService, FileAssignmentStore, TaskAssignment,
)


EXAMPLE = Path(__file__).resolve().parents[1] / "config/organization.example.json"


def record(tmp_path, task="task-one", agent="developer_one", revision="a" * 64, capacity=1):
    bundle = build_developer_task_bundle(
        task_id=task, objective="A scoped task", acceptance_criteria=["Tests pass"],
        constraints=[], context_files=[],
    )
    content = json.dumps(bundle.to_payload(), sort_keys=True, separators=(",", ":"))
    now = datetime.now(tz=UTC)
    return TaskAssignment(
        assignment_id="assignment-" + task, organization_id="product", revision=revision,
        route_id="issue_intake", event="github.issue.ready", team_id="product", workflow_id="definition_probe",
        strategy="coordinator", eligible_agents=("developer_one", "developer_two"), agent_id=agent,
        agent_capacity=capacity, actor_id="planner", rationale="Selected for this task",
        preview_id="preview-" + task, task_id=task, bundle_content=content,
        scope_digest=sha256(content.encode()).hexdigest(), budget_path=str(tmp_path / (task + ".json")),
        approved_at=now, created_at=now,
    )


def test_assignment_roundtrip_idempotency_and_terminal_receipt(tmp_path):
    path = tmp_path / "assignments.json"
    store = FileAssignmentStore(path)
    first = record(tmp_path)
    assert store.claim(first) == first
    assert FileAssignmentStore(path).claim(first) == first
    assert store.get(first.assignment_id) == first
    assert store.for_task(first.task_id) == first
    completed = store.finish(first.assignment_id, state="completed", outcome="Recorded completion")
    assert completed.state == "completed"
    assert store.claim(first) == completed
    assert store.finish(first.assignment_id, state="completed", outcome="Recorded completion") == completed
    with pytest.raises(ValueError, match="Terminal"):
        store.finish(first.assignment_id, state="failed", outcome="Different decision")
    assert store.claim(record(tmp_path, task="task-two")).state == "claimed"


def test_owner_and_capacity_are_atomic_across_instances_and_revisions(tmp_path):
    path = tmp_path / "assignments.json"
    candidates = [record(tmp_path, task=f"task-{index}", revision=str(index + 1) * 64) for index in range(2)]

    def claim(candidate):
        try:
            return FileAssignmentStore(path).claim(candidate)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, candidates))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    with pytest.raises(ValueError, match="owner"):
        FileAssignmentStore(path).claim(winner.model_copy(update={"agent_id": "developer_two"}))


def test_interrupted_journal_write_keeps_existing_owner_and_can_retry(tmp_path, monkeypatch):
    store = FileAssignmentStore(tmp_path / "assignments.json")
    first = store.claim(record(tmp_path))
    second = record(tmp_path, task="task-two", agent="developer_two")
    with monkeypatch.context() as patch:
        patch.setattr("aitobuild.organization_assignments.os.fsync", lambda _: (_ for _ in ()).throw(OSError("Interrupted")))
        with pytest.raises(OSError, match="Interrupted"):
            store.claim(second)
    assert store.get(first.assignment_id) == first
    assert store.for_task(second.task_id) is None
    assert store.claim(second) == second


def setup_service(tmp_path, strategy="coordinator", capacity=1):
    definition = json.loads(EXAMPLE.read_text())
    definition["routes"][0]["delegation"]["strategy"] = strategy
    if strategy == "rules":
        definition["routes"][0]["delegation"]["target_agent"] = "developer_two"
    definition["agents"][1]["max_concurrent_runs"] = capacity
    definitions = FileDefinitionStore(tmp_path / "definitions")
    snapshot = definitions.save(parse_organization_definition(json.dumps(definition)))
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    assignments = FileAssignmentStore(tmp_path / "assignments.json")
    service = AssignmentService(
        definitions=definitions, previews=previews, assignments=assignments,
        budget_path_for=lambda preview_id: tmp_path / "budgets" / (preview_id + ".json"),
    )
    return service, snapshot, previews, assignments


def approved_task(tmp_path, previews, task="task-one", approve=True, budget=True):
    bundle = replace(build_developer_task_bundle(
        task_id=task, objective="A scoped issue task", acceptance_criteria=["Tests pass"],
        constraints=[], context_files=[],
    ), issue_context=DeveloperIssueContext(
        repository="example/target", repository_id=1, issue_number=1, issue_id=2,
        title="Task", body="Approved criteria", base_branch="main",
    ))
    preview = previews.create_or_get(
        dedupe_key="delivery-" + task, bundle_payload=bundle.to_payload(), source_payload={}, task_key=task,
    )
    if approve:
        preview = previews.approve(preview.preview_id, base_revision="a" * 40)
    if budget:
        DeveloperTaskBudget(path=tmp_path / "budgets" / (preview.preview_id + ".json"),
                            bundle=developer_task_bundle_from_payload(preview.bundle_payload))
    return preview


def assign(service, snapshot, preview, **kwargs):
    return service.assign(
        organization_id=snapshot.organization_id, revision=snapshot.revision,
        event="github.issue.ready", preview_id=preview.preview_id, **kwargs,
    )


def coordinator_proposal(agent="developer_one"):
    return {"proposal": AssignmentProposal(agent_id=agent, rationale="Selected for this task"), "coordinator_id": "planner"}


def test_managed_revalidation_rejects_changed_approval_without_resetting_budget(tmp_path):
    service, snapshot, previews, _ = setup_service(tmp_path)
    preview = approved_task(tmp_path, previews)
    first = assign(service, snapshot, preview, **coordinator_proposal())
    assert service.revalidate(first.assignment_id) == first
    path = Path(first.budget_path)
    before = path.read_bytes()
    payload = json.loads((tmp_path / "previews.json").read_text())
    payload["previews"][0]["bundle_payload"]["objective"] = "Changed scope"
    (tmp_path / "previews.json").write_text(json.dumps(payload))
    with pytest.raises(PermissionError, match="scope"):
        service.revalidate(first.assignment_id)
    assert path.read_bytes() == before


@pytest.mark.parametrize("strategy", ["coordinator", "rules", "human"])
def test_configured_strategies_pin_approval_revision_scope_and_original_budget(tmp_path, strategy):
    service, snapshot, previews, assignments = setup_service(tmp_path, strategy)
    preview = approved_task(tmp_path, previews)
    decision = coordinator_proposal() if strategy == "coordinator" else {
        "proposal": AssignmentProposal(agent_id="developer_one", rationale="Operator decision"), "human_id": "operator",
    } if strategy == "human" else {}
    path = tmp_path / "budgets" / (preview.preview_id + ".json")
    before = path.read_bytes()
    first = assign(service, snapshot, preview, **decision)
    assert first.agent_id == ("developer_two" if strategy == "rules" else "developer_one")
    assert first.revision == snapshot.revision
    assert first.bundle.issue_context.base_revision == "a" * 40
    assert json.loads(first.bundle_content) == preview.bundle_payload
    assert first.approved_at == preview.approved_at
    assert assign(service, snapshot, preview, **decision) == first
    assert path.read_bytes() == before
    restarted, _, _, _ = setup_service(tmp_path, strategy)
    assert assign(restarted, snapshot, preview, **decision) == first
    assert assignments.for_task(first.task_id) == first


@pytest.mark.parametrize("case", [
    "pending", "missing_preview", "missing_revision", "event", "ineligible", "coordinator", "missing_proposal",
    "wrong_authority", "missing_budget", "aborted", "expired", "scope_drift", "base_drift",
])
def test_invalid_assignment_does_not_claim_or_reset_a_budget(tmp_path, monkeypatch, case):
    clock = [1000.0]
    monkeypatch.setattr("aitobuild.developer_isolation.time", lambda: clock[0])
    service, snapshot, previews, assignments = setup_service(tmp_path)
    preview = approved_task(tmp_path, previews, approve=case != "pending", budget=case != "missing_budget")
    path = tmp_path / "budgets" / (preview.preview_id + ".json")
    decision = coordinator_proposal()
    if case == "ineligible":
        decision = coordinator_proposal("planner")
    elif case == "coordinator":
        decision["coordinator_id"] = "developer_two"
    elif case == "missing_proposal":
        decision.pop("proposal")
    elif case == "wrong_authority":
        decision["human_id"] = "operator"
    elif case == "aborted":
        DeveloperTaskBudget(path=path, bundle=developer_task_bundle_from_payload(preview.bundle_payload), create=False).abort()
    elif case == "expired":
        clock[0] += 1900
    elif case in {"scope_drift", "base_drift"}:
        data = json.loads((tmp_path / "previews.json").read_text())
        if case == "scope_drift":
            data["previews"][0]["bundle_payload"]["objective"] = "Unapproved replacement"
        else:
            data["previews"][0]["bundle_payload"]["issue_context"]["base_revision"] = "b" * 40
        (tmp_path / "previews.json").write_text(json.dumps(data))
    before = path.read_bytes() if path.exists() else None
    with pytest.raises((ValueError, PermissionError, TimeoutError)):
        service.assign(
            organization_id="product", revision="f" * 64 if case == "missing_revision" else snapshot.revision,
            event="unknown.event" if case == "event" else "github.issue.ready",
            preview_id="missing" if case == "missing_preview" else preview.preview_id, **decision,
        )
    assert assignments.for_task("task-one") is None
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("strategy,decision", [
    ("rules", coordinator_proposal()), ("rules", {"human_id": "operator"}),
    ("human", coordinator_proposal()), ("human", {"proposal": AssignmentProposal(agent_id="developer_one", rationale="Decision")}),
    ("human", {"proposal": AssignmentProposal(agent_id="developer_one", rationale="Decision"), "human_id": " "}),
])
def test_strategy_authority_cannot_be_overridden(tmp_path, strategy, decision):
    service, snapshot, previews, assignments = setup_service(tmp_path, strategy)
    preview = approved_task(tmp_path, previews)
    with pytest.raises((ValueError, PermissionError)):
        assign(service, snapshot, preview, **decision)
    assert assignments.for_task("task-one") is None


def test_changed_revision_cannot_reassign_or_reset_cross_revision_capacity(tmp_path):
    service, snapshot, previews, assignments = setup_service(tmp_path)
    first_preview = approved_task(tmp_path, previews)
    first = assign(service, snapshot, first_preview, **coordinator_proposal())
    modified = snapshot.definition
    data = modified.model_dump(mode="json")
    data["agents"][0]["instructions"] = "Updated coordinator guidance"
    data["agents"][1]["max_concurrent_runs"] = 2
    updated = FileDefinitionStore(tmp_path / "definitions").save(parse_organization_definition(json.dumps(data)))
    with pytest.raises(ValueError, match="owner"):
        assign(service, updated, first_preview, **coordinator_proposal())
    second_preview = approved_task(tmp_path, previews, "task-two")
    with pytest.raises(ValueError, match="capacity"):
        assign(service, updated, second_preview, **coordinator_proposal())
    service.finish(first.assignment_id, state="completed", outcome="Operator recorded completion")
    assert assign(service, updated, second_preview, **coordinator_proposal()).revision == updated.revision
    assert assignments.get(first.assignment_id).revision == snapshot.revision


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_terminal_failures_abort_original_budget_and_release_only_other_tasks(tmp_path, state):
    service, snapshot, previews, assignments = setup_service(tmp_path)
    preview = approved_task(tmp_path, previews)
    first = assign(service, snapshot, preview, **coordinator_proposal())
    budget = DeveloperTaskBudget(path=Path(first.budget_path), bundle=first.bundle, create=False)
    budget.reserve_paths(("src/one.py",))
    before = json.loads(Path(first.budget_path).read_text())
    closed = service.finish(first.assignment_id, state=state, outcome="Operator stopped task")
    after = json.loads(Path(first.budget_path).read_text())
    assert closed.state == state
    assert after == before | {"aborted": True}
    assert service.finish(first.assignment_id, state=state, outcome="Operator stopped task") == closed
    with pytest.raises(TimeoutError, match="aborted"):
        assign(service, snapshot, preview, **coordinator_proposal())
    other = approved_task(tmp_path, previews, "task-two")
    assert assign(service, snapshot, other, **coordinator_proposal()).state == "claimed"
    assert assignments.for_task(first.task_id) == closed


def process_claim(path, candidate):
    try:
        return FileAssignmentStore(Path(path)).claim(TaskAssignment.model_validate(candidate)).assignment_id
    except ValueError:
        return None


def test_processes_cannot_claim_the_same_agent_capacity(tmp_path):
    path = tmp_path / "assignments.json"
    candidates = [record(tmp_path, task=f"task-{index}").model_dump() for index in range(2)]
    with ProcessPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(process_claim, [str(path)] * 2, candidates))
    assert sum(result is not None for result in results) == 1


def test_conflicting_terminal_decisions_keep_budget_and_record_consistent(tmp_path):
    service, snapshot, previews, assignments = setup_service(tmp_path)
    first = assign(service, snapshot, approved_task(tmp_path, previews), **coordinator_proposal())
    barrier = Barrier(2)

    def finish(state):
        barrier.wait()
        try:
            return service.finish(first.assignment_id, state=state, outcome="Operator decision")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(finish, ["completed", "failed"]))
    assert sum(result is not None for result in results) == 1
    terminal = assignments.get(first.assignment_id)
    assert json.loads(Path(first.budget_path).read_text()).get("aborted", False) == (terminal.state == "failed")


@pytest.mark.parametrize("case", [
    "version", "missing_version", "missing_assignments", "identity", "digest", "capacity", "terminal", "duplicate", "over_capacity",
])
def test_corrupt_journal_is_rejected_without_repair(tmp_path, case):
    path = tmp_path / "assignments.json"
    store = FileAssignmentStore(path)
    original = store.claim(record(tmp_path))
    data = json.loads(path.read_text())
    saved = data["assignments"][0]
    if case == "version":
        data["schema_version"] = True
    elif case == "missing_version":
        data.pop("schema_version")
    elif case == "missing_assignments":
        data.pop("assignments")
    elif case == "identity":
        saved["task_id"] = "other-task"
    elif case == "digest":
        saved["scope_digest"] = "0" * 64
    elif case == "capacity":
        saved["agent_capacity"] = True
    elif case == "terminal":
        saved["state"] = "completed"
    elif case == "duplicate":
        data["assignments"].append(saved)
    else:
        data["assignments"].append(record(tmp_path, task="task-two").model_dump(mode="json"))
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        FileAssignmentStore(path).get(original.assignment_id)
    with pytest.raises(ValueError):
        store.claim(original)
    assert path.read_bytes() == before


def test_interrupted_terminal_write_preserves_abort_and_safe_retry(tmp_path, monkeypatch):
    service, snapshot, previews, assignments = setup_service(tmp_path)
    first = assign(service, snapshot, approved_task(tmp_path, previews), **coordinator_proposal())
    with monkeypatch.context() as patch:
        patch.setattr("aitobuild.organization_assignments.os.fsync", lambda _: (_ for _ in ()).throw(OSError("Interrupted")))
        with pytest.raises(OSError, match="Interrupted"):
            service.finish(first.assignment_id, state="failed", outcome="Operator stopped task")
    assert assignments.get(first.assignment_id).state == "claimed"
    assert json.loads(Path(first.budget_path).read_text())["aborted"] is True
    assert service.finish(first.assignment_id, state="failed", outcome="Operator stopped task").state == "failed"


def test_invalid_or_expired_completion_cannot_release_a_claim(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("aitobuild.developer_isolation.time", lambda: clock[0])
    service, snapshot, previews, assignments = setup_service(tmp_path)
    first = assign(service, snapshot, approved_task(tmp_path, previews), **coordinator_proposal())
    before = Path(first.budget_path).read_bytes()
    with pytest.raises(ValueError):
        service.finish(first.assignment_id, state="failed", outcome=" ")
    assert Path(first.budget_path).read_bytes() == before
    clock[0] += 1900
    with pytest.raises(TimeoutError, match="expired"):
        service.finish(first.assignment_id, state="completed", outcome="Operator completion")
    assert assignments.get(first.assignment_id) == first
    assert service.finish(first.assignment_id, state="cancelled", outcome="Expired task stopped").state == "cancelled"