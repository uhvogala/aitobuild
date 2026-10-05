"""GitHub adapters: mock-first interface with optional gh CLI backend."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha1, sha256
import json
import re
from subprocess import CalledProcessError, TimeoutExpired, run
from typing import Any, Literal, Protocol
from uuid import uuid4

from aitobuild.policy import (
    ActionClass,
    AgentRole,
    assert_repo_write_approval,
    assert_role_action_allowed,
)


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
    ) -> GitHubPullRequest: ...


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
        encoded_files = {
            path: None if change is None else change.to_dict()
            for path, change in sorted(files.items())
        }
        payload = {
            "repository": repo,
            "branch": branch.strip(),
            "base_sha": base_sha.lower(),
            "commit_message": commit_message.strip(),
            "files": encoded_files,
            "tree_fingerprint": _tree_fingerprint(files),
        }
        head_sha = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40]
        self.branch_commits.append({**payload, "head_sha": head_sha})
        self._branch_heads.setdefault(repo, {})[branch.strip()] = head_sha
        return head_sha

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
        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if existing_pull_number is not None and (
            type(existing_pull_number) is not int or existing_pull_number <= 0
        ):
            raise ValueError("existing_pull_number must be a positive integer when provided")
        repo_prs = self.pull_requests.setdefault(repo, {})
        if existing_pull_number is None:
            for candidate in repo_prs.values():
                if (
                    candidate.head_ref == cleaned_head
                    and candidate.draft
                    and candidate.state == "open"
                ):
                    existing_pull_number = candidate.number
                    break
        if existing_pull_number is not None:
            current = repo_prs.get(existing_pull_number)
            if current is None:
                raise ValueError(f"Pull request #{existing_pull_number} was not found")
            if current.head_ref != cleaned_head:
                raise ValueError(
                    f"Pull request #{existing_pull_number} head ref does not match the delivery branch"
                )
            if not current.draft:
                raise ValueError(
                    f"Pull request #{existing_pull_number} is not a draft; refusing update"
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
        tree = self._api(
            f"repos/{repo}/git/trees",
            method="POST",
            payload={"base_tree": base_tree, "tree": tree_entries},
        )
        if not isinstance(tree, dict) or not isinstance(tree.get("sha"), str):
            raise RuntimeError("Failed to create GitHub tree")
        commit = self._api(
            f"repos/{repo}/git/commits",
            method="POST",
            payload={
                "message": commit_message.strip(),
                "tree": tree["sha"],
                "parents": [base_sha.lower()],
            },
        )
        if not isinstance(commit, dict) or not isinstance(commit.get("sha"), str):
            raise RuntimeError("Failed to create GitHub commit")
        if not isinstance(commit.get("tree"), dict) or commit["tree"].get("sha") != tree["sha"]:
            raise RuntimeError("Published commit tree SHA does not match the uploaded tree")
        head_sha = commit["sha"]
        try:
            self._api(
                f"repos/{repo}/git/refs",
                method="POST",
                payload={"ref": f"refs/heads/{branch.strip()}", "sha": head_sha},
            )
        except RuntimeError:
            self._api(
                f"repos/{repo}/git/refs/heads/{branch.strip()}",
                method="PATCH",
                payload={"sha": head_sha, "force": False},
            )
        return head_sha

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
        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if existing_pull_number is not None and (
            type(existing_pull_number) is not int or existing_pull_number <= 0
        ):
            raise ValueError("existing_pull_number must be a positive integer when provided")
        if existing_pull_number is None:
            owner = repo.split("/", 1)[0]
            listed = self._api(
                f"repos/{repo}/pulls?state=open&head={owner}:{cleaned_head}&per_page=10"
            )
            if isinstance(listed, list):
                for item in listed:
                    if not isinstance(item, dict):
                        continue
                    if item.get("draft") is not True:
                        continue
                    number = item.get("number")
                    if type(number) is int and number > 0:
                        existing_pull_number = number
                        break
        if existing_pull_number is not None:
            current = self.get_pull_request(repository=repo, pull_number=existing_pull_number)
            if current.head_ref != cleaned_head:
                raise ValueError(
                    f"Pull request #{existing_pull_number} head ref does not match the delivery branch"
                )
            if not current.draft:
                raise ValueError(
                    f"Pull request #{existing_pull_number} is not a draft; refusing update"
                )
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


def _validate_aitobuild_branch_name(branch: str, *, field_name: str = "branch") -> str:
    cleaned_branch = branch.strip() if isinstance(branch, str) else ""
    if (
        not cleaned_branch
        or not cleaned_branch.startswith("aitobuild/")
        or cleaned_branch.endswith("/")
        or ".." in cleaned_branch
        or re.fullmatch(r"[A-Za-z0-9._/-]+", cleaned_branch) is None
    ):
        raise ValueError(f"{field_name} must be an aitobuild/ task ref name")
    return cleaned_branch


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
