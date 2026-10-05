from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
from typing import Any
import subprocess

import pytest

from aitobuild.developer_isolation import developer_task_bundle_from_payload
from aitobuild.developer_execution import DeveloperExecutionEngine, PlannedFileWrite
from aitobuild.developer_delivery import DeveloperDeliveryWorker, LocalRepositorySource
import aitobuild.developer_delivery as delivery_module
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
