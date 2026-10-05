"""Mock-first GitHub adapter with approval-aware write operations."""

from __future__ import annotations

from dataclasses import dataclass

from aitobuild.policy import ActionClass, AgentRole, assert_repo_write_approval, assert_role_action_allowed


@dataclass(slots=True, frozen=True)
class GitHubIssueProposal:
    title: str
    body: str


class MockGitHubAdapter:
    def __init__(self) -> None:
        self.proposals: list[GitHubIssueProposal] = []

    def create_issue_proposal(
        self,
        *,
        role: AgentRole,
        proposal: GitHubIssueProposal,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> None:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        self.proposals.append(proposal)
