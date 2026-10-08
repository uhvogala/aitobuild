"""FastAPI ingress for webhook and internal trigger events."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
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
from filelock import Timeout as FileLockTimeout

from agent_framework import AgentSession, Content, FileSessionStore, FunctionInvocationContext, function_middleware
from fastapi import FastAPI, Header, HTTPException, Query, Request

from aitobuild.agent_tools import DeveloperToolContext, build_role_tools
from aitobuild.config import AppConfig, load_config
from aitobuild.developer_delivery import DeveloperDeliveryWorker, LocalRepositorySource
from aitobuild.developer_execution import DeveloperExecutionEngine, PlannedFileWrite
from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.developer_preview import DeveloperPreviewRegistry
from aitobuild.dispatcher import DispatcherAgent
from aitobuild.events import make_internal_event, normalize_github_webhook, parse_trigger_request
from aitobuild.organization_assignments import AssignmentProposal
from aitobuild.organization_delivery import _delivery_call
from aitobuild.organization_service import ManagedOrganizationService, ManagedServiceContext
from aitobuild.organization_worker import ManagedOrganizationWorker, WorkerJob
from aitobuild.proactive import ArchitectScanRunner
from aitobuild.runtime import bootstrap_runtime, kickoff_meeting_bootstrap
from aitobuild.scheduler import scheduler_from_config
from aitobuild.pm_tools import IssueWriteApprovalStore, PlanDraftStore
from aitobuild.tools import (
    ArchitectMemoryStore,
    BashAdapter,
    ContainerSessionBashAdapter,
    FilesystemAdapter,
    MCPBashAdapter,
    MCPDeveloperToolAdapter,
    MCPFilesystemAdapter,
    MockBashAdapter,
    MockFilesystemAdapter,
    SubprocessBashAdapter,
    build_github_adapter,
    MockGitHubAdapter,
    build_web_search_adapter,
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
    request_id = getattr(request, "request_id", None) or getattr(request, "id", None)

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


def create_app(
    config: AppConfig | None = None, *,
    managed_service_factory: Callable[[ManagedServiceContext], ManagedOrganizationService] | None = None,
    managed_worker_factory: Callable[[ManagedOrganizationService, ManagedServiceContext], ManagedOrganizationWorker] | None = None,
) -> FastAPI:
    app_config = config or load_config()
    if managed_worker_factory is not None and managed_service_factory is None:
        raise ValueError("Managed worker requires an explicit managed service factory")
    if managed_service_factory is not None and (
        not app_config.security.require_internal_auth or not app_config.security.internal_api_token
    ):
        raise ValueError("Managed organization service requires authenticated internal controls")
    if app_config.developer.enable_browser and app_config.developer.execution_mode != "container_session":
        raise ValueError("Developer browser requires container_session mode")

    app = FastAPI(title="aitobuild")
    workspace_root = Path.cwd()
    developer_state_dir = (workspace_root / app_config.developer.state_dir).resolve()
    trigger_engine = TriggerEngine(dedupe_store=InMemoryDedupeStore())
    dispatcher = DispatcherAgent(
        require_developer_preview=app_config.developer.require_preview_before_dispatch,
        developer_preview_registry=DeveloperPreviewRegistry(developer_state_dir / "previews.json"),
    )
    delivery_worker = DeveloperDeliveryWorker(
        preview_registry=dispatcher.developer_preview_registry, state_dir=developer_state_dir,
        service_root=workspace_root, command_timeout_seconds=app_config.developer.command_timeout_seconds,
        repository_sources=tuple(
            LocalRepositorySource(source.repository, source.repository_id, Path(source.path), source.verification_commands)
            for source in app_config.developer.repository_sources
        ),
    )
    scheduler = scheduler_from_config(app_config.scheduler)
    architect_scan_runner = ArchitectScanRunner()
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

    github_adapter = build_github_adapter(
        mode=app_config.github.adapter,
        default_repository=app_config.github.default_repository,
        allowed_repositories=app_config.github.allowed_repositories,
    )
    web_search_adapter = build_web_search_adapter(mode=app_config.web_search.adapter)
    architect_memory = ArchitectMemoryStore(developer_state_dir / "architect_memory.jsonl")
    plan_draft_store = PlanDraftStore()
    issue_write_store = IssueWriteApprovalStore()
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
            github_adapter=github_adapter,
            meeting_registry=dispatcher.meeting_registry,
            web_search_adapter=web_search_adapter,
            architect_memory=architect_memory,
            plan_draft_store=plan_draft_store,
            issue_write_store=issue_write_store,
            default_repository=app_config.github.default_repository,
            delivery_worker=delivery_worker,
        )
    role_tools = build_role_tools(context=developer_tool_context)
    managed_context = ManagedServiceContext(
        previews=dispatcher.developer_preview_registry, worker=delivery_worker,
        tools=developer_tool_context, state_dir=developer_state_dir,
    )
    managed_service = managed_service_factory(managed_context) if managed_service_factory is not None else None
    managed_worker = managed_worker_factory(managed_service, managed_context) if (
        managed_service is not None and managed_worker_factory is not None
    ) else None
    if managed_worker is not None:
        if managed_worker.service is not managed_service:
            raise ValueError("Managed worker must use the app's managed service")

        @asynccontextmanager
        async def managed_lifespan(application: FastAPI):
            assert managed_worker is not None
            await managed_worker.start()
            try:
                yield
            finally:
                await managed_worker.close()

        app.router.lifespan_context = managed_lifespan

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

    def _worker_metadata(job: WorkerJob) -> dict[str, Any]:
        assert managed_service is not None
        run = managed_service.recorded_run(job.task_id)
        return {"managed_admission": job.model_dump(mode="json"),
                "managed_run": run.model_dump(mode="json") if run is not None else None}

    async def _consume_managed(preview_id: str) -> dict[str, Any] | None:
        if managed_service is None:
            return None
        try:
            if managed_worker is not None:
                job = managed_worker.enqueue(preview_id)
                return _worker_metadata(job) if job is not None else None
            run = await managed_service.consume(preview_id)
        except FileLockTimeout as error:
            raise HTTPException(status_code=409, detail="Managed task invocation is already active") from error
        except (ValueError, PermissionError, RuntimeError, OSError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"managed_run": run.model_dump(mode="json")} if run is not None else None

    async def _dispatch_metadata(result: Any, *, event_payload: dict[str, Any]) -> dict[str, Any] | None:
        metadata = _metadata_with_runtime_hooks(result, event_payload=event_payload)
        if metadata is not None and result.route in {"developer.async.webhook", "dedupe"}:
            preview_id = metadata.get("preview_id")
            if isinstance(preview_id, str):
                run = await _consume_managed(preview_id)
                if run is not None:
                    metadata.update(run)
        return metadata

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
            "metadata": await _dispatch_metadata(result, event_payload=event.payload),
            "correlation_id": event.envelope.correlation_id,
        }

    @app.post("/internal/triggers")
    async def internal_trigger(
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
            "metadata": await _dispatch_metadata(result, event_payload=event.payload),
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

        if "task_bundle" in payload:
            if "repository" in body:
                raise HTTPException(status_code=400, detail="Repository issue previews must use extracted issue scope")
            raw_bundle = payload["task_bundle"]
            if not isinstance(raw_bundle, dict) or len(json.dumps(raw_bundle).encode("utf-8")) > MAX_PROMPT_BYTES:
                raise HTTPException(status_code=400, detail="task_bundle must be an object within the prompt byte limit")
            try:
                explicit_bundle = developer_task_bundle_from_payload(raw_bundle)
                if explicit_bundle.issue_context is not None:
                    raise ValueError("Repository issue previews must use extracted issue scope")
                preview = dispatcher.developer_preview_registry.create_or_get(
                    dedupe_key=dedupe_key, bundle_payload=explicit_bundle.to_payload(), source_payload=body,
                )
            except (ValueError, TypeError) as error:
                raise HTTPException(status_code=400, detail=f"Invalid task bundle: {error}") from error
        else:
            try:
                preview = dispatcher.create_developer_preview(
                    dedupe_key=dedupe_key,
                    github_event=github_event_raw.strip(),
                    action=(action_raw or "unknown").strip(),
                    body=body,
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error

        return {
            "preview_id": preview.preview_id,
            "dedupe_key": preview.dedupe_key,
            "approved": preview.approved,
            "task_state": preview.state,
            "bundle": preview.bundle_payload,
        }

    @app.post("/internal/developer/preview/approve")
    async def approve_developer_preview(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)

        preview_id_raw = payload.get("preview_id")
        if not isinstance(preview_id_raw, str) or not preview_id_raw.strip():
            raise HTTPException(status_code=400, detail="preview_id must be a non-empty string")

        base_revision = payload.get("base_revision")
        if base_revision is not None and (not isinstance(base_revision, str) or not base_revision):
            raise HTTPException(status_code=400, detail="base_revision must be a non-empty commit SHA")
        try:
            approved = dispatcher.approve_developer_preview(preview_id_raw.strip(), base_revision=base_revision)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if approved is None:
            raise HTTPException(status_code=404, detail="preview_id not found")

        response = {
            "preview_id": approved.preview_id,
            "dedupe_key": approved.dedupe_key,
            "approved": approved.approved,
            "task_state": approved.state,
            "bundle": approved.bundle_payload,
            "approved_at": approved.approved_at.isoformat() if approved.approved_at else None,
        }
        run = await _consume_managed(approved.preview_id)
        if run is not None:
            response.update(run)
        return response

    if managed_worker is not None:
        @app.get("/internal/organization/worker")
        def managed_worker_status(
            x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
        ) -> dict[str, object]:
            _assert_internal_auth(config=app_config, provided_token=x_internal_token)
            assert managed_worker is not None
            return managed_worker.diagnostics()

    if managed_service is not None:
        @app.post("/internal/organization/corrections/offer")
        async def offer_scoped_correction(
            payload: dict[str, Any],
            x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
        ) -> dict[str, Any]:
            _assert_internal_auth(config=app_config, provided_token=x_internal_token)
            if set(payload) != {"review_preview_id"} or not isinstance(payload["review_preview_id"], str) or not payload["review_preview_id"].strip():
                raise HTTPException(status_code=400, detail="Correction staging accepts only review_preview_id")
            assert managed_service is not None
            try:
                preview = await managed_service.offer_correction(payload["review_preview_id"].strip())
            except (ValueError, OSError, PermissionError, RuntimeError, FileLockTimeout) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return {"correction_preview": {"preview_id": preview.preview_id, "approved": preview.approved, "bundle": preview.bundle_payload} if preview else None}

        @app.post("/internal/organization/corrections/stage-publication")
        async def stage_correction_publication(
            payload: dict[str, Any],
            x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
        ) -> dict[str, Any]:
            _assert_internal_auth(config=app_config, provided_token=x_internal_token)
            if (set(payload) != {"correction_preview_id"} or not isinstance(payload["correction_preview_id"], str)
                    or not payload["correction_preview_id"].strip()):
                raise HTTPException(status_code=400, detail="Correction publication staging accepts only correction_preview_id")
            assert managed_service is not None
            try:
                staged = await managed_service.stage_correction_publication(payload["correction_preview_id"].strip())
            except (ValueError, OSError, PermissionError, RuntimeError, FileLockTimeout) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return {"publish_approval": staged}

        @app.post("/internal/organization/corrections/publish")
        async def publish_scoped_correction(
            payload: dict[str, Any],
            x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
        ) -> dict[str, Any]:
            _assert_internal_auth(config=app_config, provided_token=x_internal_token)
            if (set(payload) != {"correction_preview_id", "approval_digest"}
                    or not all(isinstance(payload[key], str) and payload[key].strip() for key in payload)):
                raise HTTPException(status_code=400, detail="Correction publish accepts only correction_preview_id and approval_digest")
            if isinstance(github_adapter, MockGitHubAdapter):
                raise HTTPException(status_code=409, detail="Publication requires a live GitHub adapter (gh_cli); mock publication is refused")
            assert managed_service is not None
            try:
                record, review = await managed_service.publish_correction(
                    payload["correction_preview_id"].strip(), approval_digest=payload["approval_digest"].strip(),
                    github=github_adapter,
                )
            except (ValueError, OSError, PermissionError, RuntimeError, FileLockTimeout) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return {"accepted": record.state == "published", "delivery": record.to_payload(),
                    "review_preview": {"preview_id": review.preview_id, "approved": review.approved,
                                       "bundle": review.bundle_payload} if review else None}

        @app.post("/internal/organization/corrections/retire")
        async def retire_scoped_correction(
            payload: dict[str, Any],
            x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
        ) -> dict[str, Any]:
            _assert_internal_auth(config=app_config, provided_token=x_internal_token)
            if (set(payload) != {"correction_preview_id"} or not isinstance(payload["correction_preview_id"], str)
                    or not payload["correction_preview_id"].strip()):
                raise HTTPException(status_code=400, detail="Correction retire accepts only correction_preview_id")
            if isinstance(github_adapter, MockGitHubAdapter):
                raise HTTPException(status_code=409, detail="Retiring requires a live GitHub adapter (gh_cli); mock reads are refused")
            assert managed_service is not None
            try:
                record = await managed_service.retire_correction(payload["correction_preview_id"].strip(), github=github_adapter)
            except (ValueError, OSError, PermissionError, RuntimeError, FileLockTimeout) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return {"retired": record.state == "retired", "delivery": record.to_payload()}

        @app.post("/internal/organization/reviews/offer")
        async def offer_published_review(
            payload: dict[str, Any],
            x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
        ) -> dict[str, Any]:
            _assert_internal_auth(config=app_config, provided_token=x_internal_token)
            if set(payload) != {"published_preview_id"} or not isinstance(payload["published_preview_id"], str) or not payload["published_preview_id"].strip():
                raise HTTPException(status_code=400, detail="Review staging accepts only published_preview_id")
            assert managed_service is not None
            try:
                preview = await managed_service.offer_published_review(payload["published_preview_id"].strip())
            except (ValueError, OSError, PermissionError, RuntimeError, FileLockTimeout) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return {"review_preview": {"preview_id": preview.preview_id, "approved": preview.approved, "bundle": preview.bundle_payload} if preview else None}

        @app.get("/internal/organization/tasks/{preview_id}")
        def managed_task_status(
            preview_id: str,
            x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
        ) -> dict[str, Any]:
            _assert_internal_auth(config=app_config, provided_token=x_internal_token)
            assert managed_service is not None
            try:
                if managed_worker is not None:
                    job = managed_worker.store.get(preview_id)
                    if job is not None:
                        return _worker_metadata(job)
                run = managed_service.status(preview_id)
            except ValueError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except (PermissionError, OSError) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return {"managed_run": run.model_dump(mode="json") if run is not None else None}

        @app.post("/internal/organization/tasks/{operation}")
        async def managed_task_control(
            operation: str, payload: dict[str, Any],
            x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
        ) -> dict[str, Any]:
            _assert_internal_auth(config=app_config, provided_token=x_internal_token)
            assert managed_service is not None
            fields = {
                "run": {"preview_id", "proposal"}, "approve": {"assignment_id", "request_id", "approved"},
                "resume": {"assignment_id", "request_id", "response"},
                "cancel": {"assignment_id", "preview_id"} if managed_worker is not None else {"assignment_id"},
            }.get(operation)
            if fields is None:
                raise HTTPException(status_code=404, detail="Unknown managed task control")
            if set(payload) - fields:
                raise HTTPException(status_code=400, detail="Unexpected managed task fields")
            required = fields - {"proposal"}
            if operation == "cancel" and managed_worker is not None:
                if set(payload) not in ({"assignment_id"}, {"preview_id"}):
                    raise HTTPException(status_code=400, detail="Cancellation requires one task identity")
                required = set(payload)
            if not required <= set(payload) or any(
                not isinstance(payload[name], str) or not payload[name].strip()
                for name in required - {"approved", "response"}
            ):
                raise HTTPException(status_code=400, detail="Missing or invalid managed task identity")
            if operation == "approve" and type(payload["approved"]) is not bool:
                raise HTTPException(status_code=400, detail="approved must be a Boolean")
            try:
                if managed_worker is not None:
                    if operation == "run":
                        proposal = AssignmentProposal.model_validate(payload["proposal"]) if "proposal" in payload else None
                        job = managed_worker.enqueue(payload["preview_id"], proposal=proposal)
                        if job is None:
                            raise ValueError("Task requires an activated approved repository route")
                    elif operation in {"approve", "resume"}:
                        job = managed_worker.enqueue_decision(
                            payload["assignment_id"], request_id=payload["request_id"],
                            kind="approve" if operation == "approve" else "resume",
                            value=payload["approved"] if operation == "approve" else payload["response"],
                        )
                    else:
                        preview_id = payload.get("preview_id") or managed_service.assignment_preview(payload["assignment_id"])
                        job = await managed_worker.cancel(preview_id)
                    return _worker_metadata(job)
                if operation == "run":
                    proposal = AssignmentProposal.model_validate(payload["proposal"]) if "proposal" in payload else None
                    run = await managed_service.consume(payload["preview_id"], proposal=proposal)
                    if run is None:
                        raise ValueError("Task requires an activated approved repository route and delegation decision")
                elif operation == "approve":
                    run = await managed_service.decide(payload["assignment_id"], request_id=payload["request_id"],
                                                       approved=payload["approved"])
                elif operation == "resume":
                    run = await managed_service.respond(payload["assignment_id"], request_id=payload["request_id"],
                                                        response=payload["response"])
                else:
                    run = await managed_service.cancel(payload["assignment_id"])
            except FileLockTimeout as error:
                raise HTTPException(status_code=409, detail="Managed task invocation is already active") from error
            except (ValueError, PermissionError, RuntimeError, OSError) as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return {"managed_run": run.model_dump(mode="json")}

    @app.get("/internal/pm/plans")
    def list_pm_plans(
        pending_only: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=500),
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        drafts = plan_draft_store.list_drafts(pending_only=pending_only, limit=limit)
        return {"plans": drafts, "plan_count": len(drafts)}

    @app.post("/internal/pm/plan/approve")
    def approve_pm_plan(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        draft_id_raw = payload.get("draft_id")
        if not isinstance(draft_id_raw, str) or not draft_id_raw.strip():
            raise HTTPException(status_code=400, detail="draft_id must be a non-empty string")
        approval_request_id = payload.get("approval_request_id")
        if approval_request_id is not None and (
            not isinstance(approval_request_id, str) or not approval_request_id.strip()
        ):
            raise HTTPException(status_code=400, detail="approval_request_id must be a non-empty string")
        try:
            approved = plan_draft_store.mark_approved(
                draft_id=draft_id_raw.strip(),
                approval_request_id=(
                    approval_request_id.strip() if isinstance(approval_request_id, str) else None
                ),
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"approved": True, "plan": approved}

    @app.get("/internal/pm/issue-writes")
    def list_pm_issue_writes(
        pending_only: bool = Query(default=True),
        limit: int = Query(default=50, ge=1, le=500),
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        requests = issue_write_store.list_requests(pending_only=pending_only, limit=limit)
        return {"issue_writes": requests, "issue_write_count": len(requests)}

    @app.post("/internal/pm/issue-write/approve")
    def approve_pm_issue_write(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        request_id_raw = payload.get("approval_request_id")
        if not isinstance(request_id_raw, str) or not request_id_raw.strip():
            raise HTTPException(status_code=400, detail="approval_request_id must be a non-empty string")
        try:
            approved = issue_write_store.mark_approved(approval_request_id=request_id_raw.strip())
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"approved": True, "issue_write": approved}

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
                    "task_state": preview.state,
                    "created_at": preview.created_at.isoformat(),
                    "approved_at": preview.approved_at.isoformat() if preview.approved_at else None,
                    "dispatched_at": preview.dispatched_at.isoformat() if preview.dispatched_at else None,
                    "bundle": preview.bundle_payload,
                    "source": preview.source_payload,
                }
                for preview in previews
            ],
        }

    @app.post("/internal/developer/delivery/prepare")
    def prepare_developer_delivery(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        if set(payload) != {"preview_id"}:
            raise HTTPException(status_code=400, detail="Delivery preparation accepts only preview_id; configure sources on the server")
        preview_id = payload.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id.strip():
            raise HTTPException(status_code=400, detail="preview_id must be a non-empty string")
        preview_id = preview_id.strip()
        if dispatcher.developer_preview_registry.get(preview_id) is None:
            raise HTTPException(status_code=404, detail="preview_id not found")
        try:
            record = delivery_worker.prepare(preview_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"accepted": record.state == "prepared", "delivery": record.to_payload()}

    @app.post("/internal/developer/delivery/verify")
    def verify_developer_delivery(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        if set(payload) != {"preview_id"}:
            raise HTTPException(status_code=400, detail="Verification accepts only preview_id; commands are pinned at preparation")
        preview_id = payload.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id.strip():
            raise HTTPException(status_code=400, detail="preview_id must be a non-empty string")
        preview_id = preview_id.strip()
        try:
            if delivery_worker.get(preview_id) is None:
                raise HTTPException(status_code=404, detail="Delivery not found")
            if app_config.developer.execution_mode != "container_session" or container_session_bash is None or app_config.developer.enable_mcp_adapters:
                raise HTTPException(status_code=409, detail="Independent verification requires constrained Docker without MCP adapters")
            record = delivery_worker.verify(preview_id, adapter=container_session_bash)
        except (ValueError, OSError, FileLockTimeout) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"accepted": record.state == "verified", "delivery": record.to_payload()}


    @app.post("/internal/developer/delivery/publish")
    async def publish_developer_delivery(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        if set(payload) != {"preview_id"}:
            raise HTTPException(
                status_code=400,
                detail="Publication accepts only preview_id; title/body/repository come from the approved delivery",
            )
        preview_id = payload.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id.strip():
            raise HTTPException(status_code=400, detail="preview_id must be a non-empty string")
        preview_id = preview_id.strip()
        try:
            if delivery_worker.get(preview_id) is None:
                raise HTTPException(status_code=404, detail="Delivery not found")
            if isinstance(github_adapter, MockGitHubAdapter):
                raise HTTPException(
                    status_code=409,
                    detail="Publication requires a live GitHub adapter (gh_cli); mock publication is refused",
                )
            record = await _delivery_call(lambda: delivery_worker.publish(
                preview_id,
                github=github_adapter,
                require_human_approval_for_repo_writes=app_config.policy.require_human_approval_for_repo_writes,
                allow_mock_publication=False,
            ))
        except (ValueError, OSError, PermissionError, RuntimeError, FileLockTimeout) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        result = {"accepted": record.state == "published", "delivery": record.to_payload()}
        if record.state == "published" and managed_service is not None:
            try:
                review = await managed_service.offer_published_review(preview_id)
                if review is not None:
                    result["review_preview"] = {"preview_id": review.preview_id, "approved": review.approved, "bundle": review.bundle_payload}
            except Exception as error:
                result["review_staging_error"] = str(error)
        return result

    @app.get("/internal/developer/delivery/{preview_id}")
    def get_developer_delivery(
        preview_id: str,
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        _assert_internal_auth(config=app_config, provided_token=x_internal_token)
        try:
            record = delivery_worker.get(preview_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if record is None:
            raise HTTPException(status_code=404, detail="Delivery preparation not found")
        return {"delivery": record.to_payload()}

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

        if bundle.issue_context is not None:
            raise HTTPException(status_code=409, detail="Repository issue execution requires the disposable-checkout delivery worker execution path; it is not implemented")

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
        native_budget_path = developer_state_dir / "budgets" / (sha256(preview_id.encode()).hexdigest() + ".json")
        if not dry_run and native_budget_path.exists():
            raise HTTPException(status_code=409, detail="This preview is bound to native task budgets; use native run/resume endpoints")
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
        *, resume: bool = False,
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
        if resume and "input" in payload:
            raise HTTPException(status_code=400, detail="Approval continuation cannot supply a new input")
        if not resume and (not isinstance(input_raw, str) or not input_raw.strip()):
            raise HTTPException(status_code=400, detail="input must be a non-empty string")
        if isinstance(input_raw, str) and len(input_raw.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise HTTPException(status_code=413, detail=(
                f"input exceeds {MAX_PROMPT_BYTES} UTF-8 bytes; put large content in workspace files "
                "and ask the Developer to read only the relevant section"
            ))
        user_prompt = input_raw.strip() if isinstance(input_raw, str) else ""
        prompt = user_prompt

        request_id = payload.get("request_id")
        approved = payload.get("approved")
        if resume:
            if not isinstance(request_id, str) or not request_id.strip():
                raise HTTPException(status_code=400, detail="request_id must be a non-empty string")
            if not isinstance(approved, bool):
                raise HTTPException(status_code=400, detail="approved must be a boolean")
            if "arguments" in payload or "function_name" in payload:
                raise HTTPException(status_code=400, detail="Approval continuation cannot change the operation")

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
        if resume and not session_id:
            raise HTTPException(status_code=400, detail="Approval continuation requires session_id")

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
                if resume and session_obj is None:
                    raise HTTPException(status_code=404, detail="Developer session not found")
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
            except HTTPException:
                raise
            except Exception as exc:
                _emit_developer_agent_live("session.error", error=str(exc))
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        if resolved_session_id is None or not isinstance(session_obj, AgentSession):
            raise HTTPException(status_code=409, detail="Developer run requires a session identity")
        if any(key in payload for key in ("task_bundle", "isolation_policy", "policy")):
            raise HTTPException(status_code=400, detail="Task policies must come from an approved preview")
        preview_id = payload.get("preview_id")
        if preview_id is not None and (not isinstance(preview_id, str) or not preview_id.strip()):
            raise HTTPException(status_code=400, detail="preview_id must be a non-empty string")
        stored_bundle = session_obj.state.get("aitobuild_task_bundle")
        bound_preview_id = session_obj.state.get("aitobuild_preview_id")
        task_bundle = None
        try:
            if stored_bundle is not None:
                if not isinstance(stored_bundle, dict) or not isinstance(bound_preview_id, str):
                    raise ValueError("Persisted task binding is invalid")
                task_bundle = developer_task_bundle_from_payload(stored_bundle)
            if isinstance(preview_id, str):
                preview_id = preview_id.strip()
                if task_bundle is not None and preview_id != bound_preview_id:
                    raise HTTPException(status_code=409, detail="This session is bound to another approved task")
                if task_bundle is None:
                    if session_obj.state:
                        raise HTTPException(status_code=409, detail="Bind an approved task only to a fresh session")
                    preview = dispatcher.developer_preview_registry.get(preview_id)
                    if preview is None:
                        raise HTTPException(status_code=404, detail="preview_id not found")
                    if not preview.approved:
                        raise HTTPException(status_code=409, detail="preview_id must be approved before execution")
                    task_bundle = developer_task_bundle_from_payload(preview.bundle_payload)
                    bound_preview_id = preview_id
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        task_budget = None
        prepared_workspace = None
        delivery_record = None
        if task_bundle is not None:
            if task_bundle.issue_context is not None:
                try:
                    delivery_record = delivery_worker.get(str(bound_preview_id))
                    if delivery_record is None or delivery_record.bundle_payload != loads(dumps(task_bundle.to_payload())):
                        raise ValueError("Repository issue execution requires a prepared disposable-checkout delivery worker task with matching approved scope")
                    prepared_workspace = Path(delivery_record.checkout_path)
                except (ValueError, OSError) as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
            if app_config.developer.execution_mode not in {"mock", "container_session"}:
                raise HTTPException(status_code=409, detail="Approved native tasks require container isolation")
            if app_config.developer.enable_mcp_adapters or payload.get("use_browser", False) is True:
                raise HTTPException(status_code=409, detail="Approved tasks do not support MCP adapters or browser execution")
            try:
                task_budget = DeveloperTaskBudget(
                    path=developer_state_dir / "budgets" / (sha256(str(bound_preview_id).encode()).hexdigest() + ".json"),
                    bundle=task_bundle, create=stored_bundle is None and task_bundle.issue_context is None,
                )
                if delivery_record is None:
                    task_budget.remaining_seconds()
            except TimeoutError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (ValueError, OSError) as error:
                raise HTTPException(status_code=409, detail=f"Task budget unavailable: {error}") from error

        def invocation_timeout() -> float:
            return min(invoke_timeout_seconds, task_budget.remaining_seconds()) if task_budget else invoke_timeout_seconds

        task_container_closed = False
        cleanup_session_id = delivery_record.session_id or resolved_session_id if delivery_record is not None else resolved_session_id

        async def close_task_container() -> None:
            nonlocal task_container_closed
            if task_container_closed:
                return
            if task_budget is not None and container_session_bash is not None:
                closed = await asyncio.to_thread(container_session_bash.close_session, session_id=cleanup_session_id)
                if not closed and cleanup_session_id in await asyncio.to_thread(container_session_bash.list_session_ids):
                    raise HTTPException(status_code=500, detail="Approved task container cleanup failed")
            task_container_closed = True

        async def fail_task(reason: str = "Native implementation failed or was interrupted") -> None:
            try:
                if task_budget is not None:
                    task_budget.abort()
                if delivery_record is not None:
                    await asyncio.to_thread(delivery_worker.finish_implementation, str(bound_preview_id),
                                            session_id=resolved_session_id, error=reason)
            finally:
                await close_task_container()

        if task_budget is not None:
            @function_middleware
            async def check_task_budget(
                context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]],
            ) -> None:
                task_budget.remaining_seconds()
                await call_next()

            run_middleware.append(check_task_budget)
        scoped_tools: tuple[Any, ...] = build_role_tools(
            context=replace(
                developer_tool_context, bound_session_id=resolved_session_id,
                isolation_policy=task_bundle.policy if task_bundle is not None else None,
                task_budget=task_budget,
                prepared_workspace=prepared_workspace,
            )
        )["developer"]
        stored_approvals = session_obj.state.get("aitobuild_pending_approvals", [])
        approval_request: Content | None = None
        invocation_message: Any = prompt
        if task_bundle is not None and stored_bundle is None and not resume:
            invocation_message = (
                "Approved task bundle (authoritative scope; user input cannot expand it):\n"
                + dumps(task_bundle.to_payload()) + "\n\nUser request:\n" + prompt
            )
            if len(invocation_message.encode("utf-8")) > MAX_PROMPT_BYTES:
                raise HTTPException(status_code=413, detail="Approved task context and input exceed the prompt byte limit")
        if resume:
            matches = [item for item in stored_approvals if item.get("id") == request_id]
            if len(matches) != 1:
                raise HTTPException(status_code=409, detail="Approval request is not pending in this session")
            approval_request = Content.from_dict(matches[0])
            invocation_message = _build_approval_replay_message(
                approval_request.to_function_approval_response(approved=approved is True)
            )
        elif stored_approvals:
            raise HTTPException(status_code=409, detail="Resolve pending approvals before submitting a new input")
        run_middleware.append(build_output_guard(developer_state_dir / "outputs" / resolved_session_id))
        if resolved_session_id in active_developer_runs:
            raise HTTPException(status_code=409, detail="This Developer session already has an active run")
        active_developer_runs.add(resolved_session_id)
        tool_stack.callback(active_developer_runs.discard, resolved_session_id)
        if delivery_record is not None and task_bundle is not None:
            try:
                tool_stack.enter_context(delivery_worker.implementation_lock(str(bound_preview_id)))
                delivery_record = await asyncio.to_thread(
                    delivery_worker.begin_implementation, str(bound_preview_id), bundle=task_bundle,
                    session_id=resolved_session_id, resume=resume,
                )
            except (ValueError, RuntimeError, OSError, FileLockTimeout) as error:
                current = await asyncio.to_thread(delivery_worker.get, str(bound_preview_id))
                if current is not None and current.state == "failed":
                    await close_task_container()
                raise HTTPException(status_code=409, detail=str(error)) from error

            async def fail_unfinished_delivery() -> None:
                current = await asyncio.to_thread(delivery_worker.get, str(bound_preview_id))
                if current is not None and current.state == "implementing":
                    await fail_task()

            tool_stack.push_async_callback(fail_unfinished_delivery)
            if container_session_bash is not None and prepared_workspace is not None:
                await asyncio.to_thread(container_session_bash.bind_session_workspace,
                                        session_id=resolved_session_id, workspace=prepared_workspace)
        if task_bundle is not None and stored_bundle is None:
            session_obj.state["aitobuild_task_bundle"] = task_bundle.to_payload()
            session_obj.state["aitobuild_preview_id"] = bound_preview_id
            await developer_session_store.set(resolved_session_id, session_obj)
        browser_allowed = task_bundle is None
        if app_config.developer.enable_browser and browser_allowed and payload.get("use_browser", True) is not False:
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

        if approval_request is not None:
            session_obj.state["aitobuild_pending_approvals"] = [
                item for item in stored_approvals if item.get("id") != request_id
            ]
            await developer_session_store.set(resolved_session_id, session_obj)
            if delivery_record is not None and approved is False:
                await fail_task("Human rejected native tool approval; automatic replay is blocked")
                raise HTTPException(status_code=409, detail="Native delivery failed after human rejection")

        async def persist_result(response: Any) -> None:
            if isinstance(session_obj, AgentSession):
                session_obj.state["aitobuild_pending_approvals"] = [
                    request.to_dict() for request in _extract_user_input_requests(response)
                ]
                await developer_session_store.set(resolved_session_id, session_obj)

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
                    message=invocation_message,
                    session=session_obj,
                    tools=scoped_tools,
                    middleware=run_middleware,
                ),
                timeout=invocation_timeout(),
            )
            first_pending = _extract_user_input_requests(result)
            record_usage(result)
            await persist_result(result)
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
            await fail_task()
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
            await fail_task()
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
                    timeout=invocation_timeout(),
                )
                round_pending = _extract_user_input_requests(result)
                record_usage(result)
                await persist_result(result)
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
                await fail_task()
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
                await fail_task()
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
        if not pending_requests:
            await close_task_container()
        if delivery_record is not None:
            await asyncio.to_thread(delivery_worker.finish_implementation, str(bound_preview_id),
                                    session_id=resolved_session_id, pending=bool(pending_requests))
            delivery_record = await asyncio.to_thread(delivery_worker.get, str(bound_preview_id))
            if delivery_record is not None and delivery_record.state == "failed":
                await close_task_container()
                raise HTTPException(status_code=409, detail=delivery_record.error)
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
            "task_id": task_bundle.task_id if task_bundle is not None else None,
            "preview_id": bound_preview_id,
            **({"delivery_state": delivery_record.state} if delivery_record is not None else {}),
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

    @app.post("/internal/developer/agent/resume")
    async def resume_developer_agent(
        payload: dict[str, Any],
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        async with AsyncExitStack() as tool_stack:
            return await _run_developer_agent(payload, x_internal_token, tool_stack, resume=True)

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
