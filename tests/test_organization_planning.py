from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlsplit

from agent_framework.openai import OpenAIChatCompletionClient
import httpx
from openai import AsyncOpenAI

import pytest

from aitobuild.organization_planning import FilePlanStore, PlanProposal, PlanRevision, PlanningLimits
from aitobuild.developer_isolation import default_developer_isolation_policy, DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.developer_preview import DeveloperPreviewRegistry
from aitobuild.developer_delivery import DeliveryPreparation, DeveloperDeliveryWorker, LocalRepositorySource
from aitobuild.organization import FileDefinitionStore, parse_organization_definition
from aitobuild.organization_assignments import FileAssignmentStore, AssignmentProposal
from aitobuild.organization_planning import ManagedPlanning, PlanningAdmission, PlanningRoute, PlanningScope
from aitobuild.organization_dependencies import ManagedDependencies
from aitobuild.organization_roles import NativeManagedRoles
from aitobuild.organization_runner import FileRunStore, ManagedOperation
from aitobuild.organization_runtime import ModelProfile, bootstrap_organization
from aitobuild.organization_service import ManagedOrganizationService
from aitobuild.tools.github import GhCliGitHubAdapter, MockGitHubAdapter
from test_dispatcher import approved_delivery as approved_delivery
from test_dispatcher import verification_adapter as verification_adapter


@pytest.mark.parametrize("case", ["merged", "draft", "closed_unmerged", "head", "fork", "base", "branch_head", "missing_merge",
                                  "bot", "malformed", "unrelated", "behind"])
