"""MCP-backed adapters for developer command and filesystem operations."""

from __future__ import annotations

import asyncio
import json
from importlib import import_module
from pathlib import Path
from typing import Any

from agent_framework import MCPStdioTool
from aitobuild.policy import ActionClass, AgentRole, assert_repo_write_approval, assert_role_action_allowed
from aitobuild.tools.bash import BashAdapter, BashResult, ContainerSessionBashAdapter
from aitobuild.tools.filesystem import FilesystemAdapter


class MCPDeveloperToolAdapter:
    """Invoke MCP tools through agent_framework MCP stdio transport."""

    def __init__(
        self,
        *,
        container_session_adapter: ContainerSessionBashAdapter,
        workspace_root: Path,
        shell_tool_name: str,
        filesystem_read_tool_name: str,
        filesystem_write_tool_name: str,
        request_timeout_seconds: int,
        filesystem_workdir: str,
    ) -> None:
        self._container_session_adapter = container_session_adapter
        self._workspace_root = workspace_root
        self._shell_tool_name = shell_tool_name
        self._filesystem_read_tool_name = filesystem_read_tool_name
        self._filesystem_write_tool_name = filesystem_write_tool_name
        self._request_timeout_seconds = request_timeout_seconds
        self._filesystem_workdir = filesystem_workdir

        self._mcp_stdio_tool_class = self._require_mcp_stdio_tool_class()

    def run_command(self, *, session_id: str, command: str) -> dict[str, Any]:
        result = self._call_mcp_tool(
            session_id=session_id,
            server_name="developer-shell",
            server_command="@modelcontextprotocol/server-shell",
            tool_name=self._shell_tool_name,
            kwargs={"command": command},
        )

        parsed = _parse_result_payload(result)
        exit_code = int(parsed.get("exit_code", 0)) if isinstance(parsed.get("exit_code"), int) else 0
        stdout = str(parsed.get("stdout", parsed.get("output", _result_to_text(result))))
        stderr = str(parsed.get("stderr", ""))
        return {
            "command": command,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }

    def read_file(self, *, session_id: str, path: str) -> str:
        result = self._call_mcp_tool(
            session_id=session_id,
            server_name="developer-filesystem-read",
            server_command="@modelcontextprotocol/server-filesystem",
            server_args=[self._filesystem_workdir],
            tool_name=self._filesystem_read_tool_name,
            kwargs={"path": f"{self._filesystem_workdir}/{path}"},
        )

        parsed = _parse_result_payload(result)
        if "content" in parsed:
            return str(parsed["content"])
        return _result_to_text(result)

    def write_file(self, *, session_id: str, path: str, content: str) -> None:
        _ = self._call_mcp_tool(
            session_id=session_id,
            server_name="developer-filesystem-write",
            server_command="@modelcontextprotocol/server-filesystem",
            server_args=[self._filesystem_workdir],
            tool_name=self._filesystem_write_tool_name,
            kwargs={"path": f"{self._filesystem_workdir}/{path}", "content": content},
        )

    def _call_mcp_tool(
        self,
        *,
        session_id: str,
        server_name: str,
        server_command: str,
        tool_name: str,
        kwargs: dict[str, Any],
        server_args: list[str] | None = None,
    ) -> str | list[Any]:
        container_name = self._container_session_adapter.get_container_name(session_id=session_id)
        if container_name is None:
            raise RuntimeError("No active container session for provided session_id")

        args = [
            "exec",
            "-i",
            container_name,
            "npx",
            "-y",
            server_command,
        ] + (server_args or [])

        async def invoke() -> str | list[Any]:
            tool = self._mcp_stdio_tool_class(
                name=server_name,
                command="docker",
                args=args,
                load_prompts=False,
                request_timeout=self._request_timeout_seconds,
                approval_mode="always_require",
            )
            async with tool:
                return await tool.call_tool(tool_name, **kwargs)

        return _run_async(invoke())

    @staticmethod
    def _require_mcp_stdio_tool_class() -> type[Any]:
        try:
            module = import_module("agent_framework")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "MCP adapters require agent_framework with MCP support installed"
            ) from exc

        mcp_stdio_tool = getattr(module, "MCPStdioTool", None)
        if not isinstance(mcp_stdio_tool, type):
            raise RuntimeError("agent_framework.MCPStdioTool is required for MCP adapters")

        return mcp_stdio_tool


