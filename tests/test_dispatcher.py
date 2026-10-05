from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import os
from typing import Any
import subprocess
from uuid import uuid4

import pytest

from aitobuild.developer_isolation import developer_task_bundle_from_payload
from aitobuild.developer_execution import DeveloperExecutionEngine, PlannedFileWrite
from aitobuild.developer_delivery import DeveloperDeliveryWorker, LocalRepositorySource
import aitobuild.developer_delivery as delivery_module
from aitobuild.tools.bash import ContainerSessionBashAdapter
from aitobuild.developer_preview import DeveloperPreviewRegistry
from aitobuild.dispatcher import DispatcherAgent
from aitobuild.events import EventOrigin, EventType, make_internal_event
from aitobuild.triggers import InMemoryDedupeStore, TriggerEngine


def test_dispatcher_routes_webhook_events() -> None:
    dispatcher = DispatcherAgent()
    event = make_internal_event(
        origin=EventOrigin.GITHUB_WEBHOOK,
        event_type=EventType.WEBHOOK_EVENT_RECEIVED,
        payload={
            "github_event": "issues",
            "action": "opened",
            "body": {"issue": {"number": 11}},
        },
    )

    result = dispatcher.route(event)
    assert result.accepted is True
    assert result.route == "developer.async.webhook"
    assert result.metadata is not None
    bundle = result.metadata["developer_task_bundle"]
    assert bundle["task_id"].startswith("WEBHOOK-")
    assert bundle["objective"].startswith("Handle GitHub webhook")


def test_dispatcher_routes_meeting_due() -> None:
    dispatcher = DispatcherAgent()
    request_event = make_internal_event(
        origin=EventOrigin.MANUAL_REQUEST,
        event_type=EventType.MEETING_REQUESTED,
        payload={"agenda": "Design review", "participants": ["Architect", "Developer"]},
    )

    request_result = dispatcher.route(request_event)
    assert request_result.accepted is True
    assert request_result.route == "meeting.requested"
    meeting_id = request_result.metadata["meeting_id"] if request_result.metadata else None
    assert isinstance(meeting_id, str)

    event = make_internal_event(
        origin=EventOrigin.SCHEDULER,
        event_type=EventType.MEETING_DUE,
        payload={"meeting_id": meeting_id},
    )

    result = dispatcher.route(event)
    assert result.accepted is True
    assert result.route == "meeting.bootstrap"


def test_dispatcher_meeting_due_without_meeting_id_is_pending() -> None:
    dispatcher = DispatcherAgent()
    event = make_internal_event(
        origin=EventOrigin.SCHEDULER,
        event_type=EventType.MEETING_DUE,
        payload={},
    )

    result = dispatcher.route(event)
    assert result.accepted is False
    assert result.route == "meeting.pending"


