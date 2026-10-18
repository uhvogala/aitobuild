"""Opt-in service bindings for approved configured organization tasks."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal, Mapping, assert_never

from filelock import Timeout as FileLockTimeout
from pydantic import Field, JsonValue, StrictInt, StrictStr

from aitobuild.agent_tools import DeveloperToolContext
from aitobuild.approval_consent import ConsentAdmissionEvidence
from aitobuild.approval_policy import ApprovalDecision, ApprovalPolicy, ApprovalStage
from aitobuild.developer_delivery import DeliveryPreparation, DeveloperDeliveryWorker
from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.developer_preview import (AutomaticAdmissionDeferred, AutomaticAdmissionLimitReached, DeveloperPreview,
                                         DeveloperPreviewRegistry, is_planned_child,
                                         preview_approval_operation, published_review_target,
                                         PLANNING_TASK_PREFIX, is_correction_task,
                                         REVIEW_TASK_PREFIX)
from aitobuild.organization import DefinitionModel, DefinitionSnapshot, DefinitionStore, EventName, Identifier, OrganizationDefinition
from aitobuild.organization_assignments import AssignmentProposal, AssignmentService, AssignmentStore, TaskAssignment
from aitobuild.organization_delivery import _delivery_call
from aitobuild.organization_runner import (
    ManagedOperation, ManagedRun, ManagedTaskContext, ManagedWorkflowRunner, RunStore, RuntimeActor, workflow_input_digest,
)
from aitobuild.organization_runtime import WorkflowLimits, WorkflowPredicate
from aitobuild.organization_reviews import PublishedReviewAdmission
from aitobuild.organization_planning import ManagedPlanning, PlanningAdmission
from aitobuild.organization_dependencies import ManagedDependencies
from aitobuild.tools.github import GitHubAdapter
from aitobuild.publish_approvals import publish_policy_operation


def _approved_scope_matches(preview: DeveloperPreview, run: ManagedRun) -> bool:
    """True only when the preview is still approved and its exact bundle matches the run's pinned scope digest."""
    content = json.dumps(preview.bundle_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return preview.approved and sha256(content.encode()).hexdigest() == run.scope_digest



class AdmissionRefused(PermissionError):
    """A configured (non-human) admission was refused by policy: the task stays unadmitted and waits for a human.

    Raised only by the configured-admission gate (``_admission_gate``/``_admit``) for the policy refusals, never for
    integrity failures (those stay plain ``PermissionError``). The managed worker catches exactly this type when it
    queues follow-ups (#69), so a refused automatic admission leaves the follow-up for a human instead of ending the
    worker loop."""


class Human(DefinitionModel):
    """A person approved the exact task (the HTTP approve endpoint)."""
    kind: Literal["human"] = "human"
    base_revision: StrictStr | None = None


class OperatorPolicy(DefinitionModel):
    """Configured automatic admission through the issue author/label allowlist."""
    kind: Literal["operator_policy"] = "operator_policy"


class PlannedChild(DefinitionModel):
    """A PM-planned child (or dependent handoff) qualifying through its human-approved plan (#31)."""
    kind: Literal["planned_child"] = "planned_child"


class InheritedReview(DefinitionModel):
    """A staged Architect review inheriting eligibility from its admitted parent (#37)."""
    kind: Literal["inherited_review"] = "inherited_review"


class Consent(DefinitionModel):
    """Accepted human consent from a discussion reply (#42): human-sourced, read by an automated adapter, kept as
    evidence in the consent journal. It covers exactly the frozen preview and base it was shown."""
    kind: Literal["consent"] = "consent"
    evidence: ConsentAdmissionEvidence
    base_revision: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{40}$")]


AdmissionEvidence = Annotated[Human | OperatorPolicy | PlannedChild | InheritedReview | Consent,
                              Field(discriminator="kind")]


def admit_task(previews: DeveloperPreviewRegistry, preview_id: str, evidence: AdmissionEvidence, *,
               base_revision: str | None = None, decision: ApprovalDecision | None = None,
               max_automatic_admissions: int | None = None,
               expected: DeveloperPreview | None = None) -> DeveloperPreview | None:
    """The only caller of DeveloperPreviewRegistry._approve_admission (PLAN 2.4). Configured evidence is checked by
    ManagedOrganizationService._admit first; `expected` is the exact preview those checks evaluated and must equal
    the stored one, since the decision digest omits source_payload. The limit and dedupe follow the resolved
    decision source."""
    match evidence:
        case Human():
            if decision is not None or base_revision is not None:
                raise ValueError("Human admission carries its own base revision and no policy decision")
            base_revision = evidence.base_revision
            consent = None
        case OperatorPolicy() | PlannedChild() | InheritedReview():
            if decision is None or expected is None:
                raise PermissionError("Configured admission requires a resolved decision and its evaluated preview")
            consent = None
        case Consent():
            if decision is None or expected is None or decision.source != "human" or base_revision != evidence.base_revision:
                raise PermissionError("Consent admission requires its live human decision, base and evaluated preview")
            consent = evidence.evidence
        case _:
            assert_never(evidence)
    # The limit follows the resolved decision source (human consent never counts); the registry keeps the
    # per-issue dedupe for consent as the one deliberate exception (PLAN 2.4).
    limit = max_automatic_admissions if decision is not None and decision.source == "operator_policy" else None
    return previews._approve_admission(preview_id, base_revision=base_revision, approval_policy=decision,
                                       expected=expected, max_automatic_admissions=limit, consent_approval=consent,
                                       evidence_kind=evidence.kind)


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
    approval_policy: ApprovalPolicy = ApprovalPolicy()
    organization_definition: OrganizationDefinition | None = None
    task_base_revision_for: Callable[[DeveloperPreview], str | None] | None = None
    queue_follow_ups: bool = False


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
        approval_policy: ApprovalPolicy = ApprovalPolicy(),
        task_base_revision_for: Callable[[DeveloperPreview], str | None] | None = None,
        correction_publication_github: GitHubAdapter | None = None,
        allow_mock_publication: bool = False,
        planning_publication: ManagedPlanning | None = None,
    ) -> None:
        self._definitions = definitions
        self._assignments = assignments
        self._runs = runs
        self._previews = previews
        self._worker = worker
        if approval_policy.task_admission == "automatic" and not callable(task_base_revision_for):
            raise ValueError("Automatic task admission requires a trusted pinned-base binding")
        self._task_base = task_base_revision_for
        if approval_policy.correction == "automatic" and (reviews is None or correction_publication_github is None):
            raise ValueError("Automatic correction requires trusted review and publication bindings")
        self._correction_github = correction_publication_github
        self._allow_mock_publication = allow_mock_publication
        if planning is not None and (planning.definitions is not definitions or planning.previews is not previews or
                                     planning.operator_id != operator_id):
            raise PermissionError("Planning must use service-owned definitions, previews and operator identity")
        self._planning = planning
        if planning_publication is not None and planning_publication.admission is not planning:
            raise PermissionError("Planning publication must use the exact service-owned admission binding")
        self._planning_publication = planning_publication
        self._planning_routes = tuple(ManagedRoute.model_validate(route.model_dump()) for route in planning.routes) if planning else ()
        if dependencies is not None and (
            dependencies.planning.admission is not planning or dependencies.worker is not worker or
            planning_publication is not None and dependencies.planning is not planning_publication):
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
            approval_policy=approval_policy,
            external_approval_stages=(ApprovalStage.TASK_ADMISSION, ApprovalStage.CORRECTION),
        )

    @property
    def preview_registry(self) -> DeveloperPreviewRegistry:
        return self._previews

    @property
    def approval_policy(self) -> ApprovalPolicy:
        return self._runner.approval_policy

    def _route(self, preview: DeveloperPreview) -> ManagedRoute | None:
        task_id = str(preview.bundle_payload.get("task_id", ""))
        if is_correction_task(preview) and self.approval_policy.correction == "disabled":
            raise PermissionError("Correction is disabled")
        if self._dependencies is None and task_id.startswith("dependent-issue-"):
            raise PermissionError("Dependent handoffs require their trusted admission binding")
        if self._dependencies is not None:
            owned = self._assignments.for_task(task_id)
            handoff_route = self._dependencies.route_for(preview, recheck=owned is None)
            if handoff_route is not None:
                return ManagedRoute.model_validate(handoff_route.model_dump())
        if task_id.startswith("planned-issue-"):
            if self._planning_publication is None:
                if self._dependencies is None:
                    raise PermissionError("Planned implementations require their trusted publication binding")
            else:
                parent_id = preview.source_payload.get("planning_assignment")
                parent = self._runs.get(parent_id) if isinstance(parent_id, str) else None
                if parent is None or preview.preview_id not in self._planning_publication.independent_preview_ids(parent):
                    raise PermissionError("Planned implementation requires its completed exact planning source")
                if self._assignments.for_task(task_id) is None:
                    self._planning_publication.check_independent_preview(preview, parent)
        if str(preview.bundle_payload.get("task_id", "")).startswith("pm-planning-"):
            if self._planning is None:
                raise PermissionError("Planning tasks require their trusted admission binding")
            planning_route = self._planning.route_for(preview.preview_id)
            return ManagedRoute.model_validate(planning_route.model_dump()) if planning_route else None
        if self._reviews is None and (str(preview.bundle_payload.get("task_id", "")).startswith(REVIEW_TASK_PREFIX)
                                      or is_correction_task(preview)):
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
        if route is None:
            return None
        preview = self._automatic_task_approval(preview)
        if not preview.approved:
            return None
        assignment = self._owned(preview, route)
        if assignment is not None:
            route = route.model_copy(update={"revision": assignment.revision, "event": assignment.event})
        return preview, route

    def _admission_gate(self, preview: DeveloperPreview) -> bool:
        """True when a configured (non-human) admission may be evaluated at all; False leaves the task waiting."""
        if self.approval_policy.task_admission == "disabled":
            raise AdmissionRefused("Task admission is disabled")
        if preview.approved or self.approval_policy.task_admission == "human":
            return False
        if preview.source_payload.get("action") == "edited":
            return False  # an edit never auto-admits; it waits for a human approval
        bundle = developer_task_bundle_from_payload(preview.bundle_payload)
        if bundle.task_id.startswith("pm-planning-") or bundle.issue_context is None:
            raise AdmissionRefused("Planning and non-repository tasks always require a human admission")
        return True

    @staticmethod
    def _evidence_for(preview: DeveloperPreview) -> OperatorPolicy | PlannedChild | InheritedReview:
        if published_review_target(preview) is not None:
            return InheritedReview()
        return PlannedChild() if is_planned_child(preview) else OperatorPolicy()

    def _automatic_task_approval(self, preview: DeveloperPreview) -> DeveloperPreview:
        if not self._admission_gate(preview):
            return preview
        return self._admit(preview, self._evidence_for(preview))

    def admit_consent(self, preview: DeveloperPreview, evidence: Consent) -> DeveloperPreview:
        """Admit the exact preview a consent adapter evaluated, through the one route (PLAN 2.4)."""
        return self._admit(preview, evidence)

    def _admit_consent(self, preview: DeveloperPreview, evidence: Consent) -> DeveloperPreview:
        if self.approval_policy.task_admission != "automatic":
            raise PermissionError("Consent task admission requires the automatic task-admission policy")
        if preview.source_payload.get("action") == "edited":
            raise PermissionError("An edited task needs a fresh consent")
        task_id = str(preview.bundle_payload.get("task_id", ""))
        if (task_id.startswith(REVIEW_TASK_PREFIX) or is_correction_task(preview)
                or published_review_target(preview) is not None):
            raise PermissionError("Consent cannot admit review or correction tasks")
        bundle = developer_task_bundle_from_payload(preview.bundle_payload)
        if bundle.task_id.startswith("pm-planning-") or bundle.issue_context is None:
            raise PermissionError("Planning and non-repository tasks always require a direct human admission")
        if bundle.issue_context.base_revision not in {None, evidence.base_revision}:
            raise PermissionError("Consent cannot replace an already pinned task base")
        payload = deepcopy(preview.bundle_payload)
        issue_payload = payload.get("issue_context")
        if not isinstance(issue_payload, dict):
            raise ValueError("Repository task requires its exact issue payload")
        issue_payload["base_revision"] = evidence.base_revision
        decision = self.approval_policy.resolve(
            ApprovalStage.TASK_ADMISSION, actor_id=evidence.evidence.consent.source.actor.actor_id,
            operation=preview_approval_operation(preview.preview_id, payload), automatic_supported=True, source="human")
        if decision != evidence.evidence.projection.decision:
            raise PermissionError("Consent decision differs from the live approval policy")
        approved = admit_task(self._previews, preview.preview_id, evidence, base_revision=evidence.base_revision,
                              decision=decision, expected=preview)
        if approved is None:
            raise PermissionError("Consented preview disappeared before admission")
        return approved

    def _admit(self, preview: DeveloperPreview,
               evidence: OperatorPolicy | PlannedChild | InheritedReview | Consent) -> DeveloperPreview:
        """Configured-evidence checks; the approval itself goes through the module-level admit_task (PLAN 2.4)."""
        # One policy snapshot governs this admission: the correction-mode check, the resolved decision (bound to this
        # snapshot's revision) and the automatic limit all read it, so a config change mid-admission cannot split them.
        policy = self.approval_policy
        # Every evidence that reaches _admit is non-Human (Human only enters admit_task from the HTTP approve
        # endpoint), so these refusals cover OperatorPolicy, PlannedChild, InheritedReview and Consent alike.
        if str(preview.bundle_payload.get("task_id", "")).startswith(PLANNING_TASK_PREFIX):
            # #31: only a direct human admission admits a planning task, whichever caller reached _admit.
            raise AdmissionRefused("Planning tasks always require a human admission")
        if is_correction_task(preview) and (isinstance(evidence, Consent) or policy.correction != "automatic"):
            # A correction is admitted automatically only under correction: automatic in this snapshot (the queued
            # native correction cycle); Consent never admits a correction in any mode (PLAN 2.4).
            raise AdmissionRefused("Correction tasks require a human admission unless correction is automatic")
        match evidence:
            case Consent():
                return self._admit_consent(preview, evidence)
            case OperatorPolicy() | PlannedChild() | InheritedReview():
                pass
            case _:
                assert_never(evidence)
        if self._evidence_for(preview) != evidence:
            raise PermissionError("Admission evidence does not match its task")
        bundle = developer_task_bundle_from_payload(preview.bundle_payload)
        eligibility: DeveloperPreview
        match evidence:
            case InheritedReview():
                # A staged Architect review is eligible only as the admitted parent task whose exact published head
                # it reviews; the parent then goes through the same planned-child check and trusted base callback.
                eligibility = self._admitted_review_parent(preview)
            case PlannedChild() | OperatorPolicy():
                eligibility = preview
            case _:
                assert_never(evidence)
        if is_planned_child(eligibility):
            self._check_planned_child(eligibility)
            parent_issue = developer_task_bundle_from_payload(eligibility.bundle_payload).issue_context
            if parent_issue is None or parent_issue.base_revision is None:
                raise PermissionError("Planned child admission requires its plan-pinned base revision")
        if self._task_base is None:
            raise PermissionError("Automatic task admission has no trusted base binding")
        base = self._task_base(eligibility)  # planned children: configured repository source required, allowlist replaced by the plan
        payload = deepcopy(preview.bundle_payload)
        if bundle.issue_context is not None:
            issue_payload = payload.get("issue_context")
            if not isinstance(issue_payload, dict):
                raise ValueError("Repository task requires its exact issue payload")
            if bundle.issue_context.base_revision is not None and base != bundle.issue_context.base_revision:
                raise PermissionError("Automatic admission cannot replace an already pinned task base")
            issue_payload["base_revision"] = base.lower() if isinstance(base, str) else base
        elif base is not None:
            raise PermissionError("Non-repository admission cannot add a repository base")
        decision = policy.resolve(ApprovalStage.TASK_ADMISSION, actor_id=self.operator_id,
            operation=preview_approval_operation(preview.preview_id, payload), automatic_supported=True,
            expected_revision=policy.revision)
        try:
            approved = admit_task(self._previews, preview.preview_id, evidence, base_revision=base, decision=decision, expected=preview,
                                  max_automatic_admissions=policy.max_automatic_admissions)
        except AutomaticAdmissionDeferred:
            return preview  # one automatic admission per repository/issue ID; later tasks wait for a human
        except AutomaticAdmissionLimitReached as limit:
            # The automatic limit is a policy refusal like the ones above: the task waits for a human (#69 follow-up).
            raise AdmissionRefused(str(limit)) from limit
        if approved is None:
            raise ValueError("Preview disappeared before configured admission")
        return approved

    def _admitted_review_parent(self, preview: DeveloperPreview) -> DeveloperPreview:
        """The admitted delivery task a staged review inherits eligibility from, bound to its delivery record and
        the exact published head; anything missing or mismatched fails closed."""
        target = published_review_target(preview)
        if target is None or self._reviews is None:
            raise PermissionError("Review admission requires its trusted review staging binding")
        parent_id, head_sha = target
        staged = self._reviews.target_for(preview.preview_id)  # staged receipt, unchanged snapshot, live exact head
        if (staged.preview_id, staged.head_sha) != (parent_id, head_sha):
            raise PermissionError("Review admission target differs from its staging receipt")
        record = self._worker.get(parent_id)
        if record is None or record.state != "published" or record.publication is None:
            raise PermissionError("Review admission requires the parent's published delivery record")
        if record.publication.get("head_sha") != head_sha:
            raise PermissionError("Review admission requires the exact published head of its parent")
        parent = self._previews.get(parent_id)
        # approved and approved_at are separate stored fields, and the delivery record is addressed by its directory,
        # so each is checked on its own (both have tests).
        if parent is None or not parent.approved or parent.approved_at is None or record.preview_id != parent_id:
            raise PermissionError("Review admission requires an admitted parent task")
        parent_task = str(parent.bundle_payload.get("task_id", ""))
        if parent_task.startswith(REVIEW_TASK_PREFIX):
            raise PermissionError("Only an admitted delivery task can pass eligibility to its review")
        # A correction parent is passed on as is: the trusted base callback refuses it (no issue allowlist and
        # not a planned child), so re-reviews of corrections stay human unless an operator's own binding says
        # otherwise (the queued native correction cycle relies on that binding).
        if record.bundle_payload != parent.bundle_payload:
            raise PermissionError("Parent delivery record differs from its admitted task")
        if preview.bundle_payload.get("issue_context") != parent.bundle_payload.get("issue_context"):
            raise PermissionError("Review issue and base differ from its admitted parent task")
        return parent

    def _check_planned_child(self, preview: DeveloperPreview) -> None:
        """A PM-planned child qualifies only through a human-admitted planning task and a human issue-creation
        approval of its exact plan digest; anything missing or mismatched fails closed."""
        publication = self._planning_publication
        if publication is None:
            raise PermissionError("Planned child admission requires the trusted planning publication binding")
        assignment_id = preview.source_payload.get("planning_assignment")
        if not isinstance(assignment_id, str) or not assignment_id:
            raise PermissionError("Planned child admission requires its stored planning assignment")
        run = self._runs.get(assignment_id)
        if run is None or run.state != "completed":
            raise PermissionError("Planned child admission requires its completed planning run")
        try:
            plan = publication.plans.get(assignment_id)
            saved = publication.stored_publication(assignment_id, plan)
        except (OSError, ValueError) as error:
            raise PermissionError("Planned child admission requires its stored plan and publication") from error
        task_id = str(preview.bundle_payload.get("task_id", ""))
        if task_id.startswith("dependent-issue-"):
            if self._dependencies is None:
                raise PermissionError("Dependent child admission requires its trusted handoff binding")
            try:
                handoff = self._dependencies.stored_handoff(task_id)
            except (OSError, ValueError) as error:
                raise PermissionError("Dependent child admission requires its stored handoff record") from error
            if handoff.planning_assignment_id != assignment_id:
                raise PermissionError("Dependent child handoff belongs to a different planning assignment")
            digest: object = handoff.plan_digest
        else:
            digest = preview.source_payload.get("plan_digest")
            if not isinstance(digest, str):
                raise PermissionError("Planned child admission requires its stored plan digest")
        # stored_publication refuses a non-pending publication without approved_at, so "published" implies it.
        if digest != plan.digest or saved.plan_digest != plan.digest or saved.state != "published":
            raise PermissionError("Planned child plan digest differs from its stored published plan")
        planning = self._previews.get(str(plan.pins.get("preview_id", "")))
        if (planning is None or not planning.approved or planning.approved_at is None or
                planning.approved_at.isoformat() != plan.pins.get("approved_at")):
            raise PermissionError("Planned child admission requires its exact approved planning task")
        if planning.approval_policy is None:
            # The human approve endpoint (dispatcher/registry approve without a policy) saves no decision record;
            # automatic admission always saves an operator_policy record, so an approved preview without one is human.
            pass
        elif planning.approval_policy.source != "human":
            raise PermissionError("Planned child admission requires a human-admitted planning task")
        expected = workflow_input_digest(publication.approval_data(plan, saved))
        matches = [item for item in run.decisions if item.data_digest == expected]
        if len(matches) != 1:
            raise PermissionError("Planned child admission requires exactly one recorded issue-creation decision for its plan")
        decision = matches[0]
        if (decision.kind != "service_approval" or decision.value is not True or decision.approval_policy is None or
                decision.approval_policy.stage != ApprovalStage.PM_ISSUE_CREATION):
            raise PermissionError("Planned child admission requires an approved issue-creation decision for its plan")
        if decision.approval_policy.source != "human":
            raise PermissionError("Planned child admission requires a human issue-creation approval")

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
            if not preview.approved and self.approval_policy.task_admission == "disabled":
                raise PermissionError("Task admission is disabled")

    async def offer_dependency_handoff(self, *, planning_assignment_id: str, issue_key: str, base_revision: str) -> dict[str, Any]:
        dependencies = self._dependencies
        if dependencies is None:
            raise PermissionError("Dependency readiness requires an explicit operator binding")
        return await _delivery_call(lambda: dependencies.offer(
            planning_assignment_id=planning_assignment_id, issue_key=issue_key, base_revision=base_revision))

    async def offer_correction(self, review_preview_id: str) -> DeveloperPreview | None:
        if self.approval_policy.correction == "disabled":
            raise PermissionError("Correction is disabled")
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
        if self.approval_policy.correction == "disabled":
            raise PermissionError("Correction is disabled")
        reviews = self._correction_reviews()
        binding = await _delivery_call(lambda: reviews.correction_publication_binding(correction_preview_id))
        return await _delivery_call(lambda: self._worker.stage_correction_publication(
            correction_preview_id, review_receipt_digest=binding))

    async def publish_correction(
        self, correction_preview_id: str, *, approval_digest: str, github: GitHubAdapter,
        allow_mock_publication: bool = False,
        _policy_source: Literal["human", "operator_policy"] = "human",
    ) -> tuple[DeliveryPreparation, DeveloperPreview | None]:
        """Consume the operator's exact approval, fast-forward the pinned PR, then stage the next Architect review."""
        reviews = self._correction_reviews()
        binding = await _delivery_call(lambda: reviews.correction_publication_binding(correction_preview_id))
        saved = await _delivery_call(lambda: self._worker.publication_approval(correction_preview_id))
        if saved is None or saved.state in {"pending", "invalidated"}:
            await self.stage_correction_publication(correction_preview_id)
            saved = await _delivery_call(lambda: self._worker.publication_approval(correction_preview_id))
        if saved is None:
            raise PermissionError("Correction requires an exact staged publication approval")
        run = self.status(correction_preview_id)
        if run is None:
            # #65: a correction is admitted and verified only through its managed run (PLAN), so a missing run is
            # never a legitimate publication; refusing keeps the pinned-policy rule from being skipped.
            raise PermissionError("Correction publication requires its managed correction run")
        pinned = self._runner.require_pinned_policy(run)  # the one #65 rule; refuses an unpinned run
        decision = self.approval_policy.resolve(ApprovalStage.CORRECTION, actor_id=self.operator_id,
            operation=publish_policy_operation(approval_digest, saved.snapshot),
            expected_revision=pinned,
            automatic_supported=self._correction_github is not None, source=_policy_source)
        if saved.approval_policy is not None and saved.state in {"consuming", "consumed"}:
            if saved.approval_policy.policy_revision != decision.policy_revision:
                raise PermissionError("Correction publication policy revision changed")
            decision = saved.approval_policy
        record = await _delivery_call(lambda: self._worker.approve_and_publish_correction(
            correction_preview_id, approval_digest=approval_digest, review_receipt_digest=binding,
            actor_id=self.operator_id, github=github, require_human_approval_for_repo_writes=True,
            allow_mock_publication=allow_mock_publication,
            approval_policy=decision,
        ))
        run = self.status(correction_preview_id)
        if run is not None and run.state == "completed" and run.cleanup_succeeded is True:
            followup = await _delivery_call(lambda: reviews.follow_up(correction_preview_id, run))
            review_id = followup.get("next_preview_id")
            review = self._previews.get(review_id) if isinstance(review_id, str) else None
        else:
            review = await _delivery_call(lambda: reviews.offer(correction_preview_id))
        return record, review

    async def publish_correction_by_policy(self, correction_preview_id: str) -> tuple[DeliveryPreparation, DeveloperPreview | None]:
        if self.approval_policy.correction != "automatic" or self._correction_github is None:
            raise PermissionError("Automatic correction publication is not activated")
        saved = await _delivery_call(lambda: self._worker.publication_approval(correction_preview_id))
        if saved is None or saved.state in {"pending", "invalidated"}:
            staged = await self.stage_correction_publication(correction_preview_id)
            digest = staged["digest"]
        else:
            digest = saved.digest
        return await self.publish_correction(correction_preview_id, approval_digest=digest,
            github=self._correction_github, allow_mock_publication=self._allow_mock_publication,
            _policy_source="operator_policy")

    async def retire_correction(
        self, correction_preview_id: str, *, github: GitHubAdapter, allow_mock_publication: bool = False,
    ) -> DeliveryPreparation:
        """Operator retire of a failed/abandoned correction while the live PR head still equals its parent."""
        reviews = self._correction_reviews()
        await _delivery_call(lambda: reviews.correction_publication_binding(correction_preview_id))
        return await _delivery_call(lambda: self._worker.retire_correction(
            correction_preview_id, actor_id=self.operator_id, github=github,
            allow_mock_publication=allow_mock_publication))
    async def retire_delivery(
        self, preview_id: str, *, github: GitHubAdapter, reason: str | None = None,
        allow_mock_publication: bool = False,
    ) -> DeliveryPreparation:
        """The generalized operator retire (#61). Corrections go through `retire_correction` unchanged; any other
        delivery may only retire a stuck `publishing` record, while its managed run is not executing."""
        preview = self._previews.get(preview_id)
        if preview is None:
            raise PermissionError("Retiring requires an existing approved preview")
        task_id = str(preview.bundle_payload.get("task_id", ""))
        if is_correction_task(preview):  # the full rule: task ID or correction_from_review source
            if reason is not None:
                raise PermissionError("Correction retire records no reason; omit it")
            return await self.retire_correction(preview_id, github=github,
                                                allow_mock_publication=allow_mock_publication)
        assignment = self.recorded_assignment(task_id)
        # The runner holds this lock for every invoke/approve/resume; a run left `running` by a kill does not.
        lock = self._runs.invocation_lock(assignment.assignment_id) if assignment is not None else None
        if lock is not None:
            try:
                lock.acquire()
            except FileLockTimeout as error:
                raise PermissionError("The managed run for this delivery is executing; retire is refused") from error
        try:
            return await _delivery_call(lambda: self._worker.retire_delivery(
                preview_id, actor_id=self.operator_id, github=github, reason=reason,
                allow_mock_publication=allow_mock_publication))
        finally:
            if lock is not None:
                lock.release()

    def follow_up_status(self, run_id: str) -> dict[str, JsonValue] | None:
        return self._reviews.follow_up_status(run_id) if self._reviews is not None else None

    def follow_up_preview_id(self, preview_id: str) -> str | None:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ValueError("Follow-up source preview not found")
        run = self.recorded_run(str(preview.bundle_payload["task_id"]))
        if run is None or run.state != "completed" or run.cleanup_succeeded is not True:
            return None
        if self._reviews is None:
            return None
        if not _approved_scope_matches(preview, run):
            raise PermissionError("Follow-up source approval/scope changed")
        return self._reviews.follow_up_preview_id(preview_id, run)

    def planning_follow_up_preview_ids(self, preview_id: str) -> tuple[str, ...]:
        publication = self._planning_publication
        if publication is None or publication.admission.request_for(preview_id) is None:
            return ()
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ValueError("Planning follow-up source preview not found")
        run = self.recorded_run(str(preview.bundle_payload["task_id"]))
        if run is None or run.state != "completed" or run.cleanup_succeeded is not True:
            return ()
        if not _approved_scope_matches(preview, run):
            raise PermissionError("Planning follow-up source approval/scope changed")
        return publication.independent_preview_ids(run)

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
        if self.approval_policy.correction == "automatic" and run.state == "completed" and run.cleanup_succeeded is True:
            assignment = self._assignments.get(run.assignment_id)
            preview = self._previews.get(preview_id)
            if assignment is not None and preview is not None and is_correction_task(preview):
                record = await _delivery_call(lambda: self._worker.get(preview_id))
                if record is not None and record.state in {"verified", "publishing", "published"}:
                    await self.publish_correction_by_policy(preview_id)
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
        if route is None:
            return None
        preview = self._automatic_task_approval(preview)
        if not preview.approved:
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

    async def override_approval_policy(self, assignment_id: str, *, evidence: AdmissionEvidence, reason: str) -> ManagedRun:
        """#65: a human lets this run continue under the live approval policy instead of its pinned one.

        The evidence is the same admission evidence the one admission route takes; only `Human` (the authenticated
        operator endpoint, as for task approval) can override. The runner records the entry and starts no work."""
        match evidence:
            case Human():
                if evidence.base_revision is not None:
                    raise ValueError("A policy override carries no task base revision")
            case OperatorPolicy() | PlannedChild() | InheritedReview() | Consent():
                raise PermissionError(f"Approval policy override requires human evidence, not {evidence.kind}")
            case _:
                assert_never(evidence)
        self._assignment(assignment_id)
        token = self._actor.set(self._operator)
        try:
            return await self._runner.override_policy(assignment_id, evidence_kind=evidence.kind, reason=reason)
        finally:
            self._actor.reset(token)

    async def cancel(self, assignment_id: str) -> ManagedRun:
        self._assignment(assignment_id)
        token = self._actor.set(self._operator)
        try:
            return await self._runner.cancel(assignment_id)
        finally:
            self._actor.reset(token)