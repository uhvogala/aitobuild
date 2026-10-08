"""GitHub adapters: mock-first interface with optional gh CLI backend."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha1, sha256
import json
from pathlib import PurePosixPath
import re
from subprocess import CalledProcessError, TimeoutExpired, run
from typing import Any, Literal, Protocol
from urllib.parse import urlencode
from uuid import uuid4

from aitobuild.policy import (
    ActionClass,
    AgentRole,
    assert_repo_write_approval,
    assert_role_action_allowed,
)

AdvanceOutcome = Literal["parent", "ours", "moved"]


def normalize_repository_name(repository: str) -> str:
    cleaned = repository.strip().lower()
    if not cleaned or "/" not in cleaned:
        raise ValueError("repository must be owner/name")
    owner, _, name = cleaned.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError("repository must be owner/name")
    return f"{owner}/{name}"


def assert_repository_allowed(
    repository: str,
    *,
    allowed_repositories: frozenset[str] | None,
    enforce_allowlist: bool,
) -> str:
    resolved = normalize_repository_name(repository)
    if not enforce_allowlist:
        return resolved
    allowed = allowed_repositories or frozenset()
    if resolved not in allowed:
        raise PermissionError(
            f"Repository {resolved} is outside AITOBUILD_GITHUB_ALLOWED_REPOS allowlist"
        )
    return resolved


@dataclass(slots=True, frozen=True)
class GitHubIssueProposal:
    title: str
    body: str


@dataclass(slots=True, frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...] = ()
    html_url: str | None = None
    repository: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "labels": list(self.labels),
            "html_url": self.html_url,
            "repository": self.repository,
        }


@dataclass(slots=True, frozen=True)
class GitHubPullRequest:
    number: int
    title: str
    body: str
    state: str
    head_ref: str
    base_ref: str
    draft: bool = False
    html_url: str | None = None
    repository: str | None = None
    changed_files: tuple[str, ...] = ()
    head_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "head_ref": self.head_ref,
            "base_ref": self.base_ref,
            "draft": self.draft,
            "html_url": self.html_url,
            "repository": self.repository,
            "changed_files": list(self.changed_files),
            "head_sha": self.head_sha,
        }


@dataclass(slots=True, frozen=True)
class GitHubPullRequestReview:
    pull_number: int
    event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
    body: str
    review_id: str | None = None
    html_url: str | None = None
    commit_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pull_number": self.pull_number,
            "event": self.event,
            "body": self.body,
            "review_id": self.review_id,
            "html_url": self.html_url,
            "commit_id": self.commit_id,
        }



@dataclass(slots=True, frozen=True)
class GitHubBlobChange:
    """One scoped file change published as a Git blob (bytes + mode + git blob SHA)."""

    mode: Literal["100644", "100755"]
    content: bytes
    blob_sha: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "content_base64": base64.b64encode(self.content).decode("ascii"),
            "blob_sha": self.blob_sha,
        }


@dataclass(frozen=True)
class GitHubPinnedChange:
    """A pinned file change by identity only (mode + git blob SHA), as saved in an approval snapshot."""

    mode: Literal["100644", "100755"]
    blob_sha: str


def pinned_changes(
    files: Mapping[str, GitHubBlobChange | None],
) -> dict[str, GitHubPinnedChange | None]:
    return {path: None if change is None else GitHubPinnedChange(mode=change.mode, blob_sha=change.blob_sha)
            for path, change in files.items()}


def pinned_changes_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, GitHubPinnedChange | None]:
    """Rebuild pinned changes from a snapshot's `blob_shas`/`file_modes` without touching the checkout."""
    blob_shas, file_modes = snapshot.get("blob_shas"), snapshot.get("file_modes")
    if not isinstance(blob_shas, dict) or not isinstance(file_modes, dict) or set(blob_shas) != set(file_modes):
        raise ValueError("Snapshot pinned changes are malformed")
    changes: dict[str, GitHubPinnedChange | None] = {}
    for path, blob_sha in blob_shas.items():
        mode = file_modes[path]
        if (blob_sha is None) != (mode is None):
            raise ValueError("Snapshot pinned changes are malformed")
        changes[path] = None if blob_sha is None else GitHubPinnedChange(mode=mode, blob_sha=blob_sha)
    _validate_pinned_changes(changes)
    return changes


