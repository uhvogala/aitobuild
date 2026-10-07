"""Versioned organization definitions, separate from runtime execution state."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Never, Protocol, Self

from filelock import FileLock
from pydantic import (
    BaseModel, ConfigDict, Field, JsonValue, StrictInt, StrictStr, TypeAdapter, model_validator,
)

from aitobuild.policy import AgentRole


Identifier = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=128)]
EventName = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=128)]


class DefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentDefinition(DefinitionModel):
    id: Identifier
    role: AgentRole
    instructions: Annotated[StrictStr, Field(min_length=1, max_length=32000)]
    model_profile: Identifier | None = None
    tool_profile: Identifier | None = None
    max_concurrent_runs: Annotated[StrictInt, Field(ge=1, le=64)] = 1

    @model_validator(mode="after")
    def validate_instructions(self) -> Self:
        if not self.instructions.strip():
            raise ValueError("Agent instructions must not be blank")
        return self


class TeamDefinition(DefinitionModel):
    id: Identifier
    members: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    coordinator: Identifier | None = None


class WorkflowNodeDefinition(DefinitionModel):
    id: Identifier
    kind: Literal["agent", "operation"]
    agent: Identifier | None = None
    operation: Identifier | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.kind == "agent" and (self.agent is None or self.operation is not None):
            raise ValueError("Agent nodes require only an agent reference")
        if self.kind == "operation" and (self.operation is None or self.agent is not None):
            raise ValueError("Operation nodes require only an operation reference")
        return self


class WorkflowCaseDefinition(DefinitionModel):
    condition: Identifier
    target: Identifier


class WorkflowEdgeDefinition(DefinitionModel):
    kind: Literal["edge", "fan_out", "fan_in", "switch"] = "edge"
    source: Identifier | None = None
    target: Identifier | None = None
    sources: tuple[Identifier, ...] = ()
    targets: tuple[Identifier, ...] = ()
    condition: Identifier | None = None
    cases: tuple[WorkflowCaseDefinition, ...] = ()
    default: Identifier | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.kind == "edge":
            valid = self.source is not None and self.target is not None
            valid = valid and not (self.sources or self.targets or self.cases or self.default)
        elif self.kind == "fan_out":
            valid = self.source is not None and len(self.targets) >= 2
            valid = valid and not (self.target or self.sources or self.condition or self.cases or self.default)
        elif self.kind == "fan_in":
            valid = self.target is not None and len(self.sources) >= 2
            valid = valid and not (self.source or self.targets or self.condition or self.cases or self.default)
        else:
            valid = self.source is not None and bool(self.cases)
            valid = valid and not (self.target or self.sources or self.targets or self.condition)
        if not valid:
            raise ValueError("Invalid fields for the selected native edge kind")
        for references in (self.sources, self.targets, tuple(case.target for case in self.cases)):
            if len(references) != len(set(references)):
                raise ValueError("Native edge targets/sources must be unique")
        conditions = [case.condition for case in self.cases]
        if len(conditions) != len(set(conditions)):
            raise ValueError("Switch conditions must be unique")
        return self

    def connections(self) -> tuple[tuple[str, str], ...]:
        if self.kind == "fan_out":
            return tuple((str(self.source), target) for target in self.targets)
        if self.kind == "fan_in":
            return tuple((source, str(self.target)) for source in self.sources)
        if self.kind == "switch":
            targets = [case.target for case in self.cases]
            if self.default is not None:
                targets.append(self.default)
            return tuple((str(self.source), target) for target in targets)
        return ((str(self.source), str(self.target)),)


class WorkflowGraphDefinition(DefinitionModel):
    format: Literal["python_graph"]
    start: Identifier
    nodes: Annotated[tuple[WorkflowNodeDefinition, ...], Field(min_length=1)]
    edges: tuple[WorkflowEdgeDefinition, ...] = ()
    outputs: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    max_iterations: Annotated[StrictInt, Field(ge=1)] = 100
    description: StrictStr | None = None

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        identifiers = {node.id for node in self.nodes}
        if len(identifiers) != len(self.nodes):
            raise ValueError("Workflow node IDs must be unique")
        if self.start not in identifiers or not set(self.outputs) <= identifiers:
            raise ValueError("Workflow start/outputs must reference configured nodes")
        if len(self.outputs) != len(set(self.outputs)):
            raise ValueError("Workflow output nodes must be unique")
        connections = [connection for edge in self.edges for connection in edge.connections()]
        if any(source not in identifiers or target not in identifiers for source, target in connections):
            raise ValueError("Workflow edges must reference configured nodes")
        if len(connections) != len(set(connections)):
            raise ValueError("Workflow connections must be unique")
        reachable = {self.start}
        while True:
            expanded = reachable | {target for source, target in connections if source in reachable}
            if expanded == reachable:
                break
            reachable = expanded
        if reachable != identifiers:
            raise ValueError("All workflow nodes must be reachable from the start")
        return self


class WorkflowDefinition(DefinitionModel):
    id: Identifier
    document: dict[StrictStr, JsonValue]

    @model_validator(mode="after")
    def validate_document(self) -> Self:
        WorkflowGraphDefinition.model_validate(self.document)
        return self


class DelegationDefinition(DefinitionModel):
    strategy: Literal["coordinator", "rules", "human"]
    eligible_agents: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    target_agent: Identifier | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if len(set(self.eligible_agents)) != len(self.eligible_agents):
            raise ValueError("Delegation eligibility must not contain duplicates")
        if self.strategy == "rules":
            if self.target_agent not in self.eligible_agents:
                raise ValueError("Rule-based delegation requires an eligible target agent")
        elif self.target_agent is not None:
            raise ValueError("Only rule-based delegation may pin a target agent")
        return self


class RouteDefinition(DefinitionModel):
    id: Identifier
    events: Annotated[tuple[EventName, ...], Field(min_length=1)]
    team: Identifier
    workflow: Identifier
    delegation: DelegationDefinition


class OrganizationDefinition(DefinitionModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    id: Identifier
    agents: Annotated[tuple[AgentDefinition, ...], Field(min_length=1)]
    teams: Annotated[tuple[TeamDefinition, ...], Field(min_length=1)]
    workflows: Annotated[tuple[WorkflowDefinition, ...], Field(min_length=1)]
    routes: Annotated[tuple[RouteDefinition, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        for definitions in (self.agents, self.teams, self.workflows, self.routes):
            identifiers = [definition.id for definition in definitions]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("Definition identifiers must be unique within each collection")
        agents = {agent.id for agent in self.agents}
        teams = {team.id: team for team in self.teams}
        workflows = {workflow.id for workflow in self.workflows}
        for team in self.teams:
            if len(set(team.members)) != len(team.members) or not set(team.members) <= agents:
                raise ValueError("Team members must be unique configured agents")
            if team.coordinator is not None and team.coordinator not in team.members:
                raise ValueError("Team coordinator must be a member")
        events: set[str] = set()
        for route in self.routes:
            if route.team not in teams or route.workflow not in workflows:
                raise ValueError("Routes must reference configured teams and workflows")
            team = teams[route.team]
            if not set(route.delegation.eligible_agents) <= set(team.members):
                raise ValueError("Delegation targets must belong to the configured team")
            if route.delegation.strategy == "coordinator" and team.coordinator is None:
                raise ValueError("Coordinator delegation requires a team coordinator")
            if len(set(route.events)) != len(route.events) or events.intersection(route.events):
                raise ValueError("Events must select one unambiguous configured route")
            events.update(route.events)
        return self


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON definition key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Never:
    raise ValueError(f"Non-finite JSON definition value: {value}")


def parse_organization_definition(content: str) -> OrganizationDefinition:
    document = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return OrganizationDefinition.model_validate(document)


def load_organization_definition(path: Path) -> OrganizationDefinition:
    return parse_organization_definition(path.read_text(encoding="utf-8"))


def _canonical_content(definition: OrganizationDefinition) -> str:
    return json.dumps(
        definition.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class DefinitionSnapshot:
    content: str
    organization_id: str = field(init=False)
    revision: str = field(init=False)

    def __post_init__(self) -> None:
        definition = parse_organization_definition(self.content)
        if self.content != _canonical_content(definition):
            raise ValueError("Stored definition snapshots must use canonical JSON")
        object.__setattr__(self, "organization_id", definition.id)
        object.__setattr__(self, "revision", hashlib.sha256(self.content.encode("utf-8")).hexdigest())

    @property
    def definition(self) -> OrganizationDefinition:
        return parse_organization_definition(self.content)


class DefinitionStore(Protocol):
    def save(self, definition: OrganizationDefinition) -> DefinitionSnapshot: ...

    def get(self, organization_id: str, revision: str) -> DefinitionSnapshot | None: ...


class FileDefinitionStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, organization_id: str, revision: str) -> Path:
        identifier = TypeAdapter(Identifier).validate_python(organization_id)
        if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{64}", revision) is None:
            raise ValueError("Definition revision must be a SHA-256 digest")
        return self._root / identifier / f"{revision}.json"

    def get(self, organization_id: str, revision: str) -> DefinitionSnapshot | None:
        path = self._path(organization_id, revision)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        snapshot = DefinitionSnapshot(content)
        if snapshot.organization_id != organization_id or snapshot.revision != revision:
            raise ValueError("Stored definition identity or revision does not match its address")
        return snapshot

    def save(self, definition: OrganizationDefinition) -> DefinitionSnapshot:
        snapshot = DefinitionSnapshot(_canonical_content(definition))
        path = self._path(snapshot.organization_id, snapshot.revision)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path.parent / ".lock"), timeout=10):
            if self.get(snapshot.organization_id, snapshot.revision) is not None:
                return snapshot
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(snapshot.content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            for directory in (path.parent, self._root):
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        return snapshot