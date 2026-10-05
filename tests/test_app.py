from __future__ import annotations

from dataclasses import replace
import asyncio
import hmac
import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

from agent_framework import AgentSession, Content, FileSessionStore, Message
from fastapi.testclient import TestClient
import pytest

import aitobuild.app as app_module
from aitobuild.app import create_app
from aitobuild.developer_isolation import IsolationTool, build_developer_task_bundle, default_developer_isolation_policy
from aitobuild.runtime import FrameworkAvailability, FrameworkBindings, RuntimeBootstrap


def _signature(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
    return f"sha256={digest}"


def _internal_headers(test_config) -> dict[str, str]:
    token = test_config.security.internal_api_token or ""
    return {"X-Internal-Token": token}


_FakeSession = AgentSession


def _fake_approval_request() -> Content:
    request_id = uuid4().hex
    return Content.from_function_approval_request(
        request_id,
        Content.from_function_call(
            request_id, "developer_run_command", id=request_id,
            arguments={"command": "pytest --version"},
        ),
    )


class _FakeRunResult:
    def __init__(self, *, text: str, user_input_requests: list[Any], response_id: str | None = None) -> None:
        self.text = text
        self.user_input_requests = user_input_requests
        self.response_id = response_id


class _FakeDeveloperAgent:
    def __init__(self, *, require_approval: bool = True) -> None:
        self.calls: list[tuple[Any, str | None]] = []
        self.require_approval = require_approval

    def create_session(self, *, session_id: str | None = None) -> _FakeSession:
        return _FakeSession(session_id=session_id or "fake-session")

    async def run(self, messages: Any, *, session: _FakeSession | None = None) -> _FakeRunResult:
        self.calls.append((messages, session.session_id if session is not None else None))
        if session is not None:
            session.state["turn_count"] = session.state.get("turn_count", 0) + 1

        if not isinstance(messages, str):
            return _FakeRunResult(
                text="command executed" if messages.contents[0].approved else "command rejected",
                user_input_requests=[], response_id="resp-2",
            )
        if not self.require_approval:
            return _FakeRunResult(text="done", user_input_requests=[])

        return _FakeRunResult(
            text="approval needed",
            user_input_requests=[_fake_approval_request()],
            response_id="resp-1",
        )


def _create_app_with_fake_agent(test_config, monkeypatch: pytest.MonkeyPatch, fake_agent: Any) -> TestClient:
    def fake_bootstrap_runtime(*_args: Any, role_tools: dict[str, tuple[Any, ...]] | None = None, **_kwargs: Any) -> RuntimeBootstrap:
        return RuntimeBootstrap(
            mode="foundry",
            availability=FrameworkAvailability(
                foundry_chat_client=True,
                agent=True,
                group_chat_builder=False,
            ),
            bindings=FrameworkBindings(
                foundry_chat_client_class=None,
                agent_class=None,
                group_chat_builder_class=None,
            ),
            client={"type": "fake-client"},
            role_agents={"developer": fake_agent},
            meeting_workflows={},
            role_tools=role_tools or {},
        )

    monkeypatch.setattr(app_module, "bootstrap_runtime", fake_bootstrap_runtime)
    return TestClient(create_app(test_config))


def test_health_endpoint(test_config) -> None:
    client = TestClient(create_app(test_config))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("prompt", ["x" * 32001, "\u4e2d" * 11000], ids=["ascii", "multibyte"])
def test_oversized_agent_prompt_is_rejected_before_model_call(test_config, monkeypatch, prompt: str) -> None:
    agent = _FakeDeveloperAgent()
    client = _create_app_with_fake_agent(test_config, monkeypatch, agent)
    response = client.post("/internal/developer/agent/run", headers=_internal_headers(test_config),
                           json={"input": prompt})
    assert response.status_code == 413
    assert agent.calls == []


def test_webhook_signature_validation(test_config) -> None:
    client = TestClient(create_app(test_config))
    payload = {"action": "opened", "issue": {"number": 1}}
    body = json.dumps(payload).encode("utf-8")

    response = client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "d-1",
            "X-Hub-Signature-256": _signature(test_config.webhook_secret, body),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["route"] == "developer.async.webhook"
    assert body["metadata"] is not None
    assert body["metadata"]["developer_task_bundle"]["task_id"].startswith("WEBHOOK-")


def test_webhook_duplicate_delivery_is_deduped(test_config) -> None:
    client = TestClient(create_app(test_config))
    payload = {"action": "opened", "issue": {"number": 1}}
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "d-2",
        "X-Hub-Signature-256": _signature(test_config.webhook_secret, body),
    }

    first = client.post("/webhook", content=body, headers=headers)
    second = client.post("/webhook", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["accepted"] is True
    assert second.json()["accepted"] is False
    assert second.json()["route"] == "dedupe"


def test_webhook_requires_preview_when_enabled(test_config) -> None:
    preview_config = replace(
        test_config,
        developer=replace(test_config.developer, require_preview_before_dispatch=True),
    )
    client = TestClient(create_app(preview_config))
    payload = {"action": "opened", "issue": {"number": 5}}
    body = json.dumps(payload).encode("utf-8")

    response = client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "d-preview-1",
            "X-Hub-Signature-256": _signature(preview_config.webhook_secret, body),
        },
    )

    assert response.status_code == 200
    payload_body = response.json()
    assert payload_body["accepted"] is False
    assert payload_body["route"] == "developer.preview_required"
    assert payload_body["metadata"] is not None
    assert isinstance(payload_body["metadata"]["preview_id"], str)


