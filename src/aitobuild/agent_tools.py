"""Agent tool wiring for role-specific runtime tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Callable

from agent_framework import tool
from pydantic import Field

from aitobuild.developer_isolation import default_developer_isolation_policy, is_path_allowed
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
from aitobuild.tools.bash import BashAdapter


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


def build_role_tools(*, context: DeveloperToolContext) -> dict[str, tuple[ToolFunc, ...]]:
    return {
        AgentRole.DEVELOPER.value: build_developer_tools(context=context),
    }


def build_developer_tools(*, context: DeveloperToolContext) -> tuple[ToolFunc, ...]:
    if context.enable_mcp_adapters and context.mcp_tool_adapter is None:
        raise RuntimeError("MCP adapters enabled but no MCP tool adapter was provided")

    policy = default_developer_isolation_policy()
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

    @tool(
        name="developer_run_command",
        description="Run an allowed command in the developer workspace or session.",
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
    ) -> dict[str, Any]:
        """Run an allowed command for the Developer role."""
        tool_name = "developer_run_command"
        resolved_session_id = _resolve_session_id(session_id)
        _emit_tool_live(
            "call.start",
            tool_name=tool_name,
            session_id=resolved_session_id,
            command_preview=_preview(command),
        )

        try:
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
            "stdout": str(result.get("stdout", "")),
            "stderr": str(result.get("stderr", "")),
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
                target = context.workspace_root / normalized_path
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
            "intentional full-file replacement. Prefer developer_apply_patch for targeted edits."
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

        Prefer developer_apply_patch for targeted edits. Use this tool when creating
        new files or when full-file replacement is explicitly intended.
        Set approved=true for repo writes in approval-gated runs.
        """
        tool_name = "developer_write_file"
        normalized_path = _normalize_workspace_relative_path(path)
        if not is_path_allowed(normalized_path, policy=policy):
            raise ValueError(f"path is outside allowed policy paths: {normalized_path}")

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
                target = context.workspace_root / normalized_path
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
        name="developer_apply_patch",
        description=(
            "Apply a context-matched patch to workspace files. This is the preferred edit tool. "
            "Arguments: patch (required), session_id (optional), approved (optional, defaults true). "
            "Preferred patch format: custom patch with '*** Update File: <path>' and bare '@@' hunks "
            "(no line numbers). Hard rules: (1) after each '*** Update File:' header, the next non-empty "
            "line must be '@@'; (2) in custom hunks, unchanged context lines are unprefixed exact file text, "
            "removed lines start with '-', and added lines start with '+'; (3) for multiline insertions, every "
            "inserted line must start with '+', not only blank separators. Optionally wrap with '*** Begin Patch' "
            "and '*** End Patch'. Also accepted: unified diff with '---/+++' and '@@'. Forbidden patterns: "
            "'diff --git', 'index ...', raw full-file content under '*** Update File:' with no '@@' hunk, and "
            "numbered custom hunk headers unless explicitly required. Keep patches minimal and include enough "
            "unchanged context to match exactly one location."
        ),
        approval_mode="always_require",
    )
    def developer_apply_patch(
        patch: Annotated[
            str,
            Field(description="Patch text in custom envelope format or unified diff format."),
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
        """Apply a context-matched patch document.

                Prefer this tool over developer_write_file for targeted edits.

                 Preferred format:
                 1) Custom patch (envelope preferred, wrapper optional):
                     *** Begin Patch
                     *** Update File: path
                     @@
                     exact context line
                     -old line
                     +new line
                     *** End Patch
                 - Use bare '@@' (no line-number hunks) whenever possible.
                 - For bare '@@', context lines should be exact file text.

             Also accepted: unified diff update patch with ---/+++ and @@ hunks.

                Guidance:
                - Keep edits minimal and include enough context so each hunk matches exactly once.
                - Re-read files immediately before building a patch.
                - If writes are approval-gated, set approved=true.
        """
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

                target = context.workspace_root / normalized_path
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

        _emit_tool_live(
            "call.start",
            tool_name=tool_name,
            requested_session_id=session_id,
        )

        try:
            created_session_id, container_name = context.container_session_adapter.create_session(
                session_id=session_id,
            )
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

    return (
        developer_run_command,
        developer_read_file,
        developer_write_file,
        developer_apply_patch,
        developer_start_session,
        developer_stop_session,
    )

def _normalize_workspace_relative_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("path must be non-empty")
    if normalized.startswith("/"):
        raise ValueError("path must be workspace-relative")
    if ".." in normalized.split("/"):
        raise ValueError("path must not traverse parent directories")
    return normalized
