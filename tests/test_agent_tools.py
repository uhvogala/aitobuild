from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aitobuild.agent_tools import DeveloperToolContext, build_role_tools
from aitobuild.developer_isolation import (
    DeveloperTaskBudget, IsolationTool, build_developer_task_bundle, default_developer_isolation_policy,
)
from aitobuild.policy import AgentRole
from aitobuild.tools import MockBashAdapter, MockFilesystemAdapter
from aitobuild.tools.bash import BashResult
from aitobuild.tools.bash import ContainerSessionBashAdapter


def _developer_tools(tmp_path: Path, *, legacy_patch: bool = True, include_search: bool = False):
    context = DeveloperToolContext(
        bash_adapter=MockBashAdapter(),
        filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=tmp_path,
        require_human_approval_for_repo_writes=False,
        container_session_adapter=None,
        use_legacy_patch_tool=legacy_patch,
    )
    role_tools = build_role_tools(context=context)
    tools = role_tools["developer"]
    return tools if include_search else tools[:6]


def test_build_role_tools_includes_developer_toolset(tmp_path: Path) -> None:
    tools = _developer_tools(tmp_path, legacy_patch=False, include_search=True)
    assert len(tools) == 8
    names = {tool.name for tool in tools}
    assert "developer_edit_file" in names
    assert "developer_apply_patch" not in names
    assert {"developer_find_files", "developer_search_files"} <= names


def test_budgeted_writes_restore_counts_and_reject_before_side_effects(tmp_path: Path) -> None:
    policy = replace(default_developer_isolation_policy(), max_file_changes=1)
    bundle = build_developer_task_bundle(
        task_id="one-file", objective="Edit one file", acceptance_criteria=["Scoped write"],
        constraints=[], context_files=[], policy=policy,
    )
    path = tmp_path / "state/budget.json"
    context = DeveloperToolContext(
        bash_adapter=MockBashAdapter(), filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=tmp_path, require_human_approval_for_repo_writes=True,
        bound_session_id="budgeted", isolation_policy=policy,
        task_budget=DeveloperTaskBudget(path=path, bundle=bundle),
    )
    tools = build_role_tools(context=context)["developer"]
    with pytest.raises(PermissionError):
        tools[2]("src/one.py", "before", approved=False)
    assert not (tmp_path / ".aitobuild/workspaces/budgeted").exists()
    tools[2]("src/one.py", "before", approved=True)
    restored = build_role_tools(context=replace(
        context, task_budget=DeveloperTaskBudget(path=path, bundle=bundle, create=False),
    ))["developer"]
    restored[3]("src/one.py", "before", "after", approved=True)
    root = tmp_path / ".aitobuild/workspaces/budgeted"
    assert (root / "src/one.py").read_text() == "after"
    with pytest.raises(PermissionError, match="file budget"):
        restored[2]("tests/new/two.py", "blocked", approved=True)
    assert not (root / "tests").exists()

    calls: list[bool] = []

    class ProfileAdapter(ContainerSessionBashAdapter):
        def create_session(self, *, session_id: str | None = None, read_only_workspace: bool = False,
                           deadline: float | None = None) -> tuple[str, str]:
            assert deadline is not None
            calls.append(read_only_workspace)
            return session_id or "budgeted", "container"

    adapter = ProfileAdapter(workspace_root=tmp_path, image="prepared", container_workdir="/workspace",
                             container_name_prefix="test-profile")
    profiled = build_role_tools(context=replace(context, container_session_adapter=adapter))["developer"]
    profiled[4]("budgeted")
    assert calls == [True, True]


