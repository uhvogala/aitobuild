from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess

from agent_framework.openai import OpenAIChatCompletionClient
import httpx
from openai import AsyncOpenAI
import pytest

from aitobuild.organization import FileDefinitionStore, parse_organization_definition
from aitobuild.organization_assignments import AssignmentService, FileAssignmentStore
from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.organization_roles import NativeManagedRoles, PublishedReviewTarget
from aitobuild.organization_runner import FileRunStore, ManagedWorkflowRunner, RuntimeActor
from aitobuild.organization_runtime import ModelProfile, bootstrap_organization
from test_organization_assignments import approved_task
from aitobuild.developer_preview import DeveloperPreviewRegistry
from aitobuild.tools.github import GitHubBlobChange, MockGitHubAdapter, _git_blob_sha
from test_dispatcher import (
    approved_delivery as approved_delivery, implemented_delivery as implemented_delivery,
    verification_adapter as verification_adapter,
)


def role_fixture(tmp_path, profile, *, role="pm", preview=None, previews=None, budget_path_for=None):
    document = json.loads((Path(__file__).resolve().parents[1] / "config/organization.example.json").read_text())
    selected = "planner" if role == "pm" else "reviewer"
    operation = "pm_propose_assignment" if role == "pm" else "architect_review_published"
    if role == "architect":
        document["agents"].append({"id": selected, "role": role, "instructions": "Inspect the approved published target."})
        document["teams"][0]["members"].append(selected)
    document["workflows"].append({"id": "role_probe", "document": {
        "format": "python_graph", "start": "role", "nodes": [{"id": "role", "kind": "operation", "operation": operation}],
        "outputs": ["role"],
    }})
    document["routes"].append({"id": "role_route", "events": ["role.task"], "team": "product", "workflow": "role_probe",
                               "delegation": {"strategy": "rules", "eligible_agents": [selected], "target_agent": selected}})
    definitions = FileDefinitionStore(tmp_path / "definitions")
    snapshot = definitions.save(parse_organization_definition(json.dumps(document)))
    previews = previews or DeveloperPreviewRegistry(tmp_path / "previews.json")
    preview = preview or approved_task(tmp_path, previews)
    assignments = FileAssignmentStore(tmp_path / "assignments.json")
    service = AssignmentService(definitions=definitions, previews=previews, assignments=assignments,
                                budget_path_for=budget_path_for or (lambda preview_id: tmp_path / "budgets" / (preview_id + ".json")))
    assignment = service.assign(organization_id=snapshot.organization_id, revision=snapshot.revision,
                                event="role.task", preview_id=preview.preview_id)

    def runtime_for(pinned):
        return bootstrap_organization(pinned, model_profiles={"default": profile}, default_model_profile="default",
                                      state_dir=tmp_path / "agents")

    def runner(adapter):
        return ManagedWorkflowRunner(definitions=definitions, assignments=assignments, assignment_service=service,
                                     runs=FileRunStore(tmp_path / "runs"), actor_provider=lambda: RuntimeActor("operator", "operator"),
                                     operations=adapter.operations, cleanup=adapter.cleanup, binding_revision="roles-v1")

    return assignment, runtime_for, runner, assignments


