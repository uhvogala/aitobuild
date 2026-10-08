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
    pinned_changes,
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


def _entry(text: str) -> tuple[str, str]:
    return ("100644", _git_blob_sha(text.encode()))


# Shared, content-addressed tree store so commits copied between fakes stay readable.
TREES: dict[str, dict[str, tuple[str, str]]] = {
    "0" * 40: {"src/probe.py": _entry("x = 1\n"), "README.md": _entry("readme\n")},
    "9" * 40: {"src/probe.py": _entry("other\n"), "README.md": _entry("readme\n")},
    "1" * 40: {"src/probe.py": _entry("someone\n"), "README.md": _entry("readme\n")},
}


def _tree_level(flat: dict[str, tuple[str, str]]) -> list[dict[str, str]]:
    """One non-recursive level; subtrees are content-addressed so equal directories share a SHA."""
    entries, directories = [], {}
    for path, (mode, blob) in flat.items():
        name, _, rest = path.partition("/")
        if rest:
            directories.setdefault(name, {})[rest] = (mode, blob)
        else:
            entries.append({"path": name, "mode": mode, "type": "blob", "sha": blob})
    for name, sub in directories.items():
        sha = sha256(json.dumps(sorted(sub.items())).encode()).hexdigest()[:40]
        TREES[sha] = sub
        entries.append({"path": name, "mode": "040000", "type": "tree", "sha": sha})
    return entries


class FakeGitHub:
    def __init__(self, *, head: str, draft: bool = True, state: str = "open", fork: bool = False) -> None:
        self.ref = head
        self.commits: dict[str, dict[str, Any]] = {head: {"tree": {"sha": "0" * 40}, "parents": [{"sha": "a" * 40}],
                                                         "message": "aitobuild: implement #7"}}
        self.draft, self.state, self.fork = draft, state, fork
        self.calls: list[tuple[str, str]] = []
        self.patch_mode = "apply"
        self.on_commit: Any = None
        self.truncated = False
        self.level_truncated = False

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
            sha = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40]
            entries = dict(TREES[payload["base_tree"]])
            for item in payload["tree"]:
                if item["sha"] is None:
                    entries.pop(item["path"], None)
                else:
                    entries[item["path"]] = (item["mode"], item["sha"])
            TREES[sha] = entries
            return {"sha": sha}
        if endpoint.startswith(f"repos/{REPO}/git/trees/") and endpoint.endswith("?recursive=1") and method == "GET":
            sha = endpoint.rsplit("/", 1)[1].split("?")[0]
            if sha not in TREES:
                raise RuntimeError("HTTP 404: tree not found")
            return {"sha": sha, "truncated": self.truncated, "tree": [
                {"path": "src", "mode": "040000", "type": "tree", "sha": "d" * 40},
                *({"path": path, "mode": mode, "type": "blob", "sha": blob} for path, (mode, blob) in TREES[sha].items())]}
        if endpoint.startswith(f"repos/{REPO}/git/trees/") and "?" not in endpoint and method == "GET":
            sha = endpoint.rsplit("/", 1)[1]
            if sha not in TREES:
                raise RuntimeError("HTTP 404: tree not found")
            return {"sha": sha, "truncated": self.level_truncated, "tree": _tree_level(TREES[sha])}
        if endpoint == f"repos/{REPO}/git/commits" and method == "POST":
            sha = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40]
            self.commits[sha] = {"tree": {"sha": payload["tree"]}, "parents": [{"sha": p} for p in payload["parents"]],
                                 "message": payload["message"]}
            if self.on_commit is not None:
                self.on_commit(self)
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
            if self.patch_mode == "moved_after":
                self.ref = "f" * 40
                self.commits[self.ref] = {"tree": {"sha": "1" * 40}, "parents": [{"sha": payload["sha"]}],
                                          "message": "someone else"}
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