def test_dispatcher_meeting_due_with_expired_deadline_escalates() -> None:
    dispatcher = DispatcherAgent()
    expired = (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()
    request_event = make_internal_event(
        origin=EventOrigin.MANUAL_REQUEST,
        event_type=EventType.MEETING_REQUESTED,
        payload={
            "agenda": "Urgent architecture decision",
            "participants": ["Architect", "Developer"],
            "deadline": expired,
        },
    )
    requested = dispatcher.route(request_event)
    meeting_id = requested.metadata["meeting_id"] if requested.metadata else ""

    due_event = make_internal_event(
        origin=EventOrigin.SCHEDULER,
        event_type=EventType.MEETING_DUE,
        payload={"meeting_id": meeting_id},
    )
    result = dispatcher.route(due_event)

    assert result.accepted is True
    assert result.route == "escalation.route"
    assert result.metadata is not None
    assert isinstance(result.metadata.get("escalation_id"), str)


def test_dispatcher_rejects_unsupported_combo() -> None:
    dispatcher = DispatcherAgent()
    event = make_internal_event(
        origin=EventOrigin.SYSTEM_SIGNAL,
        event_type=EventType.WEBHOOK_EVENT_RECEIVED,
        payload={},
    )

    result = dispatcher.route(event)
    assert result.accepted is False
    assert result.route == "unsupported"


def test_dispatcher_requires_preview_when_enabled() -> None:
    dispatcher = DispatcherAgent(require_developer_preview=True)
    event = make_internal_event(
        origin=EventOrigin.GITHUB_WEBHOOK,
        event_type=EventType.WEBHOOK_EVENT_RECEIVED,
        payload={
            "github_event": "issues",
            "action": "opened",
            "body": {"issue": {"number": 22}},
        },
        dedupe_key="github:d-22",
    )

    result = dispatcher.route(event)
    assert result.accepted is False
    assert result.route == "developer.preview_required"
    assert result.metadata is not None
    assert isinstance(result.metadata.get("preview_id"), str)


def test_dispatcher_accepts_webhook_after_preview_approval() -> None:
    dispatcher = DispatcherAgent(require_developer_preview=True)
    preview = dispatcher.create_developer_preview(
        dedupe_key="github:d-33",
        github_event="issues",
        action="opened",
        body={"issue": {"number": 33}},
    )
    dispatcher.approve_developer_preview(preview.preview_id)

    event = make_internal_event(
        origin=EventOrigin.GITHUB_WEBHOOK,
        event_type=EventType.WEBHOOK_EVENT_RECEIVED,
        payload={
            "github_event": "issues",
            "action": "opened",
            "body": {"issue": {"number": 33}},
        },
        dedupe_key="github:d-33",
    )

    result = dispatcher.route(event)
    assert result.accepted is True
    assert result.route == "developer.async.webhook"
    assert result.metadata is not None
    assert result.metadata["developer_task_bundle"] == preview.bundle_payload


def _issue_event(body: dict[str, Any], delivery: str = "issue-1", action: str = "opened"):
    return make_internal_event(
        origin=EventOrigin.GITHUB_WEBHOOK, event_type=EventType.WEBHOOK_EVENT_RECEIVED,
        payload={"github_event": "issues", "action": action, "body": body},
        dedupe_key=f"github:{delivery}",
    )


@pytest.mark.parametrize("action", ["opened", "assigned", "edited"])
def test_repository_issue_extracts_target_scope_and_requires_approval(repository_issue_body, action) -> None:
    result = DispatcherAgent(require_developer_preview=False).route(_issue_event(repository_issue_body, action=action))
    assert result.route == "developer.preview_required"
    assert not result.accepted
    bundle = result.metadata["developer_task_bundle"]
    assert bundle["task_id"].startswith("ISSUE-")
    assert bundle["objective"] == "Handle empty widget names"
    assert bundle["acceptance_criteria"] == ["Empty names are rejected.", "Existing names still work."]
    assert bundle["context_files"] == []
    context = bundle["issue_context"]
    assert context == {
        "repository": "fixture/widgets", "repository_id": 101, "issue_number": 7,
        "issue_id": 202, "title": repository_issue_body["issue"]["title"],
        "body": repository_issue_body["issue"]["body"], "base_branch": "main", "base_revision": None,
    }
    assert result.metadata["task_state"] == "awaiting_approval"
    assert developer_task_bundle_from_payload(bundle).to_payload()["issue_context"] == context


@pytest.mark.parametrize("revision", [None, "main", "abc", "g" * 40])
def test_repository_issue_approval_requires_resolved_base(repository_issue_body, revision) -> None:
    dispatcher = DispatcherAgent()
    preview = dispatcher.create_developer_preview(
        dedupe_key="github:base", github_event="issues", action="opened", body=repository_issue_body,
    )
    with pytest.raises(ValueError, match="base commit SHA"):
        dispatcher.approve_developer_preview(preview.preview_id, base_revision=revision)
    assert not dispatcher.developer_preview_registry.get(preview.preview_id).approved


def test_repository_issue_approval_and_dispatch_survive_restart(tmp_path, repository_issue_body) -> None:
    path = tmp_path / "previews.json"
    dispatcher = DispatcherAgent(developer_preview_registry=DeveloperPreviewRegistry(path))
    engine = TriggerEngine(dedupe_store=InMemoryDedupeStore())
    event = _issue_event(repository_issue_body)
    pending = engine.dispatch(event, dispatcher=dispatcher)
    assert engine.dispatch(event, dispatcher=dispatcher).metadata == pending.metadata
    preview_id = pending.metadata["preview_id"]
    restarted = DispatcherAgent(developer_preview_registry=DeveloperPreviewRegistry(path))
    approved = restarted.approve_developer_preview(preview_id, base_revision="A" * 40)
    assert approved.bundle_payload["issue_context"]["base_revision"] == "a" * 40
    with pytest.raises(ValueError, match="immutable"):
        restarted.approve_developer_preview(preview_id, base_revision="b" * 40)
    restarted = DispatcherAgent(developer_preview_registry=DeveloperPreviewRegistry(path))
    result = engine.dispatch(event, dispatcher=restarted)
    assert result.accepted
    assert result.metadata["developer_task_bundle"] == approved.bundle_payload
    assert result.metadata["task_state"] == "dispatched"
    restarted = DispatcherAgent(developer_preview_registry=DeveloperPreviewRegistry(path))
    for delivery in ("issue-1", "issue-2"):
        duplicate = TriggerEngine(dedupe_store=InMemoryDedupeStore()).dispatch(
            _issue_event(repository_issue_body, delivery), dispatcher=restarted,
        )
        assert duplicate.route == "dedupe"
        assert duplicate.metadata["preview_id"] == preview_id
        assert duplicate.metadata["developer_task_bundle"] == approved.bundle_payload
    assert len(restarted.list_developer_previews(pending_only=False, limit=50)) == 1


def test_repository_issue_scope_changes_need_new_approval(tmp_path, repository_issue_body) -> None:
    dispatcher = DispatcherAgent(developer_preview_registry=DeveloperPreviewRegistry(tmp_path / "previews.json"))
    original = dispatcher.create_developer_preview(
        dedupe_key="github:original", github_event="issues", action="opened", body=repository_issue_body,
    )
    approved = dispatcher.approve_developer_preview(original.preview_id, base_revision="a" * 40)
    changed = deepcopy(repository_issue_body)
    changed["issue"]["body"] += "\nAdditional scope."
    collision = dispatcher.route(_issue_event(changed, "original", "edited"))
    assert collision.route == "developer.invalid"
    updated = dispatcher.route(_issue_event(changed, "changed", "edited"))
    assert updated.route == "developer.preview_required"
    assert updated.metadata["preview_id"] != original.preview_id
    assert updated.metadata["task_id"] != original.bundle_payload["task_id"]
    assert dispatcher.developer_preview_registry.get(original.preview_id) == approved


def test_preview_scope_cannot_be_mutated_by_callers(tmp_path, repository_issue_body) -> None:
    registry = DeveloperPreviewRegistry(tmp_path / "previews.json")
    dispatcher = DispatcherAgent(developer_preview_registry=registry)
    preview = dispatcher.create_developer_preview(
        dedupe_key="github:immutable", github_event="issues", action="opened", body=repository_issue_body,
    )
    expected = deepcopy(preview.bundle_payload)
    preview.bundle_payload["policy"]["allowed_paths"].append("secrets/")
    repository_issue_body["issue"]["title"] = "Mutated"
    assert registry.get(preview.preview_id).bundle_payload == expected
    approved = registry.approve(preview.preview_id, base_revision="a" * 40)
    approved.bundle_payload["objective"] = "Mutated"
    assert registry.get(preview.preview_id).bundle_payload["objective"] == expected["objective"]


def test_repository_issue_deduplication_is_locked_across_registries(tmp_path: Path, repository_issue_body) -> None:
    path = tmp_path / "previews.json"
    dispatchers = [DispatcherAgent(developer_preview_registry=DeveloperPreviewRegistry(path)) for _ in range(2)]

    def create_preview(index: int):
        return dispatchers[index % 2].create_developer_preview(
            dedupe_key=f"github:parallel-{index}", github_event="issues", action="opened", body=repository_issue_body,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        previews = list(pool.map(create_preview, range(8)))
    assert len({preview.preview_id for preview in previews}) == 1
    preview_id = previews[0].preview_id
    dispatchers[0].approve_developer_preview(preview_id, base_revision="a" * 40)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda index: dispatchers[index].route(_issue_event(repository_issue_body, f"dispatch-{index}")), range(2)))
    assert sum(result.accepted for result in results) == 1
    assert {result.route for result in results} == {"developer.async.webhook", "dedupe"}
    assert all(result.metadata["task_state"] == "dispatched" for result in results)
    assert len(dispatchers[0].list_developer_previews(pending_only=False, limit=50)) == 1


@pytest.mark.parametrize("mutation", ["repository_id", "issue_id", "number", "title", "criteria", "closed", "pull_request", "assignee"])
def test_repository_issue_rejects_incomplete_or_unsupported_context(repository_issue_body, mutation) -> None:
    if mutation == "repository_id":
        repository_issue_body["repository"]["id"] = True
    elif mutation == "criteria":
        repository_issue_body["issue"]["body"] = "No criteria"
    elif mutation == "closed":
        repository_issue_body["issue"]["state"] = "closed"
    elif mutation == "pull_request":
        repository_issue_body["issue"]["pull_request"] = {}
    elif mutation == "assignee":
        repository_issue_body.pop("assignee")
    else:
        repository_issue_body["issue"]["id" if mutation == "issue_id" else mutation] = None
    result = DispatcherAgent().route(_issue_event(repository_issue_body, action="assigned"))
    assert not result.accepted
    assert result.route == "developer.invalid"
    assert result.metadata is None


