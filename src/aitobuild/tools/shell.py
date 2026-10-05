"""Concise, recoverable tools for Developer-owned terminals and processes."""

from __future__ import annotations

import json
import shlex
from typing import Annotated, Any, Callable, Literal
from uuid import uuid4

from agent_framework import tool
from pydantic import Field

from aitobuild.policy import AgentRole
from aitobuild.tools.bash import ContainerSessionBashAdapter


def shell_request(
    adapter: ContainerSessionBashAdapter, session_id: str, request: dict[str, Any],
) -> dict[str, Any]:
    wait_seconds = float(request.get("wait_seconds") or 0)
    wire_request = {**request, "wait_seconds": 0}
    reply_path = f"{adapter.home_dir}/.aitobuild/shell-replies/{uuid4().hex}.json"
    result = adapter.run(
        role=AgentRole.DEVELOPER, session_id=session_id,
        command=shlex.join(["python", "/opt/aitobuild/shell_runtime.py", "--reply-file", reply_path, json.dumps(wire_request)]),
    )
    if result.exit_code:
        return {"ok": False, "error_code": "shell_backend_unavailable", "error": result.stderr[-1000:],
                "next_action": "Rebuild Dockerfile.developer and restart this Developer container. List shells before restarting any command."}
    try:
        if not result.stdout.strip():
            result = adapter.run(
                role=AgentRole.DEVELOPER, session_id=session_id,
                command=shlex.join(["python", "-c",
                    "from pathlib import Path; import sys; path=Path(sys.argv[1]); "
                    "print(path.read_text()); path.unlink()", reply_path]),
            )
        else:
            adapter.run(
                role=AgentRole.DEVELOPER, session_id=session_id,
                command=shlex.join(["rm", "-f", reply_path]),
            )
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError("Expected a structured shell reply")
        if wait_seconds and response.get("status") == "running" and response.get("pid"):
            adapter.run(
                role=AgentRole.DEVELOPER, session_id=session_id,
                command=shlex.join(["python", "-c",
                    "import psutil,sys\ntry:\n psutil.Process(int(sys.argv[1])).wait(timeout=float(sys.argv[2]))\n"
                    "except (psutil.TimeoutExpired,psutil.NoSuchProcess):\n pass",
                    str(response["pid"]), str(wait_seconds)]),
            )
            return shell_request(adapter, session_id, {
                "action": "read", "shell_id": response["shell_id"],
                "cursor": request.get("cursor"),
                "max_output_chars": request.get("max_output_chars", 6000),
            })
        return response
    except ValueError as error:
        return {"ok": False, "error_code": "invalid_shell_reply", "error": f"Invalid backend JSON: {error}",
                "stdout_preview": result.stdout[:1000], "stderr_preview": result.stderr[:1000],
                "next_action": "Check the Developer image and list shells to recover; do not assume the command failed to start."}


def build_shell_tools(
    adapter: ContainerSessionBashAdapter, resolve_session: Callable[[str | None], str | None],
) -> tuple[Callable[..., Any], ...]:
    def invoke(request: dict[str, Any]) -> dict[str, Any]:
        session_id = resolve_session(None)
        if session_id is None:
            return {"ok": False, "error_code": "session_required", "error": "No Developer container session is active.",
                    "next_action": "Start a Developer container session, then retry in the same identity."}
        return shell_request(adapter, session_id, request)

    @tool(name="developer_shell", approval_mode="always_require", description=(
        "Manage this Developer's durable terminal jobs. start(command) launches detached and returns shell_id; "
        "omit command to open an interactive Bash shell. read(shell_id) reattaches to status/output WITHOUT restarting; "
        "reuse next_cursor as cursor to fetch only new bytes. send(shell_id,text) enters ONE input and Enter; "
        "control='interrupt' sends Ctrl-C, null sends literal text only. list rediscovers jobs after API restart. "
        "stop forcibly ends a managed shell and its current child processes (exit 137), retaining logs. "
        "wait_seconds bounds each call, NOT the job lifetime; running/null exit_code is not failure or success. "
        "Use start for builds/servers; do other work before read, never busy-poll or relaunch to get progress. "
        "Check ok/status/exit_code and next_action. Processes survive API disconnect, not container shutdown; logs persist. "
        "Never send credentials; ask the human to handle them directly."
    ))
    def developer_shell(
        action: Literal["start", "read", "send", "list", "stop"],
        shell_id: Annotated[str | None, Field(description="ID from start/list, required for read/send/stop.")] = None,
        command: Annotated[str | None, Field(description="For start only: command, or omit for interactive Bash.")] = None,
        text: Annotated[str | None, Field(description="For send only: one non-secret input response.")] = None,
        control: Literal["enter", "interrupt", "eof", "tab", "escape"] | None = "enter",
        cursor: Annotated[int | None, Field(description="For read: previous next_cursor, or omit for latest output.", ge=0)] = None,
        wait_seconds: Annotated[float, Field(description="Bounded wait for exit; 0 returns immediately. Does not kill the job.", ge=0, le=30)] = 0,
        max_output_chars: Annotated[int, Field(description="Output budget; keep 6000 unless more detail is needed.", ge=256, le=24000)] = 6000,
    ) -> dict[str, Any]:
        return invoke({"action": action, "shell_id": shell_id, "command": command, "text": text,
                       "control": control, "cursor": cursor, "wait_seconds": wait_seconds,
                       "max_output_chars": max_output_chars})

    @tool(name="developer_processes", approval_mode="always_require", description=(
        "Inspect or signal processes inside this Developer's container only. list(query) returns at most 50 "
        "owned processes with PID, parent, status, command, cwd, CPU seconds, memory and process_handle. "
        "inspect(process_handle) adds child processes. signal(process_handle,signal) sends TERM (default), "
        "INT or KILL; prefer TERM, verify exit, then KILL only if needed. The handle binds PID to creation time "
        "so stale handles cannot target a reused PID; never invent one. A sent signal is NOT confirmed exit. "
        "Use developer_shell stop for a whole managed job. Container init/other users are protected. "
        "On ok=false follow next_action and refresh list before retrying."
    ))
    def developer_processes(
        action: Literal["list", "inspect", "signal"] = "list",
        query: Annotated[str | None, Field(description="Optional substring filter for list, e.g. pytest or node.")] = None,
        process_handle: Annotated[str | None, Field(description="Exact PID:creation-time handle from list, required for inspect/signal.")] = None,
        signal: Literal["TERM", "INT", "KILL"] = "TERM",
    ) -> dict[str, Any]:
        return invoke({"kind": "processes", "action": action, "query": query,
                       "process_handle": process_handle, "signal": signal})

    return developer_shell, developer_processes