@pytest.mark.parametrize("stop_at", [1, 2, 3, 4])
def test_advance_rechecks_original_budget_before_every_remote_write(stop_at, monkeypatch) -> None:
    head = "b" * 40
    fake = FakeGitHub(head=head)
    checks = []

    def before_write():
        checks.append(len(checks) + 1)
        if len(checks) == stop_at:
            raise TimeoutError("Original correction deadline expired")

    with pytest.raises(TimeoutError, match="deadline expired"):
        _advance(_gh(fake, monkeypatch), expected=head, before_write=before_write)
    assert len(checks) == stop_at
    assert sum(method == "POST" for method, _ in fake.calls) == stop_at - 1
    assert all(method != "PATCH" for method, _ in fake.calls)
    assert fake.ref == head


def test_advance_rechecks_head_after_commit_creation_before_patch(monkeypatch) -> None:
    head = "b" * 40
    fake = FakeGitHub(head=head)
    adapter = _gh(fake, monkeypatch)

    def race_after_commit(endpoint, *, method="GET", payload=None):
        result = fake(endpoint, method=method, payload=payload)
        if method == "POST" and endpoint.endswith("/git/commits"):
            fake.ref = "f" * 40
            fake.commits[fake.ref] = {"tree": {"sha": "9" * 40}, "parents": [{"sha": head}], "message": "other"}
        return result

    monkeypatch.setattr(adapter, "_api", race_after_commit)
    with pytest.raises(ValueError, match="moved from the pinned SHA"):
        _advance(adapter, expected=head)
    assert all(method != "PATCH" for method, _ in fake.calls)
    assert fake.ref == "f" * 40


def test_advance_does_not_report_our_head_after_a_post_patch_race(monkeypatch) -> None:
    head = "b" * 40
    fake = FakeGitHub(head=head)
    adapter = _gh(fake, monkeypatch)

    def race_after_patch(endpoint, *, method="GET", payload=None):
        result = fake(endpoint, method=method, payload=payload)
        if method == "PATCH":
            fake.ref = "f" * 40
            fake.commits[fake.ref] = {"tree": {"sha": "9" * 40}, "parents": [{"sha": head}], "message": "other"}
        return result

    monkeypatch.setattr(adapter, "_api", race_after_patch)
    with pytest.raises(RuntimeError, match="head moved after the correction push"):
        _advance(adapter, expected=head)
    assert sum(method == "PATCH" for method, _ in fake.calls) == 1
    assert fake.ref == "f" * 40 and _reconcile(adapter, expected=head)[0] == "moved"


def test_reconciliation_needs_no_object_writes_after_budget_expiry(monkeypatch) -> None:
    head = "b" * 40
    fake = FakeGitHub(head=head)
    adapter = _gh(fake, monkeypatch)
    _advance(adapter, expected=head)
    fake.calls.clear()

    def expired(*arguments, **keywords):
        raise TimeoutError("Original correction deadline expired")

    monkeypatch.setattr(adapter, "_create_publish_tree", expired)
    outcome, pull = _reconcile(adapter, expected=head)
    assert outcome == "ours" and pull.head_sha == fake.ref
    assert all(method == "GET" for method, _ in fake.calls)


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


def _reconcile(adapter: Any, *, expected: str, **overrides: Any):
    arguments: dict[str, Any] = dict(
        role=AgentRole.DEVELOPER, repository=REPO, pull_number=1, head_branch=BRANCH, base_ref="main",
        expected_head_sha=expected, commit_message="aitobuild: correct #7",
        files={"src/probe.py": _change("fixed = True\n")}, approved=True, require_human_approval_for_repo_writes=True,
    )
    arguments.update(overrides)
    arguments["changes"] = pinned_changes(arguments.pop("files"))
    return adapter.reconcile_advanced_head(**arguments)


def _our_commit(head: str, monkeypatch) -> dict[str, Any]:
    """The exact commit a successful advance from `head` creates, learned from a throwaway fake."""
    fake = FakeGitHub(head=head)
    advanced = _advance(_gh(fake, monkeypatch), expected=head)
    return fake.commits[advanced.head_sha]