@pytest.mark.parametrize("corruption", ["approval", "base", "index", "version"])
def test_persisted_issue_state_fails_closed(tmp_path, repository_issue_body, corruption) -> None:
    path = tmp_path / "previews.json"
    dispatcher = DispatcherAgent(developer_preview_registry=DeveloperPreviewRegistry(path))
    preview = dispatcher.create_developer_preview(
        dedupe_key="github:corrupt", github_event="issues", action="opened", body=repository_issue_body,
    )
    dispatcher.approve_developer_preview(preview.preview_id, base_revision="a" * 40)
    data = json.loads(path.read_text())
    if corruption == "approval":
        data["previews"][0]["approved"] = "false"
    elif corruption == "base":
        data["previews"][0]["bundle_payload"]["issue_context"]["base_revision"] = None
    elif corruption == "index":
        data["tasks"]["unknown"] = "missing"
    else:
        data["version"] = 999
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        DeveloperPreviewRegistry(path).get(preview.preview_id)
    assert json.loads(path.read_text()) == data


def test_execution_harness_cannot_execute_issue_bundle(tmp_path, repository_issue_body) -> None:
    dispatcher = DispatcherAgent()
    preview = dispatcher.create_developer_preview(
        dedupe_key="github:no-execution", github_event="issues", action="opened", body=repository_issue_body,
    )
    result = DeveloperExecutionEngine().execute(
        bundle=developer_task_bundle_from_payload(preview.bundle_payload), commands=("pytest",),
        file_writes=(PlannedFileWrite(path="src/must-not-write.txt", content="bad"),),
        workspace_root=tmp_path, dry_run=False, approved=True, require_human_approval_for_repo_writes=True,
    )
    assert not result.accepted
    assert not result.command_outcomes and not result.file_write_outcomes
    assert not (tmp_path / "src").exists()


def test_same_issue_number_in_different_repositories_is_not_deduplicated(repository_issue_body) -> None:
    dispatcher = DispatcherAgent()
    first = dispatcher.route(_issue_event(repository_issue_body, "repo-one"))
    other = deepcopy(repository_issue_body)
    other["repository"] = {"id": 303, "full_name": "fixture/other", "default_branch": "main"}
    second = dispatcher.route(_issue_event(other, "repo-two"))
    assert first.metadata["preview_id"] != second.metadata["preview_id"]
    assert first.metadata["task_id"] != second.metadata["task_id"]


@pytest.mark.parametrize("github_event,action", [("pull_request", "opened"), ("issue_comment", "created"), ("issues", "closed")])
def test_repository_task_events_are_explicitly_supported(repository_issue_body, github_event, action) -> None:
    event = make_internal_event(
        origin=EventOrigin.GITHUB_WEBHOOK, event_type=EventType.WEBHOOK_EVENT_RECEIVED,
        payload={"github_event": github_event, "action": action, "body": repository_issue_body},
    )
    result = DispatcherAgent().route(event)
    assert result.route == "developer.unsupported"
    assert not result.accepted


@pytest.mark.parametrize("repository", ["fixture/widgets", "uhvogala/aitobuild_example"])
def test_delivery_prepares_only_the_approved_base_in_a_private_checkout(tmp_path, repository_issue_body, repository, local_issue_repository) -> None:
    repository_issue_body["repository"]["full_name"] = repository
    source, revision = local_issue_repository
    registry = DeveloperPreviewRegistry(tmp_path / "state" / "previews.json")
    dispatcher = DispatcherAgent(developer_preview_registry=registry)
    preview = dispatcher.create_developer_preview(
        dedupe_key="github:prepare", github_event="issues", action="opened", body=repository_issue_body,
    )
    worker = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource(repository, 101, source),),
    )
    with pytest.raises(ValueError, match="approval"):
        worker.prepare(preview.preview_id)
    approved = registry.approve(preview.preview_id, base_revision=revision)
    for sources in ((), (LocalRepositorySource(repository, 102, source),)):
        unconfigured = DeveloperDeliveryWorker(
            preview_registry=registry, state_dir=tmp_path / "state", service_root=Path.cwd(),
            repository_sources=sources,
        )
        with pytest.raises(ValueError, match="operator-configured"):
            unconfigured.prepare(preview.preview_id)
        assert unconfigured.get(preview.preview_id) is None
    record = worker.prepare(preview.preview_id)
    assert record.state == "prepared"
    assert record.base_revision == revision == record.head_revision
    assert record.bundle_payload == approved.bundle_payload
    checkout = Path(record.checkout_path)
    assert checkout != source
    assert (checkout / "README.md").read_text() == "Fixture baseline\n"
    assert subprocess.run(["git", "-C", str(checkout), "branch", "--show-current"], check=True, capture_output=True, text=True).stdout.strip() == record.branch
    restarted = DeveloperDeliveryWorker(
        preview_registry=DeveloperPreviewRegistry(tmp_path / "state" / "previews.json"),
        state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource(repository, 101, source),),
    )
    assert restarted.prepare(preview.preview_id) == record
    assert subprocess.run(["git", "-C", str(source), "branch", "--show-current"], check=True, capture_output=True, text=True).stdout.strip() == "main"


@pytest.fixture
def approved_delivery(tmp_path, repository_issue_body, local_issue_repository):
    source, revision = local_issue_repository
    registry = DeveloperPreviewRegistry(tmp_path / "state" / "previews.json")
    preview = DispatcherAgent(developer_preview_registry=registry).create_developer_preview(
        dedupe_key="github:worker", github_event="issues", action="opened", body=repository_issue_body,
    )
    registry.approve(preview.preview_id, base_revision=revision)
    worker = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source),),
    )
    return registry, worker, preview.preview_id, source, revision


def test_interrupted_preparation_fails_closed_after_restart(approved_delivery, tmp_path, monkeypatch) -> None:
    registry, worker, preview_id, source, _ = approved_delivery

    def crash(*arguments, **kwargs):
        raise KeyboardInterrupt("Simulated interruption")

    monkeypatch.setattr(worker, "_git", crash)
    with pytest.raises(KeyboardInterrupt):
        worker.prepare(preview_id)
    assert worker.get(preview_id).state == "preparing"
    budget_path = next((tmp_path / "state" / "budgets").glob("*.json"))
    initial_budget = json.loads(budget_path.read_text())
    restarted = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source),),
    )
    failed = restarted.prepare(preview_id)
    assert failed.state == "failed" and "Interrupted" in failed.error
    assert not Path(failed.checkout_path).exists()
    final_budget = json.loads(budget_path.read_text())
    assert final_budget["aborted"]
    assert final_budget["deadline"] == initial_budget["deadline"]
    assert restarted.prepare(preview_id) == failed
    assert json.loads(budget_path.read_text()) == final_budget


