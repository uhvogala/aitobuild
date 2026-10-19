"""#41/#72: bounded retry of GitHub reads on 502/503/504 and transport failures; writes are never repeated."""

from __future__ import annotations

from subprocess import CalledProcessError, CompletedProcess, TimeoutExpired

import pytest

from aitobuild.tools import github as github_module
from aitobuild.tools.github import (
    GhCliGitHubAdapter,
    GitHubAPIError,
    GitHubTransientError,
    is_transport_failure,
    transient_http_status,
)

REPO = "example/target"


def adapter(sleeps: list[float], *, attempts: int = 4) -> GhCliGitHubAdapter:
    return GhCliGitHubAdapter(allowed_repositories=(REPO,), read_attempts=attempts, backoff_base=0.5, backoff_cap=2.0,
                              sleep=sleeps.append, jitter=lambda: 1.0)


def scripted(monkeypatch, outcomes: list[object]) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, str):
            raise CalledProcessError(1, command, stderr=outcome)
        return CompletedProcess(command, 0, stdout=str(outcome[0]) if isinstance(outcome, tuple) else "", stderr="")

    monkeypatch.setattr(github_module, "run", fake_run)
    return calls


@pytest.mark.parametrize("status", [502, 503, 504])
def test_transient_read_is_retried_then_succeeds(monkeypatch, status):
    sleeps: list[float] = []
    calls = scripted(monkeypatch, [f"gh: Bad gateway (HTTP {status})", ('{"number": 7}',)])
    assert adapter(sleeps)._api(f"repos/{REPO}/issues/7") == {"number": 7}
    assert len(calls) == 2 and sleeps == [0.5]


def test_exhausted_read_retries_fail_cleanly_with_capped_backoff(monkeypatch):
    sleeps: list[float] = []
    calls = scripted(monkeypatch, ["gh: (HTTP 502)"] * 5)
    with pytest.raises(GitHubTransientError, match="HTTP 502"):
        adapter(sleeps, attempts=5)._api(f"repos/{REPO}/issues?state=all&per_page=100&page=1")
    assert len(calls) == 5 and sleeps == [0.5, 1.0, 2.0, 2.0]


def test_backoff_is_jittered_within_bounds(monkeypatch):
    sleeps: list[float] = []
    scripted(monkeypatch, ["gh: (HTTP 503)", ('[]',)])
    GhCliGitHubAdapter(allowed_repositories=(REPO,), backoff_base=1.0, sleep=sleeps.append,
                       jitter=lambda: 0.0)._api(f"repos/{REPO}/issues")
    assert sleeps == [0.5]


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_transient_write_is_never_repeated(monkeypatch, method):
    sleeps: list[float] = []
    calls = scripted(monkeypatch, ["gh: (HTTP 502)", ('{}',)])
    with pytest.raises(GitHubTransientError):
        adapter(sleeps)._api(f"repos/{REPO}/issues", method=method, payload={"title": "x"})
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize("detail", ["gh: Not Found (HTTP 404)", "gh: Server Error (HTTP 500)", "gh: (HTTP 422)",
                                    "gh: (HTTP 403) rate limit", "gh: Conflict (HTTP 409)",
                                    "gh: Validation Failed (HTTP 422): body mentions TLS handshake timeout and EOF",
                                    "gh: unknown flag --nope", "accepts 1 arg(s), received 0"])
def test_non_transient_failures_are_not_retried(monkeypatch, detail):
    sleeps: list[float] = []
    calls = scripted(monkeypatch, [detail, ('{}',)])
    with pytest.raises(RuntimeError) as raised:
        adapter(sleeps)._api(f"repos/{REPO}/issues/1")
    assert not isinstance(raised.value, GitHubTransientError)
    assert len(calls) == 1 and not sleeps


def test_status_parsing_only_matches_gateway_errors():
    assert transient_http_status("gh: Bad Gateway (HTTP 502)") == 502
    assert transient_http_status("HTTP 5020") is None
    assert transient_http_status("issue 502 mentioned (HTTP 404)") is None


