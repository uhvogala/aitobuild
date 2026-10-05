from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aitobuild.agent_tools import DeveloperToolContext, build_role_tools
from aitobuild.architect_tools import ARCHITECT_SESSION_PREFIX, build_architect_tools
from aitobuild.meetings import MeetingRegistry
from aitobuild.pm_tools import IssueWriteApprovalStore, PlanDraftStore
from aitobuild.policy import ActionClass, AgentRole, assert_role_action_allowed
from aitobuild.tools.architect_memory import ArchitectMemoryStore
from aitobuild.tools.bash import MockBashAdapter
from aitobuild.tools.filesystem import MockFilesystemAdapter
from aitobuild.tools.github import (
    GhCliGitHubAdapter,
    GitHubIssue,
    GitHubPullRequest,
    MockGitHubAdapter,
    build_github_adapter,
)
from aitobuild.tools.web_search import MockWebSearchAdapter, WebSearchResult


def _tool_map(tools: tuple) -> dict[str, object]:
    return {tool.name: tool for tool in tools}


def _context(
    tmp_path: Path,
    *,
    github: MockGitHubAdapter | None = None,
    plan_store: PlanDraftStore | None = None,
    issue_write_store: IssueWriteApprovalStore | None = None,
    allow_pr_approve: bool = False,
    require_human_approval_for_repo_writes: bool = True,
) -> DeveloperToolContext:
    return DeveloperToolContext(
        bash_adapter=MockBashAdapter(),
        filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=tmp_path,
        require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
        github_adapter=github or MockGitHubAdapter(),
        meeting_registry=MeetingRegistry(),
        web_search_adapter=MockWebSearchAdapter(
            results=[
                WebSearchResult(
                    title="Acceptance criteria guide",
                    url="https://example.invalid/ac",
                    snippet="Write testable acceptance criteria.",
                )
            ]
        ),
        architect_memory=ArchitectMemoryStore(tmp_path / "architect_memory.jsonl"),
        plan_draft_store=plan_store or PlanDraftStore(),
        issue_write_store=issue_write_store or IssueWriteApprovalStore(),
        default_repository="uhvogala/aitobuild_example",
        allow_pr_approve=allow_pr_approve,
    )


def test_build_role_tools_includes_architect_and_pm(tmp_path: Path) -> None:
    roles = build_role_tools(context=_context(tmp_path))
    assert "architect" in roles and "pm" in roles and "developer" in roles
    architect_names = {tool.name for tool in roles["architect"]}
    pm_names = {tool.name for tool in roles["pm"]}
    assert {
        "architect_read_file",
        "architect_find_files",
        "architect_search_files",
        "architect_run_command",
        "architect_start_session",
        "architect_stop_session",
        "architect_get_pr",
        "architect_submit_pr_review",
        "architect_memory_query",
        "architect_memory_record",
        "web_search",
        "request_meeting",
    } <= architect_names
    assert {
        "pm_get_issue",
        "pm_list_backlog",
        "pm_draft_plan",
        "pm_set_acceptance_criteria",
        "pm_request_plan_approval",
        "pm_create_issue",
        "pm_update_issue",
        "pm_link_issues",
        "web_search",
        "request_meeting",
    } <= pm_names
    assert "architect_write_file" not in architect_names
    assert "developer_write_file" not in architect_names


def test_architect_stays_read_only_for_repo_writes() -> None:
    with pytest.raises(PermissionError):
        assert_role_action_allowed(AgentRole.ARCHITECT, ActionClass.REPO_WRITE)
    with pytest.raises(PermissionError):
        assert_role_action_allowed(AgentRole.ARCHITECT, ActionClass.ISSUE_WRITE)
    assert_role_action_allowed(AgentRole.ARCHITECT, ActionClass.PR_REVIEW)


def test_architect_pr_review_and_memory_roundtrip(tmp_path: Path) -> None:
    github = MockGitHubAdapter()
    github.seed_pull_request(
        repository="uhvogala/aitobuild_example",
        pull_request=GitHubPullRequest(
            number=12,
            title="Add feature",
            body="Implements issue #7",
            state="open",
            head_ref="feat/x",
            base_ref="main",
            changed_files=("src/example.py",),
            repository="uhvogala/aitobuild_example",
        ),
    )
    tools = _tool_map(build_role_tools(context=_context(tmp_path, github=github))["architect"])
    pr = tools["architect_get_pr"](12)
    assert pr["number"] == 12
    assert pr["changed_files"] == ["src/example.py"]
    review = tools["architect_submit_pr_review"](12, "REQUEST_CHANGES", "Please extract a helper.")
    assert review["event"] == "REQUEST_CHANGES"
    assert len(github.reviews) == 1
    recorded = tools["architect_memory_record"](
        "helper-extraction", "Extract shared validation helper."
    )
    matches = tools["architect_memory_query"]("validation helper")
    assert recorded["memory_id"] in {item["memory_id"] for item in matches["matches"]}


