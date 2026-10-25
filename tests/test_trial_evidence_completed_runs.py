"""#83: a phase whose service-recorded run did not complete never yields ``evidence.ok: true``.

Pilot run trial4-20261019-071847: the Architect review of PR #114 failed on a model content-filter 400, yet the
harness wrote ``paused_at_merge_point`` with ``evidence.ok: true``, a merge point with ``review_outcome: null`` and
the failed review under ``inherited_reviews``."""

from __future__ import annotations

import json
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
HUMAN = ApprovalPolicy()  # every stage human: admission checks have nothing to say, only the run state counts
CONTENT_FILTER = (
    "service failed to complete the prompt: Error code: 400 - {'finish_reason': 'content_filter', "
    "'message': \"Response content blocked by label 'MultiSeverity_SexualScore'.\"} "
    "[exception chain: agent_framework.ChatClientException <- openai.BadRequestError]"
)
#: Every phase kind that sets evidence.ok: planning, delivery, review, correction child and its re-review.
PHASES = {
    "planning": "pm-planning-x",
    "delivery:a": "planned-issue-x",
    "review:a": "published-review-x",
    "correction:a": "published-correction-x",
    "re-review:a": "published-review-y",
}


def _report(policy: ApprovalPolicy, labels: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {"phases": [], "sessions": [{"command": "start"}]}
    common.pin_session_policy(report, {"modes": policy.model_dump(mode="json"), "revision": policy.revision})
    for label in labels:
        report["phases"].append({"phase": label, "preview_id": label, "policy_revision": policy.revision})
    return report


def _run(policy: ApprovalPolicy, state: str = "completed", error: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=state, error=error, approval_policy_revision=policy.revision,
                           policy_overrides=(), decisions=())


def _preview(task_id: str, evidence_kind: str = "human", admission: Any = None,
             source: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(approval_policy=admission, evidence_kind=evidence_kind, bundle_payload={"task_id": task_id},
                           source_payload=source or {})


def _holder(previews: dict[str, Any], runs: dict[str, Any]) -> dict[str, Any]:
    return {"previews": SimpleNamespace(get=previews.get), "service": SimpleNamespace(status=runs.get)}


def _admission(policy: ApprovalPolicy) -> Any:
    return policy.resolve(ApprovalStage.TASK_ADMISSION, actor_id="operator", operation={}, automatic_supported=True,
                          source="operator_policy")


def _all_phases(failed: str | None, state: str = "failed") -> dict[str, Any]:
    report = _report(HUMAN, list(PHASES))
    previews = {label: _preview(task_id) for label, task_id in PHASES.items()}
    runs = {label: _run(HUMAN, state if label == failed else "completed", CONTENT_FILTER if label == failed else None)
            for label in PHASES}
    return common.collect_evidence(_holder(previews, runs), report, HUMAN)


def test_all_phases_completed_is_ok() -> None:
    evidence = _all_phases(None)
    assert evidence["ok"], evidence["problems"]
    assert [phase["run_state"] for phase in evidence["phases"]] == ["completed"] * len(PHASES)


@pytest.mark.parametrize("label", list(PHASES))
@pytest.mark.parametrize("state", ["failed", "cancelled", "running", "waiting"])
def test_a_phase_whose_service_run_did_not_complete_fails_the_evidence(label: str, state: str) -> None:
    evidence = _all_phases(label, state)
    assert not evidence["ok"]
    problem, = evidence["problems"]
    assert problem.startswith(f"{label}: service-recorded run is {state}, not completed")
    assert "content_filter" in problem  # the service's own error is carried into the problem
    assert {phase["phase"]: phase["run_state"] for phase in evidence["phases"]}[label] == state


def test_a_phase_without_a_service_run_fails_the_evidence() -> None:
    report = _report(HUMAN, ["delivery:a"])
    evidence = common.collect_evidence(_holder({"delivery:a": _preview("planned-issue-x")}, {}), report, HUMAN)
    assert not evidence["ok"] and evidence["problems"] == ["delivery:a: service-recorded run is missing, not completed"]


def test_the_harness_lists_are_not_trusted() -> None:
    """The harness's own ``run_state`` stamp says completed; the service record says failed. The service wins."""
    report = _report(HUMAN, ["review:a"])
    report["phases"][0]["run_state"] = "completed"
    evidence = common.collect_evidence(
        _holder({"review:a": _preview("published-review-x")}, {"review:a": _run(HUMAN, "failed", CONTENT_FILTER)}),
        report, HUMAN)
    assert not evidence["ok"]


def _pilot_071847(review_state: str, review_outcome: str | None) -> dict[str, Any]:
    """The 071847 shape: human planning, automatic planned delivery, automatic inherited review of its head."""
    labels = ["planning", "delivery:dp-b6530fff", "review:dp-b6530fff"]
    report = _report(AUTO, labels)
    report["merge_points"] = [{"label": "dp-b6530fff", "pull_number": 114, "review_outcome": review_outcome,
                               "state": "OPEN"}]
    target = {"published_review": {"preview_id": "delivery:dp-b6530fff", "head_sha": "46777f7"}}
    previews = {
        "planning": _preview("pm-planning-x"),
        "delivery:dp-b6530fff": _preview("planned-issue-x", "planned_child", _admission(AUTO)),
        "review:dp-b6530fff": _preview("published-review-x", "inherited_review", _admission(AUTO), target),
    }
    runs = {
        "planning": _run(AUTO),
        "delivery:dp-b6530fff": _run(AUTO),
        "review:dp-b6530fff": _run(AUTO, review_state, CONTENT_FILTER if review_state == "failed" else None),
    }
    return common.collect_evidence(_holder(previews, runs), report, AUTO)


def test_071847_failed_inherited_review_is_not_evidence() -> None:
    evidence = _pilot_071847("failed", None)
    assert not evidence["ok"]
    assert evidence["inherited_reviews"] == []  # only service-completed reviews are listed
    assert evidence["problems"][0].startswith("review:dp-b6530fff: service-recorded run is failed, not completed")
    assert "MultiSeverity_SexualScore" in evidence["problems"][0]
    assert evidence["problems"][1] == (
        "merge point dp-b6530fff (PR #114): review_outcome None is not a completed review")


def test_071847_shape_with_a_completed_review_is_evidence() -> None:
    evidence = _pilot_071847("completed", "no_correction")
    assert evidence["ok"], evidence["problems"]
    assert evidence["inherited_reviews"] == ["review:dp-b6530fff"]


@pytest.mark.parametrize("outcome", [None, "error", "escalated", "pending", "not_published"])
def test_a_merge_point_needs_a_completed_review_outcome(outcome: str | None) -> None:
    evidence = _pilot_071847("completed", outcome)
    assert not evidence["ok"] and "is not a completed review" in evidence["problems"][-1]


@pytest.mark.parametrize("outcome", sorted(common.REVIEWED_OUTCOMES))
def test_reviewed_outcomes_make_a_merge_point(outcome: str) -> None:
    assert _pilot_071847("completed", outcome)["ok"]


def test_incomplete_run_reads_service_status_json_too() -> None:
    assert common.incomplete_run("x", {"state": "completed"}) is None
    assert common.incomplete_run("x", {"state": "failed", "error": "boom"}) == (
        "x: service-recorded run is failed, not completed: boom")
    assert common.incomplete_run("x", None) == "x: service-recorded run is missing, not completed"
    with pytest.raises(RuntimeError, match="x: service-recorded run is cancelled"):
        common.require_completed({"managed_run": {"state": "cancelled"}}, "x")


# --- pilot.deliver_to_merge_point / pilot.phase: the session stops, no merge point is recorded ------------------


class _Hands:
    def __init__(self) -> None:
        self.children: list[str] = []
        self.actions: list[str] = []

    def child(self, preview_id: str, what: str, *, human_admission: bool = False) -> None:
        self.children.append(preview_id)

    def correction_child(self, preview_id: str, what: str, *, reason: str) -> None:
        self.children.append(preview_id)

    def action(self, kind: str, what: str, **fields: Any) -> None:
        self.actions.append(kind)


class _Operator:
    """Service answers by preview: the run status ``drive`` returns and the follow-up after it."""

    headers: dict[str, str] = {}

    def __init__(self, runs: dict[str, dict[str, Any]], followups: dict[str, dict[str, Any] | None]) -> None:
        self.runs, self.followups, self.posts = runs, followups, []  # type: ignore[var-annotated]

    def drive(self, preview_id: str, label: str) -> dict[str, Any]:
        return {"managed_run": self.runs[preview_id]}

    def wait_follow_up(self, preview_id: str, label: str, timeout: float = 120) -> dict[str, Any] | None:
        return self.followups.get(preview_id)

    def post(self, path: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.posts.append(path)
        if path.endswith("stage-publication"):
            return {"publish_approval": {"digest": "d"}}
        return {"review_preview": {"preview_id": "rereview"}}


class _Client:
    def get(self, path: str, headers: dict[str, str]) -> Any:
        publication = {"pull_number": 114, "branch": "aitobuild/issue-111", "head_sha": "46777f7"}
        return SimpleNamespace(json=lambda: {"delivery": {"publication": publication}})


def _deliver(monkeypatch: pytest.MonkeyPatch, runs: dict[str, dict[str, Any]],
             followups: dict[str, dict[str, Any] | None]) -> tuple[dict[str, Any], _Operator]:
    monkeypatch.setattr(pilot, "git", lambda *args: "")
    monkeypatch.setattr(pilot, "gh_json", lambda *args: {"url": "u", "headRefOid": "46777f7", "isDraft": True,
                                                         "state": "OPEN"})
    report: dict[str, Any] = {"phases": [], "approvals": [], "repository": "uhvogala/aitobuild_example"}
    operator = _Operator(runs, followups)
    pilot.deliver_to_merge_point(operator, _Hands(), report, _Client(), Path("/nonexistent"),  # type: ignore[arg-type]
                                 "delivery", "dp-b6530fff", None)
    return report, operator


DONE = {"state": "completed"}
FAILED = {"state": "failed", "error": CONTENT_FILTER}


def test_failed_review_records_its_phase_but_no_merge_point(monkeypatch: pytest.MonkeyPatch) -> None:
    report: dict[str, Any] = {"phases": [], "approvals": [], "repository": "r"}
    monkeypatch.setattr(pilot, "gh_json", lambda *args: pytest.fail("no merge point may be read from GitHub"))
    operator = _Operator({"delivery": DONE, "review": FAILED},
                         {"delivery": {"state": "staged", "next_preview_id": "review"}})
    with pytest.raises(RuntimeError, match="service-recorded run is failed"):
        pilot.deliver_to_merge_point(operator, _Hands(), report, _Client(), Path("/x"),  # type: ignore[arg-type]
                                     "delivery", "dp-b6530fff", None)
    assert "merge_points" not in report
    assert [(phase["phase"], phase["run_state"]) for phase in report["phases"]] == [
        ("delivery:dp-b6530fff", "completed"), ("review:dp-b6530fff", "failed")]


def test_completed_review_without_an_outcome_is_no_merge_point(monkeypatch: pytest.MonkeyPatch) -> None:
    report: dict[str, Any] = {"phases": [], "approvals": [], "repository": "r"}
    monkeypatch.setattr(pilot, "gh_json", lambda *args: pytest.fail("no merge point may be read from GitHub"))
    operator = _Operator({"delivery": DONE, "review": DONE},
                         {"delivery": {"state": "staged", "next_preview_id": "review"}, "review": None})
    with pytest.raises(RuntimeError, match="has no review outcome, so there is no merge point"):
        pilot.deliver_to_merge_point(operator, _Hands(), report, _Client(), Path("/x"),  # type: ignore[arg-type]
                                     "delivery", "dp-b6530fff", None)
    assert "merge_points" not in report


@pytest.mark.parametrize("failing", ["delivery", "correction", "rereview"])
def test_failed_delivery_correction_or_rereview_stops_the_session(monkeypatch: pytest.MonkeyPatch,
                                                                   failing: str) -> None:
    monkeypatch.setattr(pilot, "git", lambda *args: "")
    monkeypatch.setattr(pilot, "gh_json", lambda *args: pytest.fail("no merge point may be read from GitHub"))
    runs = {"delivery": DONE, "review": DONE, "correction": DONE, "rereview": DONE, failing: FAILED}
    operator = _Operator(runs, {"delivery": {"state": "staged", "next_preview_id": "review"},
                                "review": {"state": "staged", "next_preview_id": "correction"}})
    report: dict[str, Any] = {"phases": [], "approvals": [], "repository": "r"}
    with pytest.raises(RuntimeError, match="service-recorded run is failed"):
        pilot.deliver_to_merge_point(operator, _Hands(), report, _Client(), Path("/x"),  # type: ignore[arg-type]
                                     "delivery", "dp-b6530fff", None)
    assert "merge_points" not in report
    if failing == "correction":  # a failed correction is never staged for publication
        assert operator.posts == []


def test_completed_chain_reaches_the_merge_point(monkeypatch: pytest.MonkeyPatch) -> None:
    report, operator = _deliver(
        monkeypatch, {"delivery": DONE, "review": DONE, "correction": DONE, "rereview": DONE},
        {"delivery": {"state": "staged", "next_preview_id": "review"},
         "review": {"state": "staged", "next_preview_id": "correction"}})
    point, = report["merge_points"]
    assert point["review_outcome"] == "staged" and point["pull_number"] == 114
    assert [phase["run_state"] for phase in report["phases"]] == ["completed"] * 4


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_planning_phase_stops_on_an_incomplete_run(state: str) -> None:
    report: dict[str, Any] = {"phases": []}
    with pytest.raises(RuntimeError, match=f"PM planning task \\(planning\\): service-recorded run is {state}"):
        pilot.phase(_Operator({"plan": {"state": state}}, {}), _Hands(), report,  # type: ignore[arg-type]
                    "plan", "planning", "PM planning task", human_admission=True)
    assert report["phases"][0]["run_state"] == state


# --- followups.main driven end to end with a fake operator: the session stops, evidence is never collected -------


REPO = {"full_name": "uhvogala/aitobuild_example", "id": 1, "default_branch": "main"}


class _FollowupsOperator(_Operator):
    def __init__(self, runs: dict[str, dict[str, Any]], followups: dict[str, dict[str, Any] | None]) -> None:
        super().__init__(runs, followups)
        self.recorder: Any = None


class _FollowupsHands(_Hands):
    def planned_children(self) -> list[str]:
        return ["delivery"]


class _TestClient:
    def __init__(self, app: Any) -> None:
        pass

    def __enter__(self) -> _Client:
        return _Client()

    def __exit__(self, *exc: Any) -> None:
        pass


def _followups_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runs: dict[str, dict[str, Any]],
                    followups_by_preview: dict[str, dict[str, Any] | None]) -> tuple[Any, dict[str, Any], _Operator]:
    """Run ``followups.main`` with every outside effect (GitHub, git, the service app) replaced; returns its exit code
    (or the exception it raised), the saved report and the fake operator."""
    import aitobuild.app
    import aitobuild.config
    import fastapi.testclient

    operator = _FollowupsOperator(runs, followups_by_preview)
    evidence_calls: list[dict[str, Any]] = []

    def operator_factory(client: Any, token: str, recorder: Any, *args: Any) -> _Operator:
        operator.recorder = recorder
        return operator

    def collect(holder: Any, report: dict[str, Any], policy: Any) -> dict[str, Any]:
        evidence_calls.append(report)
        return {"ok": True, "problems": []}

    planning = SimpleNamespace(offer=lambda **_: SimpleNamespace(preview_id="plan"))
    monkeypatch.setattr(sys, "argv", ["followups.py", "--work-root", str(tmp_path), "--poll-seconds", "0.01",
                                      "--allowed-issue-author", "uhvogala"])
    monkeypatch.setattr(followups, "gh_json", lambda *args: REPO)
    monkeypatch.setattr(followups, "git", lambda *args: "a" * 40)
    monkeypatch.setattr(followups, "subprocess", SimpleNamespace(run=lambda *args, **kwargs: None))
    monkeypatch.setattr(followups, "os", SimpleNamespace(chdir=lambda path: None))
    monkeypatch.setattr(followups, "build_trial_environment", lambda *args, **kwargs: {})
    monkeypatch.setattr(followups, "configure_trial_environment", lambda **kwargs: None)
    monkeypatch.setattr(followups, "check_policy", lambda *args: {"revision": "r"})
    monkeypatch.setattr(followups, "build_factories",
                        lambda **kwargs: (None, None, {"planning_admission": planning}))
    monkeypatch.setattr(followups, "Operator", operator_factory)
    monkeypatch.setattr(followups, "HandsOff", lambda *args: _FollowupsHands())
    monkeypatch.setattr(followups, "collect_evidence", collect)
    monkeypatch.setattr(aitobuild.config, "load_config", lambda: SimpleNamespace(approvals=HUMAN))
    monkeypatch.setattr(aitobuild.app, "create_app", lambda *args, **kwargs: None)
    monkeypatch.setattr(fastapi.testclient, "TestClient", _TestClient)
    outcome: Any
    try:
        outcome = followups.main()
    except RuntimeError as error:
        outcome = error
    report_path, = (tmp_path / "artifacts").glob("trial2-*/report.json")
    report = json.loads(report_path.read_text())
    assert bool(evidence_calls) == (outcome == 0)  # evidence is collected only when the session went all the way
    return outcome, report, operator


FOLLOWUP_RUNS = {"plan": DONE, "delivery": DONE, "review": DONE, "correction": DONE, "rereview": DONE}
TO_REVIEW = {"delivery": {"state": "staged", "next_preview_id": "review"}}
TO_CORRECTION = {**TO_REVIEW, "review": {"state": "staged", "next_preview_id": "correction"}}


@pytest.mark.parametrize("outcome", [None, "error", "escalated"])
def test_followups_completed_review_without_an_outcome_stops_the_session(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str | None) -> None:
    review_followup = None if outcome is None else {"state": outcome}
    result, report, operator = _followups_main(monkeypatch, tmp_path, FOLLOWUP_RUNS,
                                               {**TO_REVIEW, "review": review_followup})
    assert isinstance(result, RuntimeError) and "Architect review has no review outcome" in str(result)
    assert report["result"] == "fail" and "evidence" not in report
    assert report["review_outcome"] == outcome
    assert operator.posts == []  # nothing is staged for publication


@pytest.mark.parametrize("followups_by_preview, posts", [
    ({**TO_REVIEW, "review": {"state": "no_correction"}}, 0),
    (TO_CORRECTION, 2),
])
def test_followups_completed_review_with_an_outcome_passes(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, followups_by_preview: dict[str, Any], posts: int) -> None:
    result, report, operator = _followups_main(monkeypatch, tmp_path, FOLLOWUP_RUNS, followups_by_preview)
    assert result == 0 and report["result"] == "pass" and report["evidence"]["ok"]
    assert report["review_outcome"] in common.REVIEWED_OUTCOMES
    assert len(operator.posts) == posts
    assert {phase["run_state"] for phase in report["phases"]} == {"completed"}


@pytest.mark.parametrize("failing, message", [
    ("plan", "PM planning task: service-recorded run is failed"),
    ("delivery", "Planned Developer child: service-recorded run is failed"),
    ("review", "Architect review: service-recorded run is failed"),
    ("correction", "Correction: service-recorded run is failed"),
    ("rereview", "Architect re-review: service-recorded run is failed"),
])
def test_followups_incomplete_run_stops_the_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                    failing: str, message: str) -> None:
    result, report, operator = _followups_main(monkeypatch, tmp_path, {**FOLLOWUP_RUNS, failing: FAILED},
                                               TO_CORRECTION)
    assert isinstance(result, RuntimeError) and message in str(result)
    assert report["result"] == "fail" and "evidence" not in report
    assert report["phases"][-1]["run_state"] == "failed"
    if failing == "correction":  # a failed correction is never staged for publication
        assert operator.posts == []


@pytest.mark.parametrize("outcome", [None, "error", "escalated"])
def test_a_session_review_outcome_must_be_a_completed_reviews(outcome: str | None) -> None:
    report = _report(HUMAN, ["review:a"])
    report["review_outcome"] = outcome
    evidence = common.collect_evidence(
        _holder({"review:a": _preview("published-review-x")}, {"review:a": _run(HUMAN)}), report, HUMAN)
    assert not evidence["ok"]
    assert evidence["problems"] == [f"review_outcome {outcome!r} is not a completed review"]


@pytest.mark.parametrize("outcome", sorted(common.REVIEWED_OUTCOMES))
def test_a_reviewed_session_outcome_is_evidence(outcome: str) -> None:
    report = _report(HUMAN, ["review:a"])
    report["review_outcome"] = outcome
    assert common.collect_evidence(
        _holder({"review:a": _preview("published-review-x")}, {"review:a": _run(HUMAN)}), report, HUMAN)["ok"]
