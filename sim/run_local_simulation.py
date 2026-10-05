from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from hmac import new
from pathlib import Path
from typing import Any, Iterator

from fastapi.testclient import TestClient

from aitobuild.app import create_app
from aitobuild.config import AppConfig, load_config


def emit_live(message: str, *, enabled: bool, payload: dict[str, Any] | None = None) -> None:
    if not enabled:
        return

    print(f"[live] {message}", flush=True)
    if payload is not None:
        print(json.dumps(payload, indent=2), flush=True)


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Env file not found: {path}")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def _rebase_session_bind_path_for_sandbox(*, workspace_root: Path, sandbox_repo: Path) -> None:
    bind_path = os.getenv("AITOBUILD_DEVELOPER_SESSION_CONTAINER_BIND_PATH")
    if bind_path is None or not bind_path.strip():
        return

    try:
        relative_repo = sandbox_repo.resolve().relative_to(workspace_root.resolve())
    except ValueError:
        return

    rebound = Path(bind_path.strip()) / relative_repo
    os.environ["AITOBUILD_DEVELOPER_SESSION_CONTAINER_BIND_PATH"] = str(rebound)


@contextmanager
def copied_fixture_repo(fixture_repo: Path) -> Iterator[tuple[Path, Path]]:
    # Container-session mode uses Docker bind mounts. Creating the sandbox under
    # the workspace keeps host and devcontainer paths aligned so mounted files
    # are visible inside session containers.
    run_root = Path(tempfile.mkdtemp(prefix="aitobuild-sim-", dir=str(fixture_repo.parent)))
    repo_root = run_root / "repo"
    shutil.copytree(fixture_repo, repo_root)

    old_cwd = Path.cwd()
    os.chdir(repo_root)
    try:
        yield run_root, repo_root
    finally:
        os.chdir(old_cwd)


def read_json(path: Path) -> dict[str, Any]:
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError(f"JSON object expected in {path}")
    return content


def hmac_signature(secret: str, payload: bytes) -> str:
    digest = new(secret.encode("utf-8"), payload, sha256).hexdigest()
    return f"sha256={digest}"


def internal_headers(config: AppConfig) -> dict[str, str]:
    token = config.security.internal_api_token or ""
    return {"X-Internal-Token": token}


def call_checked(client: TestClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = client.request(method=method, url=url, **kwargs)
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}

    if response.status_code >= 400:
        raise RuntimeError(f"{method} {url} failed ({response.status_code}): {body}")

    if isinstance(body, dict):
        return body
    raise RuntimeError(f"{method} {url} returned non-object JSON: {body}")


def assert_live_model_config(config: AppConfig) -> None:
    if not config.runtime.foundry_endpoint:
        raise RuntimeError(
            "Live-model mode requires AITOBUILD_FOUNDRY_ENDPOINT to be set in env file"
        )
    if config.runtime.allow_mock_model:
        raise RuntimeError(
            "Live-model mode requires AITOBUILD_ALLOW_MOCK_MODEL=false"
        )


