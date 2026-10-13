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


def _service(tmp_path, previews):
    from conftest import consent_service
    return consent_service(tmp_path / "svc", previews, ApprovalPolicy(task_admission="automatic", max_automatic_admissions=5))


@pytest.mark.parametrize("evidence", [OperatorPolicy(), PlannedChild(), InheritedReview()])
def test_planning_admission_without_human_source_is_refused(tmp_path, evidence) -> None:
    """#31 through a real ManagedOrganizationService: _admit refuses every automatic source for planning."""
    previews, stored = _store(tmp_path, task_id="pm-planning-" + "a" * 64)
    service = _service(tmp_path, previews)
    with pytest.raises(PermissionError, match="Planning tasks always require a human admission"):
        service._admit(stored, evidence)
    with pytest.raises(PermissionError, match="human admission"):
        service._automatic_task_approval(stored)  # the public automatic path refuses it too
    after = previews.get(stored.preview_id)
    assert after is not None and not after.approved and after.evidence_kind is None
    approved = admit_task(previews, stored.preview_id, Human())
    assert approved is not None and approved.approved and approved.evidence_kind == "human"


CORRECTIONS = [("published-correction-" + "c" * 64, None), ("ISSUE-77", {"correction_from_review": "dp-review"})]


def _policy_service(tmp_path, previews, correction: str):
    from conftest import consent_service
    policy = ApprovalPolicy(task_admission="automatic", max_automatic_admissions=5, correction=correction)
    return consent_service(tmp_path / "svc", previews, policy)


@pytest.mark.parametrize("correction", ["human", "disabled"])
@pytest.mark.parametrize("task_id,source", CORRECTIONS)
@pytest.mark.parametrize("evidence", [OperatorPolicy(), PlannedChild(), InheritedReview()])
def test_correction_admission_without_human_source_is_refused(tmp_path, correction, task_id, source, evidence) -> None:
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    item = _issue_preview(previews, "corr", task_id, source or {"action": "opened"})
    service = _policy_service(tmp_path, previews, correction)
    with pytest.raises(PermissionError, match="Correction tasks require a human admission"):
        service._admit(item, evidence)
    after = previews.get(item.preview_id)
    assert after is not None and not after.approved and after.evidence_kind is None


@pytest.mark.parametrize("task_id,source", CORRECTIONS)
def test_correction_automatic_path_refused_under_human_and_human_still_admits(tmp_path, task_id, source) -> None:
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    item = _issue_preview(previews, "corr", task_id, source or {"action": "opened"})
    service = _policy_service(tmp_path, previews, "human")
    with pytest.raises(PermissionError, match="Correction tasks require a human admission"):
        service._automatic_task_approval(item)
    approved = admit_task(previews, item.preview_id, Human(base_revision="b" * 40))
    assert approved is not None and approved.approved and approved.evidence_kind == "human"


@pytest.mark.parametrize("task_id,source", CORRECTIONS)
def test_correction_automatic_admits_through_policy_and_human_still_admits(tmp_path, monkeypatch, task_id, source) -> None:
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    item = _issue_preview(previews, "corr", task_id, source or {"action": "opened"})
    service = _policy_service(tmp_path, previews, "human")
    # Constructing correction: automatic needs live review/publication bindings; the admission check only reads the
    # policy, so switch the runner's policy the way the existing end-to-end review test does.
    monkeypatch.setattr(service._runner, "approval_policy", ApprovalPolicy(
        task_admission="automatic", max_automatic_admissions=5, correction="automatic"))
    service._task_base = lambda candidate: "b" * 40
    admitted = service._automatic_task_approval(item)
    assert admitted.approved and admitted.evidence_kind == "operator_policy"
    assert admitted.approval_policy is not None and admitted.approval_policy.source == "operator_policy"
    other = _issue_preview(previews, "corr-2", task_id + "-2", source or {"action": "opened"})
    human = admit_task(previews, other.preview_id, Human(base_revision="b" * 40))
    assert human is not None and human.evidence_kind == "human"


def test_non_correction_issue_task_still_reaches_later_checks(tmp_path) -> None:
    """Control: an ordinary issue task is not refused by the correction/planning check (it fails later instead)."""
    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    plain = _issue_preview(previews, "plain", "ISSUE-88")
    service = _service(tmp_path, previews)
    with pytest.raises((PermissionError, ValueError)) as raised:
        service._admit(plain, OperatorPolicy())
    assert "always require a human admission" not in str(raised.value)


# --- persisted evidence_kind consistency -------------------------------------------------------------------------

def _rewrite(path, **fields):
    data = json.loads(path.read_text())
    for key, value in fields.items():
        if value is None:
            data["previews"][0].pop(key, None)
        else:
            data["previews"][0][key] = value
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("kind", ["operator_policy", "planned_child", "inherited_review", "consent"])
def test_human_record_relabelled_as_other_kind_is_refused(tmp_path, kind) -> None:
    previews, stored = _store(tmp_path)
    admit_task(previews, stored.preview_id, Human())
    _rewrite(tmp_path / "previews.json", evidence_kind=kind)
    with pytest.raises(ValueError, match="contradicts"):
        DeveloperPreviewRegistry(tmp_path / "previews.json").get(stored.preview_id)


