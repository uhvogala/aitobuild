"""Admission recording (audit, not policy): admit_task persists the evidence kind on the admitted preview, legacy
records still load, and a planning task is refused unless the admission is human (#31)."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage
from aitobuild.developer_preview import DeveloperPreviewRegistry, preview_approval_operation
from aitobuild.dispatcher import DispatcherAgent
from aitobuild.organization_service import Human, InheritedReview, OperatorPolicy, PlannedChild, admit_task

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))
from common import admission_problem, expected_evidence_kind  # noqa: E402

REV = "r" * 64


def _store(tmp_path: Path, task_id: str | None = None):
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    seed = DispatcherAgent(require_developer_preview=True).create_developer_preview(
        dedupe_key="seed", github_event="issues", action="opened", body={"issue": {"number": 1}})
    bundle = {key: value for key, value in seed.bundle_payload.items() if key != "issue_context"}
    if task_id is not None:
        bundle["task_id"] = task_id
    stored = previews.create_or_get(dedupe_key="k", bundle_payload=bundle, source_payload={"action": "opened"})
    return previews, stored


def _decision(stored, source: str = "operator_policy"):
    return ApprovalPolicy(task_admission="automatic").resolve(
        ApprovalStage.TASK_ADMISSION, actor_id="operator",
        operation=preview_approval_operation(stored.preview_id, stored.bundle_payload), automatic_supported=True)


@pytest.mark.parametrize("evidence,kind", [
    (OperatorPolicy(), "operator_policy"), (PlannedChild(), "planned_child"), (InheritedReview(), "inherited_review")])
def test_configured_admission_records_evidence_kind_and_persists(tmp_path, evidence, kind) -> None:
    previews, stored = _store(tmp_path)
    approved = admit_task(previews, stored.preview_id, evidence, decision=_decision(stored), expected=stored)
    assert approved is not None and approved.evidence_kind == kind
    assert approved.approval_policy is not None and approved.approval_policy.source == "operator_policy"
    reloaded = DeveloperPreviewRegistry(tmp_path / "previews.json").get(stored.preview_id)
    assert reloaded is not None and reloaded.evidence_kind == kind
    saved = json.loads((tmp_path / "previews.json").read_text())["previews"][0]
    assert saved["evidence_kind"] == kind


def test_human_admission_records_human_kind(tmp_path) -> None:
    previews, stored = _store(tmp_path)
    approved = admit_task(previews, stored.preview_id, Human())
    assert approved is not None and approved.evidence_kind == "human" and approved.approval_policy is None
    reloaded = DeveloperPreviewRegistry(tmp_path / "previews.json").get(stored.preview_id)
    assert reloaded is not None and reloaded.evidence_kind == "human"


def test_consent_kind_is_a_known_kind() -> None:
    from aitobuild.developer_preview import ADMISSION_EVIDENCE_KINDS
    from aitobuild.organization_service import Consent
    assert Consent.model_fields["kind"].default == "consent"
    assert ADMISSION_EVIDENCE_KINDS == {"human", "operator_policy", "planned_child", "inherited_review", "consent"}


def test_legacy_record_without_evidence_kind_loads(tmp_path) -> None:
    previews, stored = _store(tmp_path)
    admit_task(previews, stored.preview_id, Human())
    path = tmp_path / "previews.json"
    data = json.loads(path.read_text())
    del data["previews"][0]["evidence_kind"]
    path.write_text(json.dumps(data))
    legacy = DeveloperPreviewRegistry(path).get(stored.preview_id)
    assert legacy is not None and legacy.approved and legacy.evidence_kind is None


def test_unapproved_record_has_no_kind_and_none_is_not_saved(tmp_path) -> None:
    _, stored = _store(tmp_path)
    assert stored.evidence_kind is None
    assert "evidence_kind" not in json.loads((tmp_path / "previews.json").read_text())["previews"][0]


@pytest.mark.parametrize("bad,approved", [("bogus", True), ("human", False)])
def test_invalid_persisted_kind_is_refused(tmp_path, bad, approved) -> None:
    previews, stored = _store(tmp_path)
    if approved:
        admit_task(previews, stored.preview_id, Human())
    path = tmp_path / "previews.json"
    data = json.loads(path.read_text())
    data["previews"][0]["evidence_kind"] = bad
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="evidence kind"):
        DeveloperPreviewRegistry(path).get(stored.preview_id)


@pytest.mark.parametrize("evidence", [OperatorPolicy(), PlannedChild(), InheritedReview()])
def test_planning_admission_without_human_source_is_refused(tmp_path, evidence) -> None:
    """#31: _admit refuses every automatic source for a planning task before any other check (the one place)."""
    from types import SimpleNamespace

    from aitobuild.organization_service import ManagedOrganizationService

    previews, stored = _store(tmp_path, task_id="pm-planning-" + "a" * 64)
    with pytest.raises(PermissionError, match="human admission"):
        ManagedOrganizationService._admit(SimpleNamespace(), stored, evidence)  # type: ignore[arg-type]
    after = previews.get(stored.preview_id)
    assert after is not None and not after.approved and after.evidence_kind is None
    approved = admit_task(previews, stored.preview_id, Human())
    assert approved is not None and approved.approved and approved.evidence_kind == "human"