def test_architect_approve_gated_by_config(tmp_path: Path) -> None:
    github = MockGitHubAdapter()
    github.seed_pull_request(
        repository="uhvogala/aitobuild_example",
        pull_request=GitHubPullRequest(
            number=3,
            title="PR",
            body="body",
            state="open",
            head_ref="feat",
            base_ref="main",
            repository="uhvogala/aitobuild_example",
        ),
    )
    blocked = _tool_map(build_role_tools(context=_context(tmp_path, github=github))["architect"])
    with pytest.raises(PermissionError, match="APPROVE reviews are disabled"):
        blocked["architect_submit_pr_review"](3, "APPROVE", "LGTM")
    allowed = _tool_map(
        build_role_tools(context=_context(tmp_path, github=github, allow_pr_approve=True))[
            "architect"
        ]
    )
    review = allowed["architect_submit_pr_review"](3, "APPROVE", "LGTM")
    assert review["event"] == "APPROVE"


def test_architect_run_command_rejects_mutations(tmp_path: Path) -> None:
    tools = _tool_map(build_role_tools(context=_context(tmp_path))["architect"])
    with pytest.raises(
        PermissionError, match="mutating|metacharacter|outside allowed|dangerous|expansion"
    ):
        tools["architect_run_command"]("echo hi > src/out.txt")
    with pytest.raises(PermissionError, match="metacharacter|dangerous|expansion"):
        tools["architect_run_command"]("pytest -q; rm -rf /")
    with pytest.raises(PermissionError, match="metacharacter|expansion"):
        tools["architect_run_command"]("pytest $(echo bad)")
    result = tools["architect_run_command"]("pytest -q")
    assert result["exit_code"] == 0


def test_architect_cannot_stop_developer_session(tmp_path: Path) -> None:
    adapter = MagicMock()
    adapter.create_session.return_value = ("architect-abc", "container")
    adapter.close_session.return_value = True
    tools = _tool_map(
        build_architect_tools(
            bash_adapter=MockBashAdapter(),
            filesystem_adapter=MockFilesystemAdapter(),
            workspace_root=tmp_path,
            container_session_adapter=adapter,
        )
    )
    started = tools["architect_start_session"]()
    assert started["session_id"].startswith(ARCHITECT_SESSION_PREFIX)
    with pytest.raises(PermissionError, match="architect-"):
        tools["architect_stop_session"]("developer-sess-1")
    with pytest.raises(PermissionError, match="architect-"):
        tools["architect_start_session"]("dev-hijack")


def test_architect_read_file_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "src"
    target.mkdir()
    (target / "mod.py").write_text("value = 1\n", encoding="utf-8")
    tools = _tool_map(build_role_tools(context=_context(tmp_path))["architect"])
    assert tools["architect_read_file"]("src/mod.py") == "value = 1\n"
    found = tools["architect_find_files"]("**/*.py", "src")
    assert (
        "src/mod.py" in found["results"]
        or any(item.get("path") == "src/mod.py" for item in found.get("results", []))
        or "src/mod.py" in str(found)
    )


def test_pm_plan_approval_requires_operator_store_not_agent_flag(tmp_path: Path) -> None:
    github = MockGitHubAdapter()
    plan_store = PlanDraftStore()
    tools = _tool_map(
        build_role_tools(context=_context(tmp_path, github=github, plan_store=plan_store))["pm"]
    )
    draft = tools["pm_draft_plan"]("Handle empty names", "Reject empty widget names.")
    draft_id = draft["draft_id"]
    tools["pm_set_acceptance_criteria"](draft_id, ["Empty names are rejected."])
    pending = tools["pm_request_plan_approval"](draft_id)
    assert pending["approval_state"] == "awaiting_human_approval"

    # Agent-supplied approved=true must not create the issue.
    with pytest.raises(PermissionError, match="operator-approved"):
        tools["pm_create_issue"](draft_id, approved=True)

    # Operator path marks the store approved (mirrors /internal/pm/plan/approve).
    plan_store.mark_approved(
        draft_id=draft_id,
        approval_request_id=pending["approval_request_id"],
    )
    created = tools["pm_create_issue"](draft_id)
    assert created["created"] is True
    assert created["issue"]["number"] == 1
    assert "Acceptance Criteria" in created["issue"]["body"]


