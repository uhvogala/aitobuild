from __future__ import annotations

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
