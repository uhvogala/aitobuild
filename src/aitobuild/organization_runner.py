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
from typing import Annotated, Any, Literal, Protocol, Self

from agent_framework import (
    Executor, FileCheckpointStorage, WorkflowCheckpoint, WorkflowContext,
    WorkflowRunState, handler, response_handler,
)
from filelock import FileLock
from openai import APIError
from pydantic import AwareDatetime, Field, JsonValue, StrictBool, StrictStr, TypeAdapter, model_validator

from aitobuild.durable_files import atomic_write_text, sync_directory
from aitobuild.approval_policy import ApprovalDecision, ApprovalPolicy, ApprovalStage
from aitobuild.developer_isolation import BudgetDeadlineExpired, DeveloperTaskBudget
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
    approval_stage: ApprovalStage | None = None


class WorkflowDecision(DefinitionModel):
    request_id: StrictStr
    kind: InputKind
    actor_id: StrictStr
    value: JsonValue
    data_digest: Digest | None = None
    approval_policy: ApprovalDecision | None = None


class ExpiredBudgetAdoption(DefinitionModel):
    """#66 run receipt: the interrupted continuation finished by adopting its already-open PR after the budget
    deadline. Only the delivery layer's adopt-only hook can produce it; nothing else runs on an expired budget."""
    request_id: StrictStr
    deadline: Annotated[float, Field(strict=True, allow_inf_nan=False)]
    adopted_at: AwareDatetime


def workflow_input_digest(data: Any) -> str:
    return sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


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
    approval_policy_revision: Digest | None = None
    # #66: set only when an interrupted publication was finished by budget-expired PR adoption. Older run
    # receipts have no such field and load as None.
    adopted_after_budget_expiry: ExpiredBudgetAdoption | None = None

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
        adoption = self.adopted_after_budget_expiry
        if adoption is not None and (self.state in {"ready", "waiting"} or not self.decisions
                                     or self.decisions[-1].request_id != adoption.request_id):
            raise ValueError("Budget-expired adoption must finish the run's own last recorded decision")
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
                           "decisions", "outputs", "error", "cleanup_succeeded", "terminal_state",
                           "adopted_after_budget_expiry"}
                if original.model_dump(exclude=mutable) != run.model_dump(exclude=mutable):
                    raise ValueError("Managed run identity and pins are immutable")
                if (original.adopted_after_budget_expiry is not None
                        and original.adopted_after_budget_expiry != run.adopted_after_budget_expiry):
                    raise ValueError("Budget-expired adoption receipts are immutable")
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
            atomic_write_text(path, run.model_dump_json())
            sync_directory(self._directory)


def _assignment_digest(assignment: TaskAssignment) -> str:
    return sha256(assignment.model_dump_json(exclude={"state", "closed_at", "outcome"}).encode()).hexdigest()


def _failure_label(error: BaseException) -> str:
    """One label for budget deadline expiry, whichever check (admission, revalidation or invocation) saw it first.

    Only an actual deadline expiry is relabelled; aborted ledgers, lock timeouts and every other error keep
    their own message. The native invocation's asyncio timeout carries no message, so it is "TimeoutError" too.
    A model API failure (openai.APIError anywhere in the chain) also names its exception chain.
    """
    if type(error) is BudgetDeadlineExpired:
        return "TimeoutError"
    label = str(error) or type(error).__name__
    chain = _exception_chain(error)
    if any(isinstance(item, APIError) for item in chain):
        # A model call failure: keep the transport cause (httpx.ConnectTimeout vs httpx.ReadTimeout, ...)
        # so a connect stall can be told apart from a read stall in the failure record.
        label += " [exception chain: " + " <- ".join(_exception_name(item) for item in chain) + "]"
    return label


