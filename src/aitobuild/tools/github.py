"""GitHub adapters: mock-first interface with optional gh CLI backend."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from subprocess import CalledProcessError, run
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
        }


@dataclass(slots=True, frozen=True)
class GitHubPullRequestReview:
    pull_number: int
    event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
    body: str
    review_id: str | None = None
    html_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pull_number": self.pull_number,
            "event": self.event,
            "body": self.body,
            "review_id": self.review_id,
            "html_url": self.html_url,
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
    ) -> GitHubPullRequestReview: ...

    def create_issue_proposal(
        self,
        *,
        role: AgentRole,
        proposal: GitHubIssueProposal,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
    ) -> None: ...


@dataclass
class MockGitHubAdapter:
    """In-memory GitHub adapter used by tests and local mock mode."""

    proposals: list[GitHubIssueProposal] = field(default_factory=list)
    issues: dict[str, dict[int, GitHubIssue]] = field(default_factory=dict)
    pull_requests: dict[str, dict[int, GitHubPullRequest]] = field(default_factory=dict)
    reviews: list[GitHubPullRequestReview] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    allowed_repositories: frozenset[str] | None = None
    enforce_allowlist: bool = False
    _next_issue: int = 1
    _next_pr: int = 1

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
            return self.pull_requests[repository][pull_number]
        except KeyError as exc:
            raise LookupError(f"Pull request #{pull_number} not found in {repository}") from exc

    def submit_pr_review(
        self,
        *,
        role: AgentRole,
        repository: str,
        pull_number: int,
        event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
        body: str,
    ) -> GitHubPullRequestReview:
        assert_role_action_allowed(role, ActionClass.PR_REVIEW)
        repository = self._resolve_repository(repository)
        self.get_pull_request(repository=repository, pull_number=pull_number)
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise ValueError("event must be APPROVE, REQUEST_CHANGES, or COMMENT")
        if not body.strip():
            raise ValueError("review body must be non-empty")
        review = GitHubPullRequestReview(
            pull_number=pull_number,
            event=event,
            body=body.strip(),
            review_id=f"mock-review-{uuid4().hex[:8]}",
            html_url=f"https://github.com/{repository}/pull/{pull_number}#pullrequestreview",
        )
        self.reviews.append(review)
        return review

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
            )
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
    ) -> GitHubPullRequestReview:
        assert_role_action_allowed(role, ActionClass.PR_REVIEW)
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise ValueError("event must be APPROVE, REQUEST_CHANGES, or COMMENT")
        if not body.strip():
            raise ValueError("review body must be non-empty")
        repo = self._resolve_repository(repository)
        raw = self._api(
            f"repos/{repo}/pulls/{pull_number}/reviews",
            method="POST",
            payload={"event": event, "body": body.strip()},
        )
        return GitHubPullRequestReview(
            pull_number=pull_number,
            event=event,
            body=body.strip(),
            review_id=str(raw.get("id")) if isinstance(raw, dict) and raw.get("id") is not None else None,
            html_url=str(raw.get("html_url")) if isinstance(raw, dict) and raw.get("html_url") else None,
        )

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
    )
