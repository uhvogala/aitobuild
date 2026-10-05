from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from aitobuild.tools import shell as shell_tools
from aitobuild.tools import shell_runtime
from aitobuild.tools.bash import BashResult, ContainerSessionBashAdapter


def test_shell_reads_bounded_incremental_output_and_exit_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    shell_id = "sh-0123456789ab"
    directory = tmp_path / ".aitobuild" / "shells" / shell_id
    directory.mkdir(parents=True)
    (directory / "metadata.json").write_text(json.dumps({"shell_id": shell_id}))
    (directory / "output.log").write_bytes(b"a" * 700 + b"final output\n")
    (directory / "exit-code").write_text("7")
    first = shell_runtime.dispatch({"action": "read", "shell_id": shell_id, "cursor": 0, "max_output_chars": 256})
    assert first["ok"] and first["exit_code"] == 7
    assert len(first["output"]) == 256 and first["next_cursor"] == 256
    assert first["output_truncated"]
    second = shell_runtime.dispatch({"action": "read", "shell_id": shell_id, "cursor": first["next_cursor"]})
    assert second["output"].endswith("final output\n")
    assert second["next_cursor"] == 713
    assert not second["output_truncated"]


def test_shell_recovers_lost_stdout_without_restarting_job(tmp_path: Path, monkeypatch) -> None:
    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path, image="aitobuild-developer:local",
        container_workdir="/workspace", container_name_prefix="aitobuild-test",
    )
    calls = []

    def run(**kwargs):
        calls.append(kwargs["command"])
        return BashResult("probe", 0, "" if len(calls) == 1 else json.dumps({
            "ok": True, "shell_id": "sh-0123456789ab", "exit_code": 7,
        }), "")

    monkeypatch.setattr(adapter, "run", run)
    result = shell_tools.shell_request(adapter, "dev-one", {"action": "start", "command": "exit 7"})
    assert result["ok"] and result["exit_code"] == 7
    assert len(calls) == 2
    assert "--reply-file" in calls[0]
    assert "shell_runtime.py" not in calls[1]


@pytest.mark.parametrize("payload, code", [
    ({"action": "read", "shell_id": "../other-dev"}, "invalid_shell_id"),
    ({"action": "read", "shell_id": "sh-0123456789ab"}, "shell_not_found"),
    ({"action": "start", "wait_seconds": 100}, "invalid_limits"),
    ({"kind": "processes", "action": "signal", "process_handle": "42"}, "invalid_process_handle"),
])
def test_shell_errors_explain_recovery(tmp_path: Path, monkeypatch, payload, code) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = shell_runtime.dispatch(payload)
    assert result["ok"] is False
    assert result["error_code"] == code
    assert result["error"] and result["next_action"]


def test_process_signal_rejects_stale_pid(monkeypatch) -> None:
    signals = []
    fake_process = SimpleNamespace(
        pid=42, uids=lambda: SimpleNamespace(effective=os.geteuid()),
        create_time=lambda: 1000.5, send_signal=signals.append,
    )
    monkeypatch.setattr(shell_runtime.psutil, "Process", lambda pid: fake_process)
    result = shell_runtime.dispatch({
        "kind": "processes", "action": "signal", "process_handle": "42:999.500000",
    })
    assert result["error_code"] == "stale_process_handle"
    assert signals == []


def test_shell_agent_tools_are_compact_approved_and_recoverable(tmp_path: Path, monkeypatch) -> None:
    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path, image="aitobuild-developer:local",
        container_workdir="/workspace", container_name_prefix="aitobuild-test",
    )
    requests = []

    def fake_request(adapter, session_id, request):
        requests.append((session_id, request))
        return {"ok": True, "shell_id": "sh-0123456789ab", "status": "running", "exit_code": None}

    monkeypatch.setattr(shell_tools, "shell_request", fake_request)
    terminal, processes = shell_tools.build_shell_tools(adapter, lambda _: "dev-one")
    started = terminal(action="start", command="python -m pytest")
    terminal(action="read", shell_id=started["shell_id"], cursor=123)
    processes(action="list", query="pytest")
    assert len(requests) == 3 and all(session_id == "dev-one" for session_id, _ in requests)
    assert requests[1][1]["cursor"] == 123
    assert "WITHOUT restarting" in terminal.description
    assert "next_action" in processes.description
    assert terminal.approval_mode == processes.approval_mode == "always_require"


def test_shell_backend_error_does_not_claim_success(tmp_path: Path, monkeypatch) -> None:
    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path, image="aitobuild-developer:local",
        container_workdir="/workspace", container_name_prefix="aitobuild-test",
    )
    monkeypatch.setattr(adapter, "run", lambda **kwargs: BashResult("probe", 124, "", "deadline"))
    result = shell_tools.shell_request(adapter, "dev-one", {"action": "list"})
    assert result["ok"] is False
    assert "List shells" in result["next_action"]