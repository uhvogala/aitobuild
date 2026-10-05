from __future__ import annotations

from aitobuild.escalation import (
    EscalationCategory,
    EscalationDestination,
    EscalationRouter,
    EscalationSeverity,
)


def test_escalation_router_default_destinations_for_high_deadline_event() -> None:
    router = EscalationRouter()
    event = router.escalate(
        source="meeting.lifecycle",
        category=EscalationCategory.DEADLINE_EXCEEDED,
        severity=EscalationSeverity.HIGH,
        summary="Meeting overdue",
        details={"meeting_id": "m-1"},
    )

    assert EscalationDestination.HUMAN_REVIEW_QUEUE in event.destinations
    assert EscalationDestination.SYSTEM_EVENT_BUS in event.destinations


def test_escalation_router_accepts_custom_destinations() -> None:
    router = EscalationRouter()
    event = router.escalate(
        source="policy.engine",
        category=EscalationCategory.POLICY_VIOLATION,
        severity=EscalationSeverity.MEDIUM,
        summary="Policy check failed",
        details={"rule": "repo_write_requires_approval"},
        destinations=(EscalationDestination.EXTERNAL_WEBHOOK,),
    )

    assert event.destinations == (EscalationDestination.EXTERNAL_WEBHOOK,)
    events = router.list_events()
    assert len(events) == 1
    assert events[0].escalation_id == event.escalation_id
