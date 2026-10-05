from __future__ import annotations

from pathlib import Path

import pytest

from aitobuild.agent_tools import DeveloperToolContext, build_role_tools
from aitobuild.policy import AgentRole
from aitobuild.tools import MockBashAdapter, MockFilesystemAdapter
from aitobuild.tools.bash import BashResult
from aitobuild.tools.bash import ContainerSessionBashAdapter


def _developer_tools(tmp_path: Path):
    context = DeveloperToolContext(
        bash_adapter=MockBashAdapter(),
        filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=tmp_path,
        require_human_approval_for_repo_writes=False,
        container_session_adapter=None,
    )
    role_tools = build_role_tools(context=context)
    return role_tools["developer"]


def test_build_role_tools_includes_developer_toolset(tmp_path: Path) -> None:
    tools = _developer_tools(tmp_path)
    assert len(tools) == 6


def test_developer_tool_read_write_roundtrip(tmp_path: Path) -> None:
    run_command, read_file, write_file, _apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_result = write_file("tests/example.txt", "hello", approved=True)
    assert write_result["written"] is True

    content = read_file("tests/example.txt")
    assert content == "hello"

    command_result = run_command("uv run pytest", session_id=None)
    assert command_result["exit_code"] == 0


def test_developer_session_tools_fail_when_container_mode_disabled(tmp_path: Path) -> None:
    _run_command, _read_file, _write_file, _apply_patch, start_session, stop_session = _developer_tools(
        tmp_path
    )

    with pytest.raises(ValueError):
        start_session("demo")

    with pytest.raises(ValueError):
        stop_session("demo")


def test_container_session_adapter_requires_session_id(tmp_path: Path) -> None:
    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path,
        image="python:3.14-slim",
        container_workdir="/workspace",
        container_name_prefix="aitobuild-test",
        timeout_seconds=1,
    )

    with pytest.raises(ValueError):
        adapter.run(role=AgentRole.DEVELOPER, command="echo hi", session_id=None)


def test_build_role_tools_fails_fast_when_mcp_enabled_without_adapter(tmp_path: Path) -> None:
    context = DeveloperToolContext(
        bash_adapter=MockBashAdapter(),
        filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=tmp_path,
        require_human_approval_for_repo_writes=False,
        container_session_adapter=None,
        mcp_tool_adapter=None,
        enable_mcp_adapters=True,
    )

    with pytest.raises(RuntimeError):
        build_role_tools(context=context)


def test_developer_tools_auto_create_and_reuse_container_session(tmp_path: Path) -> None:
    class _SessionAwareBashAdapter:
        def __init__(self) -> None:
            self.session_ids: list[str | None] = []

        def run(self, *, role: AgentRole, command: str, session_id: str | None = None) -> BashResult:
            self.session_ids.append(session_id)
            if session_id is None:
                raise ValueError("session_id is required in container_session mode")
            return BashResult(command=command, exit_code=0, stdout="ok", stderr="")

    class _FakeContainerSessionAdapter:
        def __init__(self) -> None:
            self.created: list[str] = []

        def create_session(self, *, session_id: str | None = None) -> tuple[str, str]:
            resolved = session_id or "auto-session"
            self.created.append(resolved)
            return resolved, f"container-{resolved}"

        def close_session(self, *, session_id: str) -> bool:
            return True

    bash_adapter = _SessionAwareBashAdapter()
    container_adapter = _FakeContainerSessionAdapter()
    context = DeveloperToolContext(
        bash_adapter=bash_adapter,
        filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=tmp_path,
        require_human_approval_for_repo_writes=False,
        container_session_adapter=container_adapter,  # type: ignore[arg-type]
    )

    run_command, _read_file, _write_file, _apply_patch, start_session, _stop_session = build_role_tools(
        context=context
    )["developer"]

    first = run_command("pytest --version")
    second = run_command("pytest --version")
    started = start_session("explicit-session")
    third = run_command("pytest --version")

    assert first["exit_code"] == 0
    assert second["exit_code"] == 0
    assert third["exit_code"] == 0
    assert container_adapter.created == ["auto-session", "explicit-session"]
    assert bash_adapter.session_ids == ["auto-session", "auto-session", "explicit-session"]
    assert started["session_id"] == "explicit-session"


