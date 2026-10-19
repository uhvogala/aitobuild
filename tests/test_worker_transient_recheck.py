"""#72 part 2: a GitHub transport failure while the worker re-checks a saved decision leaves the run waiting.

Pilot trial4-20261019-052917 (05:42-05:43 EEST): after the 6th Developer tool approval, the worker's re-check of
the planned child's planning target hit "net/http: TLS handshake timeout" on GET commits/<base>. The worker failed
the job, cancelled the run ("Operator cancelled workflow", though nobody cancelled) and aborted the budget."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, TimeoutExpired
import sys
import time

from agent_framework.openai import OpenAIChatCompletionClient
import httpx
from openai import AsyncOpenAI
import pytest

from aitobuild.app import create_app
from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage
from aitobuild.config import ManagedWorkerConfig
from aitobuild.organization_runner import ManagedOperation, WorkflowInput
from aitobuild.organization_runtime import ModelProfile
from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker
from aitobuild.tools import github as github_module
from aitobuild.tools.github import GhCliGitHubAdapter, MockGitHubAdapter
from conftest import approve_preview
from test_dispatcher import approved_delivery as approved_delivery
from test_organization_planning import native_reply, planning_fixture, proposal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))

import common as trials  # noqa: E402

TLS = 'Get "https://api.github.com/repos/fixture/widgets/commits/{sha}": net/http: TLS handshake timeout'
POLICY = ApprovalPolicy(task_admission="automatic", pm_issue_creation="human", pm_dependency_links="automatic")


class FailingCommitLookup(MockGitHubAdapter):
    """The mock adapter, except that while `failure` is set every planning-target inspection goes through the real
    gh adapter, whose commit lookup gh answers with that failure on every attempt."""

    failure: str | None = None
    delay: float = 0.0

    def fail_with(self, monkeypatch, failure: str | None) -> None:
        self.failure, self.commit_calls, self.inspections, self.calls_per_inspection = failure, 0, 0, []

        def fake_run(command, **kwargs):
            if "/commits/" in command[-1]:
                self.commit_calls += 1
                if self.failure == "timeout":
                    raise TimeoutExpired(command, kwargs["timeout"])
                if self.failure == "not_found":
                    raise CalledProcessError(1, command, stderr="gh: Not Found (HTTP 404)")
                raise CalledProcessError(1, command, stderr=TLS.format(sha=command[-1].rsplit("/", 1)[1]))
            return CompletedProcess(command, 0, stdout=json.dumps({"id": 101, "full_name": "fixture/widgets"}), stderr="")

        monkeypatch.setattr(github_module, "run", fake_run)

    def inspect_planning_target(self, *, repository, repository_id, base_revision):
        if self.delay:
            time.sleep(self.delay)
        if self.failure is not None:
            self.inspections += 1
            before = self.commit_calls
            try:
                GhCliGitHubAdapter(allowed_repositories=(repository,), sleep=lambda _: None).inspect_planning_target(
                    repository=repository, repository_id=repository_id, base_revision=base_revision)
            finally:
                self.calls_per_inspection.append(self.commit_calls - before)
        return super().inspect_planning_target(repository=repository, repository_id=repository_id,
                                               base_revision=base_revision)


def waiting_on_tool_approval(tmp_path: Path, approved_delivery, github: MockGitHubAdapter, tools: list[str]):
    """A planned child (automatic admission) whose worker job waits on a Developer tool approval; worker closed."""
    bodies: list[object] = []
    document = proposal()
    document["issues"].append({"key": "third", "title": "Third issue", "objective": "Implement third behavior",
                               "acceptance_criteria": ["Third behavior passes"]})
    tool_approval = ManagedOperation(
        lambda context, message: WorkflowInput("Native delivery tool approval", {"tool": "developer_read_file"},
                                               "service_approval", approval_stage=ApprovalStage.DEVELOPER_TOOLS),
        on_response=lambda context, original, approved: tools.append(context.assignment.preview_id) or "read",
        approval_stage=ApprovalStage.DEVELOPER_TOOLS)
    store = tmp_path / "worker.json"

    async def prepare():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies, document))) as transport:
            async with AsyncOpenAI(api_key="test", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                service, _, _, preview, previews, _, _, _ = planning_fixture(
                    tmp_path, profile, approved_delivery[1], previews=approved_delivery[0], repository="fixture/widgets",
                    repository_id=101, base_revision=approved_delivery[4], approval_policy=POLICY,
                    implementation_followups=True, github=github, handoff_operation=tool_approval)
                approve_preview(previews, preview.preview_id)
                worker = ManagedOrganizationWorker(service=service, store=FileWorkerStore(store), queue_follow_ups=True,
                                                   poll_seconds=0.01)
                worker.enqueue(preview.preview_id)
                await worker.start()
                try:
                    async with asyncio.timeout(10):
                        job = await worker.wait_idle(preview.preview_id)
                        assert job.state == "waiting", job.error
                        run = service.recorded_run(job.task_id)
                        await worker.close()
                        worker = ManagedOrganizationWorker(service=service, store=FileWorkerStore(store),
                                                           queue_follow_ups=True, poll_seconds=0.01)
                        worker.enqueue_decision(run.assignment_id, request_id=run.pending[0].request_id,
                                                kind="approve", value=True)
                        await worker.start()
                        job = await worker.wait_idle(preview.preview_id)
                        assert job.state == "completed", job.error
                        child_id = service.recorded_run(job.task_id).outputs[0]["developer_previews"][0]
                        while worker.store.get(child_id) is None:
                            await asyncio.sleep(0.01)
                        child = await worker.wait_idle(child_id)
                        assert child.state == "waiting", child.error
                finally:
                    await worker.close()
                return service, child_id

    service, child_id = asyncio.run(prepare())
    return service, child_id, store


def _budget(service, child_id: str) -> dict:
    return json.loads(service.budget_path(child_id).read_text())


def _approve(service, store: Path, child_id: str, **worker_kwargs) -> tuple[ManagedOrganizationWorker, str]:
    run = service.recorded_run(FileWorkerStore(store).get(child_id).task_id)
    worker = ManagedOrganizationWorker(service=service, store=FileWorkerStore(store), queue_follow_ups=True,
                                       poll_seconds=0.01, transient_retry_seconds=0.01,
                                       transient_retry_cap_seconds=0.05, **worker_kwargs)
    worker.enqueue_decision(run.assignment_id, request_id=run.pending[0].request_id, kind="approve", value=True)
    return worker, run.pending[0].request_id


async def _until(predicate, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("failure,attempts", [("tls", 4), ("timeout", 1)])
def test_transient_recheck_failure_keeps_the_run_waiting_then_lands_once(
    tmp_path, approved_delivery, monkeypatch, failure, attempts,
):
    github = FailingCommitLookup(repository_ids={"fixture/widgets": 101},
                                 commit_files={("fixture/widgets", approved_delivery[4]): {}})
    tools: list[str] = []
    service, child_id, store = waiting_on_tool_approval(tmp_path, approved_delivery, github, tools)
    task_id = FileWorkerStore(store).get(child_id).task_id
    waiting = service.recorded_run(task_id)
    budget_before = _budget(service, child_id)
    worker, request_id = _approve(service, store, child_id)
    github.fail_with(monkeypatch, failure)  # after enqueue: the worker's own re-check fails, on every attempt

    async def exercise():
        await worker.start()
        try:
            await _until(lambda: worker.diagnostics()["transient_retries"].get(child_id, 0) >= 3)
            job = worker.store.get(child_id)
            assert job.state == "queued" and job.error is None  # reads exactly like any queued job
            reason = "TLS handshake timeout" if failure == "tls" else "gh api timed out"
            assert reason in worker.diagnostics()["transient_deferrals"][child_id]
            _assert_harness_sees_deferred_as_pending(tmp_path, job, service.recorded_run(task_id))
            assert set(github.calls_per_inspection) == {attempts}  # bounded read retry inside the adapter
            run = service.recorded_run(task_id)
            assert run.state == "waiting" and run.pending == waiting.pending and run.decisions == ()
            assert run.error is None and _budget(service, child_id) == budget_before  # not aborted, no cancel
            assert service.recorded_assignment(task_id).state == "claimed"
            assert tools == []

            github.failure = None  # GitHub recovers
            job = await asyncio.wait_for(worker.wait_idle(child_id), 10)
            assert job.state == "completed", job.error
            run = service.recorded_run(task_id)
            assert run.state == "completed" and [item.request_id for item in run.decisions] == [request_id]
            assert tools == [child_id]  # landed exactly once
            assert child_id not in worker.diagnostics()["transient_retries"]
        finally:
            await worker.close()

    asyncio.run(exercise())


def test_recheck_failing_past_the_budget_deadline_fails_through_the_existing_error_path(
    tmp_path, approved_delivery, monkeypatch,
):
    """A re-check still failing once the budget deadline has passed is not deferred again: the job fails through
    the worker's existing error path (job failed with the transport error, run cancelled, budget aborted), nothing
    is recorded, and the worker never drives the decision itself (no direct service.decide/respond)."""
    github = FailingCommitLookup(repository_ids={"fixture/widgets": 101},
                                 commit_files={("fixture/widgets", approved_delivery[4]): {}})
    tools: list[str] = []
    service, child_id, store = waiting_on_tool_approval(tmp_path, approved_delivery, github, tools)
    task_id = FileWorkerStore(store).get(child_id).task_id
    worker, _ = _approve(service, store, child_id)
    github.fail_with(monkeypatch, "tls")
    direct: list[str] = []

    def refuse(name):
        async def call(*args, **kwargs):
            direct.append(name)
            raise AssertionError(f"service.{name} must not be called past the deadline")
        return call

    monkeypatch.setattr(service, "decide", refuse("decide"))
    monkeypatch.setattr(service, "respond", refuse("respond"))

    async def exercise():
        await worker.start()
        try:
            await _until(lambda: worker.diagnostics()["transient_retries"].get(child_id, 0) >= 2)
            path = service.budget_path(child_id)
            state = json.loads(path.read_text())
            state["deadline"] = time.time() - 1
            path.write_text(json.dumps(state))
            job = await asyncio.wait_for(worker.wait_idle(child_id), 10)
        finally:
            await worker.close()
        run = service.recorded_run(task_id)
        assert job.state == "failed" and "TLS handshake timeout" in job.error  # the transport error, as reported
        assert run.state == "cancelled" and run.decisions == () and tools == []  # today's existing path (#78 label)
        assert _budget(service, child_id).get("aborted") is True
        assert direct == [] and child_id not in worker.diagnostics()["transient_retries"]

    asyncio.run(exercise())


def test_a_non_transient_recheck_failure_still_fails_as_before(tmp_path, approved_delivery, monkeypatch):
    """Only GitHubTransientError defers: an API answer (404) fails the job, cancels the run and aborts the budget
    exactly as on master (that end-state label is #78's, unchanged here)."""
    github = FailingCommitLookup(repository_ids={"fixture/widgets": 101},
                                 commit_files={("fixture/widgets", approved_delivery[4]): {}})
    tools: list[str] = []
    service, child_id, store = waiting_on_tool_approval(tmp_path, approved_delivery, github, tools)
    task_id = FileWorkerStore(store).get(child_id).task_id
    worker, _ = _approve(service, store, child_id)
    github.fail_with(monkeypatch, "not_found")

    async def exercise():
        await worker.start()
        try:
            return await asyncio.wait_for(worker.wait_idle(child_id), 10)
        finally:
            await worker.close()

    job = asyncio.run(exercise())
    assert job.state == "failed" and "HTTP 404" in job.error
    assert github.calls_per_inspection[0] == 1  # an API answer is never retried
    run = service.recorded_run(task_id)
    assert run.state == "cancelled" and run.decisions == () and tools == []
    assert _budget(service, child_id).get("aborted") is True
    assert not worker.diagnostics()["transient_retries"]


def test_enqueueing_a_decision_does_not_block_the_event_loop(tmp_path, test_config, approved_delivery, monkeypatch):
    """The approval route re-checks admission (GitHub reads) off the event loop: other requests keep answering."""
    github = FailingCommitLookup(repository_ids={"fixture/widgets": 101},
                                 commit_files={("fixture/widgets", approved_delivery[4]): {}})
    tools: list[str] = []
    service, child_id, store = waiting_on_tool_approval(tmp_path, approved_delivery, github, tools)
    run = service.recorded_run(FileWorkerStore(store).get(child_id).task_id)
    config = replace(test_config, approvals=POLICY, managed_worker=ManagedWorkerConfig(queue_follow_ups=True))
    app = create_app(config, managed_service_factory=lambda context: service,
                     managed_worker_factory=lambda bound, context: ManagedOrganizationWorker(
                         service=bound, store=FileWorkerStore(store), queue_follow_ups=True, poll_seconds=0.01))
    github.delay = 1.0

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            started = time.monotonic()
            approve = asyncio.create_task(client.post(
                "/internal/organization/tasks/approve", headers={"X-Internal-Token": "internal-test-token"},
                json={"assignment_id": run.assignment_id, "request_id": run.pending[0].request_id, "approved": True}))
            await asyncio.sleep(0.1)
            health = await client.get("/health")
            answered = time.monotonic() - started
            assert health.status_code == 200 and answered < 0.8 and not approve.done()
            response = await approve
            assert response.status_code == 200, response.text

    asyncio.run(exercise())


def _assert_harness_sees_deferred_as_pending(tmp_path: Path, job, run) -> None:
    """The pilot harness's readers on a real deferred job (queued, ``error`` empty, run waiting on the request):
    it is not a failure for ``drive``'s phase check and nothing is re-sent; ``_decision_landed`` reports it landed,
    which is right: the decision is in the worker's journal (a re-sent approval would be refused, 409)."""
    deferred = {"managed_admission": job.model_dump(mode="json"), "managed_run": run.model_dump(mode="json")}
    done = {"managed_admission": {"state": "completed"}, "managed_run": {"state": "completed", "pending": []}}
    reads: list[str] = []

    class Client:
        def get(self, path, **_):
            reads.append(path)
            if path == "/internal/organization/worker":
                return httpx.Response(200, json={"health": {"workers_alive": 1, "workers_expected": 1}})
            return httpx.Response(200, json=deferred if reads.count(path) <= 3 else done)

        def post(self, path, **_):
            raise AssertionError(f"nothing is re-sent for a deferred decision: {path}")

    operator = trials.Operator(Client(), "token", trials.Recorder(tmp_path / "harness"), poll=0.001, timeout=5,
                               sleep=lambda _: None)
    assert operator._decision_landed(job.preview_id, run.pending[0].request_id) is True
    reads.clear()
    assert operator.drive(job.preview_id, "child")["managed_run"]["state"] == "completed"  # never seen as failed
    assert "/internal/organization/worker" in reads  # a queued job only checks for a live worker loop
    events = [json.loads(line) for line in operator.recorder.events.read_text().splitlines()]
    statuses = [event for event in events if event["kind"] == "status"]
    assert statuses[0]["job"] == "queued" and statuses[0]["run"] == "waiting" and statuses[0]["error"] is None