def _exception_chain(error: BaseException, limit: int = 8) -> list[BaseException]:
    """The error and its causes, outermost first, following ``raise ... from`` and implicit context."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(chain) < limit and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return chain


def _exception_name(error: BaseException) -> str:
    kind = type(error)
    package = kind.__module__.split(".")[0]
    return kind.__qualname__ if package == "builtins" else f"{package}.{kind.__qualname__}"


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
    approval_stage: ApprovalStage | None = None


@dataclass(frozen=True, slots=True)
class ManagedOperation:
    handler: Callable[[ManagedTaskContext, Any], Any]
    role: AgentRole = AgentRole.DEVELOPER
    action: ActionClass = ActionClass.READ_ONLY
    on_response: Callable[[ManagedTaskContext, Any, Any], Any] | None = None
    approval_stage: ApprovalStage | None = None
    automatic_response: Callable[[ManagedTaskContext, PendingWorkflowInput], JsonValue] | None = None
    approval_stages: tuple[ApprovalStage, ...] = ()
    # Optional, approval-stage-bound: True only when the operation's own durable effect record proves that the
    # continuation for this exact approved request was interrupted mid-effect and can be finished idempotently
    # by its existing resume path. Never re-admits or re-approves; any doubt must return False.
    resume_interrupted: Callable[[ManagedTaskContext, Any], bool] | None = None
    # Optional (#66), requires ``resume_interrupted``: finish that same interrupted continuation after the budget
    # deadline by adopting an already-existing effect only (for ``delivery_publish``: the open draft PR). It gets
    # a context whose revalidation checks everything except a live deadline. It must never call a model, run the
    # Developer, push or create anything; any doubt must raise. Nothing else in the runner runs on an expired budget.
    adopt_after_budget_expiry: Callable[[ManagedTaskContext, Any], Any] | None = None

    def __post_init__(self) -> None:
        assert_role_action_allowed(self.role, self.action)
        if not callable(self.handler) or (self.on_response is not None and not callable(self.on_response)):
            raise ValueError("Managed operation handlers must be callable")
        if self.approval_stage is not None and (not isinstance(self.approval_stage, ApprovalStage) or self.on_response is None):
            raise ValueError("Approval stage requires a registered stage and continuation handler")
        if self.automatic_response is not None and (self.approval_stage is None or not callable(self.automatic_response)):
            raise ValueError("Automatic response requires a trusted stage-bound callback")
        if self.approval_stages and (self.on_response is None or
                any(not isinstance(stage, ApprovalStage) for stage in self.approval_stages) or
                len(set(self.approval_stages)) != len(self.approval_stages)):
            raise ValueError("Additional approval stages require unique registered stages and a continuation")
        if self.resume_interrupted is not None and (self.approval_stage is None or not callable(self.resume_interrupted)):
            raise ValueError("Interrupted-continuation resume requires a stage-bound continuation")
        if self.adopt_after_budget_expiry is not None and (
                self.resume_interrupted is None or not callable(self.adopt_after_budget_expiry)):
            raise ValueError("Budget-expired adoption requires an interrupted-continuation check")


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
            data: dict[str, Any] = {"kind": result.kind, "prompt": result.prompt, "data": result.data}
            stage = result.approval_stage or self._operation.approval_stage
            if stage is not None:
                if stage not in (*self._operation.approval_stages, self._operation.approval_stage):
                    raise PermissionError("Input stage is outside its trusted operation binding")
                data["approval_stage"] = stage.value
            await ctx.request_info(request_data=data, response_type=dict)
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
        sync_directory(self.path)
        sync_directory(self.path.parent)
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
        approval_policy: ApprovalPolicy = ApprovalPolicy(),
        external_approval_stages: tuple[ApprovalStage, ...] = (),
    ) -> None:
        self._definitions = definitions
        self._assignments = assignments
        self._service = assignment_service
        self._runs = runs
        self._actor = actor_provider
        self._operations = dict(operations)
        self.approval_policy = ApprovalPolicy.model_validate(approval_policy.model_dump())
        supported = {stage for operation in self._operations.values()
                 for stage in (*operation.approval_stages, operation.approval_stage) if stage is not None}
        if any(not isinstance(stage, ApprovalStage) for stage in external_approval_stages):
            raise ValueError("External approval bindings require registered stages")
        supported.update(external_approval_stages)
        defaults = ApprovalPolicy()
        for stage in ApprovalStage:
            if self.approval_policy.mode(stage) != defaults.mode(stage) and stage not in supported:
                raise ValueError(f"Approval stage has no trusted operation binding: {stage.value}")
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

    def _context(self, run: ManagedRun, assignment: TaskAssignment, *,
                 expired_deadline: float | None = None) -> ManagedTaskContext:
        """The run's task context. ``expired_deadline`` (#66 adoption only) swaps the live-deadline check for
        "the same unaborted ledger, expired at exactly this deadline"; every other pin is still revalidated."""
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
            expected = run.approval_policy_revision or ApprovalPolicy().revision
            if expected != self.approval_policy.revision:
                raise PermissionError("Approval policy revision changed during active work")
            if expired_deadline is None:
                current = self._service.revalidate(assignment.assignment_id)
            else:
                current, deadline = self._service.revalidate_expired(assignment.assignment_id)
                if deadline != expired_deadline:
                    raise PermissionError("Budget-expired adoption ledger deadline changed")
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
            # Deadline first: an expired budget refuses as TimeoutError before the approval/admission lookup,
            # leaving the ledger (reservations included) untouched.
            DeveloperTaskBudget(path=Path(assignment.budget_path), bundle=assignment.bundle, create=False).remaining_seconds()
            assignment = self._service.revalidate(assignment_id)
            now = datetime.now(tz=UTC)
            identity = sha256(assignment_id.encode()).hexdigest()
            run = ManagedRun(
                run_id="run-" + identity, session_id="session-" + identity[:32],
                assignment_id=assignment_id, assignment_digest=_assignment_digest(assignment),
                organization_id=assignment.organization_id, revision=assignment.revision,
                workflow_id=assignment.workflow_id, scope_digest=assignment.scope_digest,
                binding_revision=self._binding_revision,
                approval_policy_revision=self.approval_policy.revision,
                workflow_name=f"{assignment.organization_id}.{assignment.revision}.{assignment.workflow_id}.{identity}",
                input=input, created_at=now, updated_at=now,
            )
            self._runs.save(run)
            return await self._advance_automatic(await self._invoke(run, assignment), assignment)

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
            if type(error) is BudgetDeadlineExpired and run.state == "running":
                # #66: the only budget exemption. An interrupted approved continuation may still be finished by
                # its operation's adopt-only hook; everything else (and any refusal) fails closed as before.
                return await self._adopt_after_budget_expiry(run, assignment, context, error)
            return await self._terminate(run, context, "failed", _failure_label(error))
        if run.state == "running":
            try:
                interrupted = await self._interrupted_continuation(run, context)
            except Exception as error:
                return await self._terminate(run, context, "failed", _failure_label(error))
            if interrupted is None:
                return await self._terminate(run, context, "failed", "Interrupted native invocation; automatic replay is blocked")
            # Finish the same admitted task's own approved continuation; no new decision, admission or approval.
            return await self._advance_automatic(await self._invoke(run, assignment, interrupted=interrupted), assignment)
        if run.state == "waiting":
            return await self._advance_automatic(run, assignment)
        return await self._advance_automatic(await self._invoke(run, assignment), assignment)

    async def _interrupted_continuation(self, run: ManagedRun, context: ManagedTaskContext) -> WorkflowDecision | None:
        """The single approved continuation a killed invocation was executing, if its operation proves it resumable."""
        found = await self._interrupted_request(run, context)
        return None if found is None else found[0]

    async def _interrupted_request(
        self, run: ManagedRun, context: ManagedTaskContext,
    ) -> tuple[WorkflowDecision, PendingWorkflowInput, ManagedOperation] | None:
        if not run.decisions or run.checkpoint_id is None or run.checkpoint_digest is None:
            return None
        decision = run.decisions[-1]
        if decision.kind != "service_approval" or decision.value is not True or decision.data_digest is None:
            return None
        storage = _RunCheckpoints(self._runs.checkpoint_path(run.assignment_id), context)
        if storage.digest(run.checkpoint_id) != run.checkpoint_digest:
            return None
        checkpoint = await storage.load(run.checkpoint_id)
        if (checkpoint.workflow_name != run.workflow_name or checkpoint.metadata.get("aitobuild") != {
                "run_id": run.run_id, "assignment_id": context.assignment.assignment_id,
                "revision": context.assignment.revision, "scope_digest": context.assignment.scope_digest}
                or set(checkpoint.pending_request_info_events) != {decision.request_id}):
            return None
        event = checkpoint.pending_request_info_events[decision.request_id]
        data = event.data
        if (not isinstance(data, dict) or data.get("kind") != decision.kind
                or workflow_input_digest(data.get("data")) != decision.data_digest):
            return None
        request = PendingWorkflowInput(request_id=decision.request_id, node_id=event.source_executor_id,
                                       kind=data["kind"], prompt=data["prompt"], data=data["data"],
                                       approval_stage=data.get("approval_stage"))
        operation = self._input_operation(context, request)
        if operation.resume_interrupted is None or self._input_stage(operation, request) is None:
            return None
        return (decision, request, operation) if operation.resume_interrupted(context, request.data) is True else None

    async def _adopt_after_budget_expiry(
        self, run: ManagedRun, assignment: TaskAssignment, context: ManagedTaskContext, expired: BaseException,
    ) -> ManagedRun:
        """#66: finish an interrupted approved continuation on an expired budget by adoption only.

        Applies only when the run was killed mid-continuation (state ``running``), the pinned checkpoint and last
        recorded decision prove that exact approved continuation, and its operation declares an adopt-only hook.
        No workflow, handler, model or Developer runs; no decision, admission or approval is recorded. Without a
        hook or interrupted continuation the refusal is exactly today's; a hook refusal fails closed the same way.
        """
        label = _failure_label(expired)
        try:
            _, deadline = self._service.revalidate_expired(assignment.assignment_id)
            expired_context = self._context(run, assignment, expired_deadline=deadline)
            expired_context.revalidate()
            found = await self._interrupted_request(run, expired_context)
        except Exception:
            return await self._terminate(run, context, "failed", label)
        hook = None if found is None else found[2].adopt_after_budget_expiry
        if found is None or hook is None:
            return await self._terminate(run, context, "failed", label)
        decision, request, _ = found
        try:
            result = await _resolve(hook(expired_context, request.data))
            TypeAdapter(JsonValue).validate_python(result)
            expired_context.revalidate()
        except Exception as refusal:
            return await self._terminate(
                run, context, "failed", f"{label} (budget-expired adoption refused: {_failure_label(refusal)})")
        adopted = ManagedRun.model_validate(run.model_dump() | {
            "outputs": (*run.outputs, result), "updated_at": datetime.now(tz=UTC),
            "adopted_after_budget_expiry": ExpiredBudgetAdoption(
                request_id=decision.request_id, deadline=deadline, adopted_at=datetime.now(tz=UTC)),
        })
        self._runs.save(adopted)
        return await self._terminate(adopted, expired_context, "completed", None)

    def _input_operation(self, context: ManagedTaskContext, request: PendingWorkflowInput) -> ManagedOperation:
        workflow = next(item for item in context.snapshot.definition.workflows if item.id == context.run.workflow_id)
        admitted = _admit_graph(
            workflow.document, runtime=OrganizationRuntime(context.snapshot, {}, {}, {}, {}),
            operations={name: WorkflowOperation(operation.handler) for name, operation in self._operations.items()},
            predicates=self._predicates, limits=self._limits,
        )
        node = next((node for node in admitted.nodes if node.id == request.node_id), None)
        if node is None or node.kind != "operation" or node.operation not in self._operations:
            raise PermissionError("Pending input has no trusted operation binding")
        return self._operations[node.operation]

    def _policy_decision(
        self, context: ManagedTaskContext, request: PendingWorkflowInput, value: JsonValue,
        *, source: Literal["human", "operator_policy"],
    ) -> ApprovalDecision | None:
        operation = self._input_operation(context, request)
        stage = self._input_stage(operation, request)
        if stage is None:
            if source == "operator_policy":
                raise PermissionError("Automatic decision requires a stage-bound operation")
            return None
        return self.approval_policy.resolve(
            stage, actor_id=self._actor().actor_id,
            operation={"request": request.model_dump(mode="json"), "response": value},
            expected_revision=context.run.approval_policy_revision or ApprovalPolicy().revision,
            automatic_supported=True, source=source,
        )

    def _input_stage(self, operation: ManagedOperation, request: PendingWorkflowInput) -> ApprovalStage | None:
        stage = request.approval_stage or operation.approval_stage
        if stage is not None and stage not in (*operation.approval_stages, operation.approval_stage):
            raise PermissionError("Input stage is outside its trusted operation binding")
        return stage

    async def _advance_automatic(self, run: ManagedRun, assignment: TaskAssignment) -> ManagedRun:
        count = sum(decision.approval_policy is not None and decision.approval_policy.source == "operator_policy"
                    for decision in run.decisions)
        while run.state == "waiting":
            context = self._context(run, assignment)
            try:
                context.revalidate()
                selected = None
                for request in run.pending:
                    operation = self._input_operation(context, request)
                    stage = self._input_stage(operation, request)
                    if stage is None:
                        continue
                    mode = self.approval_policy.mode(stage)
                    if mode == "disabled":
                        raise PermissionError(f"Approval stage is disabled: {stage.value}")
                    if mode == "automatic":
                        selected = request, operation
                        break
                if selected is None:
                    return run
                if self._actor().kind != "operator":
                    raise PermissionError("Automatic decisions require trusted operator runtime context")
                if count >= self.approval_policy.max_automatic_decisions:
                    raise PermissionError("Automatic decision limit exceeded; a new task is required")
                request, operation = selected
                if operation.automatic_response is not None:
                    value = operation.automatic_response(context, request)
                elif request.kind == "service_approval":
                    value = True
                else:
                    raise PermissionError("Automatic human-input response requires a trusted exact response binding")
                TypeAdapter(JsonValue).validate_python(value)
                context.revalidate()
                decision = self._policy_decision(context, request, value, source="operator_policy")
                if decision is None:
                    raise PermissionError("Automatic policy evidence is missing")
                run = await self._invoke(run, assignment, responses={request.request_id: {"kind": request.kind, "value": value}},
                                         policy_decisions={request.request_id: decision})
                count += 1
            except Exception as error:
                saved = self._runs.get(assignment.assignment_id)
                if saved is not None and saved.state in TERMINAL | {"finalizing"}:
                    raise
                return await self._terminate(run, context, "failed", _failure_label(error))
        return run

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
            context = self._context(run, assignment)
            try:
                decision = self._policy_decision(context, request, value, source="human")
            except PermissionError as error:
                return await self._terminate(run, context, "failed", _failure_label(error))
            run = await self._invoke(run, assignment, responses={request_id: {"kind": kind, "value": value}},
                                     policy_decisions={request_id: decision} if decision is not None else None)
            return await self._advance_automatic(run, assignment)

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
            if operation.approval_stage is not None and self.approval_policy.mode(operation.approval_stage) == "disabled":
                raise PermissionError(f"Approval stage is disabled: {operation.approval_stage.value}")
            if operation.action != ActionClass.READ_ONLY and operation.role != selected.role:
                raise PermissionError("Managed write/review operation exceeds the assigned role")
            nodes[node.id] = _ManagedExecutor(node.id, operation, context)
        return _build_native_workflow(
            admitted, nodes=nodes, predicates=self._predicates, checkpoint_storage=storage,
            name=context.run.workflow_name,
        )

    async def _invoke(
        self, run: ManagedRun, assignment: TaskAssignment, *, responses: dict[str, Any] | None = None,
        policy_decisions: Mapping[str, ApprovalDecision] | None = None,
        interrupted: WorkflowDecision | None = None,
    ) -> ManagedRun:
        context = self._context(run, assignment)
        current = run
        if interrupted is not None:
            if responses is not None or policy_decisions is not None or run.state != "running":
                raise ValueError("Interrupted continuation resumes only its own recorded decision")
            responses = {interrupted.request_id: {"kind": interrupted.kind, "value": interrupted.value}}
        expected_pending = ({interrupted.request_id} if interrupted is not None
                            else {item.request_id for item in run.pending})
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
                if checkpoint.workflow_name != run.workflow_name or set(
                        checkpoint.pending_request_info_events) != expected_pending:
                    raise ValueError("Checkpoint differs from the pinned run/pending requests")
            running = ManagedRun.model_validate(run.model_dump() | {
                "state": "running", "pending": (), "updated_at": datetime.now(tz=UTC),
                "decisions": (*run.decisions, *(WorkflowDecision(
                    request_id=request_id, kind=response["kind"], actor_id=self._actor().actor_id,
                    value=response["value"],
                    data_digest=workflow_input_digest(next(item.data for item in run.pending if item.request_id == request_id)),
                    approval_policy=(policy_decisions or {}).get(request_id),
                ) for request_id, response in (responses or {}).items() if interrupted is None)),
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
                approval_stage=event.data.get("approval_stage"),
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
            return await self._terminate(current, context, "failed", _failure_label(error))

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
                adoption = intent.adopted_after_budget_expiry
                self._context(intent, assignment,
                              expired_deadline=None if adoption is None else adoption.deadline).revalidate()
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
        adoption = run.adopted_after_budget_expiry
        self._service.finish(
            run.assignment_id, state=state,  # type: ignore[arg-type]
            outcome=f"Managed workflow {state}: {run.run_id} (metadata only)"
            + (" after budget-expired PR adoption" if adoption is not None and state == "completed" else ""),
            adopted_after_budget_expiry=None if adoption is None else adoption.deadline,
        )