@pytest.mark.parametrize("proposal", [
    {"agent_id": "developer_one", "rationale": "Fits the approved task"},
    {"agent_id": "outside", "rationale": "Outside configured eligibility"},
    {"agent_id": "developer_one", "rationale": "Actor injection", "coordinator_id": "model"},
    "not JSON",
])
def test_native_pm_proposal_is_scoped_read_only_metadata(tmp_path, proposal):
    bodies = []

    def reply(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "pm_inspect_approved_task", "arguments": "{}",
            }}]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": json.dumps(proposal) if isinstance(proposal, dict) else proposal}
            finish = "stop"
        return httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 1, "model": "test-model",
                                        "choices": [{"index": 0, "message": message, "finish_reason": finish}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
            async with AsyncOpenAI(api_key="test-key", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                assignment, runtime_for, make_runner, assignments = role_fixture(tmp_path, profile)
                before = Path(assignment.budget_path).read_bytes()
                adapter = NativeManagedRoles(runtime_for=runtime_for, state_dir=tmp_path / "roles", proposal_event="github.issue.ready")
                run = await make_runner(adapter).start(assignment.assignment_id)
                if isinstance(proposal, dict) and proposal.get("rationale") == "Fits the approved task":
                    assert run.state == "completed", run.error
                    assert run.outputs[0]["proposal"]["agent_id"] == "developer_one"
                    assert run.outputs[0]["metadata_only"] is True
                    assert Path(assignment.budget_path).read_bytes() == before
                else:
                    assert run.state == "failed" and run.error
                    assert json.loads(Path(assignment.budget_path).read_text()) == json.loads(before) | {"aborted": True}
                assert assignments.for_task(assignment.task_id).state == run.state
                assert len(json.loads((tmp_path / "assignments.json").read_text())["assignments"]) == 1
                assert await make_runner(adapter).start(assignment.assignment_id) == run

    asyncio.run(exercise())
    assert len(bodies) == 2
    assert [entry["function"]["name"] for entry in bodies[0]["tools"]] == ["pm_inspect_approved_task"]


@pytest.mark.parametrize("proposal,detached", [
    ({"agent_id": "developer_one", "rationale": "Fits approved scope"}, False),
    ({"agent_id": "developer_one", "rationale": "Fits approved scope"}, True),
    ({"agent_id": "outside", "rationale": "Not eligible"}, False),
    ({"agent_id": "developer_one", "rationale": "Inject actor", "actor_id": "model"}, False),
    ("malformed JSON", False),
])
def test_native_coordinator_assigns_through_trusted_service_once(tmp_path, approved_delivery, proposal, detached):
    from aitobuild.organization_roles import NativeCoordinatorProposal
    from test_organization_runner import _service_fixture

    previews, worker, preview_id, _, _ = approved_delivery
    bodies = []

    def reply(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "inspect", "type": "function", "function": {
                "name": "pm_inspect_approved_task", "arguments": "{}",
            }}]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": json.dumps(proposal) if isinstance(proposal, dict) else proposal}
            finish = "stop"
        return httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 1, "model": "test-model",
                                        "choices": [{"index": 0, "message": message, "finish_reason": finish}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
            async with AsyncOpenAI(api_key="test-key", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))

                def runtime_for(snapshot):
                    return bootstrap_organization(snapshot, model_profiles={"default": profile}, default_model_profile="default",
                                                  state_dir=tmp_path / "coordinator-agents")

                def coordinator():
                    return NativeCoordinatorProposal(runtime_for=runtime_for, state_dir=tmp_path / "coordinator",
                                                     previews=previews, budget_path_for=worker.budget_path,
                                                     event="github.issue.ready", coordinator_id="planner", binding_revision="pm-v1").service_binding

                service, assignments, calls = _service_fixture(tmp_path, approved_delivery, coordinator=coordinator())
                worker.prepare(preview_id)
                initial = json.loads(worker.budget_path(preview_id).read_text())
                if not isinstance(proposal, dict) or proposal.get("rationale") != "Fits approved scope":
                    with pytest.raises((ValueError, PermissionError)):
                        await service.consume(preview_id)
                    assert assignments.for_task(previews.get(preview_id).bundle_payload["task_id"]) is None
                    assert json.loads(worker.budget_path(preview_id).read_text()) == initial | {"aborted": True}
                    receipt = json.loads(next((tmp_path / "coordinator" / "receipts").glob("*.json")).read_text())
                    assert receipt["state"] == "failed"
                    with pytest.raises((ValueError, PermissionError, TimeoutError)):
                        await service.consume(preview_id)
                    return
                if detached:
                    from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker

                    background = ManagedOrganizationWorker(service=service, store=FileWorkerStore(tmp_path / "native-worker.json"))
                    assert background.enqueue(preview_id).state == "queued"
                    await background.start()
                    try:
                        assert (await background.wait_idle(preview_id)).state == "completed"
                        assert background.enqueue(preview_id).state == "completed"
                        run = service.status(preview_id)
                    finally:
                        await background.close()
                else:
                    run = await service.consume(preview_id)
                assert run.state == "completed", run.error
                assignment = assignments.get(run.assignment_id)
                assert assignment.agent_id == "developer_one" and assignment.actor_id == "planner"
                assert calls == ["run", "cleanup"]
                before = worker.budget_path(preview_id).read_bytes()
                assert await service.consume(preview_id) == run
                restarted, _, _ = _service_fixture(tmp_path, approved_delivery, coordinator=coordinator())
                assert await restarted.consume(preview_id) == run
                assert worker.budget_path(preview_id).read_bytes() == before

    asyncio.run(exercise())
    assert len(bodies) == 2
    assert [entry["function"]["name"] for entry in bodies[0]["tools"]] == ["pm_inspect_approved_task"]


@pytest.mark.parametrize("scenario", ["capacity", "interrupted", "corrupt", "binding", "revision", "deadline", "symlink", "missing_budget", "event"])
def test_native_coordinator_receipt_recovery_keeps_original_admission_pins(tmp_path, approved_delivery, scenario):
    from aitobuild.organization_roles import NativeCoordinatorProposal
    from aitobuild.organization_runner import ManagedOperation, WorkflowInput
    from test_organization_runner import _service_fixture

    previews, worker, preview_id, _, revision = approved_delivery
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 1, "model": "test-model",
                                        "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                            "role": "assistant", "content": json.dumps({"agent_id": "developer_one", "rationale": "Fits scope"}),
                                        }}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
            async with AsyncOpenAI(api_key="test-key", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))

                def runtime_for(snapshot):
                    return bootstrap_organization(snapshot, model_profiles={"default": profile}, default_model_profile="default",
                                                  state_dir=tmp_path / "coordinator-agents")

                def coordinator(binding="pm-v1", event="github.issue.ready"):
                    return NativeCoordinatorProposal(runtime_for=runtime_for, state_dir=tmp_path / "coordinator",
                                                     previews=previews, budget_path_for=worker.budget_path,
                                                     event=event, coordinator_id="planner", binding_revision=binding).service_binding

                operation = ManagedOperation(lambda context, message: WorkflowInput("Wait", None),
                                             on_response=lambda context, original, response: response)
                service, assignments, _ = _service_fixture(tmp_path, approved_delivery, operation=operation, coordinator=coordinator())
                first = await service.consume(preview_id)
                assert first.state == "waiting"
                payload = json.loads(json.dumps(previews.get(preview_id).bundle_payload))
                payload["task_id"] = "second-coordinated-task"
                second = previews.create_or_get(dedupe_key="second", bundle_payload=payload, source_payload={})
                second = previews.approve(second.preview_id, base_revision=revision)
                with pytest.raises(ValueError, match="capacity"):
                    await service.consume(second.preview_id)
                assert assignments.for_task("second-coordinated-task") is None
                assert len(requests) == 2
                from hashlib import sha256

                receipt_path = tmp_path / "coordinator" / "receipts" / (sha256(second.preview_id.encode()).hexdigest() + ".json")
                saved = json.loads(receipt_path.read_text())
                assert saved["state"] == "proposed" and saved["pins"]["coordinator_id"] == "planner"
                budget_path = worker.budget_path(second.preview_id)
                initial = json.loads(budget_path.read_text())
                activation = None
                if scenario == "interrupted":
                    receipt_path.write_text(json.dumps(saved | {"state": "running", "proposal": None}))
                elif scenario == "corrupt":
                    receipt_path.write_text("{}")
                elif scenario == "symlink":
                    outside = tmp_path / "outside-receipt.json"
                    outside.write_text(receipt_path.read_text())
                    receipt_path.unlink()
                    receipt_path.symlink_to(outside)
                elif scenario == "deadline":
                    initial = initial | {"deadline": initial["deadline"] + 60}
                    budget_path.write_text(json.dumps(initial))
                elif scenario == "missing_budget":
                    budget_path.unlink()
                elif scenario == "revision":
                    original = service._routes[0]
                    snapshot = service._definitions.get(original.organization_id, original.revision)
                    document = json.loads(snapshot.definition.model_dump_json())
                    document["agents"][0]["instructions"] += " Changed binding."
                    changed = service._definitions.save(parse_organization_definition(json.dumps(document)))
                    activation = original.model_copy(update={"revision": changed.revision})
                await service.cancel(first.assignment_id)
                restarted, _, _ = _service_fixture(tmp_path, approved_delivery, operation=operation,
                                                    coordinator=coordinator("pm-v2" if scenario == "binding" else "pm-v1",
                                                                            "other.event" if scenario == "event" else "github.issue.ready"))
                if scenario == "capacity":
                    admitted = await restarted.consume(second.preview_id)
                    assert admitted.state == "waiting"
                    assert assignments.get(admitted.assignment_id).actor_id == "planner"
                    assert json.loads(budget_path.read_text()) == initial
                    assert json.loads(receipt_path.read_text()) == saved
                else:
                    with pytest.raises((ValueError, PermissionError, FileNotFoundError)):
                        await restarted.consume(second.preview_id, activation=activation)
                    assert assignments.for_task("second-coordinated-task") is None
                    if scenario == "missing_budget":
                        assert not budget_path.exists()
                    else:
                        assert json.loads(budget_path.read_text()) == initial | ({} if scenario == "event" else {"aborted": True})
                    if scenario == "interrupted":
                        assert json.loads(receipt_path.read_text())["state"] == "failed"
                assert len(requests) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("scenario", ["cancel", "expired", "runtime"])
