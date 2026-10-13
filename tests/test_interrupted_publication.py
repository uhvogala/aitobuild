"""An interrupted run resumes only the same admitted task's own in-flight draft publication (pilot trial 4).

A SIGKILL is simulated by snapshotting every durable file at the kill point, letting the process "die", and
restoring that snapshot: in-memory GitHub state survives as the remote, nothing written after the kill does.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil

import pytest

from aitobuild.approval_policy import ApprovalPolicy
from aitobuild.developer_delivery import DeveloperDeliveryWorker, LocalRepositorySource
from aitobuild.organization import FileDefinitionStore, parse_organization_definition
from aitobuild.organization_assignments import AssignmentService, FileAssignmentStore
from aitobuild.organization_delivery import DeliveryInvocation, ManagedDeliveryBindings
from aitobuild.organization_runner import FileRunStore, ManagedWorkflowRunner, RuntimeActor
from aitobuild.developer_isolation import DeveloperTaskBudget
from aitobuild.tools.github import GitHubPullRequest, MockGitHubAdapter
from test_dispatcher import approved_delivery as approved_delivery, verification_adapter as verification_adapter
from test_organization_assignments import assign, coordinator_proposal

REPO = "fixture/widgets"


class Killed(BaseException):
    """Stands in for SIGKILL: no handler may treat it as an ordinary failure."""


class Crash:
    def __init__(self, root: Path, snapshot: Path) -> None:
        self.root, self.snapshot = root, snapshot

    def kill(self) -> None:
        shutil.copytree(self.root, self.snapshot, symlinks=True)
        raise Killed()

    def restore(self) -> None:
        for child in self.root.iterdir():
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        shutil.copytree(self.snapshot, self.root, symlinks=True, dirs_exist_ok=True)


def build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github):
    previews, _, preview_id, source, _ = approved_delivery
    sources = (LocalRepositorySource(REPO, 101, source, ("python -m pytest -q",)),)
    worker = DeveloperDeliveryWorker(preview_registry=previews, state_dir=tmp_path / "state",
                                     service_root=Path.cwd(), repository_sources=sources)
    if worker.get(preview_id) is None:
        worker.prepare(preview_id)
    document = json.loads((Path(__file__).resolve().parents[1] / "config/organization.example.json").read_text())
    stages = ["prepare", "implement", "verify", "publish"]
    document["workflows"][0]["document"] = {
        "format": "python_graph", "start": "prepare",
        "nodes": [{"id": stage, "kind": "operation", "operation": "delivery_" + stage} for stage in stages],
        "edges": [{"source": first, "target": second} for first, second in zip(stages, stages[1:])],
        "outputs": ["publish"],
    }
    definitions = FileDefinitionStore(tmp_path / "definitions")
    snapshot = definitions.save(parse_organization_definition(json.dumps(document)))
    assignments = FileAssignmentStore(tmp_path / "assignments.json")
    service = AssignmentService(definitions=definitions, previews=previews, assignments=assignments,
                                budget_path_for=worker.budget_path)
    existing = assignments.for_task(previews.get(preview_id).bundle_payload["task_id"])
    assignment = existing or assign(service, snapshot, previews.get(preview_id), **coordinator_proposal())

    class Implementation:
        async def run(self, context):
            with worker.implementation_lock(preview_id):
                record = worker.begin_implementation(preview_id, bundle=context.assignment.bundle,
                                                     session_id=context.run.session_id, resume=False)
                budget = DeveloperTaskBudget(path=Path(context.assignment.budget_path),
                                             bundle=context.assignment.bundle, create=False)
                budget.reserve_paths(("src/probe.py",))
                target = Path(record.checkout_path) / "src/probe.py"
                target.parent.mkdir()
                target.write_text("def probe():\n    return True\n")
                worker.finish_implementation(preview_id, session_id=context.run.session_id)
            return DeliveryInvocation(context.run.session_id)

        async def resume(self, context, *, request_id, approved):
            raise AssertionError("No tool approval was requested")

        async def cleanup(self, context):
            return True

    bindings = ManagedDeliveryBindings(worker=worker, implementation=Implementation(),
                                       verification_adapter=verification_adapter, github=github,
                                       allow_mock_publication=True)
    monkeypatch.setattr("aitobuild.developer_delivery.shell_request", lambda *args, **kwargs: {
        "ok": True, "status": "exited", "exit_code": 0, "output": "passed", "next_cursor": 6,
    })
    runner = ManagedWorkflowRunner(
        definitions=definitions, assignments=assignments, assignment_service=service,
        runs=FileRunStore(tmp_path / "runs"), actor_provider=lambda: RuntimeActor("operator", "operator"),
        operations=bindings.operations, cleanup=bindings.cleanup, binding_revision="delivery-test-v1",
        approval_policy=ApprovalPolicy(draft_publication="human"),
    )
    return runner, worker, assignment.assignment_id, preview_id


def kill_during_publication(tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, point):
    github = MockGitHubAdapter(allowed_repositories=frozenset({REPO}), enforce_allowlist=True)
    runner, worker, assignment_id, preview_id = build(tmp_path, approved_delivery, verification_adapter,
                                                      monkeypatch, github)
    waiting = asyncio.run(runner.start(assignment_id))
    assert waiting.state == "waiting", waiting.error
    crash = Crash(tmp_path, tmp_path_factory.mktemp("killed") / "state")
    upsert, create = github.upsert_branch_commit, github.create_or_update_draft_pull_request

    def killing_upsert(**kwargs):
        head = upsert(**kwargs)
        if point == "after_push_head_unrecorded":
            crash.kill()
        return head

    def killing_create(**kwargs):
        if point == "after_push_before_pr":
            crash.kill()
        pull = create(**kwargs)
        if point == "after_pr_before_record":
            crash.kill()
        return pull

    with monkeypatch.context() as patch:
        patch.setattr(github, "upsert_branch_commit", killing_upsert)
        patch.setattr(github, "create_or_update_draft_pull_request", killing_create)
        with pytest.raises(Killed):
            asyncio.run(runner.approve(assignment_id, request_id=waiting.pending[0].request_id, approved=True))
    crash.restore()
    return github, worker, preview_id, waiting


def restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github):
    runner, worker, assignment_id, preview_id = build(tmp_path, approved_delivery, verification_adapter,
                                                      monkeypatch, github)
    return asyncio.run(runner.start(assignment_id)), worker, preview_id


@pytest.mark.parametrize("point", ["after_push_head_unrecorded", "after_push_before_pr", "after_pr_before_record"])
def test_killed_publication_resumes_to_one_branch_one_pr_one_record(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, point,
):
    github, worker, preview_id, waiting = kill_during_publication(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, point)
    assert worker.get(preview_id).state == "publishing"
    assert len(github.pull_requests.get(REPO, {})) == (1 if point == "after_pr_before_record" else 0)
    run, worker, preview_id = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert run.state == "completed", run.error
    # The original human decision is the only decision: nothing was re-admitted or re-approved.
    assert [decision.request_id for decision in run.decisions] == [waiting.pending[0].request_id]
    assert run.decisions[0].approval_policy.source == "human"
    record = worker.get(preview_id)
    assert record.state == "published"
    pulls = list(github.pull_requests[REPO].values())
    assert len(pulls) == 1 and pulls[0].number == record.publication["pull_number"]
    assert pulls[0].head_sha == record.head_revision and pulls[0].head_ref == record.branch
    assert list(github._branch_heads[REPO]) == [record.branch]
    # Re-pushing after an unrecorded push lands the identical deterministic commit (live: exact-commit adoption).
    assert {commit["head_sha"] for commit in github.branch_commits} == {record.head_revision}
    assert len(list((tmp_path / "state" / "deliveries").glob("*/state.json"))) == 1
    # Idempotent after completion.
    again, *_ = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert again == run and len(github.pull_requests[REPO]) == 1


def test_copied_marker_pr_from_another_author_is_not_adopted(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch,
):
    github, worker, preview_id, _ = kill_during_publication(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, "after_push_before_pr")
    publication = worker.get(preview_id).publication
    github.pull_requests.setdefault(REPO, {})[41] = GitHubPullRequest(
        number=41, title=publication["title"], body=publication["body"].strip(), state="open",
        head_ref=publication["branch"], base_ref=publication["base_ref"], draft=True, repository=REPO,
        head_sha=publication["head_sha"], author="intruder",
    )
    run, worker, preview_id = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert run.state == "failed"
    assert worker.get(preview_id).state == "failed" and "not adopted" in worker.get(preview_id).error
    assert list(github.pull_requests[REPO]) == [41]


@pytest.mark.parametrize("tamper", ["approved_at", "base_sha", "body", "bundle", "head_sha", "session"])
def test_interrupted_publication_binding_mismatch_stays_blocked(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, tamper,
):
    github, worker, preview_id, _ = kill_during_publication(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, "after_push_before_pr")
    path = next((tmp_path / "state" / "deliveries").glob("*/state.json"))
    raw = json.loads(path.read_text())
    record = raw.get("record", raw)
    if tamper == "approved_at":
        record["approved_at"] = "2020-01-01T00:00:00+00:00"
    elif tamper == "base_sha":
        record["publication"]["base_sha"] = "0" * 40
    elif tamper == "body":
        record["publication"]["body"] += "\nchanged"
    elif tamper == "bundle":
        record["bundle_payload"]["objective"] += " (edited)"
    elif tamper == "head_sha":
        record["publication"]["head_sha"] = "not-a-sha"
    else:
        record["session_id"] = "session-" + "0" * 32
    path.write_text(json.dumps(raw))
    if tamper == "base_sha":
        with pytest.raises(ValueError, match="Publication identity differs"):
            restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
        assert not github.pull_requests.get(REPO)
        return
    run, worker, preview_id = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    assert run.state == "failed"
    assert "Interrupted native invocation" in run.error or "replay" in (run.error or "")
    assert not github.pull_requests.get(REPO)


def test_interrupted_run_outside_publishing_is_still_refused(
    tmp_path, approved_delivery, verification_adapter, monkeypatch,
):
    github = MockGitHubAdapter(allowed_repositories=frozenset({REPO}), enforce_allowlist=True)
    runner, worker, assignment_id, preview_id = build(tmp_path, approved_delivery, verification_adapter,
                                                      monkeypatch, github)
    waiting = asyncio.run(runner.start(assignment_id))
    assert worker.get(preview_id).state == "verified"
    # A run left `running` by a kill before publication began (record still `verified`).
    store = FileRunStore(tmp_path / "runs")
    from aitobuild.organization_runner import ManagedRun, WorkflowDecision, workflow_input_digest
    pending = waiting.pending[0]
    store.save(ManagedRun.model_validate(waiting.model_dump() | {
        "state": "running", "pending": (),
        "decisions": (WorkflowDecision(request_id=pending.request_id, kind=pending.kind, actor_id="operator",
                                       value=True, data_digest=workflow_input_digest(pending.data)),),
    }))
    failed = asyncio.run(runner.start(assignment_id))
    assert failed.state == "failed" and "Interrupted native invocation" in failed.error
    assert not github.branch_commits and not github.pull_requests.get(REPO)


def _interrupted(tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch):
    github, *_ = kill_during_publication(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, "after_push_before_pr")
    runner, worker, assignment_id, preview_id = build(tmp_path, approved_delivery, verification_adapter,
                                                      monkeypatch, github)
    store = FileRunStore(tmp_path / "runs")
    run = store.get(assignment_id)
    assignment = runner._assignments.get(assignment_id)
    context = runner._context(run, assignment)
    return runner, worker, store, run, context


@pytest.mark.parametrize("tamper", ["value", "kind", "checkpoint_digest", "data_digest", "request_id", "no_decision"])
def test_runner_resumes_only_the_exact_recorded_approval(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, tamper,
):
    from aitobuild.organization_runner import ManagedRun
    runner, _, _, run, context = _interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    assert asyncio.run(runner._interrupted_continuation(run, context)) == run.decisions[-1]
    decision = run.decisions[-1]
    changed = {"value": {"value": False}, "kind": {"kind": "human_input"},
               "data_digest": {"data_digest": "0" * 64}, "request_id": {"request_id": "other-request"}}.get(tamper)
    if changed is not None:
        run = ManagedRun.model_validate(run.model_dump() | {"decisions": (decision.model_copy(update=changed),)})
    elif tamper == "checkpoint_digest":
        run = ManagedRun.model_validate(run.model_dump() | {"checkpoint_digest": "0" * 64})
    else:
        run = ManagedRun.model_validate(run.model_dump() | {"decisions": ()})
    assert asyncio.run(runner._interrupted_continuation(run, context)) is None


@pytest.mark.parametrize("tamper", ["state", "correction_task", "preview_id", "original_base", "record_base",
                                    "approved_at", "original_body"])
def test_delivery_hook_requires_the_exact_publishing_record(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, tamper,
):
    from dataclasses import replace as dc_replace
    runner, worker, _, run, context = _interrupted(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch)
    hook = runner._operations["delivery_publish"].resume_interrupted
    record = worker.get(context.assignment.preview_id)
    original = {key: record.publication[key] for key in (
        "title", "body", "commit_message", "checkout_digest", "tree_fingerprint", "changed_paths", "blob_shas",
        "file_modes", "base_ref", "branch", "repository", "issue_number", "base_sha")}
    original |= {"preview_id": record.preview_id, "approved_at": record.approved_at}
    assert hook(context, original) is True
    if tamper == "state":
        monkeypatch.setattr(worker, "get", lambda preview_id: dc_replace(record, state="verified"))
    elif tamper == "record_base":
        monkeypatch.setattr(worker, "get", lambda preview_id: dc_replace(
            record, publication={**record.publication, "base_sha": "0" * 40}))
    elif tamper == "correction_task":
        from types import SimpleNamespace
        bindings = hook.__self__
        monkeypatch.setattr(bindings, "_check", lambda context: None)
        assignment = context.assignment
        context = SimpleNamespace(run=context.run, assignment=SimpleNamespace(
            preview_id=assignment.preview_id, task_id="published-correction-" + assignment.task_id,
            bundle_content=assignment.bundle_content, approved_at=assignment.approved_at))
    else:
        key, value = {"preview_id": ("preview_id", "dp-other"), "original_base": ("base_sha", "0" * 40),
                      "approved_at": ("approved_at", "2020-01-01T00:00:00+00:00"),
                      "original_body": ("body", "changed")}[tamper]
        original = original | {key: value}
    assert hook(context, original) is False
