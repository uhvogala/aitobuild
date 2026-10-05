from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
from typing import Any

import pytest

from aitobuild.developer_isolation import developer_task_bundle_from_payload
from aitobuild.developer_execution import DeveloperExecutionEngine, PlannedFileWrite
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