def test_native_coordinator_lifecycle_failure_never_claims_or_replays(tmp_path, approved_delivery, monkeypatch, scenario):
    from aitobuild.organization_roles import NativeCoordinatorProposal
    from test_organization_runner import _service_fixture

    previews, worker, preview_id, _, _ = approved_delivery
    worker.prepare(preview_id)
    before = json.loads(worker.budget_path(preview_id).read_text())
    requests = []

    async def exercise():
        started = asyncio.Event()

        async def reply(request):
            requests.append(json.loads(request.content))
            if scenario == "cancel":
                started.set()
                await asyncio.Event().wait()
            if scenario == "expired":
                monkeypatch.setattr("aitobuild.developer_isolation.time", lambda: before["deadline"] + 1)
            return httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 1, "model": "test-model",
                                            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                                "role": "assistant", "content": json.dumps({"agent_id": "developer_one", "rationale": "Fits scope"}),
                                            }}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
            async with AsyncOpenAI(api_key="test-key", http_client=transport, max_retries=0) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))

                def runtime_for(snapshot):
                    runtime = bootstrap_organization(snapshot, model_profiles={"default": profile}, default_model_profile="default",
                                                     state_dir=tmp_path / "coordinator-agents")
                    return replace(runtime, modes=runtime.modes | {"planner": "mock"}) if scenario == "runtime" else runtime

                coordinator = NativeCoordinatorProposal(runtime_for=runtime_for, state_dir=tmp_path / "coordinator",
                                                        previews=previews, budget_path_for=worker.budget_path,
                                                        event="github.issue.ready", coordinator_id="planner", binding_revision="pm-v1")
                service, assignments, _ = _service_fixture(tmp_path, approved_delivery, coordinator=coordinator.service_binding)
                if scenario == "cancel":
                    invocation = asyncio.create_task(service.consume(preview_id))
                    await started.wait()
                    invocation.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await invocation
                else:
                    with pytest.raises((PermissionError, TimeoutError)):
                        await service.consume(preview_id)
                assert assignments.for_task(previews.get(preview_id).bundle_payload["task_id"]) is None
                assert json.loads(worker.budget_path(preview_id).read_text()) == before | {"aborted": True}
                if scenario != "runtime":
                    receipt = json.loads(next((tmp_path / "coordinator" / "receipts").glob("*.json")).read_text())
                    assert receipt["state"] == "failed"
                with pytest.raises((ValueError, PermissionError, TimeoutError)):
                    await service.consume(preview_id)

    asyncio.run(exercise())
    assert len(requests) == (0 if scenario == "runtime" else 1)


@pytest.fixture
def published_delivery(implemented_delivery, verification_adapter, monkeypatch):
    worker, published_id, developer_budget, _ = implemented_delivery
    monkeypatch.setattr("aitobuild.developer_delivery.shell_request", lambda *args, **kwargs: {
        "ok": True, "status": "exited", "exit_code": 0, "output": "passed", "next_cursor": 1,
    })
    worker.verify(published_id, adapter=verification_adapter)
    github = MockGitHubAdapter(allowed_repositories=frozenset({"fixture/widgets"}), enforce_allowlist=True)
    publication = worker.publish(published_id, github=github, require_human_approval_for_repo_writes=True, allow_mock_publication=True)
    return worker, published_id, developer_budget, github, publication


def review_definition(tmp_path):
    document = json.loads((Path(__file__).resolve().parents[1] / "config/organization.example.json").read_text())
    document["agents"].append({"id": "reviewer", "role": "architect", "instructions": "Review the pinned publication."})
    document["teams"][0]["members"].append("reviewer")
    document["workflows"].append({"id": "review", "document": {
        "format": "python_graph", "start": "review", "nodes": [{"id": "review", "kind": "operation", "operation": "architect_review_published"}],
        "outputs": ["review"],
    }})
    document["routes"].append({"id": "review", "events": ["published.review"], "team": "product", "workflow": "review",
                               "delegation": {"strategy": "rules", "eligible_agents": ["reviewer"], "target_agent": "reviewer"}})
    document["routes"].append({"id": "correction", "events": ["published.correction"], "team": "product", "workflow": document["workflows"][0]["id"],
                               "delegation": {"strategy": "rules", "eligible_agents": ["developer_one"], "target_agent": "developer_one"}})
    definitions = FileDefinitionStore(tmp_path / "review-definitions")
    return definitions, definitions.save(parse_organization_definition(json.dumps(document)))


def test_publication_stages_unapproved_head_pinned_review_once(tmp_path, published_delivery):
    from aitobuild.organization_reviews import PublishedReviewAdmission, PublishedReviewRoute

    worker, published_id, developer_budget, github, publication = published_delivery
    definitions, snapshot = review_definition(tmp_path)
    previews = DeveloperPreviewRegistry(tmp_path / "review-previews.json")
    reviews = PublishedReviewAdmission(
        definitions=definitions, previews=previews, worker=worker, github=github, state_dir=tmp_path / "review-admission",
        routes=(PublishedReviewRoute(repository="fixture/widgets", repository_id=101, organization_id=snapshot.organization_id,
                                     revision=snapshot.revision, event="published.review"),),
    )
    before = developer_budget.read_bytes()
    staged = reviews.offer(published_id)
    assert staged is not None and not staged.approved
    assert staged.bundle_payload["task_id"] != publication.task_id
    assert not reviews.budget_path(staged.preview_id).exists()
    assert reviews.offer(published_id) == staged
    assert len(previews.list_previews(pending_only=False, limit=100)) == 1
    assert reviews.route_for(staged.preview_id).event == "published.review"
    assert reviews.target_for(staged.preview_id).head_sha == publication.publication["head_sha"]
    number = publication.publication["pull_number"]
    github.pull_requests["fixture/widgets"][number] = replace(github.pull_requests["fixture/widgets"][number], head_sha="f" * 40)
    with pytest.raises((ValueError, PermissionError)):
        reviews.offer(published_id)
    assert developer_budget.read_bytes() == before


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("outcome", ["approve", "cancel_corrupt_preview", "correction", "correction_missing_seed",
                                     "correction_partial", "correction_scope", "correction_journal_loss", "correction_head_drift",
                                     "correction_revision", "correction_missing_budget", "correction_sibling"])
