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
        "architect_get_published_pr",
        "architect_read_published_source",
        "architect_read_published_diff",
        "architect_submit_published_pr_review",
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


def test_architect_memory_roundtrip_and_no_freeform_pr_tools(tmp_path: Path) -> None:
    tools = _tool_map(build_role_tools(context=_context(tmp_path))["architect"])
    assert "architect_get_pr" not in tools
    assert "architect_submit_pr_review" not in tools
    recorded = tools["architect_memory_record"](
        "helper-extraction", "Extract shared validation helper."
    )
    matches = tools["architect_memory_query"]("validation helper")
    assert recorded["memory_id"] in {item["memory_id"] for item in matches["matches"]}


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
    assert plan_store.get(draft_id)["approval_state"] == "consumed"


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
    assert plan_store.get(draft_id)["approval_state"] == "consumed"


def test_pm_create_issue_ignores_agent_repository_override(tmp_path: Path) -> None:
    """After approve, create must use snapshot.repository only — ignore agent repository."""
    github = MockGitHubAdapter()
    plan_store = PlanDraftStore()
    tools = _tool_map(
        build_role_tools(context=_context(tmp_path, github=github, plan_store=plan_store))["pm"]
    )
    draft = tools["pm_draft_plan"](
        "Repo-bound plan",
        "Must land in the approved repository.",
        acceptance_criteria=["Created in approved repo"],
        labels=["pm"],
        repository="uhvogala/aitobuild_example",
    )
    draft_id = draft["draft_id"]
    pending = tools["pm_request_plan_approval"](draft_id)
    plan_store.mark_approved(
        draft_id=draft_id,
        approval_request_id=pending["approval_request_id"],
    )
    assert plan_store.get(draft_id)["approved_snapshot"]["repository"] == (
        "uhvogala/aitobuild_example"
    )

    created = tools["pm_create_issue"](draft_id, repository="evil/other-repo")
    assert created["created"] is True
    issue = created["issue"]
    assert issue["repository"] == "uhvogala/aitobuild_example"
    assert "evil/other-repo" not in issue["html_url"]
    assert "uhvogala/aitobuild_example" in issue["html_url"]
    assert "evil/other-repo" not in github.issues
    assert 1 in github.issues["uhvogala/aitobuild_example"]


def test_pm_create_issue_consumes_plan_approval(tmp_path: Path) -> None:
    """Successful create consumes approval; replay without a new operator approve fails."""
    github = MockGitHubAdapter()
    plan_store = PlanDraftStore()
    tools = _tool_map(
        build_role_tools(context=_context(tmp_path, github=github, plan_store=plan_store))["pm"]
    )
    draft = tools["pm_draft_plan"](
        "One-shot plan",
        "Create once.",
        acceptance_criteria=["Only one issue"],
    )
    draft_id = draft["draft_id"]
    pending = tools["pm_request_plan_approval"](draft_id)
    plan_store.mark_approved(
        draft_id=draft_id,
        approval_request_id=pending["approval_request_id"],
    )

    first = tools["pm_create_issue"](draft_id)
    assert first["created"] is True
    assert first["issue"]["number"] == 1
    assert plan_store.get(draft_id)["approval_state"] == "consumed"

    with pytest.raises(PermissionError, match="operator-approved"):
        tools["pm_create_issue"](draft_id)

    # Renew: re-request + operator approve allows another create.
    tools["pm_draft_plan"](
        "One-shot plan",
        "Create once.",
        acceptance_criteria=["Only one issue"],
        draft_id=draft_id,
    )
    pending2 = tools["pm_request_plan_approval"](draft_id)
    plan_store.mark_approved(
        draft_id=draft_id,
        approval_request_id=pending2["approval_request_id"],
    )
    second = tools["pm_create_issue"](draft_id)
    assert second["created"] is True
    assert second["issue"]["number"] == 2
    assert plan_store.get(draft_id)["approval_state"] == "consumed"


