from __future__ import annotations

from dataclasses import replace

import pytest

from aitobuild.developer_isolation import (
    developer_task_bundle_from_payload,
    IsolationTool,
    build_developer_task_bundle,
    default_developer_isolation_policy,
    is_command_allowed,
    is_path_allowed,
)


def test_build_developer_task_bundle_success() -> None:
    bundle = build_developer_task_bundle(
        task_id="ISSUE-101",
        objective="Add scheduler metrics endpoint",
        acceptance_criteria=["Endpoint returns counters", "Tests added"],
        constraints=["No new external services"],
        context_files=["src/aitobuild/scheduler.py", "tests/test_scheduler.py"],
    )

    assert bundle.task_id == "ISSUE-101"
    assert len(bundle.acceptance_criteria) == 2
    assert bundle.policy.allowed_tools == (
        IsolationTool.GITHUB,
        IsolationTool.FILESYSTEM,
        IsolationTool.BASH,
    )


def test_bundle_rejects_parent_traversal_context_path() -> None:
    with pytest.raises(ValueError):
        build_developer_task_bundle(
            task_id="ISSUE-102",
            objective="Do work",
            acceptance_criteria=["Works"],
            constraints=[],
            context_files=["../secrets.txt"],
        )


def test_default_policy_path_and_command_checks() -> None:
    policy = default_developer_isolation_policy()

    assert is_path_allowed("src/aitobuild/app.py", policy=policy) is True
    assert is_path_allowed(".git/config", policy=policy) is False
    assert is_command_allowed("uv run pytest", policy=policy) is True
    assert is_command_allowed("rm -rf /", policy=policy) is False


def test_default_policy_supports_normal_development_workflows() -> None:
    policy = default_developer_isolation_policy()
    commands = (
        "uv sync", "uv add httpx", "python scripts/check.py", "python3 -c 'print(1)'",
        "git status --short", "git diff -- src/", "git switch -c feature/example",
        "node --check src/app.js", "npm ci", "npm run build", "npx eslint .", "pnpm test", "yarn test",
        "make test", "cmake --build build", "cargo test", "go test ./...", "dotnet test",
        "./gradlew test", "bash scripts/check.sh", "sh -c 'printf ready'",
        "env DEBUG=1 uv run pytest", "PYTHONPATH=src DEBUG=1 pytest -q",
        "ls -la", "find src -name '*.py'", "sed -n '1,20p' README.md", "mkdir -p build",
        "cp src/example.py build/example.py", "curl --fail https://example.com", "tar -tf archive.tar",
        "python - <<'PY'\n# user's comment\nprint('ok')\nPY",
        "set -eu; python scripts/check.py", "pwd; ls", "[ -f README.md ]",
    )
    for command in commands:
        assert is_command_allowed(command, policy=policy), command


def test_command_prefixes_have_token_boundaries_and_task_overrides() -> None:
    policy = default_developer_isolation_policy()
    assert is_command_allowed("rg", policy=policy)
    assert is_command_allowed("uv   run pytest", policy=policy)
    for command in ("", "DEBUG=1", "'unterminated", "pytest-unrelated", "npm-injected", "sudo apt update", "docker run image"):
        assert not is_command_allowed(command, policy=policy), command
    restricted = replace(policy, allowed_command_prefixes=("uv run", "git diff"))
    assert is_command_allowed("uv run pytest", policy=restricted)
    assert is_command_allowed("DEBUG=1 git diff -- src/", policy=restricted)
    assert not is_command_allowed("uv sync", policy=restricted)
    assert not is_command_allowed("git difftool", policy=restricted)
    assert not is_command_allowed("npm test", policy=restricted)


def test_developer_task_bundle_from_payload_roundtrip() -> None:
    bundle = build_developer_task_bundle(
        task_id="ISSUE-220",
        objective="Add endpoint coverage",
        acceptance_criteria=["Tests pass"],
        constraints=["No policy violations"],
        context_files=["src/aitobuild/app.py", "tests/test_app.py"],
    )

    parsed = developer_task_bundle_from_payload(bundle.to_payload())
    assert parsed.task_id == bundle.task_id
    assert parsed.objective == bundle.objective
    assert parsed.policy.allowed_paths == bundle.policy.allowed_paths
    assert parsed.policy.allowed_command_prefixes == bundle.policy.allowed_command_prefixes


def test_developer_task_bundle_from_payload_rejects_unknown_tool() -> None:
    payload = build_developer_task_bundle(
        task_id="ISSUE-221",
        objective="Add endpoint coverage",
        acceptance_criteria=["Tests pass"],
        constraints=["No policy violations"],
        context_files=["src/aitobuild/app.py"],
    ).to_payload()
    payload["policy"]["allowed_tools"] = ["github", "unknown-tool"]

    with pytest.raises(ValueError):
        developer_task_bundle_from_payload(payload)
