from __future__ import annotations

import pytest

from aitobuild.events import (
    EventOrigin,
    EventType,
    make_internal_event,
    normalize_github_webhook,
    parse_trigger_request,
)


def test_make_internal_event_defaults() -> None:
    event = make_internal_event(
        origin=EventOrigin.MANUAL_REQUEST,
        event_type=EventType.ARCHITECT_SCAN_REQUESTED,
        payload={"x": 1},
    )

    assert event.envelope.origin is EventOrigin.MANUAL_REQUEST
    assert event.envelope.event_type is EventType.ARCHITECT_SCAN_REQUESTED
    assert event.envelope.correlation_id.startswith("corr-")


def test_normalize_github_webhook() -> None:
    event = normalize_github_webhook(
        github_event="issues",
        action="opened",
        delivery_id="abc",
        body={"action": "opened"},
    )

    assert event.envelope.origin is EventOrigin.GITHUB_WEBHOOK
    assert event.envelope.event_type is EventType.WEBHOOK_EVENT_RECEIVED
    assert event.envelope.dedupe_key == "github:abc"


def test_parse_trigger_request_invalid_payload_shape() -> None:
    with pytest.raises(ValueError):
        parse_trigger_request(
            {
                "origin": "manual_request",
                "event_type": "architect_scan_requested",
                "payload": "nope",
            }
        )