class GitHubAdapter(Protocol):
    def get_issue(self, *, repository: str, issue_number: int) -> GitHubIssue: ...

    def list_issues(
        self,
        *,
        repository: str,
        state: str = "open",
        labels: tuple[str, ...] = (),
        limit: int = 30,
    ) -> tuple[GitHubIssue, ...]: ...

    def create_issue(
        self,
        *,
        role: AgentRole,
        repository: str,
        title: str,
        body: str,
        labels: tuple[str, ...] = (),
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> GitHubIssue: ...

    def update_issue(
        self,
        *,
        role: AgentRole,
        repository: str,
        issue_number: int,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: tuple[str, ...] | None = None,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> GitHubIssue: ...

    def link_issues(
        self,
        *,
        role: AgentRole,
        repository: str,
        issue_number: int,
        related_issue_number: int,
        relationship: str = "blocks",
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> dict[str, Any]: ...

    def get_pull_request(self, *, repository: str, pull_number: int) -> GitHubPullRequest: ...

    def get_blob(self, *, repository: str, blob_sha: str) -> bytes: ...

    def get_file_at_commit(self, *, repository: str, commit_sha: str, path: str) -> GitHubBlobChange | None: ...

    def submit_pr_review(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
        body: str,
        commit_id: str | None = None,
    ) -> GitHubPullRequestReview: ...

    def create_issue_proposal(
        self,
        *,
        role: AgentRole,
        proposal: GitHubIssueProposal,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> None: ...

    def upsert_branch_commit(
        self,
        *,
        role: AgentRole,
        repository: str,
        branch: str,
        base_sha: str,
        commit_message: str,
        files: Mapping[str, GitHubBlobChange | None],
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        before_write: Callable[[], object] | None = None,
    ) -> str: ...

    def create_or_update_draft_pull_request(
        self,
        *,
        role: AgentRole,
        repository: str,
        title: str,
        body: str,
        head_branch: str,
        base_ref: str,
        issue_number: int,
        existing_pull_number: int | None = None,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        before_write: Callable[[], object] | None = None,
    ) -> GitHubPullRequest: ...

    def advance_draft_pull_request_head(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        head_branch: str,
        base_ref: str,
        expected_head_sha: str,
        commit_message: str,
        files: Mapping[str, GitHubBlobChange | None],
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        before_write: Callable[[], object] | None = None,
    ) -> GitHubPullRequest: ...

    def reconcile_advanced_head(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        head_branch: str,
        base_ref: str,
        expected_head_sha: str,
        commit_message: str,
        changes: Mapping[str, GitHubPinnedChange | None],
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> tuple[AdvanceOutcome, GitHubPullRequest]: ...


@dataclass
class MockGitHubAdapter:
    """In-memory GitHub adapter used by tests and local mock mode."""

    proposals: list[GitHubIssueProposal] = field(default_factory=list)
    issues: dict[str, dict[int, GitHubIssue]] = field(default_factory=dict)
    pull_requests: dict[str, dict[int, GitHubPullRequest]] = field(default_factory=dict)
    reviews: list[GitHubPullRequestReview] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    branch_commits: list[dict[str, Any]] = field(default_factory=list)
    allowed_repositories: frozenset[str] | None = None
    enforce_allowlist: bool = False
    _next_issue: int = 1
    _next_pr: int = 1
    _branch_heads: dict[str, dict[str, str]] = field(default_factory=dict)
    commit_files: dict[tuple[str, str], dict[str, GitHubBlobChange]] = field(default_factory=dict)

    def _resolve_repository(self, repository: str) -> str:
        return assert_repository_allowed(
            repository,
            allowed_repositories=self.allowed_repositories,
            enforce_allowlist=self.enforce_allowlist,
        )

    def seed_issue(self, *, repository: str, issue: GitHubIssue) -> None:
        repository = normalize_repository_name(repository)
        self.issues.setdefault(repository, {})[issue.number] = issue
        self._next_issue = max(self._next_issue, issue.number + 1)

    def seed_pull_request(self, *, repository: str, pull_request: GitHubPullRequest) -> None:
        repository = normalize_repository_name(repository)
        self.pull_requests.setdefault(repository, {})[pull_request.number] = pull_request
        self._next_pr = max(self._next_pr, pull_request.number + 1)

    def get_issue(self, *, repository: str, issue_number: int) -> GitHubIssue:
        repository = self._resolve_repository(repository)
        try:
            return self.issues[repository][issue_number]
        except KeyError as exc:
            raise LookupError(f"Issue #{issue_number} not found in {repository}") from exc

    def list_issues(
        self,
        *,
        repository: str,
        state: str = "open",
        labels: tuple[str, ...] = (),
        limit: int = 30,
    ) -> tuple[GitHubIssue, ...]:
        repository = self._resolve_repository(repository)
        items = list(self.issues.get(repository, {}).values())
        if state != "all":
            items = [item for item in items if item.state == state]
        if labels:
            wanted = set(labels)
            items = [item for item in items if wanted.issubset(set(item.labels))]
        items.sort(key=lambda item: item.number, reverse=True)
        return tuple(items[: max(1, min(limit, 100))])

    def create_issue(
        self,
        *,
        role: AgentRole,
        repository: str,
        title: str,
        body: str,
        labels: tuple[str, ...] = (),
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> GitHubIssue:
        assert_role_action_allowed(role, ActionClass.ISSUE_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repository = self._resolve_repository(repository)
        number = self._next_issue
        self._next_issue += 1
        issue = GitHubIssue(
            number=number,
            title=title.strip(),
            body=body,
            state="open",
            labels=tuple(label.strip() for label in labels if label.strip()),
            html_url=f"https://github.com/{repository}/issues/{number}",
            repository=repository,
        )
        self.issues.setdefault(repository, {})[number] = issue
        return issue

    def update_issue(
        self,
        *,
        role: AgentRole,
        repository: str,
        issue_number: int,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: tuple[str, ...] | None = None,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> GitHubIssue:
        assert_role_action_allowed(role, ActionClass.ISSUE_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repository = self._resolve_repository(repository)
        current = self.get_issue(repository=repository, issue_number=issue_number)
        updated = GitHubIssue(
            number=current.number,
            title=title.strip() if title is not None else current.title,
            body=body if body is not None else current.body,
            state=state if state is not None else current.state,
            labels=tuple(labels) if labels is not None else current.labels,
            html_url=current.html_url,
            repository=current.repository or repository,
        )
        self.issues[repository][issue_number] = updated
        return updated

    def link_issues(
        self,
        *,
        role: AgentRole,
        repository: str,
        issue_number: int,
        related_issue_number: int,
        relationship: str = "blocks",
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.ISSUE_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repository = self._resolve_repository(repository)
        self.get_issue(repository=repository, issue_number=issue_number)
        self.get_issue(repository=repository, issue_number=related_issue_number)
        record = {
            "repository": repository,
            "issue_number": issue_number,
            "related_issue_number": related_issue_number,
            "relationship": relationship.strip() or "blocks",
        }
        self.links.append(record)
        return record

    def get_pull_request(self, *, repository: str, pull_number: int) -> GitHubPullRequest:
        repository = self._resolve_repository(repository)
        try:
            pull = self.pull_requests[repository][pull_number]
        except KeyError as exc:
            raise LookupError(f"Pull request #{pull_number} not found in {repository}") from exc
        if pull.head_sha:
            return pull
        head_sha = self._branch_heads.get(repository, {}).get(pull.head_ref)
        if not head_sha:
            return pull
        enriched = GitHubPullRequest(
            number=pull.number,
            title=pull.title,
            body=pull.body,
            state=pull.state,
            head_ref=pull.head_ref,
            base_ref=pull.base_ref,
            draft=pull.draft,
            html_url=pull.html_url,
            repository=pull.repository,
            changed_files=pull.changed_files,
            head_sha=head_sha,
        )
        self.pull_requests[repository][pull_number] = enriched
        return enriched

    def get_blob(self, *, repository: str, blob_sha: str) -> bytes:
        repository = self._resolve_repository(repository)
        _validate_blob_sha(blob_sha)
        for (repo, _), files in self.commit_files.items():
            if repo == repository:
                for change in files.values():
                    if change.blob_sha == blob_sha:
                        return _decode_blob(base64.b64encode(change.content).decode("ascii"), blob_sha)
        for commit in self.branch_commits:
            if commit["repository"] == repository:
                for change in commit["files"].values():
                    if change is not None and change["blob_sha"] == blob_sha:
                        return _decode_blob(change["content_base64"], blob_sha)
        raise LookupError("Published blob not found")

    def get_file_at_commit(self, *, repository: str, commit_sha: str, path: str) -> GitHubBlobChange | None:
        repo = self._resolve_repository(repository)
        _validate_read_path(commit_sha, path)
        if (repo, commit_sha) not in self.commit_files:
            raise LookupError("Commit not found")
        return self.commit_files[(repo, commit_sha)].get(path)

    def submit_pr_review(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
        body: str,
        commit_id: str | None = None,
    ) -> GitHubPullRequestReview:
        assert_role_action_allowed(role, ActionClass.PR_REVIEW)
        repository = self._resolve_repository(repository)
        pull = self.get_pull_request(repository=repository, pull_number=pull_number)
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise ValueError("event must be APPROVE, REQUEST_CHANGES, or COMMENT")
        if not body.strip():
            raise ValueError("review body must be non-empty")
        cleaned_commit: str | None = None
        if commit_id is not None:
            cleaned_commit = commit_id.strip().lower()
            if re.fullmatch(r"[0-9a-f]{40}", cleaned_commit) is None:
                raise ValueError("commit_id must be a 40-char lowercase hex SHA")
            if pull.head_sha and pull.head_sha.lower() != cleaned_commit:
                raise ValueError("commit_id does not match the pull request head SHA")
        review = GitHubPullRequestReview(
            pull_number=pull_number,
            event=event,
            body=body.strip(),
            review_id=f"mock-review-{uuid4().hex[:8]}",
            html_url=f"https://github.com/{repository}/pull/{pull_number}#pullrequestreview",
            commit_id=cleaned_commit,
        )
        self.reviews.append(review)
        return review


    def upsert_branch_commit(
        self,
        *,
        role: AgentRole,
        repository: str,
        branch: str,
        base_sha: str,
        commit_message: str,
        files: Mapping[str, GitHubBlobChange | None],
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        before_write: Callable[[], object] | None = None,
    ) -> str:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        _validate_publish_commit_inputs(
            branch=branch, base_sha=base_sha, commit_message=commit_message, files=files
        )
        head_sha, payload = self._mock_commit(repo, branch.strip(), base_sha, commit_message, files)
        _check_write_budget(before_write)
        self._record_mock_commit(repo, head_sha, payload, base_sha, files)
        self._branch_heads.setdefault(repo, {})[branch.strip()] = head_sha
        return head_sha

    def _mock_commit(
        self, repo: str, branch: str, base_sha: str, commit_message: str,
        files: Mapping[str, GitHubBlobChange | None],
    ) -> tuple[str, dict[str, Any]]:
        encoded_files = {
            path: None if change is None else change.to_dict()
            for path, change in sorted(files.items())
        }
        payload = {
            "repository": repo,
            "branch": branch,
            "base_sha": base_sha.lower(),
            "commit_message": commit_message.strip(),
            "files": encoded_files,
            "tree_fingerprint": _tree_fingerprint(files),
        }
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40], payload

    def _record_mock_commit(
        self, repo: str, head_sha: str, payload: dict[str, Any], base_sha: str,
        files: Mapping[str, GitHubBlobChange | None],
    ) -> None:
        base_files = self.commit_files.setdefault((repo, base_sha), {})
        head_files = dict(base_files)
        for path, change in files.items():
            if change is None:
                head_files.pop(path, None)
            else:
                head_files[path] = change
        self.commit_files[(repo, head_sha)] = head_files
        self.branch_commits.append({**payload, "head_sha": head_sha})

    def advance_draft_pull_request_head(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        head_branch: str,
        base_ref: str,
        expected_head_sha: str,
        commit_message: str,
        files: Mapping[str, GitHubBlobChange | None],
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        before_write: Callable[[], object] | None = None,
    ) -> GitHubPullRequest:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        branch, base, expected = _validate_advance_inputs(
            pull_number=pull_number, head_branch=head_branch, base_ref=base_ref,
            expected_head_sha=expected_head_sha, commit_message=commit_message, files=files,
        )
        current = self.get_pull_request(repository=repo, pull_number=pull_number)
        _validate_advance_target(current, pull_number=pull_number, head_branch=branch, base_ref=base)
        head_sha, payload = self._mock_commit(repo, branch, expected, commit_message, files)
        remote_head = self._branch_heads.get(repo, {}).get(branch) or current.head_sha
        if remote_head != head_sha:
            if remote_head != expected:
                raise ValueError("Pull request head moved from the pinned SHA; refusing stale correction")
            _check_write_budget(before_write)
            self._record_mock_commit(repo, head_sha, payload, expected, files)
            self._branch_heads.setdefault(repo, {})[branch] = head_sha
        advanced = replace(current, head_sha=head_sha)
        self.pull_requests[repo][pull_number] = advanced
        return advanced

    def reconcile_advanced_head(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        head_branch: str,
        base_ref: str,
        expected_head_sha: str,
        commit_message: str,
        changes: Mapping[str, GitHubPinnedChange | None],
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> tuple[AdvanceOutcome, GitHubPullRequest]:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        branch, _, expected = _validate_reconcile_inputs(
            pull_number=pull_number, head_branch=head_branch, base_ref=base_ref,
            expected_head_sha=expected_head_sha, commit_message=commit_message, changes=changes,
        )
        current = self.get_pull_request(repository=repo, pull_number=pull_number)
        remote_head = self._branch_heads.get(repo, {}).get(branch) or current.head_sha
        if remote_head == expected:
            return "parent", replace(current, head_sha=remote_head)
        return ("ours" if remote_head is not None
                and self._mock_commit_has_changes(repo, remote_head, expected, commit_message, changes)
                else "moved"), replace(current, head_sha=remote_head)

    def _mock_commit_has_changes(
        self, repo: str, commit_sha: str, parent_sha: str, commit_message: str,
        changes: Mapping[str, GitHubPinnedChange | None],
    ) -> bool:
        """Same identity rule as the live adapter: sole parent, message, exact tree by mode + blob SHA."""
        commit = next((item for item in self.branch_commits if item.get("head_sha") == commit_sha), None)
        if commit is None or commit.get("base_sha") != parent_sha.lower() or commit.get("commit_message") != commit_message.strip():
            return False

        def tree(sha: str) -> dict[str, tuple[str, str]]:
            return {path: (change.mode, change.blob_sha) for path, change in self.commit_files.get((repo, sha), {}).items()}

        expected = tree(parent_sha)
        for path, change in changes.items():
            if change is None:
                expected.pop(path, None)
            else:
                expected[path] = (change.mode, change.blob_sha)
        return tree(commit_sha) == expected

    def create_or_update_draft_pull_request(
        self,
        *,
        role: AgentRole,
        repository: str,
        title: str,
        body: str,
        head_branch: str,
        base_ref: str,
        issue_number: int,
        existing_pull_number: int | None = None,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        before_write: Callable[[], object] | None = None,
    ) -> GitHubPullRequest:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        cleaned_title = title.strip()
        cleaned_body = body.strip()
        cleaned_head = _validate_aitobuild_branch_name(head_branch, field_name="head_branch")
        cleaned_base = base_ref.strip()
        if not cleaned_title or not cleaned_body or not cleaned_base:
            raise ValueError("draft PR title, body, head_branch, and base_ref must be non-empty")
        _validate_base_ref_name(cleaned_base, head_branch=cleaned_head)
        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if existing_pull_number is not None and (
            type(existing_pull_number) is not int or existing_pull_number <= 0
        ):
            raise ValueError("existing_pull_number must be a positive integer when provided")
        _check_write_budget(before_write)
        repo_prs = self.pull_requests.setdefault(repo, {})
        if existing_pull_number is None:
            matches = [pull for pull in repo_prs.values()
                       if pull.head_ref == cleaned_head and pull.base_ref == cleaned_base]
            open_matches = [pull for pull in matches if pull.state == "open"]
            if matches and len(open_matches) != 1:
                raise ValueError("Existing task pull request must be one open draft")
            if open_matches:
                existing_pull_number = open_matches[0].number
        if existing_pull_number is not None:
            current = repo_prs.get(existing_pull_number)
            if current is None:
                raise ValueError(f"Pull request #{existing_pull_number} was not found")
            if current.head_ref != cleaned_head:
                raise ValueError(
                    f"Pull request #{existing_pull_number} head ref does not match the delivery branch"
                )
            if not current.draft or current.state != "open":
                raise ValueError(
                    f"Pull request #{existing_pull_number} must remain an open draft; refusing update"
                )
            updated = GitHubPullRequest(
                number=existing_pull_number,
                title=cleaned_title,
                body=cleaned_body,
                state=current.state,
                head_ref=cleaned_head,
                base_ref=cleaned_base,
                draft=True,
                html_url=current.html_url or f"https://example.test/{repo}/pull/{existing_pull_number}",
                repository=repo,
                changed_files=current.changed_files,
                head_sha=self._branch_heads.get(repo, {}).get(cleaned_head) or current.head_sha,
            )
            repo_prs[existing_pull_number] = updated
            return updated
        number = self._next_pr
        self._next_pr += 1
        created = GitHubPullRequest(
            number=number,
            title=cleaned_title,
            body=cleaned_body,
            state="open",
            head_ref=cleaned_head,
            base_ref=cleaned_base,
            draft=True,
            html_url=f"https://example.test/{repo}/pull/{number}",
            repository=repo,
            changed_files=(),
            head_sha=self._branch_heads.get(repo, {}).get(cleaned_head),
        )
        repo_prs[number] = created
        return created

    def create_issue_proposal(
        self,
        *,
        role: AgentRole,
        proposal: GitHubIssueProposal,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> None:
        assert_role_action_allowed(role, ActionClass.ISSUE_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        self.proposals.append(proposal)


class GhCliGitHubAdapter:
    """Least-privilege GitHub adapter backed by the authenticated `gh` CLI."""

    def __init__(
        self,
        *,
        default_repository: str | None = None,
        allowed_repositories: tuple[str, ...] | frozenset[str] | None = None,
        enforce_allowlist: bool = True,
    ) -> None:
        self.default_repository = (
            normalize_repository_name(default_repository) if default_repository else None
        )
        allowed = frozenset(
            normalize_repository_name(item) for item in (allowed_repositories or ())
        )
        if self.default_repository is not None:
            allowed = allowed | {self.default_repository}
        self.allowed_repositories = allowed
        self.enforce_allowlist = enforce_allowlist
        if self.enforce_allowlist and not self.allowed_repositories:
            raise ValueError(
                "GhCliGitHubAdapter requires a non-empty repository allowlist "
                "(AITOBUILD_GITHUB_ALLOWED_REPOS and/or default repository)"
            )
        self.proposals: list[GitHubIssueProposal] = []

    def _resolve_repository(self, repository: str | None) -> str:
        resolved = (repository or self.default_repository or "").strip()
        if not resolved:
            raise ValueError("repository must be owner/name")
        return assert_repository_allowed(
            resolved,
            allowed_repositories=self.allowed_repositories,
            enforce_allowlist=self.enforce_allowlist,
        )

    def _api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        command = ["gh", "api", "-X", method, endpoint]
        input_text = None
        if payload is not None:
            command.extend(["--input", "-"])
            input_text = json.dumps(payload)
        try:
            completed = run(
                command,
                input=input_text,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except TimeoutExpired as exc:
            raise RuntimeError(f"gh api timed out for {method} {endpoint}") from exc
        except CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"gh api failed for {method} {endpoint}: {detail}") from exc
        text = completed.stdout.strip()
        if not text:
            return {}
        return json.loads(text)

    def _matching_publish_head(
        self, *, repository: str, branch: str, base_sha: str, commit_message: str,
        files: Mapping[str, GitHubBlobChange | None],
    ) -> str | None:
        try:
            reference = self._api(f"repos/{repository}/git/ref/heads/{branch}")
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                return None
            raise
        head = reference.get("object", {}).get("sha") if isinstance(reference, dict) else None
        if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise RuntimeError("Unexpected GitHub task reference response")
        if not self._commit_has_changes(
            repository=repository, commit_sha=head, parent_sha=base_sha,
            commit_message=commit_message, changes=pinned_changes(files),
        ):
            raise ValueError("Existing task branch differs from the approved publication")
        return head

    def _commit_has_changes(
        self, *, repository: str, commit_sha: str, parent_sha: str, commit_message: str,
        changes: Mapping[str, GitHubPinnedChange | None],
    ) -> bool:
        """The one identity rule for adopting a remote commit as ours, shared by ordinary publish,
        correction advance and reconciliation: sole parent, same message, and tree == parent tree plus
        exactly the pinned blob/mode changes. Read-only, so it never needs the task budget."""
        commit = self._api(f"repos/{repository}/git/commits/{commit_sha}")
        parent = self._api(f"repos/{repository}/git/commits/{parent_sha.lower()}")
        if not isinstance(commit, dict) or not isinstance(parent, dict):
            raise RuntimeError("Unexpected GitHub commit response")
        parents = commit.get("parents")
        if (not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], dict)
                or parents[0].get("sha") != parent_sha.lower()
                or str(commit.get("message", "")).strip() != commit_message.strip()):
            return False
        parent_tree, live_tree = _tree_reference(parent.get("tree")), _tree_reference(commit.get("tree"))
        try:
            expected = self._tree_entries(repository, parent_tree)
            live = self._tree_entries(repository, live_tree)
        except _TruncatedTreeListing:
            # Large repositories: walk only the subtrees whose SHAs differ, which is still an exact diff.
            return self._tree_diff_has_changes(repository, parent_tree, live_tree, changes)
        for path_name, change in changes.items():
            if change is None:
                expected.pop(path_name, None)
            else:
                expected[path_name] = (change.mode, change.blob_sha)
        return live == expected

    def _tree_diff_has_changes(
        self, repository: str, parent_tree: str, live_tree: str, changes: Mapping[str, GitHubPinnedChange | None],
    ) -> bool:
        diff = self._tree_diff(repository, parent_tree, live_tree, prefix="")
        for path_name, change in changes.items():
            wanted = None if change is None else (change.mode, change.blob_sha)
            if path_name in diff:
                if diff.pop(path_name) != wanted:
                    return False
            elif self._tree_entry_at(repository, parent_tree, path_name) != wanted:
                return False  # unchanged from the parent, so the parent must already hold the pinned value
        return not diff

    def _tree_diff(
        self, repository: str, parent_tree: str | None, live_tree: str | None, *, prefix: str,
    ) -> dict[str, tuple[str, str] | None]:
        if parent_tree == live_tree:
            return {}
        parent = self._tree_level(repository, parent_tree) if parent_tree else {}
        live = self._tree_level(repository, live_tree) if live_tree else {}
        diff: dict[str, tuple[str, str] | None] = {}
        for name in sorted(parent.keys() | live.keys()):
            old, new = parent.get(name), live.get(name)
            if old == new:
                continue
            path_name = prefix + name
            old_subtree = old[2] if old is not None and old[0] == "tree" else None
            new_subtree = new[2] if new is not None and new[0] == "tree" else None
            if old_subtree or new_subtree:
                diff |= self._tree_diff(repository, old_subtree, new_subtree, prefix=path_name + "/")
            if old is not None and old[0] != "tree":
                diff[path_name] = None
            if new is not None and new[0] != "tree":
                diff[path_name] = (new[1], new[2])
        return diff

    def _tree_entry_at(self, repository: str, tree_sha: str, path_name: str) -> tuple[str, str] | None:
        *directories, name = path_name.split("/")
        level = self._tree_level(repository, tree_sha)
        for directory in directories:
            entry = level.get(directory)
            if entry is None or entry[0] != "tree":
                return None
            level = self._tree_level(repository, entry[2])
        entry = level.get(name)
        return None if entry is None or entry[0] == "tree" else (entry[1], entry[2])

    def _tree_level(self, repository: str, tree_sha: str) -> dict[str, tuple[str, str, str]]:
        raw = self._api(f"repos/{repository}/git/trees/{tree_sha}")
        if not isinstance(raw, dict) or not isinstance(raw.get("tree"), list) or raw.get("truncated") is True:
            raise RuntimeError("GitHub tree listing is unreadable or truncated; cannot confirm the push")
        level: dict[str, tuple[str, str, str]] = {}
        for entry in raw["tree"]:
            if (not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) for key in ("path", "mode", "type", "sha"))
                    or "/" in entry["path"] or entry["path"] in level):
                raise RuntimeError("Unexpected GitHub tree entry")
            level[entry["path"]] = (entry["type"], entry["mode"], entry["sha"])
        return level

    def _tree_entries(self, repository: str, tree_sha: str) -> dict[str, tuple[str, str]]:
        raw = self._api(f"repos/{repository}/git/trees/{tree_sha}?recursive=1")
        if isinstance(raw, dict) and raw.get("truncated") is True:
            raise _TruncatedTreeListing("GitHub recursive tree listing is truncated")
        if not isinstance(raw, dict) or not isinstance(raw.get("tree"), list):
            raise RuntimeError("GitHub tree listing is unreadable; cannot confirm the push")
        entries: dict[str, tuple[str, str]] = {}
        for entry in raw["tree"]:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) for key in ("path", "mode", "type", "sha")):
                raise RuntimeError("Unexpected GitHub tree entry")
            if entry["type"] != "tree":
                entries[entry["path"]] = (entry["mode"], entry["sha"])
        return entries

    def _read_branch_head(self, *, repository: str, branch: str) -> str:
        reference = self._api(f"repos/{repository}/git/ref/heads/{branch}")
        head = reference.get("object", {}).get("sha") if isinstance(reference, dict) else None
        if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise RuntimeError("Unexpected GitHub task reference response")
        return head

    def get_issue(self, *, repository: str, issue_number: int) -> GitHubIssue:
        repo = self._resolve_repository(repository)
        raw = self._api(f"repos/{repo}/issues/{issue_number}")
        return _issue_from_api(raw, repository=repo)

    def list_issues(
        self,
        *,
        repository: str,
        state: str = "open",
        labels: tuple[str, ...] = (),
        limit: int = 30,
    ) -> tuple[GitHubIssue, ...]:
        repo = self._resolve_repository(repository)
        bound = max(1, min(limit, 100))
        query = f"repos/{repo}/issues?state={state}&per_page={bound}"
        if labels:
            query += "&labels=" + ",".join(labels)
        raw_items = self._api(query)
        if not isinstance(raw_items, list):
            raise RuntimeError("Unexpected GitHub issues list response")
        issues = [
            _issue_from_api(item, repository=repo)
            for item in raw_items
            if isinstance(item, dict) and "pull_request" not in item
        ]
        return tuple(issues[:bound])

    def create_issue(
        self,
        *,
        role: AgentRole,
        repository: str,
        title: str,
        body: str,
        labels: tuple[str, ...] = (),
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> GitHubIssue:
        assert_role_action_allowed(role, ActionClass.ISSUE_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        payload: dict[str, Any] = {"title": title.strip(), "body": body}
        cleaned_labels = [label.strip() for label in labels if label.strip()]
        if cleaned_labels:
            payload["labels"] = cleaned_labels
        raw = self._api(f"repos/{repo}/issues", method="POST", payload=payload)
        return _issue_from_api(raw, repository=repo)

    def update_issue(
        self,
        *,
        role: AgentRole,
        repository: str,
        issue_number: int,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: tuple[str, ...] | None = None,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> GitHubIssue:
        assert_role_action_allowed(role, ActionClass.ISSUE_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title.strip()
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if labels is not None:
            payload["labels"] = [label.strip() for label in labels if label.strip()]
        if not payload:
            return self.get_issue(repository=repo, issue_number=issue_number)
        raw = self._api(f"repos/{repo}/issues/{issue_number}", method="PATCH", payload=payload)
        return _issue_from_api(raw, repository=repo)

    def link_issues(
        self,
        *,
        role: AgentRole,
        repository: str,
        issue_number: int,
        related_issue_number: int,
        relationship: str = "blocks",
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.ISSUE_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        source = self.get_issue(repository=repo, issue_number=issue_number)
        related = self.get_issue(repository=repo, issue_number=related_issue_number)
        note = (
            f"\n\nRelated ({relationship.strip() or 'blocks'}): "
            f"#{related_issue_number} ({related.title})"
        )
        updated = self.update_issue(
            role=role,
            repository=repo,
            issue_number=issue_number,
            body=(source.body or "") + note,
            approved=approved,
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
        )
        return {
            "repository": repo,
            "issue_number": issue_number,
            "related_issue_number": related_issue_number,
            "relationship": relationship.strip() or "blocks",
            "issue": updated.to_dict(),
        }

    def get_pull_request(self, *, repository: str, pull_number: int) -> GitHubPullRequest:
        repo = self._resolve_repository(repository)
        raw = self._api(f"repos/{repo}/pulls/{pull_number}")
        files_raw = self._api(f"repos/{repo}/pulls/{pull_number}/files?per_page=100")
        changed_files: tuple[str, ...] = ()
        if isinstance(files_raw, list):
            changed_files = tuple(
                str(item.get("filename"))
                for item in files_raw
                if isinstance(item, dict) and item.get("filename")
            )
        return _pull_request_from_api(raw, repository=repo, changed_files=changed_files)

    def get_blob(self, *, repository: str, blob_sha: str) -> bytes:
        repo = self._resolve_repository(repository)
        _validate_blob_sha(blob_sha)
        raw = self._api(f"repos/{repo}/git/blobs/{blob_sha}")
        if (not isinstance(raw, dict) or raw.get("sha") != blob_sha or raw.get("encoding") != "base64" or
                type(raw.get("size")) is not int or not 0 <= raw["size"] <= 1048576):
            raise ValueError("GitHub blob identity/encoding/size is invalid or exceeds the review limit")
        content = _decode_blob(raw.get("content"), blob_sha)
        if len(content) != raw["size"]:
            raise ValueError("GitHub blob size differs from its content")
        return content

    def get_file_at_commit(self, *, repository: str, commit_sha: str, path: str) -> GitHubBlobChange | None:
        repo = self._resolve_repository(repository)
        parts = _validate_read_path(commit_sha, path)
        commit = self._api(f"repos/{repo}/git/commits/{commit_sha}")
        if not isinstance(commit, dict) or commit.get("sha") != commit_sha or not isinstance(commit.get("tree"), dict):
            raise ValueError("Review commit identity is invalid")
        tree_sha = commit["tree"].get("sha")
        for index, part in enumerate(parts):
            _validate_blob_sha(tree_sha)
            tree = self._api(f"repos/{repo}/git/trees/{tree_sha}")
            if (not isinstance(tree, dict) or tree.get("sha") != tree_sha or tree.get("truncated") is not False or
                    not isinstance(tree.get("tree"), list) or len(tree["tree"]) > 10000):
                raise ValueError("Review tree identity is invalid or incomplete")
            matches = [entry for entry in tree["tree"] if isinstance(entry, dict) and entry.get("path") == part]
            if not matches:
                return None
            if len(matches) != 1:
                raise ValueError("Review tree contains ambiguous paths")
            entry = matches[0]
            if index < len(parts) - 1:
                if entry.get("type") != "tree" or entry.get("mode") != "040000":
                    raise ValueError("Review source cannot traverse symlinks or non-tree entries")
                tree_sha = entry.get("sha")
            else:
                if entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
                    raise ValueError("Review source requires a regular file")
                blob_sha = entry.get("sha")
                if not isinstance(blob_sha, str):
                    raise ValueError("Review tree blob SHA is missing")
                _validate_blob_sha(blob_sha)
                return GitHubBlobChange(mode=entry["mode"], blob_sha=blob_sha,
                                        content=self.get_blob(repository=repo, blob_sha=blob_sha))
        raise ValueError("Review source path is empty")

    def submit_pr_review(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
        body: str,
        commit_id: str | None = None,
    ) -> GitHubPullRequestReview:
        assert_role_action_allowed(role, ActionClass.PR_REVIEW)
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise ValueError("event must be APPROVE, REQUEST_CHANGES, or COMMENT")
        if not body.strip():
            raise ValueError("review body must be non-empty")
        repo = self._resolve_repository(repository)
        payload: dict[str, Any] = {"event": event, "body": body.strip()}
        cleaned_commit: str | None = None
        if commit_id is not None:
            cleaned_commit = commit_id.strip().lower()
            if re.fullmatch(r"[0-9a-f]{40}", cleaned_commit) is None:
                raise ValueError("commit_id must be a 40-char lowercase hex SHA")
            payload["commit_id"] = cleaned_commit
        raw = self._api(
            f"repos/{repo}/pulls/{pull_number}/reviews",
            method="POST",
            payload=payload,
        )
        return GitHubPullRequestReview(
            pull_number=pull_number,
            event=event,
            body=body.strip(),
            review_id=str(raw.get("id")) if isinstance(raw, dict) and raw.get("id") is not None else None,
            html_url=str(raw.get("html_url")) if isinstance(raw, dict) and raw.get("html_url") else None,
            commit_id=cleaned_commit,
        )


    def upsert_branch_commit(
        self,
        *,
        role: AgentRole,
        repository: str,
        branch: str,
        base_sha: str,
        commit_message: str,
        files: Mapping[str, GitHubBlobChange | None],
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        before_write: Callable[[], object] | None = None,
    ) -> str:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        _validate_publish_commit_inputs(
            branch=branch, base_sha=base_sha, commit_message=commit_message, files=files
        )
        existing_head = self._matching_publish_head(
            repository=repo, branch=branch.strip(), base_sha=base_sha,
            commit_message=commit_message, files=files,
        )
        if existing_head is not None:
            return existing_head
        tree_sha = self._create_publish_tree(repo, base_sha, files, before_write)
        commit_sha = self._create_publish_commit(repo, base_sha, tree_sha, commit_message, before_write)
        head_sha = commit_sha
        _check_write_budget(before_write)
        try:
            self._api(
                f"repos/{repo}/git/refs",
                method="POST",
                payload={"ref": f"refs/heads/{branch.strip()}", "sha": head_sha},
            )
        except RuntimeError:
            existing_head = self._matching_publish_head(
                repository=repo, branch=branch.strip(), base_sha=base_sha,
                commit_message=commit_message, files=files,
            )
            if existing_head is None:
                raise
            return existing_head
        return head_sha

    def _create_publish_tree(
        self, repo: str, base_sha: str, files: Mapping[str, GitHubBlobChange | None],
        before_write: Callable[[], object] | None = None,
    ) -> str:
        base = self._api(f"repos/{repo}/git/commits/{base_sha.lower()}")
        if not isinstance(base, dict) or not isinstance(base.get("tree"), dict):
            raise RuntimeError("Unexpected GitHub base commit response")
        base_tree = base["tree"].get("sha")
        if not isinstance(base_tree, str) or not base_tree:
            raise RuntimeError("Base commit is missing a tree SHA")
        tree_entries: list[dict[str, Any]] = []
        for path_name in sorted(files):
            change = files[path_name]
            if change is None:
                tree_entries.append({"path": path_name, "mode": "100644", "type": "blob", "sha": None})
                continue
            _check_write_budget(before_write)
            blob = self._api(
                f"repos/{repo}/git/blobs",
                method="POST",
                payload={
                    "content": base64.b64encode(change.content).decode("ascii"),
                    "encoding": "base64",
                },
            )
            if not isinstance(blob, dict) or not isinstance(blob.get("sha"), str):
                raise RuntimeError(f"Failed to create blob for {path_name}")
            if blob["sha"] != change.blob_sha:
                raise RuntimeError(
                    f"GitHub blob SHA mismatch for {path_name}: "
                    f"expected {change.blob_sha}, got {blob['sha']}"
                )
            tree_entries.append(
                {"path": path_name, "mode": change.mode, "type": "blob", "sha": blob["sha"]}
            )
        _check_write_budget(before_write)
        tree = self._api(
            f"repos/{repo}/git/trees",
            method="POST",
            payload={"base_tree": base_tree, "tree": tree_entries},
        )
        if not isinstance(tree, dict) or not isinstance(tree.get("sha"), str):
            raise RuntimeError("Failed to create GitHub tree")
        return str(tree["sha"])

    def _create_publish_commit(
        self, repo: str, base_sha: str, tree_sha: str, commit_message: str,
        before_write: Callable[[], object] | None = None,
    ) -> str:
        _check_write_budget(before_write)
        commit = self._api(
            f"repos/{repo}/git/commits",
            method="POST",
            payload={
                "message": commit_message.strip(),
                "tree": tree_sha,
                "parents": [base_sha.lower()],
            },
        )
        if not isinstance(commit, dict) or not isinstance(commit.get("sha"), str):
            raise RuntimeError("Failed to create GitHub commit")
        if not isinstance(commit.get("tree"), dict) or commit["tree"].get("sha") != tree_sha:
            raise RuntimeError("Published commit tree SHA does not match the uploaded tree")
        return str(commit["sha"])

    def advance_draft_pull_request_head(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        head_branch: str,
        base_ref: str,
        expected_head_sha: str,
        commit_message: str,
        files: Mapping[str, GitHubBlobChange | None],
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        before_write: Callable[[], object] | None = None,
    ) -> GitHubPullRequest:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        branch, base, expected = _validate_advance_inputs(
            pull_number=pull_number, head_branch=head_branch, base_ref=base_ref,
            expected_head_sha=expected_head_sha, commit_message=commit_message, files=files,
        )
        current = self._read_advance_target(repo, pull_number=pull_number, head_branch=branch, base_ref=base)

        def is_ours(remote_head: str) -> bool:
            return self._commit_has_changes(repository=repo, commit_sha=remote_head, parent_sha=expected,
                                            commit_message=commit_message, changes=pinned_changes(files))

        def ours_or_refuse(remote_head: str, target: GitHubPullRequest) -> GitHubPullRequest:
            if not is_ours(remote_head):
                raise ValueError("Pull request head moved from the pinned SHA; refusing stale correction")
            return replace(target, head_sha=remote_head)

        # A retry after our push landed is decided read-only, before any upload.
        remote_head = self._read_branch_head(repository=repo, branch=branch)
        if remote_head != expected:
            return ours_or_refuse(remote_head, current)
        tree_sha = self._create_publish_tree(repo, expected, files, before_write)
        commit_sha = self._create_publish_commit(repo, expected, tree_sha, commit_message, before_write)
        # Re-read the PR target and branch head right before the ref update: a competing update during
        # object creation must be refused here, not left to a stale PATCH.
        current = self._read_advance_target(repo, pull_number=pull_number, head_branch=branch, base_ref=base)
        remote_head = self._read_branch_head(repository=repo, branch=branch)
        if remote_head != expected:
            return ours_or_refuse(remote_head, current)
        _check_write_budget(before_write)
        try:
            self._api(
                f"repos/{repo}/git/refs/heads/{branch}",
                method="PATCH",
                payload={"sha": commit_sha, "force": False},
            )
        except RuntimeError:
            remote_head = self._read_branch_head(repository=repo, branch=branch)
            if remote_head != commit_sha and not is_ours(remote_head):
                raise
            commit_sha = remote_head
        advanced = self._read_advance_target(repo, pull_number=pull_number, head_branch=branch, base_ref=base)
        live_head = self._read_branch_head(repository=repo, branch=branch)
        if live_head != commit_sha:
            # Never report our SHA as current when the live ref says otherwise; settlement decides.
            raise RuntimeError("Branch head moved after the correction push; refusing to report success")
        return replace(advanced, head_sha=live_head)

    def reconcile_advanced_head(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        head_branch: str,
        base_ref: str,
        expected_head_sha: str,
        commit_message: str,
        changes: Mapping[str, GitHubPinnedChange | None],
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> tuple[AdvanceOutcome, GitHubPullRequest]:
        """Classify the live branch head after an uncertain push: untouched parent, our exact commit, or moved.

        Read-only: the live commit counts as ours only when its sole parent is the pinned head, its
        message matches, and its tree equals the parent tree with exactly the pinned blob/mode changes.
        No GitHub writes happen here, so settlement never depends on the task budget.
        """
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        branch, _, expected = _validate_reconcile_inputs(
            pull_number=pull_number, head_branch=head_branch, base_ref=base_ref,
            expected_head_sha=expected_head_sha, commit_message=commit_message, changes=changes,
        )
        pull = _pull_request_from_api(self._api(f"repos/{repo}/pulls/{pull_number}"), repository=repo)
        remote_head = self._read_branch_head(repository=repo, branch=branch)
        if remote_head == expected:
            return "parent", replace(pull, head_sha=remote_head)
        ours = self._commit_has_changes(repository=repo, commit_sha=remote_head, parent_sha=expected,
                                        commit_message=commit_message, changes=changes)
        return ("ours" if ours else "moved"), replace(pull, head_sha=remote_head)

    def _read_advance_target(
        self, repo: str, *, pull_number: int, head_branch: str, base_ref: str,
    ) -> GitHubPullRequest:
        raw = self._api(f"repos/{repo}/pulls/{pull_number}")
        pull = _pull_request_from_api(raw, repository=repo)
        head_repo = raw.get("head", {}).get("repo") if isinstance(raw, dict) and isinstance(raw.get("head"), dict) else None
        if not isinstance(head_repo, dict) or str(head_repo.get("full_name", "")).lower() != repo.lower():
            raise ValueError("Correction target pull request head must live in the base repository")
        _validate_advance_target(pull, pull_number=pull_number, head_branch=head_branch, base_ref=base_ref)
        return pull

    def create_or_update_draft_pull_request(
        self,
        *,
        role: AgentRole,
        repository: str,
        title: str,
        body: str,
        head_branch: str,
        base_ref: str,
        issue_number: int,
        existing_pull_number: int | None = None,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        before_write: Callable[[], object] | None = None,
    ) -> GitHubPullRequest:
        assert_role_action_allowed(role, ActionClass.REPO_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        repo = self._resolve_repository(repository)
        cleaned_title = title.strip()
        cleaned_body = body.strip()
        cleaned_head = _validate_aitobuild_branch_name(head_branch, field_name="head_branch")
        cleaned_base = base_ref.strip()
        if not cleaned_title or not cleaned_body or not cleaned_base:
            raise ValueError("draft PR title, body, head_branch, and base_ref must be non-empty")
        _validate_base_ref_name(cleaned_base, head_branch=cleaned_head)
        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if existing_pull_number is not None and (
            type(existing_pull_number) is not int or existing_pull_number <= 0
        ):
            raise ValueError("existing_pull_number must be a positive integer when provided")
        if existing_pull_number is None:
            query = urlencode({"state": "all", "head": f"{repo.split('/')[0]}:{cleaned_head}",
                               "base": cleaned_base, "per_page": 100})
            raw_matches = self._api(f"repos/{repo}/pulls?{query}")
            if not isinstance(raw_matches, list):
                raise RuntimeError("Unexpected GitHub pull request list response")
            matches = [raw for raw in raw_matches if isinstance(raw, dict)
                       and raw.get("head", {}).get("ref") == cleaned_head
                       and raw.get("head", {}).get("repo", {}).get("full_name", "").lower() == repo
                       and raw.get("base", {}).get("ref") == cleaned_base]
            open_matches = [raw for raw in matches if raw.get("state") == "open"]
            if matches and len(open_matches) != 1:
                raise ValueError("Existing task pull request must be one open draft")
            if open_matches:
                existing_pull_number = int(open_matches[0]["number"])
        if existing_pull_number is not None:
            current = self.get_pull_request(repository=repo, pull_number=existing_pull_number)
            if current.head_ref != cleaned_head:
                raise ValueError(
                    f"Pull request #{existing_pull_number} head ref does not match the delivery branch"
                )
            if not current.draft or current.state != "open":
                raise ValueError(
                    f"Pull request #{existing_pull_number} must remain an open draft; refusing update"
                )
            _check_write_budget(before_write)
            raw = self._api(
                f"repos/{repo}/pulls/{existing_pull_number}",
                method="PATCH",
                payload={
                    "title": cleaned_title,
                    "body": cleaned_body,
                    "base": cleaned_base,
                },
            )
            return _pull_request_from_api(raw, repository=repo, changed_files=())
        _check_write_budget(before_write)
        raw = self._api(
            f"repos/{repo}/pulls",
            method="POST",
            payload={
                "title": cleaned_title,
                "body": cleaned_body,
                "head": cleaned_head,
                "base": cleaned_base,
                "draft": True,
            },
        )
        return _pull_request_from_api(raw, repository=repo, changed_files=())

    def create_issue_proposal(
        self,
        *,
        role: AgentRole,
        proposal: GitHubIssueProposal,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> None:
        assert_role_action_allowed(role, ActionClass.ISSUE_WRITE)
        assert_repo_write_approval(
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
            approved=approved,
        )
        # Keep parity with MockGitHubAdapter: record the proposal only.
        # Callers that need a live issue must invoke create_issue explicitly.
        self.proposals.append(proposal)



def _validate_read_path(commit_sha: str, path: str) -> tuple[str, ...]:
    _validate_blob_sha(commit_sha)
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise ValueError("Review source requires a canonical repository path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or str(parsed) != path or ".." in parsed.parts or not 1 <= len(parsed.parts) <= 32:
        raise ValueError("Review source path is outside the repository or exceeds depth bounds")
    return parsed.parts


def _validate_blob_sha(blob_sha: str) -> None:
    if not isinstance(blob_sha, str) or re.fullmatch(r"[0-9a-f]{40}", blob_sha) is None:
        raise ValueError("Read operations require an exact Git SHA")


def _decode_blob(encoded: Any, blob_sha: str) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > 1500000:
        raise ValueError("GitHub blob exceeds the review limit or lacks base64 content")
    content = base64.b64decode("".join(encoded.split()), validate=True)
    if len(content) > 1048576 or _git_blob_sha(content) != blob_sha:
        raise ValueError("GitHub blob hash/size differs from the published snapshot")
    return content


def _git_blob_sha(content: bytes) -> str:
    return sha1(b"blob %d\x00" % len(content) + content).hexdigest()


def _tree_fingerprint(files: Mapping[str, GitHubBlobChange | None]) -> str:
    entries = []
    for path_name in sorted(files):
        change = files[path_name]
        if change is None:
            entries.append({"path": path_name, "deleted": True})
        else:
            entries.append({"path": path_name, "mode": change.mode, "blob_sha": change.blob_sha})
    return sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def _validate_ref_name(name: Any, *, field_name: str) -> str:
    """Shared git ref-name check for task heads and PR bases (no refs/ prefix)."""
    cleaned = name.strip() if isinstance(name, str) else ""
    if (
        not cleaned
        or re.fullmatch(r"[A-Za-z0-9._/-]+", cleaned) is None
        or cleaned.startswith(("/", "-", ".", "refs/"))
        or cleaned.endswith(("/", ".", ".lock"))
        or ".." in cleaned
        or "//" in cleaned
        or "@{" in cleaned
        or any(part.startswith(".") or part.endswith(".lock") for part in cleaned.split("/"))
    ):
        raise ValueError(f"{field_name} must be a plain branch name")
    return cleaned


def _validate_aitobuild_branch_name(branch: str, *, field_name: str = "branch") -> str:
    try:
        cleaned_branch = _validate_ref_name(branch, field_name=field_name)
    except ValueError:
        cleaned_branch = ""
    if not cleaned_branch.startswith("aitobuild/") or cleaned_branch == "aitobuild/":
        raise ValueError(f"{field_name} must be an aitobuild/ task ref name")
    return cleaned_branch


def _validate_base_ref_name(base_ref: str, *, head_branch: str) -> str:
    cleaned = _validate_ref_name(base_ref, field_name="base_ref")
    if cleaned == head_branch:
        raise ValueError("base_ref must differ from head_branch")
    return cleaned


def _check_write_budget(before_write: Callable[[], object] | None) -> None:
    """Run the caller's budget check (raises on expiry/abort) right before a GitHub write."""
    if before_write is not None:
        before_write()


def _validate_advance_inputs(
    *,
    pull_number: int,
    head_branch: str,
    base_ref: str,
    expected_head_sha: str,
    commit_message: str,
    files: Mapping[str, GitHubBlobChange | None],
) -> tuple[str, str, str]:
    if type(pull_number) is not int or pull_number <= 0:
        raise ValueError("pull_number must be a positive integer")
    branch = _validate_aitobuild_branch_name(head_branch, field_name="head_branch")
    base = _validate_base_ref_name(base_ref, head_branch=branch)
    _validate_publish_commit_inputs(
        branch=branch, base_sha=expected_head_sha, commit_message=commit_message, files=files,
    )
    return branch, base, expected_head_sha


class _TruncatedTreeListing(RuntimeError):
    """GitHub capped a recursive tree listing; callers fall back to an exact level-by-level diff."""


def _tree_reference(tree: object) -> str:
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str) or re.fullmatch(r"[0-9a-f]{40}", tree_sha) is None:
        raise RuntimeError("Unexpected GitHub tree reference")
    return tree_sha


def _validate_pinned_changes(changes: Mapping[str, GitHubPinnedChange | None]) -> None:
    if not isinstance(changes, Mapping) or not changes:
        raise ValueError("reconcile requires a non-empty pinned change map")
    for path_name, change in changes.items():
        if (not isinstance(path_name, str) or not path_name.strip() or path_name.startswith("/")
                or any(part == ".." for part in path_name.split("/"))):
            raise ValueError("publish file paths must be relative and scoped")
        if change is None:
            continue
        if not isinstance(change, GitHubPinnedChange) or change.mode not in {"100644", "100755"}:
            raise ValueError("pinned changes must be GitHubPinnedChange values with mode 100644 or 100755")
        if not isinstance(change.blob_sha, str) or re.fullmatch(r"[0-9a-f]{40}", change.blob_sha) is None:
            raise ValueError(f"pinned blob SHA for {path_name} must be a lowercase git SHA")


def _validate_reconcile_inputs(
    *, pull_number: int, head_branch: str, base_ref: str, expected_head_sha: str, commit_message: str,
    changes: Mapping[str, GitHubPinnedChange | None],
) -> tuple[str, str, str]:
    if type(pull_number) is not int or pull_number <= 0:
        raise ValueError("pull_number must be a positive integer")
    branch = _validate_aitobuild_branch_name(head_branch, field_name="head_branch")
    base = _validate_base_ref_name(base_ref, head_branch=branch)
    if not isinstance(expected_head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", expected_head_sha) is None:
        raise ValueError("base_sha must be a resolved lowercase commit SHA")
    if not isinstance(commit_message, str) or not commit_message.strip():
        raise ValueError("commit_message must be non-empty")
    _validate_pinned_changes(changes)
    return branch, base, expected_head_sha


def _validate_advance_target(
    pull: GitHubPullRequest, *, pull_number: int, head_branch: str, base_ref: str,
) -> None:
    if pull.number != pull_number:
        raise ValueError("Correction target pull request identity changed")
    if pull.state != "open" or not pull.draft:
        raise ValueError(f"Pull request #{pull_number} must remain an open draft; refusing correction")
    if pull.head_ref != head_branch:
        raise ValueError(f"Pull request #{pull_number} head ref differs from the pinned correction branch")
    if pull.base_ref != base_ref:
        raise ValueError(f"Pull request #{pull_number} base differs from the pinned correction base")


def _validate_publish_commit_inputs(
    *,
    branch: str,
    base_sha: str,
    commit_message: str,
    files: Mapping[str, GitHubBlobChange | None],
) -> None:
    _validate_aitobuild_branch_name(branch)
    if not isinstance(base_sha, str) or re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
        raise ValueError("base_sha must be a resolved lowercase commit SHA")
    if not isinstance(commit_message, str) or not commit_message.strip():
        raise ValueError("commit_message must be non-empty")
    if not isinstance(files, Mapping) or not files:
        raise ValueError("publish commit requires a non-empty scoped file map")
    for path_name, change in files.items():
        if (
            not isinstance(path_name, str)
            or not path_name.strip()
            or path_name.startswith("/")
            or any(part == ".." for part in path_name.split("/"))
        ):
            raise ValueError("publish file paths must be relative and scoped")
        if change is None:
            continue
        if not isinstance(change, GitHubBlobChange):
            raise ValueError("publish file changes must be GitHubBlobChange values")
        if change.mode not in {"100644", "100755"}:
            raise ValueError("publish file mode must be 100644 or 100755")
        if not isinstance(change.content, (bytes, bytearray)):
            raise ValueError("publish file contents must be raw bytes")
        if change.blob_sha != _git_blob_sha(bytes(change.content)):
            raise ValueError(f"publish blob SHA mismatch for {path_name}")



def build_github_adapter(
    *,
    mode: str = "mock",
    default_repository: str | None = None,
    allowed_repositories: tuple[str, ...] | None = None,
) -> MockGitHubAdapter | GhCliGitHubAdapter:
    normalized = mode.strip().lower()
    allowed = tuple(allowed_repositories or ())
    if normalized in {"", "mock"}:
        return MockGitHubAdapter(
            allowed_repositories=frozenset(normalize_repository_name(item) for item in allowed)
            if allowed
            else None,
            enforce_allowlist=bool(allowed),
        )
    if normalized in {"gh", "gh_cli", "cli"}:
        return GhCliGitHubAdapter(
            default_repository=default_repository,
            allowed_repositories=allowed,
            enforce_allowlist=True,
        )
    raise ValueError("AITOBUILD_GITHUB_ADAPTER must be mock or gh_cli")


def _issue_from_api(raw: Any, *, repository: str) -> GitHubIssue:
    if not isinstance(raw, dict):
        raise RuntimeError("Unexpected GitHub issue response")
    labels_raw = raw.get("labels") or []
    labels: list[str] = []
    if isinstance(labels_raw, list):
        for item in labels_raw:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                labels.append(item["name"])
            elif isinstance(item, str):
                labels.append(item)
    return GitHubIssue(
        number=int(raw["number"]),
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or ""),
        state=str(raw.get("state") or "open"),
        labels=tuple(labels),
        html_url=str(raw["html_url"]) if raw.get("html_url") else None,
        repository=repository,
    )


def _pull_request_from_api(
    raw: Any,
    *,
    repository: str,
    changed_files: tuple[str, ...] = (),
) -> GitHubPullRequest:
    if not isinstance(raw, dict):
        raise RuntimeError("Unexpected GitHub pull request response")
    head_raw = raw.get("head")
    base_raw = raw.get("base")
    head: dict[str, Any] = head_raw if isinstance(head_raw, dict) else {}
    base: dict[str, Any] = base_raw if isinstance(base_raw, dict) else {}
    head_sha_raw = head.get("sha")
    head_sha = str(head_sha_raw).lower() if isinstance(head_sha_raw, str) and head_sha_raw else None
    return GitHubPullRequest(
        number=int(raw["number"]),
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or ""),
        state=str(raw.get("state") or "open"),
        head_ref=str(head.get("ref") or ""),
        base_ref=str(base.get("ref") or ""),
        draft=bool(raw.get("draft", False)),
        html_url=str(raw["html_url"]) if raw.get("html_url") else None,
        repository=repository,
        changed_files=changed_files,
        head_sha=head_sha,
    )
