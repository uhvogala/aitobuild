from __future__ import annotations

import pytest

from aitobuild.policy import ActionClass, AgentRole, assert_repo_write_approval, assert_role_action_allowed
from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage


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


@pytest.mark.parametrize("stage", list(ApprovalStage))
def test_approval_policy_defaults_preserve_manual_gates(stage) -> None:
    policy = ApprovalPolicy()
    assert policy.mode(stage) == ("disabled" if stage == ApprovalStage.MERGE else "human")


@pytest.mark.parametrize("stage", list(ApprovalStage))
def test_automatic_policy_requires_explicit_adapter_and_exact_evidence(stage) -> None:
    policy = ApprovalPolicy.model_validate({stage.value: "automatic"})
    with pytest.raises(PermissionError, match="adapter is unavailable"):
        policy.resolve(stage, actor_id="operator", operation={"head": "a" * 40})
    decision = policy.resolve(stage, actor_id="operator", operation={"head": "a" * 40}, automatic_supported=True)
    assert decision.mode == "automatic" and decision.policy_revision == policy.revision
    changed = policy.resolve(stage, actor_id="operator", operation={"head": "b" * 40}, automatic_supported=True)
    assert decision.operation_digest != changed.operation_digest
    with pytest.raises(PermissionError, match="revision changed"):
        policy.resolve(stage, actor_id="operator", operation={}, expected_revision=ApprovalPolicy().revision)
    with pytest.raises(PermissionError):
        assert_role_action_allowed(AgentRole.ARCHITECT, ActionClass.REPO_WRITE)


@pytest.mark.parametrize("stage", list(ApprovalStage))
def test_disabled_policy_is_refusal_not_bypass(stage) -> None:
    policy = ApprovalPolicy.model_validate({stage.value: "disabled"})
    with pytest.raises(PermissionError, match="stage is disabled"):
        policy.resolve(stage, actor_id="operator", operation={}, automatic_supported=True)


@pytest.mark.parametrize("payload", [{"unknown": "automatic"}, {"developer_tools": False},
                                     {"developer_tools": "auto"}, {"developer_tools": None}])
def test_approval_policy_rejects_malformed_modes(payload) -> None:
    with pytest.raises(ValueError):
        ApprovalPolicy.model_validate(payload)


def test_approval_policy_is_frozen_and_canonical() -> None:
    policy = ApprovalPolicy(developer_tools="automatic")
    assert policy.revision == ApprovalPolicy.model_validate({"developer_tools": "automatic"}).revision
    with pytest.raises(ValueError):
        policy.developer_tools = "human"
    with pytest.raises(ValueError, match="operator identity"):
        policy.resolve(ApprovalStage.DEVELOPER_TOOLS, actor_id=" ", operation={}, automatic_supported=True)