def test_native_delivery_continues_approved_edits_without_reseeding_or_reset(approved_delivery, tmp_path) -> None:
    registry, worker, preview_id, source, _ = approved_delivery
    record = worker.prepare(preview_id)
    bundle = developer_task_bundle_from_payload(record.bundle_payload)
    budget_path = next((tmp_path / "state/budgets").glob("*.json"))
    before = json.loads(budget_path.read_text())
    with worker.implementation_lock(preview_id):
        worker.begin_implementation(preview_id, bundle=bundle, session_id="issue-native", resume=False)
        (Path(record.checkout_path) / "README.md").write_text("Approved edit\n")
        worker.finish_implementation(preview_id, session_id="issue-native", pending=True)
    restarted = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source),),
    )
    with pytest.raises(ValueError, match="another native session"):
        restarted.begin_implementation(preview_id, bundle=bundle, session_id="other-session", resume=True)
    with restarted.implementation_lock(preview_id):
        resumed = restarted.begin_implementation(preview_id, bundle=bundle, session_id="issue-native", resume=True)
        assert resumed.state == "implementing"
        assert (Path(record.checkout_path) / "README.md").read_text() == "Approved edit\n"
        restarted.finish_implementation(preview_id, session_id="issue-native")
    assert restarted.get(preview_id).state == "implemented"
    assert restarted.prepare(preview_id).state == "implemented"
    assert json.loads(budget_path.read_text()) == before
    with pytest.raises(ValueError, match="automatic replay"):
        restarted.begin_implementation(preview_id, bundle=bundle, session_id="issue-native", resume=False)


def test_interrupted_native_delivery_aborts_and_preserves_artifacts(approved_delivery, tmp_path) -> None:
    registry, worker, preview_id, source, _ = approved_delivery
    record = worker.prepare(preview_id)
    bundle = developer_task_bundle_from_payload(record.bundle_payload)
    worker.begin_implementation(preview_id, bundle=bundle, session_id="interrupted", resume=False)
    artifact = Path(record.checkout_path) / "README.md"
    artifact.write_text("Retain partial implementation\n")
    budget_path = next((tmp_path / "state/budgets").glob("*.json"))
    before = json.loads(budget_path.read_text())
    restarted = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source),),
    )
    with pytest.raises(ValueError, match="Interrupted"):
        restarted.begin_implementation(preview_id, bundle=bundle, session_id="interrupted", resume=False)
    assert restarted.get(preview_id).state == "failed"
    after = json.loads(budget_path.read_text())
    assert after["aborted"] and after["deadline"] == before["deadline"]
    assert artifact.read_text() == "Retain partial implementation\n"
    assert restarted.prepare(preview_id).state == "failed"


def test_native_delivery_lock_blocks_concurrent_worker(approved_delivery) -> None:
    from filelock import Timeout

    _, worker, preview_id, _, _ = approved_delivery
    worker.prepare(preview_id)
    with worker.implementation_lock(preview_id):
        with pytest.raises(Timeout):
            with worker.implementation_lock(preview_id):
                pytest.fail("Duplicate native invocation acquired the task lock")
    assert worker.get(preview_id).state == "prepared"


@pytest.fixture
def implemented_delivery(approved_delivery, tmp_path):
    from aitobuild.developer_isolation import DeveloperTaskBudget

    registry, _, preview_id, source, _ = approved_delivery
    worker = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source, ("python -m pytest -q",)),),
    )
    record = worker.prepare(preview_id)
    bundle = developer_task_bundle_from_payload(record.bundle_payload)
    budget_path = next((tmp_path / "state/budgets").glob("*.json"))
    budget = DeveloperTaskBudget(path=budget_path, bundle=bundle, create=False)
    with worker.implementation_lock(preview_id):
        worker.begin_implementation(preview_id, bundle=bundle, session_id="verification-developer", resume=False)
        budget.reserve_paths(("src/probe.py",))
        target = Path(record.checkout_path) / "src/probe.py"
        target.parent.mkdir()
        target.write_text("def probe():\n    return True\n")
        worker.finish_implementation(preview_id, session_id="verification-developer")
    return worker, preview_id, budget_path, source


@pytest.fixture
def verification_adapter(tmp_path):
    class RecordingAdapter(ContainerSessionBashAdapter):
        def __init__(self):
            super().__init__(workspace_root=tmp_path, image="prepared", container_workdir="/workspace", container_name_prefix="verify-test")
            self.created = []
            self.closed = []

        def create_session(self, *, session_id=None, read_only_workspace=False, deadline=None):
            self.created.append((session_id, read_only_workspace, deadline))
            return session_id, "test-verifier"

        def close_session(self, *, session_id):
            self.closed.append(session_id)
            return True

    return RecordingAdapter()


def test_verification_records_real_exit_evidence_and_dedupes_restart(implemented_delivery, verification_adapter, monkeypatch, tmp_path) -> None:
    worker, preview_id, budget_path, source = implemented_delivery
    before = json.loads(budget_path.read_text())
    calls = []

    def shell_reply(adapter, session_id, request):
        calls.append(request)
        return {"ok": True, "status": "exited", "exit_code": 0, "output": "2 passed", "next_cursor": 8}

    monkeypatch.setattr(delivery_module, "shell_request", shell_reply)
    record = worker.verify(preview_id, adapter=verification_adapter)
    assert record.state == "verified", record.error
    assert record.verification_commands == ["python -m pytest -q"]
    evidence = record.verification
    assert evidence["session_id"] != record.session_id
    assert evidence["commands"][0]["exit_code"] == 0
    assert evidence["cleanup_succeeded"]
    assert (Path(record.checkout_path).parent / evidence["commands"][0]["output_path"]).read_text() == "2 passed"
    assert verification_adapter.created[0][1] is True
    assert verification_adapter.created[0][2] == pytest.approx(before["deadline"], abs=1)
    assert verification_adapter.closed == [evidence["session_id"]]
    restarted = DeveloperDeliveryWorker(
        preview_registry=DeveloperPreviewRegistry(), state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source, ("python -m pytest -q",)),),
    )
    assert restarted.verify(preview_id, adapter=verification_adapter) == record
    assert len(calls) == 1 and len(verification_adapter.created) == 1
    assert json.loads(budget_path.read_text()) == before


