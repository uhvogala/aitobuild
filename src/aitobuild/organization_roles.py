"""Scoped native PM proposals and operator-approved Architect COMMENT reviews."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from agent_framework import Agent, AgentSession, FileSessionStore, FunctionInvocationContext, function_middleware, tool
from filelock import FileLock
from pydantic import Field, JsonValue, StrictStr, model_validator

from aitobuild.developer_delivery import DeveloperDeliveryWorker
from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload, is_path_allowed
from aitobuild.developer_preview import DeveloperPreview, DeveloperPreviewRegistry
from aitobuild.organization import DefinitionModel, DefinitionSnapshot
from aitobuild.organization_assignments import AssignmentProposal
from aitobuild.organization_delivery import _delivery_call
from aitobuild.organization_runner import ManagedOperation, ManagedTaskContext, WorkflowInput, _sync_directory
from aitobuild.organization_runtime import OrganizationRuntime
from aitobuild.organization_reviews import PublishedReviewTarget as PublishedReviewTarget
from aitobuild.organization_service import CoordinatorBinding
from aitobuild.policy import ActionClass, AgentRole
from aitobuild.tool_outputs import MAX_PROMPT_BYTES, MAX_TOOL_RESULT_BYTES, build_output_guard
from aitobuild.tools.github import GitHubAdapter


class _CoordinatorReceipt(DefinitionModel):
    schema_version: Literal[1] = 1
    pins: dict[str, JsonValue]
    state: Literal["running", "proposed", "failed"]
    proposal: AssignmentProposal | None = None
    diagnostics: tuple[dict[str, StrictStr], ...] = ()
    error: Annotated[StrictStr, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if (not self.pins or len(self.diagnostics) > 128 or
                (self.state == "proposed") != (self.proposal is not None) or
                (self.state == "failed") != (self.error is not None)):
            raise ValueError("Coordinator receipt requires consistent pins and outcome")
        return self


class NativeCoordinatorProposal:
    def __init__(
        self, *, runtime_for: Callable[[DefinitionSnapshot], OrganizationRuntime], state_dir: Path,
        previews: DeveloperPreviewRegistry, budget_path_for: Callable[[str], Path], event: str,
        coordinator_id: str, binding_revision: str, invoke_timeout_seconds: float = 180,
    ) -> None:
        if (not event.strip() or not coordinator_id.strip() or not binding_revision.strip() or
                not math.isfinite(invoke_timeout_seconds) or invoke_timeout_seconds <= 0):
            raise ValueError("Native coordinator requires explicit bindings and a finite positive timeout")
        self._runtime = runtime_for
        self._directory = state_dir.resolve() / "receipts"
        self._directory.mkdir(parents=True, exist_ok=True)
        self._sessions = FileSessionStore(state_dir.resolve() / "sessions")
        self._previews = previews
        self._budget_path = budget_path_for
        self._event = event
        self._coordinator = coordinator_id
        self._binding = binding_revision
        self._timeout = invoke_timeout_seconds

    @property
    def service_binding(self) -> CoordinatorBinding:
        return CoordinatorBinding(coordinator_id=self._coordinator, event=self._event, proposal=self)

    def _path(self, preview_id: str) -> Path:
        path = self._directory / (sha256(preview_id.encode()).hexdigest() + ".json")
        if path.resolve() != path:
            raise ValueError("Coordinator receipts cannot follow symlinks")
        return path

    def _save(self, path: Path, receipt: _CoordinatorReceipt) -> None:
        receipt = _CoordinatorReceipt.model_validate(receipt.model_dump())
        if path.exists():
            original = _CoordinatorReceipt.model_validate_json(path.read_text(encoding="utf-8"))
            if original.pins != receipt.pins or original.state != "running":
                raise ValueError("Coordinator pins and terminal receipts are immutable")
        temporary = path.with_suffix(".tmp")
        if temporary.resolve() != temporary:
            raise ValueError("Coordinator temporary receipts cannot follow symlinks")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(receipt.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _sync_directory(path.parent)

    async def __call__(self, snapshot: DefinitionSnapshot, preview: DeveloperPreview) -> AssignmentProposal:
        route = next((route for route in snapshot.definition.routes if self._event in route.events), None)
        team = next((team for team in snapshot.definition.teams if route is not None and team.id == route.team), None)
        selected = next((agent for agent in snapshot.definition.agents if agent.id == self._coordinator), None)
        if (route is None or route.delegation.strategy != "coordinator" or team is None or
                team.coordinator != self._coordinator or selected is None or selected.role != AgentRole.PM or
                selected.tool_profile is not None):
            raise PermissionError("Native PM coordinator must match the pinned configured route/team without persistent tools")
        if not preview.approved or preview.approved_at is None:
            raise PermissionError("Coordinator requires an already approved task")
        content = json.dumps(preview.bundle_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        bundle = developer_task_bundle_from_payload(preview.bundle_payload)
        budget_path = self._budget_path(preview.preview_id).resolve()
        budget = DeveloperTaskBudget(path=budget_path, bundle=bundle, create=False)
        budget.remaining_seconds()
        deadline = json.loads(budget_path.read_text(encoding="utf-8"))["deadline"]
        pins: dict[str, Any] = {
            "organization_id": snapshot.organization_id, "revision": snapshot.revision, "event": self._event,
            "route_id": route.id, "team_id": team.id, "workflow_id": route.workflow,
            "coordinator_id": self._coordinator, "binding_revision": self._binding,
            "preview_id": preview.preview_id, "approved_at": preview.approved_at.isoformat(),
            "scope_digest": sha256(content.encode()).hexdigest(), "bundle_content": content,
            "budget_path": str(budget_path), "deadline": deadline,
        }

        def revalidate() -> None:
            current = self._previews.get(preview.preview_id)
            if (current is None or not current.approved or current.approved_at != preview.approved_at or
                    json.dumps(current.bundle_payload, sort_keys=True, separators=(",", ":"), allow_nan=False) != content or
                    self._budget_path(preview.preview_id).resolve() != budget_path):
                raise PermissionError("Coordinator cannot replace approved scope, approval or budget binding")
            budget.remaining_seconds()
            if json.loads(budget_path.read_text(encoding="utf-8"))["deadline"] != deadline:
                raise PermissionError("Coordinator original deadline cannot change")

        path = self._path(preview.preview_id)
        with FileLock(str(path) + ".invoke.lock", timeout=0):
            revalidate()
            if path.exists():
                saved = _CoordinatorReceipt.model_validate_json(path.read_text(encoding="utf-8"))
                if json.dumps(saved.pins, sort_keys=True) != json.dumps(pins, sort_keys=True):
                    raise PermissionError("Saved coordinator proposal scope/revision/approval/binding/deadline differs")
                if saved.state == "proposed":
                    assert saved.proposal is not None
                    if saved.proposal.agent_id not in route.delegation.eligible_agents:
                        raise PermissionError("Saved proposal is outside configured eligibility")
                    return saved.proposal
                budget.abort()
                if saved.state == "running":
                    self._save(path, _CoordinatorReceipt(pins=pins, state="failed", error="Interrupted native coordinator cannot replay"))
                raise PermissionError("Interrupted/failed native coordinator cannot replay")
            runtime = self._runtime(snapshot)
            agent = runtime.agents[self._coordinator]
            if runtime.snapshot != snapshot or not isinstance(agent, Agent) or runtime.modes[self._coordinator] == "mock":
                raise PermissionError("Coordinator requires the pinned native runtime")
            message = ("Return only JSON with agent_id and rationale for this configured route. "
                       "This is a read-only proposal, not an assignment, approval or issue write.\n"
                       "Eligible agents: " + json.dumps(list(route.delegation.eligible_agents)) +
                       "\nAuthoritative approved task:\n" + content)
            if len(message.encode()) > MAX_PROMPT_BYTES:
                raise ValueError("Coordinator prompt exceeds the byte limit")
            self._save(path, _CoordinatorReceipt(pins=pins, state="running"))
            diagnostics: list[dict[str, str]] = []
            try:
                session_id = "session-" + sha256(preview.preview_id.encode()).hexdigest()[:32]
                session = agent.create_session(session_id=session_id)

                @tool(name="pm_inspect_approved_task", approval_mode="never_require")
                def inspect_task() -> dict[str, Any]:
                    revalidate()
                    return {"task_id": bundle.task_id, "scope_digest": pins["scope_digest"],
                            "event": self._event, "eligible_agents": list(route.delegation.eligible_agents)}

                @function_middleware
                async def guarded_tool(call: FunctionInvocationContext, call_next: Callable) -> None:
                    revalidate()
                    try:
                        await call_next()
                    except Exception as error:
                        diagnostics.append({"tool": call.function.name, "error": str(error)[:2000]})
                        raise
                    finally:
                        del diagnostics[:-128]

                async with asyncio.timeout(min(self._timeout, budget.remaining_seconds())):
                    response = await agent.run(message, session=session, tools=(inspect_task,), options={"store": False},
                                               middleware=[guarded_tool, build_output_guard(path.parent / "outputs" / path.stem)])
                revalidate()
                if response.user_input_requests or len(response.text.encode()) > MAX_PROMPT_BYTES:
                    raise PermissionError("Coordinator cannot accept native approval requests or oversized proposals")
                proposal = AssignmentProposal.model_validate_json(response.text)
                if proposal.agent_id not in route.delegation.eligible_agents:
                    raise PermissionError("Native coordinator proposed an ineligible agent")
                await self._sessions.set(session_id, session)
                revalidate()
                self._save(path, _CoordinatorReceipt(pins=pins, state="proposed", proposal=proposal, diagnostics=tuple(diagnostics)))
                return proposal
            except BaseException as error:
                budget.abort()
                self._save(path, _CoordinatorReceipt(pins=pins, state="failed", diagnostics=tuple(diagnostics),
                                                     error=(str(error) or type(error).__name__)[:2000]))
                raise


class NativeManagedRoles:
    def __init__(
        self, *, runtime_for: Callable[[DefinitionSnapshot], OrganizationRuntime], state_dir: Path,
        proposal_event: str, worker: DeveloperDeliveryWorker | None = None, github: GitHubAdapter | None = None,
        review_target_for: Callable[[ManagedTaskContext], PublishedReviewTarget] | None = None,
        invoke_timeout_seconds: float = 180,
    ) -> None:
        if not proposal_event.strip() or not math.isfinite(invoke_timeout_seconds) or invoke_timeout_seconds <= 0:
            raise ValueError("Managed roles require an explicit proposal route and positive timeout")
        self._runtime = runtime_for
        self._state_dir = state_dir.resolve()
        self._sessions = FileSessionStore(self._state_dir / "sessions")
        self._proposal_event = proposal_event
        self._worker = worker
        self._github = github
        self._target_for = review_target_for
        self._timeout = invoke_timeout_seconds

    @property
    def operations(self) -> Mapping[str, ManagedOperation]:
        operations = {"pm_propose_assignment": ManagedOperation(self.propose, role=AgentRole.PM)}
        if self._worker is not None and self._github is not None and self._target_for is not None:
            operations["architect_review_published"] = ManagedOperation(
                self.review, role=AgentRole.ARCHITECT, action=ActionClass.PR_REVIEW, on_response=self.approve_review,
            )
        return operations

    def _agent(self, context: ManagedTaskContext, role: AgentRole) -> Agent:
        assignment = context.revalidate()
        runtime = self._runtime(context.snapshot)
        selected = next(agent for agent in context.snapshot.definition.agents if agent.id == assignment.agent_id)
        agent = runtime.agents[selected.id]
        if (runtime.snapshot != context.snapshot or selected.role != role or selected.tool_profile is not None or
                not isinstance(agent, Agent) or runtime.modes[selected.id] == "mock"):
            raise PermissionError("Managed role requires its pinned native owner without persistent tool profiles")
        return agent

    async def _session(self, context: ManagedTaskContext, agent: Agent, extra: dict[str, Any]) -> AgentSession:
        assignment = context.revalidate()
        pins = {"assignment_id": assignment.assignment_id, "revision": assignment.revision,
                "scope_digest": assignment.scope_digest, "budget_path": assignment.budget_path, **extra}
        session = await self._sessions.get(context.run.session_id)
        if session is None:
            session = agent.create_session(session_id=context.run.session_id)
            session.state["aitobuild_role_pins"] = pins
            await self._sessions.set(context.run.session_id, session)
        if session.session_id != context.run.session_id or session.state.get("aitobuild_role_pins") != pins:
            raise PermissionError("Managed role session cannot switch scope, revision, target or owner")
        return session

    async def _invoke(self, context: ManagedTaskContext, agent: Agent, session: AgentSession,
                      tools: tuple[Any, ...], guidance: str) -> str:
        message = guidance + "\nAuthoritative approved task:\n" + context.assignment.bundle_content
        if len(message.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("Managed role prompt exceeds the byte limit")
        diagnostics = session.state.setdefault("aitobuild_role_diagnostics", [])

        @function_middleware
        async def guarded_tool(call: FunctionInvocationContext, call_next: Callable) -> None:
            context.revalidate()
            try:
                await call_next()
            except Exception as error:
                diagnostics.append({"tool": call.function.name, "error": str(error)[:2000]})
                raise
            finally:
                del diagnostics[:-128]

        async with asyncio.timeout(min(self._timeout, context.remaining_seconds())):
            response = await agent.run(message, session=session, tools=tools, options={"store": False},
                                       middleware=[guarded_tool, build_output_guard(self._state_dir / "outputs" / context.run.session_id)])
        context.revalidate()
        if response.user_input_requests:
            raise PermissionError("Unsupported native input cannot become managed role service approval")
        if len(response.text.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("Managed role response exceeds the byte limit")
        await self._sessions.set(context.run.session_id, session)
        return response.text

    async def propose(self, context: ManagedTaskContext, message: Any) -> dict[str, Any]:
        agent = self._agent(context, AgentRole.PM)
        route = next((route for route in context.snapshot.definition.routes if self._proposal_event in route.events), None)
        if route is None:
            raise ValueError("PM proposal event is not present in the pinned definition")
        session = await self._session(context, agent, {"role": "pm", "proposal_event": self._proposal_event})

        @tool(name="pm_inspect_approved_task", approval_mode="never_require")
        def inspect_task() -> dict[str, Any]:
            assignment = context.revalidate()
            return {"task": json.loads(assignment.bundle_content), "proposal_event": self._proposal_event,
                    "eligible_agents": list(route.delegation.eligible_agents)}

        text = await self._invoke(context, agent, session, (inspect_task,),
                                  "Return only JSON with agent_id and rationale for the configured proposal route. "
                                  "This is a read-only proposal, not an assignment, approval or issue write.")
        proposal = AssignmentProposal.model_validate_json(text)
        if proposal.agent_id not in route.delegation.eligible_agents:
            raise PermissionError("Native PM proposal is outside the configured eligible agents")
        session.state["aitobuild_role_proposal"] = proposal.model_dump(mode="json")
        await self._sessions.set(context.run.session_id, session)
        return {"proposal": proposal.model_dump(mode="json"), "event": self._proposal_event, "metadata_only": True}

    async def _target(self, context: ManagedTaskContext) -> dict[str, Any]:
        assignment = context.revalidate()
        if self._worker is None or self._github is None or self._target_for is None:
            raise ValueError("Architect review bindings are not configured")
        worker, github = self._worker, self._github
        target = PublishedReviewTarget.model_validate(self._target_for(context).model_dump())
        record = await _delivery_call(lambda: worker.get(target.preview_id))
        issue = assignment.bundle.issue_context
        published_issue = record.bundle_payload.get("issue_context") if record is not None else None
        if (record is None or record.state != "published" or not isinstance(published_issue, dict) or issue is None or
                (issue.repository, issue.repository_id, issue.issue_number, issue.issue_id, issue.base_revision) !=
                tuple(published_issue.get(key) for key in ("repository", "repository_id", "issue_number", "issue_id", "base_revision"))):
            raise PermissionError("Review target differs from the approved repository/issue/base scope")
        publication = record.publication
        if not isinstance(publication, dict) or publication["head_sha"] != target.head_sha:
            raise PermissionError("Operator-pinned review head changed")
        if not all(is_path_allowed(path, policy=assignment.bundle.policy) for path in publication["changed_paths"]):
            raise PermissionError("Published changes escape the review task's approved paths")
        pull = await _delivery_call(lambda: worker.get_published_pull_request(target.preview_id, github=github))
        if not pull["head_matches_publication"]:
            raise PermissionError("Published review head is stale")
        context.revalidate()
        return {"preview_id": target.preview_id, "head_sha": target.head_sha, "repository": publication["repository"],
                "pull_number": publication["pull_number"], "base_sha": publication["base_sha"],
                "changed_paths": publication["changed_paths"], "blob_shas": publication["blob_shas"],
                "file_modes": publication["file_modes"]}

    @staticmethod
    def _require_inspections(target: dict[str, Any], inspections: Any) -> None:
        if not isinstance(inspections, dict):
            raise PermissionError("Review requires saved complete source and diff inspection")
        for path in target["changed_paths"]:
            for kind in ("source", "diff"):
                receipt = inspections.get(kind + ":" + path)
                if (not isinstance(receipt, dict) or receipt.get("complete") is not True or
                        type(receipt.get("end")) is not int or receipt["end"] < 0):
                    raise PermissionError("Review requires complete source and diff inspection, not metadata alone")

    async def review(self, context: ManagedTaskContext, message: Any) -> WorkflowInput:
        agent = self._agent(context, AgentRole.ARCHITECT)
        target = await self._target(context)
        session = await self._session(context, agent, {"role": "architect", "target": target})
        if session.state.get("aitobuild_comment_state") is not None:
            raise PermissionError("Saved review must continue through its exact approval; model replay is blocked")
        inspections = session.state.setdefault("aitobuild_review_inspections", {})

        async def read(kind: str, path: str, offset: int, max_bytes: int) -> dict[str, Any]:
            if await self._target(context) != target:
                raise PermissionError("Review target pins changed during inspection")
            assert self._worker is not None and self._github is not None
            github = self._github
            key = kind + ":" + path
            prior = inspections.get(key, {"end": 0, "complete": False})
            if offset > prior["end"]:
                raise PermissionError("Review inspection cannot skip unread byte pages")
            call = self._worker.get_published_source if kind == "source" else self._worker.get_published_diff
            result = await _delivery_call(lambda: call(target["preview_id"], github=github, path=path,
                                                       offset=offset, max_bytes=max_bytes))
            if result["head_sha"] != target["head_sha"]:
                raise PermissionError("Inspected source differs from the pinned review head")
            if len(json.dumps(result).encode("utf-8")) > MAX_TOOL_RESULT_BYTES - 256:
                raise ValueError("Review page must fit inline; reduce max_bytes before recording inspection")
            end = result["next_offset"] if result["truncated"] else result["total_bytes"]
            inspections[key] = {"end": max(prior["end"], end),
                                "complete": prior["complete"] or not result["truncated"]}
            context.revalidate()
            await self._sessions.set(context.run.session_id, session)
            return result

        @tool(name="architect_read_published_source", approval_mode="never_require")
        async def read_source(path: str, offset: Annotated[int, Field(ge=0)] = 0,
                              max_bytes: Annotated[int, Field(ge=1, le=1000)] = 1000) -> dict[str, Any]:
            return await read("source", path, offset, max_bytes)

        @tool(name="architect_read_published_diff", approval_mode="never_require")
        async def read_diff(path: str, offset: Annotated[int, Field(ge=0)] = 0,
                            max_bytes: Annotated[int, Field(ge=1, le=1000)] = 1000) -> dict[str, Any]:
            return await read("diff", path, offset, max_bytes)

        text = await self._invoke(context, agent, session, (read_source, read_diff),
                                  "Inspect complete source AND diff for each approved changed path at the pinned target below. "
                                  "Return COMMENT review text only; never claim metadata alone proves semantic correctness. "
                                  "Publication requires a separate operator approval.\nTarget: " + json.dumps(target, sort_keys=True))
        self._require_inspections(target, inspections)
        assert self._worker is not None
        text = text.replace("\x00", "").strip()
        self._worker._normalize_architect_review_body(text)
        if await self._target(context) != target:
            raise PermissionError("Review target changed before proposal approval")
        session.state["aitobuild_comment_state"] = "proposed"
        session.state["aitobuild_comment_body"] = text
        await self._sessions.set(context.run.session_id, session)
        return WorkflowInput("Approve exact Architect COMMENT review", {"target": target, "body": text}, "service_approval")

    async def approve_review(self, context: ManagedTaskContext, original: Any, approved: Any) -> dict[str, Any]:
        self._agent(context, AgentRole.ARCHITECT)
        if type(approved) is not bool or not approved:
            raise PermissionError("Operator rejected or omitted Architect COMMENT approval")
        target = await self._target(context)
        session = await self._sessions.get(context.run.session_id)
        expected_pins = {"assignment_id": context.assignment.assignment_id, "revision": context.assignment.revision,
                         "scope_digest": context.assignment.scope_digest, "budget_path": context.assignment.budget_path,
                         "role": "architect", "target": target}
        if (session is None or session.state.get("aitobuild_role_pins") != expected_pins or
                session.state.get("aitobuild_comment_state") != "proposed" or original !=
                {"target": target, "body": session.state.get("aitobuild_comment_body")}):
            raise PermissionError("Architect approval must retain the exact saved target/body and one-shot state")
        self._require_inspections(target, session.state.get("aitobuild_review_inspections"))
        session.state["aitobuild_comment_state"] = "submitting"
        await self._sessions.set(context.run.session_id, session)
        assert self._worker is not None and self._github is not None
        worker, github = self._worker, self._github
        record = await _delivery_call(lambda: worker.submit_architect_review(
            target["preview_id"], github=github, event="COMMENT", body=original["body"], expected_target=target,
            before_submit=context.revalidate,
        ))
        session.state["aitobuild_comment_state"] = "submitted"
        session.state["aitobuild_comment_receipt"] = record.architect_review
        await self._sessions.set(context.run.session_id, session)
        context.revalidate()
        return {"target": target, "architect_review": record.architect_review, "metadata_only": True}

    async def cleanup(self, context: ManagedTaskContext) -> None:
        session = await self._sessions.get(context.run.session_id)
        if session is not None:
            session.state["aitobuild_role_cleanup_succeeded"] = True
            await self._sessions.set(context.run.session_id, session)