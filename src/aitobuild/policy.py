"""Role and approval policy constraints for agents and tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AgentRole(StrEnum):
    PM = "pm"
    ARCHITECT = "architect"
    DEVELOPER = "developer"


class ActionClass(StrEnum):
    READ_ONLY = "read_only"
    REPO_WRITE = "repo_write"


@dataclass(slots=True, frozen=True)
class RolePolicy:
    role: AgentRole
    allowed_actions: tuple[ActionClass, ...]


ROLE_POLICY_MATRIX: dict[AgentRole, RolePolicy] = {
    AgentRole.PM: RolePolicy(
        role=AgentRole.PM,
        allowed_actions=(ActionClass.READ_ONLY, ActionClass.REPO_WRITE),
    ),
    AgentRole.ARCHITECT: RolePolicy(
        role=AgentRole.ARCHITECT,
        allowed_actions=(ActionClass.READ_ONLY,),
    ),
    AgentRole.DEVELOPER: RolePolicy(
        role=AgentRole.DEVELOPER,
        allowed_actions=(ActionClass.READ_ONLY, ActionClass.REPO_WRITE),
    ),
}


def assert_role_action_allowed(role: AgentRole, action_class: ActionClass) -> None:
    policy = ROLE_POLICY_MATRIX[role]
    if action_class not in policy.allowed_actions:
        raise PermissionError(f"Role {role} is not allowed to perform action class {action_class}")


def assert_repo_write_approval(
    *,
    require_human_approval_for_repo_writes: bool,
    approved: bool,
) -> None:
    if require_human_approval_for_repo_writes and not approved:
        raise PermissionError("Repository-writing action requires human approval")