def test_preview_create_approve_allows_webhook_dispatch(test_config) -> None:
    preview_config = replace(
        test_config,
        developer=replace(test_config.developer, require_preview_before_dispatch=True),
    )
    client = TestClient(create_app(preview_config))
    headers = _internal_headers(preview_config)

    create_response = client.post(
        "/internal/developer/preview",
        headers=headers,
        json={
            "github_event": "issues",
            "action": "opened",
            "delivery_id": "d-preview-2",
            "body": {"issue": {"number": 6}},
        },
    )
    assert create_response.status_code == 200
    preview_id = create_response.json()["preview_id"]

    approve_response = client.post(
        "/internal/developer/preview/approve",
        headers=headers,
        json={"preview_id": preview_id},
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["approved"] is True

    webhook_payload = {"action": "opened", "issue": {"number": 6}}
    webhook_body = json.dumps(webhook_payload).encode("utf-8")
    webhook_response = client.post(
        "/webhook",
        content=webhook_body,
        headers={
            "content-type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "d-preview-2",
            "X-Hub-Signature-256": _signature(preview_config.webhook_secret, webhook_body),
        },
    )

    assert webhook_response.status_code == 200
    webhook_body_json = webhook_response.json()
    assert webhook_body_json["accepted"] is True
    assert webhook_body_json["route"] == "developer.async.webhook"


def test_preview_listing_defaults_to_pending_only(test_config) -> None:
    client = TestClient(create_app(test_config))
    headers = _internal_headers(test_config)

    first = client.post(
        "/internal/developer/preview",
        headers=headers,
        json={
            "github_event": "issues",
            "action": "opened",
            "delivery_id": "d-preview-list-1",
            "body": {"issue": {"number": 41}},
        },
    )
    assert first.status_code == 200
    first_id = first.json()["preview_id"]

    approve = client.post(
        "/internal/developer/preview/approve",
        headers=headers,
        json={"preview_id": first_id},
    )
    assert approve.status_code == 200

    second = client.post(
        "/internal/developer/preview",
        headers=headers,
        json={
            "github_event": "issues",
            "action": "edited",
            "delivery_id": "d-preview-list-2",
            "body": {"issue": {"number": 42}},
        },
    )
    assert second.status_code == 200
    second_id = second.json()["preview_id"]

    pending_response = client.get("/internal/developer/previews", headers=headers)
    assert pending_response.status_code == 200
    pending_body = pending_response.json()
    assert pending_body["pending_only"] is True
    assert pending_body["count"] == 1
    assert pending_body["items"][0]["preview_id"] == second_id
    assert pending_body["items"][0]["approved"] is False

    all_response = client.get("/internal/developer/previews?pending_only=false", headers=headers)
    assert all_response.status_code == 200
    all_body = all_response.json()
    assert all_body["pending_only"] is False
    ids = {item["preview_id"] for item in all_body["items"]}
    assert first_id in ids
    assert second_id in ids


@pytest.mark.parametrize("command", [
    "uv run pytest", "uv sync", "npm run build", "git diff --stat",
    "bash scripts/check.sh", "PYTHONPATH=src pytest -q",
])
def test_developer_run_dry_run_success_for_approved_preview(test_config, command: str) -> None:
    client = TestClient(create_app(test_config))
    headers = _internal_headers(test_config)

    create_response = client.post(
        "/internal/developer/preview",
        headers=headers,
        json={
            "github_event": "issues",
            "action": "opened",
            "delivery_id": "d-run-1",
            "body": {"issue": {"number": 51}},
        },
    )
    assert create_response.status_code == 200
    preview_id = create_response.json()["preview_id"]

    approve_response = client.post(
        "/internal/developer/preview/approve",
        headers=headers,
        json={"preview_id": preview_id},
    )
    assert approve_response.status_code == 200

    run_response = client.post(
        "/internal/developer/run",
        headers=headers,
        json={
            "preview_id": preview_id,
            "dry_run": True,
            "commands": [command],
            "file_writes": [{"path": "tests/sandbox-output.txt", "content": "dry-run"}],
        },
    )
    assert run_response.status_code == 200
    body = run_response.json()
    assert body["accepted"] is True
    assert body["dry_run"] is True
    assert body["command_outcomes"][0]["command"] == command
    assert body["command_outcomes"][0]["executed"] is False
    assert body["file_write_outcomes"][0]["path"] == "tests/sandbox-output.txt"
    assert body["file_write_outcomes"][0]["executed"] is False


def test_developer_run_rejects_unapproved_preview(test_config) -> None:
    client = TestClient(create_app(test_config))
    headers = _internal_headers(test_config)

    create_response = client.post(
        "/internal/developer/preview",
        headers=headers,
        json={
            "github_event": "issues",
            "action": "opened",
            "delivery_id": "d-run-2",
            "body": {"issue": {"number": 52}},
        },
    )
    assert create_response.status_code == 200
    preview_id = create_response.json()["preview_id"]

    run_response = client.post(
        "/internal/developer/run",
        headers=headers,
        json={"preview_id": preview_id, "dry_run": True},
    )
    assert run_response.status_code == 409


def test_developer_run_rejects_disallowed_command(test_config) -> None:
    client = TestClient(create_app(test_config))
    headers = _internal_headers(test_config)

    create_response = client.post(
        "/internal/developer/preview",
        headers=headers,
        json={
            "github_event": "issues",
            "action": "opened",
            "delivery_id": "d-run-3",
            "body": {"issue": {"number": 53}},
        },
    )
    assert create_response.status_code == 200
    preview_id = create_response.json()["preview_id"]

    approve_response = client.post(
        "/internal/developer/preview/approve",
        headers=headers,
        json={"preview_id": preview_id},
    )
    assert approve_response.status_code == 200

    run_response = client.post(
        "/internal/developer/run",
        headers=headers,
        json={
            "preview_id": preview_id,
            "dry_run": True,
            "commands": ["rm -rf /"],
        },
    )
    assert run_response.status_code == 200
    body = run_response.json()
    assert body["accepted"] is False
    assert body["command_outcomes"][0]["rejected_reason"] is not None


def test_developer_run_subprocess_mode_executes_command(test_config) -> None:
    subprocess_config = replace(
        test_config,
        developer=replace(
            test_config.developer,
            execution_mode="subprocess",
            command_timeout_seconds=30,
        ),
    )
    client = TestClient(create_app(subprocess_config))
    headers = _internal_headers(subprocess_config)

    create_response = client.post(
        "/internal/developer/preview",
        headers=headers,
        json={
            "github_event": "issues",
            "action": "opened",
            "delivery_id": "d-run-live-1",
            "body": {"issue": {"number": 54}},
        },
    )
    assert create_response.status_code == 200
    preview_id = create_response.json()["preview_id"]

    approve_response = client.post(
        "/internal/developer/preview/approve",
        headers=headers,
        json={"preview_id": preview_id},
    )
    assert approve_response.status_code == 200

    run_response = client.post(
        "/internal/developer/run",
        headers=headers,
        json={
            "preview_id": preview_id,
            "dry_run": False,
            "commands": ["pytest --version"],
            "approved": True,
        },
    )
    assert run_response.status_code == 200
    body = run_response.json()
    assert body["accepted"] is True
    assert body["command_outcomes"][0]["executed"] is True
    assert body["command_outcomes"][0]["exit_code"] == 0


def test_developer_session_endpoints_not_available_in_mock_mode(test_config) -> None:
    client = TestClient(create_app(test_config))
    headers = _internal_headers(test_config)

    start_response = client.post(
        "/internal/developer/session/start",
        headers=headers,
        json={},
    )
    stop_response = client.post(
        "/internal/developer/session/stop",
        headers=headers,
        json={"session_id": "demo"},
    )

    assert start_response.status_code == 409
    assert stop_response.status_code == 409


def test_developer_run_requires_session_id_in_container_mode(test_config) -> None:
    container_config = replace(
        test_config,
        developer=replace(test_config.developer, execution_mode="container_session"),
    )
    client = TestClient(create_app(container_config))
    headers = _internal_headers(container_config)

    create_response = client.post(
        "/internal/developer/preview",
        headers=headers,
        json={
            "github_event": "issues",
            "action": "opened",
            "delivery_id": "d-run-container-1",
            "body": {"issue": {"number": 55}},
        },
    )
    assert create_response.status_code == 200
    preview_id = create_response.json()["preview_id"]

    approve_response = client.post(
        "/internal/developer/preview/approve",
        headers=headers,
        json={"preview_id": preview_id},
    )
    assert approve_response.status_code == 200

    run_response = client.post(
        "/internal/developer/run",
        headers=headers,
        json={
            "preview_id": preview_id,
            "dry_run": False,
            "commands": ["pytest --version"],
            "approved": True,
        },
    )
    assert run_response.status_code == 400
    assert "session_id" in run_response.json()["detail"]


def test_developer_agent_status_reports_descriptor_mode(test_config) -> None:
    client = TestClient(create_app(test_config))
    response = client.get("/internal/runtime/developer-agent", headers=_internal_headers(test_config))

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_mode"] in {"mock", "foundry"}
    assert body["developer_handle_kind"] == "descriptor"
    assert body["ready_for_run"] is False


def test_developer_agent_run_rejected_in_descriptor_mode(test_config) -> None:
    client = TestClient(create_app(test_config))
    response = client.post(
        "/internal/developer/agent/run",
        headers=_internal_headers(test_config),
        json={"input": "Run tests and summarize failures"},
    )

    assert response.status_code == 409


def test_developer_agent_run_executes_with_auto_approvals(test_config, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_agent = _FakeDeveloperAgent()
    client = _create_app_with_fake_agent(test_config, monkeypatch, fake_agent)

    response = client.post(
        "/internal/developer/agent/run",
        headers=_internal_headers(test_config),
        json={
            "input": "Run the test command",
            "create_session": True,
            "auto_approve_tools": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["completed"] is True
    assert body["pending_approval_count"] == 0
    assert body["auto_approved_count"] == 1
    assert body["session_id"] == "fake-session"
    assert body["output_text"] == "command executed"
    assert len(fake_agent.calls) == 2
    assert fake_agent.calls[0][1] == "fake-session"
    assert fake_agent.calls[1][1] == "fake-session"


def test_app_fails_fast_when_mcp_enabled_without_container_session(test_config) -> None:
    invalid_config = replace(
        test_config,
        developer=replace(
            test_config.developer,
            execution_mode="subprocess",
            enable_mcp_adapters=True,
        ),
    )

    with pytest.raises(RuntimeError):
        create_app(invalid_config)


def test_developer_agent_session_survives_app_restart(test_config, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _create_app_with_fake_agent(test_config, monkeypatch, _FakeDeveloperAgent(require_approval=False))
    response = first.post(
        "/internal/developer/agent/run", headers=_internal_headers(test_config),
        json={"input": "First turn", "session_id": "dev-one"},
    )
    assert response.status_code == 200, response.text
    restarted = _create_app_with_fake_agent(test_config, monkeypatch, _FakeDeveloperAgent(require_approval=False))
    response = restarted.post(
        "/internal/developer/agent/run", headers=_internal_headers(test_config),
        json={"input": "Second turn", "session_id": "dev-one"},
    )
    assert response.status_code == 200
    restored = asyncio.run(FileSessionStore(test_config.developer.state_dir + "/sessions").get("dev-one"))
    assert restored is not None
    assert restored.state["turn_count"] == 2

@pytest.mark.parametrize("approved", [True, False])
def test_developer_agent_approval_resumes_after_restart(
    test_config, monkeypatch: pytest.MonkeyPatch, approved: bool,
) -> None:
    first_agent = _FakeDeveloperAgent()
    first = _create_app_with_fake_agent(test_config, monkeypatch, first_agent)
    headers = _internal_headers(test_config)
    pending = first.post(
        "/internal/developer/agent/run", headers=headers,
        json={"input": "Run tests", "session_id": "approval-task"},
    ).json()
    request = pending["pending_approval_requests"][0]
    assert request["request_id"]
    blocked = first.post(
        "/internal/developer/agent/run", headers=headers,
        json={"input": "Different task", "session_id": "approval-task"},
    )
    assert blocked.status_code == 409
    assert len(first_agent.calls) == 1
    resumed_agent = _FakeDeveloperAgent()
    restarted = _create_app_with_fake_agent(test_config, monkeypatch, resumed_agent)
    payload = {"session_id": "approval-task", "request_id": request["request_id"], "approved": approved}
    response = restarted.post("/internal/developer/agent/resume", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["completed"] is True
    assert response.json()["output_text"] == ("command executed" if approved else "command rejected")
    assert len(resumed_agent.calls) == 1
    message, session_id = resumed_agent.calls[0]
    assert isinstance(message, Message)
    assert session_id == "approval-task"
    assert message.contents[0].type == "function_approval_response"
    assert message.contents[0].approved is approved
    assert message.contents[0].function_call.arguments == {"command": "pytest --version"}
    replay = restarted.post("/internal/developer/agent/resume", headers=headers, json=payload)
    assert replay.status_code == 409
    assert len(resumed_agent.calls) == 1


@pytest.mark.parametrize("changes,status", [
    ({"session_id": "missing"}, 404),
    ({"session_id": ""}, 400),
    ({"request_id": "stale"}, 409),
    ({"approved": "false"}, 400),
    ({"input": "Replace the task"}, 400),
    ({"arguments": {"command": "different"}}, 400),
])
def test_developer_agent_approval_rejects_invalid_decisions(
    test_config, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any], status: int,
) -> None:
    agent = _FakeDeveloperAgent()
    client = _create_app_with_fake_agent(test_config, monkeypatch, agent)
    headers = _internal_headers(test_config)
    pending = client.post(
        "/internal/developer/agent/run", headers=headers,
        json={"input": "Run tests", "session_id": "approval-task"},
    ).json()
    payload = {
        "session_id": "approval-task", "request_id": pending["pending_approval_requests"][0]["request_id"],
        "approved": True, **changes,
    }
    unauthorized = client.post("/internal/developer/agent/resume", json=payload)
    assert unauthorized.status_code == 401
    response = client.post("/internal/developer/agent/resume", headers=headers, json=payload)
    assert response.status_code == status, response.text
    assert len(agent.calls) == 1


@pytest.mark.parametrize("bound_task", [True, False])
@pytest.mark.parametrize("approved", [True, False])
def test_native_developer_approval_executes_only_the_saved_operation(
    test_config, monkeypatch: pytest.MonkeyPatch, tmp_path, approved: bool, bound_task: bool,
) -> None:
    import httpx
    from agent_framework import Agent
    from agent_framework.openai import OpenAIChatCompletionClient
    from openai import AsyncOpenAI
    from aitobuild.agents import default_agent_specs
    from aitobuild.runtime import _build_role_agent_handles

    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    written_file = tmp_path / ".aitobuild" / "workspaces" / "native-approval" / "src" / "approval-probe.txt"
    requests: list[dict[str, Any]] = []

    def model_reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert sum("ORIGINAL_TASK" in (message.get("content") or "") for message in body["messages"]) == 1
        if bound_task:
            assert "developer_run_command" not in [entry["function"]["name"] for entry in body["tools"]]
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-write", "type": "function", "function": {
                    "name": "developer_write_file",
                    "arguments": json.dumps({"path": "src/approval-probe.txt", "content": "APPROVED", "approved": True}),
                },
            }]}
            finish = "tool_calls"
        else:
            assert sum(message["role"] == "tool" for message in body["messages"]) == 1
            assert written_file.exists() is approved
            message = {"role": "assistant", "content": "DONE"}
            finish = "stop"
        return httpx.Response(200, json={
            "id": f"response-{len(requests)}", "object": "chat.completion", "created": 1,
            "model": "test-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        })

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(model_reply))
    openai_client = AsyncOpenAI(api_key="test-key", http_client=http_client)
    model_client = OpenAIChatCompletionClient(model="test-model", async_client=openai_client)

    def native_agent() -> Any:
        return _build_role_agent_handles(
            default_agent_specs(), client=model_client, agent_class=Agent, role_tools=None,
            developer_state_dir=tmp_path / "developer-state",
        )["developer"]

    try:
        headers = _internal_headers(test_config)
        preview_id = None
        dispatcher_class = app_module.DispatcherAgent
        if bound_task:
            dispatcher = dispatcher_class(require_developer_preview=False)
            bundle = build_developer_task_bundle(
                task_id="native-scoped-write", objective="Write a scoped test note",
                acceptance_criteria=("The note has exact approved content",), constraints=(), context_files=(),
                policy=replace(default_developer_isolation_policy(), allowed_tools=(IsolationTool.FILESYSTEM,),
                               allowed_paths=("src/approval-probe.txt",), allowed_command_prefixes=()),
            )
            preview = dispatcher.developer_preview_registry.create_or_get(
                dedupe_key="native-scoped-write", bundle_payload=bundle.to_payload(), source_payload={},
            )
            dispatcher.developer_preview_registry.approve(preview.preview_id)
            preview_id = preview.preview_id
            monkeypatch.setattr(app_module, "DispatcherAgent", lambda **kwargs: dispatcher)
        first = _create_app_with_fake_agent(test_config, monkeypatch, native_agent())
        response = first.post(
            "/internal/developer/agent/run", headers=headers,
            json={"input": "ORIGINAL_TASK", "session_id": "native-approval", "preview_id": preview_id},
        )
        assert response.status_code == 200, response.text
        pending = response.json()
        assert pending["completed"] is False
        assert written_file.exists() is False
        monkeypatch.setattr(app_module, "DispatcherAgent", dispatcher_class)
        restarted = _create_app_with_fake_agent(test_config, monkeypatch, native_agent())
        payload = {
            "session_id": "native-approval", "approved": approved, "include_tool_trace": True,
            "request_id": pending["pending_approval_requests"][0]["request_id"],
        }
        resumed = restarted.post("/internal/developer/agent/resume", headers=headers, json=payload)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["completed"] is True
        assert resumed.json()["preview_id"] == preview_id
        assert len(resumed.json()["tool_trace"]) == (1 if approved else 0)
        if approved:
            assert written_file.read_text() == "APPROVED"
        assert len(requests) == 2
        duplicate = restarted.post("/internal/developer/agent/resume", headers=headers, json=payload)
        assert duplicate.status_code == 409
        assert len(requests) == 2
    finally:
        asyncio.run(openai_client.close())


@pytest.mark.parametrize("error,status", [(RuntimeError("provider unavailable"), 500), (asyncio.TimeoutError(), 504)])
def test_failed_developer_approval_cannot_be_replayed(
    test_config, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int,
) -> None:
    headers = _internal_headers(test_config)
    first = _create_app_with_fake_agent(test_config, monkeypatch, _FakeDeveloperAgent())
    pending = first.post(
        "/internal/developer/agent/run", headers=headers,
        json={"input": "Run tests", "session_id": "failed-approval"},
    ).json()
    payload = {
        "session_id": "failed-approval", "approved": True,
        "request_id": pending["pending_approval_requests"][0]["request_id"],
    }

    class FailingAgent(_FakeDeveloperAgent):
        async def run(self, messages: Any, *, session: AgentSession | None = None) -> _FakeRunResult:
            self.calls.append((messages, session.session_id if session else None))
            raise error

    failing_agent = FailingAgent()
    restarted = _create_app_with_fake_agent(test_config, monkeypatch, failing_agent)
    failed = restarted.post("/internal/developer/agent/resume", headers=headers, json=payload)
    assert failed.status_code == status
    assert len(failing_agent.calls) == 1
    retry_agent = _FakeDeveloperAgent()
    restarted_again = _create_app_with_fake_agent(test_config, monkeypatch, retry_agent)
    replay = restarted_again.post("/internal/developer/agent/resume", headers=headers, json=payload)
    assert replay.status_code == 409
    assert retry_agent.calls == []


def test_developer_approval_cannot_target_another_pending_session(
    test_config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _FakeDeveloperAgent()
    client = _create_app_with_fake_agent(test_config, monkeypatch, agent)
    headers = _internal_headers(test_config)
    first = client.post(
        "/internal/developer/agent/run", headers=headers,
        json={"input": "First task", "session_id": "first-task"},
    ).json()
    second = client.post(
        "/internal/developer/agent/run", headers=headers,
        json={"input": "Second task", "session_id": "second-task"},
    ).json()
    assert first["pending_approval_requests"][0]["request_id"] != second["pending_approval_requests"][0]["request_id"]
    response = client.post(
        "/internal/developer/agent/resume", headers=headers,
        json={"session_id": "second-task", "approved": True,
              "request_id": first["pending_approval_requests"][0]["request_id"]},
    )
    assert response.status_code == 409
    assert len(agent.calls) == 2


@pytest.mark.parametrize("approved", [True, False])
def test_native_task_uses_only_an_approved_persisted_preview(
    test_config, monkeypatch: pytest.MonkeyPatch, approved: bool,
) -> None:
    dispatcher_class = app_module.DispatcherAgent
    dispatcher = dispatcher_class(require_developer_preview=False)
    policy = replace(default_developer_isolation_policy(), allowed_paths=("tests/only/",),
                     allowed_command_prefixes=("git diff",))
    bundle = build_developer_task_bundle(
        task_id="approved-task", objective="Inspect the scoped test change",
        acceptance_criteria=("Report the diff",), constraints=(), context_files=(), policy=policy,
    )
    preview = dispatcher.developer_preview_registry.create_or_get(
        dedupe_key="native-task", bundle_payload=bundle.to_payload(), source_payload={},
    )
    if approved:
        dispatcher.developer_preview_registry.approve(preview.preview_id)
    monkeypatch.setattr(app_module, "DispatcherAgent", lambda **kwargs: dispatcher)
    original_builder = app_module.build_role_tools
    policies = []

    def capture_tools(*, context):
        if context.bound_session_id:
            policies.append(context.isolation_policy)
        return original_builder(context=context)

    monkeypatch.setattr(app_module, "build_role_tools", capture_tools)
    agent = _FakeDeveloperAgent()
    client = _create_app_with_fake_agent(test_config, monkeypatch, agent)
    headers = _internal_headers(test_config)
    response = client.post(
        "/internal/developer/agent/run", headers=headers,
        json={"input": "Inspect tests", "session_id": "bound-task", "preview_id": preview.preview_id},
    )
    if not approved:
        assert response.status_code == 409
        assert agent.calls == []
        assert policies == []
        return
    assert response.status_code == 200, response.text
    assert response.json()["task_id"] == bundle.task_id
    assert policies == [policy]
    assert agent.calls[0][0].count('"task_id": "approved-task"') == 1
    assert bundle.objective in agent.calls[0][0]
    monkeypatch.setattr(app_module, "DispatcherAgent", dispatcher_class)
    resumed_agent = _FakeDeveloperAgent()
    restarted = _create_app_with_fake_agent(test_config, monkeypatch, resumed_agent)
    pending_id = response.json()["pending_approval_requests"][0]["request_id"]
    tampered = restarted.post(
        "/internal/developer/agent/resume", headers=headers,
        json={"session_id": "bound-task", "request_id": pending_id, "approved": True, "policy": {}},
    )
    assert tampered.status_code == 400
    assert resumed_agent.calls == []
    changed = restarted.post(
        "/internal/developer/agent/resume", headers=headers,
        json={"session_id": "bound-task", "request_id": pending_id, "approved": True, "preview_id": "other"},
    )
    assert changed.status_code == 409
    assert resumed_agent.calls == []
    resumed = restarted.post(
        "/internal/developer/agent/resume", headers=headers,
        json={"session_id": "bound-task", "request_id": pending_id, "approved": True},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["task_id"] == bundle.task_id
    assert resumed.json()["preview_id"] == preview.preview_id
    assert policies == [policy, policy]


def test_app_fails_fast_when_browser_enabled_without_container_session(test_config) -> None:
    invalid_config = replace(
        test_config, developer=replace(test_config.developer, enable_browser=True),
    )
    with pytest.raises(ValueError):
        create_app(invalid_config)


def test_explicit_preview_budget_cannot_be_reset_or_bypassed(test_config, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _FakeDeveloperAgent()
    client = _create_app_with_fake_agent(test_config, monkeypatch, agent)
    headers = _internal_headers(test_config)
    bundle = build_developer_task_bundle(
        task_id="explicit-task", objective="Inspect the single scoped file",
        acceptance_criteria=["No other files changed"], constraints=[], context_files=["src/example.py"],
        policy=replace(default_developer_isolation_policy(), max_file_changes=1, max_runtime_minutes=1),
    )
    preview = client.post("/internal/developer/preview", headers=headers, json={
        "github_event": "issues", "action": "opened", "task_bundle": bundle.to_payload(),
    })
    assert preview.status_code == 200, preview.text
    payload = {"input": "Inspect the scope", "session_id": "explicit-budget", "preview_id": preview.json()["preview_id"]}
    assert client.post("/internal/developer/agent/run", headers=headers, json=payload).status_code == 409
    assert agent.calls == []
    assert client.post("/internal/developer/preview/approve", headers=headers,
                       json={"preview_id": payload["preview_id"]}).status_code == 200
    started = client.post("/internal/developer/agent/run", headers=headers, json=payload)
    assert started.status_code == 200, started.text
    bypass = client.post("/internal/developer/run", headers=headers, json={
        "preview_id": payload["preview_id"], "dry_run": False, "commands": [], "file_writes": [],
    })
    assert bypass.status_code == 409
    monkeypatch.setattr("aitobuild.developer_isolation.time", lambda: 1e30)
    resumed = client.post("/internal/developer/agent/resume", headers=headers, json={
        "session_id": payload["session_id"], "approved": True,
        "request_id": started.json()["pending_approval_requests"][0]["request_id"],
    })
    assert resumed.status_code == 409
    assert "expired" in resumed.json()["detail"]
    assert len(agent.calls) == 1
    another = client.post("/internal/developer/agent/run", headers=headers, json={**payload, "session_id": "another-budget"})
    assert another.status_code == 409
    assert len(agent.calls) == 1


@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "rejection"])
def test_approved_task_closes_container_on_terminal_outcomes(test_config, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    closed: list[str] = []

    class RecordingContainer(app_module.ContainerSessionBashAdapter):
        def close_session(self, *, session_id: str) -> bool:
            closed.append(session_id)
            return True

    class TerminalAgent(_FakeDeveloperAgent):
        async def run(self, messages: Any, *, session: AgentSession | None = None) -> _FakeRunResult:
            if outcome == "error":
                raise RuntimeError("task failed")
            if outcome == "timeout":
                raise asyncio.TimeoutError("task timed out")
            return await super().run(messages, session=session)

    monkeypatch.setattr(app_module, "ContainerSessionBashAdapter", RecordingContainer)
    config = replace(test_config, developer=replace(test_config.developer, execution_mode="container_session"))
    agent = TerminalAgent(require_approval=outcome == "rejection")
    client = _create_app_with_fake_agent(config, monkeypatch, agent)
    headers = _internal_headers(config)
    bundle = build_developer_task_bundle(
        task_id="terminal-task", objective="Inspect the scope", acceptance_criteria=["Report status"],
        constraints=[], context_files=[],
    )
    preview = client.post("/internal/developer/preview", headers=headers, json={
        "github_event": "issues", "task_bundle": bundle.to_payload(),
    }).json()
    client.post("/internal/developer/preview/approve", headers=headers, json={"preview_id": preview["preview_id"]})
    response = client.post("/internal/developer/agent/run", headers=headers, json={
        "input": "Inspect", "session_id": "terminal-task", "preview_id": preview["preview_id"],
    })
    if outcome == "rejection":
        assert closed == []
        response = client.post("/internal/developer/agent/resume", headers=headers, json={
            "session_id": "terminal-task", "approved": False,
            "request_id": response.json()["pending_approval_requests"][0]["request_id"],
        })
    assert response.status_code == {"error": 500, "timeout": 504}.get(outcome, 200), response.text
    assert closed == ["terminal-task"]
    if outcome in {"error", "timeout"}:
        again = client.post("/internal/developer/agent/run", headers=headers, json={
            "input": "Do not reset the task", "session_id": "another-task", "preview_id": preview["preview_id"],
        })
        assert again.status_code == 409
        assert "aborted" in again.json()["detail"]


def test_internal_trigger_architect_scan_route(test_config) -> None:
    client = TestClient(create_app(test_config))
    response = client.post(
        "/internal/triggers",
        headers=_internal_headers(test_config),
        json={
            "origin": "manual_request",
            "event_type": "architect_scan_requested",
            "payload": {"repo": "aitobuild"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["route"] == "architect.proactive.scan"
    assert body["metadata"] is not None
    assert body["metadata"]["architect_scan"]["status"] in {"healthy", "issues_found"}


def test_scheduler_tick_endpoint_dispatches_events(test_config) -> None:
    client = TestClient(create_app(test_config))

    request_response = client.post(
        "/internal/triggers",
        headers=_internal_headers(test_config),
        json={
            "origin": "manual_request",
            "event_type": "meeting_requested",
            "payload": {
                "agenda": "Architecture alignment",
                "participants": ["Architect", "Developer"],
            },
        },
    )
    assert request_response.status_code == 200
    request_body = request_response.json()
    assert request_body["accepted"] is True
    assert request_body["route"] == "meeting.requested"
    meeting_id = request_body["metadata"]["meeting_id"]

    response = client.post(
        "/internal/scheduler/tick",
        headers=_internal_headers(test_config),
        json={"manual_scan": True, "manual_meeting": True, "meeting_id": meeting_id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["produced_count"] == 2
    routes = {entry["route"] for entry in body["dispatched"]}
    assert "architect.proactive.scan" in routes
    assert "meeting.bootstrap" in routes
    architect_entries = [entry for entry in body["dispatched"] if entry["route"] == "architect.proactive.scan"]
    assert architect_entries
    assert architect_entries[0]["metadata"]["architect_scan"]["status"] in {"healthy", "issues_found"}
    meeting_entries = [entry for entry in body["dispatched"] if entry["route"] == "meeting.bootstrap"]
    assert meeting_entries
    kickoff = meeting_entries[0]["metadata"]["meeting_kickoff"]
    assert kickoff["status"] in {"mock_started", "workflow_built"}


def test_meeting_requested_payload_validation(test_config) -> None:
    client = TestClient(create_app(test_config))
    response = client.post(
        "/internal/triggers",
        headers=_internal_headers(test_config),
        json={
            "origin": "manual_request",
            "event_type": "meeting_requested",
            "payload": {
                "agenda": "",
                "participants": ["Architect"],
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is False
    assert body["route"] == "meeting.invalid"


def test_internal_endpoints_require_auth(test_config) -> None:
    client = TestClient(create_app(test_config))

    trigger_response = client.post(
        "/internal/triggers",
        json={
            "origin": "manual_request",
            "event_type": "architect_scan_requested",
            "payload": {"repo": "aitobuild"},
        },
    )
    tick_response = client.post("/internal/scheduler/tick", json={"manual_scan": True})
    escalation_response = client.get("/internal/escalations")
    preview_create_response = client.post(
        "/internal/developer/preview",
        json={"github_event": "issues", "body": {}},
    )
    preview_approve_response = client.post(
        "/internal/developer/preview/approve",
        json={"preview_id": "dp-unknown"},
    )
    preview_list_response = client.get("/internal/developer/previews")
    session_start_response = client.post("/internal/developer/session/start", json={})
    session_stop_response = client.post(
        "/internal/developer/session/stop",
        json={"session_id": "demo"},
    )
    developer_run_response = client.post(
        "/internal/developer/run",
        json={"preview_id": "dp-unknown", "dry_run": True},
    )
    developer_agent_status_response = client.get("/internal/runtime/developer-agent")
    developer_agent_run_response = client.post(
        "/internal/developer/agent/run",
        json={"input": "hello"},
    )

    assert trigger_response.status_code == 401
    assert tick_response.status_code == 401
    assert escalation_response.status_code == 401
    assert preview_create_response.status_code == 401
    assert preview_approve_response.status_code == 401
    assert preview_list_response.status_code == 401
    assert session_start_response.status_code == 401
    assert session_stop_response.status_code == 401
    assert developer_run_response.status_code == 401
    assert developer_agent_status_response.status_code == 401
    assert developer_agent_run_response.status_code == 401


def test_escalations_endpoint_returns_deadline_escalations(test_config) -> None:
    client = TestClient(create_app(test_config))
    headers = _internal_headers(test_config)

    request_response = client.post(
        "/internal/triggers",
        headers=headers,
        json={
            "origin": "manual_request",
            "event_type": "meeting_requested",
            "payload": {
                "agenda": "Escalation flow",
                "participants": ["Architect", "Developer"],
                "deadline": "2000-01-01T00:00:00Z",
            },
        },
    )
    assert request_response.status_code == 200
    meeting_id = request_response.json()["metadata"]["meeting_id"]

    due_response = client.post(
        "/internal/triggers",
        headers=headers,
        json={
            "origin": "scheduler",
            "event_type": "meeting_due",
            "payload": {"meeting_id": meeting_id},
        },
    )
    assert due_response.status_code == 200
    assert due_response.json()["route"] == "escalation.route"

    escalations_response = client.get("/internal/escalations", headers=headers)
    assert escalations_response.status_code == 200
    body = escalations_response.json()
    assert body["count"] >= 1
    assert any(event["category"] == "deadline_exceeded" for event in body["events"])
