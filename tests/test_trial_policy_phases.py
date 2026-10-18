"""#65 trial harness: each phase is judged by its own recorded policy, and a resume cannot silently change it."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))

import common  # noqa: E402
import pilot  # noqa: E402

START = ApprovalPolicy(task_admission="automatic", developer_tools="automatic")  # trial4 start flags
RESUME = ApprovalPolicy()  # resumed without --automatic-stage


def policy_report(policy):
    return {"modes": policy.model_dump(mode="json"), "revision": policy.revision}


def fresh_report():
    return {"phases": [], "sessions": [{"command": "start"}]}


def test_first_session_pins_and_the_same_flags_resume():
    report = fresh_report()
    common.pin_session_policy(report, policy_report(START))
    assert report["pinned_policy"]["revision"] == START.revision == report["start_policy_revision"]
    assert report["sessions"][-1]["approval_policy_revision"] == START.revision
    report["sessions"].append({"command": "resume"})
    common.pin_session_policy(report, policy_report(START))
    assert "policy_overrides" not in report and set(report["approval_policies"]) == {START.revision}


def test_resume_with_changed_flags_is_refused_naming_both_digests():
    report = fresh_report()
    common.pin_session_policy(report, policy_report(START))
    with pytest.raises(SystemExit) as refused:
        common.pin_session_policy(report, policy_report(RESUME))
    assert START.revision in str(refused.value) and RESUME.revision in str(refused.value)
    assert report["approval_policy"]["revision"] == START.revision  # nothing changed


def test_resume_with_changed_flags_and_a_human_override_is_recorded():
    report = fresh_report()
    common.pin_session_policy(report, policy_report(START))
    with pytest.raises(SystemExit, match="actor and a reason"):
        common.pin_session_policy(report, policy_report(RESUME), {"actor": " ", "reason": "r"})
    common.pin_session_policy(report, policy_report(RESUME), {"actor": "vertti", "reason": "tools back to human"})
    entry, = report["policy_overrides"]
    assert entry["evidence_kind"] == "human" and entry["actor"] == "vertti" and entry["reason"] == "tools back to human"
    assert (entry["previous_revision"], entry["revision"]) == (START.revision, RESUME.revision) and entry["at"]
    assert report["approval_policy"]["revision"] == RESUME.revision
    assert report["start_policy_revision"] == START.revision
    assert set(report["approval_policies"]) == {START.revision, RESUME.revision}


def test_override_without_a_change_is_refused():
    report = fresh_report()
    common.pin_session_policy(report, policy_report(START))
    with pytest.raises(SystemExit, match="did not change"):
        common.pin_session_policy(report, policy_report(START), {"actor": "vertti", "reason": "r"})


def test_pilot_override_flags_are_resume_only_and_paired(tmp_path):
    base = ["--work-root", str(tmp_path), "--allowed-issue-author", "uhvogala"]
    parsed = pilot.parse_arguments(["resume", "--run-dir", str(tmp_path / "r"), *base,
                                    "--policy-override-actor", "vertti", "--policy-override-reason", "why"])
    assert pilot.policy_override(parsed) == {"actor": "vertti", "reason": "why"}
    assert pilot.policy_override(pilot.parse_arguments(["start", *base])) is None
    for argv in (["start", *base, "--policy-override-actor", "v", "--policy-override-reason", "w"],
                 ["resume", "--run-dir", str(tmp_path / "r"), *base, "--policy-override-actor", "v"],
                 ["resume", "--run-dir", str(tmp_path / "r"), *base, "--policy-override-actor", "v",
                  "--policy-override-reason", " "]):
        with pytest.raises(SystemExit):
            pilot.parse_arguments(argv)


def _decision(policy, stage, source):
    return policy.resolve(stage, actor_id="operator", operation={}, automatic_supported=True, source=source)


class Run(SimpleNamespace):
    def policy_revision_for_decision(self, index):
        revision = self.approval_policy_revision
        for item in self.policy_overrides:
            if index >= item.after_decisions:
                revision = item.revision
        return revision


def _holder(previews, runs):
    return {"previews": SimpleNamespace(get=previews.get), "service": SimpleNamespace(status=runs.get)}


def _phase_state(policy, task_id, tool_source):
    admission = _decision(policy, ApprovalStage.TASK_ADMISSION, "operator_policy" if policy.task_admission == "automatic" else "human")
    preview = SimpleNamespace(
        approval_policy=admission if policy.task_admission == "automatic" else None,
        evidence_kind="operator_policy" if policy.task_admission == "automatic" else "human",
        bundle_payload={"task_id": task_id}, source_payload={})
    tool = SimpleNamespace(request_id=f"{task_id}-tools", kind="service_approval", actor_id="operator",
                           approval_policy=_decision(policy, ApprovalStage.DEVELOPER_TOOLS, tool_source))
    run = Run(approval_policy_revision=policy.revision, policy_overrides=(), decisions=(tool,))
    return preview, run


def test_each_phase_is_judged_by_its_own_policy_not_the_latest_session():
    """The trial4 case: start-phase automatic decisions must not be reported against the resume policy."""
    report = fresh_report()
    common.pin_session_policy(report, policy_report(START))
    report["phases"].append({"phase": "delivery:a", "preview_id": "p1", "policy_revision": START.revision})
    report["sessions"].append({"command": "resume"})
    common.pin_session_policy(report, policy_report(RESUME), {"actor": "vertti", "reason": "human tools"})
    report["phases"].append({"phase": "delivery:b", "preview_id": "p2", "policy_revision": RESUME.revision})
    p1, r1 = _phase_state(START, "issue-1", "operator_policy")
    p2, r2 = _phase_state(RESUME, "dependent-issue-2", "human")
    evidence = common.collect_evidence(_holder({"p1": p1, "p2": p2}, {"p1": r1, "p2": r2}), report, RESUME)
    assert evidence["ok"], evidence["problems"]
    assert [item["policy_revision"] for item in evidence["phases"]] == [START.revision, RESUME.revision]
    # Judged by the latest policy (the old behaviour), the start phase is wrong; by its own, it is right.
    stale = dict(report, phases=[dict(item, policy_revision=RESUME.revision) for item in report["phases"]])
    assert not common.collect_evidence(_holder({"p1": p1, "p2": p2}, {"p1": r1, "p2": r2}), stale, RESUME)["ok"]


def test_a_phase_decision_under_the_wrong_policy_is_reported():
    report = fresh_report()
    common.pin_session_policy(report, policy_report(START))
    report["phases"].append({"phase": "delivery:a", "preview_id": "p1", "policy_revision": START.revision})
    preview, run = _phase_state(START, "issue-1", "human")  # automatic stage answered by a human
    evidence = common.collect_evidence(_holder({"p1": preview}, {"p1": run}), report, START)
    assert not evidence["ok"] and "expected operator_policy" in evidence["problems"][0]


def test_run_decisions_after_a_service_override_use_the_override_policy():
    report = fresh_report()
    common.pin_session_policy(report, policy_report(START))
    report["phases"].append({"phase": "delivery:a", "preview_id": "p1", "policy_revision": START.revision})
    report["sessions"].append({"command": "resume"})
    common.pin_session_policy(report, policy_report(RESUME), {"actor": "vertti", "reason": "human tools"})
    preview, run = _phase_state(START, "issue-1", "operator_policy")
    later = SimpleNamespace(request_id="later", kind="service_approval", actor_id="operator",
                            approval_policy=_decision(RESUME, ApprovalStage.DEVELOPER_TOOLS, "human"))
    override = SimpleNamespace(revision=RESUME.revision, after_decisions=1,
                               model_dump=lambda mode=None: {"revision": RESUME.revision})
    run = Run(approval_policy_revision=START.revision, policy_overrides=(override,), decisions=(*run.decisions, later))
    evidence = common.collect_evidence(_holder({"p1": preview}, {"p1": run}), report, RESUME)
    assert evidence["ok"], evidence["problems"]
    assert [item["pinned_policy_revision"] for item in evidence["phases"][0]["decisions"]] == [START.revision, RESUME.revision]


def test_phases_from_before_pinning_use_the_start_policy():
    report = {"phases": [{"phase": "delivery:a", "preview_id": "p1"}], "sessions": [{"command": "start"}],
              "approval_policy": policy_report(START)}  # an old report: no stamps, no pinned_policy
    report["sessions"].append({"command": "resume"})
    common.pin_session_policy(report, policy_report(RESUME), {"actor": "vertti", "reason": "r"})
    assert common.phase_policy_revision(report, report["phases"][0]) == START.revision
    preview, run = _phase_state(START, "issue-1", "operator_policy")
    assert common.collect_evidence(_holder({"p1": preview}, {"p1": run}), report, RESUME)["ok"]


def test_open_runs_needing_the_service_override_are_found():
    report = {"phases": [{"preview_id": p} for p in ("done", "open", "pinned", "overridden")]}
    runs = {
        "done": {"managed_run": {"state": "completed", "assignment_id": "a0", "approval_policy_revision": START.revision}},
        "open": {"managed_run": {"state": "running", "assignment_id": "a1", "approval_policy_revision": START.revision}},
        "pinned": {"managed_run": {"state": "waiting", "assignment_id": "a2", "approval_policy_revision": RESUME.revision}},
        "overridden": {"managed_run": {"state": "waiting", "assignment_id": "a3", "approval_policy_revision": START.revision,
                                       "policy_overrides": [{"revision": RESUME.revision}]}},
    }
    assert common.runs_needing_policy_override(report, runs.get, RESUME.revision) == ["a1"]
