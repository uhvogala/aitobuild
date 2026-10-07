"""Opt-in service bindings for approved configured organization tasks."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from pydantic import Field, JsonValue, StrictInt, StrictStr

from aitobuild.agent_tools import DeveloperToolContext
from aitobuild.developer_delivery import DeveloperDeliveryWorker
from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.developer_preview import DeveloperPreview, DeveloperPreviewRegistry
from aitobuild.organization import DefinitionModel, DefinitionSnapshot, DefinitionStore, EventName, Identifier
from aitobuild.organization_assignments import AssignmentProposal, AssignmentService, AssignmentStore, TaskAssignment
from aitobuild.organization_delivery import _delivery_call
from aitobuild.organization_runner import (
    ManagedOperation, ManagedRun, ManagedTaskContext, ManagedWorkflowRunner, RunStore, RuntimeActor,
)
from aitobuild.organization_runtime import WorkflowLimits, WorkflowPredicate


class ManagedRoute(DefinitionModel):
    repository: StrictStr = Field(min_length=1)
    repository_id: StrictInt = Field(gt=0)
    organization_id: Identifier
    revision: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    event: EventName


@dataclass(frozen=True)
class ManagedServiceContext:
    previews: DeveloperPreviewRegistry
    worker: DeveloperDeliveryWorker
    tools: DeveloperToolContext
    state_dir: Path


CoordinatorProposal = Callable[[DefinitionSnapshot, DeveloperPreview], Awaitable[AssignmentProposal]]


class ManagedOrganizationService:
    def __init__(
        self, *, definitions: DefinitionStore, assignments: AssignmentStore, runs: RunStore,
        previews: DeveloperPreviewRegistry, worker: DeveloperDeliveryWorker,
        routes: tuple[ManagedRoute, ...], operator_id: str,
        operations: Mapping[str, ManagedOperation],
        cleanup: Callable[[ManagedTaskContext], Awaitable[None]], binding_revision: str,
        coordinators: Mapping[str, CoordinatorProposal] | None = None,
        predicates: Mapping[str, WorkflowPredicate] | None = None,
        limits: WorkflowLimits = WorkflowLimits(),
    ) -> None:
        self._definitions = definitions
        self._assignments = assignments
        self._runs = runs
        self._previews = previews
        self._worker = worker
        self._routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in routes)
        targets = [(route.repository, route.repository_id) for route in self._routes]
        if len(targets) != len(set(targets)):
            raise ValueError("Managed repository routes must be unambiguous")
        for route in self._routes:
            snapshot = definitions.get(route.organization_id, route.revision)
            if snapshot is None or not any(route.event in item.events for item in snapshot.definition.routes):
                raise ValueError("Managed activation requires an existing explicit revision/event")
        self._operator = RuntimeActor("operator", operator_id)
        self._actor: ContextVar[RuntimeActor] = ContextVar("managed_service_actor")
        self._coordinators = dict(coordinators or {})
        self._runner = ManagedWorkflowRunner(
            definitions=definitions, assignments=assignments, runs=runs,
            assignment_service=AssignmentService(
                definitions=definitions, assignments=assignments, previews=previews,
                budget_path_for=worker.budget_path,
            ), actor_provider=self._actor.get, operations=operations, cleanup=cleanup,
            binding_revision=binding_revision, predicates=predicates, limits=limits,
        )

    def _route(self, preview: DeveloperPreview) -> ManagedRoute | None:
        issue = developer_task_bundle_from_payload(preview.bundle_payload).issue_context
        if issue is None:
            return None
        return next((route for route in self._routes if (route.repository, route.repository_id) ==
                     (issue.repository, issue.repository_id)), None)

    def _owned(self, preview: DeveloperPreview, route: ManagedRoute) -> TaskAssignment | None:
        task_id = developer_task_bundle_from_payload(preview.bundle_payload).task_id
        assignment = self._assignments.for_task(task_id)
        if assignment is not None and (assignment.preview_id != preview.preview_id or
                                       assignment.organization_id != route.organization_id):
            raise PermissionError("Approved task belongs to another managed activation")
        return assignment

    async def consume(self, preview_id: str, *, proposal: AssignmentProposal | None = None) -> ManagedRun | None:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ValueError("Preview not found")
        route = self._route(preview)
        if route is None or not preview.approved:
            return None
        token = self._actor.set(self._operator)
        try:
            with self._runs.invocation_lock("dispatch-" + preview_id):
                assignment = self._owned(preview, route)
                if assignment is not None:
                    if proposal is not None:
                        raise ValueError("Existing ownership cannot be replaced by a new proposal")
                    return await self._runner.start(assignment.assignment_id)
                snapshot = self._definitions.get(route.organization_id, route.revision)
                if snapshot is None:
                    raise ValueError("Activated revision not found")
                configured = next(item for item in snapshot.definition.routes if route.event in item.events)
                strategy = configured.delegation.strategy
                if proposal is not None and strategy != "human":
                    raise PermissionError("Only human delegation accepts an operator selection")
                if proposal is not None and proposal.agent_id not in configured.delegation.eligible_agents:
                    raise PermissionError("Operator selection is not eligible for the activated route")
                if strategy == "human" and proposal is None:
                    return None
                coordinator = None
                if strategy == "coordinator":
                    team = next(team for team in snapshot.definition.teams if team.id == configured.team)
                    coordinator = self._coordinators.get(str(team.coordinator))
                    if coordinator is None:
                        raise PermissionError("Coordinator has no trusted runtime proposal binding")
                prepared = await _delivery_call(lambda: self._worker.prepare(preview_id))
                if prepared.state != "prepared":
                    raise ValueError("Unowned delivery is not prepared; automatic replay is blocked")
                if coordinator is not None:
                    team = next(team for team in snapshot.definition.teams if team.id == configured.team)
                    self._actor.set(RuntimeActor("agent", str(team.coordinator)))
                    budget = DeveloperTaskBudget(path=self._worker.budget_path(preview_id),
                                                 bundle=developer_task_bundle_from_payload(preview.bundle_payload), create=False)
                    try:
                        async with asyncio.timeout(budget.remaining_seconds()):
                            proposal = await coordinator(snapshot, preview)
                    except BaseException:
                        budget.abort()
                        raise
                assignment = self._runner.assign(
                    organization_id=route.organization_id, revision=route.revision,
                    event=route.event, preview_id=preview_id, proposal=proposal,
                )
                self._actor.set(self._operator)
                return await self._runner.start(assignment.assignment_id)
        finally:
            self._actor.reset(token)

    def _assignment(self, assignment_id: str) -> TaskAssignment:
        assignment = self._assignments.get(assignment_id)
        if assignment is None:
            raise ValueError("Assignment not found")
        issue = assignment.bundle.issue_context
        route = next((route for route in self._routes if issue is not None and
                      (route.repository, route.repository_id, route.organization_id) ==
                      (issue.repository, issue.repository_id, assignment.organization_id)), None)
        if route is None or self._assignments.for_task(assignment.task_id) != assignment:
            raise PermissionError("Assignment is outside this managed activation")
        return assignment

    def status(self, preview_id: str) -> ManagedRun | None:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ValueError("Preview not found")
        route = self._route(preview)
        if route is None:
            raise PermissionError("Preview is outside this managed activation")
        assignment = self._owned(preview, route)
        return self._runs.get(assignment.assignment_id) if assignment else None

    async def decide(self, assignment_id: str, *, request_id: str, approved: bool) -> ManagedRun:
        self._assignment(assignment_id)
        token = self._actor.set(self._operator)
        try:
            return await self._runner.approve(assignment_id, request_id=request_id, approved=approved)
        finally:
            self._actor.reset(token)

    async def respond(self, assignment_id: str, *, request_id: str, response: JsonValue) -> ManagedRun:
        self._assignment(assignment_id)
        token = self._actor.set(self._operator)
        try:
            return await self._runner.resume(assignment_id, request_id=request_id,
                                             response=response)
        finally:
            self._actor.reset(token)

    async def cancel(self, assignment_id: str) -> ManagedRun:
        self._assignment(assignment_id)
        token = self._actor.set(self._operator)
        try:
            return await self._runner.cancel(assignment_id)
        finally:
            self._actor.reset(token)