"""Operator adapters connecting configured graphs to existing approved delivery services."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Protocol

from agent_framework import (
    Agent, AgentSession, Content, FileSessionStore, FunctionInvocationContext, Message,
    function_middleware,
)

from aitobuild.agent_tools import DeveloperToolContext, build_developer_tools
from aitobuild.developer_delivery import DeliveryPreparation, DeveloperDeliveryWorker
from aitobuild.developer_isolation import DeveloperTaskBudget
from aitobuild.organization import DefinitionSnapshot
from aitobuild.organization_runner import ManagedOperation, ManagedTaskContext, WorkflowInput
from aitobuild.organization_runtime import OrganizationRuntime
from aitobuild.policy import ActionClass, AgentRole
from aitobuild.tool_outputs import MAX_PROMPT_BYTES, build_output_guard
from aitobuild.tools.bash import ContainerSessionBashAdapter
from aitobuild.tools.github import GitHubAdapter


@dataclass(frozen=True, slots=True)
class DeliveryInvocation:
    session_id: str
    pending_request_id: str | None = None


class ApprovedDeliveryImplementation(Protocol):
    async def run(self, context: ManagedTaskContext) -> DeliveryInvocation: ...

    async def resume(
        self, context: ManagedTaskContext, *, request_id: str, approved: bool,
    ) -> DeliveryInvocation: ...

    async def cleanup(self, context: ManagedTaskContext) -> bool: ...


async def _delivery_call(call: Callable[[], Any]) -> Any:
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class NativeDeliveryImplementation:
    def __init__(
        self, *, worker: DeveloperDeliveryWorker,
        runtime_for: Callable[[DefinitionSnapshot], OrganizationRuntime],
        tools: DeveloperToolContext, state_dir: Path, invoke_timeout_seconds: float = 180,
    ) -> None:
        if (not isinstance(tools.container_session_adapter, ContainerSessionBashAdapter)
                or tools.enable_mcp_adapters or tools.mcp_tool_adapter is not None or tools.use_legacy_patch_tool):
            raise PermissionError("Managed native delivery requires constrained Docker and exact-span tools")
        if invoke_timeout_seconds <= 0:
            raise ValueError("Native invocation timeout must be positive")
        self._worker = worker
        self._runtime = runtime_for
        self._tools = tools
        self._state_dir = state_dir.resolve()
        self._sessions = FileSessionStore(self._state_dir / "sessions")
        self._timeout = invoke_timeout_seconds

    async def run(self, context: ManagedTaskContext) -> DeliveryInvocation:
        return await self._invoke(context)

    async def resume(
        self, context: ManagedTaskContext, *, request_id: str, approved: bool,
    ) -> DeliveryInvocation:
        return await self._invoke(context, request_id=request_id, approved=approved)

    async def _invoke(
        self, context: ManagedTaskContext, *, request_id: str | None = None, approved: bool | None = None,
    ) -> DeliveryInvocation:
        assignment = context.revalidate()
        if str(self._worker.budget_path(assignment.preview_id)) != assignment.budget_path:
            raise PermissionError("Native delivery must reopen its original ledger")
        runtime = self._runtime(context.snapshot)
        if runtime.snapshot != context.snapshot:
            raise PermissionError("Native implementation runtime differs from the pinned revision")
        selected = next(agent for agent in context.snapshot.definition.agents if agent.id == assignment.agent_id)
        agent = runtime.agents[selected.id]
        if selected.role != AgentRole.DEVELOPER or not isinstance(agent, Agent) or runtime.modes[selected.id] == "mock":
            raise PermissionError("Native implementation requires its configured native Developer")
        session_id = context.run.session_id
        pins = {"assignment_id": assignment.assignment_id, "revision": assignment.revision,
                "scope_digest": assignment.scope_digest, "preview_id": assignment.preview_id,
                "budget_path": assignment.budget_path}
        session = await self._sessions.get(session_id)
        if session is None:
            if request_id is not None:
                raise ValueError("Saved native approval session is missing")
            session = agent.create_session(session_id=session_id)
            session.state["aitobuild_managed_pins"] = pins
        elif session.state.get("aitobuild_managed_pins") != pins:
            raise PermissionError("Native session cannot switch task, owner or revision")
        if session.session_id != session_id:
            raise PermissionError("Native session identity differs from the managed run")
        pending = session.state.get("aitobuild_pending_approvals", [])
        if request_id is None and pending:
            raise ValueError("Native implementation requires its saved approval continuation")
        message: Any = "Approved task bundle (authoritative scope):\n" + assignment.bundle_content
        if request_id is not None:
            if type(approved) is not bool:
                raise PermissionError("Native approval requires an explicit Boolean decision")
            matches = [item for item in pending if item.get("id") == request_id]
            if len(matches) != 1:
                raise PermissionError("Native tool request is not pending in this session")
            request = Content.from_dict(matches[0])
            if request.type != "function_approval_request":
                raise PermissionError("Saved input is not a native tool approval")
            message = Message("user", [request.to_function_approval_response(approved=approved)])
        elif len(message.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("Approved task exceeds the native prompt byte limit")
        budget = DeveloperTaskBudget(path=Path(assignment.budget_path), bundle=assignment.bundle, create=False)
        with self._worker.implementation_lock(assignment.preview_id):
            record = self._worker.begin_implementation(
                assignment.preview_id, bundle=assignment.bundle, session_id=session_id, resume=request_id is not None,
            )
            try:
                if request_id is not None:
                    session.state["aitobuild_pending_approvals"] = [item for item in pending if item.get("id") != request_id]
                await self._sessions.set(session_id, session)
                if approved is False:
                    raise PermissionError("Operator rejected native tool approval")
                adapter = self._tools.container_session_adapter
                if adapter is None:
                    raise PermissionError("Native delivery requires constrained Docker")
                workspace = Path(record.checkout_path)
                adapter.bind_session_workspace(session_id=session_id, workspace=workspace)
                output_dir = self._state_dir / "outputs" / session_id
                tools = build_developer_tools(context=replace(
                    self._tools, bound_session_id=session_id, isolation_policy=assignment.bundle.policy,
                    task_budget=budget, prepared_workspace=workspace, output_dir=output_dir,
                    require_human_approval_for_repo_writes=True, use_legacy_patch_tool=False,
                ))
                diagnostics = session.state.setdefault("aitobuild_managed_diagnostics", [])

                @function_middleware
                async def guarded_tool(call: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
                    context.revalidate()
                    try:
                        await call_next()
                        contents = call.result
                        text = "\n".join(getattr(item, "text", "") or "" for item in contents) if isinstance(contents, list) else str(contents)
                        try:
                            result = json.loads(text)
                        except ValueError:
                            result = None
                        if isinstance(result, dict) and (result.get("ok") is False or result.get("isError") is True):
                            diagnostics.append({"tool": call.function.name, "result": text[:2000]})
                    except Exception as error:
                        diagnostics.append({"tool": call.function.name, "error": str(error)[:2000]})
                        raise
                    finally:
                        del diagnostics[:-128]

                async with asyncio.timeout(min(self._timeout, context.remaining_seconds())):
                    response = await agent.run(
                        message, session=session, tools=tools, options={"store": False},
                        middleware=[guarded_tool, build_output_guard(output_dir)],
                    )
                context.revalidate()
                requests = response.user_input_requests
                if any(request.type != "function_approval_request" for request in requests):
                    raise PermissionError("Unsupported native human input cannot become service approval")
                session.state["aitobuild_pending_approvals"] = [request.to_dict() for request in requests]
                session.state["aitobuild_managed_output"] = response.text[:32000]
                await self._sessions.set(session_id, session)
                if not requests and not await self.cleanup(context):
                    raise RuntimeError("Native implementation cleanup failed")
                self._worker.finish_implementation(assignment.preview_id, session_id=session_id, pending=bool(requests))
                return DeliveryInvocation(session_id, requests[0].id if requests else None)
            except BaseException as error:
                self._worker.finish_implementation(
                    assignment.preview_id, session_id=session_id, error=str(error) or type(error).__name__,
                )
                raise

    async def cleanup(self, context: ManagedTaskContext) -> bool:
        adapter = self._tools.container_session_adapter
        if adapter is None:
            return False
        closed = await _delivery_call(lambda: adapter.close_session(session_id=context.run.session_id))
        if closed is not True and context.run.session_id in await _delivery_call(adapter.list_session_ids):
            return False
        session = await self._sessions.get(context.run.session_id)
        if isinstance(session, AgentSession):
            session.state["aitobuild_managed_cleanup_succeeded"] = True
            await self._sessions.set(context.run.session_id, session)
        return True


class ManagedDeliveryBindings:
    def __init__(
        self, *, worker: DeveloperDeliveryWorker, implementation: ApprovedDeliveryImplementation,
        verification_adapter: ContainerSessionBashAdapter, github: GitHubAdapter | None = None,
        allow_mock_publication: bool = False,
    ) -> None:
        self._worker = worker
        self._implementation = implementation
        self._verifier = verification_adapter
        self._github = github
        self._allow_mock = allow_mock_publication

    @property
    def operations(self) -> Mapping[str, ManagedOperation]:
        operations = {
            "delivery_prepare": ManagedOperation(self.prepare),
            "delivery_implement": ManagedOperation(
                self.implement, action=ActionClass.REPO_WRITE, on_response=self.continue_implementation,
            ),
            "delivery_verify": ManagedOperation(self.verify),
        }
        if self._github is not None:
            operations["delivery_publish"] = ManagedOperation(self.publish, action=ActionClass.REPO_WRITE)
        return operations

    def _check(self, context: ManagedTaskContext) -> None:
        assignment = context.revalidate()
        if str(self._worker.budget_path(assignment.preview_id).resolve()) != assignment.budget_path:
            raise PermissionError("Managed delivery must reuse the assignment's original budget")

    def _record(self, context: ManagedTaskContext, record: DeliveryPreparation, expected: str) -> dict[str, Any]:
        self._check(context)
        if (record.state != expected or record.preview_id != context.assignment.preview_id
                or record.bundle_payload != json.loads(context.assignment.bundle_content)
                or record.approved_at != context.assignment.approved_at.isoformat()):
            raise ValueError(record.error or "Delivery did not reach the required approved stage")
        return {"preview_id": record.preview_id, "state": record.state}

    async def prepare(self, context: ManagedTaskContext, message: Any) -> dict[str, Any]:
        self._check(context)
        record = await _delivery_call(lambda: self._worker.prepare(context.assignment.preview_id))
        return self._record(context, record, "prepared")

    async def implement(self, context: ManagedTaskContext, message: Any) -> dict[str, Any] | WorkflowInput:
        self._check(context)
        return self._implementation_result(context, await self._implementation.run(context))

    async def continue_implementation(
        self, context: ManagedTaskContext, original: Any, approved: Any,
    ) -> dict[str, Any] | WorkflowInput:
        self._check(context)
        if type(approved) is not bool:
            raise PermissionError("Native tool continuation requires a service approval decision")
        invocation = await self._implementation.resume(context, request_id=original["request_id"], approved=approved)
        if not approved:
            raise PermissionError("Operator rejected native tool approval; automatic replay is blocked")
        return self._implementation_result(context, invocation)

    def _implementation_result(
        self, context: ManagedTaskContext, invocation: DeliveryInvocation,
    ) -> dict[str, Any] | WorkflowInput:
        record = self._worker.get(context.assignment.preview_id)
        if (record is None or invocation.session_id != context.run.session_id
                or record.session_id != context.run.session_id):
            raise PermissionError("Implementation must use its pinned native delivery session")
        if invocation.pending_request_id is not None:
            self._record(context, record, "awaiting_tool_approval")
            if not invocation.pending_request_id.strip():
                raise ValueError("Saved native tool approval requires a request identity")
            return WorkflowInput(
                "Native delivery tool approval", {"request_id": invocation.pending_request_id}, "service_approval",
            )
        return self._record(context, record, "implemented")

    async def verify(self, context: ManagedTaskContext, message: Any) -> dict[str, Any]:
        self._check(context)
        record = await _delivery_call(lambda: self._worker.verify(context.assignment.preview_id, adapter=self._verifier))
        result = self._record(context, record, "verified")
        evidence = record.verification
        if (evidence is None or evidence.get("cleanup_succeeded") is not True
                or not evidence.get("commands") or any(
                    type(command.get("exit_code")) is not int or command["exit_code"] != 0
                    for command in evidence["commands"]
                )):
            raise ValueError("Independent verification lacks successful exit/cleanup evidence")
        return result

    async def publish(self, context: ManagedTaskContext, message: Any) -> dict[str, Any]:
        self._check(context)
        github = self._github
        if github is None:
            raise PermissionError("Publication is not registered by the operator")
        record = await _delivery_call(lambda: self._worker.publish(
            context.assignment.preview_id, github=github,
            require_human_approval_for_repo_writes=True, allow_mock_publication=self._allow_mock,
        ))
        return self._record(context, record, "published")

    async def cleanup(self, context: ManagedTaskContext) -> None:
        if await self._implementation.cleanup(context) is not True:
            raise RuntimeError("Managed native delivery cleanup did not succeed")
        record = self._worker.get(context.assignment.preview_id)
        if record is not None and record.state == "implementing" and record.session_id == context.run.session_id:
            await _delivery_call(lambda: self._worker.finish_implementation(
                context.assignment.preview_id, session_id=context.run.session_id,
                error="Managed invocation stopped; automatic replay is blocked",
            ))