"""#65: a managed run keeps the approval policy it started with (related: #41).

A resume under a different policy is refused before any work, naming both digests, unless a human records an
override on the run; decisions after the override are made, and checked, under the new policy."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from pydantic import ValidationError

from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage
from aitobuild.organization_runner import (
    ApprovalPolicyChanged, ApprovalPolicyOverride, FileRunStore, ManagedOperation, ManagedRun, RuntimeActor,
    WorkflowInput,
)
from aitobuild.organization_service import (
    Consent, Human, InheritedReview, ManagedOrganizationService, OperatorPolicy, PlannedChild,
)
from test_organization_runner import runner_fixture
from test_interrupted_publication import REPO, build, kill_during_publication, restart
from test_dispatcher import approved_delivery as approved_delivery, verification_adapter as verification_adapter

START = ApprovalPolicy()
CHANGED = ApprovalPolicy(developer_tools="automatic")


def gated(calls):
    return ManagedOperation(
        lambda context, message: calls.append("initial") or WorkflowInput("Approve", {"n": 1}, "service_approval"),
        on_response=lambda context, original, response: calls.append(("effect", response)) or original,
        approval_stage=ApprovalStage.DEVELOPER_TOOLS,
    )


def waiting_run(tmp_path, calls):
    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": gated(calls)}, approval_policy=START)
    waiting = asyncio.run(runner.start(assignment.assignment_id))
    assert waiting.state == "waiting", waiting.error
    assert waiting.approval_policy_revision == START.revision == waiting.pinned_policy_revision
    return assignment, waiting


def restarted(tmp_path, calls, policy, actor=None):
    runner, *_ = runner_fixture(tmp_path, {"definition_probe": gated(calls)}, approval_policy=policy, actor=actor)
    return runner


def stored(tmp_path, assignment):
    return FileRunStore(tmp_path / "runs").get(assignment.assignment_id)


def test_start_pins_the_same_digest_admission_snapshots(tmp_path):
    calls = []
    _, waiting = waiting_run(tmp_path, calls)
    # One function: ApprovalPolicy.revision is what _admit's snapshot resolves decisions against.
    decision = START.resolve(ApprovalStage.TASK_ADMISSION, actor_id="operator", operation={})
    assert waiting.approval_policy_revision == decision.policy_revision


def test_same_policy_resumes(tmp_path):
    calls = []
    assignment, waiting = waiting_run(tmp_path, calls)
    runner = restarted(tmp_path, calls, ApprovalPolicy())
    done = asyncio.run(runner.approve(assignment.assignment_id, request_id=waiting.pending[0].request_id, approved=True))
    assert done.state == "completed", done.error
    assert not done.policy_overrides and done.decisions[0].approval_policy.policy_revision == START.revision


@pytest.mark.parametrize("entry", ["start", "approve"])
def test_changed_policy_is_refused_before_any_work(tmp_path, entry):
    calls = []
    assignment, waiting = waiting_run(tmp_path, calls)
    runner = restarted(tmp_path, calls, CHANGED)
    with pytest.raises(ApprovalPolicyChanged) as refused:
        if entry == "start":
            asyncio.run(runner.start(assignment.assignment_id))
        else:
            asyncio.run(runner.approve(assignment.assignment_id, request_id=waiting.pending[0].request_id, approved=True))
    message = str(refused.value)
    assert START.revision in message and CHANGED.revision in message
    assert (refused.value.pinned, refused.value.current) == (START.revision, CHANGED.revision)
    assert calls == ["initial"]  # no continuation ran
    assert stored(tmp_path, assignment) == waiting  # nothing recorded; the run stays resumable


def test_human_override_resumes_and_is_recorded(tmp_path):
    calls = []
    assignment, waiting = waiting_run(tmp_path, calls)
    runner = restarted(tmp_path, calls, CHANGED)
    before = datetime.now(tz=UTC)
    overridden = asyncio.run(runner.override_policy(assignment.assignment_id, evidence_kind="human",
                                                    reason="  pilot resumes with automatic tools  "))
    entry, = overridden.policy_overrides
    assert entry.evidence_kind == "human" and entry.actor_id == "operator"
    assert (entry.previous_revision, entry.revision) == (START.revision, CHANGED.revision)
    assert entry.reason == "pilot resumes with automatic tools" and entry.at >= before
    assert entry.after_decisions == 0 and overridden.pinned_policy_revision == CHANGED.revision
    assert calls == ["initial"] and overridden.state == "waiting"  # recording starts no work
    # Resume: the pending tool approval is now decided under the new (automatic) policy.
    done = asyncio.run(runner.start(assignment.assignment_id))
    assert done.state == "completed", done.error
    assert calls == ["initial", ("effect", True)]
    decision, = done.decisions
    assert decision.approval_policy.source == "operator_policy"
    assert decision.approval_policy.policy_revision == CHANGED.revision
    assert done.policy_revision_for_decision(0) == CHANGED.revision
    phases = done.policy_phases()
    assert [(item["revision"], item["from_decision"]) for item in phases] == [(START.revision, 0), (CHANGED.revision, 0)]
    assert phases[1]["override"]["reason"] == "pilot resumes with automatic tools"
    assert stored(tmp_path, assignment).policy_overrides == (entry,)


def test_decisions_before_an_override_keep_their_policy(tmp_path):
    calls = []

    def continuation(context, original, response):
        calls.append(response)
        return WorkflowInput("Again", {"n": len(calls)}, "service_approval") if len(calls) == 1 else original

    operation = ManagedOperation(lambda context, message: WorkflowInput("First", {"n": 0}, "service_approval"),
                                 on_response=continuation, approval_stage=ApprovalStage.DEVELOPER_TOOLS)
    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": operation})
    waiting = asyncio.run(runner.start(assignment.assignment_id))
    waiting = asyncio.run(runner.approve(assignment.assignment_id, request_id=waiting.pending[0].request_id, approved=True))
    assert waiting.state == "waiting" and len(waiting.decisions) == 1
    changed, *_ = runner_fixture(tmp_path, {"definition_probe": operation}, approval_policy=CHANGED)
    asyncio.run(changed.override_policy(assignment.assignment_id, evidence_kind="human", reason="switch"))
    done = asyncio.run(changed.start(assignment.assignment_id))
    assert done.state == "completed", done.error
    assert [item.approval_policy.policy_revision for item in done.decisions] == [START.revision, CHANGED.revision]
    assert [done.policy_revision_for_decision(index) for index in range(2)] == [START.revision, CHANGED.revision]
    assert done.policy_overrides[0].after_decisions == 1


@pytest.mark.parametrize("kind", ["operator_policy", "planned_child", "inherited_review", "consent"])
def test_non_human_override_evidence_is_refused(tmp_path, kind):
    calls = []
    assignment, waiting = waiting_run(tmp_path, calls)
    runner = restarted(tmp_path, calls, CHANGED)
    with pytest.raises(PermissionError, match="human"):
        asyncio.run(runner.override_policy(assignment.assignment_id, evidence_kind=kind, reason="policy"))
    with pytest.raises(ValidationError, match="human evidence"):
        ApprovalPolicyOverride(evidence_kind=kind, actor_id="operator", previous_revision=START.revision,
                               revision=CHANGED.revision, reason="r", after_decisions=0, at=datetime.now(tz=UTC))
    assert stored(tmp_path, assignment) == waiting


def test_service_override_takes_only_human_admission_evidence():
    service = object.__new__(ManagedOrganizationService)  # evidence is checked before any service state is used
    for evidence in (OperatorPolicy(), PlannedChild(), InheritedReview()):
        with pytest.raises(PermissionError, match=f"not {evidence.kind}"):
            asyncio.run(service.override_approval_policy("assignment-x", evidence=evidence, reason="r"))
    consent = Consent.model_construct(kind="consent")
    with pytest.raises(PermissionError, match="not consent"):
        asyncio.run(service.override_approval_policy("assignment-x", evidence=consent, reason="r"))
    with pytest.raises(ValueError, match="no task base"):
        asyncio.run(service.override_approval_policy("assignment-x", evidence=Human(base_revision="a" * 40), reason="r"))


def test_agent_context_cannot_override(tmp_path):
    calls = []
    assignment, waiting = waiting_run(tmp_path, calls)
    runner = restarted(tmp_path, calls, CHANGED, actor=lambda: RuntimeActor("agent", "coordinator"))
    with pytest.raises(PermissionError, match="human decision"):
        asyncio.run(runner.override_policy(assignment.assignment_id, evidence_kind="human", reason="policy"))
    assert stored(tmp_path, assignment) == waiting


def test_override_needs_a_reason_a_change_and_an_open_run(tmp_path):
    calls = []
    assignment, waiting = waiting_run(tmp_path, calls)
    same = restarted(tmp_path, calls, START)
    with pytest.raises(ValueError, match="nothing to override"):
        asyncio.run(same.override_policy(assignment.assignment_id, evidence_kind="human", reason="r"))
    changed = restarted(tmp_path, calls, CHANGED)
    with pytest.raises(ValueError, match="reason"):
        asyncio.run(changed.override_policy(assignment.assignment_id, evidence_kind="human", reason="  "))
    cancelled = asyncio.run(same.cancel(assignment.assignment_id))
    # A terminal run is never resumed, but an override may be appended for decisions made on it afterwards
    # (correction publication); nothing else on the receipt changes.
    overridden = asyncio.run(changed.override_policy(assignment.assignment_id, evidence_kind="human", reason="r"))
    assert overridden.state == "cancelled" and len(overridden.policy_overrides) == 1
    assert overridden.model_dump(exclude={"policy_overrides", "updated_at"}) == cancelled.model_dump(
        exclude={"policy_overrides", "updated_at"})
    with pytest.raises(ValueError, match="Terminal managed receipts are immutable"):
        FileRunStore(tmp_path / "runs").save(overridden.model_copy(update={"error": "rewritten"}))


def test_finalizing_run_takes_no_override(tmp_path):
    calls = []
    assignment, waiting = waiting_run(tmp_path, calls)
    finalizing = ManagedRun.model_validate(waiting.model_dump() | {
        "state": "finalizing", "terminal_state": "cancelled", "pending": (), "cleanup_succeeded": True})
    FileRunStore(tmp_path / "runs").save(finalizing)
    changed = restarted(tmp_path, calls, CHANGED)
    with pytest.raises(ValueError, match="finalizing"):
        asyncio.run(changed.override_policy(assignment.assignment_id, evidence_kind="human", reason="r"))


def test_correction_publication_without_a_managed_run_is_refused(monkeypatch):
    from types import SimpleNamespace

    service = object.__new__(ManagedOrganizationService)
    service._reviews = SimpleNamespace(correction_publication_binding=lambda preview_id: "b" * 64)
    service._worker = SimpleNamespace(publication_approval=lambda preview_id: SimpleNamespace(state="approved"))
    pushed = []
    service._worker.approve_and_publish_correction = lambda *args, **kwargs: pushed.append(kwargs)
    monkeypatch.setattr(service, "status", lambda preview_id: None, raising=False)
    with pytest.raises(PermissionError, match="managed correction run"):
        asyncio.run(service.publish_correction("correction-x", approval_digest="a" * 64, github=None))
    assert pushed == []


def test_overrides_are_append_only_and_must_chain(tmp_path):
    calls = []
    assignment, waiting = waiting_run(tmp_path, calls)
    changed = restarted(tmp_path, calls, CHANGED)
    overridden = asyncio.run(changed.override_policy(assignment.assignment_id, evidence_kind="human", reason="r"))
    runs = FileRunStore(tmp_path / "runs")
    with pytest.raises(ValueError, match="immutable"):
        runs.save(ManagedRun.model_validate(overridden.model_dump() | {"policy_overrides": ()}))
    entry = overridden.policy_overrides[0]
    with pytest.raises(ValidationError, match="chain"):
        ManagedRun.model_validate(waiting.model_dump() | {
            "policy_overrides": (entry.model_copy(update={"previous_revision": "f" * 64}),)})
    with pytest.raises(ValidationError, match="chain"):
        ManagedRun.model_validate(waiting.model_dump() | {
            "policy_overrides": (entry.model_copy(update={"after_decisions": 1}),)})


def _drop_pinned_digest(tmp_path, assignment):
    path = tmp_path / "runs" / sha256(assignment.assignment_id.encode()).hexdigest() / "run.json"
    document = json.loads(path.read_text())
    document.pop("approval_policy_revision")
    path.write_text(json.dumps(document))
    return FileRunStore(tmp_path / "runs").get(assignment.assignment_id)


def test_record_without_a_pinned_digest_fails_closed(tmp_path):
    calls = []
    assignment, waiting = waiting_run(tmp_path, calls)
    legacy = _drop_pinned_digest(tmp_path, assignment)
    assert legacy.approval_policy_revision is None and legacy.pinned_policy_revision is None
    # Even the default policy (the old fallback) does not resume it.
    runner = restarted(tmp_path, calls, START)
    with pytest.raises(ApprovalPolicyChanged, match="none recorded") as refused:
        asyncio.run(runner.approve(assignment.assignment_id, request_id=waiting.pending[0].request_id, approved=True))
    assert START.revision in str(refused.value)
    with pytest.raises(ApprovalPolicyChanged):
        asyncio.run(runner.start(assignment.assignment_id))
    assert calls == ["initial"] and stored(tmp_path, assignment) == legacy
    # A human override pins it and records that nothing was pinned before.
    overridden = asyncio.run(runner.override_policy(assignment.assignment_id, evidence_kind="human",
                                                    reason="legacy run predates pinning"))
    assert overridden.policy_overrides[0].previous_revision is None
    done = asyncio.run(runner.approve(assignment.assignment_id, request_id=waiting.pending[0].request_id, approved=True))
    assert done.state == "completed", done.error and calls == ["initial", ("effect", True)]


# --- the killed-publication restart (pilot kill point 2) under a changed policy ---------------------------


@pytest.mark.parametrize("then", ["unchanged", "override"])
def test_killed_publication_restart_under_changed_policy_pushes_nothing_until_override(
    tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, then,
):
    github, worker, preview_id, waiting = kill_during_publication(
        tmp_path, tmp_path_factory, approved_delivery, verification_adapter, monkeypatch, "after_push_before_pr")
    pushes, pulls = list(github.branch_commits), dict(github.pull_requests.get(REPO, {}))
    calls: list[str] = []
    for name in ("upsert_branch_commit", "create_or_update_draft_pull_request", "task_pull_requests"):
        original = getattr(github, name)
        monkeypatch.setattr(github, name, lambda *a, _o=original, _n=name, **k: calls.append(_n) or _o(*a, **k))
    changed = ApprovalPolicy(draft_publication="automatic")
    with pytest.raises(ApprovalPolicyChanged):
        restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github, changed)
    assert calls == [] and github.branch_commits == pushes and github.pull_requests.get(REPO, {}) == pulls
    assert worker.get(preview_id).state == "publishing"
    run = FileRunStore(tmp_path / "runs").get(waiting.assignment_id)
    assert run.state == "running" and not run.policy_overrides
    if then == "unchanged":
        # The same restart without the policy change still resumes it (no override needed).
        done, worker, _ = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github)
    else:
        runner, *_ = build(tmp_path, approved_delivery, verification_adapter, monkeypatch, github, changed)
        asyncio.run(runner.override_policy(waiting.assignment_id, evidence_kind="human", reason="kill point 2"))
        assert calls == []
        done, worker, _ = restart(tmp_path, approved_delivery, verification_adapter, monkeypatch, github, changed)
        assert done.policy_overrides[0].after_decisions == 1  # the kill-time decision stays under the start policy
        assert done.policy_revision_for_decision(0) == ApprovalPolicy(draft_publication="human").revision
    assert done.state == "completed", done.error
    assert worker.get(preview_id).state == "published" and len(github.pull_requests[REPO]) == 1


# --- the managed worker keeps a policy-refused run for the override -------------------------------------


def test_worker_keeps_a_policy_refused_run_and_requeues_it_after_a_human_override(tmp_path, approved_delivery):
    from test_organization_worker import finish, make_worker

    _, delivery, preview_id, _, _ = approved_delivery
    calls: list = []
    operation = gated(calls)

    async def exercise():
        first, service, _, _ = make_worker(tmp_path, approved_delivery, operation)
        first.enqueue(preview_id)
        await first.start()
        try:
            assert (await finish(first, preview_id)).state == "waiting"
        finally:
            await first.close()
        run = service.recorded_run(first.store.get(preview_id).task_id)
        budget = delivery.budget_path(preview_id).read_bytes()
        second, changed, _, _ = make_worker(tmp_path, approved_delivery, operation, approval_policy=CHANGED)
        assert second.requeue_after_policy_override(preview_id) is None  # nothing was refused yet
        second.enqueue_decision(run.assignment_id, request_id=run.pending[0].request_id, kind="approve", value=True)
        await second.start()
        try:
            refused = await finish(second, preview_id)
            await asyncio.sleep(0.2)  # let the worker loop leave the job's invocation lock
            assert refused.state == "waiting" and START.revision in refused.error and CHANGED.revision in refused.error
            assert refused.refusal == "approval_policy_changed"  # structured, not read back from the error text
            kept = changed.recorded_run(refused.task_id)
            assert kept == run and calls == ["initial"]
            assert delivery.budget_path(preview_id).read_bytes() == budget  # not aborted
            with pytest.raises(PermissionError, match="overridden"):
                second.requeue_after_policy_override(preview_id)
            await changed.override_approval_policy(run.assignment_id, evidence=Human(), reason="tools now automatic")
            requeued = second.requeue_after_policy_override(preview_id)
            assert requeued.state == "queued" and requeued.refusal is None and requeued.error is None
            done = await finish(second, preview_id)
            assert done.state == "completed", done.error
        finally:
            await second.close()
        final = changed.recorded_run(done.task_id)
        assert final.policy_overrides[0].actor_id == "trusted-operator"
        assert final.decisions[0].approval_policy.policy_revision == CHANGED.revision

    asyncio.run(exercise())
    assert calls == ["initial", ("effect", True)]


# --- the operator HTTP control: the authenticated endpoint is the human evidence -------------------------


def test_http_override_records_human_evidence_then_resume_continues(
    test_config, tmp_path, repository_issue_body, local_issue_repository,
):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from aitobuild.config import RepositorySourceConfig
    from test_app import _internal_headers, _managed_app

    source, revision = local_issue_repository
    config = replace(test_config, developer=replace(test_config.developer, repository_sources=(
        RepositorySourceConfig("fixture/widgets", 101, str(source)),
    )))
    calls: list = []
    operation = ManagedOperation(lambda context, message: calls.append("initial") or WorkflowInput("Approve", {"n": 1}, "service_approval"),
                                 on_response=lambda context, original, response: calls.append(("effect", response)) or "done",
                                 approval_stage=ApprovalStage.DEVELOPER_TOOLS)
    headers = _internal_headers(config)
    client = TestClient(_managed_app(config, tmp_path, calls, operation))
    preview = client.post("/internal/developer/preview", headers=headers, json={
        "github_event": "issues", "action": "opened", "body": repository_issue_body}).json()
    waiting = client.post("/internal/developer/preview/approve", headers=headers, json={
        "preview_id": preview["preview_id"], "base_revision": revision}).json()["managed_run"]
    assert waiting["state"] == "waiting", waiting["error"]
    changed = replace(config, approvals=CHANGED)
    client = TestClient(_managed_app(changed, tmp_path, calls, operation))
    decision = {"assignment_id": waiting["assignment_id"], "request_id": waiting["pending"][0]["request_id"],
                "approved": True}
    refused = client.post("/internal/organization/tasks/approve", headers=headers, json=decision)
    assert refused.status_code == 409 and START.revision in refused.text and CHANGED.revision in refused.text
    override = {"assignment_id": waiting["assignment_id"], "reason": "tools are automatic from here"}
    assert client.post("/internal/organization/tasks/override_policy", json=override).status_code == 401
    assert client.post("/internal/organization/tasks/override_policy", headers=headers,
                       json=override | {"evidence_kind": "operator_policy"}).status_code == 400
    assert client.post("/internal/organization/tasks/override_policy", headers=headers,
                       json={"assignment_id": waiting["assignment_id"], "reason": " "}).status_code == 400
    assert calls == ["initial"]
    recorded = client.post("/internal/organization/tasks/override_policy", headers=headers, json=override)
    assert recorded.status_code == 200, recorded.text
    entry, = recorded.json()["managed_run"]["policy_overrides"]
    assert entry["evidence_kind"] == "human" and entry["actor_id"] == "configured-operator"
    assert (entry["previous_revision"], entry["revision"]) == (START.revision, CHANGED.revision)
    done = client.post("/internal/organization/tasks/approve", headers=headers, json=decision)
    assert done.status_code == 200 and done.json()["managed_run"]["state"] == "completed", done.text
    assert calls == ["initial", ("effect", True), "cleanup"]


def test_requeue_reads_the_structured_refusal_not_the_error_text(tmp_path, approved_delivery):
    from test_organization_worker import finish, make_worker

    _, _, preview_id, _, _ = approved_delivery
    calls: list = []

    async def exercise():
        first, service, _, _ = make_worker(tmp_path, approved_delivery, gated(calls))
        first.enqueue(preview_id)
        await first.start()
        try:
            waiting = await finish(first, preview_id)
        finally:
            await first.close()
        assert waiting.state == "waiting" and waiting.refusal is None
        # A waiting job whose error merely reads like a policy refusal is not requeued.
        mimic = waiting.model_copy(update={"error": "Approval policy changed for run-x: pinned a, current b."})
        first.store.save(mimic)
        assert first.requeue_after_policy_override(preview_id) is None
        with pytest.raises(ValueError, match="only on a waiting job"):
            mimic.model_validate(mimic.model_dump() | {"state": "queued", "refusal": "approval_policy_changed"})

    asyncio.run(exercise())