def test_staged_native_review_routes_only_after_fresh_task_approval(tmp_path, published_delivery, detached, outcome, monkeypatch, test_config):
    from aitobuild.organization_reviews import PublishedReviewAdmission, PublishedReviewRoute
    from aitobuild.organization_service import ManagedOrganizationService
    from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker

    worker, published_id, developer_budget, github, _ = published_delivery
    definitions, snapshot = review_definition(tmp_path)
    correcting = outcome.startswith("correction")
    previews = worker._previews if correcting else DeveloperPreviewRegistry(tmp_path / "review-previews.json")
    if correcting and outcome != "correction_missing_seed":
        published = worker.get(published_id)

        def git(directory, *arguments):
            return subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True).stdout.strip()

        checkout = Path(published.checkout_path)
        git(checkout, "add", "--", *published.publication["changed_paths"])
        git(checkout, "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Disposable reviewed head")
        head = git(checkout, "rev-parse", "HEAD")
        old_head = published.publication["head_sha"]
        worker._save(checkout.parent, replace(published, head_revision=head, publication=published.publication | {"head_sha": head}))
        number, repository = published.publication["pull_number"], published.publication["repository"]
        github.pull_requests[repository][number] = replace(github.pull_requests[repository][number], head_sha=head)
        github.commit_files[(repository, head)] = github.commit_files[(repository, old_head)]
        git(Path(published.source_path), "fetch", "--no-tags", "--no-write-fetch-head", str(checkout), head)
        git(Path(published.source_path), "update-ref", "refs/heads/" + published.branch, head)
    reviews = PublishedReviewAdmission(
        definitions=definitions, previews=previews, worker=worker, github=github, state_dir=tmp_path / "review-admission",
        routes=(PublishedReviewRoute(repository="fixture/widgets", repository_id=101, organization_id=snapshot.organization_id,
                                     revision=snapshot.revision, event="published.review"),),
        correction_routes=(PublishedReviewRoute(repository="fixture/widgets", repository_id=101, organization_id=snapshot.organization_id,
                                                revision=snapshot.revision, event="published.correction"),) if correcting else (),
    )
    before_developer = developer_budget.read_bytes()
    bodies = []

    def reply(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 3 and correcting:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "correction", "type": "function", "function": {
                "name": "architect_propose_correction", "arguments": json.dumps({"paths": worker.get(published_id).publication["changed_paths"],
                                                                                "objective": "Fix the inspected edge case"}),
            }}]}
            finish = "tool_calls"
        elif len(bodies) <= 2:
            name = "architect_read_published_source" if len(bodies) == 1 else "architect_read_published_diff"
            path = worker.get(published_id).publication["changed_paths"][0]
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"read-{len(bodies)}", "type": "function", "function": {
                "name": name, "arguments": json.dumps({"path": path}),
            }}]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Source and pinned-base diff inspected; human merge required."}
            finish = "stop"
        return httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 1, "model": "test-model",
                                        "choices": [{"index": 0, "message": message, "finish_reason": finish}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
            async with AsyncOpenAI(api_key="test-key", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))

                def runtime_for(pinned):
                    return bootstrap_organization(pinned, model_profiles={"default": profile}, default_model_profile="default",
                                                  state_dir=tmp_path / "review-agents")

                def service():
                    roles = NativeManagedRoles(runtime_for=runtime_for, state_dir=tmp_path / "review-roles", proposal_event="github.issue.ready",
                                               worker=worker, github=github, review_target_for=lambda context: reviews.target_for(context.assignment.preview_id),
                                               allow_correction_proposals=correcting)
                    operations = dict(roles.operations)
                    from aitobuild.organization_runner import ManagedOperation

                    operations["definition_probe"] = ManagedOperation(lambda task, message: {"prepared_correction": task.assignment.preview_id})
                    return ManagedOrganizationService(
                        definitions=definitions, assignments=FileAssignmentStore(tmp_path / "review-assignments.json"), runs=FileRunStore(tmp_path / "review-runs"),
                        previews=previews, worker=worker, routes=(), reviews=reviews, operations=operations, cleanup=roles.cleanup,
                        operator_id="trusted-review-operator", binding_revision="reviews-v1",
                    )

                current = service()
                staged = await current.offer_published_review(published_id)
                assert await current.consume(staged.preview_id) is None and not bodies
                preview = previews.approve(staged.preview_id)
                if detached:
                    background = ManagedOrganizationWorker(service=current, store=FileWorkerStore(tmp_path / "review-worker.json"))
                    assert background.enqueue(preview.preview_id).state == "queued"
                    await background.start()
                    try:
                        assert (await background.wait_idle(preview.preview_id)).state == "waiting"
                    finally:
                        await background.close()
                    waiting = current.status(preview.preview_id)
                else:
                    waiting = await current.consume(preview.preview_id)
                assert waiting.state == "waiting", waiting.error
                assert waiting.pending[0].kind == "service_approval" and not github.reviews
                assert worker.get(preview.preview_id) is None
                budget_before = reviews.budget_path(preview.preview_id).read_bytes()
                assert preview.bundle_payload["policy"]["max_file_changes"] == 0
                if outcome == "cancel_corrupt_preview":
                    def corrupt(preview_id):
                        raise ValueError("mutable preview is corrupt")

                    monkeypatch.setattr(previews, "get", corrupt)
                    cancelled = await service().cancel(waiting.assignment_id)
                    assert cancelled.state == "cancelled" and not github.reviews and len(bodies) == 3
                    ledger = json.loads(reviews.budget_path(preview.preview_id).read_bytes())
                    assert ledger["aborted"] and ledger["deadline"] == json.loads(budget_before)["deadline"]
                    assert developer_budget.read_bytes() == before_developer
                    return
                done = await service().decide(waiting.assignment_id, request_id=waiting.pending[0].request_id, approved=True)
                assert done.state == "completed", done.error
                assert len(github.reviews) == 1 and len(bodies) == (4 if correcting else 3)
                assert await service().consume(preview.preview_id) == done
                assert reviews.budget_path(preview.preview_id).read_bytes() == budget_before
                assert developer_budget.read_bytes() == before_developer
                if correcting:
                    with pytest.raises(PermissionError):
                        reviews.offer_correction(preview.preview_id, waiting)
                    if outcome == "correction_partial":
                        save = reviews._save

                        def interrupt_staging(journal):
                            if any(receipt.state == "staged" for receipt in journal.corrections.values()):
                                raise OSError("correction staging interrupted")
                            save(journal)

                        monkeypatch.setattr(reviews, "_save", interrupt_staging)
                        with pytest.raises(OSError):
                            await service().offer_correction(preview.preview_id)
                        partial = previews.list_previews(pending_only=True, limit=10)[0]
                        with pytest.raises(PermissionError):
                            reviews.correction_route_for(partial.preview_id)
                        monkeypatch.setattr(reviews, "_save", save)
                    if outcome == "correction_sibling":
                        from aitobuild.organization_reviews import _CorrectionReceipt
                        from filelock import FileLock

                        reviewed = reviews.target_for(preview.preview_id)
                        with FileLock(str(reviews._path) + ".lock", timeout=10):
                            journal = reviews._load()
                            journal.corrections["published-correction-sibling"] = _CorrectionReceipt(
                                task_id="published-correction-sibling", review_preview_id="published-review-other",
                                target=reviewed, route=reviews.correction_routes[0], run_content="{}", bundle_content=json.dumps({"task_id": "published-correction-sibling"}),
                                state="staged", correction_preview_id="sibling-correction")
                            reviews._save(journal)
                        pending_before = previews.list_previews(pending_only=True, limit=10)
                        with pytest.raises(PermissionError, match="offer corrections only from the chain tip"):
                            await service().offer_correction(preview.preview_id)
                        assert previews.list_previews(pending_only=True, limit=10) == pending_before
                        assert [receipt.task_id for receipt in reviews._load().corrections.values()] == ["published-correction-sibling"]
                        assert developer_budget.read_bytes() == before_developer
                        return
                    correction = await service().offer_correction(preview.preview_id)
                    assert correction is not None and not correction.approved
                    assert correction.bundle_payload["issue_context"]["base_revision"] == worker.get(published_id).publication["head_sha"]
                    assert correction.bundle_payload["issue_context"]["base_branch"] == worker.get(published_id).branch
                    assert correction.bundle_payload["policy"]["max_file_changes"] == len(done.outputs[0]["correction"]["paths"])
                    assert not worker.budget_path(correction.preview_id).exists() and worker.get(correction.preview_id) is None
                    assert await service().consume(correction.preview_id) is None
                    assert await service().offer_correction(preview.preview_id) == correction
                    assert reviews.correction_route_for(correction.preview_id).event == "published.correction"
                    if outcome == "correction_partial":
                        assert correction.preview_id == partial.preview_id
                    if outcome == "correction_journal_loss":
                        (tmp_path / "review-admission" / "reviews.json").unlink()
                        with pytest.raises(PermissionError):
                            service().admission(correction.preview_id)
                        assert worker.get(correction.preview_id) is None and not worker.budget_path(correction.preview_id).exists()
                        assert developer_budget.read_bytes() == before_developer
                        return
                    if outcome == "correction_scope":
                        get = previews.get

                        def altered_scope(preview_id):
                            current = get(preview_id)
                            return replace(current, bundle_payload=current.bundle_payload | {"objective": "unapproved"}) if preview_id == correction.preview_id else current

                        monkeypatch.setattr(previews, "get", altered_scope)
                        with pytest.raises(PermissionError):
                            service().admission(correction.preview_id)
                        assert worker.get(correction.preview_id) is None and developer_budget.read_bytes() == before_developer
                        return
                    if outcome == "correction_head_drift":
                        current = github.pull_requests[repository][number]
                        github.pull_requests[repository][number] = replace(current, head_sha="f" * 40)
                        with pytest.raises(PermissionError):
                            service().admission(correction.preview_id)
                        assert worker.get(correction.preview_id) is None and developer_budget.read_bytes() == before_developer
                        return
                    if outcome == "correction_revision":
                        document = snapshot.definition.model_dump(mode="json")
                        document["agents"][0]["instructions"] += " revised"
                        changed = definitions.save(parse_organization_definition(json.dumps(document)))
                        newer = PublishedReviewAdmission(definitions=definitions, previews=previews, worker=worker, github=github,
                                                        state_dir=tmp_path / "review-admission", routes=reviews.routes,
                                                        correction_routes=tuple(route.model_copy(update={"revision": changed.revision}) for route in reviews.correction_routes))
                        assert newer.offer_correction(preview.preview_id, done).preview_id == correction.preview_id
                        assert newer.correction_route_for(correction.preview_id).revision == snapshot.revision
                    from fastapi.testclient import TestClient
                    from aitobuild.app import create_app

                    monkeypatch.setattr("aitobuild.app.DeveloperPreviewRegistry", lambda *args, **kwargs: previews)
                    monkeypatch.setattr("aitobuild.app.DeveloperDeliveryWorker", lambda **kwargs: worker)
                    with TestClient(create_app(test_config, managed_service_factory=lambda context: service())) as client:
                        headers = {"X-Internal-Token": test_config.security.internal_api_token}
                        assert client.post("/internal/organization/corrections/offer", json={"review_preview_id": preview.preview_id}).status_code == 401
                        assert client.post("/internal/organization/corrections/offer", headers=headers,
                                           json={"review_preview_id": preview.preview_id, "actor_id": "model"}).status_code == 400
                        offered = client.post("/internal/organization/corrections/offer", headers=headers, json={"review_preview_id": preview.preview_id})
                        assert offered.status_code == 200 and offered.json()["correction_preview"]["preview_id"] == correction.preview_id
                        assert not offered.json()["correction_preview"]["approved"]
                        operator_calls = (
                            ("/internal/organization/corrections/stage-publication", {"correction_preview_id": correction.preview_id}),
                            ("/internal/organization/corrections/publish", {"correction_preview_id": correction.preview_id, "approval_digest": "0" * 64}),
                            ("/internal/organization/corrections/retire", {"correction_preview_id": correction.preview_id}),
                        )
                        for path, body in operator_calls:
                            assert client.post(path, json=body).status_code == 401
                            assert client.post(path, headers={"X-Internal-Token": "wrong-token"}, json=body).status_code == 401
                            assert client.post(path, headers=headers, json=body | {"actor_id": "model"}).status_code == 400
                        staged_publication = client.post(operator_calls[0][0], headers=headers, json=operator_calls[0][1])
                        assert staged_publication.status_code == 409
                        refused_publish = client.post(operator_calls[1][0], headers=headers, json=operator_calls[1][1])
                        assert refused_publish.status_code == 409 and "mock publication is refused" in refused_publish.json()["detail"]
                        refused_retire = client.post(operator_calls[2][0], headers=headers, json=operator_calls[2][1])
                        assert refused_retire.status_code == 409 and "mock reads are refused" in refused_retire.json()["detail"]
                        assert worker._approvals.get(correction.preview_id) is None
                    previews.approve(correction.preview_id)
                    if outcome == "correction_missing_seed":
                        with pytest.raises(ValueError, match="not prepared"):
                            await service().consume(correction.preview_id)
                        assert worker.get(correction.preview_id).state == "failed"
                    else:
                        corrected = await service().consume(correction.preview_id)
                        assert corrected.state == "completed", corrected.error
                        prepared = worker.get(correction.preview_id)
                        assert prepared.base_revision == worker.get(published_id).publication["head_sha"]
                        assert prepared.branch != worker.get(published_id).branch
                        assert prepared.checkout_path != worker.get(published_id).checkout_path
                        bundle = developer_task_bundle_from_payload(prepared.bundle_payload)
                        budget = DeveloperTaskBudget(path=worker.budget_path(correction.preview_id), bundle=bundle, create=False)
                        from aitobuild.developer_isolation import is_path_allowed

                        assert all(is_path_allowed(path, policy=bundle.policy) for path in done.outputs[0]["correction"]["paths"])
                        assert not is_path_allowed("outside.py", policy=bundle.policy)
                        with pytest.raises(PermissionError):
                            budget.reserve_paths(tuple(done.outputs[0]["correction"]["paths"]) + ("outside.py",))
                        pulls_before = {repo: dict(prs) for repo, prs in github.pull_requests.items()}
                        with pytest.raises(PermissionError, match="cannot publish a new pull request"):
                            worker.publish(correction.preview_id, github=github,
                                           require_human_approval_for_repo_writes=True, allow_mock_publication=True)
                        assert {repo: dict(prs) for repo, prs in github.pull_requests.items()} == pulls_before
                        assert worker.get(correction.preview_id).state == prepared.state
                        assert json.loads(budget.path.read_text())["reserved_paths"] == []
                        if outcome == "correction_missing_budget":
                            budget.path.unlink()
                            assert worker.prepare(correction.preview_id).state == "failed"
                            assert not budget.path.exists()
                    assert reviews.budget_path(preview.preview_id).read_bytes() == budget_before
                    assert developer_budget.read_bytes() == before_developer

    asyncio.run(exercise())