def test_pm_update_and_link_require_operator_issue_write_approval(tmp_path: Path) -> None:
    github = MockGitHubAdapter()
    write_store = IssueWriteApprovalStore()
    github.seed_issue(
        repository="uhvogala/aitobuild_example",
        issue=GitHubIssue(number=3, title="Epic", body="Parent", state="open", labels=("backlog",)),
    )
    github.seed_issue(
        repository="uhvogala/aitobuild_example",
        issue=GitHubIssue(number=4, title="Child", body="Task", state="open", labels=("backlog",)),
    )
    tools = _tool_map(
        build_role_tools(context=_context(tmp_path, github=github, issue_write_store=write_store))[
            "pm"
        ]
    )
    backlog = tools["pm_list_backlog"](labels=["backlog"])
    assert backlog["issue_count"] == 2
    issue = tools["pm_get_issue"](3)
    assert issue["title"] == "Epic"

    pending_update = tools["pm_update_issue"](3, body="Updated body")
    assert pending_update["updated"] is False
    assert pending_update["approval_state"] == "awaiting_human_approval"
    # Agent approved flag / unapproved request id must not bypass the store.
    with pytest.raises(PermissionError, match="human-approved"):
        tools["pm_update_issue"](
            3,
            body="Updated body",
            approval_request_id=pending_update["approval_request_id"],
            approved=True,
        )
    write_store.mark_approved(approval_request_id=pending_update["approval_request_id"])
    updated = tools["pm_update_issue"](
        3, body="Updated body", approval_request_id=pending_update["approval_request_id"]
    )
    assert updated["updated"] is True
    assert updated["issue"]["body"] == "Updated body"

    pending_link = tools["pm_link_issues"](3, 4, "blocks")
    assert pending_link["linked"] is False
    write_store.mark_approved(approval_request_id=pending_link["approval_request_id"])
    linked = tools["pm_link_issues"](
        3, 4, "blocks", approval_request_id=pending_link["approval_request_id"]
    )
    assert linked["relationship"] == "blocks"
    assert github.links


def test_pm_draft_edit_after_approve_invalidates_and_create_uses_snapshot(tmp_path: Path) -> None:
    """Editing after approve clears approval; create uses frozen snapshot, not mutable fields."""
    github = MockGitHubAdapter()
    plan_store = PlanDraftStore()
    tools = _tool_map(
        build_role_tools(context=_context(tmp_path, github=github, plan_store=plan_store))["pm"]
    )
    draft = tools["pm_draft_plan"](
        "Approved title",
        "Approved summary.",
        acceptance_criteria=["Criterion A"],
        labels=["pm"],
    )
    draft_id = draft["draft_id"]
    pending = tools["pm_request_plan_approval"](draft_id)
    plan_store.mark_approved(
        draft_id=draft_id,
        approval_request_id=pending["approval_request_id"],
    )
    approved = plan_store.get(draft_id)
    assert approved["approval_state"] == "approved"
    assert approved["approved_snapshot"]["title"] == "Approved title"

    # Agent rewrites the mutable draft after approval — must invalidate.
    rewritten = tools["pm_draft_plan"](
        "Malicious rewrite",
        "Steal the secrets.",
        acceptance_criteria=["Evil criterion"],
        draft_id=draft_id,
    )
    assert rewritten["approval_state"] == "draft"
    assert rewritten.get("approved_snapshot") is None
    with pytest.raises(PermissionError, match="operator-approved"):
        tools["pm_create_issue"](draft_id)

    # Re-approve, then mutate store fields underneath the snapshot — create still uses snapshot.
    tools["pm_set_acceptance_criteria"](draft_id, ["Criterion A restored"])
    tools["pm_draft_plan"](
        "Approved title",
        "Approved summary.",
        acceptance_criteria=["Criterion A"],
        labels=["pm"],
        draft_id=draft_id,
    )
    pending2 = tools["pm_request_plan_approval"](draft_id)
    plan_store.mark_approved(
        draft_id=draft_id,
        approval_request_id=pending2["approval_request_id"],
    )
    # Simulate tampering with mutable draft fields while leaving approval_state alone.
    plan_store.drafts[draft_id]["title"] = "Tampered title"
    plan_store.drafts[draft_id]["summary"] = "Tampered summary"
    created = tools["pm_create_issue"](draft_id)
    assert created["created"] is True
    assert created["issue"]["title"] == "Approved title"
    assert "Tampered" not in created["issue"]["body"]
    assert "Approved summary" in created["issue"]["body"]