@pytest.mark.parametrize("forgery", ["wrong_parent", "wrong_message", "wrong_tree"])
def test_gh_retry_treats_a_head_as_ours_only_on_exact_tree_parent_and_message(forgery, monkeypatch) -> None:
    head = "b" * 40
    ours = _our_commit(head, monkeypatch)
    forged = json.loads(json.dumps(ours))
    if forgery == "wrong_parent":
        forged["parents"] = [{"sha": "c" * 40}]
    elif forgery == "wrong_message":
        forged["message"] = "aitobuild: correct #7 (edited)"
    else:
        forged["tree"] = {"sha": "9" * 40}
    fake = FakeGitHub(head=head)
    fake.ref = "e" * 40
    fake.commits[fake.ref] = forged
    with pytest.raises(ValueError, match="moved from the pinned SHA"):
        _advance(_gh(fake, monkeypatch), expected=head)
    assert not any(method == "PATCH" for method, _ in fake.calls)
    assert _reconcile(_gh(fake, monkeypatch), expected=head)[0] == "moved"
    exact = FakeGitHub(head=head)
    exact.ref = "e" * 40
    exact.commits[exact.ref] = ours
    assert _advance(_gh(exact, monkeypatch), expected=head).head_sha == "e" * 40
    assert _reconcile(_gh(exact, monkeypatch), expected=head)[0] == "ours"


def test_reconcile_reports_parent_ours_and_moved(monkeypatch) -> None:
    head = "b" * 40
    fake = FakeGitHub(head=head)
    adapter = _gh(fake, monkeypatch)
    outcome, pull = _reconcile(adapter, expected=head)
    assert outcome == "parent" and pull.head_sha == head
    _advance(adapter, expected=head)
    outcome, pull = _reconcile(adapter, expected=head)
    assert outcome == "ours" and pull.head_sha == fake.ref != head
    fake.ref = "f" * 40
    fake.commits[fake.ref] = {"tree": {"sha": "1" * 40}, "parents": [{"sha": head}], "message": "other"}
    assert _reconcile(adapter, expected=head)[0] == "moved"
    _never_creates_or_rebases(fake)
    mock, mock_head = _published_mock()
    assert _reconcile(mock, expected=mock_head)[0] == "parent"
    _advance(mock, expected=mock_head)
    assert _reconcile(mock, expected=mock_head)[0] == "ours"
    assert _reconcile(mock, expected=mock_head, commit_message="other")[0] == "moved"


def _writes(fake: FakeGitHub) -> list[str]:
    return [endpoint.rsplit("/git/", 1)[1].split("/")[0] if "/git/" in endpoint else endpoint.rsplit("/", 1)[1]
            for method, endpoint in fake.calls if method in {"POST", "PATCH"}]


@pytest.mark.parametrize("expire_at", [1, 2, 3, 4])
def test_gh_advance_checks_the_budget_before_every_write(expire_at, monkeypatch) -> None:
    """Writes run blob, tree, commit, ref PATCH; expiry at the nth check sends nothing from there on."""
    head = "b" * 40
    fake = FakeGitHub(head=head)
    checks: list[list[str]] = []

    def budget() -> float:
        checks.append(_writes(fake))
        if len(checks) == expire_at:
            raise TimeoutError("Approved task runtime budget has expired")
        return 60.0

    with pytest.raises(TimeoutError, match="expired"):
        _advance(_gh(fake, monkeypatch), expected=head, before_write=budget)
    assert _writes(fake) == ["blobs", "trees", "commits", "refs"][: expire_at - 1]
    assert checks == [["blobs", "trees", "commits"][:n] for n in range(expire_at)]
    assert fake.ref == head
    full = FakeGitHub(head=head)
    calls: list[list[str]] = []
    _advance(_gh(full, monkeypatch), expected=head, before_write=lambda: calls.append(_writes(full)))
    assert calls == [[], ["blobs"], ["blobs", "trees"], ["blobs", "trees", "commits"]]


