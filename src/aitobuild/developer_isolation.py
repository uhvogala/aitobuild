"""Developer agent isolation contracts and task bundle helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class IsolationTool(StrEnum):
    GITHUB = "github"
    FILESYSTEM = "filesystem"
    BASH = "bash"


@dataclass(slots=True, frozen=True)
class DeveloperIsolationPolicy:
    allowed_tools: tuple[IsolationTool, ...]
    allowed_paths: tuple[str, ...]
    blocked_paths: tuple[str, ...]
    allowed_command_prefixes: tuple[str, ...]
    max_file_changes: int
    max_runtime_minutes: int


@dataclass(slots=True, frozen=True)
class DeveloperTaskBundle:
    task_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    context_files: tuple[str, ...]
    policy: DeveloperIsolationPolicy

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["policy"]["allowed_tools"] = [tool.value for tool in self.policy.allowed_tools]
        return payload


def default_developer_isolation_policy() -> DeveloperIsolationPolicy:
    return DeveloperIsolationPolicy(
        allowed_tools=(IsolationTool.GITHUB, IsolationTool.FILESYSTEM, IsolationTool.BASH),
        allowed_paths=("src/", "tests/", "README.md", "pyproject.toml"),
        blocked_paths=(".git/", ".venv/", "secrets/"),
        allowed_command_prefixes=(
            "uv run",
            "pytest",
            "ruff",
            "mypy",
            "python -m pytest",
            "python -m pip",
        ),
        max_file_changes=12,
        max_runtime_minutes=30,
    )


def build_developer_task_bundle(
    *,
    task_id: str,
    objective: str,
    acceptance_criteria: tuple[str, ...] | list[str],
    constraints: tuple[str, ...] | list[str],
    context_files: tuple[str, ...] | list[str],
    policy: DeveloperIsolationPolicy | None = None,
) -> DeveloperTaskBundle:
    if not task_id.strip():
        raise ValueError("task_id must be non-empty")
    if not objective.strip():
        raise ValueError("objective must be non-empty")

    criteria = tuple(item.strip() for item in acceptance_criteria if item and item.strip())
    if not criteria:
        raise ValueError("acceptance_criteria must contain at least one non-empty item")

    normalized_constraints = tuple(item.strip() for item in constraints if item and item.strip())
    normalized_context = tuple(_normalize_context_file(path) for path in context_files)

    bundle_policy = policy or default_developer_isolation_policy()

    for context_file in normalized_context:
        if not is_path_allowed(context_file, policy=bundle_policy):
            raise ValueError(f"context file '{context_file}' is outside allowed policy paths")

    return DeveloperTaskBundle(
        task_id=task_id.strip(),
        objective=objective.strip(),
        acceptance_criteria=criteria,
        constraints=normalized_constraints,
        context_files=normalized_context,
        policy=bundle_policy,
    )


def developer_task_bundle_from_payload(payload: dict[str, Any]) -> DeveloperTaskBundle:
    task_id_raw = payload.get("task_id")
    objective_raw = payload.get("objective")
    acceptance_criteria_raw = payload.get("acceptance_criteria")
    constraints_raw = payload.get("constraints")
    context_files_raw = payload.get("context_files")
    policy_raw = payload.get("policy")

    if not isinstance(task_id_raw, str):
        raise ValueError("task_id must be a string")
    if not isinstance(objective_raw, str):
        raise ValueError("objective must be a string")
    if not isinstance(acceptance_criteria_raw, (list, tuple)) or not all(
        isinstance(item, str) for item in acceptance_criteria_raw
    ):
        raise ValueError("acceptance_criteria must be a sequence of strings")
    if not isinstance(constraints_raw, (list, tuple)) or not all(
        isinstance(item, str) for item in constraints_raw
    ):
        raise ValueError("constraints must be a sequence of strings")
    if not isinstance(context_files_raw, (list, tuple)) or not all(
        isinstance(item, str) for item in context_files_raw
    ):
        raise ValueError("context_files must be a sequence of strings")
    if not isinstance(policy_raw, dict):
        raise ValueError("policy must be an object")

    allowed_tools_raw = policy_raw.get("allowed_tools")
    allowed_paths_raw = policy_raw.get("allowed_paths")
    blocked_paths_raw = policy_raw.get("blocked_paths")
    allowed_command_prefixes_raw = policy_raw.get("allowed_command_prefixes")
    max_file_changes_raw = policy_raw.get("max_file_changes")
    max_runtime_minutes_raw = policy_raw.get("max_runtime_minutes")

    if not isinstance(allowed_tools_raw, (list, tuple)) or not all(
        isinstance(item, str) for item in allowed_tools_raw
    ):
        raise ValueError("policy.allowed_tools must be a sequence of strings")
    if not isinstance(allowed_paths_raw, (list, tuple)) or not all(
        isinstance(item, str) for item in allowed_paths_raw
    ):
        raise ValueError("policy.allowed_paths must be a sequence of strings")
    if not isinstance(blocked_paths_raw, (list, tuple)) or not all(
        isinstance(item, str) for item in blocked_paths_raw
    ):
        raise ValueError("policy.blocked_paths must be a sequence of strings")
    if not isinstance(allowed_command_prefixes_raw, (list, tuple)) or not all(
        isinstance(item, str) for item in allowed_command_prefixes_raw
    ):
        raise ValueError("policy.allowed_command_prefixes must be a sequence of strings")
    if not isinstance(max_file_changes_raw, int):
        raise ValueError("policy.max_file_changes must be an integer")
    if not isinstance(max_runtime_minutes_raw, int):
        raise ValueError("policy.max_runtime_minutes must be an integer")

    try:
        allowed_tools = tuple(IsolationTool(item) for item in allowed_tools_raw)
    except ValueError as exc:
        raise ValueError("policy.allowed_tools contains unknown tool") from exc

    policy = DeveloperIsolationPolicy(
        allowed_tools=allowed_tools,
        allowed_paths=tuple(allowed_paths_raw),
        blocked_paths=tuple(blocked_paths_raw),
        allowed_command_prefixes=tuple(allowed_command_prefixes_raw),
        max_file_changes=max_file_changes_raw,
        max_runtime_minutes=max_runtime_minutes_raw,
    )

    return build_developer_task_bundle(
        task_id=task_id_raw,
        objective=objective_raw,
        acceptance_criteria=tuple(acceptance_criteria_raw),
        constraints=tuple(constraints_raw),
        context_files=tuple(context_files_raw),
        policy=policy,
    )


def is_path_allowed(path: str, *, policy: DeveloperIsolationPolicy) -> bool:
    normalized = path.strip()
    if not normalized:
        return False

    for blocked in policy.blocked_paths:
        if normalized == blocked or normalized.startswith(blocked):
            return False

    return any(normalized == allowed or normalized.startswith(allowed) for allowed in policy.allowed_paths)


def is_command_allowed(command: str, *, policy: DeveloperIsolationPolicy) -> bool:
    normalized = command.strip()
    if not normalized:
        return False
    return any(normalized.startswith(prefix) for prefix in policy.allowed_command_prefixes)


def _normalize_context_file(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("context file entries must be non-empty")
    if normalized.startswith("/"):
        raise ValueError("context file paths must be workspace-relative")
    if ".." in normalized.split("/"):
        raise ValueError("context file paths must not traverse parent directories")
    return normalized
