"""#90: the #89 review chain walk carries the reviewed chain tip, read once, for the dependency merge check."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aitobuild.organization_reviews import (PrerequisiteReviewResult, PublishedReviewAdmission, PublishedReviewTarget,
                                            _CorrectionReceipt, _FollowUpReceipt, _ReviewJournal, _ReviewReceipt)

HEADS = ("a" * 40, "b" * 40, "c" * 40, "e" * 40)


def walk(tmp_path, rounds, *, last_state="completed", last_outcome="no_correction", retry=False, rereview_head=None):
    """A journal where ``dp-0`` was reviewed and corrected ``rounds`` times; ``dp-N`` is each published correction."""
    receipts, corrections, followups, runs, deliveries = {}, {}, {}, {}, {}
    for index in range(rounds + 1):
        head = HEADS[index] if rereview_head is None or index != rounds else rereview_head
        receipts["review-%d" % index] = _ReviewReceipt.model_construct(
            target=PublishedReviewTarget(preview_id="dp-%d" % index, head_sha=head), review_preview_id="rv-%d" % index)
        last = index == rounds
        runs["rv-%d" % index] = SimpleNamespace(run_id="run-%d" % index, state=last_state if last else "completed",
                                                cleanup_succeeded=True, model_dump_json=lambda index=index: "run-%d" % index)
        followups["run-%d" % index] = _FollowUpReceipt(source_preview_id="rv-%d" % index, run_content="run-%d" % index,
            state=last_outcome if last else "staged", next_preview_id=None if last else "dp-%d" % (index + 1))
        if not last:
            if retry and index == 0:  # a retired first attempt: only the latest attempt is the chain tip
                corrections["retired"] = _CorrectionReceipt.model_construct(
                    review_preview_id="rv-0", correction_preview_id="dp-retired", attempt=1)
            corrections["correction-%d" % index] = _CorrectionReceipt.model_construct(
                review_preview_id="rv-%d" % index, correction_preview_id="dp-%d" % (index + 1), attempt=2 if retry and index == 0 else 1)
        deliveries["dp-%d" % index] = SimpleNamespace(state="published", head_revision=HEADS[index])
    deliveries["dp-retired"] = SimpleNamespace(state="published", head_revision="f" * 40)
    admission = object.__new__(PublishedReviewAdmission)
    loads = []
    admission._path = tmp_path / "reviews.json"
    admission._load = lambda: loads.append(1) or _ReviewJournal.model_construct(
        receipts=receipts, corrections=corrections, followups=followups)
    admission._previews = SimpleNamespace(get=lambda preview_id: SimpleNamespace(
        preview_id=preview_id, approved=True, approved_at=datetime.now(timezone.utc)))
    admission._worker = SimpleNamespace(get=deliveries.get)
    result = admission.prerequisite_review("dp-0", lambda review: runs[review.preview_id])
    assert loads == [1]  # one journal read: the correction records are walked once
    return result


def test_no_correction_keeps_the_original_publication(tmp_path):
    assert walk(tmp_path, 0) == PrerequisiteReviewResult(None)


@pytest.mark.parametrize("retry", [False, True])
def test_reviewed_correction_carries_its_exact_tip(tmp_path, retry):
    assert walk(tmp_path, 1, retry=retry) == PrerequisiteReviewResult(None, correction_preview_id="dp-1", correction_head_sha=HEADS[1])


def test_multi_round_chain_carries_the_last_tip(tmp_path):
    assert walk(tmp_path, 3) == PrerequisiteReviewResult(None, correction_preview_id="dp-3", correction_head_sha=HEADS[3])


@pytest.mark.parametrize(("state", "outcome", "reason"), [("running", "no_correction", "re_review_in_progress"),
    ("failed", "no_correction", "re_review_failed"), ("completed", "escalated", "re_review_outcome_escalated")])
def test_unfinished_re_review_has_no_tip(tmp_path, state, outcome, reason):
    assert walk(tmp_path, 2, last_state=state, last_outcome=outcome) == PrerequisiteReviewResult(reason)


def test_re_review_of_another_head_is_refused(tmp_path):
    with pytest.raises(PermissionError, match="published correction head"):
        walk(tmp_path, 2, rereview_head=HEADS[1])


@pytest.mark.parametrize("fields", [{"reason": "review_failed", "correction_preview_id": "dp-1", "correction_head_sha": "a" * 40},
    {"reason": None, "correction_preview_id": "dp-1"}, {"reason": None, "correction_head_sha": "a" * 40},
    {"reason": None, "correction_preview_id": "dp-1", "correction_head_sha": "A" * 40}, {"reason": ""}])
def test_result_is_a_reason_or_an_exact_tip(fields):
    with pytest.raises(ValueError):
        PrerequisiteReviewResult(**fields)