def test_pm_issue_write_approval_is_consumed_and_not_replayable(tmp_path: Path) -> None:
    """Issue-write approvals are one-shot after execute (pattern plan create must match)."""
    github = MockGitHubAdapter()
    write_store = IssueWriteApprovalStore()
    github.seed_issue(
        repository="uhvogala/aitobuild_example",
        issue=GitHubIssue(
            number=11, title="Epic", body="Parent", state="open", labels=("backlog",)
        ),
    )
    tools = _tool_map(
        build_role_tools(context=_context(tmp_path, github=github, issue_write_store=write_store))[
            "pm"
        ]
    )
    pending = tools["pm_update_issue"](11, body="Approved once")
    write_store.mark_approved(approval_request_id=pending["approval_request_id"])
    updated = tools["pm_update_issue"](
        11, body="Approved once", approval_request_id=pending["approval_request_id"]
    )
    assert updated["updated"] is True
    assert write_store.requests[pending["approval_request_id"]]["approval_state"] == "consumed"
    with pytest.raises(PermissionError, match="human-approved"):
        tools["pm_update_issue"](
            11, body="Replay", approval_request_id=pending["approval_request_id"]
        )


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


@pytest.mark.parametrize("scenario", ["ok", "executable", "missing", "truncated", "ambiguous", "symlink", "submodule",
                                      "commit_drift", "tree_drift", "blob_drift", "corrupt", "size", "encoding", "unsafe_path", "outside_repo"])
def test_ghcli_pinned_review_reads_are_get_only_and_fail_closed(monkeypatch, scenario):
    import base64
    from aitobuild.tools.github import _git_blob_sha

    adapter = GhCliGitHubAdapter(allowed_repositories=("fixture/widgets",))
    commit_sha, root_sha, nested_sha = "b" * 40, "c" * 40, "d" * 40
    content = b"print('reviewed')\n"
    blob_sha = _git_blob_sha(content)
    commit = {"sha": commit_sha, "tree": {"sha": root_sha}}
    root = {"sha": root_sha, "truncated": False, "tree": [{"path": "src", "type": "tree", "mode": "040000", "sha": nested_sha}]}
    entry = {"path": "probe.py", "type": "blob", "mode": "100644", "sha": blob_sha}
    nested = {"sha": nested_sha, "truncated": False, "tree": [entry]}
    blob = {"sha": blob_sha, "encoding": "base64", "size": len(content), "content": base64.b64encode(content).decode()}
    if scenario == "executable":
        entry["mode"] = "100755"
    elif scenario == "missing":
        nested["tree"] = []
    elif scenario == "truncated":
        nested["truncated"] = True
    elif scenario == "ambiguous":
        nested["tree"] = [entry, dict(entry)]
    elif scenario in {"symlink", "submodule"}:
        entry.update({"mode": "120000" if scenario == "symlink" else "160000", "type": "blob" if scenario == "symlink" else "commit"})
    elif scenario in {"commit_drift", "tree_drift", "blob_drift"}:
        {"commit_drift": commit, "tree_drift": nested, "blob_drift": blob}[scenario]["sha"] = "f" * 40
    elif scenario == "corrupt":
        blob["content"] = base64.b64encode(b"wrong bytes").decode()
    elif scenario == "size":
        blob["size"] = 1048577
    elif scenario == "encoding":
        blob["encoding"] = "utf-8"
    prefix = "repos/fixture/widgets/git/"
    responses = {prefix + "commits/" + commit_sha: commit, prefix + "trees/" + root_sha: root,
                 prefix + "trees/" + nested_sha: nested, prefix + "blobs/" + blob_sha: blob}
    calls = []

    def fake_api(endpoint, *, method="GET", payload=None):
        assert method == "GET" and payload is None
        calls.append(endpoint)
        return responses[endpoint]

    monkeypatch.setattr(adapter, "_api", fake_api)
    kwargs = {"repository": "elsewhere/target" if scenario == "outside_repo" else "fixture/widgets",
              "commit_sha": commit_sha, "path": "../probe.py" if scenario == "unsafe_path" else "src/probe.py"}
    if scenario in {"ok", "executable"}:
        result = adapter.get_file_at_commit(**kwargs)
        assert result.content == content and result.blob_sha == blob_sha
        assert result.mode == ("100755" if scenario == "executable" else "100644")
        assert calls == list(responses)
    elif scenario == "missing":
        assert adapter.get_file_at_commit(**kwargs) is None
    else:
        with pytest.raises((ValueError, PermissionError)):
            adapter.get_file_at_commit(**kwargs)
        if scenario in {"unsafe_path", "outside_repo"}:
            assert calls == []


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