def test_dependency_merge_inspection_is_exact_and_read_only(monkeypatch, case):
    adapter = GhCliGitHubAdapter(allowed_repositories=frozenset({"example/target"}))
    repo = {"id": 1, "full_name": "example/target"}
    raw = {"number": 7, "state": "closed", "draft": False, "merged": True,
           "head": {"sha": "a" * 40, "ref": "aitobuild/issue-1", "repo": dict(repo)},
           "base": {"ref": "main", "repo": dict(repo)}, "merge_commit_sha": "c" * 40,
           "merged_at": "2026-10-09T12:00:00Z", "merged_by": {"id": 10, "login": "human", "type": "User"}}
    comparison = {"status": "ahead", "merge_base_commit": {"sha": "c" * 40}}
    branch_sha = "b" * 40
    if case in {"draft", "closed_unmerged"}:
        raw.update(merged=False, state="open" if case == "draft" else "closed", draft=case == "draft")
    elif case == "head":
        raw["head"]["sha"] = "d" * 40
    elif case == "fork":
        raw["head"]["repo"]["id"] = 2
    elif case == "base":
        raw["base"]["ref"] = "other"
    elif case == "branch_head":
        branch_sha = "d" * 40
    elif case == "missing_merge":
        raw["merge_commit_sha"] = None
    elif case == "bot":
        raw["merged_by"]["type"] = "Bot"
    elif case == "malformed":
        raw["merged"] = "true"
    elif case == "unrelated":
        comparison["merge_base_commit"]["sha"] = "d" * 40
    elif case == "behind":
        comparison["status"] = "behind"
    calls = []

    def api(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        if endpoint == "repos/example/target":
            return repo
        if "/commits/" in endpoint:
            return {"sha": "b" * 40}
        if "/pulls/" in endpoint:
            return raw
        if "/compare/" in endpoint:
            return comparison
        if "/git/ref/" in endpoint:
            return {"object": {"sha": branch_sha}}
        raise AssertionError(endpoint)

    monkeypatch.setattr(adapter, "_api", api)
    arguments = dict(repository="example/target", repository_id=1, pull_number=7, head_sha="a" * 40,
                     head_branch="aitobuild/issue-1", base_branch="main", base_revision="b" * 40)
    if case in {"merged", "draft", "closed_unmerged"}:
        evidence = adapter.inspect_dependency_merge(**arguments)
        assert evidence["ready"] is (case == "merged")
        if case == "merged":
            assert evidence["merge_commit_sha"] == "c" * 40 and evidence["merged_by"]["login"] == "human"
    else:
        with pytest.raises((ValueError, PermissionError)):
            adapter.inspect_dependency_merge(**arguments)
    assert all(not kwargs or kwargs.get("method", "GET") == "GET" for _, kwargs in calls)


def proposal():
    return {"issues": [
        {"key": "first", "title": "First issue", "objective": "Implement first behavior",
         "acceptance_criteria": ["First behavior passes"]},
        {"key": "second", "title": "Second issue", "objective": "Implement second behavior",
         "acceptance_criteria": ["Second behavior passes"], "dependencies": ["first"]},
    ]}


@pytest.mark.parametrize("case", ["unknown", "cycle", "duplicate", "blank", "newline", "actor", "count", "bytes"])
def test_plan_validation_is_bounded_and_has_no_authority(case):
    document = proposal()
    limits = PlanningLimits()
    if case == "unknown":
        document["issues"][1]["dependencies"] = ["missing"]
    elif case == "cycle":
        document["issues"][0]["dependencies"] = ["second"]
    elif case == "duplicate":
        document["issues"][1]["key"] = "first"
    elif case == "blank":
        document["issues"][0]["objective"] = " "
    elif case == "newline":
        document["issues"][0]["acceptance_criteria"] = ["criterion\n## Injected"]
    elif case == "actor":
        document["actor_id"] = "model"
    elif case == "count":
        limits = PlanningLimits(max_issues=1)
    elif case == "bytes":
        limits = PlanningLimits(max_plan_bytes=10)
    with pytest.raises(ValueError):
        PlanProposal.model_validate_json(json.dumps(document)).check_limits(limits)


def test_plan_revision_is_immutable_and_survives_restart(tmp_path):
    plan = PlanRevision(pins={"operator_id": "operator", "repository": "example/target"},
                        proposal=PlanProposal.model_validate_json(json.dumps(proposal())),
                        inspections={"request": "read"}, issues=())
    store = FilePlanStore(tmp_path)
    assert store.save("assignment", plan) == plan.digest
    assert FilePlanStore(tmp_path).get("assignment") == plan
    assert store.save("assignment", plan) == plan.digest
    with pytest.raises(PermissionError, match="immutable"):
        store.save("assignment", plan.model_copy(update={"inspections": {}}))


def planning_fixture(tmp_path, profile, worker, *, limits=PlanningLimits(), github=None, previews=None, dependencies_enabled=False,
                     repository="example/target", repository_id=1, base_revision="a" * 40, handoff_operation=None):
    document = json.loads((Path(__file__).resolve().parents[1] / "config/organization.example.json").read_text())
    document["workflows"].append({"id": "planning", "document": {
        "format": "python_graph", "start": "plan", "nodes": [{"id": "plan", "kind": "operation", "operation": "pm_plan_issues"}],
        "outputs": ["plan"],
    }})
    document["routes"].append({"id": "planning", "events": ["pm.request"], "team": "product", "workflow": "planning",
                               "delegation": {"strategy": "rules", "eligible_agents": ["planner"], "target_agent": "planner"}})
    if dependencies_enabled:
        document["routes"][0]["delegation"]["strategy"] = "human"
    definitions = FileDefinitionStore(tmp_path / "planning-definitions")
    snapshot = definitions.save(parse_organization_definition(json.dumps(document)))
    previews = previews or DeveloperPreviewRegistry(tmp_path / "planning-previews.json")
    assignments = FileAssignmentStore(tmp_path / "planning-assignments.json")
    runs = FileRunStore(tmp_path / "planning-runs")
    github = github or MockGitHubAdapter(repository_ids={repository: repository_id}, commit_files={(repository, base_revision): {}})
    route = PlanningRoute(repository=repository, repository_id=repository_id, organization_id=snapshot.organization_id,
                          revision=snapshot.revision, event="pm.request")
    policy = replace(default_developer_isolation_policy(), allowed_command_prefixes=("pytest",), max_runtime_minutes=5)

    def make_service():
        admission = PlanningAdmission(definitions=definitions, previews=previews, state_dir=tmp_path / "planning-admission",
                                      routes=(route,), operator_id="operator")
        planning = ManagedPlanning(admission=admission, github=github, state_dir=tmp_path / "planning-state")

        def runtime_for(pinned):
            return bootstrap_organization(pinned, model_profiles={"default": profile}, default_model_profile="default",
                                          state_dir=tmp_path / "planning-agents")

        roles = NativeManagedRoles(runtime_for=runtime_for, state_dir=tmp_path / "planning-roles", proposal_event="github.issue.ready",
                                   planning=planning)
        dependencies = ManagedDependencies(planning=planning, worker=worker, state_dir=tmp_path / "dependency-handoffs", routes=(
            PlanningRoute(repository=repository, repository_id=repository_id, organization_id=snapshot.organization_id,
                          revision=snapshot.revision, event="github.issue.ready"),)) if dependencies_enabled else None
        service = ManagedOrganizationService(definitions=definitions, assignments=assignments, runs=runs, previews=previews,
                                              worker=worker, routes=(), operator_id="operator", operations=roles.operations | (
                                                  {"definition_probe": handoff_operation} if handoff_operation else {}),
                                              cleanup=roles.cleanup, binding_revision="planning-v1", planning=admission, dependencies=dependencies)
        return service, planning, roles

    service, planning, roles = make_service()
    scope = PlanningScope(repository=repository, repository_id=repository_id, base_revision=base_revision, base_branch="main", limits=limits)
    preview = planning.admission.offer(request_id="request-one", objective="Build two bounded behaviors", scope=scope,
                                       developer_policy=policy)
    return service, planning, roles, preview, previews, github, make_service, policy


def native_reply(bodies, document=None, *, inspect=True):
    def reply(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1 and inspect:
            message = {"role": "assistant", "content": "Inspecting request.", "tool_calls": [{"id": "inspect", "type": "function", "function": {
                "name": "pm_inspect_planning_request", "arguments": "{}",
            }}]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": json.dumps(document if document is not None else proposal())}
            finish = "stop"
        return httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 1, "model": "test-model",
                                        "choices": [{"index": 0, "message": message, "finish_reason": finish}]})
    return reply


@pytest.mark.parametrize("detached", [False, True])
def test_native_planning_exact_publication_restart_and_unapproved_handoff(tmp_path, approved_delivery, detached):
    bodies = []

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies))) as transport:
            async with AsyncOpenAI(api_key="test", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                service, planning, _, preview, previews, github, restart, policy = planning_fixture(tmp_path, profile, approved_delivery[1])
                assert await service.consume(preview.preview_id) is None
                assert not planning.admission.budget_path(preview.preview_id).exists()
                previews.approve(preview.preview_id)
                if detached:
                    from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker

                    background = ManagedOrganizationWorker(service=service, store=FileWorkerStore(tmp_path / "planning-worker.json"))
                    background.enqueue(preview.preview_id)
                    await background.start()
                    try:
                        await background.wait_idle(preview.preview_id)
                        waiting = service.status(preview.preview_id)
                    finally:
                        await background.close()
                else:
                    waiting = await service.consume(preview.preview_id)
                assert waiting.state == "waiting", waiting.error
                assert not github.issues
                budget_path = service.budget_path(preview.preview_id)
                initial = budget_path.read_bytes()
                plan = planning.plans.get(waiting.assignment_id)
                assert plan.inspections["target"]["base_revision"] == "a" * 40
                assert waiting.pending[0].data["issues"] == list(plan.issues)
                restarted, recovered, _ = restart()
                assert await restarted.consume(preview.preview_id) == waiting
                with pytest.raises(PermissionError):
                    await restarted.decide(waiting.assignment_id, request_id="wrong", approved=True)
                done = await restarted.decide(waiting.assignment_id, request_id=waiting.pending[0].request_id, approved=True)
                assert done.state == "waiting" and done.pending[0].data["stage"] == "link"
                link = done.pending[0].data["links"][0]
                assert link["issue_number"] == 2 and link["issue_id"] == 2
                assert "- Depends on #1" in link["content"]["body"]
                assert github.issues["example/target"][2].body == link["before_body"]
                assert not any(item.bundle_payload["task_id"].startswith("planned-issue-") for item in previews.list_previews(pending_only=False, limit=50))
                restarted = restart()[0]
                assert await restarted.consume(preview.preview_id) == done
                done = await restarted.decide(done.assignment_id, request_id=done.pending[0].request_id, approved=True)
                assert done.state == "completed" and done.cleanup_succeeded, done.error
                assert len(done.decisions) == 2 and all(decision.actor_id == "operator" for decision in done.decisions)
                result = done.outputs[0]
                assert result["state"] == "published" and result["implementation_approved"] is False
                issues = list(github.issues["example/target"].values())
                assert len(issues) == 2
                assert issues[0].title == plan.issues[0]["title"] and issues[0].body == plan.issues[0]["body"]
                assert issues[1].body == plan.issues[1]["body"].replace("- Planned issue: first", "- Depends on #" + str(issues[0].number))
                for preview_id in result["developer_previews"]:
                    child = previews.get(preview_id)
                    assert not child.approved and child.approved_at is None and child.dispatched_at is None
                    assert child.bundle_payload["issue_context"]["base_revision"] == "a" * 40
                    assert child.bundle_payload["policy"]["max_file_changes"] == policy.max_file_changes
                    assert service.recorded_assignment(child.bundle_payload["task_id"]) is None
                assert await restart()[0].consume(preview.preview_id) == done
                assert recovered.reconcile(waiting.assignment_id) == result
                snapshot = service._definitions.get(waiting.organization_id, waiting.revision)
                developer_route = next(route for route in snapshot.definition.routes if route.id != "planning")
                dependencies = ManagedDependencies(planning=recovered, worker=approved_delivery[1], state_dir=tmp_path / "dependencies",
                    routes=(PlanningRoute(repository="example/target", repository_id=1, organization_id=waiting.organization_id,
                                          revision=waiting.revision, event=developer_route.events[0]),))
                blocked = dependencies.offer(planning_assignment_id=waiting.assignment_id, issue_key="second", base_revision="b" * 40)
                assert blocked["state"] == "waiting" and blocked["handoff_preview"] is None
                assert blocked["prerequisites"][0]["reason"] == "not_published"
                assert not list(dependencies.directory.glob("*.json"))
                with pytest.raises(PermissionError, match="fresh dependency handoff"):
                    dependencies.route_for(previews.get(result["developer_previews"][1]))
                assert budget_path.read_bytes() == initial
                with pytest.raises(ValueError):
                    await restarted.decide(waiting.assignment_id, request_id=waiting.pending[0].request_id, approved=True)
                assert approved_delivery[1].get(preview.preview_id) is None
                assert len(github.issues["example/target"]) == 2

    asyncio.run(exercise())
    assert len(bodies) == 2
    assert [tool["function"]["name"] for tool in bodies[0]["tools"]] == ["pm_inspect_planning_request"]


@pytest.mark.parametrize("case", ["merged", "draft", "issue_drift", "dependent_closed", "parent_unapproved", "verification", "cleanup",
                                  "receipt_drift", "preview_drift", "lost_receipt", "lost_preview", "symlink", "original_approved", "parent_drift",
                                  "all_dependencies", "chain", "partial_stage", "stale_base", "route_drift", "operator_drift", "unbound"])
def test_dependency_handoff_stages_only_immutable_unapproved_scope(tmp_path, approved_delivery, monkeypatch, case):
    bodies = []
    document = proposal()
    if case in {"chain", "all_dependencies"}:
        document["issues"].append({"key": "third", "title": "Third issue", "objective": "Implement third behavior",
                                  "acceptance_criteria": ["Third behavior passes"], "dependencies": ["second"] if case == "chain" else []})
        if case == "all_dependencies":
            document["issues"][1]["dependencies"].append("third")

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies, document))) as transport:
            async with AsyncOpenAI(api_key="test", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                service, planning, _, preview, previews, github, restart, _ = planning_fixture(tmp_path, profile, approved_delivery[1])
                previews.approve(preview.preview_id)
                run = await service.consume(preview.preview_id)
                run = await service.decide(run.assignment_id, request_id=run.pending[0].request_id, approved=True)
                run = await service.decide(run.assignment_id, request_id=run.pending[0].request_id, approved=True)
                assert run.state == "completed", run.error
                first_id, second_id = run.outputs[0]["developer_previews"][:2]
                parent = previews.approve(first_id)
                original = previews.get(second_id)
                frozen_source = json.dumps(original.bundle_payload, sort_keys=True)
                pm_budget = service.budget_path(preview.preview_id).read_bytes()
                route = PlanningRoute(repository="example/target", repository_id=1, organization_id=run.organization_id,
                                      revision=run.revision, event="github.issue.ready")
                github.commit_files[("example/target", "b" * 40)] = {}
                raw = {"number": 7, "state": "closed", "draft": False, "merged": True,
                    "head": {"sha": "d" * 40, "ref": "aitobuild/issue-1", "repo": {"id": 1, "full_name": "example/target"}},
                    "base": {"ref": "main", "repo": {"id": 1, "full_name": "example/target"}}, "merge_commit_sha": "c" * 40,
                    "merged_at": "2026-10-09T12:00:00Z", "merged_by": {"id": 10, "login": "human", "type": "User"}}
                github.dependency_merges[("example/target", 7, "b" * 40)] = {
                    "pull": raw, "branch_sha": "b" * 40, "comparison": {"status": "ahead", "merge_base_commit": {"sha": "c" * 40}}}
                publication = {"repository": "example/target", "issue_number": 1, "pull_number": 7,
                    "base_ref": "main", "base_sha": "a" * 40, "head_sha": "d" * 40, "branch": "aitobuild/issue-1"}
                delivery = DeliveryPreparation(preview_id=first_id, task_id=parent.bundle_payload["task_id"], state="published",
                    source_path=str(tmp_path), checkout_path=str(tmp_path / "target"), branch="aitobuild/issue-1", base_revision="a" * 40,
                    head_revision="d" * 40, bundle_payload=parent.bundle_payload, approved_at=parent.approved_at.isoformat(), updated_at=parent.approved_at.isoformat(),
                    verification={"cleanup_succeeded": True, "commands": [{"exit_code": 0}]}, publication=publication)
                monkeypatch.setattr(approved_delivery[1], "get", lambda preview_id: delivery if preview_id == first_id else None)
                github.issues["example/target"][1] = replace(github.issues["example/target"][1], state="closed")
                if case == "draft":
                    raw.update(state="open", draft=True, merged=False)
                elif case == "issue_drift":
                    github.issues["example/target"][1] = replace(github.issues["example/target"][1], body="changed")
                elif case == "dependent_closed":
                    github.issues["example/target"][2] = replace(github.issues["example/target"][2], state="closed")
                elif case == "parent_unapproved":
                    delivery = replace(delivery, approved_at="not the saved approval")
                elif case == "verification":
                    delivery = replace(delivery, verification={"cleanup_succeeded": True, "commands": [{"exit_code": True}]})
                elif case == "cleanup":
                    delivery = replace(delivery, verification={"cleanup_succeeded": False, "commands": [{"exit_code": 0}]})
                elif case == "original_approved":
                    previews.approve(second_id)

                def manager():
                    return ManagedDependencies(planning=restart()[1], worker=approved_delivery[1], state_dir=tmp_path / "dependency-handoffs", routes=(route,))

                dependencies = manager()
                arguments = {"planning_assignment_id": run.assignment_id, "issue_key": "second", "base_revision": "b" * 40}
                if case in {"issue_drift", "dependent_closed", "parent_unapproved", "verification", "cleanup", "original_approved"}:
                    with pytest.raises(PermissionError):
                        dependencies.offer(**arguments)
                    assert not list(dependencies.directory.glob("*.json"))
                else:
                    if case == "partial_stage":
                        from aitobuild import organization_dependencies
                        writer = organization_dependencies.atomic_write_text
                        def interrupt(path, content):
                            if json.loads(content)["preview_id"] is not None:
                                raise OSError("Lost local stage acknowledgement")
                            return writer(path, content)
                        monkeypatch.setattr(organization_dependencies, "atomic_write_text", interrupt)
                        with pytest.raises(OSError, match="Lost local stage"):
                            dependencies.offer(**arguments)
                        staged = [item for item in previews.list_previews(pending_only=False, limit=50)
                                  if item.bundle_payload["task_id"].startswith("dependent-issue-")]
                        assert len(staged) == 1 and not staged[0].approved
                        monkeypatch.setattr(organization_dependencies, "atomic_write_text", writer)
                    result = dependencies.offer(**arguments)
                    if case in {"draft", "all_dependencies"}:
                        assert result["state"] == "waiting" and result["handoff_preview"] is None
                        if case == "all_dependencies":
                            assert [item["ready"] for item in result["prerequisites"]] == [True, False]
                    else:
                        fresh = previews.get(result["handoff_preview"]["preview_id"])
                        assert not fresh.approved and fresh.preview_id != second_id
                        assert fresh.bundle_payload["issue_context"]["base_revision"] == "b" * 40
                        assert fresh.bundle_payload["acceptance_criteria"] == original.bundle_payload["acceptance_criteria"]
                        assert fresh.bundle_payload["policy"] == original.bundle_payload["policy"]
                        assert manager().offer(**arguments) == result
                        assert manager().route_for(fresh) == route
                        path = dependencies._path(fresh.bundle_payload["task_id"])
                        if case == "receipt_drift":
                            damaged_receipt = json.loads(path.read_text())
                            damaged_receipt["base_revision"] = "e" * 40
                            path.write_text(json.dumps(damaged_receipt))
                        elif case == "lost_receipt":
                            path.unlink()
                        elif case == "symlink":
                            content = path.read_bytes()
                            path.unlink()
                            other = tmp_path / "redirected.json"
                            other.write_bytes(content)
                            path.symlink_to(other)
                        elif case in {"lost_preview", "preview_drift"}:
                            with previews._transaction(write=True):
                                if case == "lost_preview":
                                    del previews._by_id[fresh.preview_id]
                                    previews._by_dedupe.pop(fresh.dedupe_key)
                                    previews._by_task.pop(fresh.bundle_payload["task_id"])
                                else:
                                    previews._by_id[fresh.preview_id] = replace(fresh, bundle_payload=fresh.bundle_payload | {"objective": "outside scope"})
                        elif case == "parent_drift":
                            raw["head"]["sha"] = "e" * 40
                        elif case == "stale_base":
                            github.dependency_merges[("example/target", 7, "b" * 40)]["branch_sha"] = "e" * 40
                        elif case == "route_drift":
                            dependencies.routes = (route.model_copy(update={"event": "different.event"}),)
                        elif case == "operator_drift":
                            dependencies.planning.admission.operator_id = "another-operator"
                        elif case == "unbound":
                            with pytest.raises(PermissionError, match="trusted admission"):
                                restart()[0].admission(fresh.preview_id)
                        if case not in {"merged", "chain", "partial_stage", "unbound"}:
                            with pytest.raises((PermissionError, ValueError, OSError)):
                                if case in {"preview_drift", "parent_drift", "stale_base", "route_drift", "operator_drift"}:
                                    dependencies.route_for(previews.get(fresh.preview_id))
                                else:
                                    manager().offer(**arguments)
                        else:
                            previews.approve(fresh.preview_id)
                            assert manager().route_for(previews.get(fresh.preview_id)) == route
                            assert not approved_delivery[1].budget_path(fresh.preview_id).exists()
                            if case == "chain":
                                approved = previews.get(fresh.preview_id)
                                next_delivery = replace(delivery, preview_id=approved.preview_id, task_id=approved.bundle_payload["task_id"],
                                    bundle_payload=approved.bundle_payload, base_revision="b" * 40, head_revision="f" * 40,
                                    approved_at=approved.approved_at.isoformat(), branch="aitobuild/issue-2",
                                    publication=publication | {"issue_number": 2, "pull_number": 8, "base_sha": "b" * 40,
                                                               "head_sha": "f" * 40, "branch": "aitobuild/issue-2"})
                                monkeypatch.setattr(approved_delivery[1], "get", lambda preview_id:
                                    delivery if preview_id == first_id else next_delivery if preview_id == approved.preview_id else None)
                                github.commit_files[("example/target", "e" * 40)] = {}
                                github.issues["example/target"][2] = replace(github.issues["example/target"][2], state="closed")
                                github.dependency_merges[("example/target", 7, "e" * 40)] = {
                                    "pull": raw, "branch_sha": "e" * 40, "comparison": {"status": "ahead", "merge_base_commit": {"sha": "c" * 40}}}
                                next_raw = raw | {"number": 8, "head": raw["head"] | {"sha": "f" * 40, "ref": "aitobuild/issue-2"},
                                                  "merge_commit_sha": "2" * 40}
                                github.dependency_merges[("example/target", 8, "e" * 40)] = {
                                    "pull": next_raw, "branch_sha": "e" * 40, "comparison": {"status": "ahead", "merge_base_commit": {"sha": "2" * 40}}}
                                third = manager().offer(planning_assignment_id=run.assignment_id, issue_key="third", base_revision="e" * 40)
                                assert third["state"] == "staged" and not third["handoff_preview"]["approved"]
                                assert third["prerequisites"][0]["preview_id"] == approved.preview_id
                                assert not previews.get(run.outputs[0]["developer_previews"][2]).approved
                assert json.dumps(previews.get(second_id).bundle_payload, sort_keys=True) == frozen_source
                assert service.budget_path(preview.preview_id).read_bytes() == pm_budget
                assert not list(dependencies.directory.glob("budgets/*"))
                assert len(github.issues["example/target"]) == len(document["issues"])

    asyncio.run(exercise())
    assert len(bodies) == 2


@pytest.mark.parametrize("case", ["no_inspection", "malformed", "authority", "limit", "repository", "cancel", "reject", "scope", "approval", "session_pins", "expired", "deadline", "missing_budget"])
def test_native_planning_fails_closed_without_writes_or_model_replay(tmp_path, approved_delivery, monkeypatch, case):
    bodies = []
    document = proposal()
    if case == "malformed":
        document = "not a plan"
    if case == "authority":
        document["issues"][0]["approved"] = True

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies, document, inspect=case != "no_inspection"))) as transport:
            async with AsyncOpenAI(api_key="test", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                service, planning, roles, preview, previews, github, restart, _ = planning_fixture(
                    tmp_path, profile, approved_delivery[1], limits=PlanningLimits(max_issues=1 if case == "limit" else 8))
                previews.approve(preview.preview_id)
                if case == "repository":
                    github.repository_ids["example/target"] = 99
                waiting = await service.consume(preview.preview_id)
                if case in {"no_inspection", "malformed", "authority", "limit", "repository"}:
                    assert waiting.state == "failed", waiting.error
                    assert await restart()[0].consume(preview.preview_id) == waiting
                    assert not github.issues
                    return
                assert waiting.state == "waiting", waiting.error
                budget_path = service.budget_path(preview.preview_id)
                initial = json.loads(budget_path.read_text())
                if case == "scope":
                    path = tmp_path / "planning-previews.json"
                    state = json.loads(path.read_text())
                    state["previews"][0]["bundle_payload"]["objective"] = "Changed scope"
                    path.write_text(json.dumps(state))
                elif case == "approval":
                    session = await roles._sessions.get(waiting.session_id)
                    session.state["aitobuild_plan_approval"]["issues"][0]["title"] = "Changed title"
                    await roles._sessions.set(waiting.session_id, session)
                elif case == "session_pins":
                    session = await roles._sessions.get(waiting.session_id)
                    session.state["aitobuild_role_pins"]["revision"] = "b" * 64
                    await roles._sessions.set(waiting.session_id, session)
                elif case == "expired":
                    monkeypatch.setattr("aitobuild.developer_isolation.time", lambda: initial["deadline"] + 1)
                elif case == "deadline":
                    budget_path.write_text(json.dumps(initial | {"deadline": initial["deadline"] + 60}))
                elif case == "missing_budget":
                    budget_path.unlink()
                if case == "cancel":
                    done = await service.cancel(waiting.assignment_id)
                    assert done.state == "cancelled"
                elif case in {"deadline", "missing_budget"}:
                    with pytest.raises((PermissionError, FileNotFoundError)):
                        await service.decide(waiting.assignment_id, request_id=waiting.pending[0].request_id, approved=True)
                    done = service.status(preview.preview_id)
                    assert done.state == "finalizing" and done.cleanup_succeeded
                else:
                    done = await service.decide(waiting.assignment_id, request_id=waiting.pending[0].request_id, approved=case != "reject")
                    assert done.state == "failed", done.error
                assert not github.issues
                assert not any(item.bundle_payload["task_id"].startswith("planned-issue-") for item in previews.list_previews(pending_only=False, limit=50))
                if case != "missing_budget":
                    final = json.loads(budget_path.read_text())
                    assert final["reserved_paths"] == initial["reserved_paths"]
                    assert final["deadline"] == initial["deadline"] + (60 if case == "deadline" else 0)
                    assert final.get("aborted", False) is (case != "deadline")
                else:
                    assert not budget_path.exists()

    asyncio.run(exercise())
    assert len(bodies) <= 2


@pytest.mark.parametrize("effect", ["create_landed", "create_absent", "link_landed", "link_absent", "duplicate", "remote_drift", "receipt_save"])
def test_uncertain_issue_writes_reconcile_read_only_without_duplicate_creation(tmp_path, approved_delivery, monkeypatch, effect):
    bodies = []
    document = proposal()
    if effect not in {"link_landed", "link_absent"}:
        document["issues"] = document["issues"][:1]

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies, document))) as transport:
            async with AsyncOpenAI(api_key="test", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                service, planning, _, preview, previews, github, restart, _ = planning_fixture(tmp_path, profile, approved_delivery[1])
                previews.approve(preview.preview_id)
                waiting = await service.consume(preview.preview_id)
                assert waiting.state == "waiting", waiting.error
                original_create, original_update = github.create_issue, github.update_issue
                writes = []

                def create(**kwargs):
                    writes.append("create")
                    if effect == "create_absent":
                        raise OSError("Create response uncertain")
                    issue = original_create(**kwargs)
                    if effect == "duplicate":
                        original_create(**kwargs)
                    if effect == "remote_drift":
                        github.issues["example/target"][issue.number] = replace(issue, title="Changed remote")
                    if effect not in {"link_landed", "link_absent", "receipt_save"}:
                        raise OSError("Create response uncertain")
                    return issue

                def update(**kwargs):
                    writes.append("update")
                    if effect == "link_absent":
                        raise OSError("Link response uncertain")
                    original_update(**kwargs)
                    raise OSError("Link response uncertain")

                monkeypatch.setattr(github, "create_issue", create)
                monkeypatch.setattr(github, "update_issue", update)
                if effect == "receipt_save":
                    original_save = planning._save_publication

                    def fail_receipt(saved):
                        if saved.effects and saved.effects[0].state == "created":
                            raise OSError("Receipt save lost")
                        original_save(saved)

                    monkeypatch.setattr(planning, "_save_publication", fail_receipt)
                done = await service.decide(waiting.assignment_id, request_id=waiting.pending[0].request_id, approved=True)
                if done.state == "waiting":
                    assert done.pending[0].data["stage"] == "link"
                    done = await service.decide(done.assignment_id, request_id=done.pending[0].request_id, approved=True)
                assert done.state == "failed" and len(done.decisions) == (2 if effect.startswith("link") else 1), done.error
                ledger = service.budget_path(preview.preview_id).read_bytes()
                assert await restart()[0].consume(preview.preview_id) == done
                saved_writes = list(writes)
                if effect in {"create_landed", "link_landed", "receipt_save"}:
                    result = restart()[1].reconcile(waiting.assignment_id)
                    assert result["state"] == "published"
                    for preview_id in result["developer_previews"]:
                        assert not previews.get(preview_id).approved
                    assert restart()[1].reconcile(waiting.assignment_id) == result
                else:
                    with pytest.raises(PermissionError):
                        restart()[1].reconcile(waiting.assignment_id)
                assert writes == saved_writes
                assert service.budget_path(preview.preview_id).read_bytes() == ledger
                assert json.loads(ledger)["aborted"] is True

    asyncio.run(exercise())
    assert len(bodies) == 2


def test_cli_issue_reconciliation_pages_and_write_time_guards(monkeypatch):
    adapter = GhCliGitHubAdapter(allowed_repositories=("example/target",))
    calls = []
    marker = "<!-- aitobuild-plan:exact:first -->"
    issue = {"id": 7, "number": 2, "title": "Title", "body": marker, "state": "open"}

    def api(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        if endpoint == "repos/example/target":
            return {"id": 1, "full_name": "example/target"}
        if "/commits/" in endpoint:
            return {"sha": "a" * 40}
        page = parse_qs(urlsplit(endpoint).query).get("page", [None])[0]
        if page == "1":
            return [{"number": index, "body": "other"} for index in range(100)]
        if page == "2":
            return [issue]
        return issue

    monkeypatch.setattr(adapter, "_api", api)
    assert adapter.inspect_planning_target(repository="example/target", repository_id=1, base_revision="a" * 40)["repository_id"] == 1
    matches = adapter.find_issue_publication(repository="example/target", marker=marker, max_pages=2)
    assert len(matches) == 1 and matches[0].issue_id == 7
    with pytest.raises(PermissionError, match="incomplete"):
        adapter.find_issue_publication(repository="example/target", marker=marker, max_pages=1)
    before = len(calls)

    def expired():
        raise TimeoutError("Expired original budget")

    from aitobuild.policy import AgentRole

    with pytest.raises(TimeoutError):
        adapter.create_issue(role=AgentRole.PM, repository="example/target", title="Title", body=marker,
                             approved=True, require_human_approval_for_repo_writes=True, before_write=expired)
    with pytest.raises(TimeoutError):
        adapter.update_issue(role=AgentRole.PM, repository="example/target", issue_number=2, body=marker,
                             approved=True, require_human_approval_for_repo_writes=True, before_write=expired)
    assert len(calls) == before


@pytest.mark.parametrize("case", ["duplicate", "scope", "revision", "operator", "lost_receipt", "lost_preview", "symlink", "lost_budget", "initialization"])
def test_planning_admission_keeps_original_scope_and_ledger(tmp_path, approved_delivery, case):
    service, planning, _, preview, previews, _, restart, policy = planning_fixture(tmp_path, None, approved_delivery[1])
    admission = planning.admission
    request = admission.request_for(preview.preview_id)
    path = admission._path(str(preview.bundle_payload["task_id"]))
    previews.approve(preview.preview_id)
    admission.prepare(preview.preview_id)
    ledger = admission.budget_path(preview.preview_id)
    initial = ledger.read_bytes()
    scope = request.scope
    if case == "duplicate":
        duplicate = restart()[1].admission.offer(request_id="request-one", objective="Build two bounded behaviors", scope=scope,
                                                developer_policy=policy)
        assert duplicate.preview_id == preview.preview_id and duplicate.approved
        admission.prepare(preview.preview_id)
        assert ledger.read_bytes() == initial
        return
    if case == "scope":
        scope = scope.model_copy(update={"base_revision": "b" * 40})
    elif case == "revision":
        state = json.loads(path.read_text())
        state["route"]["revision"] = "b" * 64
        path.write_text(json.dumps(state))
    elif case == "operator":
        admission.operator_id = "another-operator"
    elif case == "lost_receipt":
        path.unlink()
        ledger.unlink()
    elif case == "lost_preview":
        (tmp_path / "planning-previews.json").unlink()
        admission.previews = DeveloperPreviewRegistry(tmp_path / "planning-previews.json")
    elif case == "symlink":
        destination = tmp_path / "outside.json"
        path.rename(destination)
        path.symlink_to(destination)
    elif case == "lost_budget":
        ledger.unlink()
    elif case == "initialization":
        state = json.loads(path.read_text())
        state.update(ledger_state="creating", deadline=None)
        path.write_text(json.dumps(state))
    with pytest.raises((ValueError, PermissionError)):
        if case in {"lost_budget", "initialization"}:
            admission.prepare(preview.preview_id)
        else:
            admission.offer(request_id="request-one", objective="Build two bounded behaviors", scope=scope, developer_policy=policy)
    if case in {"lost_receipt", "lost_budget"}:
        assert not ledger.exists()
    else:
        assert ledger.read_bytes() == initial


def test_partial_developer_staging_recovers_metadata_without_repeating_writes(tmp_path, approved_delivery, monkeypatch):
    bodies = []

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies))) as transport:
            async with AsyncOpenAI(api_key="test", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                service, planning, _, preview, previews, github, restart, _ = planning_fixture(tmp_path, profile, approved_delivery[1])
                previews.approve(preview.preview_id)
                waiting = await service.consume(preview.preview_id)
                create = previews.create_or_get
                calls = []

                def fail_second(**kwargs):
                    calls.append(kwargs["task_key"])
                    if len(calls) == 2:
                        raise OSError("Staging interrupted")
                    return create(**kwargs)

                with monkeypatch.context() as patch:
                    patch.setattr(previews, "create_or_get", fail_second)
                    done = await service.decide(waiting.assignment_id, request_id=waiting.pending[0].request_id, approved=True)
                    assert done.state == "waiting"
                    done = await service.decide(done.assignment_id, request_id=done.pending[0].request_id, approved=True)
                assert done.state == "failed" and len(github.issues["example/target"]) == 2
                before = {key: value.to_dict() for key, value in github.issues["example/target"].items()}
                ledger = service.budget_path(preview.preview_id).read_bytes()
                result = restart()[1].reconcile(waiting.assignment_id)
                assert len(result["developer_previews"]) == 2
                assert {key: value.to_dict() for key, value in github.issues["example/target"].items()} == before
                assert service.budget_path(preview.preview_id).read_bytes() == ledger
                assert all(not previews.get(preview_id).approved for preview_id in result["developer_previews"])

    asyncio.run(exercise())
    assert len(bodies) == 2


def test_dependency_rendering_does_not_rewrite_issue_objective():
    from aitobuild.organization_planning import PlannedIssue

    issue = PlannedIssue.model_validate_json(json.dumps({
        "key": "second", "title": "Second", "objective": "Preserve example:\n- Planned issue: first",
        "acceptance_criteria": ["Content preserved"], "dependencies": ["first"],
    }))
    body = ManagedPlanning._render_body(issue, "marker", {"first": 7})
    assert body.startswith(issue.objective)
    assert "## Dependencies\n- Depends on #7" in body


def test_managed_planning_http_requires_exact_task_create_and_link_approvals(tmp_path, test_config):
    from fastapi.testclient import TestClient
    from aitobuild.app import create_app

    bodies = []
    transport = httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies)))
    client = AsyncOpenAI(api_key="test", http_client=transport)
    profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
    github = MockGitHubAdapter(repository_ids={"example/target": 1}, commit_files={("example/target", "a" * 40): {}})
    refs = {}

    def factory(context):
        service, planning, _, preview, _, _, _, _ = planning_fixture(tmp_path, profile, context.worker,
                                                                   github=github, previews=context.previews)
        refs.update(service=service, planning=planning, preview=preview)
        return service

    headers = {"X-Internal-Token": "internal-test-token"}
    try:
        with TestClient(create_app(test_config, managed_service_factory=factory)) as http:
            preview_id = refs["preview"].preview_id
            assert http.post("/internal/organization/tasks/run", headers=headers, json={"preview_id": preview_id}).status_code == 409
            assert http.post("/internal/developer/preview/approve", json={"preview_id": preview_id}).status_code == 401
            response = http.post("/internal/developer/preview/approve", headers=headers, json={"preview_id": preview_id})
            assert response.status_code == 200, response.text
            waiting = response.json()["managed_run"]
            assert waiting["state"] == "waiting", waiting["error"]
            assert not github.issues
        with TestClient(create_app(test_config, managed_service_factory=factory)) as http:
            response = http.post("/internal/organization/tasks/run", headers=headers, json={"preview_id": preview_id})
            assert response.status_code == 200 and response.json()["managed_run"] == waiting
            decision = {"assignment_id": waiting["assignment_id"], "request_id": waiting["pending"][0]["request_id"], "approved": True}
            assert http.post("/internal/organization/tasks/approve", json=decision).status_code == 401
            assert http.post("/internal/organization/tasks/approve", headers=headers, json=decision | {"actor_id": "model"}).status_code == 400
            response = http.post("/internal/organization/tasks/approve", headers=headers, json=decision)
            assert response.status_code == 200, response.text
            done = response.json()["managed_run"]
            assert done["state"] == "waiting" and done["pending"][0]["data"]["stage"] == "link"
        with TestClient(create_app(test_config, managed_service_factory=factory)) as http:
            response = http.post("/internal/organization/tasks/run", headers=headers, json={"preview_id": preview_id})
            assert response.json()["managed_run"] == done
            decision = {"assignment_id": done["assignment_id"], "request_id": done["pending"][0]["request_id"], "approved": True}
            response = http.post("/internal/organization/tasks/approve", headers=headers, json=decision)
            assert response.status_code == 200, response.text
            done = response.json()["managed_run"]
            assert done["state"] == "completed", done["error"]
            assert len(github.issues["example/target"]) == 2
            assert http.post("/internal/organization/tasks/approve", headers=headers, json=decision).status_code == 409
            for child_id in done["outputs"][0]["developer_previews"]:
                child = refs["planning"].admission.previews.get(child_id)
                assert not child.approved and child.dispatched_at is None
        with TestClient(create_app(test_config, managed_service_factory=factory)) as http:
            response = http.post("/internal/organization/tasks/run", headers=headers, json={"preview_id": preview_id})
            assert response.json()["managed_run"] == done
            assert len(github.issues["example/target"]) == 2
    finally:
        asyncio.run(client.close())
    assert len(bodies) == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_dependency_http_offer_and_approval_fail_closed(tmp_path, test_config, monkeypatch, enabled):
    from fastapi.testclient import TestClient
    from aitobuild.app import create_app

    bodies, refs = [], {}
    transport = httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies)))
    client = AsyncOpenAI(api_key="test", http_client=transport)
    profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
    github = MockGitHubAdapter(repository_ids={"example/target": 1}, commit_files={("example/target", "a" * 40): {}, ("example/target", "b" * 40): {}})

    def factory(context):
        service, planning, _, preview, _, _, _, _ = planning_fixture(tmp_path, profile, context.worker,
            github=github, previews=context.previews, dependencies_enabled=enabled)
        refs.update(service=service, planning=planning, preview=preview)
        return service

    headers = {"X-Internal-Token": "internal-test-token"}
    try:
        with TestClient(create_app(test_config, managed_service_factory=factory)) as http:
            response = http.post("/internal/developer/preview/approve", headers=headers, json={"preview_id": refs["preview"].preview_id})
            run = response.json()["managed_run"]
            for _ in range(2):
                response = http.post("/internal/organization/tasks/approve", headers=headers, json={
                    "assignment_id": run["assignment_id"], "request_id": run["pending"][0]["request_id"], "approved": True})
                assert response.status_code == 200, response.text
                run = response.json()["managed_run"]
            assert run["state"] == "completed", run["error"]
            arguments = {"planning_assignment_id": run["assignment_id"], "issue_key": "second", "base_revision": "b" * 40}
            url = "/internal/organization/dependencies/offer"
            assert http.post(url, json=arguments).status_code == 401
            assert http.post(url, headers=headers, json=arguments | {"actor_id": "model"}).status_code == 400
            response = http.post(url, headers=headers, json=arguments)
            if not enabled:
                assert response.status_code == 409
                return
            assert response.status_code == 200 and response.json()["state"] == "waiting", response.text
            registry = refs["planning"].admission.previews
            worker = refs["service"]._worker
            first_id, second_id = run["outputs"][0]["developer_previews"]
            assert http.post("/internal/developer/preview/approve", headers=headers, json={"preview_id": second_id}).status_code == 409
            assert not registry.get(second_id).approved
            assert http.post("/internal/developer/delivery/prepare", headers=headers, json={"preview_id": second_id}).status_code == 409
            parent = registry.approve(first_id)
            raw = {"number": 7, "state": "closed", "draft": False, "merged": True,
                   "head": {"sha": "d" * 40, "ref": "aitobuild/issue-1", "repo": {"id": 1, "full_name": "example/target"}},
                   "base": {"ref": "main", "repo": {"id": 1, "full_name": "example/target"}}, "merge_commit_sha": "c" * 40,
                   "merged_at": "2026-10-09T12:00:00Z", "merged_by": {"id": 10, "login": "human", "type": "User"}}
            github.dependency_merges[("example/target", 7, "b" * 40)] = {
                "pull": raw, "branch_sha": "b" * 40, "comparison": {"status": "ahead", "merge_base_commit": {"sha": "c" * 40}}}
            publication = {"repository": "example/target", "issue_number": 1, "pull_number": 7, "base_ref": "main",
                           "base_sha": "a" * 40, "head_sha": "d" * 40, "branch": "aitobuild/issue-1"}
            delivery = DeliveryPreparation(preview_id=first_id, task_id=parent.bundle_payload["task_id"], state="published",
                source_path=str(tmp_path), checkout_path=str(tmp_path / "target"), branch="aitobuild/issue-1", base_revision="a" * 40,
                head_revision="d" * 40, bundle_payload=parent.bundle_payload, approved_at=parent.approved_at.isoformat(), updated_at=parent.approved_at.isoformat(),
                verification={"cleanup_succeeded": True, "commands": [{"exit_code": 0}]}, publication=publication)
            monkeypatch.setattr(worker, "get", lambda preview_id: delivery if preview_id == first_id else None)
            response = http.post(url, headers=headers, json=arguments)
            assert response.status_code == 200, response.text
            fresh_id = response.json()["handoff_preview"]["preview_id"]
            assert not registry.get(fresh_id).approved and not worker.budget_path(fresh_id).exists()
            github.dependency_merges[("example/target", 7, "b" * 40)]["branch_sha"] = "e" * 40
            response = http.post("/internal/developer/preview/approve", headers=headers, json={"preview_id": fresh_id})
            assert response.status_code == 409 and not registry.get(fresh_id).approved, response.text
            github.dependency_merges[("example/target", 7, "b" * 40)]["branch_sha"] = "b" * 40
            response = http.post("/internal/developer/preview/approve", headers=headers, json={"preview_id": fresh_id})
            assert response.status_code == 200 and registry.get(fresh_id).approved, response.text
            assert not worker.budget_path(fresh_id).exists()
            github.dependency_merges[("example/target", 7, "b" * 40)]["branch_sha"] = "e" * 40
            response = http.post("/internal/developer/delivery/prepare", headers=headers, json={"preview_id": fresh_id})
            assert response.status_code == 409 and not worker.budget_path(fresh_id).exists(), response.text
            response = http.post("/internal/organization/tasks/run", headers=headers, json={"preview_id": fresh_id})
            assert response.status_code == 409 and not worker.budget_path(fresh_id).exists(), response.text
    finally:
        asyncio.run(client.close())
    assert len(bodies) == 2