def test_developer_tools_reuse_existing_single_container_session(tmp_path: Path) -> None:
    class _SessionAwareBashAdapter:
        def __init__(self) -> None:
            self.session_ids: list[str | None] = []

        def run(self, *, role: AgentRole, command: str, session_id: str | None = None) -> BashResult:
            self.session_ids.append(session_id)
            if session_id is None:
                raise ValueError("session_id is required in container_session mode")
            return BashResult(command=command, exit_code=0, stdout="ok", stderr="")

    class _FakeContainerSessionAdapter:
        def __init__(self) -> None:
            self.created: list[str] = []
            self.existing = ("existing-session",)

        def list_session_ids(self) -> tuple[str, ...]:
            return self.existing

        def create_session(self, *, session_id: str | None = None) -> tuple[str, str]:
            resolved = session_id or "auto-session"
            self.created.append(resolved)
            return resolved, f"container-{resolved}"

        def close_session(self, *, session_id: str) -> bool:
            return True

    bash_adapter = _SessionAwareBashAdapter()
    container_adapter = _FakeContainerSessionAdapter()
    context = DeveloperToolContext(
        bash_adapter=bash_adapter,
        filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=tmp_path,
        require_human_approval_for_repo_writes=False,
        container_session_adapter=container_adapter,  # type: ignore[arg-type]
    )

    run_command, _read_file, _write_file, _apply_patch, _start_session, _stop_session = build_role_tools(
        context=context
    )["developer"]

    result = run_command("pytest --version")

    assert result["exit_code"] == 0
    assert container_adapter.created == []
    assert bash_adapter.session_ids == ["existing-session"]


def test_developer_apply_patch_updates_file(tmp_path: Path) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file("src/example.txt", "line1\nline2\n", approved=True)

    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/example.txt",
            "@@",
            "line1",
            "-line2",
            "+line2-updated",
            "*** End Patch",
        ]
    )

    result = apply_patch(patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    assert read_file("src/example.txt") == "line1\nline2-updated\n"


def test_developer_apply_patch_rejects_mismatched_context(tmp_path: Path) -> None:
    _run_command, _read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file("src/example.txt", "alpha\nbeta\n", approved=True)
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/example.txt",
            "@@",
            "missing-context",
            "-beta",
            "+gamma",
            "*** End Patch",
        ]
    )

    with pytest.raises(ValueError):
        apply_patch(patch, approved=True)