@pytest.mark.parametrize("exit_code", [1, 124, None, False, "0"])
def test_verification_failure_aborts_budget_and_cannot_replay(implemented_delivery, verification_adapter, monkeypatch, exit_code) -> None:
    worker, preview_id, budget_path, _ = implemented_delivery
    before = json.loads(budget_path.read_text())
    calls = []

    def shell_reply(*arguments):
        calls.append(arguments)
        return {"ok": True, "status": "exited", "exit_code": exit_code, "output": "failed tests"}

    monkeypatch.setattr(delivery_module, "shell_request", shell_reply)
    failed = worker.verify(preview_id, adapter=verification_adapter)
    assert failed.state == "failed" and failed.error
    assert failed.verification["commands"][0]["exit_code"] == exit_code
    assert verification_adapter.closed == [failed.verification["session_id"]]
    after = json.loads(budget_path.read_text())
    assert after["aborted"] and after["deadline"] == before["deadline"]
    assert after["reserved_paths"] == before["reserved_paths"]
    assert worker.verify(preview_id, adapter=verification_adapter) == failed
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", ["before", "during", "after"])
def test_verification_blocks_unreserved_and_mutated_checkout(implemented_delivery, verification_adapter, monkeypatch, mutation) -> None:
    worker, preview_id, budget_path, _ = implemented_delivery
    record = worker.get(preview_id)
    target = Path(record.checkout_path) / "src/probe.py"
    calls = []

    def shell_reply(*arguments):
        calls.append(arguments)
        if mutation == "during":
            target.write_text("Changed while verifying\n")
        return {"ok": True, "status": "exited", "exit_code": 0, "output": "passed"}

    monkeypatch.setattr(delivery_module, "shell_request", shell_reply)
    if mutation == "before":
        (target.parent / "unreserved.py").write_text("Unexpected change\n")
    record = worker.verify(preview_id, adapter=verification_adapter)
    if mutation == "after":
        assert record.state == "verified"
        target.write_text("Changed after verifying\n")
        record = worker.verify(preview_id, adapter=verification_adapter)
    assert record.state == "failed" and record.error
    assert json.loads(budget_path.read_text())["aborted"]
    assert len(calls) == (0 if mutation == "before" else 1)


def test_verification_cleanup_failure_blocks_success(implemented_delivery, verification_adapter, monkeypatch) -> None:
    worker, preview_id, budget_path, _ = implemented_delivery
    monkeypatch.setattr(delivery_module, "shell_request", lambda *arguments: {"ok": True, "status": "exited", "exit_code": 0})
    monkeypatch.setattr(verification_adapter, "close_session", lambda **arguments: False)
    record = worker.verify(preview_id, adapter=verification_adapter)
    assert record.state == "failed" and "cleanup" in record.error
    assert json.loads(budget_path.read_text())["aborted"]


@pytest.mark.parametrize("status", [None, "failed"])
def test_verification_requires_confirmed_exit_even_with_zero_code(implemented_delivery, verification_adapter, monkeypatch, status) -> None:
    worker, preview_id, budget_path, _ = implemented_delivery
    monkeypatch.setattr(delivery_module, "shell_request", lambda *arguments: {
        "ok": True, "status": status, "exit_code": 0, "output": "not confirmed",
    })
    record = worker.verify(preview_id, adapter=verification_adapter)
    assert record.state == "failed" and "confirm process exit" in record.error
    assert record.verification["commands"][0]["exit_code"] == 0
    assert json.loads(budget_path.read_text())["aborted"]


def test_verification_timeout_preserves_command_log_and_deadline(implemented_delivery, verification_adapter, monkeypatch) -> None:
    worker, preview_id, budget_path, _ = implemented_delivery
    before = json.loads(budget_path.read_text())
    clock = [0]

    def shell_reply(*arguments):
        clock[0] = 10000
        return {"ok": True, "status": "running", "shell_id": "job", "output": "partial output", "next_cursor": 14}

    monkeypatch.setattr(delivery_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(delivery_module, "shell_request", shell_reply)
    record = worker.verify(preview_id, adapter=verification_adapter)
    assert record.state == "failed" and "deadline" in record.error
    result = record.verification["commands"][0]
    assert result["exit_code"] is None
    assert (Path(record.checkout_path).parent / result["output_path"]).read_text() == "partial output"
    assert json.loads(budget_path.read_text())["deadline"] == before["deadline"]
    assert verification_adapter.closed


def test_verification_interruption_aborts_without_reset(implemented_delivery, verification_adapter, monkeypatch) -> None:
    worker, preview_id, budget_path, _ = implemented_delivery

    def crash(*arguments):
        raise KeyboardInterrupt("verification interrupted")

    monkeypatch.setattr(delivery_module, "shell_request", crash)
    with pytest.raises(KeyboardInterrupt):
        worker.verify(preview_id, adapter=verification_adapter)
    assert worker.get(preview_id).state == "failed"
    assert json.loads(budget_path.read_text())["aborted"]
    assert verification_adapter.closed


def test_verification_requires_implementation_and_a_pinned_plan(approved_delivery, verification_adapter) -> None:
    registry, worker, preview_id, _, _ = approved_delivery
    record = worker.prepare(preview_id)
    with pytest.raises(ValueError, match="completed native implementation"):
        worker.verify(preview_id, adapter=verification_adapter)
    bundle = developer_task_bundle_from_payload(registry.get(preview_id).bundle_payload)
    worker.begin_implementation(preview_id, bundle=bundle, session_id="no-plan-native", resume=False)
    worker.finish_implementation(preview_id, session_id="no-plan-native")
    with pytest.raises(ValueError, match="No verification plan"):
        worker.verify(preview_id, adapter=verification_adapter)
    assert verification_adapter.created == []
    assert worker.get(preview_id).verification_commands == record.verification_commands == []


@pytest.mark.parametrize("change", ["plan", "source"])
def test_verification_cannot_replace_the_pinned_target_or_plan(implemented_delivery, verification_adapter, tmp_path, change) -> None:
    worker, preview_id, budget_path, source = implemented_delivery
    before = json.loads(budget_path.read_text())
    changed = DeveloperDeliveryWorker(
        preview_registry=DeveloperPreviewRegistry(), state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, tmp_path / "other" if change == "source" else source,
                                                  ("true",) if change == "plan" else ("python -m pytest -q",)),),
    )
    with pytest.raises(ValueError, match="target/plan differs"):
        changed.verify(preview_id, adapter=verification_adapter)
    assert worker.get(preview_id).state == "implemented"
    assert json.loads(budget_path.read_text()) == before
    assert verification_adapter.created == []


def test_verification_expiry_aborts_the_original_budget(implemented_delivery, verification_adapter, monkeypatch) -> None:
    worker, preview_id, budget_path, _ = implemented_delivery
    before = json.loads(budget_path.read_text())
    monkeypatch.setattr("aitobuild.developer_isolation.time", lambda: 1e30)
    record = worker.verify(preview_id, adapter=verification_adapter)
    assert record.state == "failed" and "expired" in record.error
    after = json.loads(budget_path.read_text())
    assert after["deadline"] == before["deadline"] and after["aborted"]
    assert verification_adapter.created == []


def test_verification_restart_aborts_interrupted_run_and_closes_its_session(implemented_delivery, verification_adapter) -> None:
    from dataclasses import replace

    worker, preview_id, budget_path, _ = implemented_delivery
    record = worker.get(preview_id)
    directory = Path(record.checkout_path).parent
    worker._save(directory, replace(record, state="verifying", verification={
        "session_id": "verify-interrupted", "commands": [], "checkout_digest": "0" * 64,
        "started_at": datetime.now(tz=UTC).isoformat(), "cleanup_succeeded": False,
    }))
    failed = worker.verify(preview_id, adapter=verification_adapter)
    assert failed.state == "failed" and "Interrupted" in failed.error
    assert json.loads(budget_path.read_text())["aborted"]
    assert verification_adapter.created == [] and verification_adapter.closed == ["verify-interrupted"]
    assert worker.verify(preview_id, adapter=verification_adapter) == failed


