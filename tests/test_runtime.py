from __future__ import annotations

import asyncio
from pathlib import Path

from agent_framework import (
    Agent, AgentSession, FileMemoryProvider, FileSessionStore, FileSystemAgentFileStore, Message,
    SessionContext, tool,
)
import httpx
from openai import AsyncOpenAI
from agent_framework.openai import OpenAIChatCompletionClient
import pytest

from aitobuild.config import RuntimeConfig
from aitobuild.meetings import MeetingRecord, MeetingState
import aitobuild.runtime as runtime_module
from aitobuild.runtime import (
    FrameworkBindings,
    _construct_foundry_client,
    bootstrap_runtime,
    detect_framework_availability,
    kickoff_meeting_bootstrap,
)


def test_runtime_bootstrap_uses_mock_mode_when_allowed() -> None:
    runtime = RuntimeConfig(
        foundry_endpoint=None,
        foundry_api_key=None,
        foundry_model="gpt-4.1",
        allow_mock_model=True,
    )

    result = bootstrap_runtime(runtime)
    assert result.mode == "mock"
    assert "architect" in result.role_agents
    assert result.bindings is not None


def test_runtime_bootstrap_fails_without_mock_fallback() -> None:
    runtime = RuntimeConfig(
        foundry_endpoint=None,
        foundry_api_key=None,
        foundry_model="gpt-4.1",
        allow_mock_model=False,
    )

    with pytest.raises(RuntimeError):
        bootstrap_runtime(runtime)


def test_openai_v1_routes_to_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    class FakeChatClient:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

    monkeypatch.setattr(runtime_module, "detect_framework_bindings", lambda: FrameworkBindings(
        openai_chat_completion_client_class=FakeChatClient,
    ))
    config = RuntimeConfig(
        foundry_endpoint="https://example.services.ai.azure.com/openai/v1/",
        foundry_api_key="test-key", foundry_model="grok-4.6", allow_mock_model=False,
    )
    result = bootstrap_runtime(config)
    assert result.mode == "openai"
    assert observed["model"] == "grok-4.6"
    assert str(observed["async_client"].base_url) == config.foundry_endpoint


def test_openai_v1_missing_client_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "detect_framework_bindings", lambda: FrameworkBindings())
    with pytest.raises(RuntimeError, match="OpenAIChatCompletionClient unavailable"):
        bootstrap_runtime(RuntimeConfig(
            foundry_endpoint="https://example.services.ai.azure.com/openai/v1",
            foundry_api_key=None, foundry_model="grok-4.6", allow_mock_model=True,
        ))


def test_detect_framework_availability_shape() -> None:
    availability = detect_framework_availability()
    assert isinstance(availability.agent, bool)
    assert isinstance(availability.foundry_chat_client, bool)
    assert isinstance(availability.group_chat_builder, bool)


def test_native_developer_tools_are_only_bound_per_run() -> None:
    from aitobuild.agents import default_agent_specs
    from aitobuild.runtime import _build_role_agent_handles

    class FakeAgent:
        def __init__(self, **kwargs: object) -> None:
            self.tools = kwargs["tools"]

    handles = _build_role_agent_handles(
        default_agent_specs(), client=object(), agent_class=FakeAgent,
        role_tools={"developer": (lambda: None,)},
    )
    assert handles["developer"].tools == []


def test_kickoff_meeting_bootstrap_uses_mock_when_group_chat_unavailable() -> None:
    runtime_config = RuntimeConfig(
        foundry_endpoint=None,
        foundry_api_key=None,
        foundry_model="gpt-4.1",
        allow_mock_model=True,
    )
    runtime = bootstrap_runtime(runtime_config)
    meeting = MeetingRecord(
        meeting_id="meet-1",
        agenda="Architecture sync",
        participants=("Architect", "Developer"),
        state=MeetingState.DUE,
    )

    result = kickoff_meeting_bootstrap(runtime, meeting_record=meeting)
    assert result.status in {"mock_started", "workflow_built"}
    assert "meet-1" in runtime.meeting_workflows


def test_construct_foundry_client_uses_credential_based_sdk_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_kwargs: dict[str, object] = {}
    credential_sentinel = object()

    class _FakeFoundryClient:
        def __init__(self, **kwargs: object) -> None:
            observed_kwargs.update(kwargs)

    monkeypatch.setattr(
        runtime_module,
        "DefaultAzureCredential",
        lambda: credential_sentinel,
    )

    config = RuntimeConfig(
        foundry_endpoint="https://example.services.ai.azure.com/api/projects/demo",
        foundry_api_key=None,
        foundry_model="gpt-4.1",
        allow_mock_model=True,
    )
    bindings = FrameworkBindings(
        foundry_chat_client_class=_FakeFoundryClient,
        agent_class=None,
        group_chat_builder_class=None,
    )

    client = _construct_foundry_client(bindings=bindings, config=config)

    assert isinstance(client, _FakeFoundryClient)
    assert observed_kwargs == {
        "project_endpoint": "https://example.services.ai.azure.com/api/projects/demo",
        "model": "gpt-4.1",
        "credential": credential_sentinel,
        "function_invocation_configuration": {"include_detailed_errors": True},
    }


