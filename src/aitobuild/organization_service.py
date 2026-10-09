"""Opt-in service bindings for approved configured organization tasks."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from pydantic import Field, JsonValue, StrictInt, StrictStr

from aitobuild.agent_tools import DeveloperToolContext
from aitobuild.developer_delivery import DeliveryPreparation, DeveloperDeliveryWorker
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
from aitobuild.organization_planning import PlanningAdmission
from aitobuild.organization_dependencies import ManagedDependencies
from aitobuild.tools.github import GitHubAdapter


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
        automatic_review_followups: bool = False,
        planning: PlanningAdmission | None = None,
        dependencies: ManagedDependencies | None = None,
    ) -> None:
        self._definitions = definitions
        self._assignments = assignments
        self._runs = runs
        self._previews = previews
        self._worker = worker
        if planning is not None and (planning.definitions is not definitions or planning.previews is not previews or
                                     planning.operator_id != operator_id):
            raise PermissionError("Planning must use service-owned definitions, previews and operator identity")
        self._planning = planning
        self._planning_routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in planning.routes) if planning else ()
        if dependencies is not None and (
                dependencies.planning.admission is not planning or dependencies.worker is not worker):
            raise PermissionError("Dependencies must use the exact service-owned planning and worker bindings")
        self._dependencies = dependencies
        self._dependency_routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in dependencies.routes) if dependencies else ()
        if reviews is not None:
            reviews.validate_binding(definitions=definitions, previews=previews, worker=worker)
        self._reviews = reviews
        if type(automatic_review_followups) is not bool or automatic_review_followups and reviews is None:
            raise ValueError("Automatic review follow-ups require an explicit review admission binding")
        self._automatic_followups = automatic_review_followups
        self._routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in routes)
        self._review_routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in reviews.routes) if reviews else ()
        self._correction_routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in reviews.correction_routes) if reviews else ()
        targets = [(route.repository, route.repository_id) for route in self._routes]
        if len(targets) != len(set(targets)):
            raise ValueError("Managed repository routes must be unambiguous")
        for route in (*self._routes, *self._review_routes, *self._correction_routes, *self._planning_routes, *self._dependency_routes):
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
        task_id = str(preview.bundle_payload.get("task_id", ""))
        if self._dependencies is None and task_id.startswith("dependent-issue-"):
            raise PermissionError("Dependent handoffs require their trusted admission binding")
        if self._dependencies is not None:
            owned = self._assignments.for_task(task_id)
            handoff_route = self._dependencies.route_for(preview, recheck=owned is None)
            if handoff_route is not None:
                return ManagedRoute.model_validate(handoff_route.model_dump())
        if str(preview.bundle_payload.get("task_id", "")).startswith("pm-planning-"):
            if self._planning is None:
                raise PermissionError("Planning tasks require their trusted admission binding")
            planning_route = self._planning.route_for(preview.preview_id)
            return ManagedRoute.model_validate(planning_route.model_dump()) if planning_route else None
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
        if self._planning is not None:
            path = self._planning.original_budget_path(preview_id)
            if path is not None:
                return path
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

    async def check_preview_approval(self, preview_id: str) -> None:
        preview = self._previews.get(preview_id)
        if preview is not None:
            await _delivery_call(lambda: self._route(preview))

    async def offer_dependency_handoff(self, *, planning_assignment_id: str, issue_key: str, base_revision: str) -> dict[str, Any]:
        dependencies = self._dependencies
        if dependencies is None:
            raise PermissionError("Dependency readiness requires an explicit operator binding")
        return await _delivery_call(lambda: dependencies.offer(
            planning_assignment_id=planning_assignment_id, issue_key=issue_key, base_revision=base_revision))

    async def offer_correction(self, review_preview_id: str) -> DeveloperPreview | None:
        reviews = self._reviews
        if reviews is None:
            return None
        run = self.status(review_preview_id)
        if run is None:
            raise PermissionError("Correction requires a completed managed review")
        return await _delivery_call(lambda: reviews.offer_correction(review_preview_id, run))

    def _correction_reviews(self) -> PublishedReviewAdmission:
        if self._reviews is None:
            raise PermissionError("Same-PR correction publication requires the trusted review admission binding")
        return self._reviews

    async def stage_correction_publication(self, correction_preview_id: str) -> dict[str, Any]:
        """Stage the exact same-PR push (diff, pinned head, PR number) for one-use operator approval."""
        reviews = self._correction_reviews()
        binding = await _delivery_call(lambda: reviews.correction_publication_binding(correction_preview_id))
        return await _delivery_call(lambda: self._worker.stage_correction_publication(
            correction_preview_id, review_receipt_digest=binding))

    async def publish_correction(
        self, correction_preview_id: str, *, approval_digest: str, github: GitHubAdapter,
        allow_mock_publication: bool = False,
    ) -> tuple[DeliveryPreparation, DeveloperPreview | None]:
        """Consume the operator's exact approval, fast-forward the pinned PR, then stage the next Architect review."""
        reviews = self._correction_reviews()
        binding = await _delivery_call(lambda: reviews.correction_publication_binding(correction_preview_id))
        record = await _delivery_call(lambda: self._worker.approve_and_publish_correction(
            correction_preview_id, approval_digest=approval_digest, review_receipt_digest=binding,
            actor_id=self.operator_id, github=github, require_human_approval_for_repo_writes=True,
            allow_mock_publication=allow_mock_publication,
        ))
        run = self.status(correction_preview_id)
        if run is not None and run.state == "completed" and run.cleanup_succeeded is True:
            followup = await _delivery_call(lambda: reviews.follow_up(correction_preview_id, run))
            review_id = followup.get("next_preview_id")
            review = self._previews.get(review_id) if isinstance(review_id, str) else None
        else:
            review = await _delivery_call(lambda: reviews.offer(correction_preview_id))
        return record, review

    async def retire_correction(
        self, correction_preview_id: str, *, github: GitHubAdapter, allow_mock_publication: bool = False,
    ) -> DeliveryPreparation:
        """Operator retire of a failed/abandoned correction while the live PR head still equals its parent."""
        reviews = self._correction_reviews()
        await _delivery_call(lambda: reviews.correction_publication_binding(correction_preview_id))
        return await _delivery_call(lambda: self._worker.retire_correction(
            correction_preview_id, actor_id=self.operator_id, github=github,
            allow_mock_publication=allow_mock_publication))
    def follow_up_status(self, run_id: str) -> dict[str, JsonValue] | None:
        return self._reviews.follow_up_status(run_id) if self._reviews is not None else None

    async def offer_follow_up(self, preview_id: str) -> dict[str, JsonValue] | None:
        reviews = self._reviews
        if reviews is None:
            return None
        run = self.status(preview_id)
        if run is None:
            raise PermissionError("Follow-up requires a saved completed managed run")
        return await _delivery_call(lambda: reviews.follow_up(preview_id, run))

    async def _follow_up(self, preview_id: str, run: ManagedRun) -> ManagedRun:
        reviews = self._reviews
        if self._automatic_followups and reviews is not None and run.state == "completed" and run.cleanup_succeeded is True:
            await _delivery_call(lambda: reviews.follow_up(preview_id, run))
        return run

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
            if str(preview.bundle_payload.get("task_id", "")).startswith("dependent-issue-") and activation != route:
                raise PermissionError("Saved dependency activation must retain the exact staged revision/event")
            if ((self._planning is not None and self._planning.route_for(preview_id) is not None) or
                    self._reviews is not None and (self._reviews.route_for(preview_id) is not None or self._reviews.correction_route_for(preview_id) is not None)) and activation != route:
                raise PermissionError("Saved planning/review activation must retain the exact staged revision/event")
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
                    return await self._follow_up(preview_id, await self._runner.start(assignment.assignment_id))
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
                if str(preview.bundle_payload.get("task_id", "")).startswith("dependent-issue-"):
                    checked_route = await _delivery_call(lambda: self._route(preview))
                    if checked_route != route:
                        raise PermissionError("Dependency activation changed before task preparation")
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
                planning = self._planning
                if planning is not None and planning.route_for(preview_id) is not None:
                    await _delivery_call(lambda: planning.prepare(preview_id))
                elif reviews is not None and reviews.route_for(preview_id) is not None:
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
                return await self._follow_up(preview_id, await self._runner.start(assignment.assignment_id))
        finally:
            self._actor.reset(token)

    def _assignment(self, assignment_id: str) -> TaskAssignment:
        assignment = self._assignments.get(assignment_id)
        if assignment is None:
            raise ValueError("Assignment not found")
        issue = assignment.bundle.issue_context
        if assignment.task_id.startswith("pm-planning-"):
            if self._planning is None:
                raise PermissionError("Planning assignment requires its trusted admission binding")
            planning_route = self._planning.owned_route(assignment.task_id, assignment.preview_id)
            if (planning_route.organization_id != assignment.organization_id or self._assignments.for_task(assignment.task_id) != assignment):
                raise PermissionError("Planning assignment is outside this managed activation")
            return assignment
        route = next((route for route in (*self._routes, *self._review_routes, *self._correction_routes, *self._dependency_routes) if issue is not None and
                      (route.repository, route.repository_id, route.organization_id) ==
                      (issue.repository, issue.repository_id, assignment.organization_id)), None)
        if route is None or self._assignments.for_task(assignment.task_id) != assignment:
            raise PermissionError("Assignment is outside this managed activation")
        return assignment

    def status(self, preview_id: str) -> ManagedRun | None:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ValueError("Preview not found")
        owner = self._assignments.for_task(str(preview.bundle_payload.get("task_id", "")))
        if owner is not None and owner.preview_id == preview_id:
            self._assignment(owner.assignment_id)
            terminal = self._runs.get(owner.assignment_id)
            if terminal is not None and terminal.state in {"completed", "failed", "cancelled"}:
                return terminal
        route = self._route(preview)
        if route is None:
            raise PermissionError("Preview is outside this managed activation")
        assignment = self._owned(preview, route)
        return self._runs.get(assignment.assignment_id) if assignment else None

    async def decide(self, assignment_id: str, *, request_id: str, approved: bool) -> ManagedRun:
        assignment = self._assignment(assignment_id)
        token = self._actor.set(self._operator)
        try:
            run = await self._runner.approve(assignment_id, request_id=request_id, approved=approved)
            return await self._follow_up(assignment.preview_id, run)
        finally:
            self._actor.reset(token)

    async def respond(self, assignment_id: str, *, request_id: str, response: JsonValue) -> ManagedRun:
        assignment = self._assignment(assignment_id)
        token = self._actor.set(self._operator)
        try:
            run = await self._runner.resume(assignment_id, request_id=request_id, response=response)
            return await self._follow_up(assignment.preview_id, run)
        finally:
            self._actor.reset(token)

    async def cancel(self, assignment_id: str) -> ManagedRun:
        self._assignment(assignment_id)
        token = self._actor.set(self._operator)
        try:
            return await self._runner.cancel(assignment_id)
        finally:
            self._actor.reset(token)