def test_symlink_cannot_bypass_blocked_repository_path(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("unchanged")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/alias").symlink_to(tmp_path / ".git", target_is_directory=True)
    tools = _developer_tools(tmp_path, legacy_patch=False)
    with pytest.raises(ValueError, match="policy paths"):
        tools[2]("src/alias/config", "bypassed")
    assert (tmp_path / ".git/config").read_text() == "unchanged"


def test_patch_tool_schema_example_is_literal_and_executable(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src/example.py"
    target.write_text("old = 1\n")
    apply_patch = _developer_tools(tmp_path)[3]
    example = apply_patch.description.split("Example patch argument inserting after one unique line:\n", 1)[1]
    assert apply_patch(patch=example)["applied"]
    assert target.read_text() == "old = 1\nnew = 2\n"


@pytest.mark.parametrize("original, old_text, new_text, expected", [
    ("old = 1\n", "old = 1\n", "old = 1\nnew = 2\n", "old = 1\nnew = 2\n"),
    ("keep\nremove\nend", "remove\n", "", "keep\nend"),
    ("alpha\r\nbeta\r\n", "beta", "gamma", "alpha\r\ngamma\r\n"),
    ("alpha\nlast", "last", "changed", "alpha\nchanged"),
    ("@@\n+literal\n", "+literal", "-literal", "@@\n-literal\n"),
])
def test_structured_edit_preserves_surrounding_text(
    tmp_path: Path, original: str, old_text: str, new_text: str, expected: str,
) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src/example.txt"
    target.write_bytes(original.encode())
    edit = _developer_tools(tmp_path, legacy_patch=False)[3]
    assert edit.name == "developer_edit_file"
    assert edit(path="src/example.txt", old_text=old_text, new_text=new_text)["replacement_count"] == 1
    assert target.read_bytes() == expected.encode()


@pytest.mark.parametrize("original, old_text", [
    ("alpha\nbeta\n", "stale"),
    ("alpha\nalpha\n", "alpha"),
    ("aaa", "aa"),
    ("alpha", ""),
])
def test_structured_edit_rejects_invalid_anchors_without_writing(
    tmp_path: Path, original: str, old_text: str,
) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src/example.txt"
    target.write_text(original)
    edit = _developer_tools(tmp_path, legacy_patch=False)[3]
    with pytest.raises(ValueError):
        edit(path="src/example.txt", old_text=old_text, new_text="changed")
    assert target.read_text() == original


@pytest.mark.parametrize("path", ["/src/example.txt", "../example.txt", "secrets/example.txt"])
def test_structured_edit_rejects_unscoped_paths(tmp_path: Path, path: str) -> None:
    edit = _developer_tools(tmp_path, legacy_patch=False)[3]
    with pytest.raises(ValueError, match="workspace-relative|traverse|outside"):
        edit(path=path, old_text="old", new_text="new")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("approved", [True, False])
def test_structured_edit_uses_mcp_backend_without_bypassing_approval(tmp_path: Path, approved: bool) -> None:
    class MemoryMCPAdapter:
        def __init__(self) -> None:
            self.content = "old = 1\r\n"
            self.reads = 0
            self.writes = 0

        def read_file(self, *, session_id: str, path: str) -> str:
            assert (session_id, path) == ("dev-one", "src/example.txt")
            self.reads += 1
            return self.content

        def write_file(self, *, session_id: str, path: str, content: str) -> None:
            assert (session_id, path) == ("dev-one", "src/example.txt")
            self.writes += 1
            self.content = content

    adapter = MemoryMCPAdapter()
    context = DeveloperToolContext(
        bash_adapter=MockBashAdapter(), filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=tmp_path, require_human_approval_for_repo_writes=True,
        enable_mcp_adapters=True, mcp_tool_adapter=adapter,  # type: ignore[arg-type]
        bound_session_id="dev-one",
    )
    edit = build_role_tools(context=context)["developer"][3]
    if approved:
        assert edit("src/example.txt", "old = 1", "old = 2", approved=True)["applied"]
        assert adapter.content == "old = 2\r\n"
        assert (adapter.reads, adapter.writes) == (1, 1)
    else:
        with pytest.raises(PermissionError, match="approval"):
            edit("src/example.txt", "old = 1", "old = 2", approved=False)
        assert adapter.content == "old = 1\r\n"
        assert (adapter.reads, adapter.writes) == (0, 0)


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


def test_developer_tools_use_supplied_task_policy(tmp_path: Path) -> None:
    class CountingBashAdapter(MockBashAdapter):
        calls = 0

        def run(self, *, role: AgentRole, command: str, session_id: str | None = None) -> BashResult:
            self.calls += 1
            return super().run(role=role, command=command, session_id=session_id)

    bash = CountingBashAdapter()
    policy = replace(
        default_developer_isolation_policy(), allowed_command_prefixes=("git diff",),
        allowed_paths=("tests/only/",),
    )
    context = DeveloperToolContext(
        bash_adapter=bash, filesystem_adapter=MockFilesystemAdapter(), workspace_root=tmp_path,
        require_human_approval_for_repo_writes=False, isolation_policy=policy,
    )
    run_command, read_file, write_file = build_role_tools(context=context)["developer"][:3]
    assert "Task command prefixes: git diff" in run_command.description
    assert "Task allowed paths: tests/only/" in read_file.description
    with pytest.raises(PermissionError, match="policy prefixes"):
        run_command("python -m pytest")
    assert bash.calls == 0
    assert run_command("git diff --stat")["exit_code"] == 0
    assert bash.calls == 1
    assert write_file("tests/only/example.txt", "allowed")["written"]
    assert read_file("tests/only/example.txt") == "allowed"
    with pytest.raises(ValueError, match="outside"):
        write_file("src/example.txt", "denied")
    assert not (tmp_path / "src" / "example.txt").exists()


def test_standalone_tool_commands_keep_prototype_behavior(tmp_path: Path) -> None:
    run_command = _developer_tools(tmp_path)[0]
    assert run_command("custom-runner --help")["exit_code"] == 0


@pytest.mark.parametrize("allowed_tool,excluded", [
    (IsolationTool.FILESYSTEM, {"developer_run_command", "developer_start_session", "developer_stop_session"}),
    (IsolationTool.BASH, {"developer_read_file", "developer_write_file", "developer_edit_file", "developer_find_files", "developer_search_files"}),
])
def test_developer_tools_exclude_disallowed_task_categories(tmp_path: Path, allowed_tool, excluded) -> None:
    context = DeveloperToolContext(
        bash_adapter=MockBashAdapter(), filesystem_adapter=MockFilesystemAdapter(), workspace_root=tmp_path,
        require_human_approval_for_repo_writes=False,
        isolation_policy=replace(default_developer_isolation_policy(), allowed_tools=(allowed_tool,)),
    )
    names = {tool.name for tool in build_role_tools(context=context)["developer"]}
    assert names.isdisjoint(excluded)
    assert names


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

    run_command, _read_file, _write_file, _apply_patch, start_session, _stop_session, *_search_tools = build_role_tools(
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

    run_command, _read_file, _write_file, _apply_patch, _start_session, _stop_session, *_search_tools = build_role_tools(
        context=context
    )["developer"]

    result = run_command("pytest --version")

    assert result["exit_code"] == 0
    assert container_adapter.created == []
    assert bash_adapter.session_ids == ["existing-session"]


@pytest.mark.parametrize("container_mode", [False, True])
def test_bound_developer_tools_use_private_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container_mode: bool,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "note.txt").write_text("seed", encoding="utf-8")
    adapter = ContainerSessionBashAdapter(
        workspace_root=tmp_path, image="python:3.14-slim",
        container_workdir="/workspace", container_name_prefix="aitobuild-test",
    )
    adapter._prepare_workspace("dev-one")
    adapter._prepare_workspace("dev-two")
    monkeypatch.setattr(
        adapter, "create_session", lambda *, session_id: (session_id, f"container-{session_id}")
    )
    toolsets = [
        build_role_tools(context=DeveloperToolContext(
            bash_adapter=MockBashAdapter(), filesystem_adapter=MockFilesystemAdapter(),
            workspace_root=tmp_path, require_human_approval_for_repo_writes=True,
            container_session_adapter=adapter if container_mode else None, bound_session_id=session_id,
        ))["developer"]
        for session_id in ("dev-one", "dev-two")
    ]
    first, second = toolsets
    first[2]("src/note.txt", "first developer", approved=True)
    second[2]("src/note.txt", "second developer", approved=True)
    first[3]("src/note.txt", "first developer", "edited first developer")
    assert first[1]("src/note.txt") == "edited first developer"
    assert second[1]("src/note.txt") == "second developer"
    assert (tmp_path / "src" / "note.txt").read_text() == "seed"
    with pytest.raises(ValueError, match="bound session"):
        first[2]("src/note.txt", "wrong", session_id="dev-two")
    with pytest.raises(ValueError, match="bound session"):
        first[3]("src/note.txt", "second developer", "wrong", session_id="dev-two")
    if not container_mode:
        assert first[6](glob="**/note.txt")["results"] == [{"path": "src/note.txt"}]
        found = first[7](pattern="edited first developer")
        assert found["results"][0]["path"] == "src/note.txt"
        assert second[7](pattern="edited first developer")["results"] == []
        with pytest.raises(ValueError, match="bound session"):
            first[6](session_id="dev-two")
    if container_mode:
        with pytest.raises(ValueError, match="another Developer"):
            first[5]("dev-two")


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


def test_developer_apply_patch_rejects_ambiguous_repaired_context(tmp_path: Path) -> None:
    _run_command, read_file, write_file, apply_patch, _start_session, _stop_session = _developer_tools(
        tmp_path
    )
    original = "alpha\nbeta\nalpha\nbeta\n"
    write_file("src/example.txt", original, approved=True)
    patch = "\n".join([
        "*** Update File: src/example.txt", "@@", "alpha", "-beta", "+gamma",
        "missing-plus", "+extra",
    ])
    with pytest.raises(ValueError):
        apply_patch(patch, approved=True)
    assert read_file("src/example.txt") == original


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