def test_pm_issue_write_execute_uses_stored_approved_payload(tmp_path: Path) -> None:
    """After approve, retries with alternate args still execute the frozen payload."""
    github = MockGitHubAdapter()
    write_store = IssueWriteApprovalStore()
    github.seed_issue(
        repository="uhvogala/aitobuild_example",
        issue=GitHubIssue(number=9, title="Epic", body="Parent", state="open", labels=("backlog",)),
    )
    github.seed_issue(
        repository="uhvogala/aitobuild_example",
        issue=GitHubIssue(number=10, title="Child", body="Task", state="open", labels=("backlog",)),
    )
    tools = _tool_map(
        build_role_tools(context=_context(tmp_path, github=github, issue_write_store=write_store))[
            "pm"
        ]
    )

    pending_update = tools["pm_update_issue"](9, title="Approved title", body="Approved body")
    write_store.mark_approved(approval_request_id=pending_update["approval_request_id"])
    # Agent retries with different content — stored payload wins.
    updated = tools["pm_update_issue"](
        9,
        title="Evil title",
        body="Evil body",
        approval_request_id=pending_update["approval_request_id"],
    )
    assert updated["updated"] is True
    assert updated["issue"]["title"] == "Approved title"
    assert updated["issue"]["body"] == "Approved body"

    pending_link = tools["pm_link_issues"](9, 10, "blocks")
    write_store.mark_approved(approval_request_id=pending_link["approval_request_id"])
    linked = tools["pm_link_issues"](
        9,
        10,
        "duplicates",  # alternate relationship ignored
        approval_request_id=pending_link["approval_request_id"],
    )
    assert linked["relationship"] == "blocks"
    assert github.links[-1]["relationship"] == "blocks"


def test_ghcli_adapter_enforces_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = GhCliGitHubAdapter(
        default_repository="uhvogala/aitobuild",
        allowed_repositories=("uhvogala/aitobuild",),
    )
    calls: list[tuple[str, str]] = []

    def fake_api(endpoint: str, *, method: str = "GET", payload: dict | None = None):
        calls.append((method, endpoint))
        if method == "GET" and endpoint.endswith("/issues/1"):
            return {
                "number": 1,
                "title": "t",
                "body": "b",
                "state": "open",
                "labels": [],
                "html_url": "https://github.com/uhvogala/aitobuild/issues/1",
            }
        return {
            "number": 2,
            "title": "n",
            "body": "b",
            "state": "open",
            "labels": [],
            "html_url": "x",
        }

    monkeypatch.setattr(adapter, "_api", fake_api)
    issue = adapter.get_issue(repository="uhvogala/aitobuild", issue_number=1)
    assert issue.number == 1
    with pytest.raises(PermissionError, match="allowlist"):
        adapter.get_issue(repository="evil/other", issue_number=1)
    with pytest.raises(ValueError, match="allowlist"):
        GhCliGitHubAdapter(allowed_repositories=())


def test_build_github_adapter_gh_cli_requires_allowlist() -> None:
    with pytest.raises(ValueError, match="allowlist"):
        build_github_adapter(mode="gh_cli", allowed_repositories=())
    adapter = build_github_adapter(
        mode="gh_cli",
        default_repository="uhvogala/aitobuild",
        allowed_repositories=("uhvogala/aitobuild",),
    )
    assert isinstance(adapter, GhCliGitHubAdapter)
    mock = build_github_adapter(
        mode="mock",
        allowed_repositories=("uhvogala/aitobuild",),
    )
    assert isinstance(mock, MockGitHubAdapter)
    with pytest.raises(PermissionError, match="allowlist"):
        mock.get_issue(repository="evil/other", issue_number=1)


def test_shared_web_search_and_request_meeting(tmp_path: Path) -> None:
    roles = build_role_tools(context=_context(tmp_path))
    for role_name in ("architect", "pm"):
        tools = _tool_map(roles[role_name])
        search = tools["web_search"]("acceptance criteria")
        assert search["result_count"] >= 1
        meeting = tools["request_meeting"](
            "Clarify architecture boundaries",
            ["architect", "developer"],
        )
        assert meeting["state"] == "requested"
        assert meeting["requested_by"] == role_name
