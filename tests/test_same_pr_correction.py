from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pytest

from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.publish_approvals import PublishApprovalStore, snapshot_digest
from aitobuild.tools.github import MockGitHubAdapter
from test_dispatcher import (
    approved_delivery as approved_delivery, implemented_delivery as implemented_delivery,
    verification_adapter as verification_adapter,
)

RECEIPT = "a" * 64


def _git(directory, *arguments):
    return subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def correction_chain(implemented_delivery, verification_adapter, monkeypatch):
    worker, published_id, _, _ = implemented_delivery
    monkeypatch.setattr("aitobuild.developer_delivery.shell_request", lambda *args, **kwargs: {
        "ok": True, "status": "exited", "exit_code": 0, "output": "passed", "next_cursor": 1,
    })
    worker.verify(published_id, adapter=verification_adapter)
    github = MockGitHubAdapter(allowed_repositories=frozenset({"fixture/widgets"}), enforce_allowlist=True)
    worker.publish(published_id, github=github, require_human_approval_for_repo_writes=True, allow_mock_publication=True)
    published = worker.get(published_id)
    checkout = Path(published.checkout_path)
    _git(checkout, "add", "--", *published.publication["changed_paths"])
    _git(checkout, "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-m", "Disposable reviewed head")
    head = _git(checkout, "rev-parse", "HEAD")
    worker._save(checkout.parent, replace(published, head_revision=head, publication=published.publication | {"head_sha": head}))
    number, repository = published.publication["pull_number"], published.publication["repository"]
    github.pull_requests[repository][number] = replace(github.pull_requests[repository][number], head_sha=head)
    github._branch_heads.setdefault(repository, {})[published.branch] = head
    _git(Path(published.source_path), "fetch", "--no-tags", "--no-write-fetch-head", str(checkout), head)
    _git(Path(published.source_path), "update-ref", "refs/heads/" + published.branch, head)

    def make(review_id="review-one", content="def probe():\n    return 2\n", target_id=published_id):
        target = worker.get(target_id)
        bundle = json.loads(json.dumps(target.bundle_payload))
        task_id = "published-correction-" + sha256(f"{review_id}:{content}".encode()).hexdigest()
        bundle["task_id"] = task_id
        bundle["objective"] = "Fix the inspected edge case"
        bundle["issue_context"]["base_revision"] = target.publication["head_sha"]
        bundle["issue_context"]["base_branch"] = target.publication["branch"]
        bundle["policy"]["allowed_paths"] = ["src/probe.py"]
        bundle["policy"]["max_file_changes"] = 1
        preview = worker._previews.create_or_get(dedupe_key=task_id, bundle_payload=bundle, source_payload={
            "correction_from_review": review_id,
            "published_review": {"preview_id": target_id, "head_sha": target.publication["head_sha"]},
        })
        worker._previews.approve(preview.preview_id)
        record = worker.prepare(preview.preview_id)
        assert record.state == "prepared", record.error
        parsed = developer_task_bundle_from_payload(record.bundle_payload)
        budget = DeveloperTaskBudget(path=worker.budget_path(preview.preview_id), bundle=parsed, create=False)
        with worker.implementation_lock(preview.preview_id):
            worker.begin_implementation(preview.preview_id, bundle=parsed, session_id="correction-developer", resume=False)
            budget.reserve_paths(("src/probe.py",))
            (Path(record.checkout_path) / "src/probe.py").write_text(content)
            worker.finish_implementation(preview.preview_id, session_id="correction-developer")
        verified = worker.verify(preview.preview_id, adapter=verification_adapter)
        assert verified.state == "verified", verified.error
        return preview.preview_id

    return worker, published_id, github, head, make


def _publish(worker, github, preview_id, digest):
    return worker.approve_and_publish_correction(
        preview_id, approval_digest=digest, review_receipt_digest=RECEIPT, actor_id="operator",
        github=github, require_human_approval_for_repo_writes=True, allow_mock_publication=True,
    )