class MCPBashAdapter(BashAdapter):
    def __init__(self, *, mcp_adapter: MCPDeveloperToolAdapter) -> None:
        self._mcp_adapter = mcp_adapter

    def run(self, *, role: AgentRole, command: str, session_id: str | None = None) -> BashResult:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        if session_id is None or not session_id.strip():
            raise ValueError("session_id is required for MCP command execution")

        result = self._mcp_adapter.run_command(session_id=session_id.strip(), command=command)
        return BashResult(
            command=command,
            exit_code=int(result["exit_code"]),
            stdout=str(result["stdout"]),
            stderr=str(result["stderr"]),
        )


class MCPFilesystemAdapter(FilesystemAdapter):
    def __init__(self, *, mcp_adapter: MCPDeveloperToolAdapter, workspace_root: Path) -> None:
        self._mcp_adapter = mcp_adapter
        self._workspace_root = workspace_root

    def read_text(
        self,
        *,
        role: AgentRole,
        path: Path,
        session_id: str | None = None,
    ) -> str:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        if session_id is None or not session_id.strip():
            raise ValueError("session_id is required for MCP filesystem read")

        root = self._mcp_adapter._container_session_adapter.get_workspace_root(session_id)
        relative_path = _workspace_relative_path(path=path, workspace_root=root)
        return self._mcp_adapter.read_file(session_id=session_id.strip(), path=relative_path)

    def write_text(
        self,
        *,
        role: AgentRole,
        path: Path,
        content: str,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        session_id: str | None = None,
    ) -> None:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        if session_id is None or not session_id.strip():
            raise ValueError("session_id is required for MCP filesystem write")

        root = self._mcp_adapter._container_session_adapter.get_workspace_root(session_id)
        relative_path = _workspace_relative_path(path=path, workspace_root=root)
        self._mcp_adapter.write_file(
            session_id=session_id.strip(),
            path=relative_path,
            content=content,
        )


def _workspace_relative_path(*, path: Path, workspace_root: Path) -> str:
    resolved_workspace = workspace_root.resolve()
    resolved_path = path.resolve()
    try:
        relative = resolved_path.relative_to(resolved_workspace)
    except ValueError as exc:
        raise ValueError("path is outside workspace root") from exc
    return str(relative).replace("\\", "/")


def build_browser_tool(
    *, container_session_adapter: ContainerSessionBashAdapter,
    session_id: str, request_timeout_seconds: int,
) -> MCPStdioTool:
    container_name = container_session_adapter.get_container_name(session_id=session_id)
    if container_name is None:
        raise ValueError("Browser tools require an active Developer container session")
    return MCPStdioTool(
        name="developer-browser",
        command="docker",
        args=[
            "exec", "-i", container_name, "playwright-mcp", "--headless",
            "--executable-path", "/usr/bin/chromium", "--no-sandbox", "--no-webmcp",
            "--user-data-dir", f"{container_session_adapter.home_dir}/.aitobuild/browser",
            "--output-dir", f"{container_session_adapter.home_dir}/.aitobuild/browser-output",
        ],
        load_prompts=False,
        approval_mode="always_require",
        allowed_tools=(
            "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
            "browser_type", "browser_press_key", "browser_select_option", "browser_tabs",
            "browser_close", "browser_take_screenshot", "browser_resize", "browser_wait_for",
            "browser_console_messages", "browser_network_requests",
        ),
        request_timeout=request_timeout_seconds,
    )


def _run_async(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    raise RuntimeError("MCP adapter invocation requires no active event loop in current thread")


def _result_to_text(result: str | list[Any]) -> str:
    if isinstance(result, str):
        return result

    parts: list[str] = []
    for item in result:
        if isinstance(item, str):
            parts.append(item)
            continue

        text_value = getattr(item, "text", None)
        if isinstance(text_value, str):
            parts.append(text_value)
            continue

        parts.append(str(item))

    return "\n".join(parts)


def _parse_result_payload(result: str | list[Any]) -> dict[str, Any]:
    text = _result_to_text(result)
    if not text.strip():
        return {}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"output": text}

    if isinstance(parsed, dict):
        return parsed
    return {"output": text}
