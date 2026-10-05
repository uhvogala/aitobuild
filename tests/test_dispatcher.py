from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aitobuild.dispatcher import DispatcherAgent
from aitobuild.events import EventOrigin, EventType, make_internal_event


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