@pytest.mark.parametrize("kind", ["human", "consent"])
def test_automatic_record_relabelled_is_refused(tmp_path, kind) -> None:
    previews, stored = _store(tmp_path)
    admit_task(previews, stored.preview_id, PlannedChild(), decision=_decision(stored), expected=stored)
    _rewrite(tmp_path / "previews.json", evidence_kind=kind)
    with pytest.raises(ValueError, match="contradicts"):
        DeveloperPreviewRegistry(tmp_path / "previews.json").get(stored.preview_id)


@pytest.mark.parametrize("kind", ["operator_policy", "planned_child", "inherited_review"])
def test_configured_kinds_with_their_decision_load(tmp_path, kind) -> None:
    previews, stored = _store(tmp_path)
    admit_task(previews, stored.preview_id, OperatorPolicy(), decision=_decision(stored), expected=stored)
    _rewrite(tmp_path / "previews.json", evidence_kind=kind)
    loaded = DeveloperPreviewRegistry(tmp_path / "previews.json").get(stored.preview_id)
    assert loaded is not None and loaded.evidence_kind == kind


def test_configured_kind_with_human_decision_is_refused(tmp_path) -> None:
    previews, stored = _store(tmp_path)
    admit_task(previews, stored.preview_id, OperatorPolicy(), decision=_decision(stored), expected=stored)
    data = json.loads((tmp_path / "previews.json").read_text())
    policy = data["previews"][0]["approval_policy"] | {"source": "human", "mode": "human"}
    _rewrite(tmp_path / "previews.json", approval_policy=policy)
    with pytest.raises((ValueError, PermissionError)):
        DeveloperPreviewRegistry(tmp_path / "previews.json").get(stored.preview_id)
    # the kind check alone (independent of policy validation) refuses a human-sourced decision for a configured kind
    from aitobuild.developer_preview import _evidence_kind_consistent
    assert not _evidence_kind_consistent("planned_child", {"source": "human"}, None)
    assert not _evidence_kind_consistent("planned_child", {"source": "operator_policy"}, {"x": 1})
    assert _evidence_kind_consistent("consent", {"source": "human"}, {"x": 1})
    assert not _evidence_kind_consistent("human", None, {"x": 1})


def test_legacy_none_kind_with_any_decision_still_loads(tmp_path) -> None:
    previews, stored = _store(tmp_path)
    admit_task(previews, stored.preview_id, OperatorPolicy(), decision=_decision(stored), expected=stored)
    _rewrite(tmp_path / "previews.json", evidence_kind=None)
    loaded = DeveloperPreviewRegistry(tmp_path / "previews.json").get(stored.preview_id)
    assert loaded is not None and loaded.approved and loaded.evidence_kind is None


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
def test_corrections_accept_automatic_evidence_when_correction_is_automatic(task_id, source) -> None:
    modes = AUTO | {"correction": "automatic"}
    assert expected_evidence_kind(task_id, source, True) == "operator_policy"
    assert admission_problem("c", task_id, POLICY, "operator_policy", modes, REV, source) is None
    assert admission_problem("c", task_id, None, "human", modes, REV, source)
    assert admission_problem("c", task_id, POLICY, "operator_policy", AUTO | {"correction": "human"}, REV, source)
    assert expected_evidence_kind("pm-planning-x", None, True) == "human"


@pytest.mark.parametrize("task_id,source", [("published-correction-x", None), ("ISSUE-9", {"correction_from_review": {}})])
def test_corrections_must_be_human_admitted(task_id, source) -> None:
    assert admission_problem("c", task_id, None, "human", AUTO, REV, source) is None
    assert admission_problem("c", task_id, POLICY, "operator_policy", AUTO, REV, source)
    assert admission_problem("c", task_id, POLICY, "human", AUTO, REV, source)
    assert admission_problem("c", task_id, None, None, AUTO, REV, source)



@pytest.mark.parametrize("first,later,admitted", [("automatic", "human", True), ("human", "automatic", False)])
def test_correction_mode_follows_the_policy_snapshot_the_decision_is_bound_to(tmp_path, monkeypatch, first, later,
                                                                                admitted) -> None:
    """Config flips mid-admission: the first policy read governs both the correction check and the decision."""
    from aitobuild.organization_service import ManagedOrganizationService

    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    item = _issue_preview(previews, "corr", CORRECTIONS[0][0])
    service = _policy_service(tmp_path, previews, "human")
    service._task_base = lambda candidate: "b" * 40
    policies = {mode: ApprovalPolicy(task_admission="automatic", max_automatic_admissions=5, correction=mode)
                for mode in ("automatic", "human")}
    reads: list[str] = []

    def flipping(self):
        mode = first if not reads else later
        reads.append(mode)
        return policies[mode]

    monkeypatch.setattr(ManagedOrganizationService, "approval_policy", property(flipping))
    if admitted:
        result = service._admit(item, OperatorPolicy())
        assert result.approved and result.approval_policy is not None
        assert result.approval_policy.policy_revision == policies["automatic"].revision
    else:
        with pytest.raises(PermissionError, match="Correction tasks require a human admission"):
            service._admit(item, OperatorPolicy())
        assert not previews.get(item.preview_id).approved
    assert reads[0] == first
