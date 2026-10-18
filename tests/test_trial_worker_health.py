"""#69: the trial harness fails a run at once when a job is queued while no managed worker loop is alive."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))

import common  # noqa: E402


def _diagnostics(alive: int, expected: int = 2, errors: tuple[str, ...] = ()) -> dict[str, Any]:
    status = "ok" if alive == expected else "stopped" if alive == 0 else "degraded"
    return {"health": {"status": status, "workers_alive": alive, "workers_expected": expected}, "errors": list(errors)}


def test_queued_without_worker_reasons() -> None:
    assert common.queued_without_worker(_diagnostics(2)) is None
    assert common.queued_without_worker(_diagnostics(1)) is None  # a degraded pool can still run the job
    reason = common.queued_without_worker(_diagnostics(0, errors=("Interrupted worker receipt",)))
    assert reason == "no managed worker loop is alive (status stopped, 0/2 loops): Interrupted worker receipt"
    assert common.queued_without_worker({"workers": 0}) == "the worker status has no loop health signal"
    assert common.queued_without_worker({"health": {"workers_alive": "0"}}) == "the worker status has no loop health signal"


class _Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code, self._body, self.text = status_code, body, str(body)

    def json(self) -> Any:
        return self._body


class _Client:
    def __init__(self, worker: _Response, job_state: str = "queued") -> None:
        self.worker, self.job_state, self.worker_calls = worker, job_state, 0

    def get(self, path: str, **_: Any) -> _Response:
        if path == "/internal/organization/worker":
            self.worker_calls += 1
            return self.worker
        return _Response(200, {"managed_admission": {"state": self.job_state}, "managed_run": None})


def _operator(tmp_path: Path, client: _Client, timeout: float = 0.05) -> common.Operator:
    recorder = common.Recorder(tmp_path / "artifacts")
    recorder.report.setdefault("stalls", [])
    return common.Operator(client, "token", recorder, poll=0.001, timeout=timeout)


def test_drive_fails_immediately_on_a_queued_job_without_a_live_loop(tmp_path: Path) -> None:
    client = _Client(_Response(200, _diagnostics(0, errors=("Interrupted worker receipt",))))
    operator = _operator(tmp_path, client, timeout=60)
    with pytest.raises(RuntimeError, match=r"correction:x: job dp-1 is queued but no managed worker loop is alive"):
        operator.drive("dp-1", "correction:x")
    assert client.worker_calls == 1
    stall = operator.recorder.report["stalls"][-1]
    assert stall["preview_id"] == "dp-1" and stall["worker_errors"] == ["Interrupted worker receipt"]


@pytest.mark.parametrize("worker", [_Response(200, _diagnostics(1)), _Response(404, {"detail": "Not Found"})])
def test_drive_keeps_waiting_while_a_loop_is_alive_or_no_worker_exists(tmp_path: Path, worker: _Response) -> None:
    client = _Client(worker)
    operator = _operator(tmp_path, client)
    with pytest.raises(TimeoutError):
        operator.drive("dp-1", "delivery:x")
    assert client.worker_calls >= 1 and not operator.recorder.report["stalls"]


def test_drive_checks_worker_health_only_for_queued_jobs(tmp_path: Path) -> None:
    client = _Client(_Response(200, _diagnostics(0)), job_state="running")
    with pytest.raises(TimeoutError):
        _operator(tmp_path, client).drive("dp-1", "delivery:x")
    assert client.worker_calls == 0


def test_worker_status_error_is_not_ignored(tmp_path: Path) -> None:
    client = _Client(_Response(500, "boom"))
    with pytest.raises(RuntimeError, match="worker status -> 500"):
        _operator(tmp_path, client).drive("dp-1", "delivery:x")
