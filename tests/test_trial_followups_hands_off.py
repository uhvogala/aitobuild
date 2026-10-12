"""HandsOff admission paths in scripts/trials/followups.py (no network)."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))

import followups  # noqa: E402


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, name: str, **fields: Any) -> None:
        self.events.append((name, fields))

    def save(self) -> None:
        pass


class FakeOperator:
    def __init__(self, automatic: bool) -> None:
        self.automatic, self.poll, self.recorder = automatic, 0.0, FakeRecorder()
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.approvals: list[str] = []

    def task(self, preview_id: str) -> dict[str, Any]:
        return {}

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, body))
        return {}

    def approve_preview(self, preview_id: str, what: str) -> dict[str, Any]:
        self.approvals.append(preview_id)
        return {"managed_admission": {"state": "admitted"}}


@pytest.mark.parametrize("automatic", [True, False])
def test_planning_task_gets_human_admission_in_every_mode(automatic: bool) -> None:
    operator, report = FakeOperator(automatic), {}
    hands = followups.HandsOff(operator, report, pickup_seconds=0)  # type: ignore[arg-type]
    hands.child("plan-1", "PM planning task", human_admission=True)
    assert operator.approvals == ["plan-1"] and operator.posts == []
    assert [a["kind"] for a in report["operator_actions"]] == ["human_task_approval_enqueued"]
    assert report["operator_actions"][0]["admission"] == "admitted"


def test_automatic_child_still_uses_run_trigger() -> None:
    operator, report = FakeOperator(True), {}
    hands = followups.HandsOff(operator, report, pickup_seconds=0)  # type: ignore[arg-type]
    hands.child("child-1", "Planned Developer child")
    assert operator.posts == [("/internal/organization/tasks/run", {"preview_id": "child-1"})]
    assert operator.approvals == []


def test_run_planning_admits_planning_task_as_human() -> None:
    source = (Path(followups.__file__)).read_text()
    assert 'hands.child(planning.preview_id, "PM planning task", human_admission=True)' in source