@pytest.mark.parametrize("truncated", [False, True], ids=["recursive", "truncated_walk"])
def test_reconcile_is_read_only_and_matches_exact_changes(truncated, monkeypatch) -> None:
    """Settlement must never depend on the task budget, so reconcile performs no GitHub writes.

    A truncated recursive listing (large repos) falls back to an exact walk of differing subtrees."""
    head = "b" * 40
    ours = _our_commit(head, monkeypatch)
    fake = FakeGitHub(head=head)
    fake.ref = "e" * 40
    fake.commits[fake.ref] = ours
    fake.truncated = truncated
    adapter = _gh(fake, monkeypatch)
    assert _reconcile(adapter, expected=head)[0] == "ours"
    # Pinned paths left as the parent already had them still count as ours.
    assert _reconcile(adapter, expected=head, files={"src/probe.py": _change("fixed = True\n"),
                                                     "README.md": _change("readme\n"), "src/gone.py": None})[0] == "ours"
    assert _reconcile(adapter, expected=head, files={"src/probe.py": _change("fixed = True\n"),
                                                     "README.md": _change("changed\n")})[0] == "moved"
    assert _reconcile(adapter, expected=head, files={"src/probe.py": _change("fixed = False\n")})[0] == "moved"
    assert _reconcile(adapter, expected=head, files={"src/probe.py": _change("fixed = True\n"),
                                                     "README.md": None})[0] == "moved"
    mode_only = json.loads(json.dumps(ours))  # README.md made executable outside the pinned set
    readme_mode, readme_blob = TREES[ours["tree"]["sha"]]["README.md"]
    TREES["6" * 40] = {**TREES[ours["tree"]["sha"]], "README.md": ("100755", readme_blob)}
    mode_only["tree"] = {"sha": "6" * 40}
    fake.commits[fake.ref] = mode_only
    assert readme_mode == "100644" and _reconcile(adapter, expected=head)[0] == "moved"
    fake.commits[fake.ref] = ours
    executable = GitHubBlobChange(mode="100755", content=b"fixed = True\n", blob_sha=_git_blob_sha(b"fixed = True\n"))
    assert _reconcile(adapter, expected=head, files={"src/probe.py": executable})[0] == "moved"
    extra = json.loads(json.dumps(ours))
    TREES["7" * 40] = {**TREES[ours["tree"]["sha"]], "src/extra.py": _entry("sneaky\n")}
    extra["tree"] = {"sha": "7" * 40}
    fake.commits[fake.ref] = extra
    assert _reconcile(adapter, expected=head)[0] == "moved"
    fake.commits[fake.ref] = ours

    def expired() -> float:
        raise TimeoutError("expired")

    # A retry after our push landed is decided by the same read-only rule, so it needs no budget.
    assert _advance(adapter, expected=head, before_write=expired).head_sha == "e" * 40
    walked = any(method == "GET" and "/git/trees/" in endpoint and "?" not in endpoint for method, endpoint in fake.calls)
    assert walked is truncated
    fake.truncated = fake.level_truncated = True
    with pytest.raises(RuntimeError, match="truncated"):
        _reconcile(adapter, expected=head)
    assert _writes(fake) == []


def test_mock_checks_the_budget_before_writes() -> None:
    def expired() -> float:
        raise TimeoutError("expired")

    mock, mock_head = _published_mock()
    commits = len(mock.branch_commits)
    with pytest.raises(TimeoutError):
        _advance(mock, expected=mock_head, before_write=expired)
    assert len(mock.branch_commits) == commits
    assert mock.get_pull_request(repository=REPO, pull_number=1).head_sha == mock_head
    with pytest.raises(TimeoutError):
        mock.upsert_branch_commit(
            role=AgentRole.DEVELOPER, repository=REPO, branch="aitobuild/issue-8-task", base_sha="a" * 40,
            commit_message="aitobuild: implement #8", files={"src/probe.py": _change("y = 1\n")},
            approved=True, require_human_approval_for_repo_writes=True, before_write=expired)
    with pytest.raises(TimeoutError):
        mock.create_or_update_draft_pull_request(
            role=AgentRole.DEVELOPER, repository=REPO, title="T", body="Closes #8", head_branch="aitobuild/issue-8-task",
            base_ref="main", issue_number=8, approved=True, require_human_approval_for_repo_writes=True,
            before_write=expired)
    assert len(mock.branch_commits) == commits and list(mock.pull_requests[REPO]) == [1]


