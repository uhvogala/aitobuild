"""#72 part 1: a GitHub transport failure is one transient error type, answered 503 `transient` by every route.

Pilot trial4-20261019-050235 stopped when a Developer tool approval answered 409 for the service's own commit lookup
("net/http: TLS handshake timeout"), which a caller cannot tell from a deliberate policy refusal."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from subprocess import CalledProcessError, CompletedProcess
import time

from agent_framework.openai import OpenAIChatCompletionClient
from fastapi import HTTPException
from fastapi.testclient import TestClient
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
from aitobuild.tools.github import GhCliGitHubAdapter, GitHubAPIError, GitHubTransientError, MockGitHubAdapter
from conftest import approve_preview
from test_dispatcher import approved_delivery as approved_delivery
from test_organization_planning import native_reply, planning_fixture, proposal

HEADERS = {"X-Internal-Token": "internal-test-token"}
TLS = 'Get "https://api.github.com/repos/fixture/widgets/commits/{sha}": net/http: TLS handshake timeout'


class FlakyCommitLookup(MockGitHubAdapter):
    """The mock adapter, except that while armed each target inspection goes through the real gh adapter, whose
    commit lookup gh answers with the pilot's TLS handshake timeout on every attempt. Each armed inspection's
    commit-lookup calls and sleeps are recorded (the worker may inspect concurrently, e.g. for follow-ups)."""

    def arm(self, monkeypatch) -> None:
        self.armed, self.inspections = True, []

        def fake_run(command, **kwargs):
            if "/commits/" in command[-1]:
                raise CalledProcessError(1, command, stderr=TLS.format(sha=command[-1].rsplit("/", 1)[1]))
            return CompletedProcess(command, 0, stdout=json.dumps({"id": 101, "full_name": "fixture/widgets"}), stderr="")

        monkeypatch.setattr(github_module, "run", fake_run)

    def inspect_planning_target(self, *, repository, repository_id, base_revision):
        if getattr(self, "armed", False):
            gh = _CountingAdapter(allowed_repositories=(repository,), jitter=lambda: 1.0)
            try:
                gh.inspect_planning_target(repository=repository, repository_id=repository_id, base_revision=base_revision)
            finally:
                self.inspections.append((gh.commit_calls, gh.sleeps))
        return super().inspect_planning_target(repository=repository, repository_id=repository_id,
                                               base_revision=base_revision)


class _CountingAdapter(GhCliGitHubAdapter):
    """The real gh adapter, counting its commit-lookup calls and recording its retry sleeps."""

    def __init__(self, **kwargs) -> None:
        self.commit_calls, self.sleeps = 0, []
        super().__init__(sleep=self.sleeps.append, **kwargs)

    def _api_once(self, endpoint, *, method, payload):
        if "/commits/" in endpoint:
            self.commit_calls += 1
        return super()._api_once(endpoint, method=method, payload=payload)


def test_failed_commit_lookup_during_a_tool_approval_is_503_transient_and_retries_land_once(
    tmp_path, test_config, approved_delivery, monkeypatch,
):
    """The pilot's path: an approval of a planned child's Developer tool request re-checks the child's planning
    target, whose commit lookup fails in transport. The route answers 503 `transient`, nothing is recorded and the
    request stays pending; the next attempt lands exactly once."""
    policy = ApprovalPolicy(task_admission="automatic", pm_issue_creation="human", pm_dependency_links="automatic")
    github = FlakyCommitLookup(repository_ids={"fixture/widgets": 101},
                               commit_files={("fixture/widgets", approved_delivery[4]): {}})
    tools: list[str] = []
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

    async def plan_and_wait_on_tool_approval():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies, document))) as transport:
            async with AsyncOpenAI(api_key="test", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                service, _, _, preview, previews, _, _, _ = planning_fixture(
                    tmp_path, profile, approved_delivery[1], previews=approved_delivery[0], repository="fixture/widgets",
                    repository_id=101, base_revision=approved_delivery[4], approval_policy=policy,
                    implementation_followups=True, github=github, handoff_operation=tool_approval)
                approve_preview(previews, preview.preview_id)
                worker = ManagedOrganizationWorker(service=service, store=FileWorkerStore(store), queue_follow_ups=True,
                                                   poll_seconds=0.01)
                worker.enqueue(preview.preview_id)
                await worker.start()
                try:
                    async with asyncio.timeout(10):
                        job = await worker.wait_idle(preview.preview_id)
                        assert job.state == "waiting", job.error  # human PM issue creation
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
                        assert child.state == "waiting", child.error  # the Developer tool approval
                finally:
                    await worker.close()
                return service, child_id

    service, child_id = asyncio.run(plan_and_wait_on_tool_approval())
    config = replace(test_config, approvals=policy, managed_worker=ManagedWorkerConfig(queue_follow_ups=True))
    app = create_app(config, managed_service_factory=lambda context: service,
                     managed_worker_factory=lambda bound, context: ManagedOrganizationWorker(
                         service=bound, store=FileWorkerStore(store), queue_follow_ups=True, poll_seconds=0.01))
    with TestClient(app) as http:
        before = http.get(f"/internal/organization/tasks/{child_id}", headers=HEADERS).json()
        waiting = before["managed_run"]
        assert waiting["state"] == "waiting" and before["managed_admission"]["state"] == "waiting"
        decision = {"assignment_id": waiting["assignment_id"], "request_id": waiting["pending"][0]["request_id"],
                    "approved": True}
        commands = len(FileWorkerStore(store).get(child_id).commands)

        github.arm(monkeypatch)
        failed = http.post("/internal/organization/tasks/approve", headers=HEADERS, json=decision)
        assert failed.status_code == 503, failed.text
        body = failed.json()
        assert body["transient"] is True and body["github_status"] is None
        assert "commits/" in body["detail"] and "TLS handshake timeout" in body["detail"]
        github.armed = False
        assert github.inspections and all(item == (4, [0.5, 1.0, 2.0]) for item in github.inspections)  # bounded

        after = http.get(f"/internal/organization/tasks/{child_id}", headers=HEADERS).json()
        assert after["managed_run"] == waiting and after["managed_admission"]["state"] == "waiting"
        assert len(FileWorkerStore(store).get(child_id).commands) == commands  # nothing recorded
        assert tools == []

        landed = http.post("/internal/organization/tasks/approve", headers=HEADERS, json=decision)
        assert landed.status_code == 200, landed.text
        deadline = time.monotonic() + 10
        while (status := http.get(f"/internal/organization/tasks/{child_id}", headers=HEADERS).json()
               )["managed_admission"]["state"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.02)
        assert status["managed_admission"]["state"] == "completed", status["managed_admission"]["error"]
        assert status["managed_run"]["state"] == "completed"
        assert [item["request_id"] for item in status["managed_run"]["decisions"]] == [decision["request_id"]]
        assert tools == [child_id]  # landed exactly once
        assert http.post("/internal/organization/tasks/approve", headers=HEADERS, json=decision).status_code == 409
        assert tools == [child_id]


def _probe_app(test_config, error: BaseException, *, wrap: bool):
    app = create_app(test_config)

    async def probe():
        try:
            raise error
        except (PermissionError, RuntimeError) as caught:
            if not wrap:
                raise
            raise HTTPException(status_code=409, detail=str(caught)) from caught

    app.add_api_route("/probe", probe, methods=["POST"])
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("wrap", [True, False])
def test_transient_failure_maps_to_503_from_any_route(test_config, wrap):
    """One mapping in the app: a route's 409 raised from a transient failure, or a transient failure no route
    caught, both answer 503 with the marker."""
    error = GitHubTransientError("gh api failed for GET repos/a/b: EOF")
    response = _probe_app(test_config, error, wrap=wrap).post("/probe")
    assert response.status_code == 503
    assert response.json() == {"detail": str(error), "transient": True, "github_status": None}


@pytest.mark.parametrize("error", [
    PermissionError("Approval policy changed"),
    GitHubAPIError("gh api failed for GET repos/a/b: gh: Not Found (HTTP 404)", status=404),
    RuntimeError("Unexpected GitHub commit response"),
])
def test_refusals_and_api_answers_stay_409(test_config, error):
    response = _probe_app(test_config, error, wrap=True).post("/probe")
    assert response.status_code == 409 and response.json() == {"detail": str(error)}


def test_deliberate_refusal_wrapping_a_transient_failure_stays_409(test_config):
    """Only the direct cause maps: a fail-closed refusal raised from a transient failure is still a refusal."""
    try:
        try:
            raise GitHubTransientError("gh api failed for POST repos/a/b/issues: EOF")
        except GitHubTransientError as transient:
            raise PermissionError("Issue creation is uncertain; no write repeated") from transient
    except PermissionError as refusal:
        error = refusal
    response = _probe_app(test_config, error, wrap=True).post("/probe")
    assert response.status_code == 409 and response.json()["detail"] == "Issue creation is uncertain; no write repeated"


def test_ordinary_http_errors_keep_their_shape(test_config):
    client = TestClient(create_app(test_config))
    response = client.post("/internal/organization/tasks/run", json={})
    assert response.status_code in {401, 404} and "transient" not in response.json()
