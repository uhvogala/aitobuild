"""Runtime wiring for Agent Framework bootstrap with mock fallback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from agent_framework import (
    CompactionProvider, ContextWindowCompactionStrategy, FileHistoryProvider,
    FileSystemAgentFileStore,
)
from aitobuild.foundry_compat import FoundryCompatibleFileMemoryProvider
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncOpenAI

from aitobuild.agents import AgentSpec, default_agent_specs
from aitobuild.config import RuntimeConfig
from aitobuild.meetings import MeetingRecord


@dataclass(slots=True, frozen=True)
class FrameworkAvailability:
    foundry_chat_client: bool
    agent: bool
    group_chat_builder: bool


@dataclass(slots=True)
class FrameworkBindings:
    foundry_chat_client_class: type[Any] | None = None
    agent_class: type[Any] | None = None
    group_chat_builder_class: type[Any] | None = None
    openai_chat_completion_client_class: type[Any] | None = None


@dataclass(slots=True, frozen=True)
class MeetingKickoffResult:
    status: str
    detail: str | None = None


@dataclass(slots=True)
class RuntimeBootstrap:
    mode: str
    availability: FrameworkAvailability
    bindings: FrameworkBindings
    client: Any
    role_agents: dict[str, Any]
    meeting_workflows: dict[str, Any]
    role_tools: dict[str, tuple[Callable[..., Any], ...]]


def _try_import(module_name: str) -> ModuleType | None:
    try:
        if find_spec(module_name) is None:
            return None
    except ModuleNotFoundError:
        return None
    return import_module(module_name)


def _try_get_class(module_name: str, class_name: str) -> type[Any] | None:
    module = _try_import(module_name)
    if module is not None and hasattr(module, class_name):
        candidate = getattr(module, class_name)
        if isinstance(candidate, type):
            return candidate
    return None


def detect_framework_bindings() -> FrameworkBindings:
    # Trust the uv-managed environment and resolve only canonical package paths.
    foundry_chat_client_class = _try_get_class("agent_framework.foundry", "FoundryChatClient")
    agent_class = _try_get_class("agent_framework", "Agent")
    group_chat_builder_class = _try_get_class("agent_framework.orchestrations", "GroupChatBuilder")

    return FrameworkBindings(
        foundry_chat_client_class=foundry_chat_client_class,
        agent_class=agent_class,
        group_chat_builder_class=group_chat_builder_class,
        openai_chat_completion_client_class=_try_get_class(
            "agent_framework.openai", "OpenAIChatCompletionClient"
        ),
    )


def detect_framework_availability() -> FrameworkAvailability:
    bindings = detect_framework_bindings()

    return FrameworkAvailability(
        foundry_chat_client=bindings.foundry_chat_client_class is not None,
        agent=bindings.agent_class is not None,
        group_chat_builder=bindings.group_chat_builder_class is not None,
    )


def bootstrap_runtime(
    config: RuntimeConfig,
    *,
    role_tools: dict[str, tuple[Callable[..., Any], ...]] | None = None,
    developer_state_dir: Path | None = None,
) -> RuntimeBootstrap:
    bindings = detect_framework_bindings()
    availability = FrameworkAvailability(
        foundry_chat_client=bindings.foundry_chat_client_class is not None,
        agent=bindings.agent_class is not None,
        group_chat_builder=bindings.group_chat_builder_class is not None,
    )

    if config.foundry_endpoint and config.foundry_endpoint.rstrip("/").endswith("/openai/v1"):
        client = _construct_openai_client(bindings=bindings, config=config)
        return RuntimeBootstrap(
            mode="openai",
            availability=availability,
            bindings=bindings,
            client=client,
            role_agents=_build_role_agent_handles(
                default_agent_specs(), client=client, agent_class=bindings.agent_class,
                role_tools=role_tools, developer_state_dir=developer_state_dir,
            ),
            meeting_workflows={},
            role_tools=role_tools or {},
        )

    if availability.foundry_chat_client and config.foundry_endpoint:
        try:
            client = _construct_foundry_client(bindings=bindings, config=config)
        except Exception:
            client = None

        if client is not None:
            return RuntimeBootstrap(
                mode="foundry",
                availability=availability,
                bindings=bindings,
                client=client,
                role_agents=_build_role_agent_handles(
                    default_agent_specs(),
                    client=client,
                    agent_class=bindings.agent_class,
                    role_tools=role_tools,
                    developer_state_dir=developer_state_dir,
                ),
                meeting_workflows={},
                role_tools=role_tools or {},
            )

    if not config.allow_mock_model:
        raise RuntimeError("Foundry configuration unavailable and mock runtime disabled")

    mock_client: dict[str, str] = {
        "type": "mock_foundry_client",
        "model": config.foundry_model,
    }
    return RuntimeBootstrap(
        mode="mock",
        availability=availability,
        bindings=bindings,
        client=mock_client,
        role_agents=_build_role_agent_handles(
            default_agent_specs(),
            client=mock_client,
            agent_class=None,
            role_tools=role_tools,
        ),
        meeting_workflows={},
        role_tools=role_tools or {},
    )


def _construct_openai_client(*, bindings: FrameworkBindings, config: RuntimeConfig) -> Any:
    client_class = bindings.openai_chat_completion_client_class
    if client_class is None:
        raise RuntimeError("OpenAIChatCompletionClient unavailable; run uv sync")
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )

    async def async_token_provider() -> str:
        return await asyncio.to_thread(token_provider)

    api_key = config.foundry_api_key or async_token_provider
    return client_class(
        model=config.foundry_model,
        function_invocation_configuration={"include_detailed_errors": True},
        async_client=AsyncOpenAI(
            base_url=config.foundry_endpoint, api_key=api_key, max_retries=1,
        ),
    )


def _construct_foundry_client(*, bindings: FrameworkBindings, config: RuntimeConfig) -> Any:
    endpoint = config.foundry_endpoint
    if endpoint is None:
        return None

    foundry_cls = bindings.foundry_chat_client_class
    if foundry_cls is None:
        return None

    if config.foundry_api_key:
        raise ValueError(
            "AITOBUILD_FOUNDRY_API_KEY is set, but Foundry project endpoint auth uses Entra credentials in this SDK. "
            "Unset AITOBUILD_FOUNDRY_API_KEY and authenticate with Azure credentials (for example via az login)."
        )

    # Prefer Entra credential-based auth when no API key is configured.
    return foundry_cls(
        project_endpoint=endpoint,
        model=config.foundry_model,
        credential=DefaultAzureCredential(),
        function_invocation_configuration={"include_detailed_errors": True},
    )


def _build_role_agent_handles(
    specs: tuple[AgentSpec, ...],
    *,
    client: Any,
    agent_class: type[Any] | None,
    role_tools: dict[str, tuple[Callable[..., Any], ...]] | None,
    developer_state_dir: Path | None = None,
) -> dict[str, Any]:
    tools_by_role = role_tools or {}
    return {
        spec.role.value: build_agent_handle(
            spec, client=client, agent_class=agent_class,
            tools=tools_by_role.get(spec.role.value, ()), developer_state_dir=developer_state_dir,
        )
        for spec in specs
    }


def build_agent_handle(
    spec: AgentSpec,
    *,
    client: Any,
    agent_class: type[Any] | None,
    tools: tuple[Callable[..., Any], ...] = (),
    developer_state_dir: Path | None = None,
) -> Any:
    if agent_class is None:
        return {
            "name": spec.name, "instructions": spec.instructions,
            "client": client, "tools": tools,
        }
    options: dict[str, Any] = {}
    if spec.role.value == "developer" and developer_state_dir is not None:
        options = {
            "context_providers": [
                FileHistoryProvider(developer_state_dir / "history", skip_excluded=True),
                FoundryCompatibleFileMemoryProvider(
                    FileSystemAgentFileStore(developer_state_dir / "memory"),
                ),
                CompactionProvider(
                    history_source_id="file_history",
                    before_strategy=ContextWindowCompactionStrategy(
                        max_context_window_tokens=32000, max_output_tokens=4096,
                        keep_last_tool_call_groups=4, preserve_first_user_group=True,
                    ),
                ),
            ],
            "default_options": {"store": False, "max_tokens": 4096},
            "require_per_service_call_history_persistence": True,
        }
    try:
        return agent_class(
            client=client, name=spec.name, instructions=spec.instructions,
            tools=[] if spec.role.value == "developer" else list(tools), **options,
        )
    except Exception as error:
        raise RuntimeError(f"Failed to bind {spec.role.value} agent with its tools") from error


def kickoff_meeting_bootstrap(
    runtime: RuntimeBootstrap,
    *,
    meeting_record: MeetingRecord,
) -> MeetingKickoffResult:
    meeting_id = meeting_record.meeting_id

    if runtime.bindings.group_chat_builder_class is None:
        runtime.meeting_workflows[meeting_id] = {
            "status": "mock_started",
            "agenda": meeting_record.agenda,
            "participants": meeting_record.participants,
        }
        return MeetingKickoffResult(status="mock_started", detail="GroupChatBuilder unavailable")

    participants = _resolve_participant_agents(runtime, meeting_record.participants)
    if len(participants) < 2:
        runtime.meeting_workflows[meeting_id] = {
            "status": "mock_started",
            "agenda": meeting_record.agenda,
            "participants": meeting_record.participants,
            "detail": "insufficient bound participants",
        }
        return MeetingKickoffResult(status="mock_started", detail="insufficient bound participants")

    orchestrator_agent = runtime.role_agents.get("pm") or participants[0]
    try:
        builder_class = runtime.bindings.group_chat_builder_class
        assert builder_class is not None
        workflow_builder = builder_class(
            participants=participants,
            orchestrator_agent=orchestrator_agent,
            max_rounds=8,
        )
        workflow = workflow_builder.build()
        runtime.meeting_workflows[meeting_id] = {
            "status": "workflow_built",
            "workflow": workflow,
            "agenda": meeting_record.agenda,
            "participants": meeting_record.participants,
        }
        return MeetingKickoffResult(status="workflow_built")
    except Exception as exc:
        runtime.meeting_workflows[meeting_id] = {
            "status": "mock_started",
            "agenda": meeting_record.agenda,
            "participants": meeting_record.participants,
            "detail": str(exc),
        }
        return MeetingKickoffResult(status="mock_started", detail=str(exc))


def _resolve_participant_agents(runtime: RuntimeBootstrap, participants: tuple[str, ...]) -> list[Any]:
    role_lookup = {"pm": "pm", "architect": "architect", "developer": "developer", "dev": "developer"}

    resolved: list[Any] = []
    for participant in participants:
        key = role_lookup.get(participant.strip().lower())
        if key is None:
            continue
        agent_handle = runtime.role_agents.get(key)
        if agent_handle is None:
            continue
        if isinstance(agent_handle, dict):
            continue
        resolved.append(agent_handle)

    return resolved
