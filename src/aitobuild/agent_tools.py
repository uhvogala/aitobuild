"""Agent tool wiring for role-specific runtime tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from agent_framework import tool
from pydantic import Field

from aitobuild.developer_isolation import (
    DeveloperIsolationPolicy, DeveloperTaskBudget, IsolationTool, default_developer_isolation_policy, is_command_allowed, is_path_allowed,
)
from aitobuild.patching import apply_update_hunks, parse_patch_document
from aitobuild.policy import (
    ActionClass,
    AgentRole,
    assert_repo_write_approval,
    assert_role_action_allowed,
)
from aitobuild.tools import (
    ContainerSessionBashAdapter,
    FilesystemAdapter,
    MCPDeveloperToolAdapter,
)
from aitobuild.tools.bash import BashAdapter, prepare_developer_workspace
from aitobuild.tools.shell import build_shell_tools, shell_request
from aitobuild.tools.search import search_workspace
from aitobuild.tool_outputs import build_output_reader


ToolFunc = Callable[..., Any]


@dataclass(slots=True)
class DeveloperToolContext:
    bash_adapter: BashAdapter
    filesystem_adapter: FilesystemAdapter
    workspace_root: Path
    require_human_approval_for_repo_writes: bool
    container_session_adapter: ContainerSessionBashAdapter | None = None
    mcp_tool_adapter: MCPDeveloperToolAdapter | None = None
    enable_mcp_adapters: bool = False
    enable_agent_live_logs: bool = False
    bound_session_id: str | None = None
    output_dir: Path | None = None
    use_legacy_patch_tool: bool = False
    isolation_policy: DeveloperIsolationPolicy | None = None
    task_budget: DeveloperTaskBudget | None = None


def build_role_tools(*, context: DeveloperToolContext) -> dict[str, tuple[ToolFunc, ...]]:
    return {
        AgentRole.DEVELOPER.value: build_developer_tools(context=context),
    }


def build_developer_tools(*, context: DeveloperToolContext) -> tuple[ToolFunc, ...]:
    if context.enable_mcp_adapters and context.mcp_tool_adapter is None:
        raise RuntimeError("MCP adapters enabled but no MCP tool adapter was provided")

    policy = context.isolation_policy or default_developer_isolation_policy()
    if context.task_budget is not None and context.use_legacy_patch_tool:
        raise ValueError("Budgeted tasks require structured file edits, not the legacy patch adapter")
    active_session_id: str | None = None

    def _preview(value: Any, *, max_chars: int = 160) -> str:
        text = str(value).replace("\n", "\\n")
        return text[:max_chars]

    def _emit_tool_live(event: str, *, tool_name: str, **fields: Any) -> None:
        if not context.enable_agent_live_logs:
            return

        timestamp = datetime.now(tz=UTC).isoformat()
        detail = " ".join(f"{key}={value!r}" for key, value in fields.items())
        print(f"[developer-tool-live] {timestamp} {event} tool={tool_name!r} {detail}".rstrip(), flush=True)

    def _resolve_session_id(session_id: str | None) -> str | None:
        nonlocal active_session_id

        if context.task_budget is not None:
            context.task_budget.remaining_seconds()
        if context.bound_session_id is not None:
            if session_id is not None and session_id.strip() != context.bound_session_id:
                raise ValueError("Tool session must match this Developer's bound session")
            active_session_id = context.bound_session_id
            if context.container_session_adapter is not None:
                if context.task_budget is not None and isinstance(context.container_session_adapter, ContainerSessionBashAdapter):
                    context.container_session_adapter.create_session(
                        session_id=active_session_id, read_only_workspace=True,
                        deadline=datetime.now(tz=UTC).timestamp() + context.task_budget.remaining_seconds(),
                    )
                    try:
                        context.task_budget.remaining_seconds()
                    except TimeoutError:
                        context.container_session_adapter.close_session(session_id=active_session_id)
                        raise
                else:
                    context.container_session_adapter.create_session(session_id=active_session_id)
            return active_session_id

        if session_id is not None and session_id.strip():
            active_session_id = session_id.strip()
            return active_session_id

        if context.container_session_adapter is None:
            return None

        if active_session_id is not None:
            return active_session_id

        list_session_ids = getattr(context.container_session_adapter, "list_session_ids", None)
        if callable(list_session_ids):
            try:
                existing_session_ids = list_session_ids()
            except Exception:
                existing_session_ids = ()

            if len(existing_session_ids) == 1:
                active_session_id = str(existing_session_ids[0])
                return active_session_id

        created_session_id, _ = context.container_session_adapter.create_session()
        active_session_id = created_session_id
        return active_session_id

    def _workspace_root_for_session(session_id: str | None) -> Path:
        if context.container_session_adapter is not None and session_id:
            return context.container_session_adapter.get_workspace_root(session_id)
        if context.bound_session_id is not None and session_id:
            return prepare_developer_workspace(context.workspace_root, session_id)
        return context.workspace_root

    def _workspace_path(path: str, session_id: str | None) -> Path:
        root = _workspace_root_for_session(session_id).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError("File path resolves outside this Developer's workspace")
        if not is_path_allowed(target.relative_to(root).as_posix(), policy=policy):
            raise ValueError("File path resolves outside allowed task policy paths")
        return target

    @tool(
        name="developer_run_command",
        description=(
            "Run a short command in this Developer's workspace. Check exit_code: 0 means success, "
            "nonzero means failure, and 124 means the execution deadline was reached. "
            "Output is bounded; use max_output_chars only when the omitted detail is needed. "
            "For servers, watchers, interactive programs, or long builds, use the managed shell "
            "tools to detach and read the same shell later instead of launching duplicate work."
        ),
        approval_mode="always_require",
    )
    def developer_run_command(
        command: Annotated[
            str,
            Field(description="Command to run in the developer workspace or active session."),
        ],
        session_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional container session id. If omitted, the active or sole session is reused."
                )
            ),
        ] = None,
        max_output_chars: Annotated[
            int, Field(description="Maximum characters returned per output stream (256..24000).", ge=256, le=24000),
        ] = 6000,
    ) -> dict[str, Any]:
        """Run an allowed command for the Developer role."""
        tool_name = "developer_run_command"
        if not 256 <= max_output_chars <= 24000:
            raise ValueError("max_output_chars must be 256..24000; use 6000 for concise output")
        if context.isolation_policy is not None and not is_command_allowed(command, policy=policy):
            raise PermissionError("Command is outside allowed task policy prefixes")
        resolved_session_id = _resolve_session_id(session_id)
        _emit_tool_live(
            "call.start",
            tool_name=tool_name,
            session_id=resolved_session_id,
            command_preview=_preview(command),
        )

        try:
            if isinstance(context.container_session_adapter, ContainerSessionBashAdapter):
                if resolved_session_id is None:
                    raise ValueError("A Developer container session is required for managed commands")
                managed = shell_request(context.container_session_adapter, resolved_session_id, {
                    "action": "start", "command": command,
                    "wait_seconds": 10, "max_output_chars": max_output_chars,
                })
                return {**managed, "command": command, "stdout": managed.get("output", ""), "stderr": ""}
            if context.mcp_tool_adapter is not None:
                if resolved_session_id is None:
                    raise ValueError("session_id is required when MCP adapters are enabled")
                result = context.mcp_tool_adapter.run_command(
                    session_id=resolved_session_id,
                    command=command,
                )
            else:
                result_obj = context.bash_adapter.run(
                    role=AgentRole.DEVELOPER,
                    command=command,
                    session_id=resolved_session_id,
                )
                result = {
                    "command": result_obj.command,
                    "exit_code": result_obj.exit_code,
                    "stdout": result_obj.stdout,
                    "stderr": result_obj.stderr,
                }
        except Exception as exc:
            _emit_tool_live(
                "call.error",
                tool_name=tool_name,
                session_id=resolved_session_id,
                error=_preview(exc),
            )
            raise

        _emit_tool_live(
            "call.end",
            tool_name=tool_name,
            session_id=resolved_session_id,
            exit_code=int(result.get("exit_code", 0)),
            stdout_preview=_preview(result.get("stdout", "")),
            stderr_preview=_preview(result.get("stderr", "")),
        )

        return {
            "command": str(result.get("command", command)),
            "exit_code": int(result.get("exit_code", 0)),
            "stdout": str(result.get("stdout", ""))[-max_output_chars:],
            "stderr": str(result.get("stderr", ""))[-max_output_chars:],
            "output_truncated": any(
                len(str(result.get(stream, ""))) > max_output_chars for stream in ("stdout", "stderr")
            ),
            "next_action": (
                "The deadline was reached, not success. Use managed shell start/read for long work."
                if int(result.get("exit_code", 0)) == 124 else None
            ),
        }

    @tool(
        name="developer_read_file",
        description="Read a workspace-relative file allowed by policy.",
        approval_mode="always_require",
    )
    def developer_read_file(
        path: Annotated[
            str,
            Field(description="Workspace-relative file path to read. Must be allowed by policy."),
        ],
        session_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional container session id. If omitted, the active or sole session is reused."
                )
            ),
        ] = None,
    ) -> str:
        """Read a workspace-relative file allowed by policy."""
        tool_name = "developer_read_file"
        normalized_path = _normalize_workspace_relative_path(path)
        if not is_path_allowed(normalized_path, policy=policy):
            raise ValueError(f"path is outside allowed policy paths: {normalized_path}")

        resolved_session_id = _resolve_session_id(session_id)
        _emit_tool_live(
            "call.start",
            tool_name=tool_name,
            session_id=resolved_session_id,
            path=normalized_path,
        )

        try:
            if context.mcp_tool_adapter is not None:
                if resolved_session_id is None:
                    raise ValueError("session_id is required when MCP adapters are enabled")
                content = context.mcp_tool_adapter.read_file(
                    session_id=resolved_session_id,
                    path=normalized_path,
                )
            else:
                target = _workspace_path(normalized_path, resolved_session_id)
                content = context.filesystem_adapter.read_text(
                    role=AgentRole.DEVELOPER,
                    path=target,
                    session_id=resolved_session_id,
                )
        except Exception as exc:
            _emit_tool_live(
                "call.error",
                tool_name=tool_name,
                session_id=resolved_session_id,
                path=normalized_path,
                error=_preview(exc),
            )
            raise

        _emit_tool_live(
            "call.end",
            tool_name=tool_name,
            session_id=resolved_session_id,
            path=normalized_path,
            content_len=len(content),
            content_preview=_preview(content),
        )
        return content

    @tool(
        name="developer_write_file",
        description=(
            "Write a workspace-relative file allowed by policy. Use this only for new files or "
            "intentional full-file replacement. Prefer developer_edit_file for targeted edits."
        ),
        approval_mode="always_require",
    )
    def developer_write_file(
        path: Annotated[
            str,
            Field(description="Workspace-relative file path to write. Must be allowed by policy."),
        ],
        content: Annotated[
            str,
            Field(description="Full file content to write to the target path."),
        ],
        approved: Annotated[
            bool,
            Field(description="Set true to approve a repo write when approval gating is enabled."),
        ] = True,
        session_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional container session id. If omitted, the active or sole session is reused."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Write a workspace-relative file allowed by policy.

        Prefer developer_edit_file for targeted edits. Use this tool when creating
        new files or when full-file replacement is explicitly intended.
        Set approved=true for repo writes in approval-gated runs.
        """
        tool_name = "developer_write_file"
        normalized_path = _normalize_workspace_relative_path(path)
        if not is_path_allowed(normalized_path, policy=policy):
            raise ValueError(f"path is outside allowed policy paths: {normalized_path}")

        assert_role_action_allowed(AgentRole.DEVELOPER, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=context.require_human_approval_for_repo_writes,
            approved=approved,
        )
        if context.task_budget is not None:
            context.task_budget.reserve_paths((normalized_path,))
        resolved_session_id = _resolve_session_id(session_id)
        _emit_tool_live(
            "call.start",
            tool_name=tool_name,
            session_id=resolved_session_id,
            path=normalized_path,
            content_len=len(content),
            approved=approved,
        )

        try:
            if context.mcp_tool_adapter is not None:
                if resolved_session_id is None:
                    raise ValueError("session_id is required when MCP adapters are enabled")
                context.mcp_tool_adapter.write_file(
                    session_id=resolved_session_id,
                    path=normalized_path,
                    content=content,
                )
            else:
                target = _workspace_path(normalized_path, resolved_session_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                context.filesystem_adapter.write_text(
                    role=AgentRole.DEVELOPER,
                    path=target,
                    content=content,
                    approved=approved,
                    require_human_approval_for_repo_writes=context.require_human_approval_for_repo_writes,
                    session_id=resolved_session_id,
                )
        except Exception as exc:
            _emit_tool_live(
                "call.error",
                tool_name=tool_name,
                session_id=resolved_session_id,
                path=normalized_path,
                error=_preview(exc),
            )
            raise

        _emit_tool_live(
            "call.end",
            tool_name=tool_name,
            session_id=resolved_session_id,
            path=normalized_path,
            written=True,
        )
        return {
            "path": normalized_path,
            "written": True,
        }

    @tool(
        name="developer_edit_file",
        description=(
            "Replace one exact text span in an existing workspace file after reading it. "
            "Supply path, old_text and new_text as separate JSON fields, not a diff or patch. "
            "old_text must be non-empty and match exactly once, including whitespace. "
            "To insert, copy a unique existing anchor into old_text and repeat it with the addition "
            "in new_text. To delete a span, set new_text to an empty string. "
            "Stale or ambiguous matches fail without changing the file; re-read or use a larger anchor. "
            "Use developer_write_file to create files."
        ),
        approval_mode="always_require",
    )
    def developer_edit_file(
        path: Annotated[str, Field(description="Workspace-relative path of an existing file.")],
        old_text: Annotated[str, Field(min_length=1, description="Exact unique text copied from the current file.")],
        new_text: Annotated[str, Field(description="Literal replacement text; no diff markers. Empty deletes the span.")],
        approved: Annotated[bool, Field(description="Repo-write approval; framework approval is separate.")] = True,
        session_id: Annotated[str | None, Field(description="Optional bound Developer session identity.")] = None,
    ) -> dict[str, Any]:
        if not old_text:
            raise ValueError("old_text must be non-empty; insert using a unique existing anchor")
        assert_role_action_allowed(AgentRole.DEVELOPER, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=context.require_human_approval_for_repo_writes,
            approved=approved,
        )
        current_content = developer_read_file(path, session_id=session_id)
        match = current_content.find(old_text)
        if match < 0:
            raise ValueError("old_text not found; re-read the current file and copy an exact span. File unchanged.")
        if current_content.find(old_text, match + 1) >= 0:
            raise ValueError("old_text matches more than once; include more unchanged context. File unchanged.")
        updated = current_content[:match] + new_text + current_content[match + len(old_text):]
        developer_write_file(path, updated, approved=approved, session_id=session_id)
        return {"path": _normalize_workspace_relative_path(path), "applied": True,
                "changed": updated != current_content, "replacement_count": 1}

    @tool(
        name="developer_apply_patch",
        description=(
            "Edit existing workspace files after reading their current contents. Send a literal patch "
            "string, not Markdown. Use *** Update File: <relative-path>, then @@ on its own line. "
            "An insertion MUST include unchanged context from the existing file; @@ alone is not an anchor. "
            "Copy unchanged context exactly; prefix every removed line with - and every added line "
            "(including blank lines) with +. Each old/context block must match exactly once. "
            "Omit envelope markers and line numbers. Add/delete files use *** Add File: or *** Delete File:. "
            "On failure read the returned diagnosis; correct the format before retrying, and re-read "
            "the file only for stale/missing context. approved defaults true; framework approval is separate. "
            "Example patch argument inserting after one unique line:\n"
            "*** Update File: src/example.py\n"
            "@@\n"
            "old = 1\n"
            "+new = 2\n"
        ),
        approval_mode="always_require",
    )
    def developer_apply_patch(
        patch: Annotated[
            str,
            Field(description="Literal patch text following the tool's example. No code fences or decorated markers."),
        ],
        approved: Annotated[
            bool,
            Field(description="Set true to approve a repo write when approval gating is enabled."),
        ] = True,
        session_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional container session id. If omitted, the active or sole session is reused."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Apply a uniquely context-matched patch without replacing the whole file."""
        tool_name = "developer_apply_patch"
        resolved_session_id = _resolve_session_id(session_id)
        _emit_tool_live(
            "call.start",
            tool_name=tool_name,
            session_id=resolved_session_id,
            patch_preview=_preview(patch),
            approved=approved,
        )

        try:
            operations = parse_patch_document(patch)
            if not operations:
                raise ValueError("patch must contain at least one file operation")

            results: list[dict[str, Any]] = []
            for operation in operations:
                normalized_path = _normalize_workspace_relative_path(operation.path)
                if not is_path_allowed(normalized_path, policy=policy):
                    raise ValueError(f"path is outside allowed policy paths: {normalized_path}")

                target = _workspace_path(normalized_path, resolved_session_id)
                if operation.action == "delete":
                    if context.mcp_tool_adapter is not None:
                        raise ValueError("delete operations are not supported when MCP adapters are enabled")
                    if not target.exists():
                        raise ValueError(f"delete target does not exist: {normalized_path}")
                    assert_role_action_allowed(AgentRole.DEVELOPER, ActionClass.REPO_WRITE)
                    assert_repo_write_approval(
                        require_human_approval_for_repo_writes=context.require_human_approval_for_repo_writes,
                        approved=approved,
                    )
                    if target.is_dir():
                        raise ValueError(f"delete target must be a file: {normalized_path}")
                    target.unlink()
                    results.append({"path": normalized_path, "action": "delete"})
                    continue

                if operation.action == "add":
                    if target.exists():
                        raise ValueError(f"add target already exists: {normalized_path}")

                    if context.mcp_tool_adapter is None:
                        target.parent.mkdir(parents=True, exist_ok=True)

                    content = "\n".join(operation.add_lines)
                    context.filesystem_adapter.write_text(
                        role=AgentRole.DEVELOPER,
                        path=target,
                        content=content,
                        approved=approved,
                        require_human_approval_for_repo_writes=context.require_human_approval_for_repo_writes,
                        session_id=resolved_session_id,
                    )
                    results.append({"path": normalized_path, "action": "add"})
                    continue

                if operation.action != "update":
                    raise ValueError(f"unsupported patch action: {operation.action}")

                if not target.exists():
                    raise ValueError(f"update target does not exist: {normalized_path}")
                current_content = context.filesystem_adapter.read_text(
                    role=AgentRole.DEVELOPER,
                    path=target,
                    session_id=resolved_session_id,
                )
                updated_content = apply_update_hunks(current_content, operation.hunks)

                if context.mcp_tool_adapter is None:
                    target.parent.mkdir(parents=True, exist_ok=True)

                context.filesystem_adapter.write_text(
                    role=AgentRole.DEVELOPER,
                    path=target,
                    content=updated_content,
                    approved=approved,
                    require_human_approval_for_repo_writes=context.require_human_approval_for_repo_writes,
                    session_id=resolved_session_id,
                )
                results.append(
                    {
                        "path": normalized_path,
                        "action": "update",
                        "hunk_count": len(operation.hunks),
                    }
                )
        except Exception as exc:
            guidance = (
                "Patch apply failed. Re-read the latest file content, then regenerate a minimal patch "
                "with exact context and retry. Accepted formats: custom patch body ('*** Update File:' + "
                "'@@' hunks with +/- lines, wrapper optional) or unified diff ('---/+++' + '@@'). "
                "After each '*** Update File:' header, the next non-empty line must be '@@'. "
                "For bare '@@' custom hunks, context lines must be exact file text. Every inserted "
                "line in multiline additions must start with '+'. Prefer bare '@@' hunks without "
                "line numbers. Never send raw full-file content under '*** Update File:' without '@@'. "
                "Write tools default approved=true; set approved=false only when explicitly refusing a write."
            )
            _emit_tool_live(
                "call.error",
                tool_name=tool_name,
                session_id=resolved_session_id,
                error=_preview(exc),
            )
            raise ValueError(f"{guidance} Original error: {exc}") from exc

        _emit_tool_live(
            "call.end",
            tool_name=tool_name,
            session_id=resolved_session_id,
            operation_count=len(results),
        )
        return {
            "applied": True,
            "operation_count": len(results),
            "operations": results,
        }

    @tool(
        name="developer_start_session",
        description="Start or reuse a persistent container session.",
        approval_mode="always_require",
    )
    def developer_start_session(
        session_id: Annotated[
            str | None,
            Field(description="Optional desired session id. If omitted, a new id is generated."),
        ] = None,
    ) -> dict[str, str]:
        """Start or reuse a persistent container session for Developer commands."""
        tool_name = "developer_start_session"
        nonlocal active_session_id
        if context.container_session_adapter is None:
            raise ValueError("container_session mode is not enabled")

        if context.bound_session_id is not None:
            session_id = _resolve_session_id(session_id)

        _emit_tool_live(
            "call.start",
            tool_name=tool_name,
            requested_session_id=session_id,
        )

        try:
            if context.task_budget is not None and isinstance(context.container_session_adapter, ContainerSessionBashAdapter):
                created_session_id, container_name = context.container_session_adapter.create_session(
                    session_id=session_id, read_only_workspace=True,
                    deadline=datetime.now(tz=UTC).timestamp() + context.task_budget.remaining_seconds(),
                )
            else:
                created_session_id, container_name = context.container_session_adapter.create_session(session_id=session_id)
        except Exception as exc:
            _emit_tool_live(
                "call.error",
                tool_name=tool_name,
                requested_session_id=session_id,
                error=_preview(exc),
            )
            raise

        active_session_id = created_session_id
        _emit_tool_live(
            "call.end",
            tool_name=tool_name,
            session_id=created_session_id,
            container_name=container_name,
        )
        return {
            "session_id": created_session_id,
            "container_name": container_name,
        }

    @tool(
        name="developer_stop_session",
        description="Stop and clean up a persistent container session.",
        approval_mode="always_require",
    )
    def developer_stop_session(
        session_id: Annotated[
            str,
            Field(description="Session id to stop and clean up."),
        ]
    ) -> dict[str, Any]:
        """Stop and clean up a persistent container session."""
        tool_name = "developer_stop_session"
        nonlocal active_session_id
        if context.container_session_adapter is None:
            raise ValueError("container_session mode is not enabled")

        if context.bound_session_id is not None and session_id != context.bound_session_id:
            raise ValueError("Cannot stop another Developer's session")

        _emit_tool_live("call.start", tool_name=tool_name, session_id=session_id)

        try:
            closed = context.container_session_adapter.close_session(session_id=session_id)
        except Exception as exc:
            _emit_tool_live(
                "call.error",
                tool_name=tool_name,
                session_id=session_id,
                error=_preview(exc),
            )
            raise

        if closed and active_session_id == session_id:
            active_session_id = None
        _emit_tool_live("call.end", tool_name=tool_name, session_id=session_id, closed=closed)
        return {
            "session_id": session_id,
            "closed": closed,
        }

    def _search(*, kind: Literal["files", "content"], pattern: str, path: str, globs: list[str] | None,
                include_hidden: bool, include_ignored: bool, offset: int, max_results: int,
                session_id: str | None, is_regex: bool = False, case_sensitive: bool = True) -> dict[str, Any]:
        assert_role_action_allowed(AgentRole.DEVELOPER, ActionClass.READ_ONLY)
        resolved = _resolve_session_id(session_id)
        prefix: tuple[str, ...] = ()
        if isinstance(context.container_session_adapter, ContainerSessionBashAdapter):
            if resolved is None:
                raise ValueError("A Developer container session is required for search")
            container = context.container_session_adapter.get_container_name(session_id=resolved)
            if container is None:
                raise ValueError("Developer container session is not running")
            prefix = ("docker", "exec", container, "timeout", "20s")
        return search_workspace(
            workspace=_workspace_root_for_session(resolved), policy=policy, kind=kind,
            pattern=pattern, path=path, globs=globs, include_hidden=include_hidden,
            include_ignored=include_ignored, offset=offset, max_results=max_results,
            command_prefix=prefix, is_regex=is_regex, case_sensitive=case_sensitive,
        )

    @tool(name="developer_find_files", approval_mode="always_require", description=(
        "Find file paths using ripgrep discovery and glob filters in this Developer's allowed workspace paths. "
        "Use **/*.py or **/*test*.py, not shell commands. Results are sorted and paged. "
        "Ignore files are respected by default; hidden/ignored discovery is opt-in. "
        "Symlinks are not followed. Reuse next_offset only for needed pages."
    ))
    def developer_find_files(
        glob: Annotated[str, Field(description="File glob, e.g. **/*.py or *math*.")] = "**/*",
        path: Annotated[str, Field(description="Workspace-relative directory or file; . searches allowed roots.")] = ".",
        globs: Annotated[list[str] | None, Field(description="Additional include/exclude globs; prefix exclusions with !.")] = None,
        include_hidden: bool = False, include_ignored: bool = False,
        offset: Annotated[int, Field(ge=0, le=10000)] = 0,
        max_results: Annotated[int, Field(ge=1, le=100)] = 20,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return _search(kind="files", pattern=glob, path=path, globs=globs,
                       include_hidden=include_hidden, include_ignored=include_ignored,
                       offset=offset, max_results=max_results, session_id=session_id)

    @tool(name="developer_search_files", approval_mode="always_require", description=(
        "Search file contents with ripgrep, returning paths, 1-based line numbers, byte columns and "
        "bounded text previews. Literal matching is default; set is_regex for Rust regex syntax. "
        "Filter by path/globs before broad searches. No matches is a successful empty result. "
        "Hidden/ignored files are opt-in; symlinks are not followed and files over 1 MiB are skipped. "
        "Results are sorted and paged; read a selected file before making exact-text edits."
    ))
    def developer_search_files(
        pattern: Annotated[str, Field(description="Literal text or a Rust regex when is_regex=true.")],
        path: str = ".", globs: list[str] | None = None,
        is_regex: bool = False, case_sensitive: bool = True,
        include_hidden: bool = False, include_ignored: bool = False,
        offset: Annotated[int, Field(ge=0, le=10000)] = 0,
        max_results: Annotated[int, Field(ge=1, le=100)] = 20,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return _search(kind="content", pattern=pattern, path=path, globs=globs,
                       is_regex=is_regex, case_sensitive=case_sensitive,
                       include_hidden=include_hidden, include_ignored=include_ignored,
                       offset=offset, max_results=max_results, session_id=session_id)

    tools: tuple[ToolFunc, ...] = (
        developer_run_command,
        developer_read_file,
        developer_write_file,
        developer_apply_patch if context.use_legacy_patch_tool else developer_edit_file,
        developer_start_session,
        developer_stop_session,
        developer_find_files,
        developer_search_files,
    )
    if isinstance(context.container_session_adapter, ContainerSessionBashAdapter):
        tools += build_shell_tools(context.container_session_adapter, _resolve_session_id, policy=context.isolation_policy)
    if context.output_dir is not None and context.bound_session_id is not None:
        tools += (build_output_reader(context.output_dir / context.bound_session_id),)
    if IsolationTool.FILESYSTEM not in policy.allowed_tools:
        filesystem_names = {
            "developer_read_file", "developer_write_file", "developer_edit_file", "developer_apply_patch",
            "developer_find_files", "developer_search_files",
        }
        tools = tuple(tool_func for tool_func in tools if getattr(tool_func, "name", None) not in filesystem_names)
    if IsolationTool.BASH not in policy.allowed_tools:
        bash_names = {
            "developer_run_command", "developer_start_session", "developer_stop_session",
            "developer_shell", "developer_processes",
        }
        tools = tuple(tool_func for tool_func in tools if getattr(tool_func, "name", None) not in bash_names)
    if context.isolation_policy is not None:
        for tool_func in tools:
            name = getattr(tool_func, "name", None)
            description = getattr(tool_func, "description", "")
            if name in {"developer_run_command", "developer_shell"}:
                description += "\nTask command prefixes: " + ", ".join(policy.allowed_command_prefixes)
            if name in {
                "developer_read_file", "developer_write_file", "developer_edit_file", "developer_apply_patch",
                "developer_find_files", "developer_search_files",
            }:
                description += "\nTask allowed paths: " + ", ".join(policy.allowed_paths)
                description += "\nTask blocked paths: " + ", ".join(policy.blocked_paths)
            setattr(tool_func, "description", description)
    return tools

def _normalize_workspace_relative_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("path must be non-empty")
    if normalized.startswith("/"):
        raise ValueError("path must be workspace-relative")
    if ".." in normalized.split("/"):
        raise ValueError("path must not traverse parent directories")
    return normalized
