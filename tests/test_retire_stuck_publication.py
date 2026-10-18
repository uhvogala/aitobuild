"""#61: operator retire of a non-correction delivery record stuck at `publishing`.

The record is produced by the real kill fixture of the interrupted-publication tests (a SIGKILL stand-in after the
branch push and before the PR), so every case starts from exactly what a killed service leaves behind.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aitobuild.organization_runner import FileRunStore
from aitobuild.organization_service import ManagedOrganizationService
from aitobuild.tools.github import GhCliGitHubAdapter, GitHubPullRequest, GitHubTransientError
from test_dispatcher import approved_delivery as approved_delivery, verification_adapter as verification_adapter
from test_interrupted_publication import REPO, build, kill_during_publication, restart

REASON = "Pilot PR closed and branch deleted; superseded by a fresh run (#61)"
WRITES = ("upsert_branch_commit", "create_or_update_draft_pull_request", "advance_draft_pull_request_head",
          "reconcile_advanced_head", "submit_pr_review", "update_issue", "create_issue", "create_pull_request",
          "comment_on_issue", "add_labels")


def _no_github_writes(github, monkeypatch) -> list[str]:
    calls: list[str] = []
    for name in WRITES:
        if hasattr(github, name):
            def refuse(*args, _name=name, **kwargs):
                calls.append(_name)
                raise AssertionError(f"unexpected GitHub write {_name}")
            monkeypatch.setattr(github, name, refuse)
    return calls


def _stuck(tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
           point="after_push_before_pr", branch="gone"):
    github, worker, preview_id, waiting = kill_during_publication(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, point)
    _, worker, _, _ = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    record = worker.get(preview_id)
    assert record.state == "publishing" and record.publication.get("pull_number") is None
    heads = github._branch_heads.setdefault(REPO, {})
    heads[record.publication["base_ref"]] = record.base_revision  # the repository is readable
    if branch == "gone":
        heads.pop(record.branch, None)
    elif branch == "moved":
        heads[record.branch] = "f" * 40
    return github, worker, preview_id, record


def _budget_and_approvals(tmp_path: Path) -> dict[str, bytes]:
    root = tmp_path / "state"
    files = [*root.glob("budgets/*"), *root.rglob("publish-approvals/*")]
    return {str(path): path.read_bytes() for path in sorted(files) if path.is_file()}


@pytest.mark.parametrize("branch", ["gone", "moved"])
def test_retire_allowed_when_the_live_branch_is_gone_or_moved(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, branch,
):
    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, branch=branch)
    heads_before = dict(github._branch_heads[REPO])
    pulls_before = dict(github.pull_requests.get(REPO, {}))
    budgets_before = _budget_and_approvals(tmp_path)
    writes = _no_github_writes(github, monkeypatch)
    locks = []
    implementation_lock = worker.implementation_lock
    monkeypatch.setattr(worker, "implementation_lock", lambda pid: locks.append(implementation_lock(pid)) or locks[-1])
    retired = worker.retire_delivery(preview_id, actor_id=" operator ", github=github, reason=f"  {REASON} ",
                                     allow_mock_publication=True)
    assert len(locks) == 1 and not locks[0].is_locked  # held during the retire, released after it
    assert retired.state == "retired" and retired.error == "Retired by operator"
    assert set(retired.retirement) == {"actor_id", "retired_at", "previous_state", "live_head_sha", "reason",
                                       "live_branch"}
    assert retired.retirement["actor_id"] == "operator" and retired.retirement["reason"] == REASON
    assert retired.retirement["previous_state"] == "publishing"
    assert retired.retirement["live_branch"] == branch
    assert retired.retirement["live_head_sha"] == (None if branch == "gone" else "f" * 40)
    # Saved head, snapshot and evidence are untouched.
    assert retired.publication == before.publication and retired.verification == before.verification
    assert retired.head_revision == before.head_revision and retired.bundle_payload == before.bundle_payload
    assert retired.session_id == before.session_id and retired.branch == before.branch
    # Nothing written to GitHub; budget and approvals untouched.
    assert writes == []
    assert github._branch_heads[REPO] == heads_before and github.pull_requests.get(REPO, {}) == pulls_before
    assert _budget_and_approvals(tmp_path) == budgets_before
    # Durable and idempotent; a fresh worker loads (and validates) the same retired record.
    _, fresh, _, _ = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert fresh.get(preview_id) == retired
    assert fresh.retire_delivery(preview_id, actor_id="other", github=github, reason="again",
                                 allow_mock_publication=True) == retired


def test_retire_allowed_with_no_saved_head_only_when_the_branch_is_gone(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
        point="after_push_head_unrecorded", branch="kept")
    assert before.publication.get("head_sha") is None and github._branch_heads[REPO].get(before.branch)
    with pytest.raises(PermissionError, match="unrecorded push"):
        worker.retire_delivery(preview_id, actor_id="operator", github=github, reason=REASON,
                               allow_mock_publication=True)
    assert worker.get(preview_id) == before
    del github._branch_heads[REPO][before.branch]
    retired = worker.retire_delivery(preview_id, actor_id="operator", github=github, reason=REASON,
                                     allow_mock_publication=True)
    assert retired.state == "retired" and retired.retirement["live_branch"] == "gone"
    assert retired.publication == before.publication


def _as_prefix_correction(worker, preview_id, record, monkeypatch):
    """Present a consistent correction-prefixed record and preview (same bundle, correction task ID)."""
    task_id = "published-correction-x"
    bundle = {**record.bundle_payload, "task_id": task_id}
    preview = replace(worker.preview(preview_id), bundle_payload=bundle)
    get = worker._previews.get
    monkeypatch.setattr(worker._previews, "get", lambda pid: preview if pid == preview_id else get(pid))
    monkeypatch.setattr(worker, "_load_preview_record",
                        lambda pid: replace(record, task_id=task_id, bundle_payload=bundle))


def _tamper_record(tmp_path: Path, change) -> None:
    path = next((tmp_path / "state" / "deliveries").glob("*/state.json"))
    raw = json.loads(path.read_text())
    change(raw["record"])
    path.write_text(json.dumps(raw))


@pytest.mark.parametrize("case", ["pr_recorded", "pr_url_recorded", "at_saved_head", "correction_source",
                                  "correction_task_with_reason", "verified", "failed", "implementation_locked",
                                  "missing_record", "unknown_preview", "base_unreadable", "mock_without_opt_in",
                                  "no_actor", "no_reason", "foreign_preview_scope", "malformed_live_head",
                                  "malformed_saved_head", "foreign_repository", "oversized_reason",
                                  "no_publication", "missing_preview", "classification_mismatch"])
def test_retire_refusals_leave_the_record_and_github_untouched(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, case,
):
    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
        branch="kept" if case == "at_saved_head" else "gone")
    kwargs = {"actor_id": "operator", "github": github, "reason": REASON, "allow_mock_publication": True}
    target = preview_id
    error: type[Exception] = PermissionError
    match = None
    if case == "pr_recorded":
        _tamper_record(tmp_path, lambda record: record["publication"].update(pull_number=74))
        match = "pull request is recorded"
    elif case == "pr_url_recorded":
        _tamper_record(tmp_path, lambda record: record["publication"].update(html_url="https://x/pull/74"))
        match = "pull request is recorded"
    elif case == "at_saved_head":
        assert github._branch_heads[REPO][before.branch] == before.publication["head_sha"]
        match = "still at the saved head"
    elif case == "correction_source":
        preview = worker.preview(preview_id)
        fake = replace(preview, source_payload={**preview.source_payload, "correction_from_review": "dp-review"})
        get = worker._previews.get
        monkeypatch.setattr(worker._previews, "get", lambda pid: fake if pid == preview_id else get(pid))
        _tamper_record(tmp_path, lambda raw: raw.update(correction=True))  # as if created from that preview
        kwargs["reason"] = None  # corrections take no reason
        # Classified as a correction at load by the full rule, its non-correction publication fails the correction
        # validation, so retire is refused before either path runs.
        match = "failed validation"
    elif case == "classification_mismatch":  # the preview says correction, the stored verdict does not
        preview = worker.preview(preview_id)
        fake = replace(preview, source_payload={**preview.source_payload, "correction_from_review": "dp-review"})
        get = worker._previews.get
        monkeypatch.setattr(worker._previews, "get", lambda pid: fake if pid == preview_id else get(pid))
        match = "failed validation"
    elif case == "missing_preview":
        get = worker._previews.get
        monkeypatch.setattr(worker._previews, "get", lambda pid: None if pid == preview_id else get(pid))
        match = "own approved preview"
    elif case == "correction_task_with_reason":
        _as_prefix_correction(worker, preview_id, before, monkeypatch)
        monkeypatch.setattr(worker, "retire_correction", lambda *args, **kwargs: pytest.fail("must not route"))
        match = "records no reason"
    elif case in {"verified", "failed"}:
        _tamper_record(tmp_path, lambda record: record.update(state="verified", publication=None)
                       if case == "verified" else record.update(state="failed", error="boom"))
        match = f"A {case} delivery cannot be retired"
    elif case == "implementation_locked":
        held = worker.implementation_lock(preview_id)
        held.acquire()
        match = "in flight"
    elif case == "missing_record":
        next((tmp_path / "state" / "deliveries").glob("*/state.json")).unlink()
        match = "saved delivery record"
    elif case == "unknown_preview":
        target = "dp-00000000-0000-0000-0000-000000000000"
        match = "saved delivery record"
    elif case == "base_unreadable":
        del github._branch_heads[REPO][before.publication["base_ref"]]
        match = "Base branch is unreadable"
    elif case == "mock_without_opt_in":
        kwargs["allow_mock_publication"] = False
        error, match = ValueError, "mock reads are refused"
    elif case == "no_actor":
        kwargs["actor_id"] = "  "
        match = "operator identity"
    elif case == "no_reason":
        kwargs["reason"] = " "
        match = "operator reason"
    elif case == "foreign_preview_scope":
        preview = worker.preview(preview_id)
        fake = replace(preview, bundle_payload={**preview.bundle_payload, "objective": "edited"})
        get = worker._previews.get
        monkeypatch.setattr(worker._previews, "get", lambda pid: fake if pid == preview_id else get(pid))
        match = "failed validation"  # a present-but-differing preview is refused at load, never a fallback
    elif case == "malformed_live_head":
        github._branch_heads[REPO][before.branch] = "not-a-sha"
        match = "Live task branch head is malformed"
    elif case == "malformed_saved_head":
        monkeypatch.setattr(worker, "_load_preview_record", lambda pid: replace(
            before, publication={**before.publication, "head_sha": "E" * 40}))
        match = "Saved head SHA is malformed"
    elif case == "foreign_repository":
        _tamper_record(tmp_path, lambda record: record["publication"].update(repository="someone/else"))
        match = "Publication target differs"
    elif case == "no_publication":
        # Unreachable through a validated load (publishing requires a publication); the guard still fails closed.
        monkeypatch.setattr(worker, "_load_preview_record", lambda pid: replace(before, publication=None))
        match = "without a saved publication"
    elif case == "oversized_reason":
        kwargs["reason"] = "x" * 2001
        match = "operator reason"
    saved = {path: path.read_bytes() for path in (tmp_path / "state" / "deliveries").glob("*/state.json")}
    heads, pulls = dict(github._branch_heads[REPO]), dict(github.pull_requests.get(REPO, {}))
    writes = _no_github_writes(github, monkeypatch)
    if case == "correction_task_with_reason":
        with pytest.raises(error, match=match):
            worker.retire_delivery(target, **kwargs)
        return
    with pytest.raises(error, match=match):
        worker.retire_delivery(target, **kwargs)
    if case == "implementation_locked":
        held.release()
    if case == "unknown_preview":  # refusing an unknown preview leaves no lock or state files behind
        assert not worker._task_dir(target).exists() and not Path(str(worker._task_dir(target)) + ".lock").exists()
    assert {path: path.read_bytes() for path in saved} == saved
    assert writes == [] and github._branch_heads[REPO] == heads and github.pull_requests.get(REPO, {}) == pulls


def test_correction_records_still_route_to_the_unchanged_correction_retire(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, _ = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    seen: list[dict] = []
    _as_prefix_correction(worker, preview_id, worker.get(preview_id), monkeypatch)
    monkeypatch.setattr(worker, "retire_correction", lambda pid, **kwargs: seen.append({"pid": pid, **kwargs}) or "c")
    assert worker.retire_delivery(preview_id, actor_id="operator", github=github, allow_mock_publication=True) == "c"
    assert seen == [{"pid": preview_id, "actor_id": "operator", "github": github, "allow_mock_publication": True}]


def test_after_retire_resume_adoption_publish_and_prepare_all_refuse(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, branch="moved")
    retired = worker.retire_delivery(preview_id, actor_id="operator", github=github, reason=REASON,
                                     allow_mock_publication=True)
    # An adoptable PR (our marker, exact receipt, open draft, our author) on the task branch, at the saved head.
    publication = before.publication
    github._branch_heads[REPO][before.branch] = publication["head_sha"]
    github.pull_requests.setdefault(REPO, {})[74] = GitHubPullRequest(
        number=74, title=publication["title"], body=publication["body"].strip(), state="open",
        head_ref=publication["branch"], base_ref=publication["base_ref"], draft=True, repository=REPO,
        head_sha=publication["head_sha"], author=github.login,
    )
    pulls = dict(github.pull_requests[REPO])
    writes = _no_github_writes(github, monkeypatch)
    # The runner's resume hook refuses a retired record.
    runner, worker, assignment_id, _ = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    run = FileRunStore(tmp_path / "runs").get(assignment_id)
    context = runner._context(run, runner._assignments.get(assignment_id))
    original = {key: publication[key] for key in publication if key != "head_sha"} | {
        "preview_id": preview_id, "approved_at": before.approved_at}
    assert runner._operations["delivery_publish"].resume_interrupted(context, original) is False
    # Restarting the killed run fails closed without adopting or writing anything.
    failed, worker, _ = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert failed.state == "failed"
    # Direct publish (the only adoption path) refuses before any GitHub call; no new admission under this record.
    with pytest.raises(ValueError, match="verified delivery"):
        worker.publish(preview_id, github=github, require_human_approval_for_repo_writes=True,
                       allow_mock_publication=True)
    with pytest.raises(PermissionError, match="independently verified"):
        worker.publication_snapshot(preview_id)
    assert worker.prepare(preview_id) == retired
    assert writes == [] and github.pull_requests[REPO] == pulls
    assert worker.get(preview_id) == retired


@pytest.mark.parametrize("tamper", ["no_reason", "gone_with_head", "moved_to_saved_head", "moved_without_head",
                                    "previous_state", "pull_number", "extra_key", "correction_shape",
                                    "bad_observation", "verification_evidence", "bad_timestamp", "empty_actor"])
def test_retired_record_load_and_replay_validation(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, tamper,
):
    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, branch="moved")
    retired = worker.retire_delivery(preview_id, actor_id="operator", github=github, reason=REASON,
                                     allow_mock_publication=True)
    assert worker.get(preview_id) == retired

    def change(record):
        receipt = record["retirement"]
        if tamper == "no_reason":
            receipt["reason"] = ""
        elif tamper == "gone_with_head":
            receipt["live_branch"] = "gone"
        elif tamper == "moved_to_saved_head":
            receipt["live_head_sha"] = record["publication"]["head_sha"]
        elif tamper == "moved_without_head":
            record["publication"].pop("head_sha")
            record["head_revision"] = record["base_revision"]
        elif tamper == "previous_state":
            receipt["previous_state"] = "verified"
        elif tamper == "pull_number":
            record["publication"]["pull_number"] = 74
        elif tamper == "extra_key":
            receipt["note"] = "x"
        elif tamper == "correction_shape":
            del receipt["reason"], receipt["live_branch"]
        elif tamper == "bad_observation":
            receipt["live_branch"] = "unknown"
        elif tamper == "verification_evidence":
            record["verification"]["cleanup_succeeded"] = False
        elif tamper == "bad_timestamp":
            receipt["retired_at"] = "yesterday"
        else:
            receipt["actor_id"] = " "

    _tamper_record(tmp_path, change)
    with pytest.raises(ValueError):
        worker.get(preview_id)
    # A replayed restart cannot get past a tampered retired record either.
    with pytest.raises(ValueError):
        restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)


def test_a_retirement_receipt_on_a_live_publishing_record_is_refused(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    _, worker, preview_id, _ = _stuck(tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    _tamper_record(tmp_path, lambda record: record.update(retirement={"actor_id": "operator"}))
    with pytest.raises(ValueError, match="Only retired deliveries"):
        worker.get(preview_id)


def _service(worker, previews, runs, assignments):
    return SimpleNamespace(_previews=previews, _worker=worker, _runs=runs, operator_id="operator",
                           recorded_assignment=lambda task_id: assignments.for_task(task_id),
                           retire_correction=None)


def test_service_retire_refuses_while_the_managed_run_is_executing(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, _ = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    runner, worker, assignment_id, _ = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    runs = FileRunStore(tmp_path / "runs")
    assert runs.get(assignment_id).state == "running"  # left by the kill; nobody holds its lock
    service = _service(worker, worker._previews, runs, runner._assignments)
    executing = runs.invocation_lock(assignment_id)
    executing.acquire()
    try:
        with pytest.raises(PermissionError, match="managed run for this delivery is executing"):
            asyncio.run(ManagedOrganizationService.retire_delivery(
                service, preview_id, github=github, reason=REASON, allow_mock_publication=True))
        assert worker.get(preview_id).state == "publishing"
    finally:
        executing.release()
    seen = []

    class Runs:
        def invocation_lock(self, assignment):
            seen.append(runs.invocation_lock(assignment))
            return seen[-1]

    service._runs = Runs()
    retired = asyncio.run(ManagedOrganizationService.retire_delivery(
        service, preview_id, github=github, reason=REASON, allow_mock_publication=True))
    assert retired.state == "retired" and retired.retirement["actor_id"] == "operator"
    # The run lock was taken for this assignment and is released again afterwards.
    assert len(seen) == 1 and not seen[0].is_locked
    with runs.invocation_lock(assignment_id):
        pass


def test_service_retire_routes_corrections_to_the_correction_rule(tmp_path):
    calls: list[tuple] = []

    async def retire_correction(preview_id, *, github, allow_mock_publication=False):
        calls.append((preview_id, allow_mock_publication))
        return "correction"

    previews = SimpleNamespace(get=lambda pid: SimpleNamespace(
        bundle_payload={"task_id": "published-correction-ISSUE-7"}, source_payload={}))
    service = SimpleNamespace(_previews=previews, retire_correction=retire_correction)
    assert asyncio.run(ManagedOrganizationService.retire_delivery(service, "dp-c", github=None)) == "correction"
    with pytest.raises(PermissionError, match="records no reason"):
        asyncio.run(ManagedOrganizationService.retire_delivery(service, "dp-c", github=None, reason="why"))
    assert calls == [("dp-c", False)]
    missing = SimpleNamespace(_previews=SimpleNamespace(get=lambda pid: None))
    with pytest.raises(PermissionError, match="approved preview"):
        asyncio.run(ManagedOrganizationService.retire_delivery(missing, "dp-x", github=None, reason="why"))


def test_live_branch_head_is_none_only_on_a_404_status(monkeypatch):
    """The 404 decision reads the structured `GitHubAPIError.status`, never the error wording."""
    from subprocess import CalledProcessError
    from aitobuild.tools.github import GitHubAPIError
    adapter = GhCliGitHubAdapter(allowed_repositories=("fixture/widgets",))
    answers: list = []

    def api(endpoint, *, method="GET", payload=None):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(adapter, "_api", api)
    read = lambda: adapter.branch_head(repository="fixture/widgets", branch="aitobuild/x")  # noqa: E731
    answers.append({"object": {"sha": "c" * 40}})
    assert read() == "c" * 40
    for wording in ("gh: Not Found (HTTP 404)", "branch is gone", ""):
        answers.append(GitHubAPIError(wording, status=404))
        assert read() is None, wording
    for failure in (GitHubAPIError("gh: Not Found (HTTP 404)", status=403),  # wording says 404, status does not
                    GitHubAPIError("HTTP 404", status=None), RuntimeError("gh api failed: HTTP 404"),
                    GitHubTransientError("gh api failed: HTTP 502", status=502)):
        answers.append(failure)
        with pytest.raises(RuntimeError):
            read()
    answers.append({"object": {"sha": "nope"}})
    with pytest.raises(RuntimeError, match="Unexpected"):
        read()

    # The status is taken once, where gh reports it, and carried on the error.
    def gh_fails(stderr):
        def run(*args, **kwargs):
            raise CalledProcessError(1, ["gh"], stderr=stderr)
        return run

    for stderr, status, kind in (("gh: Not Found (HTTP 404)", 404, GitHubAPIError),
                                 ("gh: Bad Gateway (HTTP 502)", 502, GitHubTransientError),
                                 ("gh: could not connect", None, GitHubAPIError)):
        monkeypatch.setattr("aitobuild.tools.github.run", gh_fails(stderr))
        with pytest.raises(kind) as raised:
            GhCliGitHubAdapter(allowed_repositories=("fixture/widgets",))._api_once("x", method="GET", payload=None)
        assert raised.value.status == status


def _issue_correction_preview(worker, preview_id):
    """An ISSUE-* task whose approved source is a correction_from_review: a correction by the full rule only."""
    preview = worker.preview(preview_id)
    assert str(preview.bundle_payload["task_id"]).startswith("ISSUE-")
    return replace(preview, source_payload={**preview.source_payload, "correction_from_review": "dp-review"})


def test_worker_routes_a_source_only_correction_to_retire_correction(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    fake = _issue_correction_preview(worker, preview_id)
    get = worker._previews.get
    monkeypatch.setattr(worker._previews, "get", lambda pid: fake if pid == preview_id else get(pid))
    # Routing only: a real source-only correction would carry correction publication evidence, which this stuck
    # non-correction fixture lacks (the load-level refusal is the `correction_source` refusal case).
    monkeypatch.setattr(worker, "_load_preview_record", lambda pid: replace(before, correction=True))
    state = next((tmp_path / "state" / "deliveries").glob("*/state.json"))
    saved = state.read_bytes()
    routed: list[tuple] = []
    monkeypatch.setattr(worker, "retire_correction", lambda pid, **kwargs: routed.append((pid, kwargs)) or "correction")
    monkeypatch.setattr(worker, "_retire_stuck_publication",
                        lambda *args, **kwargs: pytest.fail("a correction must not take the non-correction path"))
    monkeypatch.setattr(worker, "implementation_lock",
                        lambda pid: pytest.fail("routing must not take the non-correction lock"))
    assert worker.retire_delivery(preview_id, actor_id="operator", github=github,
                                  allow_mock_publication=True) == "correction"
    assert routed == [(preview_id, {"actor_id": "operator", "github": github, "allow_mock_publication": True})]
    with pytest.raises(PermissionError, match="records no reason"):
        worker.retire_delivery(preview_id, actor_id="operator", github=github, reason=REASON,
                               allow_mock_publication=True)
    assert len(routed) == 1 and state.read_bytes() == saved


def test_service_routes_a_source_only_correction_to_retire_correction(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    fake = _issue_correction_preview(worker, preview_id)
    routed: list[tuple] = []

    async def retire_correction(pid, *, github, allow_mock_publication=False):
        routed.append((pid, allow_mock_publication))
        return "correction"

    def never(*args, **kwargs):
        pytest.fail("a correction must not take the non-correction run-lock path")

    service = SimpleNamespace(
        _previews=SimpleNamespace(get=lambda pid: fake), retire_correction=retire_correction,
        recorded_assignment=never, _runs=SimpleNamespace(invocation_lock=never),
        _worker=SimpleNamespace(retire_delivery=never), operator_id="operator")
    assert asyncio.run(ManagedOrganizationService.retire_delivery(
        service, preview_id, github=github, allow_mock_publication=True)) == "correction"
    assert routed == [(preview_id, True)]
    with pytest.raises(PermissionError, match="records no reason"):
        asyncio.run(ManagedOrganizationService.retire_delivery(service, preview_id, github=github, reason=REASON))
    assert routed == [(preview_id, True)] and worker.get(preview_id) == before


def test_retired_stuck_publication_reloads_as_a_non_correction_after_restart(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    from aitobuild.developer_delivery import DeveloperDeliveryWorker
    from aitobuild.developer_preview import DeveloperPreviewRegistry

    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    assert before.correction is False and not worker.is_correction_record(before)
    retired = worker.retire_delivery(preview_id, actor_id="operator", github=github, reason=REASON,
                                     allow_mock_publication=True)
    for registry in (DeveloperPreviewRegistry(worker._previews._path), DeveloperPreviewRegistry()):
        fresh = DeveloperDeliveryWorker(preview_registry=registry, state_dir=worker._state_dir,
                                        service_root=worker._service_root, repository_sources=worker._sources)
        loaded = fresh.get(preview_id)
        assert loaded == retired and not fresh.is_correction_record(loaded)
        assert loaded.retirement["previous_state"] == "publishing" and loaded.retirement["live_branch"] == "gone"
        assert loaded.retirement["reason"] == REASON
        with pytest.raises(PermissionError, match="saved correction"):
            fresh.retire_correction(preview_id, actor_id="operator", github=github, allow_mock_publication=True)
    # A stored verdict flipped to correction disagrees with the approved preview and fails closed.
    _tamper_record(tmp_path, lambda raw: raw.update(correction=True))
    with pytest.raises(ValueError, match="differs from its approved preview"):
        worker.get(preview_id)


def test_record_classification_uses_the_preview_then_the_stored_verdict_and_fails_closed(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    from aitobuild.developer_delivery import DeveloperDeliveryWorker
    from aitobuild.developer_preview import DeveloperPreviewRegistry

    _, worker, _, before = _stuck(tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    blind = DeveloperDeliveryWorker(preview_registry=DeveloperPreviewRegistry(), state_dir=worker._state_dir,
                                    service_root=worker._service_root, repository_sources=worker._sources)
    legacy = replace(before, correction=None)
    assert worker.is_correction_record(legacy) is False  # pre-#61 record: classified by its approved preview
    with pytest.raises(ValueError, match="no stored correction classification"):
        blind.is_correction_record(legacy)
    # A present preview whose bundle differs is refused for every stored verdict; it never falls back.
    for verdict in (None, False, True):
        with pytest.raises(ValueError, match="approved preview differs from the record"):
            worker.is_correction_record(replace(before, correction=verdict,
                                                bundle_payload={**before.bundle_payload, "objective": "x"}))
    assert blind.is_correction_record(before) is False
    assert blind.is_correction_record(replace(before, correction=True)) is True
    with pytest.raises(ValueError, match="contradicts its correction task ID"):
        blind.is_correction_record(replace(before, task_id="published-correction-x"))
    with pytest.raises(ValueError, match="differs from its approved preview"):
        worker.is_correction_record(replace(before, correction=True))


@pytest.mark.parametrize("case", ["moved_marker_pr", "moved_foreign_pr", "gone_marker_pr", "other_base_pr",
                                  "lookup_error", "lookup_transient", "lookup_malformed"])
def test_retire_refuses_while_any_open_pr_is_on_the_task_branch(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, case,
):
    """ReviewBot's #62 condition: any open PR on the task branch (ours with the delivery marker, or anyone's
    without it, into any base) refuses the retire whatever the branch state; an unreadable lookup fails closed."""
    from aitobuild.developer_delivery import delivery_marker

    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
        branch="gone" if case == "gone_marker_pr" else "moved")
    publication = before.publication
    marker = delivery_marker(preview_id, before.bundle_payload)
    pulls = github.pull_requests.setdefault(REPO, {})
    ours = GitHubPullRequest(number=74, title=publication["title"], body=f"{publication['body'].strip()}\n{marker}",
                             state="open", head_ref=before.branch, base_ref=publication["base_ref"], draft=True,
                             repository=REPO, head_sha=publication["head_sha"], author=github.login)
    match = "has open pull request"
    if case in {"moved_marker_pr", "gone_marker_pr"}:
        pulls[74] = ours
        match = "has open pull request\\(s\\) #74;"
    elif case == "moved_foreign_pr":
        pulls[75] = replace(ours, number=75, title="Someone else's", body="no marker here", draft=False,
                            author="intruder", head_sha="f" * 40)
        pulls[76] = replace(ours, number=76, state="closed")  # closed PRs alone never block; open ones are listed
        match = "has open pull request\\(s\\) #75;"
    elif case == "other_base_pr":
        pulls[77] = replace(ours, number=77, base_ref="release", body="no marker")
        match = "#77;"
    else:
        failure = {"lookup_error": RuntimeError("gh: boom"), "lookup_transient": GitHubTransientError("gh: 502"),
                   "lookup_malformed": KeyError("number")}[case]
        monkeypatch.setattr(github, "task_pull_requests", lambda **kwargs: (_ for _ in ()).throw(failure))
        match = "unreadable; retire is refused"
    state = next((tmp_path / "state" / "deliveries").glob("*/state.json"))
    saved, budgets = state.read_bytes(), _budget_and_approvals(tmp_path)
    pulls_before = dict(pulls)
    writes = _no_github_writes(github, monkeypatch)
    with pytest.raises(PermissionError, match=match):
        worker.retire_delivery(preview_id, actor_id="operator", github=github, reason=REASON,
                               allow_mock_publication=True)
    assert state.read_bytes() == saved and _budget_and_approvals(tmp_path) == budgets
    assert writes == [] and github.pull_requests.get(REPO, {}) == pulls_before
    assert worker.get(preview_id) == before


def test_retire_succeeds_when_the_branch_is_gone_and_no_open_pr_exists(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, before = _stuck(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    lookups = []
    lookup = github.task_pull_requests
    monkeypatch.setattr(github, "task_pull_requests", lambda **kwargs: lookups.append(kwargs) or lookup(**kwargs))
    github.pull_requests.setdefault(REPO, {})[74] = GitHubPullRequest(  # the pilot's PR #74: closed, not open
        number=74, title="t", body="b", state="closed", head_ref=before.branch,
        base_ref=before.publication["base_ref"], repository=REPO)
    retired = worker.retire_delivery(preview_id, actor_id="operator", github=github, reason=REASON,
                                     allow_mock_publication=True)
    assert retired.state == "retired" and retired.retirement["live_branch"] == "gone"
    assert lookups == [{"repository": before.publication["repository"], "head_branch": before.branch}]  # any base


def test_gh_task_pull_requests_is_one_read_that_raises_on_failure(monkeypatch):
    """The shared lookup: one GET on the task branch head (any state; any base unless one is given), filtered to
    this repository's branch, so a fork's same-named branch is ignored; a failure or odd answer raises."""
    adapter = GhCliGitHubAdapter(allowed_repositories=("fixture/widgets",))
    calls, answers = [], []

    def api(endpoint, *, method="GET", payload=None):
        calls.append((endpoint, method))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(adapter, "_api", api)

    def raw(number, *, ref="aitobuild/x", repo="fixture/widgets", base="main", state="open", draft=True):
        return {"number": number, "title": "t", "body": "b", "state": state, "draft": draft, "user": {"login": "w"},
                "head": {"ref": ref, "repo": {"full_name": repo}, "sha": "c" * 40}, "base": {"ref": base}}

    answers.append([raw(1), raw(2, repo="fork/widgets"), raw(3, ref="aitobuild/y"), raw(4, base="release", state="closed"),
                    raw(5, draft="yes")])
    found = adapter.task_pull_requests(repository="fixture/widgets", head_branch="aitobuild/x")
    assert [pull.number for pull in found] == [1, 4, 5] and [pull.draft for pull in found] == [True, True, False]
    assert calls[-1] == ("repos/fixture/widgets/pulls?state=all&head=fixture%3Aaitobuild%2Fx&per_page=100", "GET")
    answers.append([raw(1), raw(4, base="release")])
    assert [p.number for p in adapter.task_pull_requests(repository="fixture/widgets", head_branch="aitobuild/x",
                                                         base_ref="main")] == [1]
    assert "base=main" in calls[-1][0]
    for failure in (RuntimeError("gh: boom"), GitHubTransientError("gh: 502"), {"not": "a list"}):
        answers.append(failure)
        with pytest.raises(RuntimeError):
            adapter.task_pull_requests(repository="fixture/widgets", head_branch="aitobuild/x")