@pytest.mark.parametrize("scenario", ["partial", "missing_journal", "corrupt_journal", "symlink_journal", "changed_scope",
                                      "missing_budget", "interrupted_initialization", "deadline_drift", "expired", "aborted", "revision"])
def test_staged_review_recovery_retains_scope_and_original_ledger(tmp_path, published_delivery, monkeypatch, scenario):
    from aitobuild.organization_reviews import PublishedReviewAdmission, PublishedReviewRoute

    worker, published_id, developer_budget, github, _ = published_delivery
    definitions, snapshot = review_definition(tmp_path)
    previews = DeveloperPreviewRegistry(tmp_path / "review-previews.json")
    route = PublishedReviewRoute(repository="fixture/widgets", repository_id=101, organization_id=snapshot.organization_id,
                                 revision=snapshot.revision, event="published.review")
    before = developer_budget.read_bytes()

    def admission(activation=route):
        return PublishedReviewAdmission(definitions=definitions, previews=previews, worker=worker, github=github,
                                        state_dir=tmp_path / "review-admission", routes=(activation,))

    reviews = admission()
    if scenario == "partial":
        save = reviews._save

        def fail_staged(journal):
            if any(receipt.state == "staged" for receipt in journal.receipts.values()):
                raise OSError("staging persistence interrupted")
            save(journal)

        monkeypatch.setattr(reviews, "_save", fail_staged)
        with pytest.raises(OSError):
            reviews.offer(published_id)
        staged = previews.list_previews(pending_only=False, limit=10)[0]
        with pytest.raises(PermissionError):
            reviews.route_for(staged.preview_id)
        recovered = admission().offer(published_id)
        assert recovered.preview_id == staged.preview_id and not recovered.approved
        assert not reviews.budget_path(staged.preview_id).exists()
    else:
        staged = reviews.offer(published_id)
        if scenario == "revision":
            document = snapshot.definition.model_dump(mode="json")
            document["agents"][0]["instructions"] += " updated"
            newer = definitions.save(parse_organization_definition(json.dumps(document)))
            recovered = admission(route.model_copy(update={"revision": newer.revision})).offer(published_id)
            assert recovered.preview_id == staged.preview_id
            assert admission().route_for(staged.preview_id).revision == snapshot.revision
        elif scenario in {"missing_journal", "corrupt_journal", "symlink_journal", "changed_scope"}:
            journal = tmp_path / "review-admission" / "reviews.json"
            if scenario == "missing_journal":
                journal.unlink()
            elif scenario == "corrupt_journal":
                journal.write_text("{", encoding="utf-8")
            elif scenario == "symlink_journal":
                moved = journal.with_suffix(".saved")
                journal.rename(moved)
                journal.symlink_to(moved)
            else:
                payload = json.loads(journal.read_text())
                receipt = payload["receipts"][staged.bundle_payload["task_id"]]
                bundle = json.loads(receipt["bundle_content"])
                bundle["objective"] = "altered scope"
                receipt["bundle_content"] = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
                journal.write_text(json.dumps(payload), encoding="utf-8")
            with pytest.raises((ValueError, PermissionError)):
                admission().route_for(staged.preview_id)
            assert not reviews.budget_path(staged.preview_id).exists()
        else:
            previews.approve(staged.preview_id)
            path = reviews.budget_path(staged.preview_id)
            if scenario == "interrupted_initialization":
                monkeypatch.setattr("aitobuild.organization_reviews.DeveloperTaskBudget", lambda **kwargs: (_ for _ in ()).throw(OSError("interrupted")))
                with pytest.raises(OSError):
                    reviews.prepare(staged.preview_id)
                monkeypatch.undo()
            else:
                reviews.prepare(staged.preview_id)
            if scenario == "missing_budget":
                path.unlink()
            elif scenario in {"deadline_drift", "expired", "aborted"}:
                payload = json.loads(path.read_text())
                if scenario == "aborted":
                    payload["aborted"] = True
                else:
                    payload["deadline"] = payload["deadline"] + 1000 if scenario == "deadline_drift" else 1.0
                path.write_text(json.dumps(payload), encoding="utf-8")
            with pytest.raises((ValueError, PermissionError, TimeoutError)):
                admission().prepare(staged.preview_id)
            if scenario == "deadline_drift":
                with pytest.raises(PermissionError, match="deadline"):
                    admission().original_budget_path(staged.preview_id)
            if scenario in {"missing_budget", "interrupted_initialization"}:
                assert not path.exists()
    assert developer_budget.read_bytes() == before


