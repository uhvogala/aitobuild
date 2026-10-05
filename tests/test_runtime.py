from __future__ import annotations

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


def test_detect_framework_availability_shape() -> None:
    availability = detect_framework_availability()
    assert isinstance(availability.agent, bool)
    assert isinstance(availability.foundry_chat_client, bool)
    assert isinstance(availability.group_chat_builder, bool)


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
