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


class WorkflowDefinition(DefinitionModel):
    id: Identifier
    document: dict[StrictStr, JsonValue]

    @model_validator(mode="after")
    def validate_document(self) -> Self:
        actions = self.document.get("actions")
        if not isinstance(actions, list) or not actions:
            raise ValueError("Native workflow documents require nonempty actions")
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError("Native workflow actions must be objects")
            kind = action.get("kind")
            if not isinstance(kind, str) or not kind.strip():
                raise ValueError("Native workflow actions require a kind")
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