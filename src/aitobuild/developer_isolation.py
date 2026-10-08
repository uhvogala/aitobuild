"""Developer agent isolation contracts and task bundle helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import re
import shlex
import json
from pathlib import Path
from time import time
from math import isfinite
from typing import Any

from filelock import FileLock


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
class DeveloperIssueContext:
    repository: str
    repository_id: int
    issue_number: int
    issue_id: int
    title: str
    body: str
    base_branch: str
    base_revision: str | None = None


@dataclass(slots=True, frozen=True)
class DeveloperTaskBundle:
    task_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    context_files: tuple[str, ...]
    policy: DeveloperIsolationPolicy
    issue_context: DeveloperIssueContext | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["policy"]["allowed_tools"] = [tool.value for tool in self.policy.allowed_tools]
        if self.issue_context is None:
            payload.pop("issue_context")
        return payload


class DeveloperTaskBudget:
    def __init__(self, *, path: Path, bundle: DeveloperTaskBundle, create: bool = True,
                 allow_aborted: bool = False) -> None:
        self.path = path
        self.bundle = bundle
        self._snapshot = json.loads(json.dumps(bundle.to_payload()))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(path) + ".lock", timeout=10)
        if bundle.policy.max_file_changes < 0 or bundle.policy.max_runtime_minutes <= 0:
            raise ValueError("Task budgets require nonnegative file count and positive runtime")
        with self._lock:
            if not path.exists():
                if not create:
                    raise ValueError("Persisted task budget is missing; a new approved task is required")
                self._save({"task_id": bundle.task_id, "bundle": bundle.to_payload(), "deadline": time() + bundle.policy.max_runtime_minutes * 60,
                            "reserved_paths": []})
            self._load(allow_aborted=allow_aborted)

    def _load(self, *, allow_aborted: bool = False) -> dict[str, Any]:
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("task_id") != self.bundle.task_id:
            raise ValueError("Task budget identity does not match the approved task")
        if state.get("bundle") != self._snapshot:
            raise ValueError("Task budget does not match the approved task snapshot")
        if not allow_aborted and state.get("aborted", False) is not False:
            raise TimeoutError("Approved task was aborted; a new approved task is required")
        deadline = state.get("deadline")
        paths = state.get("reserved_paths")
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not isfinite(deadline):
            raise ValueError("Task budget deadline is invalid")
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise ValueError("Task budget file reservations are invalid")
        if len(set(paths)) > self.bundle.policy.max_file_changes:
            raise ValueError("Persisted file reservations exceed the approved budget")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(self.path)

    def remaining_seconds(self) -> float:
        with self._lock:
            remaining = float(self._load()["deadline"]) - time()
        if remaining <= 0:
            raise TimeoutError("Approved task runtime budget has expired; a new approved task is required")
        return remaining

    def abort(self) -> None:
        with self._lock:
            state = self._load(allow_aborted=True)
            state["aborted"] = True
            self._save(state)

    def reserve_paths(self, paths: tuple[str, ...]) -> None:
        with self._lock:
            state = self._load()
            if float(state["deadline"]) <= time():
                raise TimeoutError("Approved task runtime budget has expired; no files were written")
            reserved = sorted(set(state["reserved_paths"]) | set(paths))
            if len(reserved) > self.bundle.policy.max_file_changes:
                raise PermissionError(f"Task file budget exceeds max_file_changes={self.bundle.policy.max_file_changes}")
            state["reserved_paths"] = reserved
            self._save(state)


def default_developer_isolation_policy() -> DeveloperIsolationPolicy:
    return DeveloperIsolationPolicy(
        allowed_tools=(IsolationTool.GITHUB, IsolationTool.FILESYSTEM, IsolationTool.BASH),
        allowed_paths=("src/", "tests/", "README.md", "pyproject.toml"),
        blocked_paths=(".git/", ".venv/", "secrets/"),
        allowed_command_prefixes=(
            "uv", "python", "python3", "pytest", "ruff", "mypy", "coverage",
            "node", "npm", "npx", "pnpm", "yarn", "corepack", "bun", "deno",
            "tsc", "eslint", "prettier",
            "git", "make", "cmake", "ninja", "gcc", "g++", "cc", "c++", "clang", "clang++",
            "go", "cargo", "rustc", "rustfmt", "dotnet", "java", "javac",
            "mvn", "gradle", "./mvnw", "./gradlew", "ruby", "bundle", "rake", "php", "composer",
            "bash", "sh", "env", "timeout", "set", "export", "source", ".", "test", "[", "true", "false",
            "pwd", "ls", "tree", "find", "rg", "grep", "cat", "head", "tail", "wc",
            "sort", "uniq", "cut", "tr", "sed", "awk", "xargs", "diff", "file", "stat", "du",
            "which", "printf", "echo", "mkdir", "cp", "mv", "touch", "tee", "ln",
            "curl", "wget", "jq", "tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "xz",
        ),
        max_file_changes=12,
        max_runtime_minutes=30,
    )



def default_architect_isolation_policy() -> DeveloperIsolationPolicy:
    """Stricter read-only analysis policy for Architect workspace tools."""
    return DeveloperIsolationPolicy(
        allowed_tools=(IsolationTool.GITHUB, IsolationTool.FILESYSTEM, IsolationTool.BASH),
        allowed_paths=("src/", "tests/", "README.md", "pyproject.toml", "PLAN.md", "MILESTONES.md"),
        blocked_paths=(".git/", ".venv/", "secrets/", ".aitobuild/"),
        allowed_command_prefixes=(
            "uv", "python", "python3", "pytest", "ruff", "mypy", "coverage",
            "npm", "npx", "pnpm", "yarn", "eslint", "prettier", "tsc",
            "git", "rg", "grep", "find", "ls", "tree", "cat", "head", "tail", "wc",
            "sort", "uniq", "cut", "tr", "sed", "awk", "diff", "file", "stat", "du",
            "which", "printf", "echo", "pwd", "true", "false", "test", "[", "timeout", "env",
        ),
        max_file_changes=0,
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
    issue_context: DeveloperIssueContext | None = None,
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
        issue_context=issue_context,
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

    issue_context = None
    context_raw = payload.get("issue_context")
    if context_raw is not None:
        if not isinstance(context_raw, dict):
            raise ValueError("issue_context must be an object")
        for key in ("repository", "title", "body", "base_branch"):
            if not isinstance(context_raw.get(key), str):
                raise ValueError(f"issue_context.{key} must be a string")
        for key in ("repository_id", "issue_number", "issue_id"):
            value = context_raw.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"issue_context.{key} must be a positive integer")
        revision = context_raw.get("base_revision")
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)):
            raise ValueError("issue_context.base_revision must be a resolved lowercase commit SHA")
        issue_context = DeveloperIssueContext(
            repository=context_raw["repository"], repository_id=context_raw["repository_id"],
            issue_number=context_raw["issue_number"], issue_id=context_raw["issue_id"],
            title=context_raw["title"], body=context_raw["body"], base_branch=context_raw["base_branch"],
            base_revision=revision,
        )

    return build_developer_task_bundle(
        task_id=task_id_raw,
        objective=objective_raw,
        acceptance_criteria=tuple(acceptance_criteria_raw),
        constraints=tuple(constraints_raw),
        context_files=tuple(context_files_raw),
        policy=policy,
        issue_context=issue_context,
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
    prefixes: list[list[str]] = []
    for prefix in policy.allowed_command_prefixes:
        try:
            prefix_parts = shlex.split(prefix)
        except ValueError:
            continue
        if prefix_parts:
            prefixes.append(prefix_parts)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    command_parts: list[str] = []
    try:
        for token in lexer:
            if not command_parts and "=" in token:
                name = token.partition("=")[0]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None:
                    continue
            command_parts.append(token)
            if command_parts in prefixes:
                return True
            if not any(parts[:len(command_parts)] == command_parts for parts in prefixes):
                return False
    except ValueError:
        return False
    return False


def _normalize_context_file(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("context file entries must be non-empty")
    if normalized.startswith("/"):
        raise ValueError("context file paths must be workspace-relative")
    if ".." in normalized.split("/"):
        raise ValueError("context file paths must not traverse parent directories")
    return normalized