def test_verification_live_status_and_concurrent_rejection(implemented_delivery, verification_adapter, monkeypatch) -> None:
    from threading import Event
    from filelock import Timeout

    worker, preview_id, _, _ = implemented_delivery
    entered, release = Event(), Event()

    def shell_reply(*arguments):
        entered.set()
        assert release.wait(5)
        return {"ok": True, "status": "exited", "exit_code": 0, "output": "passed"}

    monkeypatch.setattr(delivery_module, "shell_request", shell_reply)
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(worker.verify, preview_id, adapter=verification_adapter)
        try:
            assert entered.wait(5)
            assert worker.get(preview_id).state == "verifying"
            with pytest.raises(Timeout):
                worker.verify(preview_id, adapter=verification_adapter)
            assert worker.get(preview_id).state == "verifying"
        finally:
            release.set()
        assert running.result().state == "verified"


@pytest.mark.parametrize("field,value", [("checkout_digest", "not-a-digest"), ("cleanup_succeeded", False),
                                        ("commands", []), ("session_id", "verification-developer"),
                                        ("started_at", None), ("completed_at", 123)])
def test_verification_corrupt_persisted_evidence_is_rejected(implemented_delivery, verification_adapter, monkeypatch, field, value) -> None:
    worker, preview_id, _, _ = implemented_delivery
    monkeypatch.setattr(delivery_module, "shell_request", lambda *arguments: {"ok": True, "status": "exited", "exit_code": 0})
    record = worker.verify(preview_id, adapter=verification_adapter)
    path = Path(record.checkout_path).parent / "state.json"
    state = json.loads(path.read_text())
    state["record"]["verification"][field] = value
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="verification"):
        worker.get(preview_id)


@pytest.mark.skipif(os.environ.get("AITOBUILD_RUN_DELIVERY_DOCKER_TEST") != "1", reason="Opt-in prepared-image Docker check")
@pytest.mark.parametrize("verification_succeeds", [True, False])
def test_prepared_target_native_tools_execute_in_constrained_docker(approved_delivery, verification_succeeds) -> None:
    from aitobuild.agent_tools import DeveloperToolContext, build_role_tools
    from aitobuild.developer_isolation import DeveloperTaskBudget
    from aitobuild.tools import MockBashAdapter, MockFilesystemAdapter
    from aitobuild.tools.bash import ContainerSessionBashAdapter

    registry, _, preview_id, source, _ = approved_delivery
    identity = "delivery-probe-" + uuid4().hex[:12]
    state = Path.cwd() / "sim/.run-artifacts" / identity
    verification_command = "PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider" + ("" if verification_succeeds else " -k no_such_test")
    worker = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=state, service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source, (verification_command,)),),
    )
    record = worker.prepare(preview_id)
    assert record.state == "prepared", record.error
    bundle = developer_task_bundle_from_payload(record.bundle_payload)
    budget_path = next((state / "budgets").glob("*.json"))
    budget = DeveloperTaskBudget(path=budget_path, bundle=bundle, create=False)
    initial_budget = json.loads(budget_path.read_text())
    adapter = ContainerSessionBashAdapter(
        workspace_root=Path.cwd(), image="aitobuild-developer:local", container_workdir="/workspace",
        container_name_prefix=identity, data_volume_name=identity,
    )
    adapter.bind_session_workspace(session_id=identity, workspace=Path(record.checkout_path))
    tools = {tool.name: tool for tool in build_role_tools(context=DeveloperToolContext(
        bash_adapter=MockBashAdapter(), filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=Path.cwd(), require_human_approval_for_repo_writes=True,
        container_session_adapter=adapter, bound_session_id=identity, isolation_policy=bundle.policy,
        task_budget=budget, prepared_workspace=Path(record.checkout_path),
    ))["developer"]}
    try:
        with worker.implementation_lock(preview_id):
            worker.begin_implementation(preview_id, bundle=bundle, session_id=identity, resume=False)
            tools["developer_write_file"]("src/widgets.py", "def validate_name(name):\n    if not name:\n        raise ValueError('Empty name')\n    return name\n", approved=True)
            tools["developer_write_file"]("tests/test_widgets.py", "import sys\nfrom pathlib import Path\nimport pytest\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\nfrom widgets import validate_name\n\ndef test_empty():\n    with pytest.raises(ValueError):\n        validate_name('')\n\ndef test_existing():\n    assert validate_name('Known') == 'Known'\n", approved=True)
            result = tools["developer_run_command"]("PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider")
            assert result["exit_code"] == 0, result
            container = adapter.get_container_name(session_id=identity)
            inspected = subprocess.run(["docker", "inspect", container], check=True, capture_output=True, text=True)
            config = json.loads(inspected.stdout)[0]
            assert config["HostConfig"]["ReadonlyRootfs"] and config["HostConfig"]["NetworkMode"] == "none"
            repo = next(mount for mount in config["Mounts"] if mount["Destination"] == "/workspace")
            assert not repo["RW"] and repo["Source"].endswith(str(Path(record.checkout_path).relative_to(Path.cwd())))
            worker.finish_implementation(preview_id, session_id=identity)
            print(json.dumps({"artifact_dir": str(state), "command": result["command"], "exit_code": result["exit_code"],
                              "output": result["stdout"], "delivery_state": worker.get(preview_id).state}))
        assert adapter.close_session(session_id=identity)
        verified = worker.verify(preview_id, adapter=adapter)
        assert verified.state == ("verified" if verification_succeeds else "failed"), verified.error
        evidence = verified.verification
        assert evidence["session_id"] != identity
        assert evidence["commands"][0]["exit_code"] == (0 if verification_succeeds else 5)
        assert evidence["cleanup_succeeded"]
        assert adapter.list_session_ids() == ()
        print(json.dumps({"artifact_dir": str(state), "verification": evidence, "delivery_state": verified.state}))
    finally:
        adapter.close_session(session_id=identity)
        assert identity not in adapter.list_session_ids()
        subprocess.run(["docker", "volume", "rm", identity], check=True, capture_output=True)
    final_budget = json.loads(budget_path.read_text())
    assert final_budget["deadline"] == initial_budget["deadline"]
    assert final_budget["reserved_paths"] == ["src/widgets.py", "tests/test_widgets.py"]
    assert final_budget.get("aborted", False) is (not verification_succeeds)
    assert (source / "README.md").read_text() == "Fixture baseline\n"
    assert not (source / "src").exists()


def test_concurrent_preparation_creates_one_checkout(approved_delivery, tmp_path) -> None:
    registry, first, preview_id, source, _ = approved_delivery
    second = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source),),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda worker: worker.prepare(preview_id), (first, second)))
    assert records[0] == records[1]
    assert records[0].state == "prepared"
    assert len(list((tmp_path / "state" / "deliveries").glob("*/repo"))) == 1


