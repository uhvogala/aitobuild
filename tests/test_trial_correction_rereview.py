"""Harness-only: a correction's re-review is human-admitted as a scripted step unless `correction` is automatic.

Vertti's Oct 12 rule and #39: a correction's re-review inherits from the correction, so it is automatic only when
corrections are automatic. Pilot trial4-20261019-021602 sent the run trigger for it and got a 409 instead."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))

import common  # noqa: E402
import followups  # noqa: E402
import pilot  # noqa: E402

AUTO = ApprovalPolicy(task_admission="automatic", developer_tools="automatic")  # the pilot's flags
AUTO_CORRECTION = ApprovalPolicy(task_admission="automatic", developer_tools="automatic", correction="automatic")


def modes(policy: ApprovalPolicy) -> dict[str, Any]:
    return {"modes": policy.model_dump(mode="json"), "revision": policy.revision}


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, name: str, **fields: Any) -> None:
        self.events.append((name, fields))

    def save(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeOperator:
    """Records every call in order; ``admitted_at`` makes the worker admit the task once the clock reaches it."""

    def __init__(self, clock: Clock, automatic: bool = True, admitted_at: float | None = None) -> None:
        self.clock, self.automatic, self.poll, self.recorder = clock, automatic, 5.0, FakeRecorder()
        self.admitted_at = admitted_at
        self.calls: list[tuple[str, str, float]] = []
        self.human = False

    def task(self, preview_id: str) -> dict[str, Any]:
        self.calls.append(("task", preview_id, self.clock.now))
        if self.human or (self.admitted_at is not None and self.clock.now >= self.admitted_at):
            return {"managed_admission": {"state": "queued"}}
        return {}

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("post:" + path, body["preview_id"], self.clock.now))
        return {}

    def approve_preview(self, preview_id: str, what: str) -> dict[str, Any]:
        self.calls.append(("approve", preview_id, self.clock.now))
        self.human = True
        return {"managed_admission": {"state": "queued"}}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(followups.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(followups.time, "sleep", fake.sleep)
    return fake


def hands_for(operator: FakeOperator, policy: ApprovalPolicy, pickup: float = 90.0):
    report: dict[str, Any] = {"approval_policy": modes(policy)}
    return followups.HandsOff(operator, report, pickup), report  # type: ignore[arg-type]


def test_rereview_is_human_admitted_only_after_the_gate(clock: Clock) -> None:
    operator = FakeOperator(clock)
    hands, report = hands_for(operator, AUTO)
    hands.correction_child("rr-1", "Architect re-review a", reason="correction_rereview")
    approvals = [call for call in operator.calls if call[0] == "approve"]
    assert approvals == [("approve", "rr-1", 90.0)]  # admitted once, after the full gate
    polls = [call for call in operator.calls if call[0] == "task"]
    assert polls[0][2] == 0.0 and polls[-1][2] == 90.0 and operator.calls[-1][0] == "approve"
    assert not [call for call in operator.calls if call[0].startswith("post:")]  # no run trigger, no 409 path
    entry, = report["scripted_human_admissions"]
    assert entry["kind"] == "scripted_human_admission" == common.SCRIPTED_HUMAN_ADMISSION
    assert entry["reason"] == "correction_rereview" and entry["preview_id"] == "rr-1"
    assert entry["auto_admitted"] is False and entry["evidence_kind"] == "human" and entry["gate_seconds"] == 90.0
    assert [a["kind"] for a in report["operator_actions"]] == ["scripted_human_admission"]
    assert report["worker_pickups"] == []  # never recorded as an automatic pickup


def test_nothing_admits_the_rereview_before_the_gate_ends(clock: Clock) -> None:
    operator = FakeOperator(clock, admitted_at=30.0)  # something admits it without a human
    hands, report = hands_for(operator, AUTO)
    with pytest.raises(RuntimeError, match="admitted without a human"):
        hands.correction_child("rr-1", "Architect re-review a", reason="correction_rereview")
    assert not [call for call in operator.calls if call[0] == "approve" or call[0].startswith("post:")]
    entry, = report["scripted_human_admissions"]
    assert entry["auto_admitted"] is True and "evidence_kind" not in entry
    assert report["operator_actions"] == []


def test_correction_gate_uses_the_same_scripted_human_step(clock: Clock) -> None:
    operator = FakeOperator(clock)
    hands, report = hands_for(operator, AUTO, pickup=10.0)
    hands.correction_child("c-1", "Correction a", reason="correction")
    assert [call[:2] for call in operator.calls if call[0] != "task"] == [("approve", "c-1")]
    assert report["scripted_human_admissions"][0]["reason"] == "correction"


def test_automatic_correction_policy_waits_for_the_worker_instead(clock: Clock) -> None:
    operator = FakeOperator(clock, admitted_at=15.0)
    hands, report = hands_for(operator, AUTO_CORRECTION)
    hands.correction_child("rr-1", "Architect re-review a", reason="correction_rereview")
    assert not [call for call in operator.calls if call[0] != "task"]  # no human admission, no trigger
    assert "scripted_human_admissions" not in report
    assert [item["preview_id"] for item in report["worker_pickups"]] == ["rr-1"]


def test_pilot_gates_the_correction_and_its_rereview() -> None:
    calls: list[tuple[str, str, str | None]] = []
    hands = SimpleNamespace(
        child=lambda preview_id, what, **kw: calls.append(("child", preview_id, None)),
        correction_child=lambda preview_id, what, *, reason: calls.append(("gate", preview_id, reason)),
    )
    operator = SimpleNamespace(drive=lambda preview_id, label: {"managed_run": {"state": "completed"}})
    report: dict[str, Any] = {"phases": [], "approval_policy": modes(AUTO)}
    pilot.phase(operator, hands, report, "c-1", "correction:a", "Correction a", correction_admission="correction")
    pilot.phase(operator, hands, report, "rr-1", "re-review:a", "Architect re-review a",
                correction_admission="correction_rereview")
    pilot.phase(operator, hands, report, "r-1", "review:a", "Architect review a")
    assert calls == [("gate", "c-1", "correction"), ("gate", "rr-1", "correction_rereview"), ("child", "r-1", None)]
    source = Path(pilot.__file__).read_text()
    assert 'correction_admission="correction",' in source and 'correction_admission="correction_rereview",' in source
    trial2 = Path(followups.__file__).read_text()
    assert 'hands.correction_child(correction_id, "Correction", reason="correction")' in trial2
    assert 'rereview_id, "Architect re-review", reason="correction_rereview"' in trial2


# --- evidence (common.collect_evidence) -------------------------------------------------------------------------


def _admission(policy: ApprovalPolicy) -> Any:
    return policy.resolve(ApprovalStage.TASK_ADMISSION, actor_id="operator", operation={}, automatic_supported=True,
                          source="operator_policy")


def _preview(task_id: str, evidence_kind: str, admission: Any = None, source: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(approval_policy=admission, evidence_kind=evidence_kind, bundle_payload={"task_id": task_id},
                           source_payload=source or {})


def _evidence(policy: ApprovalPolicy, rereview: Any, scripted: list[dict[str, Any]], parent: str = "corr") -> dict:
    report: dict[str, Any] = {"phases": [], "sessions": [{"command": "start"}]}
    common.pin_session_policy(report, modes(policy))
    report["phases"].append({"phase": "re-review:a", "preview_id": "rr", "policy_revision": policy.revision})
    if scripted:
        report["scripted_human_admissions"] = scripted
    previews = {
        "rr": rereview,
        "corr": _preview("published-correction-x", "human", source={"correction_from_review": "rev"}),
        "dev": _preview("planned-issue-x", "planned_child", _admission(policy)),
    }
    # #83: evidence counts a phase only when its service-recorded run completed.
    run = SimpleNamespace(state="completed", approval_policy_revision=policy.revision, policy_overrides=(),
                          decisions=())
    holder = {"previews": SimpleNamespace(get=previews.get), "service": SimpleNamespace(status={"rr": run}.get)}
    return common.collect_evidence(holder, report, policy)


TARGET = {"published_review": {"preview_id": "corr", "head_sha": "eb45e84"}}
SCRIPTED = {"kind": "scripted_human_admission", "preview_id": "rr", "reason": "correction_rereview",
            "auto_admitted": False, "evidence_kind": "human"}


def test_evidence_expects_the_scripted_human_admission_for_a_correction_rereview() -> None:
    evidence = _evidence(AUTO, _preview("published-review-x", "human", source=TARGET), [SCRIPTED])
    assert evidence["ok"], evidence["problems"]
    phase, = evidence["phases"]
    assert phase["admission_path"] == "scripted_human_admission" and phase["rereviews_correction"] is True
    assert evidence["scripted_human_admissions"] == ["rr"]


def test_scripted_human_admission_is_never_counted_as_inherited_review() -> None:
    evidence = _evidence(AUTO, _preview("published-review-x", "human", source=TARGET), [SCRIPTED])
    assert evidence["inherited_reviews"] == []
    # A review of a delivery (not a correction) must inherit automatically; a scripted human step there fails.
    plain = {"published_review": {"preview_id": "dev", "head_sha": "b4e1878"}}
    evidence = _evidence(AUTO, _preview("published-review-y", "inherited_review", _admission(AUTO), plain), [SCRIPTED])
    assert not evidence["ok"] and "scripted human admission recorded" in evidence["problems"][0]
    assert evidence["phases"][0]["admission_path"] == "scripted_human_admission"
    assert evidence["inherited_reviews"] == []
    # The same review without the scripted step is the one counted as inherited.
    evidence = _evidence(AUTO, _preview("published-review-y", "inherited_review", _admission(AUTO), plain), [])
    assert evidence["ok"], evidence["problems"]
    assert evidence["inherited_reviews"] == ["re-review:a"]


@pytest.mark.parametrize("scripted", [
    [],
    [{**SCRIPTED, "auto_admitted": True}],
    [{**SCRIPTED, "kind": "human_task_approval_enqueued"}],
    [{**SCRIPTED, "evidence_kind": None}],
])
def test_correction_rereview_without_a_clean_scripted_step_fails(scripted: list[dict[str, Any]]) -> None:
    evidence = _evidence(AUTO, _preview("published-review-x", "human", source=TARGET), scripted)
    assert not evidence["ok"] and "scripted human admission" in evidence["problems"][0]


def test_correction_rereview_admitted_automatically_fails_under_human_correction() -> None:
    evidence = _evidence(AUTO, _preview("published-review-x", "inherited_review", _admission(AUTO), TARGET), [])
    assert not evidence["ok"] and "not human-admitted" in evidence["problems"][0]


def test_automatic_correction_policy_expects_automatic_inheritance() -> None:
    rereview = _preview("published-review-x", "inherited_review", _admission(AUTO_CORRECTION), TARGET)
    evidence = _evidence(AUTO_CORRECTION, rereview, [])
    assert evidence["ok"], evidence["problems"]
    assert evidence["inherited_reviews"] == ["re-review:a"]
    assert not _evidence(AUTO_CORRECTION, rereview, [SCRIPTED])["ok"]  # no scripted human step there
    human = _evidence(AUTO_CORRECTION, _preview("published-review-x", "human", source=TARGET), [SCRIPTED])
    assert not human["ok"]
    assert common.expected_evidence_kind("published-review-x", TARGET, True, True) == "inherited_review"
    assert common.expected_evidence_kind("published-review-x", TARGET, False, True) == "human"
    assert common.expected_evidence_kind("published-review-x", TARGET, False, False) == "inherited_review"


def test_reviews_correction_uses_the_full_correction_rule() -> None:
    previews = {"corr": _preview("ISSUE-1", "human", source={"correction_from_review": "r"}),
                "dev": _preview("planned-issue-x", "planned_child")}
    assert common.reviews_correction({"published_review": {"preview_id": "corr"}}, previews.get)
    assert not common.reviews_correction({"published_review": {"preview_id": "dev"}}, previews.get)
    assert not common.reviews_correction({"published_review": {"preview_id": "gone"}}, previews.get)
    assert not common.reviews_correction({}, previews.get)
