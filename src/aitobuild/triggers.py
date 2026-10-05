"""Trigger handling for webhook and internal events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aitobuild.events import InternalEvent


@dataclass(slots=True, frozen=True)
class DispatchResult:
    accepted: bool
    route: str
    reason: str | None = None
    metadata: dict[str, Any] | None = None


class DedupeStore:
    def seen(self, dedupe_key: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class InMemoryDedupeStore(DedupeStore):
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen(self, dedupe_key: str) -> bool:
        if dedupe_key in self._seen:
            return True
        self._seen.add(dedupe_key)
        return False


class TriggerEngine:
    """Single entrypoint for webhook and non-webhook trigger handling."""

    def __init__(self, *, dedupe_store: DedupeStore) -> None:
        self._dedupe_store = dedupe_store

    def dispatch(self, event: InternalEvent, *, dispatcher: "Dispatcher") -> DispatchResult:
        if dispatcher.uses_durable_dedupe(event):
            return dispatcher.route(event)
        dedupe_key = event.envelope.dedupe_key
        if self._dedupe_store.seen(dedupe_key):
            return DispatchResult(accepted=False, route="dedupe", reason="duplicate event")

        return dispatcher.route(event)


class Dispatcher:
    def uses_durable_dedupe(self, event: InternalEvent) -> bool:
        return False

    def route(self, event: InternalEvent) -> DispatchResult:  # pragma: no cover - interface
        raise NotImplementedError