def run_simulation(
    *,
    root: Path,
    env_file: Path,
    output_file: Path | None,
    use_session: bool,
    requested_session_id: str | None,
    live_model: bool,
    agent_prompt: str,
    auto_approve_agent_tools: bool,
    agent_max_approval_rounds: int,
    live_output: bool,
) -> int:
    load_env_file(env_file)
    if use_session:
        os.environ["AITOBUILD_DEVELOPER_EXECUTION_MODE"] = "container_session"
    if live_model:
        os.environ["AITOBUILD_ALLOW_MOCK_MODEL"] = "false"
    if live_output:
        os.environ["AITOBUILD_DEVELOPER_ENABLE_AGENT_LIVE_LOGS"] = "true"

    caller_cwd = Path.cwd()

    fixture_repo = root / "repo-fixture"
    payload_dir = root / "payloads"

    with copied_fixture_repo(fixture_repo) as (run_root, repo_root):
        _rebase_session_bind_path_for_sandbox(workspace_root=root.parent, sandbox_repo=repo_root)

        config = load_config()
        if live_model:
            assert_live_model_config(config)

        client = TestClient(create_app(config))
        headers = internal_headers(config)

        runtime_status = call_checked(client, "GET", "/internal/runtime/developer-agent", headers=headers)
        emit_live("runtime status", enabled=live_output, payload=runtime_status)
        if live_model:
            if runtime_status.get("runtime_mode") != "foundry":
                raise RuntimeError(
                    "Live-model mode expected runtime_mode=foundry; check model provider setup"
                )
            if not bool(runtime_status.get("ready_for_run")):
                raise RuntimeError(
                    "Live-model mode requires a native runnable Developer agent handle"
                )

        webhook_payload = read_json(payload_dir / "webhook.json")
        webhook_body = json.dumps(webhook_payload).encode("utf-8")
        webhook_delivery = "sim-delivery-001"

        webhook_initial = call_checked(
            client,
            "POST",
            "/webhook",
            content=webhook_body,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": webhook_delivery,
                "X-Hub-Signature-256": hmac_signature(config.webhook_secret, webhook_body),
            },
        )
        emit_live("webhook initial", enabled=live_output, payload=webhook_initial)

        preview_create = read_json(payload_dir / "preview-create.json")
        preview_created = call_checked(
            client,
            "POST",
            "/internal/developer/preview",
            headers=headers,
            json=preview_create,
        )
        preview_id = str(preview_created["preview_id"])
        emit_live("preview created", enabled=live_output, payload=preview_created)

        preview_approved = call_checked(
            client,
            "POST",
            "/internal/developer/preview/approve",
            headers=headers,
            json={"preview_id": preview_id},
        )
        emit_live("preview approved", enabled=live_output, payload=preview_approved)

        session_started: dict[str, Any] | None = None
        session_stopped: dict[str, Any] | None = None
        active_session_id: str | None = None
        if use_session:
            start_payload: dict[str, Any] = {}
            if requested_session_id is not None:
                start_payload["session_id"] = requested_session_id

            session_started = call_checked(
                client,
                "POST",
                "/internal/developer/session/start",
                headers=headers,
                json=start_payload,
            )
            active_session_id = str(session_started.get("session_id", "")).strip() or None
            if active_session_id is None:
                raise RuntimeError("Session start did not return a session_id")
            emit_live("session started", enabled=live_output, payload=session_started)

        webhook_replayed = call_checked(
            client,
            "POST",
            "/webhook",
            content=webhook_body,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": str(preview_create.get("delivery_id", "sim-delivery-002")),
                "X-Hub-Signature-256": hmac_signature(config.webhook_secret, webhook_body),
            },
        )
        emit_live("webhook replayed", enabled=live_output, payload=webhook_replayed)

        developer_run_payload = read_json(payload_dir / "developer-run.json")
        developer_run_payload["preview_id"] = preview_id
        if active_session_id is not None:
            developer_run_payload["session_id"] = active_session_id
        commands_raw = developer_run_payload.get("commands")
        if not isinstance(commands_raw, list) or not all(isinstance(item, str) for item in commands_raw):
            raise RuntimeError("payloads/developer-run.json must define commands as a list of strings")

        developer_run_second: dict[str, Any] | None = None
        agent_run_result: dict[str, Any] | None = None
        session_cleanup: dict[str, Any] | None = None
        try:
            developer_run_first = call_checked(
                client,
                "POST",
                "/internal/developer/run",
                headers=headers,
                json=developer_run_payload,
            )
            emit_live("developer run #1", enabled=live_output, payload=developer_run_first)

            if active_session_id is not None:
                developer_run_second = call_checked(
                    client,
                    "POST",
                    "/internal/developer/run",
                    headers=headers,
                    json={
                        "preview_id": preview_id,
                        "session_id": active_session_id,
                        "dry_run": False,
                        "approved": True,
                        "commands": [],
                        "file_writes": [],
                    },
                )
                emit_live("developer run #2", enabled=live_output, payload=developer_run_second)

            if live_model or bool(runtime_status.get("ready_for_run")):
                agent_run_payload: dict[str, Any] = {
                    "input": agent_prompt,
                    "create_session": True,
                    "auto_approve_tools": auto_approve_agent_tools,
                    "max_approval_rounds": agent_max_approval_rounds,
                }
                if active_session_id is not None:
                    agent_run_payload["session_id"] = active_session_id

                agent_run_result = call_checked(
                    client,
                    "POST",
                    "/internal/developer/agent/run",
                    headers=headers,
                    json=agent_run_payload,
                )
                emit_live("developer agent run", enabled=live_output, payload=agent_run_result)
        finally:
            if active_session_id is not None:
                try:
                    session_stopped = call_checked(
                        client,
                        "POST",
                        "/internal/developer/session/stop",
                        headers=headers,
                        json={"session_id": active_session_id},
                    )
                    emit_live("session stopped", enabled=live_output, payload=session_stopped)
                except Exception as exc:
                    session_stopped = {
                        "session_id": active_session_id,
                        "closed": False,
                        "error": str(exc),
                    }
                    emit_live("session stop failed", enabled=live_output, payload=session_stopped)

                try:
                    session_cleanup = call_checked(
                        client,
                        "POST",
                        "/internal/developer/session/stop-all",
                        headers=headers,
                    )
                    emit_live("session cleanup", enabled=live_output, payload=session_cleanup)
                except Exception as exc:
                    session_cleanup = {
                        "closed_count": 0,
                        "failed_count": 1,
                        "error": str(exc),
                    }
                    emit_live("session cleanup failed", enabled=live_output, payload=session_cleanup)

        summary = {
            "simulation_root": str(run_root),
            "sandbox_repo": str(repo_root),
            "runtime_status": runtime_status,
            "live_model_mode": live_model,
            "webhook_initial": webhook_initial,
            "preview_created": preview_created,
            "preview_approved": preview_approved,
            "session_mode": use_session,
            "session_started": session_started,
            "active_session_id": active_session_id,
            "webhook_replayed": webhook_replayed,
            "developer_run": developer_run_first,
            "developer_run_second": developer_run_second,
            "session_stopped": session_stopped,
            "session_cleanup": session_cleanup,
            "developer_agent_run": agent_run_result,
            "generated_file_exists": (repo_root / "src/demo_app/generated_note.txt").exists(),
        }

        if output_file is None:
            output_target = run_root / "simulation-report.json"
        else:
            output_target = output_file
            if not output_target.is_absolute():
                output_target = caller_cwd / output_target

        output_target.parent.mkdir(parents=True, exist_ok=True)
        output_target.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        print(json.dumps(summary, indent=2))
        print(f"\nSimulation report written to: {output_target}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local aitobuild simulation without GitHub.")
    parser.add_argument(
        "--env-file",
        default="sim/.env.simulation",
        help="Path to env file containing required AITOBUILD_* values.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional JSON report output path.",
    )
    parser.add_argument(
        "--session",
        action="store_true",
        help="Run developer execution using container_session lifecycle endpoints.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Optional explicit session id for --session mode.",
    )
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="Require real model runtime (foundry mode, no mock fallback) and run developer agent.",
    )
    parser.add_argument(
        "--agent-prompt",
        default="Read the repository and summarize current work.",
        help="Prompt used for /internal/developer/agent/run.",
    )
    parser.add_argument(
        "--auto-approve-agent-tools",
        action="store_true",
        help="Auto-approve tool calls during /internal/developer/agent/run.",
    )
    parser.add_argument(
        "--agent-max-approval-rounds",
        type=int,
        default=12,
        help="Max auto-approval rounds sent to /internal/developer/agent/run (1..20).",
    )
    parser.add_argument(
        "--live-output",
        action="store_true",
        help="Print step-by-step simulation responses as they happen.",
    )

    args = parser.parse_args()
    root = Path(__file__).resolve().parent

    env_file = Path(args.env_file)
    if not env_file.is_absolute():
        env_file = Path.cwd() / env_file

    output_file = Path(args.output) if args.output else None

    if args.agent_max_approval_rounds < 1 or args.agent_max_approval_rounds > 20:
        raise ValueError("--agent-max-approval-rounds must be within 1..20")

    return run_simulation(
        root=root,
        env_file=env_file,
        output_file=output_file,
        use_session=bool(args.session),
        requested_session_id=args.session_id,
        live_model=bool(args.live_model),
        agent_prompt=str(args.agent_prompt),
        auto_approve_agent_tools=bool(args.auto_approve_agent_tools),
        agent_max_approval_rounds=int(args.agent_max_approval_rounds),
        live_output=bool(args.live_output),
    )


if __name__ == "__main__":
    raise SystemExit(main())
