"""Meeting request registry and bootstrap event support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class MeetingValidationError(ValueError):
    pass


class MeetingDeadlineExceededError(MeetingValidationError):
    pass


class MeetingState(StrEnum):
    REQUESTED = "requested"
    DUE = "due"
    BOOTSTRAPPED = "bootstrapped"


@dataclass(slots=True)
class MeetingRecord:
    meeting_id: str
    agenda: str
    participants: tuple[str, ...]
    state: MeetingState
    deadline: datetime | None = None


class MeetingRegistry:
    def __init__(self) -> None:
        self._records: dict[str, MeetingRecord] = {}

    def add(self, record: MeetingRecord) -> None:
        self._records[record.meeting_id] = record

    def get(self, meeting_id: str) -> MeetingRecord | None:
        return self._records.get(meeting_id)

    def request_meeting(self, payload: dict[str, object]) -> MeetingRecord:
        agenda = _parse_agenda(payload.get("agenda"))
        participants = _parse_participants(payload.get("participants"))
        deadline = _parse_deadline(payload.get("deadline"))
        meeting_id = _parse_or_generate_meeting_id(payload.get("meeting_id"))

        record = MeetingRecord(
            meeting_id=meeting_id,
            agenda=agenda,
            participants=participants,
            state=MeetingState.REQUESTED,
            deadline=deadline,
        )
        self.add(record)
        return record

    def mark_due(self, meeting_id: str) -> MeetingRecord:
        record = self.get(meeting_id)
        if record is None:
            raise MeetingValidationError("meeting_id does not exist")
        if record.state is MeetingState.BOOTSTRAPPED:
            raise MeetingValidationError("meeting is already bootstrapped")

        updated = MeetingRecord(
            meeting_id=record.meeting_id,
            agenda=record.agenda,
            participants=record.participants,
            state=MeetingState.DUE,
            deadline=record.deadline,
        )
        self.add(updated)
        return updated

    def mark_bootstrapped(self, meeting_id: str) -> MeetingRecord:
        record = self.get(meeting_id)
        if record is None:
            raise MeetingValidationError("meeting_id does not exist")
        if record.state is not MeetingState.DUE:
            raise MeetingValidationError("meeting must be in due state before bootstrap")

        now = datetime.now(tz=UTC)
        if record.deadline is not None and now > record.deadline:
            raise MeetingDeadlineExceededError("meeting deadline has passed")

        updated = MeetingRecord(
            meeting_id=record.meeting_id,
            agenda=record.agenda,
            participants=record.participants,
            state=MeetingState.BOOTSTRAPPED,
            deadline=record.deadline,
        )
        self.add(updated)
        return updated


def _parse_agenda(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MeetingValidationError("meeting agenda must be a non-empty string")
    return value.strip()


def _parse_participants(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MeetingValidationError("meeting participants must be a list")

    participants = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    unique = tuple(dict.fromkeys(participants))
    if len(unique) < 2:
        raise MeetingValidationError("meeting requires at least two unique participants")
    return unique


def _parse_deadline(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MeetingValidationError("meeting deadline must be an ISO datetime string")

    deadline_raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(deadline_raw)
    except ValueError as exc:
        raise MeetingValidationError("meeting deadline must be ISO-8601") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_or_generate_meeting_id(value: object) -> str:
    if value is None:
        return f"meet-{uuid4()}"
    if not isinstance(value, str) or not value.strip():
        raise MeetingValidationError("meeting_id must be a non-empty string")
    return value.strip()
