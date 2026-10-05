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
            for item in (
                acceptance_criteria
                if acceptance_criteria is not None
                else existing.get("acceptance_criteria", [])
            )
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
        if record.get("approval_state") in {"approved", "awaiting_human_approval"}:
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

    def mark_approved(self, *, draft_id: str, approval_request_id: str | None = None) -> dict[str, Any]:
        """Operator-only approval. Must not be callable from agent tools."""
        record = self._require(draft_id)
        if record.get("approval_state") != "awaiting_human_approval":
            raise PermissionError("plan is not awaiting human approval")
        expected = record.get("approval_request_id")
        if approval_request_id is not None and expected != approval_request_id:
            raise PermissionError("approval_request_id does not match the pending plan approval")
        record["approval_state"] = "approved"
        record["updated_at"] = datetime.now(tz=UTC).isoformat()
        return dict(record)

    def list_drafts(self, *, pending_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        items = list(self.drafts.values())
        if pending_only:
            items = [item for item in items if item.get("approval_state") == "awaiting_human_approval"]
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return [dict(item) for item in items[: max(1, min(limit, 500))]]

    def get(self, draft_id: str) -> dict[str, Any]:
        return dict(self._require(draft_id))

    def _require(self, draft_id: str) -> dict[str, Any]:
        record = self.drafts.get(draft_id.strip())
        if record is None:
            raise LookupError(f"Unknown plan draft_id: {draft_id}")
        return record


@dataclass
class IssueWriteApprovalStore:
    """Operator-gated approvals for PM issue update/link mutations."""

    requests: dict[str, dict[str, Any]] = field(default_factory=dict)

    def create_pending(self, *, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"issue-write-{uuid4().hex[:10]}"
        record = {
            "approval_request_id": request_id,
            "kind": kind,
            "payload": dict(payload),
            "approval_state": "awaiting_human_approval",
            "updated_at": datetime.now(tz=UTC).isoformat(),
        }
        self.requests[request_id] = record
        return dict(record)

    def mark_approved(self, *, approval_request_id: str) -> dict[str, Any]:
        record = self._require(approval_request_id)
        if record.get("approval_state") != "awaiting_human_approval":
            raise PermissionError("issue write is not awaiting human approval")
        record["approval_state"] = "approved"
        record["updated_at"] = datetime.now(tz=UTC).isoformat()
        return dict(record)

    def consume_approved(self, *, approval_request_id: str, kind: str) -> dict[str, Any]:
        record = self._require(approval_request_id)
        if record.get("kind") != kind:
            raise PermissionError("approval_request_id kind mismatch")
        if record.get("approval_state") != "approved":
            raise PermissionError("issue write is not human-approved")
        record["approval_state"] = "consumed"
        record["updated_at"] = datetime.now(tz=UTC).isoformat()
        return dict(record)

    def list_requests(self, *, pending_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        items = list(self.requests.values())
        if pending_only:
            items = [item for item in items if item.get("approval_state") == "awaiting_human_approval"]
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return [dict(item) for item in items[: max(1, min(limit, 500))]]

    def _require(self, approval_request_id: str) -> dict[str, Any]:
        record = self.requests.get(approval_request_id.strip())
        if record is None:
            raise LookupError(f"Unknown issue-write approval_request_id: {approval_request_id}")
        return record


def build_pm_tools(
    *,
    github_adapter: GitHubAdapter | None = None,
    meeting_registry: MeetingRegistry | None = None,
    web_search_adapter: WebSearchAdapter | None = None,
    plan_store: PlanDraftStore | None = None,
    issue_write_store: IssueWriteApprovalStore | None = None,
    require_human_approval_for_repo_writes: bool = True,
    default_repository: str | None = None,
) -> tuple[ToolFunc, ...]:
    github = github_adapter or MockGitHubAdapter()
    drafts = plan_store or PlanDraftStore()
    write_approvals = issue_write_store or IssueWriteApprovalStore()
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
            "Does not create a GitHub issue until a human approves the draft via the internal API."
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
            "Mark a plan draft as awaiting human approval. A privileged operator must approve via "
            "POST /internal/pm/plan/approve before pm_create_issue can create the GitHub issue."
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
                "Wait for a human/operator to approve this plan via "
                "POST /internal/pm/plan/approve with X-Internal-Token, "
                "then call pm_create_issue with draft_id."
            ),
        }

    @tool(
        name="pm_create_issue",
        approval_mode="always_require",
        description=(
            "Create a GitHub issue from an operator-approved plan draft (preferred) or an explicit "
            "proposal when approval gating is disabled. Agent-supplied approved flags are ignored; "
            "when gating is on, the draft must already be approved in the plan store."
        ),
    )
    def pm_create_issue(
        draft_id: Annotated[str | None, Field(description="Operator-approved plan draft id.")] = None,
        title: Annotated[str | None, Field(description="Issue title when not using a draft.")] = None,
        body: Annotated[str | None, Field(description="Issue body when not using a draft.")] = None,
        labels: Annotated[list[str] | None, Field(description="Optional labels.")] = None,
        repository: Annotated[str | None, Field(description="owner/name repository.")] = None,
        approved: Annotated[
            bool | None,
            Field(
                description=(
                    "Ignored. Kept for compatibility; human approval comes from the plan store / "
                    "internal API, never from this agent-supplied flag."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        del approved  # Agent-supplied approval is never authoritative.
        if draft_id is not None:
            draft = drafts.get(draft_id)
            if require_human_approval_for_repo_writes:
                if draft.get("approval_state") != "approved":
                    raise PermissionError(
                        "plan draft is not operator-approved for issue creation; "
                        "approve via POST /internal/pm/plan/approve"
                    )
            issue_title = str(draft["title"])
            issue_body = _issue_body(
                summary=str(draft["summary"]),
                acceptance_criteria=list(draft["acceptance_criteria"]),
            )
            issue_labels = tuple(draft.get("labels") or ())
            issue_repo = _repo(repository or draft.get("repository"))
        else:
            if require_human_approval_for_repo_writes:
                raise PermissionError(
                    "When approval gating is enabled, pm_create_issue requires an operator-approved "
                    "draft_id (create a draft, request approval, then approve via internal API)"
                )
            if not title or not title.strip():
                raise ValueError("title is required when draft_id is omitted")
            if body is None:
                raise ValueError("body is required when draft_id is omitted")
            issue_title = title.strip()
            issue_body = body
            issue_labels = tuple(labels or ())
            issue_repo = _repo(repository)
            github.create_issue_proposal(
                role=role,
                proposal=GitHubIssueProposal(title=issue_title, body=issue_body),
                approved=True,
                require_human_approval_for_repo_writes=False,
            )

        issue = github.create_issue(
            role=role,
            repository=issue_repo,
            title=issue_title,
            body=issue_body,
            labels=issue_labels,
            approved=True,
            require_human_approval_for_repo_writes=False,
        )
        return {
            "created": True,
            "issue": issue.to_dict(),
            "draft_id": draft_id,
            "operator_approved": True,
        }

    @tool(
        name="pm_update_issue",
        approval_mode="always_require",
        description=(
            "Update a GitHub issue title/body/state/labels. When approval gating is enabled, omit "
            "approval_request_id to create a pending operator approval, then retry after "
            "POST /internal/pm/issue-write/approve."
        ),
    )
    def pm_update_issue(
        issue_number: Annotated[int, Field(ge=1)],
        title: Annotated[str | None, Field(description="Optional new title.")] = None,
        body: Annotated[str | None, Field(description="Optional new body.")] = None,
        state: Annotated[str | None, Field(description="Optional state: open or closed.")] = None,
        labels: Annotated[list[str] | None, Field(description="Optional full label replacement.")] = None,
        repository: Annotated[str | None, Field(description="owner/name repository.")] = None,
        approval_request_id: Annotated[
            str | None,
            Field(description="Operator-approved issue-write request id when gating is enabled."),
        ] = None,
        approved: Annotated[
            bool | None,
            Field(description="Ignored. Human approval comes from the issue-write store / internal API."),
        ] = None,
    ) -> dict[str, Any]:
        del approved
        issue_repo = _repo(repository)
        payload = {
            "issue_number": issue_number,
            "title": title,
            "body": body,
            "state": state,
            "labels": list(labels) if labels is not None else None,
            "repository": issue_repo,
        }
        if require_human_approval_for_repo_writes:
            if approval_request_id is None:
                pending = write_approvals.create_pending(kind="update_issue", payload=payload)
                return {
                    **pending,
                    "updated": False,
                    "next_action": (
                        "Wait for operator approval via POST /internal/pm/issue-write/approve, "
                        "then call pm_update_issue again with approval_request_id."
                    ),
                }
            write_approvals.consume_approved(approval_request_id=approval_request_id, kind="update_issue")

        issue = github.update_issue(
            role=role,
            repository=issue_repo,
            issue_number=issue_number,
            title=title,
            body=body,
            state=state,
            labels=tuple(labels) if labels is not None else None,
            approved=True,
            require_human_approval_for_repo_writes=False,
        )
        return {"updated": True, "issue": issue.to_dict()}

    @tool(
        name="pm_link_issues",
        approval_mode="always_require",
        description=(
            "Record a relationship between two issues. When approval gating is enabled, omit "
            "approval_request_id to create a pending operator approval, then retry after "
            "POST /internal/pm/issue-write/approve."
        ),
    )
    def pm_link_issues(
        issue_number: Annotated[int, Field(ge=1)],
        related_issue_number: Annotated[int, Field(ge=1)],
        relationship: Annotated[str, Field(description="Relationship label, e.g. blocks.")] = "blocks",
        repository: Annotated[str | None, Field(description="owner/name repository.")] = None,
        approval_request_id: Annotated[
            str | None,
            Field(description="Operator-approved issue-write request id when gating is enabled."),
        ] = None,
        approved: Annotated[
            bool | None,
            Field(description="Ignored. Human approval comes from the issue-write store / internal API."),
        ] = None,
    ) -> dict[str, Any]:
        del approved
        issue_repo = _repo(repository)
        payload = {
            "issue_number": issue_number,
            "related_issue_number": related_issue_number,
            "relationship": relationship,
            "repository": issue_repo,
        }
        if require_human_approval_for_repo_writes:
            if approval_request_id is None:
                pending = write_approvals.create_pending(kind="link_issues", payload=payload)
                return {
                    **pending,
                    "linked": False,
                    "next_action": (
                        "Wait for operator approval via POST /internal/pm/issue-write/approve, "
                        "then call pm_link_issues again with approval_request_id."
                    ),
                }
            write_approvals.consume_approved(approval_request_id=approval_request_id, kind="link_issues")

        return github.link_issues(
            role=role,
            repository=issue_repo,
            issue_number=issue_number,
            related_issue_number=related_issue_number,
            relationship=relationship,
            approved=True,
            require_human_approval_for_repo_writes=False,
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
