"""Opt-in bounded PM planning and exact, receipt-based issue publication."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from filelock import FileLock
from pydantic import Field, JsonValue, StrictInt, StrictStr, model_validator

from aitobuild.developer_isolation import (
    DeveloperIsolationPolicy, DeveloperIssueContext, DeveloperTaskBudget, build_developer_task_bundle,
    developer_task_bundle_from_payload,
)
from aitobuild.developer_preview import DeveloperPreview, DeveloperPreviewRegistry
from aitobuild.durable_files import atomic_write_text
from aitobuild.organization import DefinitionModel, DefinitionStore, EventName, Identifier
from aitobuild.organization_runner import ManagedTaskContext
from aitobuild.policy import AgentRole
from aitobuild.tools.github import GitHubAdapter, GitHubIssue, normalize_repository_name


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class PlanningLimits(DefinitionModel):
    max_issues: StrictInt = Field(default=8, gt=0)
    max_plan_bytes: StrictInt = Field(default=16000, gt=0)
    max_criteria_per_issue: StrictInt = Field(default=16, gt=0)
    max_dependencies_per_issue: StrictInt = Field(default=8, ge=0)


class PlanningScope(DefinitionModel):
    repository: StrictStr
    repository_id: StrictInt = Field(gt=0)
    base_revision: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    base_branch: StrictStr = Field(min_length=1)
    limits: PlanningLimits = PlanningLimits()

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if (normalize_repository_name(self.repository) != self.repository or
                self.base_branch.startswith(("/", "-")) or ".." in self.base_branch or
                any(character.isspace() for character in self.base_branch)):
            raise ValueError("Planning requires a canonical repository and plain base branch")
        return self


class PlannedIssue(DefinitionModel):
    key: Identifier
    title: StrictStr = Field(min_length=1, max_length=256)
    objective: StrictStr = Field(min_length=1)
    acceptance_criteria: tuple[StrictStr, ...] = Field(min_length=1)
    dependencies: tuple[Identifier, ...] = ()
    labels: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_text(self) -> Self:
        texts = (self.title, self.objective, *self.acceptance_criteria, *self.labels)
        if any(not text.strip() or text != text.strip() or "\x00" in text or
               "<!-- aitobuild-plan:" in text for text in texts):
            raise ValueError("Issue content must be nonblank, exact and free of reserved markers")
        if any("\n" in text or "\r" in text for text in (self.title, *self.acceptance_criteria, *self.labels)):
            raise ValueError("Titles, criteria and labels must each fit one line")
        if (len(set(self.dependencies)) != len(self.dependencies) or
                len(set(self.labels)) != len(self.labels) or self.key in self.dependencies):
            raise ValueError("Issue dependencies and labels must be unique and cannot depend on self")
        return self


class PlanProposal(DefinitionModel):
    issues: tuple[PlannedIssue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        graph = {issue.key: issue.dependencies for issue in self.issues}
        if len(graph) != len(self.issues) or any(key not in graph for dependencies in graph.values() for key in dependencies):
            raise ValueError("Plan issue keys must be unique and dependencies must exist in this plan")
        visited: set[str] = set()
        pending: set[str] = set()

        def visit(key: str) -> None:
            if key in pending:
                raise ValueError("Plan dependencies cannot contain a cycle")
            if key not in visited:
                pending.add(key)
                for dependency in graph[key]:
                    visit(dependency)
                pending.remove(key)
                visited.add(key)

        for key in graph:
            visit(key)
        return self

    def check_limits(self, limits: PlanningLimits) -> None:
        if (len(self.issues) > limits.max_issues or len(self.model_dump_json().encode()) > limits.max_plan_bytes or
                any(len(issue.acceptance_criteria) > limits.max_criteria_per_issue or
                    len(issue.dependencies) > limits.max_dependencies_per_issue for issue in self.issues)):
            raise ValueError("Plan exceeds the approved planning limits")


class PlanningRoute(DefinitionModel):
    repository: StrictStr
    repository_id: StrictInt = Field(gt=0)
    organization_id: Identifier
    revision: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    event: EventName


class _PlanningRequest(DefinitionModel):
    request_id: StrictStr
    scope: PlanningScope
    route: PlanningRoute
    operator_id: StrictStr
    bundle_content: StrictStr
    developer_policy: dict[str, JsonValue]
    preview_id: StrictStr | None = None
    ledger_state: Literal["none", "creating", "ready"] = "none"
    approved_at: StrictStr | None = None
    deadline: float | None = Field(default=None, allow_inf_nan=False)


class PlanningAdmission:
    def __init__(self, *, definitions: DefinitionStore, previews: DeveloperPreviewRegistry,
                 state_dir: Path, routes: tuple[PlanningRoute, ...], operator_id: str) -> None:
        self.definitions, self.previews = definitions, previews
        self.directory = state_dir.absolute()
        self.routes, self.operator_id = routes, operator_id
        if self.directory.resolve() != self.directory or not operator_id.strip():
            raise ValueError("Planning admission requires trusted nonsymlinked storage and an operator")
        self.directory.mkdir(parents=True, exist_ok=True)
        if len({(route.repository, route.repository_id) for route in routes}) != len(routes):
            raise ValueError("Planning routes must be unambiguous")
        for route in routes:
            snapshot = definitions.get(route.organization_id, route.revision)
            configured = next((item for item in snapshot.definition.routes if route.event in item.events), None) if snapshot else None
            if snapshot is None or configured is None or any(agent.role != AgentRole.PM for agent in snapshot.definition.agents
                                         if agent.id in configured.delegation.eligible_agents):
                raise ValueError("Planning requires an explicit PM-only revision/event route")

    def _path(self, task_id: str) -> Path:
        path = self.directory / (sha256(task_id.encode()).hexdigest() + ".json")
        if path.resolve() != path:
            raise ValueError("Planning receipts cannot follow symlinks")
        return path

    def _load(self, task_id: str) -> _PlanningRequest:
        record = _PlanningRequest.model_validate_json(self._path(task_id).read_text())
        bundle = json.loads(record.bundle_content)
        if (task_id != "pm-planning-" + sha256(record.request_id.encode()).hexdigest() or
            bundle["task_id"] != task_id or bundle["constraints"] != [canonical(record.scope.model_dump(mode="json")),
                                         canonical(record.developer_policy)] or
            (record.route.repository, record.route.repository_id) != (record.scope.repository, record.scope.repository_id) or
            (record.ledger_state == "none") != (record.approved_at is None) or
            (record.ledger_state == "ready") != (record.deadline is not None)):
            raise PermissionError("Planning request address or approved scope changed")
        return record

    def offer(self, *, request_id: str, objective: str, scope: PlanningScope,
              developer_policy: DeveloperIsolationPolicy) -> DeveloperPreview:
        scope = PlanningScope.model_validate(scope.model_dump())
        if not request_id.strip():
            raise ValueError("Planning requires an explicit stable request identity")
        route = next((route for route in self.routes if (route.repository, route.repository_id) ==
                      (scope.repository, scope.repository_id)), None)
        if route is None:
            raise PermissionError("Planning repository is not activated")
        child = build_developer_task_bundle(task_id="policy-probe", objective=objective,
                                            acceptance_criteria=["Operator approves implementation separately"],
                                            constraints=[], context_files=[], policy=developer_policy).to_payload()["policy"]
        child = json.loads(canonical(child))
        task_id = "pm-planning-" + sha256(request_id.encode()).hexdigest()
        bundle = build_developer_task_bundle(
            task_id=task_id, objective=objective, acceptance_criteria=["Draft bounded objectives, criteria and dependencies"],
            constraints=[canonical(scope.model_dump(mode="json")), canonical(child)], context_files=[],
            policy=replace(developer_policy, max_file_changes=0, allowed_command_prefixes=()),
        )
        record = _PlanningRequest(request_id=request_id, scope=scope, route=route, operator_id=self.operator_id,
                                  bundle_content=canonical(bundle.to_payload()), developer_policy=child)
        path = self._path(task_id)
        with FileLock(str(path) + ".lock"):
            if path.exists():
                saved = self._load(task_id)
                if saved.model_dump(exclude={"preview_id", "ledger_state", "approved_at", "deadline"}) != record.model_dump(
                        exclude={"preview_id", "ledger_state", "approved_at", "deadline"}):
                    raise PermissionError("Planning request/revision/scope drift requires a fresh request and approval")
                record = saved
            else:
                if self.previews.get_by_dedupe(task_id) is not None:
                    raise PermissionError("Lost planning receipt cannot initialize a replacement ledger")
                atomic_write_text(path, record.model_dump_json())
            preview = self.previews.create_or_get(dedupe_key=task_id, task_key=task_id,
                                                 bundle_payload=json.loads(record.bundle_content),
                                                 source_payload={"planning_request": request_id})
            if record.preview_id is not None and record.preview_id != preview.preview_id:
                raise PermissionError("Lost planning preview cannot be recreated")
            if record.preview_id is None:
                record = record.model_copy(update={"preview_id": preview.preview_id})
                atomic_write_text(path, record.model_dump_json())
            return preview

    def request_for(self, preview_id: str) -> _PlanningRequest | None:
        preview = self.previews.get(preview_id)
        if preview is None:
            raise ValueError("Planning preview not found")
        task_id = str(preview.bundle_payload.get("task_id", ""))
        if not task_id.startswith("pm-planning-"):
            return None
        record = self._load(task_id)
        if record.preview_id != preview_id or canonical(preview.bundle_payload) != record.bundle_content:
            raise PermissionError("Planning preview differs from its pinned request")
        if record.operator_id != self.operator_id:
            raise PermissionError("Planning operator identity changed")
        self.owned_route(task_id, preview_id)
        return record

    def route_for(self, preview_id: str) -> PlanningRoute | None:
        record = self.request_for(preview_id)
        return record.route if record else None

    def owned_route(self, task_id: str, preview_id: str) -> PlanningRoute:
        record = self._load(task_id)
        if record.preview_id != preview_id or not any(
                (route.repository, route.repository_id, route.organization_id) ==
                (record.route.repository, record.route.repository_id, record.route.organization_id) for route in self.routes):
            raise PermissionError("Planning ownership is outside this activation")
        return record.route

    def original_budget_path(self, preview_id: str) -> Path | None:
        matches = []
        for path in self.directory.glob("*.json"):
            if path.resolve() != path:
                raise PermissionError("Planning receipt cannot follow symlinks")
            record = _PlanningRequest.model_validate_json(path.read_text())
            matches.append(self._load("pm-planning-" + sha256(record.request_id.encode()).hexdigest()))
        matches = [record for record in matches if record.preview_id == preview_id]
        if not matches:
            return None
        if len(matches) != 1 or matches[0].ledger_state != "ready":
            raise PermissionError("Planning original ledger is ambiguous or not initialized")
        path = self.budget_path(preview_id)
        if json.loads(path.read_text())["deadline"] != matches[0].deadline:
            raise PermissionError("Planning original deadline changed")
        return path

    def budget_path(self, preview_id: str) -> Path:
        path = self.directory / "budgets" / (sha256(preview_id.encode()).hexdigest() + ".json")
        if path.resolve() != path:
            raise ValueError("Planning budgets cannot follow symlinks")
        return path

    def prepare(self, preview_id: str) -> None:
        record = self.request_for(preview_id)
        preview = self.previews.get(preview_id)
        if record is None or preview is None or not preview.approved or preview.approved_at is None:
            raise PermissionError("Planning requires explicit task approval")
        path = self._path(str(preview.bundle_payload["task_id"]))
        with FileLock(str(path) + ".lock"):
            record = self.request_for(preview_id)
            if record is None:
                raise PermissionError("Planning request disappeared")
            approved_at = preview.approved_at.isoformat()
            bundle = developer_task_bundle_from_payload(preview.bundle_payload)
            if record.ledger_state != "none":
                if record.approved_at != approved_at or record.ledger_state != "ready":
                    raise PermissionError("Ambiguous planning ledger initialization cannot replay")
                budget = DeveloperTaskBudget(path=self.budget_path(preview_id), bundle=bundle, create=False)
                if json.loads(budget.path.read_text())["deadline"] != record.deadline:
                    raise PermissionError("Original planning deadline changed")
                budget.remaining_seconds()
                return
            record = record.model_copy(update={"ledger_state": "creating", "approved_at": approved_at})
            atomic_write_text(path, record.model_dump_json())
            budget = DeveloperTaskBudget(path=self.budget_path(preview_id), bundle=bundle)
            record = record.model_copy(update={"ledger_state": "ready", "deadline": json.loads(budget.path.read_text())["deadline"]})
            atomic_write_text(path, record.model_dump_json())


class PlanRevision(DefinitionModel):
    schema_version: Literal[1] = 1
    pins: dict[str, JsonValue]
    proposal: PlanProposal
    inspections: dict[str, JsonValue]
    issues: tuple[dict[str, Any], ...]

    @property
    def digest(self) -> str:
        return sha256(canonical(self.model_dump(mode="json")).encode()).hexdigest()


class FilePlanStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.absolute()
        if self.directory.resolve() != self.directory:
            raise ValueError("Plan store cannot follow symlinks")
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, identity: str) -> Path:
        path = self.directory / (sha256(identity.encode()).hexdigest() + ".json")
        if path.resolve() != path:
            raise ValueError("Plan storage cannot follow symlinks")
        return path

    def save(self, assignment_id: str, plan: PlanRevision) -> str:
        plan = PlanRevision.model_validate(plan.model_dump())
        path = self.path(assignment_id)
        with FileLock(str(path) + ".lock"):
            if path.exists() and PlanRevision.model_validate_json(path.read_text()) != plan:
                raise PermissionError("Plan revisions are immutable; use a freshly approved planning task")
            atomic_write_text(path, plan.model_dump_json())
        return plan.digest

    def get(self, assignment_id: str) -> PlanRevision:
        return PlanRevision.model_validate_json(self.path(assignment_id).read_text())


class _IssueEffect(DefinitionModel):
    key: Identifier
    content: dict[str, Any]
    state: Literal["creating", "created", "linking", "linked"]
    receipt: dict[str, Any] | None = None


class _PlanPublication(DefinitionModel):
    schema_version: Literal[1] = 1
    assignment_id: StrictStr
    plan_digest: StrictStr
    nonce: StrictStr
    operator_id: StrictStr
    state: Literal["pending", "publishing", "awaiting_links", "published"] = "pending"
    approved_at: StrictStr | None = None
    link_approval: dict[str, Any] | None = None
    link_approved_at: StrictStr | None = None
    effects: tuple[_IssueEffect, ...] = ()
    developer_previews: tuple[StrictStr, ...] = ()


class ManagedPlanning:
    def __init__(self, *, admission: PlanningAdmission, github: GitHubAdapter,
                 state_dir: Path, lookup_max_pages: int = 10) -> None:
        if type(lookup_max_pages) is not int or lookup_max_pages <= 0:
            raise ValueError("Planning publication requires a positive lookup page bound")
        self.admission, self.github = admission, github
        self.plans = FilePlanStore(state_dir / "plans")
        self.publications = FilePlanStore(state_dir / "publications")
        self.lookup_max_pages = lookup_max_pages

    def request(self, context: ManagedTaskContext) -> _PlanningRequest:
        assignment = context.revalidate()
        record = self.admission.request_for(assignment.preview_id)
        if (record is None or record.route.organization_id != assignment.organization_id or
                record.route.revision != assignment.revision or record.route.event != assignment.event or
                record.bundle_content != assignment.bundle_content or record.operator_id != self.admission.operator_id or
                assignment.budget_path != str(self.admission.budget_path(assignment.preview_id))):
            raise PermissionError("Planning task differs from its original route, operator, scope or budget")
        self.admission.prepare(assignment.preview_id)
        return record

    def inspect(self, context: ManagedTaskContext) -> dict[str, Any]:
        record = self.request(context)
        evidence = self.github.inspect_planning_target(repository=record.scope.repository,
                                                      repository_id=record.scope.repository_id,
                                                      base_revision=record.scope.base_revision)
        context.revalidate()
        return {"request": json.loads(record.bundle_content), "target": evidence,
                "limits": record.scope.limits.model_dump(mode="json")}

    def propose(self, context: ManagedTaskContext, proposal: PlanProposal, inspections: dict[str, Any]) -> dict[str, Any]:
        record = self.request(context)
        proposal.check_limits(record.scope.limits)
        if inspections != self.inspect(context):
            raise PermissionError("Plan requires exact saved request and repository/base inspection")
        identity = sha256((context.assignment.assignment_id + proposal.model_dump_json()).encode()).hexdigest()
        issues: list[dict[str, Any]] = []
        for issue in proposal.issues:
            marker = "<!-- aitobuild-plan:" + identity + ":" + issue.key + " -->"
            body = self._render_body(issue, marker)
            issues.append({"key": issue.key, "title": issue.title, "body": body, "labels": list(issue.labels), "marker": marker})
        plan = PlanRevision(pins={
            "assignment_id": context.assignment.assignment_id, "scope_digest": context.assignment.scope_digest,
            "revision": context.assignment.revision, "binding_revision": context.run.binding_revision,
            "preview_id": context.assignment.preview_id, "operator_id": record.operator_id,
            "approved_at": context.assignment.approved_at.isoformat(), "budget_path": context.assignment.budget_path,
            "deadline": record.deadline, "scope": record.scope.model_dump(mode="json"), "developer_policy": record.developer_policy,
        }, proposal=proposal, inspections=inspections, issues=tuple(issues))
        digest = self.plans.save(context.assignment.assignment_id, plan)
        publication = _PlanPublication(assignment_id=context.assignment.assignment_id, plan_digest=digest,
                                       nonce=uuid4().hex, operator_id=record.operator_id)
        path = self.publications.path(context.assignment.assignment_id)
        with FileLock(str(path) + ".lock"):
            if path.exists():
                publication = self._load_publication(context.assignment.assignment_id, plan)
            else:
                atomic_write_text(path, publication.model_dump_json())
        if publication.state != "pending":
            raise PermissionError("Consumed publication cannot request another approval")
        return self.approval_data(plan, publication)

    @staticmethod
    def approval_data(plan: PlanRevision, publication: _PlanPublication) -> dict[str, Any]:
        return {"stage": "create", "plan_digest": plan.digest, "nonce": publication.nonce, "operator_id": publication.operator_id,
                "pins": plan.pins, "issues": list(plan.issues), "dependencies": {
                    issue.key: list(issue.dependencies) for issue in plan.proposal.issues},
                "dependency_publication": "Resolved dependency links require a separate exact operator approval",
                "implementation": "Stage unapproved previews only; no assignment or execution"}

    def _load_publication(self, assignment_id: str, plan: PlanRevision) -> _PlanPublication:
        saved = _PlanPublication.model_validate_json(self.publications.path(assignment_id).read_text())
        scope = PlanningScope.model_validate(plan.pins["scope"])
        if (saved.assignment_id != assignment_id or saved.plan_digest != plan.digest or
                saved.operator_id != plan.pins["operator_id"] or saved.operator_id != self.admission.operator_id or
                (saved.state == "pending") != (saved.approved_at is None) or
                len({effect.key for effect in saved.effects}) != len(saved.effects) or
                any(effect.key not in {issue.key for issue in plan.proposal.issues} for effect in saved.effects)):
            raise PermissionError("Publication pins or consumed approval differ from the immutable plan")
        for effect in saved.effects:
            draft = next(issue for issue in plan.issues if issue["key"] == effect.key)
            expected = self._linked_content(plan, draft, saved) if effect.state in {"linking", "linked"} else draft
            if effect.content != expected or (effect.state != "creating" and effect.receipt is None):
                raise PermissionError("Saved publication effect differs from exact approved issue content")
            if effect.receipt is not None:
                receipt = effect.receipt
                body = expected["body"] if effect.state != "linking" else draft["body"]
                if (receipt.get("title") != expected["title"] or receipt.get("body") != body or
                        receipt.get("labels") != expected["labels"] or receipt.get("repository") != scope.repository):
                    raise PermissionError("Saved issue receipt differs from its approved content")
        if saved.link_approval is not None and saved.link_approval != self._link_data(plan, saved, saved.link_approval.get("nonce")):
            raise PermissionError("Dependency approval differs from the exact resolved issue IDs and content")
        if (saved.state == "awaiting_links" and (saved.link_approval is None or saved.link_approved_at is not None) or
                saved.link_approved_at is not None and saved.link_approval is None or
                any(effect.state == "linking" for effect in saved.effects) and saved.link_approved_at is None):
            raise PermissionError("Dependency publication requires its separate consumed exact approval")
        return saved

    @staticmethod
    def _render_body(issue: PlannedIssue, marker: str, numbers: dict[str, Any] | None = None) -> str:
        body = issue.objective + "\n\n## Acceptance Criteria\n" + "\n".join("- " + item for item in issue.acceptance_criteria)
        if issue.dependencies:
            body += "\n\n## Dependencies\n" + "\n".join(
                "- Planned issue: " + key if numbers is None else "- Depends on #" + str(numbers[key]) for key in issue.dependencies)
        return body + "\n\n" + marker + "\n"

    @staticmethod
    def _linked_content(plan: PlanRevision, draft: dict[str, Any], publication: _PlanPublication) -> dict[str, Any]:
        proposal = next(issue for issue in plan.proposal.issues if issue.key == draft["key"])
        numbers = {effect.key: effect.receipt["number"] for effect in publication.effects if effect.receipt is not None}
        for dependency in proposal.dependencies:
            if dependency not in numbers:
                raise PermissionError("Dependency issue has no saved publication receipt")
        body = ManagedPlanning._render_body(proposal, draft["marker"], numbers)
        return dict(draft) | {"body": body}

    def _save_publication(self, publication: _PlanPublication) -> None:
        atomic_write_text(self.publications.path(publication.assignment_id), publication.model_dump_json())

    def _link_data(self, plan: PlanRevision, saved: _PlanPublication, nonce: Any) -> dict[str, Any]:
        if not isinstance(nonce, str) or not nonce:
            raise PermissionError("Dependency approval requires a stable nonce")
        effects = {effect.key: effect for effect in saved.effects}
        links = []
        for draft in plan.issues:
            content = self._linked_content(plan, draft, saved)
            if content != draft:
                effect = effects[draft["key"]]
                if effect.receipt is None:
                    raise PermissionError("Dependency approval requires exact created issue identities")
                links.append({"issue_number": effect.receipt["number"], "issue_id": effect.receipt["issue_id"],
                              "before_body": draft["body"], "content": content})
        return {"stage": "link", "plan_digest": plan.digest, "nonce": nonce, "pins": plan.pins, "links": links,
                "implementation": "Stage unapproved previews only; no assignment or execution"}

    @staticmethod
    def _receipt(issue: GitHubIssue, content: dict[str, Any], scope: PlanningScope) -> dict[str, Any]:
        if (issue.repository != scope.repository or issue.title != content["title"] or issue.body != content["body"] or
                set(issue.labels) != set(content["labels"]) or len(issue.labels) != len(content["labels"]) or
                issue.state != "open" or type(issue.number) is not int or issue.number <= 0 or
                type(issue.issue_id) is not int or issue.issue_id <= 0):
            raise PermissionError("Remote issue differs from exact approved content or identity")
        return issue.to_dict() | {"issue_id": issue.issue_id, "labels": list(content["labels"])}

    def _target(self, plan: PlanRevision) -> PlanningScope:
        scope = PlanningScope.model_validate(plan.pins["scope"])
        self.github.inspect_planning_target(repository=scope.repository, repository_id=scope.repository_id,
                                            base_revision=scope.base_revision)
        return scope

    def publish(self, context: ManagedTaskContext, original: Any, approved: Any) -> dict[str, Any]:
        if type(approved) is not bool or not approved:
            raise PermissionError("Exact issue publication requires affirmative operator approval")
        record = self.request(context)
        assignment_id = context.assignment.assignment_id
        plan = self.plans.get(assignment_id)

        def before_write() -> None:
            self._target(plan)
            self.request(context)
            context.revalidate()

        path = self.publications.path(assignment_id)
        with FileLock(str(path) + ".lock", timeout=0):
            saved = self._load_publication(assignment_id, plan)
            if (plan.pins["scope"] != record.scope.model_dump(mode="json") or
                    plan.pins["scope_digest"] != context.assignment.scope_digest or
                    plan.pins["binding_revision"] != context.run.binding_revision):
                raise PermissionError("Publication must consume the exact saved approval once")
            creating = saved.state == "pending"
            if creating:
                if original != self.approval_data(plan, saved):
                    raise PermissionError("Issue creation approval differs from exact saved content")
            elif (saved.state != "awaiting_links" or saved.link_approved_at is not None or original != saved.link_approval):
                raise PermissionError("Dependency publication must consume its exact saved approval once")
            self._target(plan)
            context.revalidate()
            saved = saved.model_copy(update={"state": "publishing",
                                            "approved_at" if creating else "link_approved_at": datetime.now(UTC).isoformat()})
            self._save_publication(saved)
            for content in plan.issues if creating else ():
                self._target(plan)
                matches = self.github.find_issue_publication(repository=record.scope.repository, marker=str(content["marker"]),
                                                            max_pages=self.lookup_max_pages)
                if matches:
                    raise PermissionError("New publication marker already exists; operator inspection required")
                effect = _IssueEffect(key=content["key"], content=content, state="creating")
                saved = saved.model_copy(update={"effects": (*saved.effects, effect)})
                self._save_publication(saved)
                issue = self.github.create_issue(role=AgentRole.PM, repository=record.scope.repository,
                                                 title=str(content["title"]), body=str(content["body"]), labels=tuple(content["labels"]),
                                                 approved=True, require_human_approval_for_repo_writes=True,
                                                 before_write=before_write)
                receipt = self._receipt(issue, content, record.scope)
                saved = saved.model_copy(update={"effects": (*saved.effects[:-1], effect.model_copy(update={"state": "created", "receipt": receipt}))})
                self._save_publication(saved)
            if creating and any(issue.dependencies for issue in plan.proposal.issues):
                data = self._link_data(plan, saved, uuid4().hex)
                saved = saved.model_copy(update={"state": "awaiting_links", "link_approval": data})
                self._save_publication(saved)
                return {"state": "awaiting_links", "approval": data}
            for index, draft in enumerate(plan.issues):
                content = self._linked_content(plan, draft, saved)
                effect = saved.effects[index]
                if effect.receipt is None:
                    raise PermissionError("Dependency publication requires the created issue receipt")
                receipt = effect.receipt
                if content != draft:
                    self._target(plan)
                    current = self.github.get_issue(repository=record.scope.repository, issue_number=effect.receipt["number"])
                    self._receipt(current, draft, record.scope)
                    effect = effect.model_copy(update={"state": "linking", "content": content})
                    saved = saved.model_copy(update={"effects": (*saved.effects[:index], effect, *saved.effects[index + 1:])})
                    self._save_publication(saved)

                    def before_link() -> None:
                        before_write()
                        live = self.github.get_issue(repository=record.scope.repository, issue_number=current.number)
                        if self._receipt(live, draft, record.scope) != receipt:
                            raise PermissionError("Issue content changed immediately before dependency publication")
                        context.revalidate()

                    issue = self.github.update_issue(role=AgentRole.PM, repository=record.scope.repository,
                                                     issue_number=current.number, body=str(content["body"]),
                                                     approved=True, require_human_approval_for_repo_writes=True,
                                                     before_write=before_link)
                    receipt = self._receipt(issue, content, record.scope)
                effect = effect.model_copy(update={"state": "linked", "receipt": receipt})
                saved = saved.model_copy(update={"effects": (*saved.effects[:index], effect, *saved.effects[index + 1:])})
                self._save_publication(saved)
            context.revalidate()
            return self._stage(plan, saved)

    def reconcile(self, assignment_id: str) -> dict[str, Any]:
        plan = self.plans.get(assignment_id)
        path = self.publications.path(assignment_id)
        with FileLock(str(path) + ".lock", timeout=0):
            saved = self._load_publication(assignment_id, plan)
            if saved.state == "pending":
                raise PermissionError("Unapproved plans cannot reconcile publication")
            if saved.state == "awaiting_links":
                raise PermissionError("Dependency links still require the separate exact saved approval")
            scope = self._target(plan)
            for index, effect in enumerate(saved.effects):
                matches = self.github.find_issue_publication(repository=scope.repository, marker=str(effect.content["marker"]),
                                                            max_pages=self.lookup_max_pages)
                if len(matches) != 1:
                    raise PermissionError("Uncertain or ambiguous issue creation cannot repeat")
                receipt = self._receipt(matches[0], effect.content, scope)
                if effect.receipt is not None and (receipt["number"], receipt["issue_id"]) != (
                        effect.receipt["number"], effect.receipt["issue_id"]):
                    raise PermissionError("Published issue identity changed")
                final = self._linked_content(plan, next(item for item in plan.issues if item["key"] == effect.key), saved)
                effect = effect.model_copy(update={"receipt": receipt, "state": "linked" if final == effect.content else "created"})
                saved = saved.model_copy(update={"effects": (*saved.effects[:index], effect, *saved.effects[index + 1:])})
                self._save_publication(saved)
            if (len(saved.effects) != len(plan.issues) or any(effect.state != "linked" for effect in saved.effects) or
                    any(issue.dependencies for issue in plan.proposal.issues) and saved.link_approved_at is None):
                raise PermissionError("Incomplete approved publication requires operator inspection; no writes replayed")
            return self._stage(plan, saved)

    def _stage(self, plan: PlanRevision, saved: _PlanPublication) -> dict[str, Any]:
        scope = self._target(plan)
        if len(saved.effects) != len(plan.issues) or any(effect.state != "linked" for effect in saved.effects):
            raise PermissionError("Developer previews require every exact issue and dependency receipt")
        for effect in saved.effects:
            if effect.receipt is None:
                raise PermissionError("Issue has no publication receipt")
            current = self.github.get_issue(repository=scope.repository, issue_number=effect.receipt["number"])
            if self._receipt(current, effect.content, scope) != effect.receipt:
                raise PermissionError("Published issue drift prevents Developer handoff")
        previews: list[str] = []
        for effect in saved.effects:
            if effect.receipt is None:
                raise PermissionError("Issue has no exact publication receipt")
            receipt = effect.receipt
            draft = next(issue for issue in plan.proposal.issues if issue.key == effect.key)
            task_id = "planned-issue-" + sha256((saved.assignment_id + ":" + effect.key).encode()).hexdigest()
            payload = build_developer_task_bundle(task_id=task_id, objective=draft.objective,
                                                  acceptance_criteria=list(draft.acceptance_criteria), constraints=[], context_files=[],
                                                  issue_context=DeveloperIssueContext(
                                                      repository=scope.repository, repository_id=scope.repository_id,
                                                      issue_number=receipt["number"], issue_id=receipt["issue_id"],
                                                      title=receipt["title"], body=receipt["body"],
                                                      base_branch=scope.base_branch, base_revision=scope.base_revision,
                                                  )).to_payload()
            payload["policy"] = plan.pins["developer_policy"]
            developer_task_bundle_from_payload(payload)
            preview = self.admission.previews.create_or_get(dedupe_key=task_id, task_key=task_id, bundle_payload=payload,
                                                           source_payload={"plan_digest": plan.digest, "planning_assignment": saved.assignment_id})
            previews.append(preview.preview_id)
        if saved.developer_previews and tuple(previews) != saved.developer_previews:
            raise PermissionError("Published Developer preview identities changed")
        saved = saved.model_copy(update={"state": "published", "developer_previews": tuple(previews)})
        self._save_publication(saved)
        return {"plan_digest": plan.digest, "state": saved.state, "issues": [effect.receipt for effect in saved.effects],
                "developer_previews": previews, "implementation_approved": False}