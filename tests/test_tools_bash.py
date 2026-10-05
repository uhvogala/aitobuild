from __future__ import annotations

from pathlib import Path

import aitobuild.tools.bash as bash_module
from aitobuild.tools.bash import BashResult, ContainerSessionBashAdapter


def _fake_success(command: list[str]) -> BashResult:
    return BashResult(command=" ".join(command), exit_code=0, stdout="ok", stderr="")


def test_container_session_adapter_uses_bind_source_path(monkeypatch) -> None:
    recorded: list[list[str]] = []

    def fake_run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
        del timeout_seconds
        recorded.append(command)
        if command[:3] == ["docker", "ps", "--format"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        if command[:4] == ["docker", "ps", "-a", "--format"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)

    adapter = ContainerSessionBashAdapter(
        workspace_root=Path("/workspace/in-container"),
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        bind_source_path="/host/workspace",
        timeout_seconds=5,
    )

    adapter.create_session(session_id="demo")

    run_command = next(command for command in recorded if command[:2] == ["docker", "run"])
    mount_index = run_command.index("-v")
    assert run_command[mount_index + 1] == "/host/workspace:/workspace"


def test_container_session_adapter_defaults_to_workspace_root(monkeypatch) -> None:
    recorded: list[list[str]] = []

    def fake_run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
        del timeout_seconds
        recorded.append(command)
        if command[:3] == ["docker", "ps", "--format"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        if command[:4] == ["docker", "ps", "-a", "--format"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)

    adapter = ContainerSessionBashAdapter(
        workspace_root=Path("/workspace/in-container"),
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        timeout_seconds=5,
    )

    adapter.create_session(session_id="demo")

    run_command = next(command for command in recorded if command[:2] == ["docker", "run"])
    mount_index = run_command.index("-v")
    assert run_command[mount_index + 1] == "/workspace/in-container:/workspace"


def test_container_session_adapter_runs_as_current_user_by_default(monkeypatch) -> None:
    recorded: list[list[str]] = []

    def fake_run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
        del timeout_seconds
        recorded.append(command)
        if command[:3] == ["docker", "ps", "--format"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        if command[:4] == ["docker", "ps", "-a", "--format"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)
    monkeypatch.setattr(bash_module.os, "getuid", lambda: 1000)
    monkeypatch.setattr(bash_module.os, "getgid", lambda: 1001)

    adapter = ContainerSessionBashAdapter(
        workspace_root=Path("/workspace/in-container"),
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        timeout_seconds=5,
    )

    adapter.create_session(session_id="demo")

    run_command = next(command for command in recorded if command[:2] == ["docker", "run"])
    assert "--user" in run_command
    user_index = run_command.index("--user")
    assert run_command[user_index + 1] == "1000:1001"


def test_container_session_adapter_allows_root_opt_out(monkeypatch) -> None:
    recorded: list[list[str]] = []

    def fake_run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
        del timeout_seconds
        recorded.append(command)
        if command[:3] == ["docker", "ps", "--format"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        if command[:4] == ["docker", "ps", "-a", "--format"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)
    monkeypatch.setattr(bash_module.os, "getuid", lambda: 1000)
    monkeypatch.setattr(bash_module.os, "getgid", lambda: 1001)

    adapter = ContainerSessionBashAdapter(
        workspace_root=Path("/workspace/in-container"),
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        run_as_current_user=False,
        timeout_seconds=5,
    )

    adapter.create_session(session_id="demo")

    run_command = next(command for command in recorded if command[:2] == ["docker", "run"])
    assert "--user" not in run_command


def test_container_session_adapter_reuses_existing_running_container(monkeypatch) -> None:
    recorded: list[list[str]] = []

    def fake_run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
        del timeout_seconds
        recorded.append(command)
        if command[:3] == ["docker", "ps", "--format"]:
            return BashResult(
                command=" ".join(command),
                exit_code=0,
                stdout="aitobuild-test-demo\n",
                stderr="",
            )
        if command[:4] == ["docker", "ps", "-a", "--format"]:
            return BashResult(
                command=" ".join(command),
                exit_code=0,
                stdout="aitobuild-test-demo\n",
                stderr="",
            )
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)

    adapter = ContainerSessionBashAdapter(
        workspace_root=Path("/workspace/in-container"),
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        timeout_seconds=5,
    )

    session_id, container_name = adapter.create_session(session_id="demo")

    assert session_id == "demo"
    assert container_name == "aitobuild-test-demo"
    assert not any(command[:2] == ["docker", "run"] for command in recorded)


def test_container_session_adapter_close_session_stops_untracked_running_container(monkeypatch) -> None:
    recorded: list[list[str]] = []

    def fake_run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
        del timeout_seconds
        recorded.append(command)
        if command == ["docker", "ps", "--format", "{{.Names}}"]:
            return BashResult(
                command=" ".join(command),
                exit_code=0,
                stdout="aitobuild-test-demo\n",
                stderr="",
            )
        if command == ["docker", "ps", "-a", "--format", "{{.Names}}"]:
            return BashResult(
                command=" ".join(command),
                exit_code=0,
                stdout="aitobuild-test-demo\n",
                stderr="",
            )
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)

    adapter = ContainerSessionBashAdapter(
        workspace_root=Path("/workspace/in-container"),
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        timeout_seconds=5,
    )

    closed = adapter.close_session(session_id="demo")

    assert closed is True
    assert ["docker", "stop", "--time", "5", "aitobuild-test-demo"] in recorded
