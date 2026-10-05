"""Architect role tools: read-only workspace analysis, PR review, and memory."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Callable, Literal
from uuid import uuid4

from agent_framework import tool
from pydantic import Field

from aitobuild.developer_isolation import (
    DeveloperIsolationPolicy,
    default_architect_isolation_policy,
    is_command_allowed,
    is_path_allowed,
)
from aitobuild.meetings import MeetingRegistry
from aitobuild.policy import ActionClass, AgentRole, assert_role_action_allowed
from aitobuild.shared_tools import build_request_meeting_tool, build_web_search_tool
from aitobuild.tools.architect_memory import ArchitectMemoryStore
from aitobuild.tools.bash import BashAdapter, ContainerSessionBashAdapter, prepare_developer_workspace
from aitobuild.tools.filesystem import FilesystemAdapter
from aitobuild.tools.github import GitHubAdapter, MockGitHubAdapter
from aitobuild.tools.mcp_adapters import MCPDeveloperToolAdapter
from aitobuild.tools.search import search_workspace
from aitobuild.tools.web_search import WebSearchAdapter
from aitobuild.developer_delivery import DeveloperDeliveryWorker


ToolFunc = Callable[..., Any]

ARCHITECT_SESSION_PREFIX = "architect-"
_ARCHITECT_SHELL_META_CHARS = set(";|&`$<>\n\r")
_ARCHITECT_BANNED_TOKEN_FRAGMENTS = (
    " rm ",
    " mv ",
    " cp ",
    " tee ",
    " npm install",
    " pip install",
    " uv add",
    " uv pip",
    "&&rm ",
    ";rm ",
    " chmod ",
    " chown ",
    " curl ",
    " wget ",
    " dd ",
    " mkfs",
    " shutdown",
    " reboot",
)


def _assert_architect_session_id(session_id: str) -> str:
    cleaned = session_id.strip()
    if not cleaned:
        raise ValueError("session_id must be non-empty")
    if not cleaned.startswith(ARCHITECT_SESSION_PREFIX):
        raise PermissionError(
            "Architect may only manage sessions prefixed with "
            f"'{ARCHITECT_SESSION_PREFIX}' (cannot attach/stop Developer sessions)"
        )
    return cleaned


def _harden_architect_command(command: str) -> None:
    if any(ch in command for ch in _ARCHITECT_SHELL_META_CHARS):
        raise PermissionError(
            "Architect run_command rejects shell metacharacters (;|&`$<> and newlines)"
        )
    if "$(" in command or "${" in command:
        raise PermissionError("Architect run_command rejects shell expansions")
    lowered = f" {command.lower()} "
    if any(token in lowered for token in _ARCHITECT_BANNED_TOKEN_FRAGMENTS):
        raise PermissionError("Architect run_command rejects mutating or dangerous operations")


def build_architect_tools(

    *,
    bash_adapter: BashAdapter,
    filesystem_adapter: FilesystemAdapter,
    workspace_root: Path,
    github_adapter: GitHubAdapter | None = None,
    meeting_registry: MeetingRegistry | None = None,
    web_search_adapter: WebSearchAdapter | None = None,
    memory_store: ArchitectMemoryStore | None = None,
    container_session_adapter: ContainerSessionBashAdapter | None = None,
    mcp_tool_adapter: MCPDeveloperToolAdapter | None = None,
    isolation_policy: DeveloperIsolationPolicy | None = None,
    bound_session_id: str | None = None,
    prepared_workspace: Path | None = None,
    default_repository: str | None = None,
    allow_pr_approve: bool = False,
    delivery_worker: DeveloperDeliveryWorker | None = None,
) -> tuple[ToolFunc, ...]:
    policy = isolation_policy or default_architect_isolation_policy()
    github = github_adapter or MockGitHubAdapter()
    memory = memory_store
    active_session_id: str | None = None
    role = AgentRole.ARCHITECT
    if bound_session_id is not None:
        bound_session_id = _assert_architect_session_id(bound_session_id)

    def _resolve_session_id(session_id: str | None) -> str | None:
        nonlocal active_session_id
        if bound_session_id is not None:
            if session_id is not None and _assert_architect_session_id(session_id) != bound_session_id:
                raise ValueError("Tool session must match this Architect's bound session")
            active_session_id = bound_session_id
            if container_session_adapter is not None:
                container_session_adapter.create_session(session_id=active_session_id)
            return active_session_id
        if session_id is not None and session_id.strip():
            active_session_id = _assert_architect_session_id(session_id)
            return active_session_id
        if container_session_adapter is None:
            return None
        if active_session_id is not None:
            return active_session_id
        list_session_ids = getattr(container_session_adapter, "list_session_ids", None)
        if callable(list_session_ids):
            try:
                existing = [
                    str(item)
                    for item in list_session_ids()
                    if str(item).startswith(ARCHITECT_SESSION_PREFIX)
                ]
            except Exception:
                existing = []
            if len(existing) == 1:
                active_session_id = existing[0]
                return active_session_id
        created_session_id, _ = container_session_adapter.create_session(
            session_id=f"{ARCHITECT_SESSION_PREFIX}{uuid4().hex[:10]}"
        )
        active_session_id = created_session_id
        return active_session_id

    def _workspace_root_for_session(session_id: str | None) -> Path:
        if prepared_workspace is not None:
            return prepared_workspace
        if container_session_adapter is not None and session_id:
            return container_session_adapter.get_workspace_root(session_id)
        if bound_session_id is not None and session_id:
            return prepare_developer_workspace(workspace_root, session_id)
        return workspace_root

    def _workspace_path(path: str, session_id: str | None) -> Path:
        root = _workspace_root_for_session(session_id).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError("File path resolves outside this Architect's workspace")
        if not is_path_allowed(target.relative_to(root).as_posix(), policy=policy):
            raise ValueError("File path resolves outside allowed task policy paths")
        return target

    def _normalize_path(path: str) -> str:
        normalized = path.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("path must be non-empty")
        if normalized.startswith("/"):
            raise ValueError("path must be workspace-relative")
        if ".." in normalized.split("/"):
            raise ValueError("path must not traverse parent directories")
        return normalized

    def _repo(repository: str | None) -> str:
        resolved = (repository or default_repository or "").strip()
        if not resolved:
            raise ValueError("repository is required (owner/name)")
        return resolved

    @tool(
        name="architect_run_command",
        approval_mode="always_require",
        description=(
            "Run a short read-only analysis command (lint/test/static analysis). "
            "Architect commands are stricter than Developer: no writes or package installs. "
            "Check exit_code; 0 means success."
        ),
    )
    def architect_run_command(
        command: Annotated[str, Field(description="Allowed analysis command.")],
        session_id: Annotated[str | None, Field(description="Optional container session id.")] = None,
        max_output_chars: Annotated[int, Field(ge=256, le=24000)] = 6000,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        if not is_command_allowed(command, policy=policy):
            raise PermissionError("Command is outside allowed Architect analysis prefixes")
        _harden_architect_command(command)
        resolved_session_id = _resolve_session_id(session_id)
        if mcp_tool_adapter is not None:
            if resolved_session_id is None:
                raise ValueError("session_id is required when MCP adapters are enabled")
            result = mcp_tool_adapter.run_command(session_id=resolved_session_id, command=command)
        else:
            result_obj = bash_adapter.run(role=role, command=command, session_id=resolved_session_id)
            result = {
                "command": result_obj.command,
                "exit_code": result_obj.exit_code,
                "stdout": result_obj.stdout,
                "stderr": result_obj.stderr,
            }
        return {
            "command": str(result.get("command", command)),
            "exit_code": int(result.get("exit_code", 0)),
            "stdout": str(result.get("stdout", ""))[-max_output_chars:],
            "stderr": str(result.get("stderr", ""))[-max_output_chars:],
            "output_truncated": any(
                len(str(result.get(stream, ""))) > max_output_chars for stream in ("stdout", "stderr")
            ),
        }

    @tool(
        name="architect_read_file",
        approval_mode="always_require",
        description="Read a workspace-relative file allowed by Architect policy. Read-only.",
    )
    def architect_read_file(
        path: Annotated[str, Field(description="Workspace-relative file path.")],
        session_id: Annotated[str | None, Field(description="Optional container session id.")] = None,
    ) -> str:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        normalized_path = _normalize_path(path)
        if not is_path_allowed(normalized_path, policy=policy):
            raise ValueError(f"path is outside allowed policy paths: {normalized_path}")
        resolved_session_id = _resolve_session_id(session_id)
        if mcp_tool_adapter is not None:
            if resolved_session_id is None:
                raise ValueError("session_id is required when MCP adapters are enabled")
            return mcp_tool_adapter.read_file(session_id=resolved_session_id, path=normalized_path)
        target = _workspace_path(normalized_path, resolved_session_id)
        return filesystem_adapter.read_text(role=role, path=target, session_id=resolved_session_id)

    def _search(
        *,
        kind: Literal["files", "content"],
        pattern: str,
        path: str,
        globs: list[str] | None,
        include_hidden: bool,
        include_ignored: bool,
        offset: int,
        max_results: int,
        session_id: str | None,
        is_regex: bool = False,
        case_sensitive: bool = True,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        resolved = _resolve_session_id(session_id)
        prefix: tuple[str, ...] = ()
        if isinstance(container_session_adapter, ContainerSessionBashAdapter):
            if resolved is None:
                raise ValueError("A container session is required for Architect search")
            container = container_session_adapter.get_container_name(session_id=resolved)
            if container is None:
                raise ValueError("Architect container session is not running")
            prefix = ("docker", "exec", container, "timeout", "20s")
        return search_workspace(
            workspace=_workspace_root_for_session(resolved),
            policy=policy,
            kind=kind,
            pattern=pattern,
            path=path,
            globs=globs,
            include_hidden=include_hidden,
            include_ignored=include_ignored,
            offset=offset,
            max_results=max_results,
            command_prefix=prefix,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
        )

    @tool(
        name="architect_find_files",
        approval_mode="always_require",
        description="Find file paths with ripgrep discovery in Architect-allowed workspace paths.",
    )
    def architect_find_files(
        glob: Annotated[str, Field(description="File glob, e.g. **/*.py.")] = "**/*",
        path: Annotated[str, Field(description="Workspace-relative search root.")] = ".",
        globs: Annotated[list[str] | None, Field(description="Extra include/exclude globs.")] = None,
        include_hidden: bool = False,
        include_ignored: bool = False,
        offset: Annotated[int, Field(ge=0, le=10000)] = 0,
        max_results: Annotated[int, Field(ge=1, le=100)] = 20,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return _search(
            kind="files",
            pattern=glob,
            path=path,
            globs=globs,
            include_hidden=include_hidden,
            include_ignored=include_ignored,
            offset=offset,
            max_results=max_results,
            session_id=session_id,
        )

    @tool(
        name="architect_search_files",
        approval_mode="always_require",
        description="Search file contents with ripgrep under Architect-allowed paths.",
    )
    def architect_search_files(
        pattern: Annotated[str, Field(description="Literal text or Rust regex when is_regex=true.")],
        path: str = ".",
        globs: list[str] | None = None,
        is_regex: bool = False,
        case_sensitive: bool = True,
        include_hidden: bool = False,
        include_ignored: bool = False,
        offset: Annotated[int, Field(ge=0, le=10000)] = 0,
        max_results: Annotated[int, Field(ge=1, le=100)] = 20,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return _search(
            kind="content",
            pattern=pattern,
            path=path,
            globs=globs,
            is_regex=is_regex,
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            include_ignored=include_ignored,
            offset=offset,
            max_results=max_results,
            session_id=session_id,
        )

    @tool(
        name="architect_start_session",
        approval_mode="always_require",
        description="Start or reuse a persistent read-only container session for Architect analysis.",
    )
    def architect_start_session(
        session_id: Annotated[str | None, Field(description="Optional desired Architect session id.")] = None,
    ) -> dict[str, str]:
        nonlocal active_session_id
        if container_session_adapter is None:
            raise ValueError("container_session mode is not enabled")
        if bound_session_id is not None:
            resolved = _resolve_session_id(session_id)
            created_session_id, container_name = container_session_adapter.create_session(
                session_id=resolved
            )
        else:
            desired = (
                _assert_architect_session_id(session_id)
                if session_id and session_id.strip()
                else f"{ARCHITECT_SESSION_PREFIX}{uuid4().hex[:10]}"
            )
            created_session_id, container_name = container_session_adapter.create_session(
                session_id=desired
            )
        active_session_id = created_session_id
        return {"session_id": created_session_id, "container_name": container_name}

    @tool(
        name="architect_stop_session",
        approval_mode="always_require",
        description="Stop and clean up a persistent Architect container session.",
    )
    def architect_stop_session(
        session_id: Annotated[str, Field(description="Architect session id to stop.")],
    ) -> dict[str, Any]:
        nonlocal active_session_id
        if container_session_adapter is None:
            raise ValueError("container_session mode is not enabled")
        session_id = _assert_architect_session_id(session_id)
        if bound_session_id is not None and session_id != bound_session_id:
            raise ValueError("Cannot stop another Architect session")
        closed = container_session_adapter.close_session(session_id=session_id)
        if closed and active_session_id == session_id:
            active_session_id = None
        return {"session_id": session_id, "closed": closed}

    @tool(
        name="architect_get_pr",
        approval_mode="always_require",
        description="Fetch a pull request summary including changed files for review.",
    )
    def architect_get_pr(
        pull_number: Annotated[int, Field(ge=1, description="Pull request number.")],
        repository: Annotated[
            str | None,
            Field(description="owner/name repository; defaults to configured repository."),
        ] = None,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        pr = github.get_pull_request(repository=_repo(repository), pull_number=pull_number)
        return pr.to_dict()

    @tool(
        name="architect_submit_pr_review",
        approval_mode="always_require",
        description=(
            "Submit a GitHub PR review (APPROVE, REQUEST_CHANGES, or COMMENT). "
            "Architect may review but must not author implementation files."
        ),
    )
    def architect_submit_pr_review(
        pull_number: Annotated[int, Field(ge=1)],
        event: Annotated[
            Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
            Field(description="GitHub review event."),
        ],
        body: Annotated[str, Field(description="Review commentary for the Developer/human.")],
        repository: Annotated[str | None, Field(description="owner/name repository.")] = None,
    ) -> dict[str, Any]:
        if event == "APPROVE" and not allow_pr_approve:
            raise PermissionError(
                "Architect APPROVE reviews are disabled; set AITOBUILD_ARCHITECT_ALLOW_PR_APPROVE=true "
                "to enable, or use COMMENT / REQUEST_CHANGES"
            )
        review = github.submit_pr_review(
            role=role,
            repository=_repo(repository),
            pull_number=pull_number,
            event=event,
            body=body,
        )
        return review.to_dict()

    @tool(
        name="architect_get_published_pr",
        approval_mode="always_require",
        description=(
            "Fetch the draft pull request for a published delivery. "
            "Resolves repository/PR/head only from delivery publication (preview_id); "
            "no repository or pull_number override."
        ),
    )
    def architect_get_published_pr(
        preview_id: Annotated[str, Field(description="Published delivery preview_id.")],
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        if delivery_worker is None:
            raise ValueError("Delivery worker is not configured for Architect review")
        cleaned = preview_id.strip()
        if not cleaned:
            raise ValueError("preview_id must be non-empty")
        return delivery_worker.get_published_pull_request(cleaned, github=github)

    @tool(
        name="architect_submit_published_pr_review",
        approval_mode="always_require",
        description=(
            "Submit COMMENT or REQUEST_CHANGES on a published delivery's draft PR. "
            "PR identity comes only from publication; APPROVE and merge are unavailable. "
            "Review body is length-capped and framed."
        ),
    )
    def architect_submit_published_pr_review(
        preview_id: Annotated[str, Field(description="Published delivery preview_id.")],
        event: Annotated[
            Literal["COMMENT", "REQUEST_CHANGES"],
            Field(description="GitHub review event (APPROVE unavailable on this path)."),
        ],
        body: Annotated[str, Field(description="Review commentary (length-capped).")],
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.PR_REVIEW)
        if delivery_worker is None:
            raise ValueError("Delivery worker is not configured for Architect review")
        cleaned = preview_id.strip()
        if not cleaned:
            raise ValueError("preview_id must be non-empty")
        record = delivery_worker.submit_architect_review(
            cleaned, github=github, event=event, body=body,
        )
        return {
            "preview_id": record.preview_id,
            "state": record.state,
            "architect_review": dict(record.architect_review or {}),
        }

    @tool(
        name="architect_memory_query",

        approval_mode="always_require",
        description="Query durable Architect memory for prior architectural decisions and conventions.",
    )
    def architect_memory_query(
        query: Annotated[str, Field(description="Keyword query over stored architectural notes.")],
        limit: Annotated[int, Field(ge=1, le=50)] = 5,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        if memory is None:
            raise ValueError("Architect memory store is not configured")
        matches = memory.query(query=query, limit=limit)
        return {
            "query": query.strip(),
            "matches": [item.to_dict() for item in matches],
            "match_count": len(matches),
        }

    @tool(
        name="architect_memory_record",
        approval_mode="always_require",
        description=(
            "Record a verified architectural decision or convention for later reuse. "
            "Never store credentials or secrets."
        ),
    )
    def architect_memory_record(
        topic: Annotated[str, Field(description="Short topic label.")],
        content: Annotated[str, Field(description="Decision/content to remember.")],
        tags: Annotated[list[str] | None, Field(description="Optional tags.")] = None,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        if memory is None:
            raise ValueError("Architect memory store is not configured")
        item = memory.record(topic=topic, content=content, tags=tuple(tags or ()))
        return item.to_dict()

    tools: list[ToolFunc] = [
        architect_run_command,
        architect_read_file,
        architect_find_files,
        architect_search_files,
        architect_start_session,
        architect_stop_session,
        architect_get_pr,
        architect_submit_pr_review,
        architect_get_published_pr,
        architect_submit_published_pr_review,
        architect_memory_query,
        architect_memory_record,
        build_web_search_tool(role=role, adapter=web_search_adapter),
        build_request_meeting_tool(role=role, meeting_registry=meeting_registry),
    ]
    for tool_func in tools:
        name = getattr(tool_func, "name", None)
        description = getattr(tool_func, "description", "")
        if name == "architect_run_command":
            description += "\nArchitect command prefixes: " + ", ".join(policy.allowed_command_prefixes)
        if name in {"architect_read_file", "architect_find_files", "architect_search_files"}:
            description += "\nArchitect allowed paths: " + ", ".join(policy.allowed_paths)
            description += "\nArchitect blocked paths: " + ", ".join(policy.blocked_paths)
        setattr(tool_func, "description", description)
    return tuple(tools)
