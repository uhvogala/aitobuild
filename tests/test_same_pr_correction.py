from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pytest

from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.publish_approvals import PublishApproval, PublishApprovalStore, approval_digest, snapshot_digest
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
    assert staged["state"] == "pending" and staged["content_digest"] == snapshot_digest(snapshot)
    assert staged["digest"] == approval_digest(staged["content_digest"], worker._approvals.get(correction).nonce)
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
    assert worker._approvals.get(correction).state == "consumed"
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
    with monkeypatch.context() as patched:
        patched.setattr(worker, "_publication_metadata", lambda bundle, record: (*metadata(bundle, record)[:2], "changed message"))
        with pytest.raises(PermissionError, match="drifted"):
            _publish(worker, github, correction, staged["digest"])
    with pytest.raises(PermissionError, match="invalidated"):
        _publish(worker, github, correction, staged["digest"])
    assert worker.get(correction).state == "verified"
    restaged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    assert restaged["state"] == "pending" and restaged["content_digest"] == staged["content_digest"]
    assert restaged["digest"] != staged["digest"]  # fresh nonce: the invalidated digest never comes back
    with pytest.raises(PermissionError):
        _publish(worker, github, correction, staged["digest"])
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


def _pull(github, worker, preview_id):
    publication = worker.get(preview_id).publication
    return github.pull_requests[publication["repository"]][publication["pull_number"]]


def _move_head(github, worker, preview_id, sha):
    publication = worker.get(preview_id).publication
    repository, number = publication["repository"], publication["pull_number"]
    github.pull_requests[repository][number] = replace(github.pull_requests[repository][number], head_sha=sha)
    github._branch_heads[repository][publication["branch"]] = sha


def test_failure_after_the_push_applied_reconciles_to_published(correction_chain, monkeypatch):
    worker, published_id, github, head, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    advance = github.advance_draft_pull_request_head
    for failure in (RuntimeError("response lost after the push applied"), KeyboardInterrupt("stopped after push")):
        if worker.get(correction).state == "published":
            break

        def applied(failure=failure, **kwargs):
            advance(**kwargs)
            raise failure

        with monkeypatch.context() as patched:
            patched.setattr(github, "advance_draft_pull_request_head", applied)
            record = _publish(worker, github, correction, staged["digest"])
    assert record.state == "published" and record.publication["parent_head_sha"] == head
    assert record.publication["head_sha"] == _pull(github, worker, published_id).head_sha != head
    assert worker._approvals.get(correction).state == "consumed"
    assert worker.superseded_by(published_id) == correction


