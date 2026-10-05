"""FastAPI ingress for webhook and internal trigger events."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from importlib import import_module
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest, new
from inspect import isawaitable
from json import dumps, loads
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable
from uuid import uuid4

from agent_framework import AgentSession, FileSessionStore, FunctionInvocationContext, function_middleware
from fastapi import FastAPI, Header, HTTPException, Query, Request

from aitobuild.agent_tools import DeveloperToolContext, build_role_tools
from aitobuild.config import AppConfig, load_config
from aitobuild.developer_execution import DeveloperExecutionEngine, PlannedFileWrite
from aitobuild.developer_isolation import developer_task_bundle_from_payload
from aitobuild.dispatcher import DispatcherAgent
from aitobuild.events import make_internal_event, normalize_github_webhook, parse_trigger_request
from aitobuild.proactive import ArchitectScanRunner
from aitobuild.runtime import bootstrap_runtime, kickoff_meeting_bootstrap
from aitobuild.scheduler import scheduler_from_config
from aitobuild.tools import (
    BashAdapter,
    ContainerSessionBashAdapter,
    FilesystemAdapter,
    MCPBashAdapter,
    MCPDeveloperToolAdapter,
    MCPFilesystemAdapter,
    MockBashAdapter,
    MockFilesystemAdapter,
    SubprocessBashAdapter,
)
from aitobuild.triggers import InMemoryDedupeStore, TriggerEngine
from aitobuild.tools.mcp_adapters import build_browser_tool
from aitobuild.tools.bash import _normalize_session_id
from aitobuild.tool_outputs import MAX_PROMPT_BYTES, build_output_guard


def _is_valid_signature(*, secret: str, payload: bytes, provided: str | None) -> bool:
    if not provided:
        return False
    digest = new(secret.encode("utf-8"), payload, sha256).hexdigest()
    expected = f"sha256={digest}"
    return compare_digest(expected, provided)


def _is_valid_internal_token(*, expected_token: str | None, provided_token: str | None) -> bool:
    if expected_token is None:
        return False
    if provided_token is None:
        return False
    return compare_digest(expected_token, provided_token)


def _assert_internal_auth(*, config: AppConfig, provided_token: str | None) -> None:
    if not config.security.require_internal_auth:
        return
    if not _is_valid_internal_token(
        expected_token=config.security.internal_api_token,
        provided_token=provided_token,
    ):
        raise HTTPException(status_code=401, detail="Invalid internal token")


def _extract_response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    return str(response)


def _extract_user_input_requests(response: Any) -> list[Any]:
    user_input_requests = getattr(response, "user_input_requests", None)
    if user_input_requests is None:
        return []
    if isinstance(user_input_requests, list):
        return user_input_requests
    try:
        return list(user_input_requests)
    except TypeError:
        return []


def _serialize_user_input_request(request: Any) -> dict[str, Any]:
    function_call = getattr(request, "function_call", None)
    function_name = getattr(function_call, "name", None)
    function_args = getattr(function_call, "arguments", None)
    request_id = getattr(request, "request_id", None)

    return {
        "request_id": request_id if isinstance(request_id, str) else None,
        "function_name": function_name if isinstance(function_name, str) else None,
        "arguments": function_args,
    }


def _create_agent_session(agent_handle: Any, *, session_id: str | None) -> tuple[Any, str | None]:
    create_session = getattr(agent_handle, "create_session", None)
    if not callable(create_session):
        raise RuntimeError("Developer agent does not support session creation")

    session: Any
    try:
        if session_id is not None:
            session = create_session(session_id=session_id)
        else:
            session = create_session()
    except TypeError:
        session = create_session()

    session_identifier = getattr(session, "session_id", None)
    if isinstance(session_identifier, str) and session_identifier.strip():
        return session, session_identifier.strip()
    return session, session_id


def _build_approval_replay_message(approval_response: Any) -> Any:
    """Build a user message for function-approval replay when Message is available."""
    try:
        message_class = getattr(import_module("agent_framework"), "Message", None)
    except Exception:
        return approval_response

    if message_class is None:
        return approval_response

    try:
        return message_class(role="user", contents=[approval_response])
    except Exception:
        return approval_response


def _tool_name(tool: Any) -> str:
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()

    fallback = getattr(tool, "__name__", None)
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()

    return type(tool).__name__


def _tool_description(tool: Any) -> str | None:
    description = getattr(tool, "description", None)
    if isinstance(description, str) and description.strip():
        return description.strip()

    doc = getattr(tool, "__doc__", None)
    if isinstance(doc, str) and doc.strip():
        return doc.strip()

    return None


def _build_tool_usage_guidance(*, tools: tuple[Any, ...]) -> str:
    if not tools:
        return ""

    lines: list[str] = [
        "Tool usage contracts (authoritative):",
        "Follow each tool description exactly when choosing arguments and formats.",
    ]
    for tool in tools:
        name = _tool_name(tool)
        description = _tool_description(tool)
        if description is None:
            lines.append(f"- {name}")
        else:
            lines.append(f"- {name}: {description}")

    return "\n".join(lines)


def _augment_prompt_with_tool_guidance(*, prompt: str, tools: tuple[Any, ...]) -> str:
    guidance = _build_tool_usage_guidance(tools=tools)
    if not guidance:
        return prompt

    return f"{guidance}\n\nTask:\n{prompt}"


async def _invoke_agent_run(
    *, agent_handle: Any, message: Any, session: Any | None,
    tools: tuple[Any, ...] | None = None,
    middleware: list[Any] | None = None,
) -> Any:
    run_method = getattr(agent_handle, "run", None)
    if not callable(run_method):
        raise RuntimeError("Developer agent does not provide callable run(...)")

    if session is not None:
        try:
            kwargs: dict[str, Any] = {"session": session, "options": {"store": False}}
            if tools is not None:
                kwargs["tools"] = tools
            if middleware is not None:
                kwargs["middleware"] = middleware
            outcome = run_method(message, **kwargs)
        except TypeError:
            try:
                outcome = run_method(message, session=session)
            except TypeError:
                outcome = run_method(message)
    else:
        outcome = run_method(message)

    if isawaitable(outcome):
        return await outcome
    return outcome


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or load_config()
    if app_config.developer.enable_browser and app_config.developer.execution_mode != "container_session":
        raise ValueError("Developer browser requires container_session mode")

    app = FastAPI(title="aitobuild")
    trigger_engine = TriggerEngine(dedupe_store=InMemoryDedupeStore())
    dispatcher = DispatcherAgent(
        require_developer_preview=app_config.developer.require_preview_before_dispatch,
    )
    scheduler = scheduler_from_config(app_config.scheduler)
    architect_scan_runner = ArchitectScanRunner()
    workspace_root = Path.cwd()
    developer_state_dir = (workspace_root / app_config.developer.state_dir).resolve()
    developer_session_store = FileSessionStore(developer_state_dir / "sessions")
    developer_sessions: dict[str, Any] = {}
    active_developer_runs: set[str] = set()
    filesystem_adapter: FilesystemAdapter = MockFilesystemAdapter()

    bash_adapter: BashAdapter
    container_session_bash: ContainerSessionBashAdapter | None = None
    if app_config.developer.execution_mode == "subprocess":
        bash_adapter = SubprocessBashAdapter(
            workspace_root=workspace_root,
            timeout_seconds=app_config.developer.command_timeout_seconds,
        )
    elif app_config.developer.execution_mode == "container_session":
        container_session_bash = ContainerSessionBashAdapter(
            workspace_root=workspace_root,
            image=app_config.developer.session_container_image,
            container_workdir=app_config.developer.session_container_workdir,
            container_name_prefix=app_config.developer.session_container_name_prefix,
            bind_source_path=app_config.developer.session_container_bind_path,
            run_as_current_user=app_config.developer.session_container_run_as_current_user,
            timeout_seconds=app_config.developer.command_timeout_seconds,
            data_volume_name=app_config.developer.session_data_volume,
        )
        bash_adapter = container_session_bash
    else:
        bash_adapter = MockBashAdapter()

    mcp_tool_adapter: MCPDeveloperToolAdapter | None = None
    if app_config.developer.enable_mcp_adapters:
        if container_session_bash is None:
            raise RuntimeError(
                "AITOBUILD_DEVELOPER_ENABLE_MCP_ADAPTERS requires execution_mode=container_session"
            )

        mcp_tool_adapter = MCPDeveloperToolAdapter(
            container_session_adapter=container_session_bash,
            workspace_root=workspace_root,
            shell_tool_name=app_config.developer.mcp_shell_tool_name,
            filesystem_read_tool_name=app_config.developer.mcp_filesystem_read_tool_name,
            filesystem_write_tool_name=app_config.developer.mcp_filesystem_write_tool_name,
            request_timeout_seconds=app_config.developer.command_timeout_seconds,
            filesystem_workdir=app_config.developer.session_container_workdir,
        )
        bash_adapter = MCPBashAdapter(mcp_adapter=mcp_tool_adapter)
        filesystem_adapter = MCPFilesystemAdapter(
            mcp_adapter=mcp_tool_adapter,
            workspace_root=workspace_root,
        )

    developer_tool_context = DeveloperToolContext(
            bash_adapter=bash_adapter,
            filesystem_adapter=filesystem_adapter,
            workspace_root=workspace_root,
            require_human_approval_for_repo_writes=app_config.policy.require_human_approval_for_repo_writes,
            container_session_adapter=container_session_bash,
            mcp_tool_adapter=mcp_tool_adapter,
            enable_mcp_adapters=app_config.developer.enable_mcp_adapters,
            enable_agent_live_logs=app_config.developer.enable_agent_live_logs,
            output_dir=developer_state_dir / "outputs",
        )
    role_tools = build_role_tools(context=developer_tool_context)

    runtime = bootstrap_runtime(
        app_config.runtime,
        role_tools=role_tools,
        developer_state_dir=developer_state_dir,
    )

    developer_execution = DeveloperExecutionEngine(
        filesystem_adapter=filesystem_adapter,
        bash_adapter=bash_adapter,
    )

    def _emit_developer_agent_live(event: str, **fields: Any) -> None:
        if not app_config.developer.enable_agent_live_logs:
            return

        timestamp = datetime.now(tz=UTC).isoformat()
        detail = " ".join(f"{key}={value!r}" for key, value in fields.items())
        print(f"[developer-agent-live] {timestamp} {event} {detail}".rstrip(), flush=True)

    def _pending_tool_names(requests: list[Any]) -> list[str]:
        names: list[str] = []
        for request in requests:
            function_call = getattr(request, "function_call", None)
            function_name = getattr(function_call, "name", None)
            names.append(function_name if isinstance(function_name, str) else "<unknown>")
        return names

    def _pending_tool_calls(requests: list[Any]) -> list[str]:
        calls: list[str] = []
        for request in requests:
            function_call = getattr(request, "function_call", None)
            function_name = getattr(function_call, "name", None)
            name = function_name if isinstance(function_name, str) else "<unknown>"

            arguments = getattr(function_call, "arguments", None)
            if isinstance(arguments, dict):
                args_preview = dumps(arguments, ensure_ascii=True)
            else:
                args_preview = str(arguments)
            args_preview = args_preview.replace("\n", "\\n")[:120]
            calls.append(f"{name}({args_preview})")
        return calls

    def _emit_patch_request_args(*, round_number: int, request: Any) -> None:
        function_call = getattr(request, "function_call", None)
        function_name = getattr(function_call, "name", None)
        if function_name != "developer_apply_patch":
            return

        raw_arguments = getattr(function_call, "arguments", None)
        parsed_arguments: Any = raw_arguments
        if isinstance(raw_arguments, str):
            try:
                parsed_arguments = loads(raw_arguments)
            except Exception:
                parsed_arguments = raw_arguments

        patch_text: str | None = None
        if isinstance(parsed_arguments, dict):
            patch_candidate = parsed_arguments.get("patch")
            if isinstance(patch_candidate, str):
                patch_text = patch_candidate
        elif isinstance(raw_arguments, str):
            patch_text = raw_arguments

        _emit_developer_agent_live(
            "approval.patch_args",
            round=round_number,
            patch_len=len(patch_text) if patch_text is not None else None,
            patch_text=patch_text,
            arguments=parsed_arguments,
        )

    def _metadata_with_runtime_hooks(result: Any, *, event_payload: dict[str, Any]) -> dict[str, Any] | None:
        metadata = dict(result.metadata or {})
        if result.route == "architect.proactive.scan":
            metadata["architect_scan"] = architect_scan_runner.run(event_payload).to_dict()

        if result.route == "meeting.bootstrap":
            meeting_id = metadata.get("meeting_id")
            if isinstance(meeting_id, str):
                record = dispatcher.meeting_registry.get(meeting_id)
                if record is not None:
                    kickoff = kickoff_meeting_bootstrap(runtime, meeting_record=record)
                    metadata["meeting_kickoff"] = {
                        "status": kickoff.status,
                        "detail": kickoff.detail,
                    }
        return metadata if metadata else None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_github_event: str = Header(alias="X-GitHub-Event"),
        x_github_delivery: str = Header(alias="X-GitHub-Delivery"),
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    ) -> dict[str, Any]:
        payload_bytes = await request.body()
        if not _is_valid_signature(
            secret=app_config.webhook_secret,
            payload=payload_bytes,
            provided=x_hub_signature_256,
        ):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        try:
            payload_obj = loads(payload_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        if not isinstance(payload_obj, dict):
            raise HTTPException(status_code=400, detail="Webhook payload must be an object")

        event = normalize_github_webhook(
            github_event=x_github_event,
            action=payload_obj.get("action"),
            delivery_id=x_github_delivery,
            body=payload_obj,
        )
        result = trigger_engine.dispatch(event, dispatcher=dispatcher)
        return {
            "accepted": result.accepted,
            "route": result.route,
            "reason": result.reason,
            "metadata": _metadata_with_runtime_hooks(result, event_payload=event.payload),
            "correlation_id": event.envelope.correlation_id,
        }

    @app.post("/internal/triggers")
    def internal_trigger(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        try:
            request = parse_trigger_request(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        event = make_internal_event(
            origin=request.origin,
            event_type=request.event_type,
            payload=request.payload,
            dedupe_key=request.dedupe_key,
            priority=request.priority,
        )
        result = trigger_engine.dispatch(event, dispatcher=dispatcher)
        return {
            "accepted": result.accepted,
            "route": result.route,
            "reason": result.reason,
            "metadata": _metadata_with_runtime_hooks(result, event_payload=event.payload),
            "correlation_id": event.envelope.correlation_id,
        }

    @app.post("/internal/scheduler/tick")
    def scheduler_tick(
        payload: dict[str, Any] | None = None,
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        body = payload or {}
        manual_scan = bool(body.get("manual_scan", False))
        manual_meeting = bool(body.get("manual_meeting", False))
        meeting_id = body.get("meeting_id")
        if meeting_id is not None and not isinstance(meeting_id, str):
            raise HTTPException(status_code=400, detail="meeting_id must be a string")
        current_proactive_jobs = int(body.get("current_proactive_jobs", 0))

        tick_result = scheduler.run_tick(
            manual_scan=manual_scan,
            manual_meeting=manual_meeting,
            meeting_id=meeting_id,
            current_proactive_jobs=current_proactive_jobs,
        )

        dispatched: list[dict[str, Any]] = []
        for event in tick_result.produced:
            result = trigger_engine.dispatch(event, dispatcher=dispatcher)
            dispatched.append(
                {
                    "accepted": result.accepted,
                    "route": result.route,
                    "reason": result.reason,
                    "metadata": _metadata_with_runtime_hooks(result, event_payload=event.payload),
                    "correlation_id": event.envelope.correlation_id,
                    "event_type": event.envelope.event_type.value,
                }
            )

        return {
            "produced_count": len(tick_result.produced),
            "dispatched": dispatched,
        }

    @app.post("/internal/developer/preview")
    def create_developer_preview(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)

        github_event_raw = payload.get("github_event")
        action_raw = payload.get("action")
        body_raw = payload.get("body")

        if not isinstance(github_event_raw, str) or not github_event_raw.strip():
            raise HTTPException(status_code=400, detail="github_event must be a non-empty string")
        if action_raw is not None and not isinstance(action_raw, str):
            raise HTTPException(status_code=400, detail="action must be a string when provided")
        if body_raw is None:
            body: dict[str, Any] = {}
        elif isinstance(body_raw, dict):
            body = body_raw
        else:
            raise HTTPException(status_code=400, detail="body must be an object when provided")

        dedupe_key_raw = payload.get("dedupe_key")
        delivery_id_raw = payload.get("delivery_id")
        if dedupe_key_raw is not None and not isinstance(dedupe_key_raw, str):
            raise HTTPException(status_code=400, detail="dedupe_key must be a string")
        if delivery_id_raw is not None and not isinstance(delivery_id_raw, str):
            raise HTTPException(status_code=400, detail="delivery_id must be a string")

        dedupe_key = (
            dedupe_key_raw
            or (f"github:{delivery_id_raw}" if delivery_id_raw else None)
            or f"preview:{uuid4()}"
        )

        preview = dispatcher.create_developer_preview(
            dedupe_key=dedupe_key,
            github_event=github_event_raw.strip(),
            action=(action_raw or "unknown").strip(),
            body=body,
        )

        return {
            "preview_id": preview.preview_id,
            "dedupe_key": preview.dedupe_key,
            "approved": preview.approved,
            "bundle": preview.bundle_payload,
        }

    @app.post("/internal/developer/preview/approve")
    def approve_developer_preview(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)

        preview_id_raw = payload.get("preview_id")
        if not isinstance(preview_id_raw, str) or not preview_id_raw.strip():
            raise HTTPException(status_code=400, detail="preview_id must be a non-empty string")

        approved = dispatcher.approve_developer_preview(preview_id_raw.strip())
        if approved is None:
            raise HTTPException(status_code=404, detail="preview_id not found")

        return {
            "preview_id": approved.preview_id,
            "dedupe_key": approved.dedupe_key,
            "approved": approved.approved,
            "approved_at": approved.approved_at.isoformat() if approved.approved_at else None,
        }

    @app.get("/internal/developer/previews")
    def list_developer_previews(
        pending_only: bool = Query(default=True),
        limit: int = Query(default=50, ge=1, le=500),
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)

        previews = dispatcher.list_developer_previews(
            pending_only=pending_only,
            limit=limit,
        )
        return {
            "count": len(previews),
            "pending_only": pending_only,
            "items": [
                {
                    "preview_id": preview.preview_id,
                    "dedupe_key": preview.dedupe_key,
                    "approved": preview.approved,
                    "created_at": preview.created_at.isoformat(),
                    "approved_at": preview.approved_at.isoformat() if preview.approved_at else None,
                    "bundle": preview.bundle_payload,
                    "source": preview.source_payload,
                }
                for preview in previews
            ],
        }

    @app.post("/internal/developer/session/start")
    def start_developer_session(
        payload: dict[str, Any] | None = None,
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        if container_session_bash is None:
            raise HTTPException(
                status_code=409,
                detail="Developer container_session mode is not enabled",
            )

        body = payload or {}
        session_id_raw = body.get("session_id")
        if session_id_raw is not None and not isinstance(session_id_raw, str):
            raise HTTPException(status_code=400, detail="session_id must be a string when provided")

        try:
            session_id, container_name = container_session_bash.create_session(session_id=session_id_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "session_id": session_id,
            "container_name": container_name,
            "execution_mode": app_config.developer.execution_mode,
            "data_volume": container_session_bash.data_volume_name,
            "workspace": str(container_session_bash.get_workspace_root(session_id)),
        }

    @app.post("/internal/developer/session/stop")
    def stop_developer_session(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        if container_session_bash is None:
            raise HTTPException(
                status_code=409,
                detail="Developer container_session mode is not enabled",
            )

        session_id_raw = payload.get("session_id")
        if not isinstance(session_id_raw, str) or not session_id_raw.strip():
            raise HTTPException(status_code=400, detail="session_id must be a non-empty string")

        try:
            closed = container_session_bash.close_session(session_id=session_id_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "session_id": session_id_raw.strip(),
            "closed": closed,
        }

    @app.post("/internal/developer/session/stop-all")
    def stop_all_developer_sessions(
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        if container_session_bash is None:
            raise HTTPException(
                status_code=409,
                detail="Developer container_session mode is not enabled",
            )

        closed_session_ids: list[str] = []
        failed_session_ids: list[str] = []
        for session_id in container_session_bash.list_session_ids():
            try:
                if container_session_bash.close_session(session_id=session_id):
                    closed_session_ids.append(session_id)
                else:
                    failed_session_ids.append(session_id)
            except Exception:
                failed_session_ids.append(session_id)

        return {
            "closed_count": len(closed_session_ids),
            "failed_count": len(failed_session_ids),
            "closed_session_ids": closed_session_ids,
            "failed_session_ids": failed_session_ids,
        }

    @app.post("/internal/developer/run")
    def run_developer_preview(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)

        preview_id_raw = payload.get("preview_id")
        if not isinstance(preview_id_raw, str) or not preview_id_raw.strip():
            raise HTTPException(status_code=400, detail="preview_id must be a non-empty string")
        preview_id = preview_id_raw.strip()

        preview = dispatcher.developer_preview_registry.get(preview_id)
        if preview is None:
            raise HTTPException(status_code=404, detail="preview_id not found")
        if not preview.approved:
            raise HTTPException(status_code=409, detail="preview_id must be approved before execution")

        try:
            bundle = developer_task_bundle_from_payload(preview.bundle_payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid preview bundle payload: {exc}") from exc

        commands_raw = payload.get("commands", [])
        if not isinstance(commands_raw, list) or not all(isinstance(item, str) for item in commands_raw):
            raise HTTPException(status_code=400, detail="commands must be a list of strings")
        commands = tuple(item.strip() for item in commands_raw if item.strip())

        writes_raw = payload.get("file_writes", [])
        if not isinstance(writes_raw, list):
            raise HTTPException(status_code=400, detail="file_writes must be a list")

        planned_writes: list[PlannedFileWrite] = []
        for entry in writes_raw:
            if not isinstance(entry, dict):
                raise HTTPException(status_code=400, detail="file_writes entries must be objects")
            path_raw = entry.get("path")
            content_raw = entry.get("content")
            if not isinstance(path_raw, str) or not path_raw.strip():
                raise HTTPException(status_code=400, detail="file_writes.path must be a non-empty string")
            if not isinstance(content_raw, str):
                raise HTTPException(status_code=400, detail="file_writes.content must be a string")
            planned_writes.append(
                PlannedFileWrite(path=path_raw, content=content_raw)
            )

        dry_run = bool(payload.get("dry_run", True))
        approved = bool(payload.get("approved", False))

        session_id_raw = payload.get("session_id")
        if session_id_raw is not None and not isinstance(session_id_raw, str):
            raise HTTPException(status_code=400, detail="session_id must be a string when provided")
        session_id = session_id_raw.strip() if isinstance(session_id_raw, str) else None

        if app_config.developer.execution_mode == "container_session" and not session_id:
            raise HTTPException(
                status_code=400,
                detail="session_id is required when execution mode is container_session",
            )

        result = developer_execution.execute(
            bundle=bundle,
            commands=commands,
            file_writes=tuple(planned_writes),
            workspace_root=(
                container_session_bash.get_workspace_root(session_id)
                if container_session_bash is not None and session_id else workspace_root
            ),
            dry_run=dry_run,
            approved=approved,
            require_human_approval_for_repo_writes=app_config.policy.require_human_approval_for_repo_writes,
            session_id=session_id,
        )

        return {
            "preview_id": preview.preview_id,
            "session_id": session_id,
            "accepted": result.accepted,
            "reason": result.reason,
            "dry_run": dry_run,
            "command_outcomes": [
                {
                    "command": outcome.command,
                    "executed": outcome.executed,
                    "exit_code": outcome.exit_code,
                    "stdout": outcome.stdout,
                    "stderr": outcome.stderr,
                    "rejected_reason": outcome.rejected_reason,
                }
                for outcome in result.command_outcomes
            ],
            "file_write_outcomes": [
                {
                    "path": outcome.path,
                    "executed": outcome.executed,
                    "rejected_reason": outcome.rejected_reason,
                }
                for outcome in result.file_write_outcomes
            ],
        }

    @app.get("/internal/runtime/developer-agent")
    def developer_agent_status(
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)

        developer_handle = runtime.role_agents.get("developer")
        is_native_handle = developer_handle is not None and not isinstance(developer_handle, dict)
        can_run = is_native_handle and callable(getattr(developer_handle, "run", None))
        supports_sessions = is_native_handle and callable(getattr(developer_handle, "create_session", None))

        configured_tools = runtime.role_tools.get("developer", ())
        tool_names = [
            _tool_name(tool_func)
            for tool_func in configured_tools
        ]

        return {
            "runtime_mode": runtime.mode,
            "developer_handle_kind": "native" if is_native_handle else "descriptor",
            "ready_for_run": bool(can_run),
            "supports_sessions": bool(supports_sessions),
            "tool_count": len(configured_tools),
            "tool_names": tool_names,
            "persistent_memory": bool(can_run),
            "state_directory": str(developer_state_dir),
            "data_volume": container_session_bash.data_volume_name if container_session_bash else None,
            "browser_enabled": app_config.developer.enable_browser,
        }

    async def _run_developer_agent(
        payload: dict[str, Any],
        x_internal_token: str | None,
        tool_stack: AsyncExitStack,
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)

        developer_handle = runtime.role_agents.get("developer")
        if developer_handle is None:
            raise HTTPException(status_code=500, detail="Developer agent is not registered in runtime")
        if isinstance(developer_handle, dict):
            raise HTTPException(
                status_code=409,
                detail="Developer agent is in descriptor mode; native runtime binding is required",
            )
        if not callable(getattr(developer_handle, "run", None)):
            raise HTTPException(
                status_code=409,
                detail="Developer agent does not provide a callable run(...) method",
            )

        input_raw = payload.get("input")
        if not isinstance(input_raw, str) or not input_raw.strip():
            raise HTTPException(status_code=400, detail="input must be a non-empty string")
        if len(input_raw.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise HTTPException(status_code=413, detail=(
                f"input exceeds {MAX_PROMPT_BYTES} UTF-8 bytes; put large content in workspace files "
                "and ask the Developer to read only the relevant section"
            ))
        user_prompt = input_raw.strip()
        prompt = user_prompt

        auto_approve_tools = bool(payload.get("auto_approve_tools", False))
        max_approval_rounds_raw = payload.get("max_approval_rounds", 3)
        if not isinstance(max_approval_rounds_raw, int):
            raise HTTPException(status_code=400, detail="max_approval_rounds must be an integer")
        if max_approval_rounds_raw < 1 or max_approval_rounds_raw > 20:
            raise HTTPException(status_code=400, detail="max_approval_rounds must be within 1..20")
        max_approval_rounds = max_approval_rounds_raw
        invoke_timeout_seconds = app_config.developer.agent_invoke_timeout_seconds
        include_tool_trace = payload.get("include_tool_trace", False) is True
        tool_trace: list[dict[str, Any]] = []
        usage: dict[str, int] = {}

        @function_middleware
        async def trace_tool(
            context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]],
        ) -> None:
            started = perf_counter()
            entry: dict[str, Any] = {
                "name": context.function.name,
                "arguments": dict(context.arguments),
            }
            try:
                await call_next()
                result_contents = context.result
                if getattr(result_contents, "type", None) == "function_approval_request":
                    return
                if isinstance(result_contents, list):
                    if any(getattr(item, "type", None) == "function_approval_request" for item in result_contents):
                        return
                    text = "\n".join(getattr(item, "text", "") or "" for item in result_contents)
                else:
                    text = str(result_contents)
                try:
                    structured = loads(text)
                except ValueError:
                    structured = None
                entry["ok"] = not (isinstance(structured, dict) and (
                    structured.get("ok") is False or structured.get("isError") is True
                ))
                entry["result"] = structured if isinstance(structured, dict) else text[:6000]
            except Exception as error:
                entry.update(ok=False, error=str(error)[:2000])
                raise
            finally:
                if "ok" in entry:
                    entry["duration_ms"] = int((perf_counter() - started) * 1000)
                    tool_trace.append(entry)

        run_middleware = [trace_tool] if include_tool_trace else []

        def record_usage(response: Any) -> None:
            for key, value in (getattr(response, "usage_details", None) or {}).items():
                if isinstance(value, int):
                    usage[key] = usage.get(key, 0) + value

        session_id_raw = payload.get("session_id")
        if session_id_raw is not None and not isinstance(session_id_raw, str):
            raise HTTPException(status_code=400, detail="session_id must be a string when provided")
        session_id = session_id_raw.strip() if isinstance(session_id_raw, str) else None

        create_session_requested = True
        if session_id:
            try:
                _normalize_session_id(session_id)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        _emit_developer_agent_live(
            "run.start",
            prompt_chars=len(prompt),
            user_prompt_chars=len(user_prompt),
            auto_approve_tools=auto_approve_tools,
            max_approval_rounds=max_approval_rounds,
            create_session_requested=create_session_requested,
            provided_session_id=session_id,
        )
        session_obj: Any | None = developer_sessions.get(session_id) if session_id else None
        resolved_session_id: str | None = None
        if create_session_requested:
            try:
                if session_obj is None and session_id:
                    session_obj = await developer_session_store.get(session_id)
                if session_obj is None:
                    session_obj, resolved_session_id = _create_agent_session(
                        developer_handle,
                        session_id=session_id,
                    )
                else:
                    resolved_session_id = session_obj.session_id
                if resolved_session_id:
                    developer_sessions[resolved_session_id] = session_obj
                _emit_developer_agent_live(
                    "session.ready",
                    resolved_session_id=resolved_session_id,
                )
            except Exception as exc:
                _emit_developer_agent_live("session.error", error=str(exc))
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        scoped_tools: tuple[Any, ...] = build_role_tools(
            context=replace(developer_tool_context, bound_session_id=resolved_session_id)
        )["developer"]
        if resolved_session_id is None:
            raise HTTPException(status_code=409, detail="Developer run requires a session identity")
        run_middleware.append(build_output_guard(developer_state_dir / "outputs" / resolved_session_id))
        if resolved_session_id in active_developer_runs:
            raise HTTPException(status_code=409, detail="This Developer session already has an active run")
        active_developer_runs.add(resolved_session_id)
        tool_stack.callback(active_developer_runs.discard, resolved_session_id)
        if app_config.developer.enable_browser and payload.get("use_browser", True) is not False:
            if container_session_bash is None or resolved_session_id is None:
                raise HTTPException(status_code=409, detail="Browser requires a Developer container session")
            await asyncio.to_thread(
                container_session_bash.create_session, session_id=resolved_session_id
            )
            browser_tool = build_browser_tool(
                container_session_adapter=container_session_bash,
                session_id=resolved_session_id,
                request_timeout_seconds=app_config.developer.command_timeout_seconds,
            )
            await tool_stack.enter_async_context(browser_tool)
            scoped_tools = (*scoped_tools, browser_tool)

        try:
            invoke_started = perf_counter()
            _emit_developer_agent_live(
                "invoke.start",
                round=0,
                message_type="prompt",
                session_id=resolved_session_id,
                timeout_seconds=invoke_timeout_seconds,
            )
            result = await asyncio.wait_for(
                _invoke_agent_run(
                    agent_handle=developer_handle,
                    message=prompt,
                    session=session_obj,
                    tools=scoped_tools,
                    middleware=run_middleware,
                ),
                timeout=invoke_timeout_seconds,
            )
            first_pending = _extract_user_input_requests(result)
            record_usage(result)
            if isinstance(session_obj, AgentSession) and resolved_session_id:
                await developer_session_store.set(resolved_session_id, session_obj)
            _emit_developer_agent_live(
                "invoke.end",
                round=0,
                duration_ms=int((perf_counter() - invoke_started) * 1000),
                pending_count=len(first_pending),
                pending_tools=",".join(_pending_tool_names(first_pending)),
                pending_calls=";".join(_pending_tool_calls(first_pending)),
                output_preview=_extract_response_text(result)[:160].replace("\n", "\\n"),
            )
        except asyncio.TimeoutError as exc:
            _emit_developer_agent_live(
                "invoke.timeout",
                round=0,
                timeout_seconds=invoke_timeout_seconds,
            )
            raise HTTPException(
                status_code=504,
                detail=(
                    "Developer agent run timed out while awaiting model/framework response "
                    f"after {invoke_timeout_seconds} seconds"
                ),
            ) from exc
        except Exception as exc:
            _emit_developer_agent_live("invoke.error", round=0, error=str(exc))
            raise HTTPException(status_code=500, detail=f"Developer agent run failed: {exc}") from exc

        approval_rounds = 0
        approved_request_count = 0
        while auto_approve_tools and approval_rounds < max_approval_rounds:
            pending_requests = _extract_user_input_requests(result)
            if not pending_requests:
                break

            next_request = pending_requests[0]
            to_approval_response = getattr(next_request, "to_function_approval_response", None)
            if not callable(to_approval_response):
                raise HTTPException(
                    status_code=500,
                    detail="Developer agent approval request cannot be auto-approved",
                )

            approval_rounds += 1
            approved_request_count += 1
            _emit_patch_request_args(round_number=approval_rounds, request=next_request)
            _emit_developer_agent_live(
                "approval.auto",
                round=approval_rounds,
                function_name=_pending_tool_names([next_request])[0],
            )
            approval_response = to_approval_response(approved=True)
            approval_replay_message = _build_approval_replay_message(approval_response)

            try:
                invoke_started = perf_counter()
                _emit_developer_agent_live(
                    "invoke.start",
                    round=approval_rounds,
                    message_type="approval_response",
                    session_id=resolved_session_id,
                    timeout_seconds=invoke_timeout_seconds,
                    replay_type=type(approval_replay_message).__name__,
                )
                result = await asyncio.wait_for(
                    _invoke_agent_run(
                        agent_handle=developer_handle,
                        message=approval_replay_message,
                        session=session_obj,
                        tools=scoped_tools,
                        middleware=run_middleware,
                    ),
                    timeout=invoke_timeout_seconds,
                )
                round_pending = _extract_user_input_requests(result)
                record_usage(result)
                if isinstance(session_obj, AgentSession) and resolved_session_id:
                    await developer_session_store.set(resolved_session_id, session_obj)
                _emit_developer_agent_live(
                    "invoke.end",
                    round=approval_rounds,
                    duration_ms=int((perf_counter() - invoke_started) * 1000),
                    pending_count=len(round_pending),
                    pending_tools=",".join(_pending_tool_names(round_pending)),
                    pending_calls=";".join(_pending_tool_calls(round_pending)),
                    output_preview=_extract_response_text(result)[:160].replace("\n", "\\n"),
                )
            except asyncio.TimeoutError as exc:
                _emit_developer_agent_live(
                    "invoke.timeout",
                    round=approval_rounds,
                    timeout_seconds=invoke_timeout_seconds,
                )
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Developer agent approval replay timed out while awaiting "
                        f"model/framework response after {invoke_timeout_seconds} seconds"
                    ),
                ) from exc
            except Exception as exc:
                _emit_developer_agent_live(
                    "invoke.error",
                    round=approval_rounds,
                    error=str(exc),
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Developer agent approval replay failed: {exc}",
                ) from exc

        pending_requests = _extract_user_input_requests(result)
        response_id = getattr(result, "response_id", None)
        _emit_developer_agent_live(
            "run.finish",
            completed=len(pending_requests) == 0,
            approval_rounds=approval_rounds,
            auto_approved_count=approved_request_count,
            pending_count=len(pending_requests),
            pending_tools=",".join(_pending_tool_names(pending_requests)),
            pending_calls=";".join(_pending_tool_calls(pending_requests)),
            response_id=response_id if isinstance(response_id, str) else None,
        )
        return {
            "runtime_mode": runtime.mode,
            "session_id": resolved_session_id,
            "input": prompt,
            "output_text": _extract_response_text(result),
            "response_id": response_id if isinstance(response_id, str) else None,
            "auto_approve_tools": auto_approve_tools,
            "approval_rounds": approval_rounds,
            "auto_approved_count": approved_request_count,
            "approval_round_limit_reached": bool(
                auto_approve_tools and pending_requests and approval_rounds >= max_approval_rounds
            ),
            "pending_approval_count": len(pending_requests),
            "pending_approval_requests": [
                _serialize_user_input_request(request)
                for request in pending_requests
            ],
            "completed": len(pending_requests) == 0,
            **({"tool_trace": tool_trace, "usage": usage} if include_tool_trace else {}),
        }

    @app.post("/internal/developer/agent/run")
    async def run_developer_agent(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        async with AsyncExitStack() as tool_stack:
            return await _run_developer_agent(payload, x_internal_token, tool_stack)

    @app.get("/internal/escalations")
    def list_escalations(
        limit: int = Query(default=50, ge=1, le=500),
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        events = dispatcher.escalation_router.list_events()
        recent = events[-limit:]
        return {
            "count": len(recent),
            "events": [
                {
                    "escalation_id": event.escalation_id,
                    "created_at": event.created_at.isoformat(),
                    "source": event.source,
                    "category": event.category.value,
                    "severity": event.severity.value,
                    "summary": event.summary,
                    "details": event.details,
                    "destinations": [destination.value for destination in event.destinations],
                }
                for event in recent
            ],
        }

    return app