def test_preparation_rejects_service_checkout_sources(approved_delivery, tmp_path) -> None:
    registry, _, preview_id, source, _ = approved_delivery
    worker = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=source,
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source),),
    )
    failed = worker.prepare(preview_id)
    assert failed.state == "failed" and "service checkout" in failed.error
    assert not Path(failed.checkout_path).exists()


def test_preparation_rejects_linked_service_worktrees(approved_delivery, tmp_path) -> None:
    registry, _, preview_id, source, revision = approved_delivery
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(linked), revision], check=True, capture_output=True)
    worker = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=source,
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, linked),),
    )
    failed = worker.prepare(preview_id)
    assert failed.state == "failed" and "linked service worktree" in failed.error


def test_preparation_base_is_pinned_when_source_advances(approved_delivery) -> None:
    _, worker, preview_id, source, revision = approved_delivery
    (source / "README.md").write_text("Later content\n")
    subprocess.run(["git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-am", "Later"], check=True, capture_output=True)
    record = worker.prepare(preview_id)
    assert record.state == "prepared" and record.head_revision == revision
    assert (Path(record.checkout_path) / "README.md").read_text() == "Fixture baseline\n"


def test_preparation_rejects_commit_outside_the_base_branch(tmp_path, repository_issue_body, local_issue_repository) -> None:
    source, revision = local_issue_repository
    unrelated = subprocess.run([
        "git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit-tree", f"{revision}^{{tree}}", "-m", "Unrelated root",
    ], check=True, capture_output=True, text=True).stdout.strip()
    registry = DeveloperPreviewRegistry(tmp_path / "state" / "previews.json")
    preview = DispatcherAgent(developer_preview_registry=registry).create_developer_preview(
        dedupe_key="github:unrelated", github_event="issues", action="opened", body=repository_issue_body,
    )
    registry.approve(preview.preview_id, base_revision=unrelated)
    worker = DeveloperDeliveryWorker(
        preview_registry=registry, state_dir=tmp_path / "state", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source),),
    )
    record = worker.prepare(preview.preview_id)
    assert record.state == "failed" and record.error
    assert not Path(record.checkout_path).exists()
    assert (Path(record.checkout_path).parent / "preparation.log").exists()


def test_modified_prepared_checkout_is_not_silently_recreated(approved_delivery) -> None:
    _, worker, preview_id, _, _ = approved_delivery
    record = worker.prepare(preview_id)
    checkout = Path(record.checkout_path)
    (checkout / "README.md").write_text("Retain failure artifact\n")
    failed = worker.prepare(preview_id)
    assert failed.state == "failed" and "modified" in failed.error
    assert (checkout / "README.md").read_text() == "Retain failure artifact\n"


@pytest.mark.parametrize("field,value", [("branch", "wrong"), ("checkout_path", "/tmp/wrong"), ("preview_id", "wrong")])
def test_delivery_state_identity_is_validated(approved_delivery, field, value) -> None:
    _, worker, preview_id, _, _ = approved_delivery
    record = worker.prepare(preview_id)
    state_path = Path(record.checkout_path).parent / "state.json"
    data = json.loads(state_path.read_text())
    data["record"][field] = value
    state_path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="identity"):
        worker.get(preview_id)


def test_preparation_timeout_kills_process_group_and_retains_diagnostics(approved_delivery, tmp_path, monkeypatch) -> None:
    _, worker, preview_id, _, _ = approved_delivery
    killed = []

    class TimedOutProcess:
        pid = 987654321
        returncode = -9

        def __init__(self, command, **kwargs):
            self.command = command
            self.calls = 0
            assert kwargs["start_new_session"] is True
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(self.command, timeout)
            return "Retained stdout", "Retained stderr"

    monkeypatch.setattr(delivery_module.subprocess, "Popen", TimedOutProcess)
    monkeypatch.setattr(delivery_module.os, "killpg", lambda pid, signal_number: killed.append((pid, signal_number)))
    failed = worker.prepare(preview_id)
    assert failed.state == "failed" and "deadline" in failed.error
    assert killed == [(TimedOutProcess.pid, delivery_module.signal.SIGKILL)]
    log = json.loads((Path(failed.checkout_path).parent / "preparation.log").read_text())
    assert log["timed_out"] and log["exit_code"] == -9
    assert log["stdout"] == "Retained stdout" and log["stderr"] == "Retained stderr"
    assert json.loads(next((tmp_path / "state" / "budgets").glob("*.json")).read_text())["aborted"]


def test_preparation_restart_does_not_reset_expired_deadline(approved_delivery, tmp_path) -> None:
    _, worker, preview_id, _, _ = approved_delivery
    record = worker.prepare(preview_id)
    budget_path = next((tmp_path / "state" / "budgets").glob("*.json"))
    budget = json.loads(budget_path.read_text())
    budget["deadline"] = 0
    budget_path.write_text(json.dumps(budget))
    log_path = Path(record.checkout_path).parent / "preparation.log"
    initial_log = log_path.read_text()
    failed = worker.prepare(preview_id)
    assert failed.state == "failed" and "expired" in failed.error
    final_budget = json.loads(budget_path.read_text())
    assert final_budget["deadline"] == 0 and final_budget["aborted"]
    assert log_path.read_text() == initial_log


def test_publish_verified_delivery_opens_draft_pr_idempotently(
    implemented_delivery, verification_adapter, monkeypatch,
) -> None:
    from aitobuild.tools.github import MockGitHubAdapter

    worker, preview_id, _, _ = implemented_delivery
    monkeypatch.setattr(
        delivery_module,
        "shell_request",
        lambda *arguments, **kwargs: {
            "ok": True,
            "status": "exited",
            "exit_code": 0,
            "output": "ok",
            "next_cursor": 1,
        },
    )
    verified = worker.verify(preview_id, adapter=verification_adapter)
    assert verified.state == "verified"
    github = MockGitHubAdapter(
        allowed_repositories=frozenset({"fixture/widgets"}),
        enforce_allowlist=True,
    )
    with pytest.raises(ValueError, match="mock publication is refused"):
        worker.publish(
            preview_id,
            github=github,
            require_human_approval_for_repo_writes=True,
        )
    published = worker.publish(
        preview_id,
        github=github,
        require_human_approval_for_repo_writes=True,
        allow_mock_publication=True,
    )
    assert published.state == "published", published.error
    assert published.publication["draft"] is True
    assert published.publication["pull_number"] == 1
    assert published.head_revision == published.publication["head_sha"]
    assert "Closes #7" in published.publication["body"] or "Closes #" in published.publication["body"]
    assert len(github.branch_commits) == 1
    assert github.pull_requests["fixture/widgets"][1].draft is True
    again = worker.publish(
        preview_id,
        github=github,
        require_human_approval_for_repo_writes=True,
        allow_mock_publication=True,
    )
    assert again == published
    assert len(github.branch_commits) == 1


