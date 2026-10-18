"""#66: the one runtime-budget exemption. An interrupted publication whose PR is already open is adopted on resume
even after the Developer budget expired; nothing that would call the model, run the Developer, push or create a
PR runs on an expired budget, and every adoption check (#56 binding, #51 marker/body/draft/author, live head)
still applies in full. A refusal leaves the delivery record at ``publishing`` and fails the run as before.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace as dc_replace
from datetime import datetime
import json
from pathlib import Path
import time

import pytest

from aitobuild.developer_delivery import DeveloperDeliveryWorker, delivery_marker
from aitobuild.organization_runner import ManagedWorkflowRunner
from aitobuild.organization_delivery import ManagedDeliveryBindings
from aitobuild.organization_runner import FileRunStore, ManagedRun
from aitobuild.tools.github import GitHubPullRequest, MockGitHubAdapter
from test_dispatcher import approved_delivery as approved_delivery, verification_adapter as verification_adapter
from test_interrupted_publication import REPO, build, kill_during_publication, restart

SNAPSHOT_KEYS = ("title", "body", "commit_message", "checkout_digest", "tree_fingerprint", "changed_paths",
                 "blob_shas", "file_modes", "base_ref", "branch", "repository", "issue_number", "base_sha")


def _budget_path(tmp_path: Path) -> Path:
    paths = list((tmp_path / "state" / "budgets").glob("*.json"))
    assert len(paths) == 1
    return paths[0]


def expire(tmp_path: Path) -> float:
    path = _budget_path(tmp_path)
    state = json.loads(path.read_text())
    state["deadline"] = time.time() - 120
    path.write_text(json.dumps(state))
    return state["deadline"]


class Spy:
    """Records every model/Developer entry point and every GitHub write; each one raises if reached."""

    def __init__(self, monkeypatch, github: MockGitHubAdapter) -> None:
        self.calls: list[str] = []

        def refuse(name):
            def call(*args, **kwargs):
                self.calls.append(name)
                raise AssertionError(f"unexpected {name}")
            return call

        for name in ("implement", "continue_implementation", "verify", "publish", "continue_publication"):
            monkeypatch.setattr(ManagedDeliveryBindings, name, refuse("bindings." + name))
        for name in ("begin_implementation", "publish", "verify"):
            monkeypatch.setattr(DeveloperDeliveryWorker, name, refuse("worker." + name))
        for name in ("upsert_branch_commit", "create_or_update_draft_pull_request", "advance_draft_pull_request_head",
                     "reconcile_advanced_head", "submit_pr_review", "create_issue", "update_issue"):
            monkeypatch.setattr(github, name, refuse("github." + name))


def interrupted(tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
                point="after_pr_before_record"):
    github, worker, preview_id, waiting = kill_during_publication(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, point)
    record = worker.get(preview_id)
    assert record.state == "publishing" and "pull_number" not in record.publication
    return github, worker, preview_id, waiting, record


def assert_refused(run, worker, preview_id, record, github, pulls_before, spy, waiting):
    assert run.state == "failed" and run.error.startswith("TimeoutError"), run.error
    assert run.adopted_after_budget_expiry is None
    after = worker.get(preview_id)
    assert after.state == "publishing"
    assert after.publication == record.publication and "adopted_after_budget_expiry" not in after.publication
    assert github.pull_requests.get(REPO, {}) == pulls_before
    assert spy.calls == []
    assert [decision.request_id for decision in run.decisions] == [waiting.pending[0].request_id]


def test_expired_budget_adopts_the_matching_open_pr(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, waiting, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    deadline = expire(tmp_path)
    budget_before = _budget_path(tmp_path).read_text()
    pulls_before = dict(github.pull_requests[REPO])
    heads_before = json.loads(json.dumps(github._branch_heads))
    spy = Spy(monkeypatch, github)
    run, worker, preview_id = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert run.state == "completed", run.error
    assert spy.calls == []  # no model, Developer, verification, push or PR write
    assert github.pull_requests[REPO] == pulls_before and github._branch_heads == heads_before
    # No re-admission or re-approval: the original human decision is still the only one.
    assert [decision.request_id for decision in run.decisions] == [waiting.pending[0].request_id]
    assert run.decisions[0].approval_policy.source == "human"
    published = worker.get(preview_id)
    (pull,) = pulls_before.values()
    assert published.state == "published" and published.head_revision == record.publication["head_sha"]
    assert published.publication["pull_number"] == pull.number and published.publication["draft"] is True
    receipt = published.publication["adopted_after_budget_expiry"]
    assert set(receipt) == {"deadline", "adopted_at"}
    assert abs(datetime.fromisoformat(receipt["deadline"]).timestamp() - deadline) < 1e-3
    assert datetime.fromisoformat(receipt["adopted_at"]).timestamp() > deadline
    assert run.adopted_after_budget_expiry.deadline == deadline
    assert run.adopted_after_budget_expiry.request_id == waiting.pending[0].request_id
    assert run.outputs[-1] == {"preview_id": preview_id, "state": "published"}
    runner = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)[0]
    assignment = runner._assignments.get(run.assignment_id)
    assert assignment.state == "completed" and "budget-expired PR adoption" in assignment.outcome
    # The ledger is neither extended nor reset.
    assert _budget_path(tmp_path).read_text() == budget_before
    # Idempotent afterwards: the terminal run is returned unchanged with no further effect.
    again, *_ = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert again == run and spy.calls == [] and github.pull_requests[REPO] == pulls_before


def test_expired_budget_without_a_pr_is_refused_and_stays_publishing(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, waiting, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, "after_push_before_pr")
    assert not github.pull_requests.get(REPO)
    expire(tmp_path)
    spy = Spy(monkeypatch, github)
    heads_before = json.loads(json.dumps(github._branch_heads))
    run, worker, preview_id = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert_refused(run, worker, preview_id, record, github, {}, spy, waiting)
    assert "No open draft PR to adopt" in run.error
    assert github._branch_heads == heads_before


@pytest.mark.parametrize("tamper", ["pr_marker", "body", "title", "draft", "author", "live_head", "pr_head",
                                    "second_open", "binding", "approval_snapshot"])
def test_expired_budget_refuses_each_failing_check_on_its_own(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, tamper,
):
    github, worker, preview_id, waiting, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    expire(tmp_path)
    pulls = github.pull_requests[REPO]
    (number,) = pulls
    pull = pulls[number]
    if tamper == "pr_marker":
        # The open PR's body lost this task's delivery marker (the worker-only marker check is isolated below).
        marker = delivery_marker(preview_id, record.bundle_payload)
        assert marker in pull.body
        pulls[number] = dc_replace(pull, body=pull.body.replace(marker, ""))
    elif tamper == "body":
        pulls[number] = dc_replace(pull, body=pull.body + "\nedited")
    elif tamper == "title":
        pulls[number] = dc_replace(pull, title=pull.title + " (edited)")
    elif tamper == "draft":
        pulls[number] = dc_replace(pull, draft=False)
    elif tamper == "author":
        pulls[number] = dc_replace(pull, author="intruder")
    elif tamper == "live_head":
        github._branch_heads[REPO][record.branch] = "f" * 40
    elif tamper == "pr_head":
        pulls[number] = dc_replace(pull, head_sha="e" * 40)
    elif tamper == "second_open":
        pulls[number + 1] = GitHubPullRequest(
            number=number + 1, title=pull.title, body=pull.body, state="open", head_ref=pull.head_ref,
            base_ref=pull.base_ref, draft=True, repository=REPO, head_sha=pull.head_sha, author=pull.author)
    elif tamper == "binding":
        # The saved record no longer matches its run: the runner's #56 binding check refuses first.
        path = next((tmp_path / "state" / "deliveries").glob("*/state.json"))
        raw = json.loads(path.read_text())
        raw["record"]["session_id"] = "session-" + "0" * 32
        path.write_text(json.dumps(raw))
        record = worker.get(preview_id)
    else:
        # The worker re-checks the exact approved snapshot itself, even if the binding hook were bypassed.
        original = ManagedDeliveryBindings.adopt_after_budget_expiry

        async def tampered(self, context, data):
            return await original(self, context, {**data, "commit_message": data["commit_message"] + " (edited)"})
        monkeypatch.setattr(ManagedDeliveryBindings, "interrupted_publication", lambda self, context, data: True)
        monkeypatch.setattr(ManagedDeliveryBindings, "adopt_after_budget_expiry", tampered)
    pulls_before = dict(pulls)
    heads_before = json.loads(json.dumps(github._branch_heads))
    spy = Spy(monkeypatch, github)
    run, worker, preview_id = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert_refused(run, worker, preview_id, record, github, pulls_before, spy, waiting)
    assert github._branch_heads == heads_before
    if tamper == "binding":
        assert run.error == "TimeoutError"  # no interrupted continuation proven: exactly today's refusal
    else:
        assert "budget-expired adoption refused" in run.error


def test_expired_budget_never_pushes_when_the_head_was_not_saved(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    """Killed after the push but before the head was recorded: finishing would need a push, so it is refused."""
    github, worker, preview_id, waiting, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, "after_push_head_unrecorded")
    assert "head_sha" not in record.publication
    expire(tmp_path)
    spy = Spy(monkeypatch, github)
    run, worker, preview_id = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert_refused(run, worker, preview_id, record, github, {}, spy, waiting)
    assert "push is not allowed" in run.error


def test_expired_budget_never_runs_the_developer_for_an_interrupted_implementation(
    tmp_path, approved_delivery, verification_adapter, monkeypatch,
):
    """A run killed before publishing (record still ``verified``) has nothing to adopt: the model path stays shut."""
    from aitobuild.organization_runner import WorkflowDecision, workflow_input_digest
    github = MockGitHubAdapter(allowed_repositories=frozenset({REPO}), enforce_allowlist=True)
    runner, worker, assignment_id, preview_id = build(tmp_path, approved_delivery, verification_adapter,
                                                      monkeypatch, github)
    waiting = asyncio.run(runner.start(assignment_id))
    store = FileRunStore(tmp_path / "runs")
    pending = waiting.pending[0]
    store.save(ManagedRun.model_validate(waiting.model_dump() | {
        "state": "running", "pending": (),
        "decisions": (WorkflowDecision(request_id=pending.request_id, kind=pending.kind, actor_id="operator",
                                       value=True, data_digest=workflow_input_digest(pending.data)),),
    }))
    expire(tmp_path)
    spy = Spy(monkeypatch, github)
    failed, *_ = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert failed.state == "failed" and failed.error == "TimeoutError"
    assert spy.calls == [] and not github.branch_commits and not github.pull_requests.get(REPO)
    assert worker.get(preview_id).state == "verified"


def test_expired_budget_still_refuses_a_waiting_publication_approval(
    tmp_path, approved_delivery, verification_adapter, monkeypatch,
):
    """A fresh approval on an expired budget would push and create a PR: it is refused before any write."""
    github = MockGitHubAdapter(allowed_repositories=frozenset({REPO}), enforce_allowlist=True)
    runner, worker, assignment_id, preview_id = build(tmp_path, approved_delivery, verification_adapter,
                                                      monkeypatch, github)
    waiting = asyncio.run(runner.start(assignment_id))
    expire(tmp_path)
    run = asyncio.run(runner.approve(assignment_id, request_id=waiting.pending[0].request_id, approved=True))
    assert run.state == "failed" and run.adopted_after_budget_expiry is None
    assert not github.branch_commits and not github.pull_requests.get(REPO)
    assert worker.get(preview_id).state == "verified"


def test_expired_budget_ordinary_publish_stays_fail_closed(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    """The normal publish path (push + PR create/update) is untouched: an expired budget refuses it."""
    github, worker, preview_id, _, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    expire(tmp_path)
    writes: list[str] = []
    for name in ("upsert_branch_commit", "create_or_update_draft_pull_request"):
        monkeypatch.setattr(github, name, lambda name=name, **kwargs: writes.append(name))
    failed = worker.publish(preview_id, github=github, require_human_approval_for_repo_writes=True,
                            allow_mock_publication=True)
    assert failed.state == "failed" and "budget has expired" in failed.error and writes == []


def test_adoption_refuses_a_live_budget_and_an_aborted_ledger(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, _, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    snapshot = {key: record.publication[key] for key in SNAPSHOT_KEYS}
    snapshot |= {"preview_id": preview_id, "approved_at": record.approved_at, "repository_id": 101}

    def adopt():
        return worker.adopt_after_budget_expiry(preview_id, github=github, expected_publication=snapshot,
                                                allow_mock_publication=True)
    with pytest.raises(ValueError, match="has not expired"):
        adopt()  # a live budget takes the normal budgeted path, never this one
    with pytest.raises(ValueError, match="mock publication is refused"):
        worker.adopt_after_budget_expiry(preview_id, github=github, expected_publication=snapshot)
    expire(tmp_path)
    path = _budget_path(tmp_path)
    state = json.loads(path.read_text())
    path.write_text(json.dumps(state | {"aborted": True}))
    with pytest.raises(TimeoutError, match="aborted"):
        adopt()
    path.write_text(json.dumps(state))
    assert worker.get(preview_id).state == "publishing"
    assert adopt().state == "published"
    assert worker.get(preview_id).publication["adopted_after_budget_expiry"]["deadline"]


def test_live_budget_resume_is_unchanged(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, waiting, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    calls: list[str] = []
    create = github.create_or_update_draft_pull_request

    def counting_create(**kwargs):
        calls.append("create_or_update_draft_pull_request")
        return create(**kwargs)
    monkeypatch.setattr(github, "create_or_update_draft_pull_request", counting_create)
    monkeypatch.setattr(DeveloperDeliveryWorker, "adopt_after_budget_expiry",
                        lambda *args, **kwargs: calls.append("adopt_after_budget_expiry"))
    run, worker, preview_id = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert run.state == "completed", run.error
    assert calls == ["create_or_update_draft_pull_request"]  # today's #56 resume path, not the exemption
    assert run.adopted_after_budget_expiry is None
    published = worker.get(preview_id)
    assert published.state == "published" and "adopted_after_budget_expiry" not in published.publication
    assert len(github.pull_requests[REPO]) == 1


def test_adoption_receipts_load_and_older_records_without_them_still_load(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, _, _ = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    expire(tmp_path)
    run, worker, preview_id = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert run.state == "completed", run.error
    path = next((tmp_path / "state" / "deliveries").glob("*/state.json"))
    raw = json.loads(path.read_text())
    receipt = raw["record"]["publication"]["adopted_after_budget_expiry"]
    # An older published record carries no receipt and loads as before.
    del raw["record"]["publication"]["adopted_after_budget_expiry"]
    path.write_text(json.dumps(raw))
    assert worker.get(preview_id).state == "published"
    for bad in ({**receipt, "extra": "x"}, {"deadline": receipt["deadline"]}, "yes",
                {"deadline": receipt["adopted_at"], "adopted_at": receipt["deadline"]}):
        raw["record"]["publication"]["adopted_after_budget_expiry"] = bad
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError):
            worker.get(preview_id)
    # A receipt is only valid on a published record.
    raw["record"]["publication"]["adopted_after_budget_expiry"] = receipt
    raw["record"]["state"] = "publishing"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        worker.get(preview_id)
    # Older run receipts have no adoption field and load with None; a saved receipt is immutable.
    dumped = run.model_dump(mode="json")
    del dumped["adopted_after_budget_expiry"]
    assert ManagedRun.model_validate(dumped).adopted_after_budget_expiry is None
    with pytest.raises(ValueError):
        FileRunStore(tmp_path / "runs").save(
            ManagedRun.model_validate(run.model_dump() | {"adopted_after_budget_expiry": None}))


def test_run_adoption_receipt_must_name_the_last_decision(tmp_path, approved_delivery, verification_adapter,
                                                          monkeypatch):
    github = MockGitHubAdapter(allowed_repositories=frozenset({REPO}), enforce_allowlist=True)
    runner, _, assignment_id, _ = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    waiting = asyncio.run(runner.start(assignment_id))
    receipt = {"request_id": waiting.pending[0].request_id, "deadline": 1.0,
               "adopted_at": "2026-10-18T00:00:00+00:00"}
    with pytest.raises(ValueError, match="last recorded decision"):
        ManagedRun.model_validate(waiting.model_dump() | {"adopted_after_budget_expiry": receipt})


def test_finish_binds_the_adoption_to_the_expired_ledger_deadline(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, *_ = interrupted(tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    runner, _, assignment_id, _ = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    service = runner._service
    with pytest.raises(ValueError, match="has not expired"):
        service.revalidate_expired(assignment_id)
    with pytest.raises(ValueError, match="has not expired"):
        service.finish(assignment_id, state="completed", outcome="x", adopted_after_budget_expiry=time.time())
    deadline = expire(tmp_path)
    assert service.revalidate_expired(assignment_id)[1] == deadline
    with pytest.raises(PermissionError, match="deadline differs"):
        service.finish(assignment_id, state="completed", outcome="x", adopted_after_budget_expiry=deadline - 1)
    with pytest.raises(TimeoutError):
        service.finish(assignment_id, state="completed", outcome="x")
    assert service.finish(assignment_id, state="completed", outcome="x",
                          adopted_after_budget_expiry=deadline).state == "completed"


@pytest.mark.parametrize("case", ["adopted", "none", "author", "body", "draft", "two_open", "closed_only"])
def test_ghcli_adoptable_pr_lookup_is_read_only(monkeypatch, case) -> None:
    from aitobuild.tools.github import GhCliGitHubAdapter
    body = "Automated draft\n\n<!-- aitobuild-delivery:" + "a" * 64 + " -->"
    adapter = GhCliGitHubAdapter(allowed_repositories=("fixture/widgets",))
    raw = {"number": 8, "title": "Trial", "body": body, "state": "open", "draft": True, "user": {"login": "aitobuild-bot"},
           "head": {"ref": "aitobuild/issue-7-task", "sha": "b" * 40, "repo": {"full_name": "fixture/widgets"}},
           "base": {"ref": "main"}, "html_url": "https://github.com/fixture/widgets/pull/8"}
    listing = {"adopted": [raw], "none": [], "author": [raw | {"user": {"login": "intruder"}}],
               "body": [raw | {"body": body + " edited"}], "draft": [raw | {"draft": False}],
               "two_open": [raw, raw | {"number": 9}], "closed_only": [raw | {"state": "closed"}]}[case]
    calls = []

    def api(endpoint, *, method="GET", payload=None):
        calls.append(method)
        if endpoint == "user":
            return {"login": "AItobuild-Bot"}
        assert "/pulls?" in endpoint and "state=all" in endpoint
        return listing

    monkeypatch.setattr(adapter, "_api", api)

    def find():
        return adapter.find_adoptable_draft_pull_request(repository="fixture/widgets", title="Trial", body=body,
                                                         head_branch="aitobuild/issue-7-task", base_ref="main")
    if case == "adopted":
        pull = find()
        assert (pull.number, pull.head_sha, pull.draft, pull.author) == (8, "b" * 40, True, "aitobuild-bot")
    elif case == "none":
        assert find() is None
    else:
        with pytest.raises((PermissionError, ValueError), match="not adopted" if case in {
                "author", "body", "draft"} else "must be one open draft"):
            find()
    assert set(calls) <= {"GET"}


def _snapshot(record, preview_id):
    return {key: record.publication[key] for key in SNAPSHOT_KEYS} | {
        "preview_id": preview_id, "approved_at": record.approved_at, "repository_id": 101}


@pytest.mark.parametrize("tamper", ["other_task_marker", "approved_at", "preview_id", "repository_id",
                                    "found_not_draft", "found_closed", "found_other_number", "correction_record"])
def test_worker_adoption_rechecks_every_guard_itself(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, tamper,
):
    """The worker does not rely on the runner/binding checks or the adapter: each guard refuses on its own."""
    github, worker, preview_id, _, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    expire(tmp_path)
    snapshot = _snapshot(record, preview_id)
    (number,) = github.pull_requests[REPO]
    pull = github.pull_requests[REPO][number]
    if tamper == "other_task_marker":
        # A well-formed marker of another task everywhere (saved body, approval, PR, recomputed metadata): exact
        # receipt and #51 regex both pass, so only the worker's own marker check refuses.
        ours = delivery_marker(preview_id, record.bundle_payload)
        theirs = delivery_marker("dp-other", record.bundle_payload)
        path = next((tmp_path / "state" / "deliveries").glob("*/state.json"))
        raw = json.loads(path.read_text())
        raw["record"]["publication"]["body"] = raw["record"]["publication"]["body"].replace(ours, theirs)
        path.write_text(json.dumps(raw))
        snapshot["body"] = snapshot["body"].replace(ours, theirs)
        github.pull_requests[REPO][number] = dc_replace(pull, body=pull.body.replace(ours, theirs))
        metadata = DeveloperDeliveryWorker._publication_metadata
        monkeypatch.setattr(DeveloperDeliveryWorker, "_publication_metadata", staticmethod(
            lambda bundle, rec: tuple(item.replace(ours, theirs) for item in metadata(bundle, rec))))
        match = "delivery marker"
    elif tamper in {"approved_at", "preview_id", "repository_id"}:
        snapshot[tamper] = {"approved_at": "2020-01-01T00:00:00+00:00", "preview_id": "dp-other",
                            "repository_id": 999}[tamper]
        match = "exact saved approval"
    elif tamper == "correction_record":
        # The preview passes the full rule as a non-correction; the record-side full rule alone refuses.
        import inspect
        real = DeveloperDeliveryWorker.is_correction_record
        monkeypatch.setattr(DeveloperDeliveryWorker, "is_correction_record", lambda self, rec: (
            inspect.stack()[1].function == "_refuse_correction_record" or real(self, rec)))
        match = "Scoped correction tasks cannot publish"
    else:
        found = {"found_not_draft": dc_replace(pull, draft=False), "found_closed": dc_replace(pull, state="closed"),
                 "found_other_number": dc_replace(pull, number=99)}[tamper]
        if tamper == "found_other_number":
            path = next((tmp_path / "state" / "deliveries").glob("*/state.json"))
            raw = json.loads(path.read_text())
            raw["record"]["publication"]["pull_number"] = number
            path.write_text(json.dumps(raw))
        monkeypatch.setattr(github, "find_adoptable_draft_pull_request", lambda **kwargs: found)
        match = "open draft PR at the verified head"
    before = worker.get(preview_id)
    with pytest.raises(PermissionError, match=match):
        worker.adopt_after_budget_expiry(preview_id, github=github, expected_publication=snapshot,
                                         allow_mock_publication=True)
    assert worker.get(preview_id) == before


def test_binding_hook_rechecks_the_interrupted_publication_itself(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, _, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    deadline = expire(tmp_path)
    runner, _, assignment_id, _ = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    run = FileRunStore(tmp_path / "runs").get(assignment_id)
    context = runner._context(run, runner._assignments.get(assignment_id), expired_deadline=deadline)
    hook = runner._operations["delivery_publish"].adopt_after_budget_expiry
    original = _snapshot(record, preview_id)
    with pytest.raises(PermissionError, match="own interrupted publication"):
        asyncio.run(hook(context, original | {"body": original["body"] + " changed"}))
    assert worker.get(preview_id).state == "publishing"
    # The expired-mode context is pinned to that exact deadline and still revalidates everything else.
    stale = runner._context(run, runner._assignments.get(assignment_id), expired_deadline=deadline - 1)
    with pytest.raises(PermissionError, match="deadline changed"):
        stale.revalidate()
    assert asyncio.run(hook(context, original)) == {"preview_id": preview_id, "state": "published"}


def test_other_resume_failures_never_enter_adoption(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    """Only an actual deadline expiry on a run killed mid-continuation is routed to the adopt-only hook."""
    github, worker, preview_id, _, _ = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    expire(tmp_path)
    entered: list[str] = []
    monkeypatch.setattr(ManagedWorkflowRunner, "_adopt_after_budget_expiry",
                        lambda *args, **kwargs: entered.append("adopt"))
    runner, _, assignment_id, _ = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    runner._binding_revision = "delivery-test-v2"
    failed = asyncio.run(runner.start(assignment_id))
    assert failed.state == "failed" and "binding revision" in failed.error and entered == []
    assert worker.get(preview_id).state == "publishing"


def test_waiting_runs_never_enter_adoption(tmp_path, approved_delivery, verification_adapter, monkeypatch):
    github = MockGitHubAdapter(allowed_repositories=frozenset({REPO}), enforce_allowlist=True)
    runner, worker, assignment_id, preview_id = build(tmp_path, approved_delivery, verification_adapter,
                                                      monkeypatch, github)
    asyncio.run(runner.start(assignment_id))
    expire(tmp_path)
    entered: list[str] = []
    monkeypatch.setattr(ManagedWorkflowRunner, "_adopt_after_budget_expiry",
                        lambda *args, **kwargs: entered.append("adopt"))
    failed = asyncio.run(runner.start(assignment_id))
    assert failed.state == "failed" and failed.error == "TimeoutError" and entered == []


def test_running_adoption_receipt_is_immutable(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, *_ = interrupted(tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    expire(tmp_path)
    run, *_ = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    running = ManagedRun.model_validate(run.model_dump() | {
        "state": "running", "error": None, "cleanup_succeeded": None, "terminal_state": None})
    store = FileRunStore(tmp_path / "other-runs")
    store.save(running)
    receipt = running.adopted_after_budget_expiry
    with pytest.raises(ValueError, match="immutable"):
        store.save(ManagedRun.model_validate(running.model_dump() | {
            "adopted_after_budget_expiry": receipt.model_copy(update={"deadline": receipt.deadline - 5})}))


@pytest.mark.parametrize("field", ["preview_id", "approved_at", "base_sha", "publication.base_sha", "missing_field",
                                   *__import__("aitobuild.developer_delivery", fromlist=["x"]).PUBLICATION_BINDING_FIELDS])
def test_shared_publication_binding_check_names_each_field(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, field,
):
    """#56/#66 share one field-by-field binding check; each field on its own makes it refuse."""
    from aitobuild.developer_delivery import publication_binding_mismatch
    _, worker, preview_id, _, record = interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    approved = _snapshot(record, preview_id)
    assert publication_binding_mismatch(record, approved) is None
    assert publication_binding_mismatch(record, None) == "publication"
    assert publication_binding_mismatch(dc_replace(record, publication=None), approved) == "publication"
    if field == "publication.base_sha":
        changed, expected = dc_replace(record, publication={**record.publication, "base_sha": "0" * 40}), field
        assert publication_binding_mismatch(changed, approved) == expected
        return
    if field == "missing_field":
        # Absent on both sides is still a mismatch: the saved publication must carry every approved field.
        publication = dict(record.publication)
        del publication["file_modes"]
        approved.pop("file_modes")
        assert publication_binding_mismatch(dc_replace(record, publication=publication), approved) == "file_modes"
        return
    value = approved[field]
    approved[field] = (value + "-x") if isinstance(value, str) else ([*value, "x"] if isinstance(value, list) else (
        {**value, "x": None} if isinstance(value, dict) else value + 1))
    assert publication_binding_mismatch(record, approved) == field