def test_correction_fast_forwards_the_pinned_pr_once(correction_chain):
    worker, published_id, github, head, make = correction_chain
    correction = make()
    original = worker.get(published_id).publication
    pulls_before = {number: pull for number, pull in github.pull_requests["fixture/widgets"].items()}
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    snapshot = staged["snapshot"]
    assert staged["state"] == "pending" and staged["digest"] == snapshot_digest(snapshot)
    assert (snapshot["pull_number"], snapshot["head_branch"], snapshot["base_ref"], snapshot["parent_head_sha"]) == (
        original["pull_number"], original["branch"], original["base_ref"], head)
    assert snapshot["chain"] == [published_id] and snapshot["review_receipt_digest"] == RECEIPT
    assert "+    return 2" in snapshot["diff"] and snapshot["changed_paths"] == ["src/probe.py"]
    assert worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)["digest"] == staged["digest"]
    with pytest.raises(PermissionError, match="differs from the staged"):
        _publish(worker, github, correction, "0" * 64)
    with pytest.raises(PermissionError, match="drifted"):
        worker.approve_and_publish_correction(
            correction, approval_digest=staged["digest"], review_receipt_digest="b" * 64, actor_id="operator",
            github=github, require_human_approval_for_repo_writes=True, allow_mock_publication=True)
    assert worker.get(correction).state == "verified"
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    record = _publish(worker, github, correction, staged["digest"])
    assert record.state == "published" and record.publication["mode"] == "advance"
    new_head = record.publication["head_sha"]
    assert new_head != head and record.publication["parent_head_sha"] == head
    pulls = github.pull_requests["fixture/widgets"]
    assert set(pulls) == set(pulls_before)
    assert pulls[original["pull_number"]].head_sha == new_head
    assert pulls[original["pull_number"]].base_ref == pulls_before[original["pull_number"]].base_ref
    assert worker._approvals.get(correction)["state"] == "consumed"
    assert _publish(worker, github, correction, staged["digest"]) == record
    with pytest.raises(ValueError):
        worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    with pytest.raises(PermissionError, match=f"superseded by {correction}"):
        worker.get_published_pull_request(published_id, github=github)
    with pytest.raises(PermissionError, match=f"superseded by {correction}"):
        worker.submit_architect_review(published_id, github=github, event="COMMENT", body="late")
    current = worker.get_published_pull_request(correction, github=github)
    assert current["head_matches_publication"]
    reviewed = worker.submit_architect_review(correction, github=github, event="COMMENT", body="second round")
    assert reviewed.architect_review["head_sha"] == reviewed.architect_review["commit_id"] == new_head
    assert github.reviews[-1].commit_id == new_head
    with pytest.raises(PermissionError, match="cannot publish a new pull request"):
        worker.publish(correction, github=github, require_human_approval_for_repo_writes=True, allow_mock_publication=True)


def test_digest_drift_invalidates_the_one_use_approval(correction_chain, monkeypatch):
    worker, _, github, _, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    metadata = worker._publication_metadata
    monkeypatch.setattr(worker, "_publication_metadata", lambda bundle, record: (*metadata(bundle, record)[:2], "changed message"))
    with pytest.raises(PermissionError, match="drifted"):
        _publish(worker, github, correction, staged["digest"])
    monkeypatch.undo()
    with pytest.raises(PermissionError, match="invalidated"):
        _publish(worker, github, correction, staged["digest"])
    assert worker.get(correction).state == "verified"
    restaged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    assert restaged["state"] == "pending" and restaged["digest"] == staged["digest"]
    assert _publish(worker, github, correction, restaged["digest"]).state == "published"


def test_sibling_on_the_same_parent_head_is_terminal_stale(correction_chain):
    worker, _, github, head, make = correction_chain
    first = make("review-one", "def probe():\n    return 2\n")
    second = make("review-two", "def probe():\n    return 3\n")
    staged = worker.stage_correction_publication(first, review_receipt_digest=RECEIPT)
    with pytest.raises(PermissionError, match=f"{first} already uses parent head {head}"):
        worker.stage_correction_publication(second, review_receipt_digest=RECEIPT)
    loser = worker.get(second)
    assert loser.state == "failed" and "stale" in loser.error
    assert worker._approvals.get(second) is None
    assert _publish(worker, github, first, staged["digest"]).state == "published"