# --- legacy records (evidence_kind None) never count as automatic evidence ---------------------------------------

def _issue_preview(previews, key: str, task_id: str, source: dict | None = None, issue: int = 202):
    seed = DispatcherAgent(require_developer_preview=True).create_developer_preview(
        dedupe_key="seed", github_event="issues", action="opened", body={"issue": {"number": 1}})
    bundle = dict(seed.bundle_payload) | {"task_id": task_id, "issue_context": {
        "repository": "fixture/widgets", "repository_id": 101, "issue_number": issue, "issue_id": issue, "title": "t",
        "body": "b", "base_branch": "main", "base_revision": None}}
    return previews.create_or_get(dedupe_key=key, bundle_payload=bundle, source_payload=source or {"action": "opened"})


def _auto(previews, preview, limit: int = 5):
    from copy import deepcopy
    bundle = deepcopy(preview.bundle_payload)
    bundle["issue_context"]["base_revision"] = "b" * 40
    decision = ApprovalPolicy(task_admission="automatic").resolve(
        ApprovalStage.TASK_ADMISSION, actor_id="operator",
        operation=preview_approval_operation(preview.preview_id, bundle), automatic_supported=True)
    return previews._approve_admission(preview.preview_id, approval_policy=decision, base_revision="b" * 40,
                                       max_automatic_admissions=limit, evidence_kind="operator_policy")


def _legacy_human(tmp_path, previews, preview):
    previews._approve_admission(preview.preview_id, base_revision="b" * 40)  # no decision, no kind: a legacy record
    reloaded = DeveloperPreviewRegistry(tmp_path / "previews.json")
    legacy = reloaded.get(preview.preview_id)
    assert legacy is not None and legacy.approved and legacy.evidence_kind is None and legacy.approval_policy is None
    return reloaded


def test_legacy_record_does_not_count_toward_automatic_limit(tmp_path) -> None:
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    old = _issue_preview(previews, "old", "ISSUE-old")
    previews = _legacy_human(tmp_path, previews, old)
    new = _issue_preview(previews, "new", "ISSUE-new", issue=303)
    assert _auto(previews, new, limit=1).approved


def test_legacy_record_does_not_trigger_issue_dedupe(tmp_path) -> None:
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    old = _issue_preview(previews, "old", "ISSUE-1-old")
    previews = _legacy_human(tmp_path, previews, old)
    same_issue = _issue_preview(previews, "again", "ISSUE-1-again")
    assert _auto(previews, same_issue).approved