@pytest.mark.parametrize("race", ["foreign_head", "marked_ready", "closed"])
def test_gh_advance_rechecks_target_and_head_right_before_patch(race, monkeypatch) -> None:
    head = "b" * 40
    fake = FakeGitHub(head=head)

    def compete(github: FakeGitHub) -> None:
        if race == "foreign_head":
            github.ref = "e" * 40
            github.commits[github.ref] = {"tree": {"sha": "9" * 40}, "parents": [{"sha": head}], "message": "other"}
        elif race == "marked_ready":
            github.draft = False
        else:
            github.state = "closed"

    fake.on_commit = compete
    with pytest.raises(ValueError, match="moved from the pinned SHA" if race == "foreign_head" else "open draft"):
        _advance(_gh(fake, monkeypatch), expected=head)
    assert "refs" not in _writes(fake)


def test_gh_advance_never_reports_our_sha_when_the_ref_moved_after_patch(monkeypatch) -> None:
    head = "b" * 40
    fake = FakeGitHub(head=head)
    fake.patch_mode = "moved_after"
    adapter = _gh(fake, monkeypatch)
    with pytest.raises(RuntimeError, match="moved after the correction push"):
        _advance(adapter, expected=head)
    assert fake.ref == "f" * 40
    assert _reconcile(adapter, expected=head)[0] == "moved"


class FreshBranchGitHub(FakeGitHub):
    """Ordinary publish: a new task branch and a new draft PR."""

    def __call__(self, endpoint: str, *, method: str = "GET", payload: dict | None = None) -> Any:
        if endpoint == f"repos/{REPO}/git/ref/heads/aitobuild/issue-8-task" and method == "GET":
            self.calls.append((method, endpoint))
            raise RuntimeError("HTTP 404: Not Found")
        if endpoint == f"repos/{REPO}/git/refs" and method == "POST":
            self.calls.append((method, endpoint))
            return {"object": {"sha": (payload or {})["sha"]}}
        if endpoint.startswith(f"repos/{REPO}/pulls?") and method == "GET":
            self.calls.append((method, endpoint))
            return []
        if endpoint == f"repos/{REPO}/pulls" and method == "POST":
            self.calls.append((method, endpoint))
            return {**self.pull(), "number": 2, "head": {"ref": "aitobuild/issue-8-task", "sha": "c" * 40,
                                                         "repo": {"full_name": REPO}}}
        return super().__call__(endpoint, method=method, payload=payload)


@pytest.mark.parametrize("expire_at", [1, 2, 3, 4, 5])
def test_gh_ordinary_publish_checks_the_budget_before_every_write(expire_at, monkeypatch) -> None:
    fake = FreshBranchGitHub(head="b" * 40)
    adapter = _gh(fake, monkeypatch)
    checks = 0

    def budget() -> float:
        nonlocal checks
        checks += 1
        if checks == expire_at:
            raise TimeoutError("expired")
        return 60.0

    with pytest.raises(TimeoutError):
        head = adapter.upsert_branch_commit(
            role=AgentRole.DEVELOPER, repository=REPO, branch="aitobuild/issue-8-task", base_sha="b" * 40,
            commit_message="aitobuild: implement #8", files={"src/probe.py": _change("y = 1\n")},
            approved=True, require_human_approval_for_repo_writes=True, before_write=budget)
        adapter.create_or_update_draft_pull_request(
            role=AgentRole.DEVELOPER, repository=REPO, title="T", body="Closes #8",
            head_branch="aitobuild/issue-8-task", base_ref="main", issue_number=8, approved=True,
            require_human_approval_for_repo_writes=True, before_write=budget)
        assert head
    assert _writes(fake) == ["blobs", "trees", "commits", "refs", "pulls"][: expire_at - 1]


