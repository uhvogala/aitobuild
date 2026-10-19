"""#72: the harness's one HTTP helper retries a `transient` 503 only after a state re-read shows nothing landed,
counts each retry in the report, and keeps every 409 fatal."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))

import common  # noqa: E402

TRANSIENT = {"detail": "gh api failed for GET repos/a/b/commits/c: net/http: TLS handshake timeout",
             "transient": True, "github_status": None}


class _Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code, self._body, self.text = status_code, body, str(body)

    def json(self) -> Any:
        return self._body


class _Client:
    """A pending tool approval; each approve POST answers the next scripted response."""

    def __init__(self, approvals: list[_Response], *, lands_on_failure: bool = False, reads: list[_Response] | None = None):
        self.approvals, self.lands_on_failure, self.reads = approvals, lands_on_failure, reads or []
        self.posts: list[dict[str, Any]] = []
        self.state = "waiting"

    def get(self, path: str, **_: Any) -> _Response:
        if self.reads:
            return self.reads.pop(0)
        if self.state == "completed":
            return _Response(200, {"managed_admission": {"state": "completed"},
                                   "managed_run": {"state": "completed", "pending": []}})
        return _Response(200, {"managed_admission": {"state": "waiting"}, "managed_run": {
            "state": "waiting", "assignment_id": "a-1",
            "pending": [{"request_id": "r-1", "kind": "service_approval", "prompt": "Native delivery tool approval",
                         "approval_stage": "developer_tools", "data": {}}]}})

    def post(self, path: str, json: dict[str, Any], **_: Any) -> _Response:
        self.posts.append(json)
        response = self.approvals.pop(0)
        if response.status_code == 200 or self.lands_on_failure:
            self.state = "completed"
        return response


def _operator(tmp_path: Path, client: Any, sleeps: list[float]) -> common.Operator:
    recorder = common.Recorder(tmp_path / "artifacts")
    return common.Operator(client, "token", recorder, poll=0.001, timeout=5, transient_retry_delays=(1.0, 2.0),
                           sleep=sleeps.append)


def test_transient_approval_is_retried_after_the_re_read_and_counted(tmp_path: Path) -> None:
    sleeps: list[float] = []
    client = _Client([_Response(503, TRANSIENT), _Response(200, {})])
    operator = _operator(tmp_path, client, sleeps)
    assert operator.drive("dp-1", "delivery:x")["managed_run"]["state"] == "completed"
    assert len(client.posts) == 2 and sleeps == [1.0]
    retries = operator.recorder.report["transient_retries"]
    assert [(item["method"], item["path"], item["attempt"]) for item in retries] == [
        ("POST", "/internal/organization/tasks/approve", 1)]
    assert "TLS handshake timeout" in retries[0]["detail"]


def test_transient_retries_are_bounded(tmp_path: Path) -> None:
    sleeps: list[float] = []
    client = _Client([_Response(503, TRANSIENT)] * 3)
    operator = _operator(tmp_path, client, sleeps)
    with pytest.raises(RuntimeError, match="approve -> 503"):
        operator.drive("dp-1", "delivery:x")
    assert len(client.posts) == 3 and sleeps == [1.0, 2.0]
    assert len(operator.recorder.report["transient_retries"]) == 2


def test_a_write_that_landed_is_not_retried(tmp_path: Path) -> None:
    sleeps: list[float] = []
    client = _Client([_Response(503, TRANSIENT), _Response(200, {})], lands_on_failure=True)
    operator = _operator(tmp_path, client, sleeps)
    with pytest.raises(RuntimeError, match="state re-read shows the write landed; not retried"):
        operator.drive("dp-1", "delivery:x")
    assert len(client.posts) == 1 and not sleeps and "transient_retries" not in operator.recorder.report


@pytest.mark.parametrize("response", [
    _Response(409, {"detail": "gh api failed for GET repos/a/b/commits/c: net/http: TLS handshake timeout"}),
    _Response(503, {"detail": "Service Unavailable"}),  # no marker: not the service's transient answer
    _Response(500, {"detail": "boom", "transient": True}),
])
def test_refusals_and_unmarked_failures_stay_fatal(tmp_path: Path, response: _Response) -> None:
    sleeps: list[float] = []
    client = _Client([response, _Response(200, {})])
    operator = _operator(tmp_path, client, sleeps)
    with pytest.raises(RuntimeError, match=f"approve -> {response.status_code}"):
        operator.drive("dp-1", "delivery:x")
    assert len(client.posts) == 1 and not sleeps and "transient_retries" not in operator.recorder.report


def test_a_write_without_a_re_read_never_retries(tmp_path: Path) -> None:
    sleeps: list[float] = []
    client = _Client([_Response(503, TRANSIENT), _Response(200, {})])
    with pytest.raises(RuntimeError, match="run -> 503"):
        _operator(tmp_path, client, sleeps).post("/internal/organization/tasks/run", {"preview_id": "dp-1"})
    assert len(client.posts) == 1 and not sleeps


def test_a_transient_status_read_is_retried_and_a_409_read_stays_fatal(tmp_path: Path) -> None:
    sleeps: list[float] = []
    ok = _Response(200, {"managed_admission": None, "managed_run": None})
    operator = _operator(tmp_path, _Client([], reads=[_Response(503, TRANSIENT), ok]), sleeps)
    assert operator.task("dp-1") == ok.json() and sleeps == [1.0]
    assert operator.recorder.report["transient_retries"][0]["method"] == "GET"
    operator = _operator(tmp_path, _Client([], reads=[_Response(409, {"detail": "outside"}), ok]), sleeps)
    with pytest.raises(RuntimeError, match="task status dp-1 -> 409"):
        operator.task("dp-1")