def test_publish_rejects_wrong_state_and_drift(
    implemented_delivery, verification_adapter, monkeypatch,
) -> None:
    from aitobuild.tools.github import MockGitHubAdapter

    worker, preview_id, _, _ = implemented_delivery
    github = MockGitHubAdapter(
        allowed_repositories=frozenset({"fixture/widgets"}),
        enforce_allowlist=True,
    )
    with pytest.raises(ValueError, match="verified"):
        worker.publish(
            preview_id,
            github=github,
            require_human_approval_for_repo_writes=True,
            allow_mock_publication=True,
        )
    monkeypatch.setattr(
        delivery_module,
        "shell_request",
        lambda *a, **k: {"ok": True, "status": "exited", "exit_code": 0, "output": "ok", "next_cursor": 1},
    )
    worker.verify(preview_id, adapter=verification_adapter)
    record = worker.get(preview_id)
    assert record is not None
    drifted = Path(record.checkout_path) / "src/probe.py"
    drifted.write_text("def probe():\n    return False\n")
    blocked = worker.publish(
        preview_id,
        github=github,
        require_human_approval_for_repo_writes=True,
        allow_mock_publication=True,
    )
    assert blocked.state == "failed"
    assert blocked.error and (
        "publication is blocked" in blocked.error or "Verified checkout changed" in blocked.error
    )


def test_publish_resumes_interrupted_publishing_and_reuses_pull(
    implemented_delivery, verification_adapter, monkeypatch,
) -> None:
    from aitobuild.tools.github import MockGitHubAdapter
    from dataclasses import replace

    worker, preview_id, _, _ = implemented_delivery
    monkeypatch.setattr(
        delivery_module,
        "shell_request",
        lambda *a, **k: {"ok": True, "status": "exited", "exit_code": 0, "output": "ok", "next_cursor": 1},
    )
    worker.verify(preview_id, adapter=verification_adapter)
    github = MockGitHubAdapter(
        allowed_repositories=frozenset({"fixture/widgets"}),
        enforce_allowlist=True,
    )
    # First publish succeeds.
    published = worker.publish(
        preview_id,
        github=github,
        require_human_approval_for_repo_writes=True,
        allow_mock_publication=True,
    )
    assert published.state == "published"
    pull_number = published.publication["pull_number"]
    # Simulate interrupted publishing after PR create (orphan-avoidance resume).
    directory = worker._task_dir(preview_id)
    interrupted = replace(
        published,
        state="publishing",
        head_revision=published.base_revision,
        publication={
            key: value
            for key, value in published.publication.items()
            if key != "published_at"
        },
    )
    worker._save(directory, interrupted)
    resumed = worker.publish(
        preview_id,
        github=github,
        require_human_approval_for_repo_writes=True,
        allow_mock_publication=True,
    )
    assert resumed.state == "published"
    assert resumed.publication["pull_number"] == pull_number
    assert len(github.pull_requests["fixture/widgets"]) == 1
    assert "Verification" in resumed.publication["body"]


def test_architect_review_published_draft_from_publication_only(
    implemented_delivery, verification_adapter, monkeypatch,
) -> None:
    from aitobuild.tools.github import MockGitHubAdapter
    from aitobuild.developer_delivery import ARCHITECT_REVIEW_BODY_PREFIX

    worker, preview_id, _, _ = implemented_delivery
    monkeypatch.setattr(
        delivery_module,
        "shell_request",
        lambda *a, **k: {"ok": True, "status": "exited", "exit_code": 0, "output": "ok", "next_cursor": 1},
    )
    worker.verify(preview_id, adapter=verification_adapter)
    github = MockGitHubAdapter(
        allowed_repositories=frozenset({"fixture/widgets"}),
        enforce_allowlist=True,
    )
    published = worker.publish(
        preview_id,
        github=github,
        require_human_approval_for_repo_writes=True,
        allow_mock_publication=True,
    )
    assert published.state == "published"
    pull = worker.get_published_pull_request(preview_id, github=github)
    assert pull["number"] == published.publication["pull_number"]
    assert pull["draft"] is True
    assert pull["head_matches_publication"] is True
    assert pull["repository"] == "fixture/widgets"
    reviewed = worker.submit_architect_review(
        preview_id,
        github=github,
        event="COMMENT",
        body="Please extract a helper before merge.",
    )
    assert reviewed.architect_review is not None
    assert reviewed.architect_review["event"] == "COMMENT"
    assert reviewed.architect_review["body"].startswith(ARCHITECT_REVIEW_BODY_PREFIX)
    assert reviewed.architect_review["head_sha"] == published.publication["head_sha"]
    assert len(github.reviews) == 1
    again = worker.get_published_pull_request(preview_id, github=github)
    assert again["architect_review"]["event"] == "COMMENT"
    with pytest.raises(ValueError, match="distinct Architect reviewer|COMMENT only"):
        worker.submit_architect_review(
            preview_id, github=github, event="REQUEST_CHANGES", body="Needs changes.",
        )
    with pytest.raises(ValueError, match="allows only COMMENT"):
        worker.submit_architect_review(
            preview_id, github=github, event="APPROVE", body="LGTM",
        )
    with pytest.raises(ValueError, match="exceeds"):
        worker.submit_architect_review(
            preview_id, github=github, event="COMMENT", body="x" * 5000,
        )


def test_architect_review_refuses_when_publication_head_moved(
    implemented_delivery, verification_adapter, monkeypatch,
) -> None:
    from aitobuild.tools.github import MockGitHubAdapter, GitHubPullRequest

    worker, preview_id, _, _ = implemented_delivery
    monkeypatch.setattr(
        delivery_module,
        "shell_request",
        lambda *a, **k: {"ok": True, "status": "exited", "exit_code": 0, "output": "ok", "next_cursor": 1},
    )
    worker.verify(preview_id, adapter=verification_adapter)
    github = MockGitHubAdapter(
        allowed_repositories=frozenset({"fixture/widgets"}),
        enforce_allowlist=True,
    )
    published = worker.publish(
        preview_id,
        github=github,
        require_human_approval_for_repo_writes=True,
        allow_mock_publication=True,
    )
    pull_number = published.publication["pull_number"]
    current = github.pull_requests["fixture/widgets"][pull_number]
    github.pull_requests["fixture/widgets"][pull_number] = GitHubPullRequest(
        number=current.number,
        title=current.title,
        body=current.body,
        state=current.state,
        head_ref=current.head_ref,
        base_ref=current.base_ref,
        draft=current.draft,
        html_url=current.html_url,
        repository=current.repository,
        changed_files=current.changed_files,
        head_sha="f" * 40,
    )
    fetched = worker.get_published_pull_request(preview_id, github=github)
    assert fetched["head_matches_publication"] is False
    with pytest.raises(ValueError, match="head SHA no longer matches|head SHA changed"):
        worker.submit_architect_review(
            preview_id, github=github, event="COMMENT", body="Looks fine overall.",
        )
