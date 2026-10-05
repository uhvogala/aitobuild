from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aitobuild.meetings import (
    MeetingDeadlineExceededError,
    MeetingState,
    MeetingValidationError,
    MeetingRegistry,
)


def test_meeting_registry_request_due_bootstrap_lifecycle() -> None:
    registry = MeetingRegistry()
    record = registry.request_meeting(
        {
            "agenda": "Code review resolution",
            "participants": ["Architect", "Developer"],
        }
    )

    assert record.state is MeetingState.REQUESTED

    due = registry.mark_due(record.meeting_id)
    assert due.state is MeetingState.DUE

    bootstrapped = registry.mark_bootstrapped(record.meeting_id)
    assert bootstrapped.state is MeetingState.BOOTSTRAPPED


def test_meeting_registry_rejects_invalid_participants() -> None:
    registry = MeetingRegistry()

    with pytest.raises(MeetingValidationError):
        registry.request_meeting({"agenda": "Planning", "participants": ["Architect"]})


def test_meeting_registry_deadline_exceeded_raises_specific_error() -> None:
    registry = MeetingRegistry()
    past_deadline = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
    record = registry.request_meeting(
        {
            "agenda": "Resolve blocker",
            "participants": ["Architect", "Developer"],
            "deadline": past_deadline,
        }
    )
    registry.mark_due(record.meeting_id)

    with pytest.raises(MeetingDeadlineExceededError):
        registry.mark_bootstrapped(record.meeting_id)
