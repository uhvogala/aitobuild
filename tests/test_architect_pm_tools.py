from __future__ import annotations

from pathlib import Path

import pytest

from aitobuild.agent_tools import DeveloperToolContext, build_role_tools
from aitobuild.meetings import MeetingRegistry
from aitobuild.pm_tools import PlanDraftStore
from aitobuild.policy import ActionClass, AgentRole, assert_role_action_allowed
from aitobuild.tools.architect_memory import ArchitectMemoryStore
from aitobuild.tools.bash import MockBashAdapter
from aitobuild.tools.filesystem import MockFilesystemAdapter
from aitobuild.tools.github import GitHubIssue, GitHubPullRequest, MockGitHubAdapter
from aitobuild.tools.web_search import MockWebSearchAdapter, WebSearchResult


def _tool_map(tools: tuple) -> dict[str, object]:
    return {tool.name: tool for tool in tools}


def _context(tmp_path: Path, *, github: MockGitHubAdapter | None = None) -> DeveloperToolContext:
    return DeveloperToolContext(
        bash_adapter=MockBashAdapter(),
        filesystem_adapter=MockFilesystemAdapter(),
        workspace_root=tmp_path,
        require_human_approval_for_repo_writes=True,
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
        plan_draft_store=PlanDraftStore(),
        default_repository="uhvogala/aitobuild_example",
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
    recorded = tools["architect_memory_record"]("helper-extraction", "Extract shared validation helper.")
    matches = tools["architect_memory_query"]("validation helper")
    assert recorded["memory_id"] in {item["memory_id"] for item in matches["matches"]}


def test_architect_run_command_rejects_mutations(tmp_path: Path) -> None:
    tools = _tool_map(build_role_tools(context=_context(tmp_path))["architect"])
    with pytest.raises(PermissionError, match="mutating|outside allowed"):
        tools["architect_run_command"]("echo hi > src/out.txt")
    result = tools["architect_run_command"]("pytest -q")
    assert result["exit_code"] == 0


def test_architect_read_file_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "src"
    target.mkdir()
    (target / "mod.py").write_text("value = 1\n", encoding="utf-8")
    tools = _tool_map(build_role_tools(context=_context(tmp_path))["architect"])
    assert tools["architect_read_file"]("src/mod.py") == "value = 1\n"
    found = tools["architect_find_files"]("**/*.py", "src")
    assert "src/mod.py" in found["results"] or any(
        item.get("path") == "src/mod.py" for item in found.get("results", [])
    ) or "src/mod.py" in str(found)


def test_pm_plan_approval_gate_and_issue_create(tmp_path: Path) -> None:
    github = MockGitHubAdapter()
    tools = _tool_map(build_role_tools(context=_context(tmp_path, github=github))["pm"])
    draft = tools["pm_draft_plan"]("Handle empty names", "Reject empty widget names.")
    draft_id = draft["draft_id"]
    tools["pm_set_acceptance_criteria"](draft_id, ["Empty names are rejected."])
    pending = tools["pm_request_plan_approval"](draft_id)
    assert pending["approval_state"] == "awaiting_human_approval"
    with pytest.raises(PermissionError):
        tools["pm_create_issue"](False, draft_id, pending["approval_request_id"])
    created = tools["pm_create_issue"](True, draft_id, pending["approval_request_id"])
    assert created["created"] is True
    assert created["issue"]["number"] == 1
    assert "Acceptance Criteria" in created["issue"]["body"]


def test_pm_backlog_update_and_link(tmp_path: Path) -> None:
    github = MockGitHubAdapter()
    github.seed_issue(
        repository="uhvogala/aitobuild_example",
        issue=GitHubIssue(number=3, title="Epic", body="Parent", state="open", labels=("backlog",)),
    )
    github.seed_issue(
        repository="uhvogala/aitobuild_example",
        issue=GitHubIssue(number=4, title="Child", body="Task", state="open", labels=("backlog",)),
    )
    tools = _tool_map(build_role_tools(context=_context(tmp_path, github=github))["pm"])
    backlog = tools["pm_list_backlog"](labels=["backlog"])
    assert backlog["issue_count"] == 2
    issue = tools["pm_get_issue"](3)
    assert issue["title"] == "Epic"
    updated = tools["pm_update_issue"](3, True, None, "Updated body", None, None, None)
    assert updated["issue"]["body"] == "Updated body"
    linked = tools["pm_link_issues"](3, 4, "blocks", True)
    assert linked["relationship"] == "blocks"
    assert github.links


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