def test_gh_existing_pr_update_checks_the_budget_before_patch(monkeypatch) -> None:
    fake = FakeGitHub(head="b" * 40)

    def expired() -> float:
        raise TimeoutError("expired")

    with pytest.raises(TimeoutError):
        _gh(fake, monkeypatch).create_or_update_draft_pull_request(
            role=AgentRole.DEVELOPER, repository=REPO, title="Trial", body="Closes #7", head_branch=BRANCH,
            base_ref="main", issue_number=7, existing_pull_number=1, approved=True,
            require_human_approval_for_repo_writes=True, before_write=expired)
    assert fake.calls and all(method == "GET" for method, _ in fake.calls)


def test_mock_matcher_compares_modes_inside_and_outside_the_pinned_set() -> None:
    mock, head = _published_mock()
    advanced = _advance(mock, expected=head)
    assert _reconcile(mock, expected=head)[0] == "ours"
    executable = GitHubBlobChange(mode="100755", content=b"fixed = True\n", blob_sha=_git_blob_sha(b"fixed = True\n"))
    assert _reconcile(mock, expected=head, files={"src/probe.py": executable})[0] == "moved"
    tree = mock.commit_files[(REPO, advanced.head_sha)]
    tree["README.md"] = GitHubBlobChange(mode="100755", content=b"r\n", blob_sha=_git_blob_sha(b"r\n"))
    mock.commit_files[(REPO, head)]["README.md"] = replace(tree["README.md"], mode="100644")
    assert _reconcile(mock, expected=head)[0] == "moved"


class TimedOutCreateGitHub(FreshBranchGitHub):
    """`gh` times out on the PR POST; `created` says whether GitHub accepted it anyway."""

    def __init__(self, *, created: list[dict[str, Any]]) -> None:
        super().__init__(head="b" * 40)
        self.created = created
        self.posted = False

    def __call__(self, endpoint: str, *, method: str = "GET", payload: dict | None = None) -> Any:
        if endpoint.startswith(f"repos/{REPO}/pulls?") and method == "GET":
            self.calls.append((method, endpoint))
            return self.created if self.posted else []
        if endpoint == f"repos/{REPO}/pulls" and method == "POST":
            self.calls.append((method, endpoint))
            self.posted = True
            raise RuntimeError("gh: context deadline exceeded")
        return super().__call__(endpoint, method=method, payload=payload)


def _created_pull(**overrides: Any) -> dict[str, Any]:
    pull = {"number": 2, "title": "T", "body": "Closes #8", "state": "open", "draft": True,
            "head": {"ref": "aitobuild/issue-8-task", "sha": "c" * 40, "repo": {"full_name": REPO}},
            "base": {"ref": "main"}}
    pull.update(overrides)
    return pull


@pytest.mark.parametrize("created, adopted", [
    ([_created_pull()], True),
    ([], False),
    ([_created_pull(), _created_pull(number=3)], False),
    ([_created_pull(title="Someone else's PR")], False),
    ([_created_pull(draft=False)], False),
], ids=["accepted", "not_created", "ambiguous", "other_title", "not_draft"])
def test_gh_pr_create_timeout_adopts_only_the_exact_created_draft(created, adopted, monkeypatch) -> None:
    fake = TimedOutCreateGitHub(created=created)
    arguments = dict(role=AgentRole.DEVELOPER, repository=REPO, title="T", body="Closes #8",
                     head_branch="aitobuild/issue-8-task", base_ref="main", issue_number=8, approved=True,
                     require_human_approval_for_repo_writes=True)
    if adopted:
        pull = _gh(fake, monkeypatch).create_or_update_draft_pull_request(**arguments)
        assert pull.number == 2 and pull.draft and pull.head_sha == "c" * 40
    else:
        with pytest.raises(RuntimeError, match="deadline exceeded"):
            _gh(fake, monkeypatch).create_or_update_draft_pull_request(**arguments)
    assert [call for call in fake.calls if call[0] != "GET"] == [("POST", f"repos/{REPO}/pulls")]
