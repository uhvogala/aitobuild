from __future__ import annotations

from datetime import UTC, datetime

from aitobuild.events import EventType
from aitobuild.scheduler import Scheduler


def test_scheduler_manual_tick_emits_scan_and_meeting() -> None:
    scheduler = Scheduler(enabled=True)
    result = scheduler.run_tick(manual_scan=True, manual_meeting=True)

    types = {event.envelope.event_type for event in result.produced}
    assert EventType.ARCHITECT_SCAN_REQUESTED in types
    assert EventType.MEETING_DUE in types


def test_scheduler_disabled_emits_no_events() -> None:
    scheduler = Scheduler(enabled=False)
    result = scheduler.run_tick(manual_scan=True, manual_meeting=True)
    assert len(result.produced) == 2


def test_scheduler_kill_switch_blocks_events() -> None:
    scheduler = Scheduler(enabled=True, kill_switch=True)
    result = scheduler.run_tick(manual_scan=True, manual_meeting=True)
    assert result.produced == ()


def test_scheduler_cron_emits_due_events() -> None:
    scheduler = Scheduler(
        enabled=True,
        architect_scan_cron="*/5 * * * *",
        meeting_tick_cron="*/10 * * * *",
    )
    now = datetime(2026, 4, 10, 8, 10, tzinfo=UTC)
    result = scheduler.run_tick(now=now)
    types = {event.envelope.event_type for event in result.produced}
    assert EventType.ARCHITECT_SCAN_REQUESTED in types
    assert EventType.MEETING_DUE in types


def test_scheduler_quiet_window_suppresses_scheduled_events() -> None:
    scheduler = Scheduler(
        enabled=True,
        architect_scan_cron="* * * * *",
        meeting_tick_cron="* * * * *",
        quiet_window_start_hour=0,
        quiet_window_end_hour=9,
    )
    now = datetime(2026, 4, 10, 8, 10, tzinfo=UTC)
    result = scheduler.run_tick(now=now)
    assert result.produced == ()


def test_scheduler_manual_override_bypasses_quiet_window() -> None:
    scheduler = Scheduler(
        enabled=True,
        quiet_window_start_hour=0,
        quiet_window_end_hour=23,
    )
    now = datetime(2026, 4, 10, 8, 10, tzinfo=UTC)
    result = scheduler.run_tick(now=now, manual_scan=True)
    types = {event.envelope.event_type for event in result.produced}
    assert EventType.ARCHITECT_SCAN_REQUESTED in types


def test_scheduler_respects_concurrency_for_scan() -> None:
    scheduler = Scheduler(
        enabled=True,
        architect_scan_cron="* * * * *",
        meeting_tick_cron="* * * * *",
        max_concurrent_proactive_jobs=1,
    )
    now = datetime(2026, 4, 10, 8, 10, tzinfo=UTC)
    result = scheduler.run_tick(now=now, current_proactive_jobs=1)
    types = {event.envelope.event_type for event in result.produced}
    assert EventType.ARCHITECT_SCAN_REQUESTED not in types
    assert EventType.MEETING_DUE in types
