"""#72 part 2: one bounded gh call timeout, a read-retry bound in time, and an immutable full-SHA commit cache."""

from __future__ import annotations

from subprocess import CalledProcessError, CompletedProcess
import json

import pytest

from aitobuild.config import load_config
from aitobuild.tools import github as github_module
from aitobuild.tools.github import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    GhCliGitHubAdapter,
    GitHubTransientError,
    build_github_adapter,
)

REPO = "example/target"
SHA = "a" * 40
TLS = "net/http: TLS handshake timeout"


def scripted(monkeypatch, outcomes, clock=None, step=0.0):
    calls: list[tuple[list[str], float]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        if clock is not None:
            clock[0] += step
        outcome = outcomes.pop(0) if outcomes else outcomes_default(command)
        if isinstance(outcome, str):
            raise CalledProcessError(1, command, stderr=outcome)
        return CompletedProcess(command, 0, stdout=json.dumps(outcome), stderr="")

    def outcomes_default(command):
        return {"sha": command[4].rsplit("/", 1)[1]}

    monkeypatch.setattr(github_module, "run", fake_run)
    return calls


def adapter(**kwargs) -> GhCliGitHubAdapter:
    return GhCliGitHubAdapter(allowed_repositories=(REPO,), sleep=lambda _: None, jitter=lambda: 1.0, **kwargs)


def test_every_gh_call_uses_the_one_configured_timeout(monkeypatch):
    calls = scripted(monkeypatch, [{"number": 1}])
    adapter()._api(f"repos/{REPO}/issues/1")
    assert calls[0][1] == DEFAULT_CALL_TIMEOUT_SECONDS == 20
    calls = scripted(monkeypatch, [{"number": 1}])
    adapter(call_timeout_seconds=7)._api(f"repos/{REPO}/issues/1", method="POST", payload={"x": 1})
    assert calls[0][1] == 7


@pytest.mark.parametrize("value", [0, 60, 120, 1.5, True])
def test_call_timeout_is_bounded_below_gh_default(value):
    with pytest.raises(ValueError, match="call timeout"):
        adapter(call_timeout_seconds=value)


def test_call_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("AITOBUILD_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("AITOBUILD_INTERNAL_API_TOKEN", "test-token")
    assert load_config().github.call_timeout_seconds == 20
    monkeypatch.setenv("AITOBUILD_GITHUB_CALL_TIMEOUT_SECONDS", "9")
    assert load_config().github.call_timeout_seconds == 9
    monkeypatch.setenv("AITOBUILD_GITHUB_CALL_TIMEOUT_SECONDS", "60")
    with pytest.raises(ValueError, match="1..59"):
        load_config()
    built = build_github_adapter(mode="gh_cli", allowed_repositories=(REPO,), call_timeout_seconds=9)
    assert isinstance(built, GhCliGitHubAdapter) and built.call_timeout_seconds == 9


def test_read_retries_stop_once_one_call_timeout_has_been_spent(monkeypatch):
    """Each failing call takes 8 s here: with a 20 s call timeout a retry starts at 8 s and 16 s, never at 24 s."""
    clock = [0.0]
    calls = scripted(monkeypatch, [TLS] * 4, clock=clock, step=8.0)
    with pytest.raises(GitHubTransientError):
        adapter(clock=lambda: clock[0])._api(f"repos/{REPO}/commits/{SHA}")
    assert len(calls) == 3


@pytest.mark.parametrize("endpoint", [f"repos/{REPO}/commits/{SHA}", f"repos/{REPO}/git/commits/{SHA}"])
def test_full_sha_commit_reads_are_cached(monkeypatch, endpoint):
    calls = scripted(monkeypatch, [])
    github = adapter()
    first = github._api(endpoint)
    first["mutated"] = True  # callers get their own copy
    assert github._api(endpoint) == {"sha": SHA} and len(calls) == 1


@pytest.mark.parametrize("endpoint", [
    f"repos/{REPO}/git/ref/heads/main",  # a ref moves
    f"repos/{REPO}/branches/main",
    f"repos/{REPO}/commits/main",  # a branch name, not a SHA
    f"repos/{REPO}/commits/{SHA[:12]}",  # a short SHA
    f"repos/{REPO}/commits/{SHA}?per_page=1",
    f"repos/{REPO}",
])
def test_refs_branches_and_short_shas_are_never_cached(monkeypatch, endpoint):
    calls = scripted(monkeypatch, [{"sha": SHA}, {"sha": SHA}])
    github = adapter()
    github._api(endpoint)
    github._api(endpoint)
    assert len(calls) == 2


def test_failed_or_mismatched_commit_reads_are_not_cached(monkeypatch):
    calls = scripted(monkeypatch, [TLS, {"sha": "b" * 40}])
    github = adapter(read_attempts=1)
    with pytest.raises(GitHubTransientError):
        github._api(f"repos/{REPO}/commits/{SHA}")
    assert github._api(f"repos/{REPO}/commits/{SHA}") == {"sha": "b" * 40}  # not the asked SHA: not cached
    assert github._api(f"repos/{REPO}/commits/{SHA}") == {"sha": SHA}
    assert github._api(f"repos/{REPO}/commits/{SHA}") == {"sha": SHA}
    assert len(calls) == 3


def test_writes_are_never_served_from_the_cache(monkeypatch):
    calls = scripted(monkeypatch, [])
    github = adapter()
    github._api(f"repos/{REPO}/git/commits/{SHA}")
    github._api(f"repos/{REPO}/git/commits/{SHA}", method="POST", payload={"x": 1})
    assert len(calls) == 2