@pytest.mark.parametrize("staging_failure", [False, True])
def test_published_review_http_hook_and_metadata_only_retry(tmp_path, published_delivery, test_config, monkeypatch, staging_failure):
    from fastapi.testclient import TestClient
    from aitobuild.app import create_app
    from aitobuild.organization_reviews import PublishedReviewAdmission, PublishedReviewRoute
    from aitobuild.organization_runner import ManagedOperation
    from aitobuild.organization_service import ManagedOrganizationService
    from aitobuild.policy import AgentRole

    worker, published_id, developer_budget, github, published = published_delivery
    definitions, snapshot = review_definition(tmp_path)
    before = developer_budget.read_bytes()
    calls = []

    class PublishedGitHub:
        def __getattr__(self, name):
            return getattr(github, name)

    def publish(*args, **kwargs):
        calls.append("publish")
        return published

    monkeypatch.setattr(worker, "publish", publish)
    monkeypatch.setattr("aitobuild.app.DeveloperDeliveryWorker", lambda **kwargs: worker)
    monkeypatch.setattr("aitobuild.app.build_github_adapter", lambda **kwargs: PublishedGitHub())

    def factory(context):
        async def cleanup(task):
            calls.append("cleanup")

        async def review(task, message):
            calls.append("review")
            return "metadata probe; not semantic review"

        reviews = PublishedReviewAdmission(definitions=definitions, previews=context.previews, worker=context.worker,
                                          github=github, state_dir=tmp_path / "http-review-admission",
                                          routes=(PublishedReviewRoute(repository="fixture/widgets", repository_id=101,
                                                                       organization_id=snapshot.organization_id, revision=snapshot.revision,
                                                                       event="published.review"),))
        service = ManagedOrganizationService(
            definitions=definitions, assignments=FileAssignmentStore(tmp_path / "http-review-assignments.json"),
            runs=FileRunStore(tmp_path / "http-review-runs"), previews=context.previews, worker=context.worker, routes=(), reviews=reviews,
            operator_id="review-operator", binding_revision="http-review-v1",
            operations={"architect_review_published": ManagedOperation(review, role=AgentRole.ARCHITECT)},
            cleanup=cleanup,
        )
        if staging_failure:
            monkeypatch.setattr(reviews, "offer", lambda preview_id: (_ for _ in ()).throw(OSError("staging interrupted")))
        return service

    with TestClient(create_app(test_config, managed_service_factory=factory)) as client:
        headers = {"X-Internal-Token": test_config.security.internal_api_token}
        response = client.post("/internal/developer/delivery/publish", headers=headers, json={"preview_id": published_id})
        assert response.status_code == 200 and response.json()["accepted"]
        if staging_failure:
            assert response.json()["review_staging_error"] == "staging interrupted"
            monkeypatch.undo()
        else:
            assert not response.json()["review_preview"]["approved"]
        assert client.post("/internal/organization/reviews/offer", json={"published_preview_id": published_id}).status_code == 401
        assert client.post("/internal/organization/reviews/offer", headers=headers,
                           json={"published_preview_id": published_id, "head_sha": "f" * 40}).status_code == 400
        offered = client.post("/internal/organization/reviews/offer", headers=headers, json={"published_preview_id": published_id})
        assert offered.status_code == 200 and not offered.json()["review_preview"]["approved"]
        duplicate = client.post("/internal/organization/reviews/offer", headers=headers, json={"published_preview_id": published_id})
        assert duplicate.json() == offered.json()
        assert calls == ["publish"] and developer_budget.read_bytes() == before
        staged_id = offered.json()["review_preview"]["preview_id"]
        approved = client.post("/internal/developer/preview/approve", headers=headers, json={"preview_id": staged_id})
        assert approved.status_code == 200 and approved.json()["managed_run"]["state"] == "completed"
        assert calls == ["publish", "review", "cleanup"] and worker.get(staged_id) is None
        assert developer_budget.read_bytes() == before