@pytest.mark.parametrize("draft,state", [(True, "open"), (False, "open"), (True, "closed")])
def test_ghcli_recovers_existing_task_pr_without_local_number(monkeypatch, draft, state) -> None:
    from aitobuild.policy import AgentRole

    adapter = GhCliGitHubAdapter(allowed_repositories=("fixture/widgets",))
    raw = {"number": 8, "title": "Trial", "body": "Original", "state": state, "draft": draft,
           "head": {"ref": "aitobuild/issue-7-task", "sha": "b" * 40,
                    "repo": {"full_name": "fixture/widgets"}}, "base": {"ref": "main"}}
    calls = []

    def api(endpoint, *, method="GET", payload=None):
        calls.append((method, endpoint))
        if method == "POST":
            raise AssertionError("Recovery must not create a second PR")
        if "/pulls?" in endpoint:
            return [raw]
        if endpoint.endswith("/files?per_page=100"):
            return []
        return {**raw, **(payload or {})}

    monkeypatch.setattr(adapter, "_api", api)
    arguments = dict(role=AgentRole.DEVELOPER, repository="fixture/widgets", title="Trial",
                     body="Updated", head_branch="aitobuild/issue-7-task", base_ref="main",
                     issue_number=7, approved=True, require_human_approval_for_repo_writes=True)
    if draft and state == "open":
        pull = adapter.create_or_update_draft_pull_request(**arguments)
        assert pull.number == 8 and pull.body == "Updated"
        assert any(method == "PATCH" for method, _ in calls)
    else:
        with pytest.raises(ValueError, match="open draft"):
            adapter.create_or_update_draft_pull_request(**arguments)
        assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("remote", ["matching", "wrong_tree", "wrong_parent", "wrong_message",
                                    "missing", "uncertain_ref", "forbidden"])
def test_ghcli_recovers_only_the_exact_remote_commit(monkeypatch, remote) -> None:
    from aitobuild.policy import AgentRole
    from aitobuild.tools.github import GitHubBlobChange, _git_blob_sha

    adapter = GhCliGitHubAdapter(allowed_repositories=("fixture/widgets",))
    base, old_head, new_head, tree = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    content = b"value = 1\n"
    calls = []
    ref_created = False

    def api(endpoint, *, method="GET", payload=None):
        nonlocal ref_created
        calls.append((method, endpoint))
        if endpoint.endswith("/git/commits/" + base):
            return {"tree": {"sha": "e" * 40}}
        if "/git/ref/heads/" in endpoint:
            if remote == "forbidden":
                raise RuntimeError("gh: Forbidden (HTTP 403)")
            if remote == "missing" or remote == "uncertain_ref" and not ref_created:
                raise RuntimeError("gh: Not Found (HTTP 404)")
            return {"object": {"sha": old_head}}
        if endpoint.endswith("/git/commits/" + old_head):
            return {"tree": {"sha": "e" * 40 if remote == "wrong_tree" else tree},
                    "parents": [{"sha": "f" * 40 if remote == "wrong_parent" else base,
                                 "url": "https://api.github.com/example"}],
                    "message": "Different" if remote == "wrong_message" else "Trial"}
        if endpoint.endswith("/git/blobs"):
            return {"sha": _git_blob_sha(content)}
        if endpoint.endswith("/git/trees"):
            return {"sha": tree}
        if endpoint.endswith("/git/commits"):
            return {"sha": new_head, "tree": {"sha": tree}}
        if endpoint.endswith("/git/refs"):
            if remote == "uncertain_ref":
                ref_created = True
                raise RuntimeError("Lost transport after remote ref creation")
            return {"object": {"sha": new_head}}
        raise AssertionError(endpoint)

    monkeypatch.setattr(adapter, "_api", api)
    arguments = dict(role=AgentRole.DEVELOPER, repository="fixture/widgets",
                     branch="aitobuild/issue-7-task", base_sha=base, commit_message="Trial",
                     files={"src/probe.py": GitHubBlobChange(mode="100644", content=content,
                                                           blob_sha=_git_blob_sha(content))},
                     approved=True, require_human_approval_for_repo_writes=True)
    if remote == "forbidden":
        with pytest.raises(RuntimeError, match="HTTP 403"):
            adapter.upsert_branch_commit(**arguments)
    elif remote in {"wrong_tree", "wrong_parent", "wrong_message"}:
        with pytest.raises(ValueError, match="approved publication"):
            adapter.upsert_branch_commit(**arguments)
    else:
        assert adapter.upsert_branch_commit(**arguments) == (new_head if remote == "missing" else old_head)
    creates = [(method, endpoint) for method, endpoint in calls
               if endpoint.endswith(("/git/commits", "/git/refs"))]
    assert bool(creates) is (remote in {"missing", "uncertain_ref"})
    assert all(method != "PATCH" for method, _ in calls)


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


