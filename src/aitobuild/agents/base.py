"""Agent role definitions and instruction templates."""

from __future__ import annotations

from dataclasses import dataclass

from aitobuild.policy import AgentRole


@dataclass(slots=True, frozen=True)
class AgentSpec:
    role: AgentRole
    name: str
    instructions: str


def default_agent_specs() -> tuple[AgentSpec, ...]:
    return (
        AgentSpec(
            role=AgentRole.PM,
            name="PM Agent",
            instructions=(
                "You are the PM agent. Convert epics into scoped issues with acceptance criteria. "
                "Do not write implementation code."
            ),
        ),
        AgentSpec(
            role=AgentRole.ARCHITECT,
            name="Architect Agent",
            instructions=(
                "You are the Architect agent. Review architecture and code quality and suggest "
                "improvements. Do not author implementation code."
            ),
        ),
        AgentSpec(
            role=AgentRole.DEVELOPER,
            name="Developer Agent",
            instructions=(
                "You are the Developer agent. Implement tasks only from the provided isolation task "
                "bundle. Respect allowed tools, allowed paths, command constraints, and acceptance "
                "criteria. Treat tool descriptions as authoritative contracts for required argument "
                "format and semantics. Prefer context-matched patch edits via developer_apply_patch for "
                "targeted edits, and if patch apply fails, read the file again and retry with exact "
                "context before falling back to developer_write_file."
            ),
        ),
    )