@pytest.mark.parametrize("mode", ["direct", "detached", "archived"])
def test_dependency_admission_uses_real_pinned_checkout_and_separate_budget(tmp_path, approved_delivery, verification_adapter, monkeypatch, mode):
    from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker

    _, _, _, source, base = approved_delivery
    registry = DeveloperPreviewRegistry(tmp_path / "dependency-admission-previews.json")
    delivery = DeveloperDeliveryWorker(preview_registry=registry, state_dir=tmp_path / "dependency-delivery", service_root=Path.cwd(),
        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source, ("pytest -q",)),))
    monkeypatch.setattr("aitobuild.developer_delivery.shell_request", lambda *args, **kwargs: {
        "ok": True, "status": "exited", "exit_code": 0, "output": "passed", "next_cursor": 1})
    calls, bodies = [], []

    def probe(context, message):
        context.revalidate()
        record = delivery.get(context.assignment.preview_id)
        calls.append(record.preview_id)
        return {"prepared_base": record.base_revision}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies))) as transport:
            async with AsyncOpenAI(api_key="test", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                service, planning, _, preview, previews, github, restart, _ = planning_fixture(tmp_path, profile, delivery,
                    previews=registry, dependencies_enabled=True, repository="fixture/widgets", repository_id=101,
                    base_revision=base, handoff_operation=ManagedOperation(probe))
                registry.approve(preview.preview_id)
                run = await service.consume(preview.preview_id)
                for _ in range(2):
                    run = await service.decide(run.assignment_id, request_id=run.pending[0].request_id, approved=True)
                assert run.state == "completed", run.error
                first_id, original_id = run.outputs[0]["developer_previews"]
                registry.approve(first_id)
                parent = delivery.prepare(first_id)
                bundle = developer_task_bundle_from_payload(parent.bundle_payload)
                budget = DeveloperTaskBudget(path=delivery.budget_path(first_id), bundle=bundle, create=False)
                with delivery.implementation_lock(first_id):
                    delivery.begin_implementation(first_id, bundle=bundle, session_id="dependency-parent", resume=False)
                    budget.reserve_paths(("README.md",))
                    (Path(parent.checkout_path) / "README.md").write_text("Approved prerequisite fixture\n")
                    delivery.finish_implementation(first_id, session_id="dependency-parent")
                assert delivery.verify(first_id, adapter=verification_adapter).state == "verified"
                delivery.publish(first_id, github=github, require_human_approval_for_repo_writes=True, allow_mock_publication=True)
                parent = delivery.get(first_id)
                assert parent.state == "published" and parent.verification["cleanup_succeeded"] is True
                publication = parent.publication
                head = publication["head_sha"]
                (source / "README.md").write_bytes((Path(parent.checkout_path) / "README.md").read_bytes())
                subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "Squash merged prerequisite fixture"],
                    check=True, capture_output=True)
                merged = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
                github.commit_files[("fixture/widgets", merged)] = {}
                github.issues["fixture/widgets"][1] = replace(github.issues["fixture/widgets"][1], state="closed")
                github.dependency_merges[("fixture/widgets", publication["pull_number"], merged)] = {
                    "pull": {"number": publication["pull_number"], "state": "closed", "draft": False, "merged": True,
                        "head": {"sha": head, "ref": publication["branch"], "repo": {"id": 101, "full_name": "fixture/widgets"}},
                        "base": {"ref": "main", "repo": {"id": 101, "full_name": "fixture/widgets"}}, "merge_commit_sha": merged,
                        "merged_at": "2026-10-09T12:00:00Z", "merged_by": {"id": 10, "login": "human-fixture", "type": "User"}},
                    "branch_sha": merged, "comparison": {"status": "identical", "merge_base_commit": {"sha": merged}}}
                original_pm_budget = service.budget_path(preview.preview_id).read_bytes()
                original_parent_budget = delivery.budget_path(first_id).read_bytes()
                if mode == "archived":
                    fresh_worker = DeveloperDeliveryWorker(preview_registry=registry, state_dir=tmp_path / "fresh-handoff-worker", service_root=Path.cwd(),
                        repository_sources=(LocalRepositorySource("fixture/widgets", 101, source, ("pytest -q",)),))
                    arguments = {"planning_assignment_id": run.assignment_id, "issue_key": "second", "base_revision": merged}
                    fresh = ManagedDependencies(planning=planning, worker=fresh_worker, state_dir=tmp_path / "fresh-handoffs",
                        routes=service._dependencies.routes, prerequisite_workers=(delivery,))
                    assert fresh_worker.get(first_id) is None
                    staged = fresh.offer(**arguments)
                    fresh_id = staged["handoff_preview"]["preview_id"]
                    assert not registry.get(fresh_id).approved and not fresh_worker.budget_path(fresh_id).exists()
                    registry.approve(fresh_id)
                    assert fresh.route_for(registry.get(fresh_id)) is not None
                    assert fresh_worker.prepare(fresh_id).base_revision == merged
                    assert fresh_worker.budget_path(fresh_id) != delivery.budget_path(first_id)
                    assert service.budget_path(preview.preview_id).read_bytes() == original_pm_budget
                    assert delivery.budget_path(first_id).read_bytes() == original_parent_budget
                    duplicate = DeveloperDeliveryWorker(preview_registry=registry, state_dir=tmp_path / "dependency-delivery", service_root=Path.cwd())
                    fresh.prerequisite_workers = (delivery, duplicate)
                    with pytest.raises(PermissionError, match="ambiguous receipt ownership"):
                        fresh.route_for(registry.get(fresh_id))
                    return
                staged = await service.offer_dependency_handoff(planning_assignment_id=run.assignment_id, issue_key="second", base_revision=merged)
                fresh_id = staged["handoff_preview"]["preview_id"]
                assert await service.consume(fresh_id) is None and not delivery.budget_path(fresh_id).exists()
                await service.check_preview_approval(fresh_id)
                registry.approve(fresh_id)
                selection = AssignmentProposal(agent_id="developer_one", rationale="Fresh explicit dependent implementation approval")
                service = restart()[0]
                if mode == "direct":
                    result = await service.consume(fresh_id, proposal=selection)
                else:
                    worker = ManagedOrganizationWorker(service=service, store=FileWorkerStore(tmp_path / "dependency-worker.json"), poll_seconds=0.01)
                    worker.enqueue(fresh_id, proposal=selection)
                    await worker.start()
                    try:
                        async with asyncio.timeout(5):
                            assert (await worker.wait_idle(fresh_id)).state == "completed"
                    finally:
                        await worker.close()
                    result = service.status(fresh_id)
                    assert not worker.diagnostics()["errors"]
                assert result.state == "completed", result.error
                assert result.outputs == ({"prepared_base": merged},)
                prepared = delivery.get(fresh_id)
                assert prepared.state == "prepared" and prepared.base_revision == merged and prepared.preview_id != first_id
                assert delivery.budget_path(fresh_id).exists()
                assert not registry.get(original_id).approved and delivery.get(original_id) is None
                assert service.budget_path(preview.preview_id).read_bytes() == original_pm_budget
                assert delivery.budget_path(first_id).read_bytes() == original_parent_budget
                frozen_budget = delivery.budget_path(fresh_id).read_bytes()
                github.dependency_merges[("fixture/widgets", publication["pull_number"], merged)]["branch_sha"] = "e" * 40
                assert await restart()[0].consume(fresh_id) == result
                assert restart()[0].status(fresh_id) == result
                assert delivery.budget_path(fresh_id).read_bytes() == frozen_budget
                assert calls == [fresh_id]

    asyncio.run(exercise())
    assert len(bodies) == 2