def test_construct_foundry_client_rejects_api_key_mode() -> None:
    class _FakeFoundryClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

    config = RuntimeConfig(
        foundry_endpoint="https://example.services.ai.azure.com/api/projects/demo",
        foundry_api_key="test-key",
        foundry_model="gpt-4.1",
        allow_mock_model=True,
    )
    bindings = FrameworkBindings(
        foundry_chat_client_class=_FakeFoundryClient,
        agent_class=None,
        group_chat_builder_class=None,
    )

    with pytest.raises(ValueError):
        _construct_foundry_client(bindings=bindings, config=config)


def test_native_memory_persists_without_sharing_between_developers(tmp_path: Path) -> None:
    async def memory_tools(session_id: str):
        provider = FileMemoryProvider(FileSystemAgentFileStore(tmp_path / "memory"))
        session = AgentSession(session_id=session_id)
        context = SessionContext(session_id=session_id, input_messages=[])
        await provider.before_run(agent=None, session=session, context=context, state={})
        return {tool.name: tool for tool in context.tools}

    async def check() -> None:
        first = await memory_tools("dev-one")
        await first["file_memory_write"](file_name="repo.md", content="Run pytest after edits.")
        restarted = await memory_tools("dev-one")
        assert "Run pytest after edits." in str(
            await restarted["file_memory_read"](file_name="repo.md")
        )
        second = await memory_tools("dev-two")
        assert "repo.md" not in str(await second["file_memory_ls"]())

    asyncio.run(check())


def test_native_session_store_restores_state_after_restart(tmp_path: Path) -> None:
    async def check() -> None:
        session = AgentSession(session_id="dev-one")
        session.state["verified_decision"] = "Use separate task checkouts"
        await FileSessionStore(tmp_path).set(session.session_id, session)
        restored = await FileSessionStore(tmp_path).get("dev-one")
        assert restored is not None
        assert restored.state["verified_decision"] == "Use separate task checkouts"
        assert await FileSessionStore(tmp_path).get("dev-two") is None

    asyncio.run(check())


@pytest.mark.parametrize("error_suffix", ["", "x" * 100000], ids=["small-error", "large-error"])
def test_native_history_has_no_duplicates_and_tool_errors_reach_model(tmp_path: Path, error_suffix: str) -> None:
    import json
    from aitobuild.agents import default_agent_specs
    from aitobuild.runtime import _build_role_agent_handles
    from aitobuild.tool_outputs import build_output_guard

    requests = []

    def model_reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        messages = body["messages"]
        assert sum(message.get("content") == "ORIGINAL_TASK" for message in messages) == 1
        assert sum(message["role"] == "system" for message in messages) == 1
        names = [entry["function"]["name"] for entry in body["tools"]]
        assert len(names) == len(set(names))
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-patch", "type": "function", "function": {
                    "name": "patch_probe", "arguments": "{}",
                },
            }]}
            finish = "tool_calls"
        else:
            assert sum(message["role"] == "tool" for message in messages) == 1
            tool_result = next(
                message["content"] for message in messages if message["role"] == "tool"
            )
            assert "EXPECTED_CONTEXT_DIAGNOSIS" in tool_result
            assert len(tool_result) < 2000
            if error_suffix:
                assert '"output_saved": true' in tool_result
            message = {"role": "assistant", "content": "DONE"}
            finish = "stop"
        return httpx.Response(200, json={
            "id": f"response-{len(requests)}", "object": "chat.completion", "created": 1,
            "model": "test-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        })

    @tool(approval_mode="always_require")
    def patch_probe() -> str:
        raise ValueError("EXPECTED_CONTEXT_DIAGNOSIS: read the current file before retrying" + error_suffix)

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(model_reply)) as http_client:
            async with AsyncOpenAI(api_key="test-key", http_client=http_client) as openai_client:
                client = OpenAIChatCompletionClient(
                    model="test-model", async_client=openai_client,
                    function_invocation_configuration={"include_detailed_errors": True},
                )
                agent = _build_role_agent_handles(
                    default_agent_specs(), client=client, agent_class=Agent,
                    role_tools=None, developer_state_dir=tmp_path,
                )["developer"]
                session = agent.create_session(session_id="dev-history")
                middleware = [build_output_guard(tmp_path / "outputs")]
                pending = await agent.run("ORIGINAL_TASK", session=session, tools=[patch_probe],
                                          middleware=middleware)
                assert len(pending.user_input_requests) == 1
                approved = pending.user_input_requests[0].to_function_approval_response(approved=True)
                result = await agent.run(Message(role="user", contents=[approved]),
                                         session=session, tools=[patch_probe], middleware=middleware)
                assert result.text == "DONE"
                await agent.run("NEXT_TASK", session=session, tools=[patch_probe], middleware=middleware)
                assert len(requests) == 3

    asyncio.run(check())
    if error_suffix:
        artifacts = list((tmp_path / "outputs").glob("*.txt"))
        assert len(artifacts) == 1
        assert json.loads(artifacts[0].read_text())["error"].endswith(error_suffix)
