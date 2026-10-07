"""Revision-pinned native execution with conservative, receipt-based recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from inspect import isawaitable, iscoroutinefunction
import json
import os
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from agent_framework import (
    Executor, FileCheckpointStorage, WorkflowCheckpoint, WorkflowContext,
    WorkflowRunState, handler, response_handler,
)
from filelock import FileLock
from pydantic import AwareDatetime, JsonValue, StrictBool, StrictStr, TypeAdapter, model_validator

from aitobuild.developer_isolation import DeveloperTaskBudget
from aitobuild.organization import DefinitionModel, DefinitionSnapshot, DefinitionStore, Identifier
from aitobuild.organization_assignments import (
    AssignmentProposal, AssignmentService, AssignmentStore, Digest, TaskAssignment,
)
from aitobuild.organization_runtime import (
    OrganizationRuntime, WorkflowLimits, WorkflowOperation, WorkflowPredicate,
    _admit_graph, _build_native_workflow,
)
from aitobuild.policy import ActionClass, AgentRole, assert_role_action_allowed


RunState = Literal["ready", "running", "waiting", "finalizing", "completed", "failed", "cancelled"]
InputKind = Literal["human_input", "service_approval"]
TERMINAL = {"completed", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class RuntimeActor:
    kind: Literal["operator", "agent"]
    actor_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"operator", "agent"} or not self.actor_id.strip():
            raise ValueError("Trusted runtime actor requires a kind and identity")


class PendingWorkflowInput(DefinitionModel):
    request_id: StrictStr
    node_id: Identifier
    kind: InputKind
    prompt: StrictStr
    data: JsonValue


class WorkflowDecision(DefinitionModel):
    request_id: StrictStr
    kind: InputKind
    actor_id: StrictStr
    value: JsonValue


class ManagedRun(DefinitionModel):
    run_id: Identifier
    assignment_id: Identifier
    assignment_digest: Digest
    organization_id: Identifier
    revision: Digest
    workflow_id: Identifier
    workflow_name: StrictStr
    binding_revision: Identifier
    scope_digest: Digest
    session_id: Identifier
    input: JsonValue
    created_at: AwareDatetime
    updated_at: AwareDatetime
    state: RunState = "ready"
    checkpoint_id: StrictStr | None = None
    checkpoint_digest: Digest | None = None
    pending: tuple[PendingWorkflowInput, ...] = ()
    decisions: tuple[WorkflowDecision, ...] = ()
    outputs: tuple[JsonValue, ...] = ()
    error: StrictStr | None = None
    cleanup_succeeded: StrictBool | None = None
    terminal_state: Literal["completed", "failed", "cancelled"] | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.checkpoint_id is None) != (self.checkpoint_digest is None):
            raise ValueError("Run checkpoint identity requires an integrity pin")
        if self.state == "waiting" and (not self.pending or self.checkpoint_id is None):
            raise ValueError("Waiting runs require checkpointed requests")
        if self.state != "waiting" and self.pending:
            raise ValueError("Only waiting runs may carry pending requests")
        if len({item.request_id for item in self.pending}) != len(self.pending):
            raise ValueError("Run requests must be unique")
        if len({item.request_id for item in self.decisions}) != len(self.decisions):
            raise ValueError("Run decisions cannot be consumed twice")
        if set(item.request_id for item in self.pending) & set(item.request_id for item in self.decisions):
            raise ValueError("Consumed requests cannot remain pending")
        if self.state in TERMINAL | {"finalizing"} and self.cleanup_succeeded is None:
            raise ValueError("Terminal runs require a cleanup receipt")
        if (self.state == "finalizing") != (self.terminal_state is not None):
            raise ValueError("Finalization requires exactly one recorded terminal intent")
        if self.state == "completed" and (self.error is not None or self.cleanup_succeeded is not True):
            raise ValueError("Completed runs require successful cleanup")
        return self


class RunStore(Protocol):
    def get(self, assignment_id: str) -> ManagedRun | None: ...

    def save(self, run: ManagedRun) -> None: ...

    def invocation_lock(self, assignment_id: str) -> FileLock: ...

    def checkpoint_path(self, assignment_id: str) -> Path: ...


class FileRunStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory.resolve()
        self._directory.mkdir(parents=True, exist_ok=True)

    def _task_path(self, assignment_id: str) -> Path:
        TypeAdapter(Identifier).validate_python(assignment_id)
        path = self._directory / sha256(assignment_id.encode()).hexdigest()
        if path.resolve() != path:
            raise ValueError("Managed run storage cannot follow symlinks")
        return path

    def invocation_lock(self, assignment_id: str) -> FileLock:
        return FileLock(str(self._task_path(assignment_id)) + ".invoke.lock", timeout=0)

    def checkpoint_path(self, assignment_id: str) -> Path:
        path = self._task_path(assignment_id) / "checkpoints"
        if path.resolve() != path:
            raise ValueError("Managed checkpoints cannot follow symlinks")
        return path

    def get(self, assignment_id: str) -> ManagedRun | None:
        path = self._task_path(assignment_id) / "run.json"
        if path.resolve() != path:
            raise ValueError("Managed receipts cannot follow symlinks")
        with FileLock(str(path.parent) + ".state.lock", timeout=10):
            if not path.exists():
                return None
            run = ManagedRun.model_validate_json(path.read_text(encoding="utf-8"))
            if run.assignment_id != assignment_id:
                raise ValueError("Managed run assignment address differs")
            return run

    def save(self, run: ManagedRun) -> None:
        run = ManagedRun.model_validate(run.model_dump())
        directory = self._task_path(run.assignment_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "run.json"
        if path.resolve() != path:
            raise ValueError("Managed receipts cannot follow symlinks")
        with FileLock(str(directory) + ".state.lock", timeout=10):
            if path.exists():
                original = ManagedRun.model_validate_json(path.read_text(encoding="utf-8"))
                mutable = {"updated_at", "state", "checkpoint_id", "checkpoint_digest", "pending",
                           "decisions", "outputs", "error", "cleanup_succeeded", "terminal_state"}
                if original.model_dump(exclude=mutable) != run.model_dump(exclude=mutable):
                    raise ValueError("Managed run identity and pins are immutable")
                if original.state in TERMINAL and original != run:
                    raise ValueError("Terminal managed receipts are immutable")
                transitions = {
                    "ready": {"ready", "running", "finalizing"},
                    "running": {"running", "waiting", "finalizing"},
                    "waiting": {"waiting", "running", "finalizing"},
                    "finalizing": {"finalizing", "completed", "failed", "cancelled"},
                }
                if original.state not in TERMINAL and run.state not in transitions[original.state]:
                    raise ValueError("Managed run state cannot rewind or skip finalization")
                if run.decisions[:len(original.decisions)] != original.decisions:
                    raise ValueError("Consumed workflow decisions are immutable")
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(run.model_dump_json())
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            _sync_directory(directory)
            _sync_directory(self._directory)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assignment_digest(assignment: TaskAssignment) -> str:
    return sha256(assignment.model_dump_json(exclude={"state", "closed_at", "outcome"}).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ManagedTaskContext:
    run: ManagedRun
    assignment: TaskAssignment
    snapshot: DefinitionSnapshot
    revalidate: Callable[[], TaskAssignment]

    def remaining_seconds(self) -> float:
        assignment = self.revalidate()
        return DeveloperTaskBudget(
            path=Path(assignment.budget_path), bundle=assignment.bundle, create=False,
        ).remaining_seconds()


@dataclass(frozen=True, slots=True)
class WorkflowInput:
    prompt: str
    data: JsonValue
    kind: InputKind = "human_input"


@dataclass(frozen=True, slots=True)
class ManagedOperation:
    handler: Callable[[ManagedTaskContext, Any], Any]
    role: AgentRole = AgentRole.DEVELOPER
    action: ActionClass = ActionClass.READ_ONLY
    on_response: Callable[[ManagedTaskContext, Any, Any], Any] | None = None

    def __post_init__(self) -> None:
        assert_role_action_allowed(self.role, self.action)
        if not callable(self.handler) or (self.on_response is not None and not callable(self.on_response)):
            raise ValueError("Managed operation handlers must be callable")


async def _resolve(value: Any) -> Any:
    return await value if isawaitable(value) else value


class _ManagedExecutor(Executor):
    def __init__(self, node_id: str, operation: ManagedOperation, context: ManagedTaskContext) -> None:
        self._operation = operation
        self._context = context
        super().__init__(id=node_id)

    async def _emit(self, result: Any, ctx: WorkflowContext[Any, Any]) -> None:
        self._context.revalidate()
        if isinstance(result, WorkflowInput):
            if self._operation.on_response is None or result.kind not in {"human_input", "service_approval"}:
                raise ValueError("Workflow input requires an explicit continuation handler and kind")
            await ctx.request_info(request_data={"kind": result.kind, "prompt": result.prompt,
                                                 "data": result.data}, response_type=dict)
        else:
            TypeAdapter(JsonValue).validate_python(result)
            await ctx.send_message(result)
            await ctx.yield_output(result)

    @handler
    async def invoke(self, message: Any, ctx: WorkflowContext[Any, Any]) -> None:
        self._context.revalidate()
        await self._emit(await _resolve(self._operation.handler(self._context, message)), ctx)

    @response_handler
    async def respond(self, original_request: dict, response: dict, ctx: WorkflowContext[Any, Any]) -> None:
        self._context.revalidate()
        if self._operation.on_response is None or response.get("kind") != original_request["kind"]:
            raise PermissionError("Native input cannot stand in for service approval")
        result = self._operation.on_response(self._context, original_request["data"], response["value"])
        await self._emit(await _resolve(result), ctx)


class _RunCheckpoints(FileCheckpointStorage):
    def __init__(self, path: Path, context: ManagedTaskContext) -> None:
        super().__init__(path)
        self.path = path
        self.context = context
        self.latest: str | None = None

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        self.context.revalidate()
        checkpoint.metadata["aitobuild"] = {
            "run_id": self.context.run.run_id, "assignment_id": self.context.assignment.assignment_id,
            "revision": self.context.assignment.revision, "scope_digest": self.context.assignment.scope_digest,
        }
        checkpoint_id = await super().save(checkpoint)
        with (self.path / (checkpoint_id + ".json")).open("rb") as handle:
            os.fsync(handle.fileno())
        _sync_directory(self.path)
        _sync_directory(self.path.parent)
        self.latest = checkpoint_id
        return checkpoint_id

    def digest(self, checkpoint_id: str) -> str:
        if not checkpoint_id or Path(checkpoint_id).name != checkpoint_id:
            raise ValueError("Invalid checkpoint identity")
        path = self.path / (checkpoint_id + ".json")
        if path.resolve() != path:
            raise ValueError("Checkpoint path cannot follow symlinks")
        return sha256(path.read_bytes()).hexdigest()


class ManagedWorkflowRunner:
    def __init__(
        self, *, definitions: DefinitionStore, assignments: AssignmentStore,
        assignment_service: AssignmentService, runs: RunStore,
        actor_provider: Callable[[], RuntimeActor], operations: Mapping[str, ManagedOperation],
        cleanup: Callable[[ManagedTaskContext], Awaitable[None]],
        binding_revision: str,
        predicates: Mapping[str, WorkflowPredicate] | None = None,
        limits: WorkflowLimits = WorkflowLimits(),
    ) -> None:
        self._definitions = definitions
        self._assignments = assignments
        self._service = assignment_service
        self._runs = runs
        self._actor = actor_provider
        self._operations = dict(operations)
        self._cleanup = cleanup
        self._predicates = dict(predicates or {})
        self._limits = limits
        self._binding_revision = TypeAdapter(Identifier).validate_python(binding_revision)
        for name in (*self._operations, *self._predicates):
            TypeAdapter(Identifier).validate_python(name)
        for predicate in self._predicates.values():
            if not callable(predicate) or iscoroutinefunction(predicate) or iscoroutinefunction(
                getattr(predicate, "__call__", None),
            ):
                raise ValueError("Workflow predicates must be synchronous Python callables")

    def assign(
        self, *, organization_id: str, revision: str, event: str, preview_id: str,
        proposal: AssignmentProposal | None = None,
    ) -> TaskAssignment:
        snapshot = self._definitions.get(organization_id, revision)
        if snapshot is None:
            raise ValueError("Organization revision not found")
        route = next((item for item in snapshot.definition.routes if event in item.events), None)
        if route is None:
            raise ValueError("Configured event route not found")
        actor = self._actor()
        strategy = route.delegation.strategy
        if strategy == "coordinator" and actor.kind != "agent":
            raise PermissionError("Coordinator proposal requires trusted agent runtime context")
        if strategy in {"human", "rules"} and actor.kind != "operator":
            raise PermissionError("Human/rule assignment requires trusted operator context")
        return self._service.assign(
            organization_id=organization_id, revision=revision, event=event, preview_id=preview_id,
            proposal=proposal, coordinator_id=actor.actor_id if strategy == "coordinator" else None,
            human_id=actor.actor_id if strategy == "human" else None,
        )

    def _authorize(self, assignment: TaskAssignment) -> None:
        actor = self._actor()
        if actor.kind == "agent" and (assignment.strategy != "coordinator" or actor.actor_id != assignment.actor_id):
            raise PermissionError("Managed execution requires its trusted coordinator or an operator")

    def _context(self, run: ManagedRun, assignment: TaskAssignment) -> ManagedTaskContext:
        if run.assignment_digest != _assignment_digest(assignment):
            raise PermissionError("Managed run assignment pins changed")
        if (run.organization_id, run.revision, run.workflow_id, run.scope_digest) != (
            assignment.organization_id, assignment.revision, assignment.workflow_id, assignment.scope_digest,
        ):
            raise PermissionError("Managed run definition/scope references differ from assignment")
        snapshot = self._definitions.get(run.organization_id, run.revision)
        if snapshot is None:
            raise ValueError("Pinned organization revision not found")

        def revalidate() -> TaskAssignment:
            current = self._service.revalidate(assignment.assignment_id)
            if _assignment_digest(current) != run.assignment_digest:
                raise PermissionError("Managed run assignment pins changed")
            return current

        return ManagedTaskContext(run, assignment, snapshot, revalidate)

    async def start(self, assignment_id: str, *, input: JsonValue = None) -> ManagedRun:
        TypeAdapter(JsonValue).validate_python(input)
        if len(json.dumps(input).encode()) > 32000:
            raise ValueError("Managed input exceeds the prompt byte limit")
        with self._runs.invocation_lock(assignment_id):
            assignment = self._assignments.get(assignment_id)
            if assignment is None:
                raise ValueError("Assignment not found")
            self._authorize(assignment)
            run = self._runs.get(assignment_id)
            if run is not None:
                if run.input != input:
                    raise ValueError("Duplicate invocation cannot replace managed input")
                return await self._existing(run, assignment)
            assignment = self._service.revalidate(assignment_id)
            now = datetime.now(tz=UTC)
            identity = sha256(assignment_id.encode()).hexdigest()
            run = ManagedRun(
                run_id="run-" + identity, session_id="session-" + identity[:32],
                assignment_id=assignment_id, assignment_digest=_assignment_digest(assignment),
                organization_id=assignment.organization_id, revision=assignment.revision,
                workflow_id=assignment.workflow_id, scope_digest=assignment.scope_digest,
                binding_revision=self._binding_revision,
                workflow_name=f"{assignment.organization_id}.{assignment.revision}.{assignment.workflow_id}.{identity}",
                input=input, created_at=now, updated_at=now,
            )
            self._runs.save(run)
            return await self._invoke(run, assignment)

    async def _existing(self, run: ManagedRun, assignment: TaskAssignment) -> ManagedRun:
        if run.state in TERMINAL:
            self._finish_assignment(run)
            return run
        if run.state == "finalizing":
            return self._finalize(run)
        context = self._context(run, assignment)
        try:
            if run.binding_revision != self._binding_revision:
                raise PermissionError("Managed operator binding revision has changed")
            context.revalidate()
        except Exception as error:
            return await self._terminate(run, context, "failed", str(error))
        if run.state == "running":
            return await self._terminate(run, context, "failed", "Interrupted native invocation; automatic replay is blocked")
        if run.state == "waiting":
            return run
        return await self._invoke(run, assignment)

    async def resume(self, assignment_id: str, *, request_id: str, response: JsonValue) -> ManagedRun:
        return await self._resume(assignment_id, request_id, response, "human_input")

    async def approve(self, assignment_id: str, *, request_id: str, approved: bool) -> ManagedRun:
        if type(approved) is not bool or self._actor().kind != "operator":
            raise PermissionError("Service approval requires an explicit trusted operator decision")
        return await self._resume(assignment_id, request_id, approved, "service_approval")

    async def _resume(self, assignment_id: str, request_id: str, value: JsonValue, kind: InputKind) -> ManagedRun:
        TypeAdapter(JsonValue).validate_python(value)
        if len(json.dumps(value).encode()) > 32000:
            raise ValueError("Managed response exceeds the prompt byte limit")
        with self._runs.invocation_lock(assignment_id):
            assignment = self._assignments.get(assignment_id)
            run = self._runs.get(assignment_id)
            if assignment is None or run is None:
                raise ValueError("Managed run not found")
            self._authorize(assignment)
            if run.state == "running":
                return await self._existing(run, assignment)
            if run.state != "waiting":
                raise ValueError("Managed run has no pending continuation")
            request = next((item for item in run.pending if item.request_id == request_id), None)
            if request is None or request.kind != kind:
                raise PermissionError("Request identity/kind is not pending; native input is not service approval")
            return await self._invoke(run, assignment, responses={request_id: {"kind": kind, "value": value}})

    async def cancel(self, assignment_id: str) -> ManagedRun:
        if self._actor().kind != "operator":
            raise PermissionError("Cancellation requires trusted operator context")
        with self._runs.invocation_lock(assignment_id):
            assignment = self._assignments.get(assignment_id)
            run = self._runs.get(assignment_id)
            if assignment is None or run is None:
                raise ValueError("Managed run not found")
            if run.state in TERMINAL:
                self._finish_assignment(run)
                return run
            if run.state == "finalizing":
                return self._finalize(run)
            return await self._terminate(run, self._context(run, assignment), "cancelled", "Operator cancelled workflow")

    def _workflow(self, context: ManagedTaskContext, storage: _RunCheckpoints) -> Any:
        definition = context.snapshot.definition
        document = next(item.document for item in definition.workflows if item.id == context.run.workflow_id)
        runtime = OrganizationRuntime(context.snapshot, {}, {}, {}, {})
        admitted = _admit_graph(
            document, runtime=runtime,
            operations={name: WorkflowOperation(operation.handler) for name, operation in self._operations.items()},
            predicates=self._predicates, limits=self._limits,
        )
        selected = next(agent for agent in definition.agents if agent.id == context.assignment.agent_id)
        nodes: dict[str, Executor] = {}
        for node in admitted.nodes:
            operation = self._operations[str(node.operation)]
            if operation.action != ActionClass.READ_ONLY and operation.role != selected.role:
                raise PermissionError("Managed write/review operation exceeds the assigned role")
            nodes[node.id] = _ManagedExecutor(node.id, operation, context)
        return _build_native_workflow(
            admitted, nodes=nodes, predicates=self._predicates, checkpoint_storage=storage,
            name=context.run.workflow_name,
        )

    async def _invoke(
        self, run: ManagedRun, assignment: TaskAssignment, *, responses: dict[str, Any] | None = None,
    ) -> ManagedRun:
        context = self._context(run, assignment)
        current = run
        try:
            if run.binding_revision != self._binding_revision:
                raise PermissionError("Managed operator binding revision has changed")
            context.revalidate()
            storage = _RunCheckpoints(self._runs.checkpoint_path(assignment.assignment_id), context)
            workflow = self._workflow(context, storage)
            if responses is not None:
                if run.checkpoint_id is None or storage.digest(run.checkpoint_id) != run.checkpoint_digest:
                    raise ValueError("Managed checkpoint integrity pin differs")
                checkpoint = await storage.load(run.checkpoint_id)
                if checkpoint.metadata.get("aitobuild") != {
                    "run_id": run.run_id, "assignment_id": assignment.assignment_id,
                    "revision": assignment.revision, "scope_digest": assignment.scope_digest,
                }:
                    raise ValueError("Checkpoint managed assignment references differ")
                if checkpoint.workflow_name != run.workflow_name or set(checkpoint.pending_request_info_events) != {
                    item.request_id for item in run.pending
                }:
                    raise ValueError("Checkpoint differs from the pinned run/pending requests")
            running = ManagedRun.model_validate(run.model_dump() | {
                "state": "running", "pending": (), "updated_at": datetime.now(tz=UTC),
                "decisions": (*run.decisions, *(WorkflowDecision(
                    request_id=request_id, kind=response["kind"], actor_id=self._actor().actor_id,
                    value=response["value"],
                ) for request_id, response in (responses or {}).items())),
            })
            self._runs.save(running)
            current = running
            async with asyncio.timeout(context.remaining_seconds()):
                result = await workflow.run(
                    message=(run.input if run.input is not None else json.loads(assignment.bundle_content))
                    if responses is None else None,
                    checkpoint_id=run.checkpoint_id if responses is not None else None,
                    responses=responses,
                )
            context.revalidate()
            if storage.latest is None:
                raise ValueError("Native workflow did not persist a checkpoint")
            checkpoint = await storage.load(storage.latest)
            pending = tuple(PendingWorkflowInput(
                request_id=request_id, node_id=event.source_executor_id,
                kind=event.data["kind"], prompt=event.data["prompt"], data=event.data["data"],
            ) for request_id, event in checkpoint.pending_request_info_events.items())
            updated = ManagedRun.model_validate(running.model_dump() | {
                "checkpoint_id": storage.latest, "checkpoint_digest": storage.digest(storage.latest),
                "outputs": (*run.outputs, *result.get_outputs()), "updated_at": datetime.now(tz=UTC),
            })
            current = updated
            if pending:
                if result.get_final_state() != WorkflowRunState.IDLE_WITH_PENDING_REQUESTS:
                    raise ValueError("Native pending input did not reach a safe idle boundary")
                waiting = ManagedRun.model_validate(updated.model_dump() | {"state": "waiting", "pending": pending})
                self._runs.save(waiting)
                return waiting
            if result.get_final_state() != WorkflowRunState.IDLE:
                raise ValueError("Native workflow did not finish safely")
            return await self._terminate(updated, context, "completed", None)
        except asyncio.CancelledError:
            await self._terminate(current, context, "cancelled", "Native invocation cancelled; automatic replay is blocked")
            raise
        except Exception as error:
            saved = self._runs.get(assignment.assignment_id)
            if saved is not None and saved.state in TERMINAL | {"finalizing"}:
                raise
            return await self._terminate(current, context, "failed", str(error) or type(error).__name__)

    async def _terminate(
        self, run: ManagedRun, context: ManagedTaskContext,
        state: Literal["completed", "failed", "cancelled"], error: str | None,
    ) -> ManagedRun:
        cleanup_succeeded = True
        try:
            await self._cleanup(context)
            if state == "completed":
                context.revalidate()
        except Exception as cleanup_error:
            cleanup_succeeded = False
            state = "failed"
            error = (error + "; " if error else "") + "Cleanup/final validation failed: " + str(cleanup_error)
        intent = ManagedRun.model_validate(run.model_dump() | {
            "state": "finalizing", "terminal_state": state, "pending": (),
            "error": error, "cleanup_succeeded": cleanup_succeeded,
            "updated_at": datetime.now(tz=UTC),
        })
        self._runs.save(intent)
        return self._finalize(intent)

    def _finalize(self, intent: ManagedRun) -> ManagedRun:
        try:
            assignment = self._assignments.get(intent.assignment_id)
            if assignment is not None and assignment.state == "claimed" and intent.terminal_state == "completed":
                self._context(intent, assignment).revalidate()
            self._finish_assignment(intent)
        except (TimeoutError, PermissionError, ValueError) as error:
            if intent.terminal_state != "completed":
                raise
            intent = ManagedRun.model_validate(intent.model_dump() | {
                "terminal_state": "failed", "error": str(error),
            })
            self._runs.save(intent)
            self._finish_assignment(intent)
        terminal = ManagedRun.model_validate(intent.model_dump() | {
            "state": intent.terminal_state, "terminal_state": None,
        })
        self._runs.save(terminal)
        return terminal

    def _finish_assignment(self, run: ManagedRun) -> None:
        state = run.terminal_state if run.state == "finalizing" else run.state
        if state not in TERMINAL:
            raise ValueError("Assignment outcome requires a terminal run receipt")
        assignment = self._assignments.get(run.assignment_id)
        if assignment is None or run.assignment_digest != _assignment_digest(assignment):
            raise PermissionError("Managed terminal assignment pins changed")
        self._service.finish(
            run.assignment_id, state=state,  # type: ignore[arg-type]
            outcome=f"Managed workflow {state}: {run.run_id} (metadata only)",
        )