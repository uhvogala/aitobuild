from __future__ import annotations

from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import aitobuild.tools.bash as bash_module
from aitobuild.tools.bash import BashResult, ContainerSessionBashAdapter


def _fake_success(command: list[str]) -> BashResult:
    return BashResult(command=" ".join(command), exit_code=0, stdout="ok", stderr="")


def test_prepared_checkout_binding_skips_service_seed_and_requires_profile(monkeypatch, tmp_path: Path) -> None:
    recorded = []

    def fake_run_process(*, command, timeout_seconds):
        recorded.append(command)
        if command[:2] == ["docker", "ps"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)
    target = tmp_path / ".aitobuild/developer/deliveries/task/repo"
    (target / ".git").mkdir(parents=True)
    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path, image="prepared", container_workdir="/workspace",
        container_name_prefix="test-target", bind_source_path="/host/service",
    )
    adapter.bind_session_workspace(session_id="target-task", workspace=target)
    assert adapter.get_workspace_root("target-task") == target
    with pytest.raises(PermissionError, match="profile"):
        adapter.create_session(session_id="target-task")
    adapter.create_session(session_id="target-task", read_only_workspace=True, deadline=10000000000.0)
    command = next(command for command in recorded if command[:3] == ["docker", "run", "-d"])
    assert command[command.index("-v") + 1] == "/host/service/.aitobuild/developer/deliveries/task/repo:/workspace:ro"
    assert command[command.index("--network") + 1] == "none" and "--read-only" in command
    assert not (tmp_path / ".aitobuild/workspaces/target-task").exists()
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="another target"):
        adapter.bind_session_workspace(session_id="target-task", workspace=other)


@pytest.mark.parametrize("mismatch", [None, "source", "resources", "expiry"])
def test_prepared_container_reuse_checks_the_exact_host_checkout(monkeypatch, tmp_path, mismatch) -> None:
    target = tmp_path / ".aitobuild/developer/deliveries/task/repo"
    (target / ".git").mkdir(parents=True)
    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path, image="prepared", container_workdir="/workspace",
        container_name_prefix="test-target", bind_source_path="/host/service",
    )
    adapter.bind_session_workspace(session_id="target-task", workspace=target)
    container = f"{adapter._container_name_prefix}-target-task"
    commands = []

    def fake_run_process(*, command, timeout_seconds):
        commands.append(command)
        if command[:2] == ["docker", "ps"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout=container, stderr="")
        if command[:3] == ["docker", "inspect", container]:
            config = {"HostConfig": {"ReadonlyRootfs": True, "NetworkMode": "none", "CapDrop": ["ALL"],
                                      "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 256,
                                      "Memory": 0 if mismatch == "resources" else 1024 ** 3, "NanoCpus": 2_000_000_000},
                      "Mounts": [{"Destination": "/workspace", "RW": False, "Type": "bind",
                                  "Source": "/host/service" if mismatch == "source" else "/host/service/.aitobuild/developer/deliveries/task/repo"}],
                      "Config": {"Cmd": ["python", "-c", "import sys,time; time.sleep(max(0, float(sys.argv[1])-time.time()))",
                                         "10000000100.0" if mismatch == "expiry" else "10000000000.0"]}}
            return BashResult(command=" ".join(command), exit_code=0, stdout=json.dumps(config), stderr="")
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)
    if mismatch is not None:
        with pytest.raises(PermissionError, match="profile"):
            adapter.create_session(session_id="target-task", read_only_workspace=True, deadline=10000000000.0)
    else:
        assert adapter.create_session(session_id="target-task", read_only_workspace=True, deadline=10000000000.0) == ("target-task", container)
    assert not any(command[:3] == ["docker", "run", "-d"] for command in commands)


def test_parallel_container_starts_share_one_creation(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
        del timeout_seconds
        recorded.append(command)
        if command[:2] == ["docker", "ps"]:
            return BashResult(command=" ".join(command), exit_code=0, stdout="", stderr="")
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)
    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path, image="python:3.14-slim", container_workdir="/workspace",
        container_name_prefix="aitobuild-test", timeout_seconds=5,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda unused: adapter.create_session(session_id="parallel"), range(8)))
    assert len(set(results)) == 1
    assert sum(command[:3] == ["docker", "run", "-d"] for command in recorded) == 1


@pytest.mark.parametrize("read_only_workspace", [False, True])
def test_container_session_adapter_uses_bind_source_path(monkeypatch, tmp_path: Path, read_only_workspace: bool) -> None:
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
        workspace_root=tmp_path,
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        bind_source_path="/host/workspace",
        timeout_seconds=5,
    )

    adapter.create_session(session_id="demo", read_only_workspace=read_only_workspace, deadline=10000000000.0)

    run_command = next(command for command in recorded if command[:3] == ["docker", "run", "-d"])
    mount_index = run_command.index("-v")
    assert run_command[mount_index + 1] == "/host/workspace/.aitobuild/workspaces/demo:/workspace" + (":ro" if read_only_workspace else "")
    assert f"volume-subpath={adapter.data_volume_subpath('demo')}" in run_command[run_command.index("--mount") + 1]
    if read_only_workspace:
        assert run_command[run_command.index("--network") + 1] == "none"
        assert "--read-only" in run_command
        assert run_command[run_command.index("--cap-drop") + 1] == "ALL"
        assert run_command[-1] == "10000000000.0"
        with pytest.raises(PermissionError, match="profile"):
            adapter.create_session(session_id="demo", read_only_workspace=True, deadline=10000000000.0)