def test_resume_after_lost_response_reuses_the_exact_push(correction_chain, monkeypatch):
    worker, published_id, github, head, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    advance = github.advance_draft_pull_request_head

    def lost(**kwargs):
        advance(**kwargs)
        raise RuntimeError("gh timed out after applying")

    monkeypatch.setattr(github, "advance_draft_pull_request_head", lost)
    with pytest.raises(RuntimeError):
        _publish(worker, github, correction, staged["digest"])
    pending = worker.get(correction)
    assert pending.state == "publishing" and pending.error == "gh timed out after applying"
    assert worker._approvals.get(correction)["state"] == "consuming"
    with pytest.raises(PermissionError, match=f"superseded by {correction}"):
        worker.get_published_pull_request(published_id, github=github)
    monkeypatch.undo()
    with pytest.raises(PermissionError, match="another operator"):
        worker.approve_and_publish_correction(
            correction, approval_digest=staged["digest"], review_receipt_digest=RECEIPT, actor_id="intruder",
            github=github, require_human_approval_for_repo_writes=True, allow_mock_publication=True)
    record = _publish(worker, github, correction, staged["digest"])
    assert record.state == "published" and record.publication["parent_head_sha"] == head


def test_broken_chain_link_fails_closed(correction_chain, monkeypatch):
    worker, published_id, github, _, make = correction_chain
    correction = make()
    real_get = worker._previews.get

    def broken(preview_id, target):
        preview = real_get(preview_id)
        if preview_id != correction:
            return preview
        return replace(preview, source_payload=preview.source_payload | {"published_review": target})

    head = worker.get(published_id).publication["head_sha"]
    for target in ({"preview_id": "missing-delivery", "head_sha": head}, {"preview_id": published_id, "head_sha": "e" * 40}, None):
        monkeypatch.setattr(worker._previews, "get", lambda preview_id, target=target: broken(preview_id, target))
        with pytest.raises(PermissionError, match="Correction chain"):
            worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
        assert worker._approvals.get(correction) is None and worker.get(correction).state == "verified"
    monkeypatch.undo()
    original_dir = worker._task_dir(published_id)
    original = worker.get(published_id)
    worker._save(original_dir, replace(original, publication=original.publication | {"branch": "aitobuild/other"}))
    with pytest.raises(PermissionError):
        worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    worker._save(original_dir, original)
    (original_dir / "state.json").write_text("{}")
    with pytest.raises(PermissionError, match="failed validation"):
        worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    assert worker._approvals.get(correction) is None


def test_forked_chain_refuses_review_and_staging(correction_chain, monkeypatch):
    worker, published_id, github, _, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    published = _publish(worker, github, correction, staged["digest"])
    records = worker._correction_records
    monkeypatch.setattr(worker, "_correction_records", lambda: [*records(), replace(published, preview_id="forked-copy")])
    with pytest.raises(PermissionError, match="forked"):
        worker.superseded_by(published_id)
    with pytest.raises(PermissionError, match="forked"):
        worker.get_published_pull_request(published_id, github=github)


def test_approval_store_is_single_use_and_tamper_evident(tmp_path):
    store = PublishApprovalStore(tmp_path / "approvals")
    snapshot = {"preview_id": "p1", "diff": "x"}
    digest = store.stage("p1", snapshot)["digest"]
    with pytest.raises(ValueError):
        store.stage("p2", snapshot)
    with pytest.raises(PermissionError, match="operator identity"):
        store.begin_consume("p1", digest=digest, recomputed_digest=digest, actor_id=" ")
    assert store.begin_consume("p1", digest=digest, recomputed_digest=digest, actor_id="op")["state"] == "consuming"
    with pytest.raises(PermissionError, match="already consumed"):
        store.stage("p1", {"preview_id": "p1", "diff": "y"})
    store.finish_consume("p1", digest=digest, head_sha="c" * 40)
    with pytest.raises(PermissionError, match="already consumed"):
        store.begin_consume("p1", digest=digest, recomputed_digest=digest, actor_id="op")
    path = next((tmp_path / "approvals").glob("*.json"))
    data = json.loads(path.read_text())
    data["snapshot"]["diff"] = "tampered"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid persisted"):
        store.get("p1")