def test_developer_apply_patch_recovers_missing_plus_inside_addition_block(
    tmp_path: Path,
) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file(
        "src/example.txt",
        '"""Simple module used by the simulation fixture tests."""\n\n\n'
        "def add(left: int, right: int) -> int:\n"
        "    return left + right\n",
        approved=True,
    )
    near_miss_patch = "\n".join(
        [
            "*** Update File: src/example.txt",
            "@@",
            '-"""Simple module used by the simulation fixture tests."""',
            "-",
            "-",
            "-def add(left: int, right: int) -> int:",
            "-    return left + right",
            '+"""Basic math operations module with add, subtract, increment, and decrement."""',
            "+",
            "+",
            "def add(left: int, right: int) -> int:",
            '+    """Return the sum of two integers."""',
            "+    return left + right",
            "+",
            "+",
            "def increment(value: int) -> int:",
            "+    return value + 1",
            "+",
            "+",
            "def decrement(value: int) -> int:",
            "+    return value - 1",
        ]
    )

    result = apply_patch(near_miss_patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    updated = read_file("src/example.txt")
    assert "def increment(value: int) -> int:" in updated
    assert "def decrement(value: int) -> int:" in updated


def test_developer_apply_patch_surfaces_missing_plus_hint_when_not_repairable(
    tmp_path: Path,
) -> None:
    _run_command, _read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file("src/example.txt", "alpha\nbeta\n", approved=True)
    malformed_patch = "\n".join(
        [
            "*** Update File: src/example.txt",
            "@@",
            "alpha",
            "-beta",
            "+gamma",
            "delta",
        ]
    )

    with pytest.raises(ValueError) as exc_info:
        apply_patch(malformed_patch, approved=True)

    assert "every inserted line must start with '+'" in str(exc_info.value)


def test_developer_apply_patch_accepts_unified_diff_format(tmp_path: Path) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file("src/example.txt", "alpha\nbeta\n", approved=True)
    patch = "\n".join(
        [
            "--- a/src/example.txt",
            "+++ b/src/example.txt",
            "@@ -1,2 +1,2 @@",
            " alpha",
            "-beta",
            "+gamma",
        ]
    )

    result = apply_patch(patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    assert read_file("src/example.txt") == "alpha\ngamma\n"


def test_developer_apply_patch_accepts_custom_numbered_hunk_header(tmp_path: Path) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file("src/example.txt", "alpha\nbeta\n", approved=True)
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/example.txt",
            "@@ -1,2 +1,2 @@",
            " alpha",
            "-beta",
            "+delta",
            "*** End Patch",
        ]
    )

    result = apply_patch(patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    assert read_file("src/example.txt") == "alpha\ndelta\n"


def test_developer_apply_patch_accepts_unified_diff_inside_wrapper(tmp_path: Path) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file("src/example.txt", "left\nright\n", approved=True)
    patch = "\n".join(
        [
            "*** Begin Patch",
            "--- a/src/example.txt",
            "+++ b/src/example.txt",
            "@@ -1,2 +1,2 @@",
            " left",
            "-right",
            "+middle",
            "*** End Patch",
        ]
    )

    result = apply_patch(patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    assert read_file("src/example.txt") == "left\nmiddle\n"


def test_developer_apply_patch_accepts_custom_without_wrapper(tmp_path: Path) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file("src/example.txt", "one\ntwo\n", approved=True)
    patch = "\n".join(
        [
            "*** Update File: src/example.txt",
            "@@",
            "one",
            "-two",
            "+three",
        ]
    )

    result = apply_patch(patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    assert read_file("src/example.txt") == "one\nthree\n"


def test_developer_apply_patch_accepts_missing_end_marker(tmp_path: Path) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file("src/example.txt", "left\nright\n", approved=True)
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/example.txt",
            "@@",
            "left",
            "-right",
            "+center",
        ]
    )

    result = apply_patch(patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    assert read_file("src/example.txt") == "left\ncenter\n"


def test_developer_apply_patch_accepts_custom_bare_hunk_with_unified_context_prefix(
    tmp_path: Path,
) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file(
        "src/example.txt",
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        approved=True,
    )
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/example.txt",
            "@@",
            " def add(left: int, right: int) -> int:",
            "     return left + right",
            "+",
            "+def subtract(left: int, right: int) -> int:",
            "+    return left - right",
            "*** End Patch",
        ]
    )

    result = apply_patch(patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    assert read_file("src/example.txt") == (
        "def add(left: int, right: int) -> int:\n"
        "    return left + right\n"
        "\n"
        "def subtract(left: int, right: int) -> int:\n"
        "    return left - right\n"
    )


def test_developer_apply_patch_accepts_custom_hunk_with_multiple_change_groups(tmp_path: Path) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file(
        "src/example.txt",
        "header\n\nfrom pkg import add\n\n__all__ = [\"add\"]\n",
        approved=True,
    )
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/example.txt",
            "@@",
            "header",
            "",
            "-from pkg import add",
            "+from pkg import add, subtract",
            "",
            "-__all__ = [\"add\"]",
            "+__all__ = [\"add\", \"subtract\"]",
            "*** End Patch",
        ]
    )

    result = apply_patch(patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    assert read_file("src/example.txt") == (
        "header\n\n"
        "from pkg import add, subtract\n\n"
        "__all__ = [\"add\", \"subtract\"]\n"
    )


def test_developer_apply_patch_accepts_insertion_when_one_inserted_line_misses_plus(
    tmp_path: Path,
) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )

    write_file(
        "src/example.txt",
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        approved=True,
    )
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: src/example.txt",
            "@@",
            "def add(left: int, right: int) -> int:",
            "    return left + right",
            "+",
            "+def subtract(left: int, right: int) -> int:",
            "    return left - right",
            "*** End Patch",
        ]
    )

    result = apply_patch(patch, approved=True)

    assert result["applied"] is True
    assert result["operation_count"] == 1
    assert read_file("src/example.txt") == (
        "def add(left: int, right: int) -> int:\n"
        "    return left + right\n"
        "\n"
        "def subtract(left: int, right: int) -> int:\n"
        "    return left - right\n"
    )
