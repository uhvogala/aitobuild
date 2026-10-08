from __future__ import annotations

import base64
from hashlib import sha256
import json
from dataclasses import replace
from typing import Any

import pytest

from aitobuild.policy import AgentRole
from aitobuild.tools.github import (
    GhCliGitHubAdapter,
    GitHubBlobChange,
    MockGitHubAdapter,
    _git_blob_sha,
)

REPO = "fixture/widgets"
BRANCH = "aitobuild/issue-7-task"


def _change(text: str) -> GitHubBlobChange:
    content = text.encode()
    return GitHubBlobChange(mode="100644", content=content, blob_sha=_git_blob_sha(content))


def _advance(adapter: Any, *, expected: str, files: dict | None = None, **overrides: Any):
    arguments: dict[str, Any] = dict(
        role=AgentRole.DEVELOPER, repository=REPO, pull_number=overrides.pop("pull_number", 1),
        head_branch=BRANCH, base_ref="main", expected_head_sha=expected,
        commit_message="aitobuild: correct #7", files=files or {"src/probe.py": _change("fixed = True\n")},
        approved=True, require_human_approval_for_repo_writes=True,
    )
    arguments.update(overrides)
    return adapter.advance_draft_pull_request_head(**arguments)


def _published_mock() -> tuple[MockGitHubAdapter, str]:
    adapter = MockGitHubAdapter(allowed_repositories=frozenset({REPO}), enforce_allowlist=True)
    head = adapter.upsert_branch_commit(
        role=AgentRole.DEVELOPER, repository=REPO, branch=BRANCH, base_sha="a" * 40,
        commit_message="aitobuild: implement #7", files={"src/probe.py": _change("x = 1\n")},
        approved=True, require_human_approval_for_repo_writes=True,
    )
    adapter.create_or_update_draft_pull_request(
        role=AgentRole.DEVELOPER, repository=REPO, title="Trial", body="Closes #7", head_branch=BRANCH,
        base_ref="main", issue_number=7, approved=True, require_human_approval_for_repo_writes=True,
    )
    return adapter, head


def test_mock_advance_moves_existing_pr_head_and_is_idempotent() -> None:
    adapter, head = _published_mock()
    advanced = _advance(adapter, expected=head)
    assert advanced.number == 1 and advanced.head_sha not in {None, head}
    assert adapter.branch_commits[-1]["base_sha"] == head
    assert adapter.get_pull_request(repository=REPO, pull_number=1).head_sha == advanced.head_sha
    assert list(adapter.pull_requests[REPO]) == [1]
    commits = len(adapter.branch_commits)
    assert _advance(adapter, expected=head) == advanced
    assert len(adapter.branch_commits) == commits


def test_mock_advance_refuses_stale_head_and_changed_target() -> None:
    adapter, head = _published_mock()
    _advance(adapter, expected=head, files={"src/probe.py": _change("other = 1\n")})
    with pytest.raises(ValueError, match="moved from the pinned SHA"):
        _advance(adapter, expected=head)
    adapter, head = _published_mock()
    with pytest.raises(ValueError, match="base differs"):
        _advance(adapter, expected=head, base_ref="develop")
    with pytest.raises(LookupError):
        _advance(adapter, expected=head, pull_number=2)
    adapter.pull_requests[REPO][1] = replace(adapter.pull_requests[REPO][1], draft=False)
    with pytest.raises(ValueError, match="open draft"):
        _advance(adapter, expected=head)
    adapter.pull_requests[REPO][1] = replace(adapter.pull_requests[REPO][1], draft=True, state="closed")
    with pytest.raises(ValueError, match="open draft"):
        _advance(adapter, expected=head)
    assert len(adapter.branch_commits) == 1


@pytest.mark.parametrize(("field", "value", "message"), [
    ("head_branch", "aitobuild/x//y", "aitobuild/"),
    ("head_branch", "aitobuild/x.lock", "aitobuild/"),
    ("head_branch", "main", "aitobuild/"),
    ("base_ref", "main.lock", "plain branch"),
    ("base_ref", BRANCH, "differ"),
    ("expected_head_sha", "abc", "base_sha"),
    ("pull_number", 0, "pull_number"),
])
@pytest.mark.parametrize("adapter_kind", ["mock", "gh_cli"])
def test_advance_refuses_bad_inputs_before_any_call(adapter_kind, field, value, message, monkeypatch) -> None:
    if adapter_kind == "mock":
        adapter, head = _published_mock()
    else:
        adapter, head = GhCliGitHubAdapter(allowed_repositories=(REPO,)), "b" * 40

        def api(*_args, **_kwargs):
            raise AssertionError("Invalid inputs must be refused before any GitHub call")

        monkeypatch.setattr(adapter, "_api", api)
    with pytest.raises(ValueError, match=message):
        _advance(adapter, expected=head, **{field: value})


