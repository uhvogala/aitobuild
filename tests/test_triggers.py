from __future__ import annotations

from aitobuild.events import EventOrigin, EventType, make_internal_event
from aitobuild.triggers import DispatchResult, Dispatcher, InMemoryDedupeStore, TriggerEngine


class _TestDispatcher(Dispatcher):
    def route(self, _event):
        return DispatchResult(accepted=True, route="ok")


def test_trigger_engine_deduplicates() -> None:
    engine = TriggerEngine(dedupe_store=InMemoryDedupeStore())
    dispatcher = _TestDispatcher()

    event = make_internal_event(
        origin=EventOrigin.MANUAL_REQUEST,
        event_type=EventType.ARCHITECT_SCAN_REQUESTED,
        payload={},
        dedupe_key="same",
    )

    first = engine.dispatch(event, dispatcher=dispatcher)
    second = engine.dispatch(event, dispatcher=dispatcher)

    assert first.accepted is True
    assert second.accepted is False
    assert second.route == "dedupe"