def test_mock_publish_commit_and_draft_pr_respect_allowlist() -> None:
    from aitobuild.policy import AgentRole
    from aitobuild.tools.github import GitHubBlobChange, _git_blob_sha

    adapter = MockGitHubAdapter(
        allowed_repositories=frozenset({"uhvogala/aitobuild_example"}),
        enforce_allowlist=True,
    )
    base = "a" * 40
    content = b"x = 1\n"
    change = GitHubBlobChange(mode="100644", content=content, blob_sha=_git_blob_sha(content))
    head = adapter.upsert_branch_commit(
        role=AgentRole.DEVELOPER,
        repository="uhvogala/aitobuild_example",
        branch="aitobuild/issue-1-deadbeefdeadbeef",
        base_sha=base,
        commit_message="aitobuild: implement #1",
        files={"src/probe.py": change},
        approved=True,
        require_human_approval_for_repo_writes=True,
    )
    assert len(head) == 40
    pull = adapter.create_or_update_draft_pull_request(
        role=AgentRole.DEVELOPER,
        repository="uhvogala/aitobuild_example",
        title="aitobuild: probe",
        body="Closes #1",
        head_branch="aitobuild/issue-1-deadbeefdeadbeef",
        base_ref="main",
        issue_number=1,
        approved=True,
        require_human_approval_for_repo_writes=True,
    )
    assert pull.draft is True and pull.number == 1
    updated = adapter.create_or_update_draft_pull_request(
        role=AgentRole.DEVELOPER,
        repository="uhvogala/aitobuild_example",
        title="aitobuild: probe",
        body="Closes #1\nupdated",
        head_branch="aitobuild/issue-1-deadbeefdeadbeef",
        base_ref="main",
        issue_number=1,
        existing_pull_number=1,
        approved=True,
        require_human_approval_for_repo_writes=True,
    )
    assert updated.number == 1 and "updated" in updated.body
    with pytest.raises(ValueError, match="aitobuild/"):
        adapter.upsert_branch_commit(
            role=AgentRole.DEVELOPER,
            repository="uhvogala/aitobuild_example",
            branch="feature/not-scoped",
            base_sha=base,
            commit_message="nope",
            files={"a.py": change},
            approved=True,
            require_human_approval_for_repo_writes=True,
        )
    with pytest.raises(PermissionError, match="allowlist"):
        adapter.upsert_branch_commit(
            role=AgentRole.DEVELOPER,
            repository="evil/other",
            branch="aitobuild/issue-1-deadbeefdeadbeef",
            base_sha=base,
            commit_message="nope",
            files={"a.py": change},
            approved=True,
            require_human_approval_for_repo_writes=True,
        )