@pytest.mark.parametrize("case", ["reject", "drift", "write_drift", "expired", "approval"])
def test_resolved_dependency_approval_cannot_be_bypassed(tmp_path, approved_delivery, monkeypatch, case):
    bodies = []

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_reply(bodies))) as transport:
            async with AsyncOpenAI(api_key="test", http_client=transport) as client:
                profile = ModelProfile("openai", OpenAIChatCompletionClient(model="test-model", async_client=client))
                service, planning, roles, preview, previews, github, restart, _ = planning_fixture(tmp_path, profile, approved_delivery[1])
                previews.approve(preview.preview_id)
                waiting = await service.consume(preview.preview_id)
                links = await service.decide(waiting.assignment_id, request_id=waiting.pending[0].request_id, approved=True)
                assert links.state == "waiting"
                with pytest.raises(PermissionError):
                    planning.reconcile(links.assignment_id)
                with pytest.raises(PermissionError):
                    await service.decide(waiting.assignment_id, request_id=waiting.pending[0].request_id, approved=True)
                if case == "drift":
                    issue = github.issues["example/target"][2]
                    github.issues["example/target"][2] = replace(issue, body="Changed before linking")
                elif case == "write_drift":
                    update = github.update_issue

                    def changed_before_patch(**kwargs):
                        issue = github.issues["example/target"][2]
                        github.issues["example/target"][2] = replace(issue, body="External change before PATCH")
                        return update(**kwargs)

                    monkeypatch.setattr(github, "update_issue", changed_before_patch)
                elif case == "expired":
                    ledger = json.loads(service.budget_path(preview.preview_id).read_text())
                    monkeypatch.setattr("aitobuild.developer_isolation.time", lambda: ledger["deadline"] + 1)
                elif case == "approval":
                    session = await roles._sessions.get(links.session_id)
                    session.state["aitobuild_plan_approval"]["links"][0]["issue_number"] = 999
                    await roles._sessions.set(links.session_id, session)
                before = {key: value.to_dict() for key, value in github.issues["example/target"].items()}
                done = await service.decide(links.assignment_id, request_id=links.pending[0].request_id, approved=case != "reject")
                assert done.state == "failed"
                if case == "write_drift":
                    assert github.issues["example/target"][2].body == "External change before PATCH"
                    assert github.issues["example/target"][1].to_dict() == before[1]
                else:
                    assert {key: value.to_dict() for key, value in github.issues["example/target"].items()} == before
                assert await restart()[0].consume(preview.preview_id) == done
                assert not any(item.bundle_payload["task_id"].startswith("planned-issue-") for item in previews.list_previews(pending_only=False, limit=50))

    asyncio.run(exercise())
    assert len(bodies) == 2