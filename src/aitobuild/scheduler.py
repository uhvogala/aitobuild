"""Scheduler scaffolding for proactive scans and meeting triggers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from croniter import croniter

from aitobuild.config import SchedulerConfig
from aitobuild.events import EventOrigin, EventType, InternalEvent, make_internal_event


@dataclass(slots=True, frozen=True)
class SchedulerTickResult:
    produced: tuple[InternalEvent, ...]


class Scheduler:
    """Phase 1 scheduler that emits trigger events with cron cadence controls."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        kill_switch: bool = False,
        architect_scan_cron: str = "0 8 * * *",
        meeting_tick_cron: str = "*/30 * * * *",
        max_concurrent_proactive_jobs: int = 2,
        quiet_window_start_hour: int | None = None,
        quiet_window_end_hour: int | None = None,
    ) -> None:
        if not croniter.is_valid(architect_scan_cron):
            raise ValueError("Invalid architect_scan_cron expression")
        if not croniter.is_valid(meeting_tick_cron):
            raise ValueError("Invalid meeting_tick_cron expression")
        if max_concurrent_proactive_jobs < 1:
            raise ValueError("max_concurrent_proactive_jobs must be >= 1")
        if (quiet_window_start_hour is None) != (quiet_window_end_hour is None):
            raise ValueError("quiet window requires both start and end hour")

        self._enabled = enabled
        self._kill_switch = kill_switch
        self._architect_scan_cron = architect_scan_cron
        self._meeting_tick_cron = meeting_tick_cron
        self._max_concurrent_proactive_jobs = max_concurrent_proactive_jobs
        self._quiet_window_start_hour = quiet_window_start_hour
        self._quiet_window_end_hour = quiet_window_end_hour
        self._last_scan_emitted_at: datetime | None = None
        self._last_meeting_emitted_at: datetime | None = None

    def run_tick(
        self,
        *,
        now: datetime | None = None,
        manual_scan: bool = False,
        manual_meeting: bool = False,
        meeting_id: str | None = None,
        current_proactive_jobs: int = 0,
    ) -> SchedulerTickResult:
        tick_now = now or datetime.now(tz=UTC)

        if self._kill_switch:
            return SchedulerTickResult(produced=())

        # Manual flags are an explicit override when disabled.
        if not self._enabled and not (manual_scan or manual_meeting):
            return SchedulerTickResult(produced=())

        produced: list[InternalEvent] = []

        in_quiet_window = self._is_in_quiet_window(tick_now)
        scan_due = self._is_due(
            cron_expr=self._architect_scan_cron,
            last_emitted=self._last_scan_emitted_at,
            now=tick_now,
        )
        meeting_due = self._is_due(
            cron_expr=self._meeting_tick_cron,
            last_emitted=self._last_meeting_emitted_at,
            now=tick_now,
        )

        should_emit_scan = (
            (manual_scan or (scan_due and not in_quiet_window))
            and current_proactive_jobs < self._max_concurrent_proactive_jobs
        )
        should_emit_meeting = manual_meeting or (meeting_due and not in_quiet_window)

        if should_emit_scan:
            bucket = tick_now.strftime("%Y%m%d%H%M")
            produced.append(
                make_internal_event(
                    origin=EventOrigin.SCHEDULER,
                    event_type=EventType.ARCHITECT_SCAN_REQUESTED,
                    payload={
                        "source": "manual" if manual_scan else "scheduled",
                        "emitted_at": tick_now.isoformat(),
                    },
                    dedupe_key=f"scheduler:architect_scan:{bucket}",
                )
            )
            self._last_scan_emitted_at = tick_now

        if should_emit_meeting:
            bucket = tick_now.strftime("%Y%m%d%H%M")
            meeting_payload: dict[str, str] = {
                "source": "manual" if manual_meeting else "scheduled",
                "emitted_at": tick_now.isoformat(),
            }
            if meeting_id is not None and meeting_id.strip():
                meeting_payload["meeting_id"] = meeting_id.strip()

            produced.append(
                make_internal_event(
                    origin=EventOrigin.SCHEDULER,
                    event_type=EventType.MEETING_DUE,
                    payload=meeting_payload,
                    dedupe_key=f"scheduler:meeting_due:{bucket}",
                )
            )
            self._last_meeting_emitted_at = tick_now

        return SchedulerTickResult(produced=tuple(produced))

    def _is_in_quiet_window(self, now: datetime) -> bool:
        if self._quiet_window_start_hour is None or self._quiet_window_end_hour is None:
            return False

        hour = now.hour
        start = self._quiet_window_start_hour
        end = self._quiet_window_end_hour

        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _is_due(self, *, cron_expr: str, last_emitted: datetime | None, now: datetime) -> bool:
        base = last_emitted or (now - timedelta(days=1))
        next_due = croniter(cron_expr, base).get_next(datetime)
        if next_due.tzinfo is None:
            next_due = next_due.replace(tzinfo=UTC)
        return next_due <= now


EventConsumer = Callable[[InternalEvent], None]


def scheduler_from_config(config: SchedulerConfig) -> Scheduler:
    return Scheduler(
        enabled=config.enabled,
        kill_switch=config.kill_switch,
        architect_scan_cron=config.architect_scan_cron,
        meeting_tick_cron=config.meeting_tick_cron,
        max_concurrent_proactive_jobs=config.max_concurrent_proactive_jobs,
        quiet_window_start_hour=config.quiet_window_start_hour,
        quiet_window_end_hour=config.quiet_window_end_hour,
    )


def emit_scheduled_events(result: SchedulerTickResult, *, consumer: EventConsumer) -> None:
    for event in result.produced:
        consumer(event)
