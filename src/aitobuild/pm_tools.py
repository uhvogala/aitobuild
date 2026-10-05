"""Product Manager role tools: backlog planning and gated issue writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Callable
from uuid import uuid4

from agent_framework import tool
from pydantic import Field

from aitobuild.meetings import MeetingRegistry
from aitobuild.policy import ActionClass, AgentRole, assert_role_action_allowed
from aitobuild.shared_tools import build_request_meeting_tool, build_web_search_tool
from aitobuild.tools.github import GitHubAdapter, GitHubIssueProposal, MockGitHubAdapter
from aitobuild.tools.web_search import WebSearchAdapter


ToolFunc = Callable[..., Any]


@dataclass
class PlanDraftStore:
    """Local PM plan drafts awaiting human approval before issue creation."""

    drafts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def upsert(
        self,
        *,
        draft_id: str | None,
        title: str,
        summary: str,
        acceptance_criteria: list[str] | None = None,
        labels: list[str] | None = None,
        repository: str | None = None,
    ) -> dict[str, Any]:
        cleaned_title = title.strip()
        cleaned_summary = summary.strip()
        if not cleaned_title:
            raise ValueError("title must be non-empty")
        if not cleaned_summary:
            raise ValueError("summary must be non-empty")
        identity = (draft_id or f"plan-{uuid4().hex[:10]}").strip()
        existing = self.drafts.get(identity, {})
        criteria = [
            item.strip()
            for item in (acceptance_criteria if acceptance_criteria is not None else existing.get("acceptance_criteria", []))
            if isinstance(item, str) and item.strip()
        ]
        record = {
            "draft_id": identity,
            "title": cleaned_title,
            "summary": cleaned_summary,
            "acceptance_criteria": criteria,
            "labels": [
                item.strip()
                for item in (labels if labels is not None else existing.get("labels", []))
                if isinstance(item, str) and item.strip()
            ],
            "repository": (repository or existing.get("repository") or "").strip() or None,
            "approval_state": existing.get("approval_state", "draft"),
            "approval_request_id": existing.get("approval_request_id"),
            "updated_at": datetime.now(tz=UTC).isoformat(),
        }
        self.drafts[identity] = record
        return dict(record)

    def set_acceptance_criteria(self, *, draft_id: str, acceptance_criteria: list[str]) -> dict[str, Any]:
        record = self._require(draft_id)
        criteria = [item.strip() for item in acceptance_criteria if item and item.strip()]
        if not criteria:
            raise ValueError("acceptance_criteria must contain at least one non-empty item")
        record["acceptance_criteria"] = criteria
        record["updated_at"] = datetime.now(tz=UTC).isoformat()
        if record.get("approval_state") == "approved":
            record["approval_state"] = "draft"
            record["approval_request_id"] = None
        return dict(record)

    def request_approval(self, *, draft_id: str) -> dict[str, Any]:
        record = self._require(draft_id)
        if not record["acceptance_criteria"]:
            raise ValueError("acceptance criteria are required before requesting plan approval")
        request_id = f"plan-approval-{uuid4().hex[:10]}"
        record["approval_state"] = "awaiting_human_approval"
        record["approval_request_id"] = request_id
        record["updated_at"] = datetime.now(tz=UTC).isoformat()
        return dict(record)

    def mark_approved(self, *, draft_id: str, approval_request_id: str) -> dict[str, Any]:
        record = self._require(draft_id)
        if record.get("approval_request_id") != approval_request_id:
            raise PermissionError("approval_request_id does not match the pending plan approval")
        if record.get("approval_state") != "awaiting_human_approval":
            raise PermissionError("plan is not awaiting human approval")
        record["approval_state"] = "approved"
        record["updated_at"] = datetime.now(tz=UTC).isoformat()
        return dict(record)

    def get(self, draft_id: str) -> dict[str, Any]:
        return dict(self._require(draft_id))

    def _require(self, draft_id: str) -> dict[str, Any]:
        record = self.drafts.get(draft_id.strip())
        if record is None:
            raise LookupError(f"Unknown plan draft_id: {draft_id}")
        return record


def build_pm_tools(
    *,
    github_adapter: GitHubAdapter | None = None,
    meeting_registry: MeetingRegistry | None = None,
    web_search_adapter: WebSearchAdapter | None = None,
    plan_store: PlanDraftStore | None = None,
    require_human_approval_for_repo_writes: bool = True,
    default_repository: str | None = None,
) -> tuple[ToolFunc, ...]:
    github = github_adapter or MockGitHubAdapter()
    drafts = plan_store or PlanDraftStore()
    role = AgentRole.PM

    def _repo(repository: str | None) -> str:
        resolved = (repository or default_repository or "").strip()
        if not resolved:
            raise ValueError("repository is required (owner/name)")
        return resolved

    def _issue_body(*, summary: str, acceptance_criteria: list[str]) -> str:
        lines = [summary.strip(), "", "## Acceptance Criteria"]
        lines.extend(f"- {item}" for item in acceptance_criteria)
        return "\n".join(lines).strip() + "\n"

    @tool(
        name="pm_get_issue",
        approval_mode="always_require",
        description="Fetch a single GitHub issue for backlog planning.",
    )
    def pm_get_issue(
        issue_number: Annotated[int, Field(ge=1)],
        repository: Annotated[str | None, Field(description="owner/name repository.")] = None,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        return github.get_issue(repository=_repo(repository), issue_number=issue_number).to_dict()

    @tool(
        name="pm_list_backlog",
        approval_mode="always_require",
        description="List backlog issues from GitHub (open by default), optionally filtered by labels.",
    )
    def pm_list_backlog(
        repository: Annotated[str | None, Field(description="owner/name repository.")] = None,
        state: Annotated[str, Field(description="open, closed, or all.")] = "open",
        labels: Annotated[list[str] | None, Field(description="Required labels.")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        issues = github.list_issues(
            repository=_repo(repository),
            state=state,
            labels=tuple(labels or ()),
            limit=limit,
        )
        return {
            "repository": _repo(repository),
            "state": state,
            "issues": [issue.to_dict() for issue in issues],
            "issue_count": len(issues),
        }

    @tool(
        name="pm_draft_plan",
        approval_mode="always_require",
        description=(
            "Create or update a local plan draft (title, summary, optional criteria/labels). "
            "Does not create a GitHub issue until approved creation."
        ),
    )
    def pm_draft_plan(
        title: Annotated[str, Field(description="Issue/plan title.")],
        summary: Annotated[str, Field(description="Problem statement / plan summary.")],
        acceptance_criteria: Annotated[list[str] | None, Field(description="Optional criteria list.")] = None,
        labels: Annotated[list[str] | None, Field(description="Optional labels.")] = None,
        repository: Annotated[str | None, Field(description="Target repository owner/name.")] = None,
        draft_id: Annotated[str | None, Field(description="Existing draft id to update.")] = None,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        return drafts.upsert(
            draft_id=draft_id,
            title=title,
            summary=summary,
            acceptance_criteria=acceptance_criteria,
            labels=labels,
            repository=repository or default_repository,
        )

    @tool(
        name="pm_set_acceptance_criteria",
        approval_mode="always_require",
        description="Replace acceptance criteria on a local plan draft. Clears prior approval if criteria change.",
    )
    def pm_set_acceptance_criteria(
        draft_id: Annotated[str, Field(description="Plan draft id.")],
        acceptance_criteria: Annotated[list[str], Field(description="Non-empty acceptance criteria.")],
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        return drafts.set_acceptance_criteria(draft_id=draft_id, acceptance_criteria=acceptance_criteria)

    @tool(
        name="pm_request_plan_approval",
        approval_mode="always_require",
        description=(
            "Mark a plan draft as awaiting human approval. Issue creation remains blocked until a human "
            "approves and pm_create_issue is called with approved=true."
        ),
    )
    def pm_request_plan_approval(
        draft_id: Annotated[str, Field(description="Plan draft id.")],
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        record = drafts.request_approval(draft_id=draft_id)
        return {
            **record,
            "next_action": (
                "Wait for a human to approve this plan, then call pm_create_issue with "
                "draft_id, approval_request_id, and approved=true."
            ),
        }

    @tool(
        name="pm_create_issue",
        approval_mode="always_require",
        description=(
            "Create a GitHub issue from an approved plan draft (preferred) or an explicit proposal. "
            "Requires human approval when approval gating is enabled."
        ),
    )
    def pm_create_issue(
        approved: Annotated[bool, Field(description="Human approval gate for issue creation.")] = False,
        draft_id: Annotated[str | None, Field(description="Approved plan draft id.")] = None,
        approval_request_id: Annotated[
            str | None,
            Field(description="Approval request id returned by pm_request_plan_approval."),
        ] = None,
        title: Annotated[str | None, Field(description="Issue title when not using a draft.")] = None,
        body: Annotated[str | None, Field(description="Issue body when not using a draft.")] = None,
        labels: Annotated[list[str] | None, Field(description="Optional labels.")] = None,
        repository: Annotated[str | None, Field(description="owner/name repository.")] = None,
    ) -> dict[str, Any]:
        if draft_id is not None:
            draft = drafts.get(draft_id)
            if require_human_approval_for_repo_writes:
                if not approved:
                    raise PermissionError("Repository-writing action requires human approval")
                if approval_request_id is None:
                    raise PermissionError("approval_request_id is required when creating from a gated draft")
                if draft.get("approval_state") == "awaiting_human_approval":
                    drafts.mark_approved(draft_id=draft_id, approval_request_id=approval_request_id)
                    draft = drafts.get(draft_id)
                elif draft.get("approval_state") != "approved" or draft.get("approval_request_id") != approval_request_id:
                    raise PermissionError("plan draft is not human-approved for issue creation")
            issue_title = str(draft["title"])
            issue_body = _issue_body(
                summary=str(draft["summary"]),
                acceptance_criteria=list(draft["acceptance_criteria"]),
            )
            issue_labels = tuple(draft.get("labels") or ())
            issue_repo = _repo(repository or draft.get("repository"))
        else:
            if not title or not title.strip():
                raise ValueError("title is required when draft_id is omitted")
            if body is None:
                raise ValueError("body is required when draft_id is omitted")
            issue_title = title.strip()
            issue_body = body
            issue_labels = tuple(labels or ())
            issue_repo = _repo(repository)
            # Preserve the historical mock proposal path for compatibility.
            github.create_issue_proposal(
                role=role,
                proposal=GitHubIssueProposal(title=issue_title, body=issue_body),
                approved=approved,
                require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            )

        issue = github.create_issue(
            role=role,
            repository=issue_repo,
            title=issue_title,
            body=issue_body,
            labels=issue_labels,
            approved=approved,
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
        )
        return {"created": True, "issue": issue.to_dict(), "approved": approved}

    @tool(
        name="pm_update_issue",
        approval_mode="always_require",
        description="Update a GitHub issue title/body/state/labels. Requires human approval when gating is enabled.",
    )
    def pm_update_issue(
        issue_number: Annotated[int, Field(ge=1)],
        approved: Annotated[bool, Field(description="Human approval gate.")] = False,
        title: Annotated[str | None, Field(description="Optional new title.")] = None,
        body: Annotated[str | None, Field(description="Optional new body.")] = None,
        state: Annotated[str | None, Field(description="Optional state: open or closed.")] = None,
        labels: Annotated[list[str] | None, Field(description="Optional full label replacement.")] = None,
        repository: Annotated[str | None, Field(description="owner/name repository.")] = None,
    ) -> dict[str, Any]:
        issue = github.update_issue(
            role=role,
            repository=_repo(repository),
            issue_number=issue_number,
            title=title,
            body=body,
            state=state,
            labels=tuple(labels) if labels is not None else None,
            approved=approved,
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
        )
        return {"updated": True, "issue": issue.to_dict()}

    @tool(
        name="pm_link_issues",
        approval_mode="always_require",
        description="Record a relationship between two issues (for example blocks/blocked_by). Approval-gated.",
    )
    def pm_link_issues(
        issue_number: Annotated[int, Field(ge=1)],
        related_issue_number: Annotated[int, Field(ge=1)],
        relationship: Annotated[str, Field(description="Relationship label, e.g. blocks.")] = "blocks",
        approved: Annotated[bool, Field(description="Human approval gate.")] = False,
        repository: Annotated[str | None, Field(description="owner/name repository.")] = None,
    ) -> dict[str, Any]:
        return github.link_issues(
            role=role,
            repository=_repo(repository),
            issue_number=issue_number,
            related_issue_number=related_issue_number,
            relationship=relationship,
            approved=approved,
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
        )

    return (
        pm_get_issue,
        pm_list_backlog,
        pm_draft_plan,
        pm_set_acceptance_criteria,
        pm_request_plan_approval,
        pm_create_issue,
        pm_update_issue,
        pm_link_issues,
        build_web_search_tool(role=role, adapter=web_search_adapter),
        build_request_meeting_tool(role=role, meeting_registry=meeting_registry),
    )