class FakeGitHub:
    def __init__(self, *, head: str, draft: bool = True, state: str = "open", fork: bool = False) -> None:
        self.ref = head
        self.commits: dict[str, dict[str, Any]] = {head: {"tree": {"sha": "0" * 40}, "parents": [{"sha": "a" * 40}],
                                                         "message": "aitobuild: implement #7"}}
        self.draft, self.state, self.fork = draft, state, fork
        self.calls: list[tuple[str, str]] = []
        self.patch_mode = "apply"

    def pull(self) -> dict[str, Any]:
        return {"number": 1, "title": "Trial", "body": "Closes #7", "state": self.state, "draft": self.draft,
                "head": {"ref": BRANCH, "sha": self.ref,
                         "repo": {"full_name": "someone/widgets" if self.fork else REPO}},
                "base": {"ref": "main"}}

    def __call__(self, endpoint: str, *, method: str = "GET", payload: dict | None = None) -> Any:
        self.calls.append((method, endpoint))
        payload = payload or {}
        if endpoint == f"repos/{REPO}/pulls/1" and method == "GET":
            return self.pull()
        if endpoint.startswith(f"repos/{REPO}/git/commits/") and method == "GET":
            return {"sha": endpoint.rsplit("/", 1)[1], **self.commits[endpoint.rsplit("/", 1)[1]]}
        if endpoint == f"repos/{REPO}/git/blobs" and method == "POST":
            return {"sha": _git_blob_sha(base64.b64decode(payload["content"]))}
        if endpoint == f"repos/{REPO}/git/trees" and method == "POST":
            return {"sha": sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40]}
        if endpoint == f"repos/{REPO}/git/commits" and method == "POST":
            sha = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40]
            self.commits[sha] = {"tree": {"sha": payload["tree"]}, "parents": [{"sha": p} for p in payload["parents"]],
                                 "message": payload["message"]}
            return {"sha": sha, "tree": {"sha": payload["tree"]}}
        if endpoint == f"repos/{REPO}/git/ref/heads/{BRANCH}" and method == "GET":
            return {"object": {"sha": self.ref}}
        if endpoint == f"repos/{REPO}/git/refs/heads/{BRANCH}" and method == "PATCH":
            assert payload == {"sha": payload["sha"], "force": False}
            if self.patch_mode == "race":
                self.ref = "f" * 40
                self.commits[self.ref] = {"tree": {"sha": "1" * 40}, "parents": [{"sha": "c" * 40}], "message": "other"}
                raise RuntimeError("HTTP 422: Update is not a fast forward")
            parent = self.commits[payload["sha"]]["parents"][0]["sha"]
            if parent != self.ref:
                raise RuntimeError("HTTP 422: Update is not a fast forward")
            self.ref = payload["sha"]
            if self.patch_mode == "timeout":
                raise RuntimeError("gh: timed out")
            return {"object": {"sha": self.ref}}
        if endpoint.endswith("/files?per_page=100"):
            return []
        raise AssertionError(f"Unexpected GitHub call {method} {endpoint}")


def _gh(fake: FakeGitHub, monkeypatch) -> GhCliGitHubAdapter:
    adapter = GhCliGitHubAdapter(allowed_repositories=(REPO,))
    monkeypatch.setattr(adapter, "_api", fake)
    return adapter


def _never_creates_or_rebases(fake: FakeGitHub) -> None:
    assert not any(method == "POST" and "/pulls" in endpoint for method, endpoint in fake.calls)
    assert not any(method == "PATCH" and "/pulls" in endpoint for method, endpoint in fake.calls)
    assert not any(method == "POST" and endpoint.endswith("/git/refs") for method, endpoint in fake.calls)


@pytest.mark.parametrize("patch_mode", ["apply", "timeout"])
def test_gh_advance_fast_forwards_pinned_pr_head(patch_mode, monkeypatch) -> None:
    head = "b" * 40
    fake = FakeGitHub(head=head)
    fake.patch_mode = patch_mode
    adapter = _gh(fake, monkeypatch)
    advanced = _advance(adapter, expected=head)
    assert advanced.number == 1 and advanced.head_sha == fake.ref != head
    assert fake.commits[fake.ref]["parents"] == [{"sha": head}]
    _never_creates_or_rebases(fake)
    commit_posts = sum(1 for method, endpoint in fake.calls if method == "POST" and endpoint.endswith("/git/commits"))
    fake.patch_mode = "apply"
    again = _advance(adapter, expected=head)
    assert again.head_sha == advanced.head_sha
    assert sum(1 for method, endpoint in fake.calls if method == "POST" and endpoint.endswith("/git/commits")) == commit_posts
    _never_creates_or_rebases(fake)


def test_gh_advance_refuses_foreign_head_and_lost_race(monkeypatch) -> None:
    head = "b" * 40
    fake = FakeGitHub(head=head)
    fake.ref = "e" * 40
    fake.commits[fake.ref] = {"tree": {"sha": "9" * 40}, "parents": [{"sha": head}], "message": "aitobuild: correct #7"}
    with pytest.raises(ValueError, match="moved from the pinned SHA"):
        _advance(_gh(fake, monkeypatch), expected=head)
    assert not any(method == "PATCH" for method, _ in fake.calls)
    raced = FakeGitHub(head=head)
    raced.patch_mode = "race"
    with pytest.raises(RuntimeError, match="fast forward"):
        _advance(_gh(raced, monkeypatch), expected=head)
    assert raced.ref == "f" * 40
    _never_creates_or_rebases(raced)


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"draft": False}, "open draft"),
    ({"state": "closed"}, "open draft"),
    ({"fork": True}, "base repository"),
])
def test_gh_advance_refuses_non_draft_closed_or_fork(kwargs, message, monkeypatch) -> None:
    fake = FakeGitHub(head="b" * 40, **kwargs)
    with pytest.raises(ValueError, match=message):
        _advance(_gh(fake, monkeypatch), expected="b" * 40)
    assert all(method == "GET" for method, _ in fake.calls)