def test_interrupt_after_push_still_raises_once_reconciled(correction_chain, monkeypatch):
    worker, published_id, github, _, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    advance = github.advance_draft_pull_request_head

    def interrupted(**kwargs):
        advance(**kwargs)
        raise KeyboardInterrupt("process stopped after the push applied")

    with monkeypatch.context() as patched:
        patched.setattr(github, "advance_draft_pull_request_head", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _publish(worker, github, correction, staged["digest"])
    assert worker.get(correction).state == "published"
    assert worker._approvals.get(correction).state == "consumed"


def test_unreadable_head_stays_publishing_and_resumes(correction_chain, monkeypatch):
    worker, published_id, github, head, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    advance = github.advance_draft_pull_request_head

    def interrupted(**kwargs):
        advance(**kwargs)
        raise KeyboardInterrupt("process stopped after the push applied")

    def unreadable(**kwargs):
        raise RuntimeError("GitHub unreachable")

    with monkeypatch.context() as patched:
        patched.setattr(github, "advance_draft_pull_request_head", interrupted)
        patched.setattr(github, "reconcile_advanced_head", unreadable)
        with pytest.raises(KeyboardInterrupt):
            _publish(worker, github, correction, staged["digest"])
    assert worker.get(correction).state == "publishing"
    assert worker._approvals.get(correction).state == "consuming"
    with pytest.raises(PermissionError, match=f"superseded by {correction}"):
        worker.get_published_pull_request(published_id, github=github)
    with pytest.raises(PermissionError, match="another operator"):
        worker.approve_and_publish_correction(
            correction, approval_digest=staged["digest"], review_receipt_digest=RECEIPT, actor_id="intruder",
            github=github, require_human_approval_for_repo_writes=True, allow_mock_publication=True)
    with pytest.raises(PermissionError, match="publishing"):
        worker.retire_correction(correction, actor_id="operator", github=github, allow_mock_publication=True)
    record = _publish(worker, github, correction, staged["digest"])
    assert record.state == "published" and record.publication["parent_head_sha"] == head
    assert worker._approvals.get(correction).state == "consumed"


@pytest.mark.parametrize("live", ["landed", "parent_expired", "parent_valid", "moved", "unreadable"])
def test_resume_settles_by_the_live_head_before_any_budget_check(live, correction_chain, monkeypatch):
    """Crash mid-push, restart later: GitHub decides first, the budget only gates a fresh push."""
    worker, published_id, github, head, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    budget = DeveloperTaskBudget(path=worker.budget_path(correction),
                                 bundle=developer_task_bundle_from_payload(worker.get(correction).bundle_payload),
                                 create=False)
    advance = github.advance_draft_pull_request_head

    def crashed(**kwargs):
        if live == "landed":
            advance(**kwargs)
        raise KeyboardInterrupt("process stopped mid-push")

    def unreadable(**kwargs):
        raise RuntimeError("GitHub unreachable")

    with monkeypatch.context() as patched:
        patched.setattr(github, "advance_draft_pull_request_head", crashed)
        patched.setattr(github, "reconcile_advanced_head", unreadable)
        with pytest.raises(KeyboardInterrupt):
            _publish(worker, github, correction, staged["digest"])
    assert worker.get(correction).state == "publishing"
    if live != "parent_valid":
        budget.abort()  # restarted after the deadline
    if live == "moved":
        _move_head(github, worker, published_id, "f" * 40)
    if live == "unreadable":
        with monkeypatch.context() as patched:
            patched.setattr(github, "reconcile_advanced_head", unreadable)
            with pytest.raises(RuntimeError, match="unreachable"):
                _publish(worker, github, correction, staged["digest"])
        assert worker.get(correction).state == "publishing"
        assert worker._approvals.get(correction).state == "consuming"
        return
    if live in {"landed", "parent_valid"}:
        record = _publish(worker, github, correction, staged["digest"])
        assert record.state == "published" and record.publication["parent_head_sha"] == head
        assert record.publication["head_sha"] == _pull(github, worker, published_id).head_sha != head
        assert worker._approvals.get(correction).state == "consumed"
        assert worker.superseded_by(published_id) == correction
        return
    with pytest.raises(PermissionError, match="budget is gone" if live == "parent_expired" else "head moved"):
        _publish(worker, github, correction, staged["digest"])
    failed = worker.get(correction)
    assert failed.state == "failed"
    assert failed.publication["push_outcome"] == ("not_applied" if live == "parent_expired" else "moved")
    assert worker._approvals.get(correction).state == "consumed"
    assert worker.correction_releases_parent(correction) is (live == "parent_expired")


def _interrupted_correction(worker, github, make, monkeypatch, *, landed):
    """A publish that stopped mid-push with GitHub unreadable: publishing, approval still consuming."""
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    advance = github.advance_draft_pull_request_head

    def crashed(**kwargs):
        if landed:
            advance(**kwargs)
        raise KeyboardInterrupt("process stopped mid-push")

    def unreadable(**kwargs):
        raise RuntimeError("GitHub unreachable")

    with monkeypatch.context() as patched:
        patched.setattr(github, "advance_draft_pull_request_head", crashed)
        patched.setattr(github, "reconcile_advanced_head", unreadable)
        with pytest.raises(KeyboardInterrupt):
            _publish(worker, github, correction, staged["digest"])
    assert worker.get(correction).state == "publishing"
    return correction, staged["digest"]


def _count_github_calls(github, monkeypatch):
    calls = []
    for name in ("reconcile_advanced_head", "advance_draft_pull_request_head"):
        original = getattr(github, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(github, name, counted)
    return calls


@pytest.mark.parametrize("binding", ["digest", "operator", "receipt", "blob_shas", "file_modes", "repository",
                                     "branch", "parent", "preview"])
def test_resume_refuses_a_mismatched_binding_before_touching_github(binding, correction_chain, monkeypatch):
    worker, _, github, _, make = correction_chain
    correction, digest = _interrupted_correction(worker, github, make, monkeypatch, landed=True)
    state_path = worker._task_dir(correction) / "state.json"
    approval = worker._approvals.get(correction)
    arguments = dict(approval_digest=digest, review_receipt_digest=RECEIPT, actor_id="operator")
    if binding == "digest":
        arguments["approval_digest"] = "f" * 64
    elif binding == "operator":
        arguments["actor_id"] = "someone-else"
    elif binding == "receipt":
        arguments["review_receipt_digest"] = "b" * 64
    else:
        raw = json.loads(state_path.read_text())
        publication = raw["record"]["publication"]
        if binding == "blob_shas":
            path = next(iter(publication["blob_shas"]))
            publication["blob_shas"][path] = "0" * 40
        elif binding == "file_modes":
            path = next(iter(publication["file_modes"]))
            publication["file_modes"][path] = "100755"
        elif binding == "repository":
            publication["repository"] = "fixture/elsewhere"
        if binding in {"parent", "preview", "branch"}:
            # Bind the in-flight approval to a different parent/preview/branch than the saved record.
            key, value = {"parent": ("parent_head_sha", "c" * 40), "preview": ("preview_id", "forged-preview"),
                          "branch": ("head_branch", approval.snapshot["head_branch"] + "-forged")}[binding]
            snapshot = {**approval.snapshot, key: value}
            monkeypatch.setattr(worker._approvals, "get",
                                lambda preview_id: approval.model_copy(update={"snapshot": snapshot}))
        else:
            state_path.write_text(json.dumps(raw))
    before = state_path.read_bytes()
    calls = _count_github_calls(github, monkeypatch)
    with pytest.raises(PermissionError):
        worker.approve_and_publish_correction(
            correction, github=github, require_human_approval_for_repo_writes=True, allow_mock_publication=True,
            **arguments)
    assert calls == []
    assert state_path.read_bytes() == before
    assert approval.state == "consuming"
    if binding not in {"parent", "preview", "branch"}:
        assert worker._approvals.get(correction).state == "consuming"


@pytest.mark.parametrize("pull_change", [{"draft": False}, {"state": "closed"}], ids=["undrafted", "closed"])
def test_resume_that_finds_our_push_on_a_closed_or_ready_pr_stays_publishing(pull_change, correction_chain, monkeypatch):
    worker, published_id, github, head, make = correction_chain
    correction, digest = _interrupted_correction(worker, github, make, monkeypatch, landed=True)
    publication = worker.get(published_id).publication
    pulls = github.pull_requests[publication["repository"]]
    pulls[publication["pull_number"]] = replace(pulls[publication["pull_number"]], **pull_change)
    calls = _count_github_calls(github, monkeypatch)
    with pytest.raises(RuntimeError, match="pinned open draft"):
        _publish(worker, github, correction, digest)
    assert calls == ["reconcile_advanced_head"]
    assert worker.get(correction).state == "publishing"
    assert worker._approvals.get(correction).state == "consuming"


def test_resume_with_a_missing_budget_file_still_ends_not_applied(correction_chain, monkeypatch):
    worker, published_id, github, head, make = correction_chain
    correction, digest = _interrupted_correction(worker, github, make, monkeypatch, landed=False)
    worker.budget_path(correction).unlink()
    with pytest.raises(PermissionError, match="budget is gone"):
        _publish(worker, github, correction, digest)
    failed = worker.get(correction)
    assert failed.state == "failed" and failed.publication["push_outcome"] == "not_applied"
    assert worker._approvals.get(correction).state == "consumed"
    assert worker.correction_releases_parent(correction)


def test_push_that_did_not_apply_releases_the_parent_head(correction_chain, monkeypatch):
    worker, published_id, github, head, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)

    def refused(**kwargs):
        raise RuntimeError("GitHub rejected the ref update")

    with monkeypatch.context() as patched:
        patched.setattr(github, "advance_draft_pull_request_head", refused)
        with pytest.raises(RuntimeError, match="rejected"):
            _publish(worker, github, correction, staged["digest"])
    failed = worker.get(correction)
    assert failed.state == "failed" and failed.error == "GitHub rejected the ref update"
    assert failed.publication["push_outcome"] == "not_applied" and failed.publication["live_head_sha"] == head
    approval = worker._approvals.get(correction)
    assert approval.state == "consumed" and approval.error == "GitHub rejected the ref update"
    with pytest.raises((ValueError, PermissionError)):
        _publish(worker, github, correction, staged["digest"])
    assert worker.superseded_by(published_id) is None
    assert worker.correction_releases_parent(correction)
    sibling = make("review-two", "def probe():\n    return 3\n")
    sibling_staged = worker.stage_correction_publication(sibling, review_receipt_digest=RECEIPT)
    assert _publish(worker, github, sibling, sibling_staged["digest"]).state == "published"
    assert worker.superseded_by(published_id) == sibling


def test_budget_aborted_during_the_advance_sends_no_write(correction_chain, monkeypatch):
    """The adapter re-checks the original ledger before every write, not only at the preflight."""
    worker, published_id, github, head, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    record = worker.get(correction)
    budget = DeveloperTaskBudget(path=worker.budget_path(correction),
                                 bundle=developer_task_bundle_from_payload(record.bundle_payload), create=False)
    advance, commits = github.advance_draft_pull_request_head, len(github.branch_commits)
    hooks = []

    def aborted_mid_call(**kwargs):
        hooks.append(kwargs["before_write"])
        budget.abort()  # e.g. an operator abort or expiry after the preflight check passed
        return advance(**kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(github, "advance_draft_pull_request_head", aborted_mid_call)
        with pytest.raises((TimeoutError, PermissionError, ValueError)):
            _publish(worker, github, correction, staged["digest"])
    assert hooks and hooks[0].__self__.path == budget.path  # the original ledger, not a copy of its deadline
    assert len(github.branch_commits) == commits and _pull(github, worker, published_id).head_sha == head
    failed = worker.get(correction)
    assert failed.state == "failed" and failed.publication["push_outcome"] == "not_applied"
    assert worker.correction_releases_parent(correction)


def test_push_that_landed_settles_as_published_even_after_the_budget_is_gone(correction_chain, monkeypatch):
    """Reconcile is read-only, so a landed push is never stranded in publishing by an expired budget."""
    worker, published_id, github, head, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    budget = DeveloperTaskBudget(path=worker.budget_path(correction),
                                 bundle=developer_task_bundle_from_payload(worker.get(correction).bundle_payload),
                                 create=False)
    advance = github.advance_draft_pull_request_head

    def landed_then_expired(**kwargs):
        advance(**kwargs)
        budget.abort()
        raise RuntimeError("response lost after the push applied")

    with monkeypatch.context() as patched:
        patched.setattr(github, "advance_draft_pull_request_head", landed_then_expired)
        record = _publish(worker, github, correction, staged["digest"])
    assert record.state == "published" and record.publication["parent_head_sha"] == head
    assert record.publication["head_sha"] == _pull(github, worker, published_id).head_sha != head
    assert worker._approvals.get(correction).state == "consumed"
    assert worker.superseded_by(published_id) == correction


def test_moved_head_keeps_holding_and_cannot_be_retired(correction_chain, monkeypatch):
    worker, published_id, github, head, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)

    def raced(**kwargs):
        _move_head(github, worker, published_id, "f" * 40)
        raise RuntimeError("someone else pushed first")

    with monkeypatch.context() as patched:
        patched.setattr(github, "advance_draft_pull_request_head", raced)
        with pytest.raises(RuntimeError, match="pushed first"):
            _publish(worker, github, correction, staged["digest"])
    failed = worker.get(correction)
    assert failed.state == "failed" and failed.publication["push_outcome"] == "moved"
    assert failed.publication["live_head_sha"] == "f" * 40
    assert not worker.correction_releases_parent(correction)
    with pytest.raises(PermissionError, match="parent head"):
        worker.retire_correction(correction, actor_id="operator", github=github, allow_mock_publication=True)
    sibling = make("review-two", "def probe():\n    return 3\n")
    with pytest.raises(PermissionError, match=f"{correction} already uses parent head {head}"):
        worker.stage_correction_publication(sibling, review_receipt_digest=RECEIPT)


def test_retire_releases_an_abandoned_correction_only_at_the_parent_head(correction_chain):
    worker, published_id, github, head, make = correction_chain
    abandoned = make()
    staged = worker.stage_correction_publication(abandoned, review_receipt_digest=RECEIPT)
    with pytest.raises(ValueError, match="live GitHub adapter"):
        worker.retire_correction(abandoned, actor_id="operator", github=github)
    with pytest.raises(PermissionError, match="operator identity"):
        worker.retire_correction(abandoned, actor_id=" ", github=github, allow_mock_publication=True)
    _move_head(github, worker, published_id, "f" * 40)
    with pytest.raises(PermissionError, match="parent head"):
        worker.retire_correction(abandoned, actor_id="operator", github=github, allow_mock_publication=True)
    assert worker.get(abandoned).state == "verified" and worker._approvals.get(abandoned).state == "pending"
    _move_head(github, worker, published_id, head)
    retired = worker.retire_correction(abandoned, actor_id="operator", github=github, allow_mock_publication=True)
    assert retired.state == "retired" and retired.retirement["previous_state"] == "verified"
    assert retired.retirement["actor_id"] == "operator" and retired.retirement["live_head_sha"] == head
    assert worker.get(abandoned) == retired
    assert worker.retire_correction(abandoned, actor_id="operator", github=github, allow_mock_publication=True) == retired
    assert worker._approvals.get(abandoned).state == "invalidated"
    with pytest.raises(ValueError, match="verified"):
        _publish(worker, github, abandoned, staged["digest"])
    assert worker.correction_releases_parent(abandoned)
    sibling = make("review-two", "def probe():\n    return 3\n")
    sibling_staged = worker.stage_correction_publication(sibling, review_receipt_digest=RECEIPT)
    assert _publish(worker, github, sibling, sibling_staged["digest"]).state == "published"
    with pytest.raises(PermissionError, match="published correction cannot be retired"):
        worker.retire_correction(sibling, actor_id="operator", github=github, allow_mock_publication=True)
    with pytest.raises(PermissionError, match="saved correction"):
        worker.retire_correction(published_id, actor_id="operator", github=github, allow_mock_publication=True)
    directory = worker._task_dir(abandoned)
    worker._save(directory, replace(retired, retirement=retired.retirement | {"live_head_sha": "e" * 40}))
    with pytest.raises(ValueError, match="retirement"):
        worker.get(abandoned)


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
        with monkeypatch.context() as patched:
            patched.setattr(worker._previews, "get", lambda preview_id, target=target: broken(preview_id, target))
            with pytest.raises(PermissionError, match="Correction chain"):
                worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
        assert worker._approvals.get(correction) is None and worker.get(correction).state == "verified"
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
    monkeypatch.setattr(worker, "_correction_records", lambda relevant: [*records(relevant), replace(published, preview_id="forked-copy")])
    with pytest.raises(PermissionError, match="forked"):
        worker.superseded_by(published_id)
    with pytest.raises(PermissionError, match="forked"):
        worker.get_published_pull_request(published_id, github=github)


def test_chain_walk_refuses_a_mismatched_link(correction_chain, monkeypatch):
    worker, published_id, github, head, make = correction_chain
    correction = make()
    real_load = worker._load_preview_record
    original = worker.get(published_id)
    other_issue = json.loads(json.dumps(original.bundle_payload))
    other_issue["issue_context"]["repository_id"] = 999
    forgeries = (
        replace(original, publication=original.publication | {"head_sha": "e" * 40}),
        replace(original, publication=original.publication | {"repository": "fixture/other"}),
        replace(original, publication=original.publication | {"branch": "aitobuild/other"}),
        replace(original, bundle_payload=other_issue),
    )
    for forged in forgeries:
        with monkeypatch.context() as patched:
            patched.setattr(worker, "_load_preview_record",
                            lambda preview_id, forged=forged: forged if preview_id == published_id else real_load(preview_id))
            with pytest.raises(PermissionError, match="repository/PR/branch/head mismatch"):
                worker.correction_target(correction)
    assert worker.correction_target(correction)["chain"] == [published_id]


def test_unbound_architect_review_is_never_reposted(correction_chain, monkeypatch):
    worker, published_id, github, _, _ = correction_chain
    submit = github.submit_pr_review
    calls = []

    def misbound(**kwargs):
        calls.append(kwargs)
        return replace(submit(**kwargs), commit_id="d" * 40)

    monkeypatch.setattr(github, "submit_pr_review", misbound)
    with pytest.raises(RuntimeError, match="different commit"):
        worker.submit_architect_review(published_id, github=github, event="COMMENT", body="first")
    with pytest.raises(PermissionError, match="already posted"):
        worker.submit_architect_review(published_id, github=github, event="COMMENT", body="first")
    assert len(calls) == 1 and worker.get(published_id).architect_review is None


def _forge_correction_file(worker, source_id, name, mutate):
    raw = json.loads((worker._task_dir(source_id) / "state.json").read_text())
    mutate(raw["record"])
    directory = worker._state_dir / "deliveries" / name
    directory.mkdir()
    (directory / "state.json").write_text(json.dumps(raw))
    return directory


def test_only_related_corrupt_records_block_correction_checks(correction_chain):
    worker, published_id, github, _, make = correction_chain
    correction = make()
    staged = worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    _publish(worker, github, correction, staged["digest"])

    def unrelated(record):
        record["publication"] = record["publication"] | {"repository": "fixture/other"}
        del record["publication"]["title"]

    _forge_correction_file(worker, correction, "unrelated-corrupt", unrelated)
    assert worker.superseded_by(published_id) == correction
    assert worker.get_published_pull_request(correction, github=github)["head_matches_publication"]

    def related(record):
        del record["publication"]["title"]

    related_dir = _forge_correction_file(worker, correction, "related-corrupt", related)
    with pytest.raises(PermissionError, match="failed validation"):
        worker.superseded_by(published_id)
    (related_dir / "state.json").write_text("not json")
    with pytest.raises(PermissionError, match="unreadable"):
        worker.superseded_by(published_id)


def test_correction_without_issue_context_fails_validation(correction_chain):
    worker, published_id, github, _, make = correction_chain
    correction = make()
    directory = worker._task_dir(correction)
    raw = json.loads((directory / "state.json").read_text())
    raw["record"]["bundle_payload"].pop("issue_context")
    (directory / "state.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        worker.get(correction)
    with pytest.raises(PermissionError, match="failed validation"):
        worker.correction_target(correction)
    with pytest.raises((ValueError, PermissionError)):
        worker.stage_correction_publication(correction, review_receipt_digest=RECEIPT)
    with pytest.raises((ValueError, PermissionError)):
        worker.retire_correction(correction, actor_id="operator", github=github, allow_mock_publication=True)


def test_approval_store_is_single_use_and_tamper_evident(tmp_path):
    store = PublishApprovalStore(tmp_path / "approvals")
    snapshot = {"preview_id": "p1", "diff": "x"}
    first = store.stage("p1", snapshot)
    assert store.stage("p1", snapshot) == first
    with pytest.raises(ValueError):
        store.stage("p2", snapshot)
    changed = store.stage("p1", {"preview_id": "p1", "diff": "y"})
    assert changed.replaced_digest == first.digest and changed.digest != first.digest
    with pytest.raises(PermissionError):
        store.begin_consume("p1", digest=first.digest, recomputed_content_digest=first.content_digest, actor_id="op")
    store.invalidate("p1", reason="drift")
    assert store.get("p1").state == "invalidated"
    again = store.stage("p1", {"preview_id": "p1", "diff": "y"})
    assert again.content_digest == changed.content_digest and again.digest != changed.digest
    digest, content = again.digest, again.content_digest
    with pytest.raises(PermissionError, match="operator identity"):
        store.begin_consume("p1", digest=digest, recomputed_content_digest=content, actor_id=" ")
    with pytest.raises(PermissionError, match="drifted"):
        store.begin_consume("p1", digest=digest, recomputed_content_digest="0" * 64, actor_id="op")
    assert store.get("p1").state == "invalidated"
    again = store.stage("p1", {"preview_id": "p1", "diff": "y"})
    digest, content = again.digest, again.content_digest
    assert store.begin_consume("p1", digest=digest, recomputed_content_digest=content, actor_id="op").state == "consuming"
    with pytest.raises(PermissionError, match="already consumed"):
        store.stage("p1", {"preview_id": "p1", "diff": "z"})
    with pytest.raises(PermissionError):
        store.invalidate("p1", reason="late")
    consumed = store.finish_consume("p1", digest=digest, head_sha="c" * 40)
    assert consumed.state == "consumed" and consumed.approved_by == "op" and consumed.error is None
    with pytest.raises(PermissionError, match="already consumed"):
        store.begin_consume("p1", digest=digest, recomputed_content_digest=content, actor_id="op")
    path = next((tmp_path / "approvals").glob("*.json"))
    # Backstop under every public method: consumed is final, and states never skip or go back.
    revived = PublishApproval.model_validate({
        "preview_id": "p1", "snapshot": consumed.snapshot, "content_digest": content, "nonce": "1" * 32,
        "digest": approval_digest(content, "1" * 32), "state": "pending", "staged_at": consumed.staged_at,
        "replaced_digest": consumed.digest})
    for original, updated in ((consumed, revived), (consumed, consumed), (again, consumed),
                              (store.get("p1"), revived)):
        with pytest.raises(PermissionError, match="only moves forward"):
            store._save(path, original, updated)
    assert store.get("p1") == consumed
    pristine = path.read_text()
    for tamper in (
        lambda data: data["snapshot"].__setitem__("diff", "tampered"),
        lambda data: data.__setitem__("nonce", "0" * 32),
        lambda data: data.__setitem__("extra", True),
        lambda data: data.__setitem__("state", "pending"),
        lambda data: data.__setitem__("error", "and a head"),
    ):
        data = json.loads(pristine)
        tamper(data)
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="Invalid persisted"):
            store.get("p1")


@pytest.mark.parametrize("abort_during", ["upsert_branch_commit", "create_or_update_draft_pull_request"])
def test_ordinary_publish_rechecks_the_budget_inside_each_adapter_write(
        abort_during, implemented_delivery, verification_adapter, monkeypatch):
    worker, preview_id, _, _ = implemented_delivery
    monkeypatch.setattr("aitobuild.developer_delivery.shell_request", lambda *args, **kwargs: {
        "ok": True, "status": "exited", "exit_code": 0, "output": "passed", "next_cursor": 1,
    })
    worker.verify(preview_id, adapter=verification_adapter)
    github = MockGitHubAdapter(allowed_repositories=frozenset({"fixture/widgets"}), enforce_allowlist=True)
    budget = DeveloperTaskBudget(path=worker.budget_path(preview_id),
                                 bundle=developer_task_bundle_from_payload(worker.get(preview_id).bundle_payload),
                                 create=False)
    original = getattr(github, abort_during)

    def aborted_mid_call(**kwargs):
        budget.abort()  # the preflight already passed; the adapter must still refuse the write
        return original(**kwargs)

    monkeypatch.setattr(github, abort_during, aborted_mid_call)
    record = worker.publish(preview_id, github=github, require_human_approval_for_repo_writes=True,
                            allow_mock_publication=True)
    assert record.state == "failed"
    if abort_during == "upsert_branch_commit":
        assert github.branch_commits == []
    assert github.pull_requests.get("fixture/widgets", {}) == {}


def test_budget_gone_after_the_branch_landed_reports_the_orphan_branch(
        implemented_delivery, verification_adapter, monkeypatch):
    worker, preview_id, _, _ = implemented_delivery
    monkeypatch.setattr("aitobuild.developer_delivery.shell_request", lambda *args, **kwargs: {
        "ok": True, "status": "exited", "exit_code": 0, "output": "passed", "next_cursor": 1,
    })
    worker.verify(preview_id, adapter=verification_adapter)
    github = MockGitHubAdapter(allowed_repositories=frozenset({"fixture/widgets"}), enforce_allowlist=True)
    budget = DeveloperTaskBudget(path=worker.budget_path(preview_id),
                                 bundle=developer_task_bundle_from_payload(worker.get(preview_id).bundle_payload),
                                 create=False)
    upsert = github.upsert_branch_commit

    def landed_then_budget_gone(**kwargs):
        head = upsert(**kwargs)
        budget.abort()
        return head

    monkeypatch.setattr(github, "upsert_branch_commit", landed_then_budget_gone)
    record = worker.publish(preview_id, github=github, require_human_approval_for_repo_writes=True,
                            allow_mock_publication=True)
    assert record.state == "failed"
    assert github.branch_commits and github.pull_requests.get("fixture/widgets", {}) == {}
    assert record.publication["orphan_branch"] == record.branch
    assert record.publication["head_sha"] == github.branch_commits[-1]["head_sha"]
    assert "has no saved pull request" in record.error
    assert worker.get(preview_id) == record
