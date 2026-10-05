from __future__ import annotations

import pytest

from aitobuild.policy import ActionClass, AgentRole, assert_repo_write_approval, assert_role_action_allowed


def test_architect_cannot_repo_write() -> None:
    with pytest.raises(PermissionError):
        assert_role_action_allowed(AgentRole.ARCHITECT, ActionClass.REPO_WRITE)


def test_repo_write_requires_approval() -> None:
    with pytest.raises(PermissionError):
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=True,
            approved=False,
        )


def test_repo_write_approval_passes_when_not_required() -> None:
    assert_repo_write_approval(
        require_human_approval_for_repo_writes=False,
        approved=False,
    )


def test_pm_can_issue_write_but_not_repo_write() -> None:
    assert_role_action_allowed(AgentRole.PM, ActionClass.ISSUE_WRITE)
    with pytest.raises(PermissionError):
        assert_role_action_allowed(AgentRole.PM, ActionClass.REPO_WRITE)


def test_architect_can_pr_review() -> None:
    assert_role_action_allowed(AgentRole.ARCHITECT, ActionClass.PR_REVIEW)