def test_container_session_adapter_defaults_to_workspace_root(monkeypatch, tmp_path: Path) -> None:
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
        workspace_root=tmp_path,
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        timeout_seconds=5,
    )

    adapter.create_session(session_id="demo")

    run_command = next(command for command in recorded if command[:3] == ["docker", "run", "-d"])
    mount_index = run_command.index("-v")
    assert run_command[mount_index + 1] == f"{tmp_path}/.aitobuild/workspaces/demo:/workspace"


def test_container_session_adapter_runs_as_current_user_by_default(monkeypatch, tmp_path: Path) -> None:
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
        workspace_root=tmp_path,
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        timeout_seconds=5,
    )

    adapter.create_session(session_id="demo")

    run_command = next(command for command in recorded if command[:3] == ["docker", "run", "-d"])
    assert "--user" in run_command
    user_index = run_command.index("--user")
    assert run_command[user_index + 1] == "1000:1001"


def test_container_session_adapter_allows_root_opt_out(monkeypatch, tmp_path: Path) -> None:
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
        workspace_root=tmp_path,
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        run_as_current_user=False,
        timeout_seconds=5,
    )

    adapter.create_session(session_id="demo")

    run_command = next(command for command in recorded if command[:3] == ["docker", "run", "-d"])
    assert run_command[run_command.index("--user") + 1] == "0:0"


def test_container_session_adapter_reuses_existing_running_container(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
        del timeout_seconds
        recorded.append(command)
        if command[:3] == ["docker", "ps", "--format"]:
            return BashResult(
                command=" ".join(command),
                exit_code=0,
                stdout=f"{adapter._container_name_prefix}-demo\n",
                stderr="",
            )
        if command[:4] == ["docker", "ps", "-a", "--format"]:
            return BashResult(
                command=" ".join(command),
                exit_code=0,
                stdout=f"{adapter._container_name_prefix}-demo\n",
                stderr="",
            )
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)

    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path,
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        timeout_seconds=5,
    )

    session_id, container_name = adapter.create_session(session_id="demo")

    assert session_id == "demo"
    assert container_name == f"{adapter._container_name_prefix}-demo"
    assert not any(command[:2] == ["docker", "run"] for command in recorded)


def test_container_session_adapter_close_session_stops_untracked_running_container(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
        del timeout_seconds
        recorded.append(command)
        if command == ["docker", "ps", "--format", "{{.Names}}"]:
            return BashResult(
                command=" ".join(command),
                exit_code=0,
                stdout=f"{adapter._container_name_prefix}-demo\n",
                stderr="",
            )
        if command == ["docker", "ps", "-a", "--format", "{{.Names}}"]:
            return BashResult(
                command=" ".join(command),
                exit_code=0,
                stdout=f"{adapter._container_name_prefix}-demo\n",
                stderr="",
            )
        return _fake_success(command)

    monkeypatch.setattr(bash_module, "_run_process", fake_run_process)

    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path,
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        timeout_seconds=5,
    )

    closed = adapter.close_session(session_id="demo")

    assert closed is True
    assert ["docker", "stop", "--time", "5", f"{adapter._container_name_prefix}-demo"] in recorded


def test_developer_workspaces_are_independent_and_seeded_once(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "example.txt").write_text("seed", encoding="utf-8")
    (tmp_path / ".env.local").write_text("private", encoding="utf-8")
    (tmp_path / ".env.simulation.example").write_text("template", encoding="utf-8")
    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path, image="python:3.14-slim",
        container_workdir="/workspace", container_name_prefix="aitobuild-test",
    )
    adapter._prepare_workspace("dev-one")
    adapter._prepare_workspace("dev-two")
    first = adapter.get_workspace_root("dev-one")
    second = adapter.get_workspace_root("dev-two")
    (first / "src" / "example.txt").write_text("first developer", encoding="utf-8")
    adapter._prepare_workspace("dev-one")
    assert (first / "src" / "example.txt").read_text() == "first developer"
    assert (second / "src" / "example.txt").read_text() == "seed"
    assert (tmp_path / "src" / "example.txt").read_text() == "seed"
    assert not (first / ".env.local").exists()
    assert (first / ".env.simulation.example").read_text() == "template"
    assert not (first / ".aitobuild").exists()


@pytest.mark.parametrize("session_id", ["DEV-one", "dev_one", "../dev-one", "x" * 49])
def test_developer_identity_is_not_silently_aliased(session_id: str) -> None:
    with pytest.raises(ValueError, match="never silently"):
        bash_module._normalize_session_id(session_id)