@pytest.mark.parametrize("adapter_kind", ["mock", "gh_cli"])
@pytest.mark.parametrize(
    ("head_branch", "base_ref", "message"),
    [
        ("main", "main", "head_branch must be an aitobuild/"),
        ("feature/not-scoped", "main", "head_branch must be an aitobuild/"),
        ("aitobuild/", "main", "head_branch must be an aitobuild/"),
        ("aitobuild/../main", "main", "head_branch must be an aitobuild/"),
        ("aitobuild/x//y", "main", "head_branch must be an aitobuild/"),
        ("aitobuild/x.lock", "main", "head_branch must be an aitobuild/"),
        ("aitobuild/issue-1-task", "main.lock", "base_ref must be a plain branch"),
        ("aitobuild/issue-1-task", "aitobuild/issue-1-task", "base_ref must differ"),
        ("aitobuild/issue-1-task", "refs/heads/main", "base_ref must be a plain branch"),
        ("aitobuild/issue-1-task", "main..x", "base_ref must be a plain branch"),
        ("aitobuild/issue-1-task", "-main", "base_ref must be a plain branch"),
    ],
)
def test_draft_pr_adapters_refuse_unscoped_head_or_bad_base(
    adapter_kind: str, head_branch: str, base_ref: str, message: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aitobuild.policy import AgentRole

    if adapter_kind == "mock":
        adapter = MockGitHubAdapter(
            allowed_repositories=frozenset({"fixture/widgets"}), enforce_allowlist=True,
        )
    else:
        adapter = GhCliGitHubAdapter(allowed_repositories=("fixture/widgets",))

        def api(*_args, **_kwargs):
            raise AssertionError("Invalid refs must be refused before any GitHub call")

        monkeypatch.setattr(adapter, "_api", api)
    with pytest.raises(ValueError, match=message):
        adapter.create_or_update_draft_pull_request(
            role=AgentRole.DEVELOPER, repository="fixture/widgets", title="Trial", body="Closes #1",
            head_branch=head_branch, base_ref=base_ref, issue_number=1, approved=True,
            require_human_approval_for_repo_writes=True,
        )
    if adapter_kind == "mock":
        assert not adapter.pull_requests.get("fixture/widgets")


@pytest.mark.parametrize("adapter_kind", ["mock", "gh_cli"])
def test_pr_review_records_bound_commit_id(adapter_kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from aitobuild.policy import AgentRole

    head = "b" * 40
    if adapter_kind == "mock":
        from aitobuild.tools.github import GitHubBlobChange, _git_blob_sha

        adapter = MockGitHubAdapter(
            allowed_repositories=frozenset({"fixture/widgets"}), enforce_allowlist=True,
        )
        content = b"x = 1\n"
        adapter.upsert_branch_commit(
            role=AgentRole.DEVELOPER, repository="fixture/widgets", branch="aitobuild/issue-1-task",
            base_sha="a" * 40, commit_message="aitobuild: implement #1",
            files={"src/probe.py": GitHubBlobChange(mode="100644", content=content, blob_sha=_git_blob_sha(content))},
            approved=True, require_human_approval_for_repo_writes=True,
        )
        pull = adapter.create_or_update_draft_pull_request(
            role=AgentRole.DEVELOPER, repository="fixture/widgets", title="Trial", body="Closes #1",
            head_branch="aitobuild/issue-1-task", base_ref="main", issue_number=1, approved=True,
            require_human_approval_for_repo_writes=True,
        )
        assert pull.head_sha is not None
        head = pull.head_sha
        number = pull.number
    else:
        adapter = GhCliGitHubAdapter(allowed_repositories=("fixture/widgets",))
        number = 8
        sent: list[dict] = []
        raw = {"number": 8, "title": "Trial", "body": "Body", "state": "open", "draft": True,
               "head": {"ref": "aitobuild/issue-1-task", "sha": head,
                        "repo": {"full_name": "fixture/widgets"}}, "base": {"ref": "main"}}

        def api(endpoint, *, method="GET", payload=None):
            if method == "POST":
                sent.append(dict(payload or {}))
                return {"id": 1, "html_url": "https://github.com/fixture/widgets/pull/8#r1"}
            return raw

        monkeypatch.setattr(adapter, "_api", api)
    review = adapter.submit_pr_review(
        role=AgentRole.ARCHITECT, repository="fixture/widgets", pull_number=number, event="COMMENT",
        body="Looks scoped.", commit_id=head.upper(),
    )
    assert review.commit_id == head.lower()
    assert review.to_dict()["commit_id"] == head.lower()
    if adapter_kind == "gh_cli":
        assert sent and sent[-1].get("commit_id") == head.lower()
