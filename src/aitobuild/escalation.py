"""Shared escalation routing and sink abstractions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class EscalationSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EscalationCategory(StrEnum):
    DEADLINE_EXCEEDED = "deadline_exceeded"
    POLICY_VIOLATION = "policy_violation"
    SYSTEM_FAILURE = "system_failure"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class EscalationDestination(StrEnum):
    HUMAN_REVIEW_QUEUE = "human_review_queue"
    SYSTEM_EVENT_BUS = "system_event_bus"
    EXTERNAL_WEBHOOK = "external_webhook"


@dataclass(slots=True, frozen=True)
class EscalationEvent:
    escalation_id: str
    created_at: datetime
    source: str
    category: EscalationCategory
    severity: EscalationSeverity
    summary: str
    details: dict[str, object]
    destinations: tuple[EscalationDestination, ...]


class EscalationSink:
    def emit(self, event: EscalationEvent) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class InMemoryEscalationSink(EscalationSink):
    def __init__(self) -> None:
        self._events: list[EscalationEvent] = []

    def emit(self, event: EscalationEvent) -> None:
        self._events.append(event)

    def list_events(self) -> tuple[EscalationEvent, ...]:
        return tuple(self._events)


class EscalationRouter:
    """Single escalation route that fans out to destination-specific sinks."""

    def __init__(self, *, sinks: dict[EscalationDestination, EscalationSink] | None = None) -> None:
        self._all_events: list[EscalationEvent] = []
        self._sinks = sinks or {
            EscalationDestination.HUMAN_REVIEW_QUEUE: InMemoryEscalationSink(),
            EscalationDestination.SYSTEM_EVENT_BUS: InMemoryEscalationSink(),
            EscalationDestination.EXTERNAL_WEBHOOK: InMemoryEscalationSink(),
        }

    def escalate(
        self,
        *,
        source: str,
        category: EscalationCategory,
        severity: EscalationSeverity,
        summary: str,
        details: dict[str, object],
        destinations: tuple[EscalationDestination, ...] | None = None,
    ) -> EscalationEvent:
        event = EscalationEvent(
            escalation_id=f"esc-{uuid4()}",
            created_at=datetime.now(tz=UTC),
            source=source,
            category=category,
            severity=severity,
            summary=summary,
            details=details,
            destinations=destinations or self._default_destinations(category, severity),
        )

        for destination in event.destinations:
            sink = self._sinks.get(destination)
            if sink is not None:
                sink.emit(event)

        self._all_events.append(event)
        return event

    def list_events(self) -> tuple[EscalationEvent, ...]:
        return tuple(self._all_events)

    def _default_destinations(
        self,
        category: EscalationCategory,
        severity: EscalationSeverity,
    ) -> tuple[EscalationDestination, ...]:
        destinations = [EscalationDestination.HUMAN_REVIEW_QUEUE]
        if severity in {EscalationSeverity.HIGH, EscalationSeverity.CRITICAL}:
            destinations.append(EscalationDestination.SYSTEM_EVENT_BUS)
        if category is EscalationCategory.SYSTEM_FAILURE:
            destinations.append(EscalationDestination.EXTERNAL_WEBHOOK)
        return tuple(destinations)