def test_legacy_review_does_not_block_inherited_review_of_same_head(tmp_path) -> None:
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    target = {"published_review": {"preview_id": "dp-parent", "head_sha": "c" * 40}}
    old = _issue_preview(previews, "old-review", "published-review-old", target)
    previews = _legacy_human(tmp_path, previews, old)
    new = _issue_preview(previews, "new-review", "published-review-new", target)
    assert _auto(previews, new).approved


def test_legacy_automatic_decision_still_counts_through_its_decision(tmp_path) -> None:
    """Fail closed: a legacy record that carries an operator_policy decision still counts (by its decision)."""
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    old = _issue_preview(previews, "old", "ISSUE-old")
    _auto(previews, old)
    path = tmp_path / "previews.json"
    data = json.loads(path.read_text())
    for item in data["previews"]:
        item.pop("evidence_kind", None)
    path.write_text(json.dumps(data))
    previews = DeveloperPreviewRegistry(path)
    new = _issue_preview(previews, "new", "ISSUE-new", issue=303)
    with pytest.raises(PermissionError, match="limit reached"):
        _auto(previews, new, limit=1)


# --- harness verdict (scripts/trials/common.py) ---------------------------------------------------------------

AUTO = {"task_admission": "automatic"}
POLICY = {"source": "operator_policy", "policy_revision": REV}


@pytest.mark.parametrize("task_id,kind", [
    ("pm-planning-x", "human"), ("published-review-x", "inherited_review"), ("planned-issue-x", "planned_child"),
    ("dependent-issue-x", "planned_child"), ("ISSUE-1", "operator_policy"), ("published-correction-x", "human")])
def test_expected_evidence_kind(task_id, kind) -> None:
    assert expected_evidence_kind(task_id) == kind


def test_planning_human_admission_passes_and_automatic_fails() -> None:
    assert admission_problem("planning", "pm-planning-x", None, "human", AUTO, REV) is None
    assert admission_problem("planning", "pm-planning-x", POLICY, "human", AUTO, REV)
    assert admission_problem("planning", "pm-planning-x", None, None, AUTO, REV)
    assert admission_problem("planning", "pm-planning-x", POLICY, "operator_policy", AUTO, REV)


@pytest.mark.parametrize("task_id,kind", [("planned-issue-x", "planned_child"), ("published-review-x", "inherited_review")])
def test_children_and_reviews_need_automatic_admission_with_their_kind(task_id, kind) -> None:
    assert admission_problem("p", task_id, POLICY, kind, AUTO, REV) is None
    assert admission_problem("p", task_id, None, "human", AUTO, REV)
    assert admission_problem("p", task_id, {**POLICY, "source": "human"}, kind, AUTO, REV)
    assert admission_problem("p", task_id, {**POLICY, "policy_revision": "x" * 64}, kind, AUTO, REV)
    assert admission_problem("p", task_id, POLICY, "operator_policy", AUTO, REV)
    assert admission_problem("p", task_id, POLICY, None, AUTO, REV)


def test_human_task_admission_mode_has_no_admission_verdict() -> None:
    assert admission_problem("p", "planned-issue-x", None, "human", {"task_admission": "human"}, REV) is None


def test_correction_by_source_payload_expects_human() -> None:
    assert expected_evidence_kind("ISSUE-1", {"correction_from_review": {}}) == "human"
    assert expected_evidence_kind("ISSUE-1", {"other": 1}) == "operator_policy"


@pytest.mark.parametrize("task_id,source", [("published-correction-x", None), ("ISSUE-9", {"correction_from_review": {}})])
def test_corrections_must_be_human_admitted(task_id, source) -> None:
    assert admission_problem("c", task_id, None, "human", AUTO, REV, source) is None
    assert admission_problem("c", task_id, POLICY, "operator_policy", AUTO, REV, source)
    assert admission_problem("c", task_id, POLICY, "human", AUTO, REV, source)
    assert admission_problem("c", task_id, None, None, AUTO, REV, source)