@pytest.mark.parametrize("scenario", ["approved", "rejected", "metadata_only", "head_drift", "recovered", "publication_race",
                                      "partial_page", "skip_page", "oversized_output", "body_drift", "evidence_drift", "uncertain_submit", "expired_at_write",
                                      "correction", "correction_outside", "correction_partial", "correction_drift"])
def test_native_architect_review_requires_inspection_and_exact_saved_approval(
    tmp_path, published_delivery, monkeypatch, scenario,
):
    worker, published_id, developer_budget, github, publication = published_delivery
    target = PublishedReviewTarget(preview_id=published_id, head_sha=publication.publication["head_sha"])
    before_developer = developer_budget.read_bytes()
    payload = json.loads(json.dumps(publication.bundle_payload))
    payload["task_id"] = "fresh-architect-review"
    payload["objective"] = "Inspect this published head and propose a COMMENT for human approval"
    previews = DeveloperPreviewRegistry(tmp_path / "review-previews.json")
    preview = previews.create_or_get(dedupe_key="review", bundle_payload=payload, source_payload={})
    preview = previews.approve(preview.preview_id, base_revision=publication.base_revision)
    def budget_path_for(preview_id):
        return tmp_path / "review-budgets" / (preview_id + ".json")
    DeveloperTaskBudget(path=budget_path_for(preview.preview_id), bundle=developer_task_bundle_from_payload(preview.bundle_payload))
    path = publication.publication["changed_paths"][0]
    correction_case = scenario.startswith("correction")
    if scenario == "oversized_output":
        source_reader = worker.get_published_source
        monkeypatch.setattr(worker, "get_published_source", lambda *args, **kwargs: source_reader(*args, **kwargs) | {
            "content": "x" * 7000, "total_bytes": 7000, "truncated": False, "next_offset": None,
        })
    bodies = []

    def reply(request):
        body = json.loads(request.content)
        bodies.append(body)
        step = len(bodies) - (1 if scenario == "recovered" else 0)
        if correction_case and step == 3:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "correction", "type": "function", "function": {
                "name": "architect_propose_correction", "arguments": json.dumps({"paths": ["outside.py" if scenario == "correction_outside" else path],
                                                                                "objective": "Correct the inspected edge case and retain passing tests"}),
            }}]}
            finish = "tool_calls"
        elif scenario == "metadata_only" or step >= 3:
            message = {"role": "assistant", "content": "  Scoped source/diff inspected; retain human merge authority.  "}
            finish = "stop"
        else:
            name = "architect_read_published_source" if step <= 1 else "architect_read_published_diff"
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"read-{len(bodies)}", "type": "function", "function": {
                "name": name, "arguments": json.dumps({"path": "outside.py" if step == 0 else path} |
                    ({"max_bytes": 1} if scenario in {"partial_page", "correction_partial"} else {}) |
                    ({"offset": 5} if scenario == "skip_page" and step == 1 else {})),
            }}]}
            finish = "tool_calls"
        return httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 1, "model": "test-model",
                                        "choices": [{"index": 0, "message": message, "finish_reason": finish}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
            async with AsyncOpenAI(api_key="test-key", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                assignment, runtime_for, make_runner, _ = role_fixture(
                    tmp_path, profile, role="architect", preview=preview, previews=previews, budget_path_for=budget_path_for,
                )

                def adapter():
                    return NativeManagedRoles(runtime_for=runtime_for, state_dir=tmp_path / "roles", proposal_event="github.issue.ready",
                                              worker=worker, github=github, review_target_for=lambda context: target,
                                              allow_correction_proposals=correction_case)

                before = json.loads(Path(assignment.budget_path).read_text())
                waiting = await make_runner(adapter()).start(assignment.assignment_id)
                if scenario in {"metadata_only", "partial_page", "skip_page", "oversized_output", "correction_partial"}:
                    assert waiting.state == "failed" and "complete source and diff" in waiting.error
                else:
                    assert waiting.state == "waiting", waiting.error
                    assert waiting.pending[0].kind == "service_approval"
                    assert github.reviews == []
                    if scenario == "correction":
                        assert waiting.pending[0].data["correction"] == {"paths": [path], "objective": "Correct the inspected edge case and retain passing tests"}
                    if scenario == "correction_outside":
                        assert "correction" not in waiting.pending[0].data
                    if scenario in {"body_drift", "evidence_drift", "uncertain_submit", "correction_drift"}:
                        roles = adapter()
                        saved = await roles._sessions.get(waiting.session_id)
                        if scenario == "body_drift":
                            saved.state["aitobuild_comment_body"] = "different text"
                        elif scenario == "evidence_drift":
                            saved.state["aitobuild_review_inspections"] = {}
                        elif scenario == "correction_drift":
                            saved.state["aitobuild_correction_proposal"]["objective"] = "different unapproved objective"
                        else:
                            saved.state["aitobuild_comment_state"] = "submitting"
                        await roles._sessions.set(waiting.session_id, saved)
                    if scenario == "head_drift":
                        number = publication.publication["pull_number"]
                        github.pull_requests["fixture/widgets"][number] = replace(github.pull_requests["fixture/widgets"][number], head_sha="f" * 40)
                    if scenario == "publication_race":
                        original_submit = worker.submit_architect_review

                        def raced(*args, **kwargs):
                            current = worker.get(published_id)
                            worker._save(Path(current.checkout_path).parent, replace(
                                current, head_revision="f" * 40, publication=current.publication | {"head_sha": "f" * 40},
                            ))
                            return original_submit(*args, **kwargs)

                        monkeypatch.setattr(worker, "submit_architect_review", raced)
                    if scenario == "expired_at_write":
                        original_submit = worker.submit_architect_review
                        original_pull = github.get_pull_request

                        def expired(*args, **kwargs):
                            def expire_clock(**pull_kwargs):
                                pull = original_pull(**pull_kwargs)
                                monkeypatch.setattr("aitobuild.developer_isolation.time", lambda: before["deadline"] + 1)
                                return pull

                            monkeypatch.setattr(github, "get_pull_request", expire_clock)
                            return original_submit(*args, **kwargs)

                        monkeypatch.setattr(worker, "submit_architect_review", expired)
                    done = await make_runner(adapter()).approve(assignment.assignment_id, request_id=waiting.pending[0].request_id,
                                                               approved=scenario != "rejected")
                    if scenario in {"approved", "recovered", "correction", "correction_outside"}:
                        assert done.state == "completed", done.error
                        assert github.reviews[0].body == "aitobuild Architect review\n\nScoped source/diff inspected; retain human merge authority."
                        assert len(github.reviews) == 1
                        assert await make_runner(adapter()).start(assignment.assignment_id) == done
                        assert json.loads(Path(assignment.budget_path).read_text()) == before
                        if scenario == "correction":
                            assert done.outputs[0]["correction"] == waiting.pending[0].data["correction"]
                        if scenario == "correction_outside":
                            assert "correction" not in done.outputs[0]
                    else:
                        assert done.state == "failed" and not github.reviews
                        assert json.loads(Path(assignment.budget_path).read_text()) == before | {"aborted": True}
                assert developer_budget.read_bytes() == before_developer
                if scenario in {"recovered", "correction_outside"}:
                    session = await adapter()._sessions.get(waiting.session_id)
                    assert session.state["aitobuild_role_diagnostics"]

    asyncio.run(exercise())
    assert {entry["function"]["name"] for entry in bodies[0]["tools"]} == {
        "architect_read_published_source", "architect_read_published_diff",
    } | ({"architect_propose_correction"} if correction_case else set())


@pytest.mark.parametrize("scenario", ["immutable", "unicode", "deleted", "mode_only", "binary", "corrupt_blob",
                                      "head_drift", "repository_drift", "base_drift", "unsafe_path"])
def test_published_source_and_diff_use_immutable_pins(published_delivery, monkeypatch, scenario):
    worker, preview_id, budget_path, github, record = published_delivery
    before_budget = budget_path.read_bytes()
    publication = record.publication
    repo, head, base = (publication[key] for key in ("repository", "head_sha", "base_sha"))
    path = publication["changed_paths"][0]
    content = github.get_blob(repository=repo, blob_sha=publication["blob_shas"][path])
    old = b"previous content without newline"
    if scenario == "mode_only":
        old = content
    github.commit_files[(repo, base)] = {path: GitHubBlobChange("100644", old, _git_blob_sha(old))}
    if scenario in {"unicode", "binary", "deleted", "mode_only"}:
        content = {"unicode": "caf\u00e9\n".encode(), "binary": b"\xff", "deleted": b"", "mode_only": content}[scenario]
        change = None if scenario == "deleted" else GitHubBlobChange("100755" if scenario == "mode_only" else "100644", content, _git_blob_sha(content))
        pins = publication | {"blob_shas": {path: change.blob_sha if change else None},
                              "file_modes": {path: change.mode if change else None}}
        worker._save(Path(record.checkout_path).parent, replace(record, publication=pins))
        github.commit_files[(repo, head)] = {path: change} if change else {}
    if scenario == "immutable":
        (Path(record.checkout_path) / path).write_text("mutated local checkout\n")
    if scenario == "corrupt_blob":
        monkeypatch.setattr(github, "get_blob", lambda **kwargs: b"incorrect bytes")
    if scenario == "head_drift":
        original = github.get_blob

        def drift(**kwargs):
            result = original(**kwargs)
            number = publication["pull_number"]
            github.pull_requests[repo][number] = replace(github.pull_requests[repo][number], head_sha="f" * 40)
            return result

        monkeypatch.setattr(github, "get_blob", drift)
    if scenario in {"repository_drift", "base_drift"}:
        number = publication["pull_number"]
        github.pull_requests[repo][number] = replace(github.pull_requests[repo][number], **{
            "repository" if scenario == "repository_drift" else "base_ref": "other/target",
        })
    if scenario in {"binary", "corrupt_blob", "head_drift", "repository_drift", "base_drift", "unsafe_path"}:
        with pytest.raises((ValueError, PermissionError)):
            worker.get_published_source(preview_id, github=github, path="../outside.py" if scenario == "unsafe_path" else path)
    else:
        source = worker.get_published_source(preview_id, github=github, path=path)
        assert source["content"].encode() == content and source["head_sha"] == head
        diff = worker.get_published_diff(preview_id, github=github, path=path)
        assert diff["before_blob_sha"] == _git_blob_sha(old) and diff["head_sha"] == head
        if scenario == "unicode":
            page = worker.get_published_source(preview_id, github=github, path=path, max_bytes=4)
            assert page["content"] == "caf" and page["next_offset"] == 3
            assert worker.get_published_source(preview_id, github=github, path=path, offset=3)["content"] == "\u00e9\n"
            with pytest.raises(ValueError, match="UTF-8"):
                worker.get_published_source(preview_id, github=github, path=path, offset=4)
        elif scenario == "deleted":
            assert source["deleted"] and diff["after_blob_sha"] is None
            assert "-previous content" in diff["diff"]
        elif scenario == "mode_only":
            assert diff["diff"] == "" and diff["before_mode"] == "100644" and diff["after_mode"] == "100755"
        else:
            assert "-previous content" in diff["diff"] and "\\ No newline at end of file" in diff["diff"]
    assert budget_path.read_bytes() == before_budget