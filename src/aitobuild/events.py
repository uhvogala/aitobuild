"""Core event contracts for webhook and internal trigger flows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class EventOrigin(StrEnum):
    GITHUB_WEBHOOK = "github_webhook"
    SCHEDULER = "scheduler"
    MANUAL_REQUEST = "manual_request"
    SYSTEM_SIGNAL = "system_signal"


class EventType(StrEnum):
    WEBHOOK_EVENT_RECEIVED = "webhook_event_received"
    ARCHITECT_SCAN_REQUESTED = "architect_scan_requested"
    MEETING_REQUESTED = "meeting_requested"
    MEETING_DUE = "meeting_due"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(slots=True, frozen=True)
class TriggerEnvelope:
    trigger_id: str
    correlation_id: str
    origin: EventOrigin
    event_type: EventType
    created_at: datetime
    dedupe_key: str
    priority: int = 5
    scheduled_for: datetime | None = None
    policy_context: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class InternalEvent:
    envelope: TriggerEnvelope
    payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class TriggerRequest:
    """External-facing request body for internal trigger submission."""

    origin: EventOrigin
    event_type: EventType
    payload: dict[str, Any]
    dedupe_key: str | None = None
    priority: int = 5


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def make_internal_event(
    *,
    origin: EventOrigin,
    event_type: EventType,
    payload: dict[str, Any],
    correlation_id: str | None = None,
    dedupe_key: str | None = None,
    priority: int = 5,
    scheduled_for: datetime | None = None,
    policy_context: dict[str, Any] | None = None,
) -> InternalEvent:
    now = utc_now()
    final_correlation = correlation_id or f"corr-{uuid4()}"
    final_dedupe = dedupe_key or f"{origin}:{event_type}:{final_correlation}"
    envelope = TriggerEnvelope(
        trigger_id=f"trg-{uuid4()}",
        correlation_id=final_correlation,
        origin=origin,
        event_type=event_type,
        created_at=now,
        dedupe_key=final_dedupe,
        priority=priority,
        scheduled_for=scheduled_for,
        policy_context=policy_context,
    )
    return InternalEvent(envelope=envelope, payload=payload)


def normalize_github_webhook(
    *,
    github_event: str,
    action: str | None,
    delivery_id: str,
    body: dict[str, Any],
) -> InternalEvent:
    payload = {
        "github_event": github_event,
        "action": action,
        "body": body,
    }
    dedupe_key = f"github:{delivery_id}"
    return make_internal_event(
        origin=EventOrigin.GITHUB_WEBHOOK,
        event_type=EventType.WEBHOOK_EVENT_RECEIVED,
        correlation_id=f"gh-{delivery_id}",
        dedupe_key=dedupe_key,
        payload=payload,
    )


def parse_trigger_request(data: dict[str, Any]) -> TriggerRequest:
    try:
        origin = EventOrigin(data["origin"])
        event_type = EventType(data["event_type"])
    except (KeyError, ValueError) as exc:
        raise ValueError("Invalid trigger origin or event_type") from exc

    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    priority_raw = data.get("priority", 5)
    if not isinstance(priority_raw, int):
        raise ValueError("priority must be an integer")

    dedupe_key = data.get("dedupe_key")
    if dedupe_key is not None and not isinstance(dedupe_key, str):
        raise ValueError("dedupe_key must be a string")

    return TriggerRequest(
        origin=origin,
        event_type=event_type,
        payload=payload,
        dedupe_key=dedupe_key,
        priority=priority_raw,
    )