def test_marker_lookup_survives_a_single_502(monkeypatch):
    sleeps: list[float] = []
    body = '[{"number": 3, "id": 30, "title": "t", "body": "x <!-- m -->", "state": "open", "labels": []}]'
    scripted(monkeypatch, ["gh: (HTTP 502)", (body,)])
    found = adapter(sleeps).find_issue_publication(repository=REPO, marker="<!-- m -->")
    assert [issue.number for issue in found] == [3] and sleeps == [0.5]


def test_retry_configuration_is_bounded():
    for kwargs in ({"read_attempts": 0}, {"read_attempts": True}, {"backoff_base": -1.0}, {"backoff_cap": 0.1}):
        with pytest.raises(ValueError):
            GhCliGitHubAdapter(allowed_repositories=(REPO,), **kwargs)


# Transport failures as gh reports them, including the two from pilot trial4-20261019-050235 (#72).
TRANSPORT_FAILURES = [
    'Get "https://api.github.com/repos/example/target/commits/abc": net/http: TLS handshake timeout',
    'Get "https://api.github.com/repos/example/target": EOF',
    'Post "https://api.github.com/graphql": unexpected EOF',
    'read tcp 10.0.0.2:51234->140.82.121.6:443: read: connection reset by peer',
    'dial tcp 140.82.121.6:443: connect: connection refused',
    'dial tcp 140.82.121.6:443: i/o timeout',
    'Get "https://api.github.com/user": context deadline exceeded (Client.Timeout exceeded while awaiting headers)',
    'dial tcp: lookup api.github.com: no such host',
    "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com",
]


@pytest.mark.parametrize("detail", TRANSPORT_FAILURES)
def test_transport_failures_are_the_one_transient_type(detail):
    assert is_transport_failure(detail)


@pytest.mark.parametrize("detail", TRANSPORT_FAILURES)
def test_transport_failure_read_is_retried_then_succeeds(monkeypatch, detail):
    sleeps: list[float] = []
    calls = scripted(monkeypatch, [detail, ('{"sha": "abc"}',)])
    assert adapter(sleeps)._api(f"repos/{REPO}/commits/abc") == {"sha": "abc"}
    assert len(calls) == 2 and sleeps == [0.5]


def test_exhausted_transport_retries_raise_transient_without_status(monkeypatch):
    sleeps: list[float] = []
    calls = scripted(monkeypatch, [TRANSPORT_FAILURES[0]] * 5)
    with pytest.raises(GitHubTransientError, match="TLS handshake timeout") as raised:
        adapter(sleeps)._api(f"repos/{REPO}/commits/abc")
    assert raised.value.status is None and raised.value.retryable
    assert len(calls) == 4 and sleeps == [0.5, 1.0, 2.0]  # the read bound, then it surfaces


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
@pytest.mark.parametrize("detail", [TRANSPORT_FAILURES[0], TRANSPORT_FAILURES[3]])
def test_transport_failure_write_is_never_repeated(monkeypatch, method, detail):
    sleeps: list[float] = []
    calls = scripted(monkeypatch, [detail, ('{}',)])
    with pytest.raises(GitHubTransientError):
        adapter(sleeps)._api(f"repos/{REPO}/issues", method=method, payload={"title": "x"})
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_gh_timeout_is_transient_but_not_repeated(monkeypatch, method):
    """A gh call that hit its own timeout already spent the whole per-call budget: transient, never repeated."""
    sleeps: list[float] = []
    calls = scripted(monkeypatch, [TimeoutExpired(["gh"], 60), ('{}',)])
    with pytest.raises(GitHubTransientError, match="timed out") as raised:
        adapter(sleeps)._api(f"repos/{REPO}/issues/1", method=method, payload={"x": 1} if method == "POST" else None)
    assert not raised.value.retryable and raised.value.status is None
    assert len(calls) == 1 and not sleeps


def test_api_answers_are_never_transport_failures():
    assert not is_transport_failure("gh: Not Found (HTTP 404): EOF")
    assert not is_transport_failure("gh: Conflict (HTTP 409)")
    error = GitHubAPIError("x", status=404)
    assert not isinstance(error, GitHubTransientError)
