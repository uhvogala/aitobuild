"""Durable assignment ownership, separate from organization definitions and execution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import uuid4

from filelock import FileLock
from pydantic import AwareDatetime, Field, StrictInt, StrictStr, model_validator

from aitobuild.developer_isolation import (
    DeveloperTaskBudget, DeveloperTaskBundle, developer_task_bundle_from_payload,
)
from aitobuild.developer_preview import DeveloperPreviewRegistry
from aitobuild.organization import DefinitionModel, DefinitionStore, EventName, Identifier


Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
DecisionText = Annotated[StrictStr, Field(min_length=1, max_length=32000)]
TerminalState = Literal["completed", "failed", "cancelled"]


class AssignmentProposal(DefinitionModel):
    agent_id: Identifier
    rationale: DecisionText

    @model_validator(mode="after")
    def validate_rationale(self) -> Self:
        if not self.rationale.strip():
            raise ValueError("Assignment rationale must not be blank")
        return self


class TaskAssignment(DefinitionModel):
    assignment_id: Identifier
    organization_id: Identifier
    revision: Digest
    route_id: Identifier
    event: EventName
    team_id: Identifier
    workflow_id: Identifier
    strategy: Literal["coordinator", "rules", "human"]
    eligible_agents: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    agent_id: Identifier
    agent_capacity: Annotated[StrictInt, Field(ge=1, le=64)]
    actor_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    rationale: DecisionText
    preview_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    task_id: StrictStr
    bundle_content: StrictStr
    scope_digest: Digest
    budget_path: StrictStr
    approved_at: AwareDatetime
    created_at: AwareDatetime
    state: Literal["claimed", "completed", "failed", "cancelled"] = "claimed"
    closed_at: AwareDatetime | None = None
    outcome: DecisionText | None = None

    @property
    def bundle(self) -> DeveloperTaskBundle:
        return developer_task_bundle_from_payload(json.loads(self.bundle_content))

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if len(self.eligible_agents) != len(set(self.eligible_agents)) or self.agent_id not in self.eligible_agents:
            raise ValueError("Assignment must select a unique eligible agent")
        if not self.rationale.strip() or not self.actor_id.strip():
            raise ValueError("Assignment requires actor identity and rationale")
        if sha256(self.bundle_content.encode("utf-8")).hexdigest() != self.scope_digest:
            raise ValueError("Assignment scope digest does not match its frozen content")
        payload = json.loads(self.bundle_content)
        if json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) != self.bundle_content:
            raise ValueError("Assignment scope must use canonical JSON")
        if self.bundle.task_id != self.task_id:
            raise ValueError("Assignment task identity differs from its approved scope")
        if not Path(self.budget_path).is_absolute():
            raise ValueError("Assignment budget path must be operator-owned and absolute")
        if self.created_at < self.approved_at:
            raise ValueError("Assignment cannot precede human approval")
        if self.state == "claimed":
            if self.closed_at is not None or self.outcome is not None:
                raise ValueError("Active assignments cannot have terminal outcomes")
        elif (self.closed_at is None or self.closed_at < self.created_at
              or self.outcome is None or not self.outcome.strip()):
            raise ValueError("Terminal assignments require timestamp and outcome")
        return self


class AssignmentJournal(DefinitionModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    assignments: tuple[TaskAssignment, ...]

    @model_validator(mode="after")
    def validate_ownership(self) -> Self:
        for field in ("assignment_id", "task_id", "preview_id"):
            values = [getattr(record, field) for record in self.assignments]
            if len(values) != len(set(values)):
                raise ValueError("Assignment journal contains duplicate ownership identities")
        for record in self.assignments:
            if record.state == "claimed":
                active = [other for other in self.assignments if other.state == "claimed"
                          and (other.organization_id, other.agent_id) == (record.organization_id, record.agent_id)]
                if len(active) > min(other.agent_capacity for other in active):
                    raise ValueError("Persisted assignment capacity is exceeded")
        return self


class AssignmentStore(Protocol):
    def get(self, assignment_id: str) -> TaskAssignment | None: ...

    def for_task(self, task_id: str) -> TaskAssignment | None: ...

    def claim(self, assignment: TaskAssignment) -> TaskAssignment: ...

    def finish(
        self, assignment_id: str, *, state: TerminalState, outcome: str,
        before_finish: Callable[[TaskAssignment], None] | None = None,
    ) -> TaskAssignment: ...


class FileAssignmentStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = str(path) + ".lock"

    def _load(self) -> AssignmentJournal:
        try:
            content = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return AssignmentJournal(schema_version=1, assignments=())
        return AssignmentJournal.model_validate_json(content)

    def _save(self, journal: AssignmentJournal) -> None:
        temporary = self._path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(journal.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self._path)
        directory_fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def get(self, assignment_id: str) -> TaskAssignment | None:
        with FileLock(self._lock_path, timeout=10):
            return next((record for record in self._load().assignments if record.assignment_id == assignment_id), None)

    def for_task(self, task_id: str) -> TaskAssignment | None:
        with FileLock(self._lock_path, timeout=10):
            return next((record for record in self._load().assignments if record.task_id == task_id), None)

    def claim(self, assignment: TaskAssignment) -> TaskAssignment:
        candidate = TaskAssignment.model_validate(assignment.model_dump())
        if candidate.state != "claimed":
            raise ValueError("New assignments must claim ownership")
        with FileLock(self._lock_path, timeout=10):
            journal = self._load()
            existing = next((record for record in journal.assignments
                             if record.task_id == candidate.task_id or record.preview_id == candidate.preview_id), None)
            if existing is not None:
                ignored = {"assignment_id", "created_at", "state", "closed_at", "outcome"}
                if existing.model_dump(exclude=ignored) != candidate.model_dump(exclude=ignored):
                    raise ValueError("Task already has an immutable assignment owner")
                return existing
            updated = AssignmentJournal(schema_version=1, assignments=(*journal.assignments, candidate))
            self._save(updated)
            return candidate

    def finish(
        self, assignment_id: str, *, state: TerminalState, outcome: str,
        before_finish: Callable[[TaskAssignment], None] | None = None,
    ) -> TaskAssignment:
        if state not in {"completed", "failed", "cancelled"}:
            raise ValueError("Assignment completion requires a terminal state")
        with FileLock(self._lock_path, timeout=10):
            journal = self._load()
            original = next((record for record in journal.assignments if record.assignment_id == assignment_id), None)
            if original is None:
                raise ValueError("Assignment not found")
            if original.state != "claimed":
                if original.state != state or original.outcome != outcome:
                    raise ValueError("Terminal assignments cannot be changed or reclaimed")
                return original
            updated = TaskAssignment.model_validate(original.model_dump() | {
                "state": state, "outcome": outcome, "closed_at": datetime.now(tz=UTC),
            })
            if before_finish is not None:
                before_finish(original)
            self._save(AssignmentJournal(schema_version=1, assignments=tuple(
                updated if record.assignment_id == assignment_id else record for record in journal.assignments
            )))
            return updated


class AssignmentService:
    def __init__(
        self, *, definitions: DefinitionStore, previews: DeveloperPreviewRegistry,
        assignments: AssignmentStore, budget_path_for: Callable[[str], Path],
    ) -> None:
        self._definitions = definitions
        self._previews = previews
        self._assignments = assignments
        self._budget_path_for = budget_path_for

    def assign(
        self, *, organization_id: str, revision: str, event: str, preview_id: str,
        proposal: AssignmentProposal | None = None,
        coordinator_id: str | None = None, human_id: str | None = None,
    ) -> TaskAssignment:
        snapshot = self._definitions.get(organization_id, revision)
        if snapshot is None:
            raise ValueError("Organization revision not found")
        definition = snapshot.definition
        route = next((route for route in definition.routes if event in route.events), None)
        if route is None:
            raise ValueError("No configured route for the assignment event")
        team = next(team for team in definition.teams if team.id == route.team)
        delegation = route.delegation
        if delegation.strategy == "rules":
            if proposal is not None or coordinator_id is not None or human_id is not None:
                raise PermissionError("Rule assignments must use the configured target without overrides")
            proposal = AssignmentProposal(
                agent_id=str(delegation.target_agent), rationale=f"Configured rule selects {delegation.target_agent}",
            )
            actor_id = route.id
        elif delegation.strategy == "coordinator":
            if proposal is None or coordinator_id != team.coordinator or human_id is not None:
                raise PermissionError("Assignment requires a proposal from the configured coordinator")
            actor_id = str(coordinator_id)
        else:
            if proposal is None or human_id is None or coordinator_id is not None:
                raise PermissionError("Human delegation requires an operator identity and decision")
            actor_id = human_id
        proposal = AssignmentProposal.model_validate(proposal.model_dump())
        if proposal.agent_id not in delegation.eligible_agents:
            raise PermissionError("Assignment target is not eligible for the routed team")
        agent = next(agent for agent in definition.agents if agent.id == proposal.agent_id)
        preview = self._previews.get(preview_id)
        if preview is None or not preview.approved or preview.approved_at is None:
            raise PermissionError("Assignment requires an existing human-approved task")
        bundle = developer_task_bundle_from_payload(preview.bundle_payload)
        budget_path = self._budget_path_for(preview_id).resolve()
        budget = DeveloperTaskBudget(path=budget_path, bundle=bundle, create=False)
        budget.remaining_seconds()
        content = json.dumps(preview.bundle_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        candidate = TaskAssignment(
            assignment_id="assignment-" + uuid4().hex, organization_id=definition.id,
            revision=snapshot.revision, route_id=route.id, event=event, team_id=team.id,
            workflow_id=route.workflow, strategy=delegation.strategy,
            eligible_agents=delegation.eligible_agents, agent_id=agent.id,
            agent_capacity=agent.max_concurrent_runs, actor_id=actor_id, rationale=proposal.rationale,
            preview_id=preview_id, task_id=bundle.task_id, bundle_content=content,
            scope_digest=sha256(content.encode("utf-8")).hexdigest(), budget_path=str(budget_path),
            approved_at=preview.approved_at, created_at=datetime.now(tz=UTC),
        )
        return self._assignments.claim(candidate)

    def revalidate(self, assignment_id: str) -> TaskAssignment:
        record = self._assignments.get(assignment_id)
        if record is None or record.state != "claimed":
            raise PermissionError("Managed execution requires a claimed assignment")
        if self._assignments.for_task(record.task_id) != record:
            raise PermissionError("Assignment no longer owns its task")
        snapshot = self._definitions.get(record.organization_id, record.revision)
        if snapshot is None:
            raise ValueError("Pinned organization revision not found")
        definition = snapshot.definition
        route = next((route for route in definition.routes if route.id == record.route_id), None)
        team = next((team for team in definition.teams if team.id == record.team_id), None)
        agent = next((agent for agent in definition.agents if agent.id == record.agent_id), None)
        if (route is None or team is None or agent is None or record.event not in route.events
                or route.team != record.team_id or route.workflow != record.workflow_id
                or route.delegation.strategy != record.strategy
                or route.delegation.eligible_agents != record.eligible_agents
                or agent.max_concurrent_runs != record.agent_capacity
                or (record.strategy == "coordinator" and record.actor_id != team.coordinator)
                or (record.strategy == "rules" and (record.agent_id != route.delegation.target_agent
                                                    or record.actor_id != route.id))):
            raise PermissionError("Assignment differs from its pinned definition")
        preview = self._previews.get(record.preview_id)
        if (preview is None or not preview.approved or preview.approved_at != record.approved_at
                or json.dumps(preview.bundle_payload, sort_keys=True, separators=(",", ":"),
                              allow_nan=False) != record.bundle_content):
            raise PermissionError("Assignment approved scope has changed")
        path = self._budget_path_for(record.preview_id).resolve()
        if str(path) != record.budget_path:
            raise ValueError("Assignment must use its original budget ledger")
        DeveloperTaskBudget(path=path, bundle=record.bundle, create=False).remaining_seconds()
        return record

    def finish(self, assignment_id: str, *, state: TerminalState, outcome: str) -> TaskAssignment:
        def check_budget(record: TaskAssignment) -> None:
            path = self._budget_path_for(record.preview_id).resolve()
            if str(path) != record.budget_path:
                raise ValueError("Assignment must use its original budget ledger")
            try:
                budget = DeveloperTaskBudget(path=path, bundle=record.bundle, create=False)
            except TimeoutError:
                if state == "completed":
                    raise
            else:
                if state == "completed":
                    budget.remaining_seconds()
                else:
                    budget.abort()

        return self._assignments.finish(assignment_id, state=state, outcome=outcome, before_finish=check_budget)