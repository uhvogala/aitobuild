"""#77: a status read that overlaps the worker finishing the assignment.

Pilot run trial4-20261019-034521 stopped on a 409 "Assignment is outside this managed activation" from
GET /internal/organization/tasks/{preview_id} right after the worker finished that assignment. The status
endpoint is sync (threadpool) and reads the assignment store while the worker finishes the assignment on the
event loop. The first test makes that overlap deterministic: the reader thread is held right after its first
assignment-store read until the real worker has finished the assignment, then it continues. The second test is
the control: the same finished assignment read at rest is inside the activation.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from aitobuild.organization_runner import ManagedOperation, WorkflowInput
from test_dispatcher import approved_delivery as approved_delivery
from test_organization_worker import finish, make_worker


def _operation():
    return ManagedOperation(lambda context, message: WorkflowInput("Decide", None, "service_approval"),
                            on_response=lambda context, original, response: response)


async def _waiting_with_queued_approval(tmp_path, approved_delivery):
    """Run to the service approval, stop that worker, and queue the approval for the next worker."""
    _, _, preview_id, _, _ = approved_delivery
    first, service, _, _ = make_worker(tmp_path, approved_delivery, _operation())
    first.enqueue(preview_id)
    await first.start()
    try:
        assert (await finish(first, preview_id)).state == "waiting"
    finally:
        await first.close()
    job = first.store.get(preview_id)
    waiting = service.recorded_run(job.task_id)
    first.enqueue_decision(waiting.assignment_id, request_id=waiting.pending[0].request_id, kind="approve", value=True)
    return job.task_id, waiting


def _read(service, read, task_id, preview_id):
    return service.recorded_run(task_id) if read == "recorded_run" else service.status(preview_id)


@pytest.mark.parametrize("hold_after", [1, 2, 3])
@pytest.mark.parametrize("read", ["recorded_run", "status"])
def test_status_read_overlapping_finish_returns_the_final_state(tmp_path, approved_delivery, read, hold_after):
    """Hold the reader after its Nth assignment-store read until the worker has finished the assignment.

    Wherever the finish lands between the reader's store reads, the read must not be refused: it returns the
    run, which is past running once the reader was held through the finish, and the next read through the same
    path returns the final, completed run. On master,
    hold_after=2 lands between `_assignment`'s `get` and its `for_task` and reproduces the pilot's 409.
    """
    _, _, preview_id, _, _ = approved_delivery
    reader = threading.local()
    held_or_done = threading.Event()
    assignment_finished = threading.Event()
    reads = []

    async def exercise():
        task_id, waiting = await _waiting_with_queued_approval(tmp_path, approved_delivery)
        worker, service, assignments, _ = make_worker(tmp_path, approved_delivery, _operation())
        assert assignments.get(waiting.assignment_id).state == "claimed"
        real_finish = assignments.finish

        def counted(name):
            real = getattr(assignments, name)

            def read_once(*args, **kwargs):
                result = real(*args, **kwargs)
                if getattr(reader, "active", False):
                    reads.append(name)
                    if len(reads) == hold_after:
                        held_or_done.set()
                        assert assignment_finished.wait(10), "the worker never finished the assignment"
                return result
            return read_once

        for name in ("get", "for_task", "get_with_owner"):
            if hasattr(assignments, name):
                setattr(assignments, name, counted(name))

        def signalling_finish(*args, **kwargs):
            record = real_finish(*args, **kwargs)
            assignment_finished.set()
            return record

        assignments.finish = signalling_finish

        def status_read():
            reader.active = True
            try:
                return _read(service, read, task_id, preview_id)
            finally:
                reader.active = False
                held_or_done.set()

        pending_read = asyncio.get_running_loop().run_in_executor(None, status_read)
        assert await asyncio.to_thread(held_or_done.wait, 10)
        await worker.start()
        try:
            assert (await finish(worker, preview_id)).state == "completed"
            result = await pending_read
        finally:
            await worker.close()
        assert assignment_finished.is_set()
        assert result is not None
        assert result.assignment_id == waiting.assignment_id
        if len(reads) >= hold_after:
            # The reader resumed after finish(); the run is saved completed right after it (finalizing until then).
            assert result.state in {"finalizing", "completed"}
        else:
            assert result.state == "waiting"
        assert _read(service, read, task_id, preview_id).state == "completed"

    asyncio.run(exercise())


@pytest.mark.parametrize("read", ["recorded_run", "status"])
def test_status_read_after_finish_passes_the_scope_rule(tmp_path, approved_delivery, read):
    _, _, preview_id, _, _ = approved_delivery

    async def exercise():
        task_id, waiting = await _waiting_with_queued_approval(tmp_path, approved_delivery)
        worker, service, assignments, _ = make_worker(tmp_path, approved_delivery, _operation())
        await worker.start()
        try:
            assert (await finish(worker, preview_id)).state == "completed"
        finally:
            await worker.close()
        assert assignments.get(waiting.assignment_id).state == "completed"
        result = _read(service, read, task_id, preview_id)
        assert result is not None and result.state == "completed"

    asyncio.run(exercise())
