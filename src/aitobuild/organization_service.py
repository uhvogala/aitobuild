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
from aitobuild.organization_reviews import PublishedReviewAdmission


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


@dataclass(frozen=True)
class CoordinatorBinding:
    coordinator_id: str
    event: str
    proposal: CoordinatorProposal

    def __post_init__(self) -> None:
        if not self.coordinator_id.strip() or not self.event.strip() or not callable(self.proposal):
            raise ValueError("Scoped coordinator binding requires explicit coordinator/event and callback")


class ManagedOrganizationService:
    def __init__(
        self, *, definitions: DefinitionStore, assignments: AssignmentStore, runs: RunStore,
        previews: DeveloperPreviewRegistry, worker: DeveloperDeliveryWorker,
        routes: tuple[ManagedRoute, ...], operator_id: str,
        operations: Mapping[str, ManagedOperation],
        cleanup: Callable[[ManagedTaskContext], Awaitable[None]], binding_revision: str,
        coordinators: Mapping[str, CoordinatorProposal | CoordinatorBinding] | None = None,
        predicates: Mapping[str, WorkflowPredicate] | None = None,
        limits: WorkflowLimits = WorkflowLimits(),
        reviews: PublishedReviewAdmission | None = None,
    ) -> None:
        self._definitions = definitions
        self._assignments = assignments
        self._runs = runs
        self._previews = previews
        self._worker = worker
        if reviews is not None:
            reviews.validate_binding(definitions=definitions, previews=previews, worker=worker)
        self._reviews = reviews
        self._routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in routes)
        self._review_routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in reviews.routes) if reviews else ()
        self._correction_routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in reviews.correction_routes) if reviews else ()
        targets = [(route.repository, route.repository_id) for route in self._routes]
        if len(targets) != len(set(targets)):
            raise ValueError("Managed repository routes must be unambiguous")
        for route in (*self._routes, *self._review_routes, *self._correction_routes):
            snapshot = definitions.get(route.organization_id, route.revision)
            if snapshot is None or not any(route.event in item.events for item in snapshot.definition.routes):
                raise ValueError("Managed activation requires an existing explicit revision/event")
        self._operator = RuntimeActor("operator", operator_id)
        self.binding_revision = binding_revision
        self.operator_id = operator_id
        self._actor: ContextVar[RuntimeActor] = ContextVar("managed_service_actor")
        self._coordinators = dict(coordinators or {})
        self._runner = ManagedWorkflowRunner(
            definitions=definitions, assignments=assignments, runs=runs,
            assignment_service=AssignmentService(
                definitions=definitions, assignments=assignments, previews=previews,
                budget_path_for=self.budget_path,
            ), actor_provider=self._actor.get, operations=operations, cleanup=cleanup,
            binding_revision=binding_revision, predicates=predicates, limits=limits,
        )

    def _route(self, preview: DeveloperPreview) -> ManagedRoute | None:
        if self._reviews is None and str(preview.bundle_payload.get("task_id", "")).startswith(("published-review-", "published-correction-")):
            raise PermissionError("Staged review tasks require their trusted admission binding")
        if self._reviews is not None:
            correction = self._reviews.correction_route_for(preview.preview_id)
            if correction is not None:
                return ManagedRoute.model_validate(correction.model_dump())
            review = self._reviews.route_for(preview.preview_id)
            if review is not None:
                return ManagedRoute.model_validate(review.model_dump())
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

    def admission(self, preview_id: str) -> tuple[DeveloperPreview, ManagedRoute] | None:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ValueError("Preview not found")
        route = self._route(preview)
        if route is None or not preview.approved:
            return None
        assignment = self._owned(preview, route)
        if assignment is not None:
            route = route.model_copy(update={"revision": assignment.revision, "event": assignment.event})
        return preview, route

    def recorded_assignment(self, task_id: str) -> TaskAssignment | None:
        assignment = self._assignments.for_task(task_id)
        return self._assignment(assignment.assignment_id) if assignment is not None else None

    def recorded_run(self, task_id: str) -> ManagedRun | None:
        assignment = self.recorded_assignment(task_id)
        return self._runs.get(assignment.assignment_id) if assignment is not None else None

    def budget_path(self, preview_id: str) -> Path:
        if self._reviews is not None:
            path = self._reviews.original_budget_path(preview_id)
            if path is not None:
                return path
        return self._worker.budget_path(preview_id)

    async def offer_published_review(self, published_preview_id: str) -> DeveloperPreview | None:
        if self._reviews is None:
            return None
        reviews = self._reviews
        return await _delivery_call(lambda: reviews.offer(published_preview_id))

    async def offer_correction(self, review_preview_id: str) -> DeveloperPreview | None:
        reviews = self._reviews
        if reviews is None:
            return None
        run = self.status(review_preview_id)
        if run is None:
            raise PermissionError("Correction requires a completed managed review")
        return await _delivery_call(lambda: reviews.offer_correction(review_preview_id, run))

    def approved_previews(self, *, limit: int) -> tuple[DeveloperPreview, ...]:
        return tuple(preview for preview in self._previews.list_previews(pending_only=False, limit=limit) if preview.approved)

    def assignment_preview(self, assignment_id: str) -> str:
        return self._assignment(assignment_id).preview_id

    def validate_selection(self, activation: ManagedRoute, proposal: AssignmentProposal | None) -> None:
        snapshot = self._definitions.get(activation.organization_id, activation.revision)
        if snapshot is None:
            raise ValueError("Activated revision not found")
        configured = next((route for route in snapshot.definition.routes if activation.event in route.events), None)
        if configured is None:
            raise ValueError("Activated event not found")
        if proposal is not None and (configured.delegation.strategy != "human" or
                                     proposal.agent_id not in configured.delegation.eligible_agents):
            raise PermissionError("Only eligible human delegation accepts an operator selection")

    async def consume(
        self, preview_id: str, *, proposal: AssignmentProposal | None = None,
        activation: ManagedRoute | None = None,
    ) -> ManagedRun | None:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ValueError("Preview not found")
        route = self._route(preview)
        if route is None or not preview.approved:
            return None
        if activation is not None:
            if self._reviews is not None and (self._reviews.route_for(preview_id) is not None or self._reviews.correction_route_for(preview_id) is not None) and activation != route:
                raise PermissionError("Saved review activation must retain the exact staged revision/event")
            if (activation.repository, activation.repository_id, activation.organization_id) != (
                route.repository, route.repository_id, route.organization_id,
            ):
                raise PermissionError("Saved activation is outside this managed service")
            route = activation
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
                coordinator: CoordinatorProposal | None = None
                if strategy == "coordinator":
                    team = next(team for team in snapshot.definition.teams if team.id == configured.team)
                    binding = self._coordinators.get(str(team.coordinator))
                    if binding is None:
                        raise PermissionError("Coordinator has no trusted runtime proposal binding")
                    if isinstance(binding, CoordinatorBinding):
                        if (binding.coordinator_id, binding.event) != (team.coordinator, route.event):
                            raise PermissionError("Coordinator binding differs from activated coordinator/event")
                        coordinator = binding.proposal
                    else:
                        coordinator = binding
                reviews = self._reviews
                if reviews is not None and reviews.route_for(preview_id) is not None:
                    await _delivery_call(lambda: reviews.prepare(preview_id))
                else:
                    prepared = await _delivery_call(lambda: self._worker.prepare(preview_id))
                    if prepared.state != "prepared":
                        raise ValueError("Unowned delivery is not prepared; automatic replay is blocked")
                if coordinator is not None:
                    team = next(team for team in snapshot.definition.teams if team.id == configured.team)
                    self._actor.set(RuntimeActor("agent", str(team.coordinator)))
                    budget = DeveloperTaskBudget(path=self.budget_path(preview_id),
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
        route = next((route for route in (*self._routes, *self._review_routes, *self._correction_routes) if issue is not None and
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