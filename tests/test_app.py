from __future__ import annotations

from dataclasses import replace
import asyncio
import hmac
import json
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

from agent_framework import AgentSession, FileSessionStore
from fastapi.testclient import TestClient
import pytest

import aitobuild.app as app_module
from aitobuild.app import create_app
from aitobuild.runtime import FrameworkAvailability, FrameworkBindings, RuntimeBootstrap


def _signature(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
    return f"sha256={digest}"


def _internal_headers(test_config) -> dict[str, str]:
    token = test_config.security.internal_api_token or ""
    return {"X-Internal-Token": token}


_FakeSession = AgentSession


class _FakeApprovalRequest:
    def __init__(self) -> None:
        self.request_id = "req-1"
        self.function_call = SimpleNamespace(
            name="developer_run_command",
            arguments={"command": "pytest --version"},
        )

    def to_function_approval_response(self, approved: bool) -> dict[str, Any]:
        return {
            "approved": approved,
            "request_id": self.request_id,
        }


class _FakeRunResult:
    def __init__(self, *, text: str, user_input_requests: list[Any], response_id: str | None = None) -> None:
        self.text = text
        self.user_input_requests = user_input_requests
        self.response_id = response_id


class _FakeDeveloperAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, str | None]] = []

    def create_session(self, *, session_id: str | None = None) -> _FakeSession:
        return _FakeSession(session_id=session_id or "fake-session")

    async def run(self, messages: Any, *, session: _FakeSession | None = None) -> _FakeRunResult:
        self.calls.append((messages, session.session_id if session is not None else None))
        if session is not None:
            session.state["turn_count"] = session.state.get("turn_count", 0) + 1

        if not isinstance(messages, str):
            return _FakeRunResult(text="command executed", user_input_requests=[], response_id="resp-2")

        return _FakeRunResult(
            text="approval needed",
            user_input_requests=[_FakeApprovalRequest()],
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
    first = _create_app_with_fake_agent(test_config, monkeypatch, _FakeDeveloperAgent())
    response = first.post(
        "/internal/developer/agent/run", headers=_internal_headers(test_config),
        json={"input": "First turn", "session_id": "dev-one"},
    )
    assert response.status_code == 200, response.text
    restarted = _create_app_with_fake_agent(test_config, monkeypatch, _FakeDeveloperAgent())
    response = restarted.post(
        "/internal/developer/agent/run", headers=_internal_headers(test_config),
        json={"input": "Second turn", "session_id": "dev-one"},
    )
    assert response.status_code == 200
    restored = asyncio.run(FileSessionStore(test_config.developer.state_dir + "/sessions").get("dev-one"))
    assert restored is not None
    assert restored.state["turn_count"] == 2


def test_app_fails_fast_when_browser_enabled_without_container_session(test_config) -> None:
    invalid_config = replace(
        test_config, developer=replace(test_config.developer, enable_browser=True),
    )
    with pytest.raises(ValueError):
        create_app(invalid_config)


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
