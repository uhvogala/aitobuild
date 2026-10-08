"""Durable local admission and detached execution of configured managed tasks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from filelock import FileLock, Timeout as LockTimeout
from pydantic import AwareDatetime, Field, JsonValue, StrictBool, StrictInt, StrictStr, model_validator

from aitobuild.durable_files import atomic_write_text
from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.organization import DefinitionModel
from aitobuild.organization_assignments import AssignmentCapacityError, AssignmentProposal, Digest
from aitobuild.organization_service import ManagedOrganizationService, ManagedRoute


class WorkerCommand(DefinitionModel):
    kind: Literal["run", "approve", "resume"]
    actor_id: StrictStr
    created_at: AwareDatetime
    request_id: StrictStr | None = None
    value: JsonValue = None
    proposal: AssignmentProposal | None = None

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if not self.actor_id.strip():
            raise ValueError("Worker commands require trusted actor identity")
        if self.kind == "run":
            if self.request_id is not None or self.value is not None:
                raise ValueError("Initial admission cannot contain a native response")
        elif self.request_id is None or not self.request_id.strip() or self.proposal is not None:
            raise ValueError("Worker continuations require the exact saved request")
        if self.kind == "approve" and type(self.value) is not bool:
            raise ValueError("Service approval requires an actual Boolean")
        if len(json.dumps(self.value, allow_nan=False).encode()) > 32000:
            raise ValueError("Worker input exceeds the prompt byte limit")
        return self


class WorkerJob(DefinitionModel):
    preview_id: StrictStr
    task_id: StrictStr
    activation: ManagedRoute
    binding_revision: StrictStr
    bundle_content: StrictStr
    scope_digest: Digest
    approved_at: AwareDatetime
    commands: tuple[WorkerCommand, ...]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    state: Literal["queued", "running", "waiting", "completed", "failed", "cancelled"] = "queued"
    error: StrictStr | None = None
    cancel_requested: StrictBool = False

    @model_validator(mode="after")
    def validate_job(self) -> Self:
        payload = json.loads(self.bundle_content)
        if (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) != self.bundle_content or
                sha256(self.bundle_content.encode()).hexdigest() != self.scope_digest or
                developer_task_bundle_from_payload(payload).task_id != self.task_id):
            raise ValueError("Worker scope/task pins are invalid")
        if not self.commands or self.commands[0].kind != "run" or len(self.commands) > 256:
            raise ValueError("Worker requires bounded initial admission and decision history")
        request_ids = [command.request_id for command in self.commands if command.request_id is not None]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("Worker decisions cannot be admitted twice")
        if self.created_at < self.approved_at or self.updated_at < self.created_at:
            raise ValueError("Worker timestamps cannot precede approval/admission")
        if any(command.created_at < self.created_at or command.created_at > self.updated_at for command in self.commands):
            raise ValueError("Worker command timestamps must lie within the receipt history")
        if self.state == "failed" and not self.error:
            raise ValueError("Failed worker receipts require a diagnostic")
        return self


class WorkerJournal(DefinitionModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    jobs: tuple[WorkerJob, ...] = ()


class WorkerStore(Protocol):
    def get(self, preview_id: str) -> WorkerJob | None: ...
    def list(self) -> tuple[WorkerJob, ...]: ...
    def admit(self, job: WorkerJob) -> WorkerJob: ...
    def save(self, job: WorkerJob) -> None: ...
    def request_cancel(self, preview_id: str) -> WorkerJob: ...
    def invocation_lock(self, preview_id: str) -> FileLock: ...


class _ReceiptError(RuntimeError):
    pass


class FileWorkerStore:
    def __init__(self, path: Path) -> None:
        self._path = path.absolute()
        path.parent.mkdir(parents=True, exist_ok=True)

    def _checked(self, path: Path) -> Path:
        if path.resolve() != path:
            raise ValueError("Worker state cannot follow symlinks")
        return path

    def _lock(self) -> FileLock:
        return FileLock(self._checked(Path(str(self._path) + ".lock")), timeout=10)

    def _load(self) -> WorkerJournal:
        path = self._checked(self._path)
        if not path.exists():
            return WorkerJournal()
        journal = WorkerJournal.model_validate_json(path.read_text(encoding="utf-8"))
        if len({job.preview_id for job in journal.jobs}) != len(journal.jobs):
            raise ValueError("Worker journal has duplicate preview ownership")
        if len({job.task_id for job in journal.jobs}) != len(journal.jobs):
            raise ValueError("Worker journal has duplicate task ownership")
        return journal

    def _save(self, journal: WorkerJournal) -> None:
        atomic_write_text(self._checked(self._path), journal.model_dump_json())

    def get(self, preview_id: str) -> WorkerJob | None:
        with self._lock():
            return next((job for job in self._load().jobs if job.preview_id == preview_id), None)

    def list(self) -> tuple[WorkerJob, ...]:
        with self._lock():
            return self._load().jobs

    def admit(self, job: WorkerJob) -> WorkerJob:
        job = WorkerJob.model_validate(job.model_dump())
        if job.state != "queued" or len(job.commands) != 1 or job.cancel_requested:
            raise ValueError("New worker admissions must be pristine queued receipts")
        with self._lock():
            journal = self._load()
            original = next((item for item in journal.jobs if item.preview_id == job.preview_id), None)
            if original is not None:
                pins = {"preview_id", "task_id", "bundle_content", "scope_digest", "approved_at"}
                if original.model_dump(include=pins) != job.model_dump(include=pins):
                    raise ValueError("Duplicate admission cannot replace approved scope")
                return original
            if any(item.task_id == job.task_id for item in journal.jobs):
                raise ValueError("Worker task already has a different preview owner")
            self._save(WorkerJournal(jobs=(*journal.jobs, job)))
            return job

    def save(self, job: WorkerJob) -> None:
        job = WorkerJob.model_validate(job.model_dump())
        with self._lock():
            journal = self._load()
            original = next((item for item in journal.jobs if item.preview_id == job.preview_id), None)
            if original is None:
                raise ValueError("Worker admission not found")
            mutable = {"state", "updated_at", "error", "commands", "cancel_requested"}
            if original.model_dump(exclude=mutable) != job.model_dump(exclude=mutable):
                raise ValueError("Worker admission pins are immutable")
            if original.state in {"completed", "failed", "cancelled"} and original != job:
                raise ValueError("Terminal worker receipts are immutable")
            if job.commands[:len(original.commands)] != original.commands:
                raise ValueError("Worker command history is immutable")
            if len(job.commands) > len(original.commands) and (original.state != "waiting" or job.state != "queued"):
                raise ValueError("Only idle waiting jobs can admit a continuation")
            if original.cancel_requested and not job.cancel_requested:
                raise ValueError("Worker cancellation cannot be revoked")
            transitions = {
                "queued": {"queued", "running", "cancelled", "failed"},
                "running": {"running", "queued", "waiting", "completed", "failed", "cancelled"},
                "waiting": {"waiting", "queued", "cancelled", "failed"},
            }
            if original.state not in {"completed", "failed", "cancelled"} and job.state not in transitions[original.state]:
                raise ValueError("Worker lifecycle cannot rewind")
            self._save(WorkerJournal(jobs=tuple(job if item.preview_id == job.preview_id else item for item in journal.jobs)))

    def request_cancel(self, preview_id: str) -> WorkerJob:
        with self._lock():
            journal = self._load()
            job = next((item for item in journal.jobs if item.preview_id == preview_id), None)
            if job is None:
                raise ValueError("Worker admission not found")
            if job.state in {"completed", "failed", "cancelled"}:
                return job
            updated = job.model_copy(update={"cancel_requested": True, "updated_at": datetime.now(tz=UTC)})
            self._save(WorkerJournal(jobs=tuple(updated if item.preview_id == preview_id else item for item in journal.jobs)))
            return updated

    def invocation_lock(self, preview_id: str) -> FileLock:
        path = self._path.parent / (sha256(preview_id.encode()).hexdigest() + ".worker.lock")
        return FileLock(self._checked(path), timeout=0)


class ManagedOrganizationWorker:
    def __init__(
        self, *, service: ManagedOrganizationService, store: WorkerStore,
        max_workers: int = 1, poll_seconds: float = 1, recover_approved: bool = False,
        startup_limit: int = 500,
    ) -> None:
        if (type(max_workers) is not int or not 1 <= max_workers <= 64 or type(poll_seconds) not in {float, int} or
            not math.isfinite(poll_seconds) or poll_seconds <= 0):
            raise ValueError("Worker limits must be positive and bounded")
        self.service = service
        self.store = store
        self._max_workers = max_workers
        self._poll = poll_seconds
        if type(recover_approved) is not bool or type(startup_limit) is not int or not 1 <= startup_limit <= 500:
            raise ValueError("Startup recovery must be explicit and bounded")
        self._recover = recover_approved
        self._startup_limit = startup_limit
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._active: dict[str, asyncio.Task[None]] = {}
        self._errors: list[str] = []

    def diagnostics(self) -> dict[str, object]:
        return {"workers": sum(not task.done() for task in self._tasks),
                "active_previews": tuple(self._active), "errors": tuple(self._errors)}

    def _finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._errors.append(str(error)[:4000])
                self._changed.set()

    def enqueue(self, preview_id: str, *, proposal: AssignmentProposal | None = None) -> WorkerJob | None:
        admission = self.service.admission(preview_id)
        if admission is None:
            return None
        preview, route = admission
        original = self.store.get(preview_id)
        if original is not None:
            if proposal is not None and any(command.proposal == proposal for command in original.commands):
                return original
            if original.state in {"completed", "failed", "cancelled"}:
                if proposal is not None:
                    raise ValueError("Terminal worker ownership cannot be replaced")
                return original
            if proposal is not None:
                if original.state != "waiting" or self.service.recorded_assignment(original.task_id) is not None:
                    raise ValueError("Existing worker ownership cannot be replaced")
                self.service.validate_selection(original.activation, proposal)
                command = WorkerCommand(kind="run", actor_id=self.service.operator_id,
                                        created_at=datetime.now(tz=UTC), proposal=proposal)
                with self.store.invocation_lock(preview_id):
                    self._validate(original)
                    self.store.save(original.model_copy(update={
                        "commands": (*original.commands, command), "state": "queued", "error": None,
                        "updated_at": datetime.now(tz=UTC),
                    }))
                self._wake.set()
                return self.store.get(preview_id)
            self._validate(original)
            self._wake.set()
            return original
        self.service.validate_selection(route, proposal)
        content = json.dumps(preview.bundle_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        now = datetime.now(tz=UTC)
        if preview.approved_at is None:
            raise PermissionError("Worker admission requires an approved preview")
        job = self.store.admit(WorkerJob(
            preview_id=preview_id, task_id=str(preview.bundle_payload["task_id"]), activation=route,
            binding_revision=self.service.binding_revision, bundle_content=content,
            scope_digest=sha256(content.encode()).hexdigest(), approved_at=preview.approved_at,
            commands=(WorkerCommand(kind="run", actor_id=self.service.operator_id, created_at=now, proposal=proposal),),
            created_at=now, updated_at=now,
        ))
        self._wake.set()
        return job

    def enqueue_decision(self, assignment_id: str, *, request_id: str, kind: Literal["approve", "resume"],
                         value: JsonValue) -> WorkerJob:
        preview_id = self.service.assignment_preview(assignment_id)
        with self.store.invocation_lock(preview_id):
            job = self.store.get(preview_id)
            run = self.service.recorded_run(job.task_id) if job else None
            if job is None or run is None or job.state != "waiting" or run.state != "waiting":
                raise ValueError("Worker has no idle saved continuation")
            self._validate(job)
            expected = "service_approval" if kind == "approve" else "human_input"
            if not any(request.request_id == request_id and request.kind == expected for request in run.pending):
                raise PermissionError("Worker request kind/identity is not pending")
            command = WorkerCommand(kind=kind, request_id=request_id, value=value,
                                    actor_id=self.service.operator_id, created_at=datetime.now(tz=UTC))
            updated = job.model_copy(update={"commands": (*job.commands, command), "state": "queued", "error": None,
                                             "updated_at": datetime.now(tz=UTC)})
            self.store.save(updated)
        self._wake.set()
        return WorkerJob.model_validate(updated.model_dump())

    async def start(self) -> None:
        if self._tasks:
            raise ValueError("Managed workers are already started")
        self.store.list()
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        if self._recover:
            for preview in self.service.approved_previews(limit=self._startup_limit):
                if (self.store.get(preview.preview_id) is None and self.service.admission(preview.preview_id) is not None and
                        self.service.recorded_assignment(str(preview.bundle_payload["task_id"])) is None):
                    self.enqueue(preview.preview_id)
        self._tasks = [asyncio.create_task(self._loop()) for _ in range(self._max_workers)]
        for task in self._tasks:
            task.add_done_callback(self._finished)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        drain = asyncio.gather(*self._tasks, return_exceptions=True)
        cancelled = False
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                cancelled = True
        self._tasks.clear()
        if cancelled:
            raise asyncio.CancelledError

    async def cancel(self, preview_id: str) -> WorkerJob:
        job = self.store.request_cancel(preview_id)
        if job.state in {"completed", "failed", "cancelled"}:
            return job
        active = self._active.get(preview_id)
        if active is not None:
            active.cancel()
            try:
                await asyncio.shield(active)
            except asyncio.CancelledError:
                if not active.done():
                    raise
        with self.store.invocation_lock(preview_id):
            current = self.store.get(preview_id)
            if current is None:
                raise ValueError("Worker admission disappeared")
            if current.state not in {"completed", "failed", "cancelled"}:
                await self._abort(current)
                self._update(current, state="cancelled", error="Operator cancelled worker admission")
        result = self.store.get(preview_id)
        if result is None:
            raise RuntimeError("Internal invariant violated: result is not None")
        return result

    async def wait_idle(self, preview_id: str) -> WorkerJob:
        while True:
            self._changed.clear()
            job = self.store.get(preview_id)
            if job is None:
                raise ValueError("Worker admission not found")
            if job.state not in {"queued", "running"}:
                return job
            if self._tasks and all(task.done() for task in self._tasks):
                raise RuntimeError("Managed workers stopped; inspect retained receipts and diagnostics")
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=self._poll)
            except TimeoutError:
                pass

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            for job in self.store.list():
                if job.state not in {"queued", "running"} and not (job.state == "waiting" and job.cancel_requested):
                    continue
                try:
                    with self.store.invocation_lock(job.preview_id):
                        current = self.store.get(job.preview_id)
                        if current is not None and (current.state in {"queued", "running"} or
                                                    current.state == "waiting" and current.cancel_requested):
                            parent = asyncio.current_task()
                            if parent is None:
                                raise RuntimeError("Internal invariant violated: parent is not None")
                            task = asyncio.create_task(self._execute(current))
                            self._active[current.preview_id] = task
                            try:
                                while not task.done():
                                    try:
                                        await asyncio.wait_for(asyncio.shield(task), timeout=self._poll)
                                    except TimeoutError:
                                        latest = self.store.get(current.preview_id)
                                        if latest is not None and latest.cancel_requested:
                                            task.cancel()
                                await task
                            except asyncio.CancelledError:
                                if parent.cancelling():
                                    task.cancel()
                                    while not task.done():
                                        try:
                                            await asyncio.shield(task)
                                        except asyncio.CancelledError:
                                            pass
                                    raise
                            finally:
                                self._active.pop(current.preview_id, None)
                except LockTimeout:
                    continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll)
            except TimeoutError:
                pass

    def _update(self, job: WorkerJob, *, state: str, error: str | None = None) -> None:
        current = self.store.get(job.preview_id)
        if current is None:
            raise ValueError("Worker admission disappeared")
        try:
            self.store.save(current.model_copy(update={"state": state, "error": error, "updated_at": datetime.now(tz=UTC)}))
        except Exception as failure:
            raise _ReceiptError(str(failure)) from failure
        self._changed.set()

    def _validate(self, job: WorkerJob) -> None:
        admission = self.service.admission(job.preview_id)
        if admission is None or self.service.binding_revision != job.binding_revision:
            raise PermissionError("Worker approval/activation/bindings changed")
        preview, activation = admission
        if (activation.repository, activation.repository_id, activation.organization_id) != (
                job.activation.repository, job.activation.repository_id, job.activation.organization_id):
            raise PermissionError("Worker repository activation changed")
        content = json.dumps(preview.bundle_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if preview.approved_at != job.approved_at or content != job.bundle_content:
            raise PermissionError("Worker approved scope changed")
        if job.commands[-1].actor_id != self.service.operator_id:
            raise PermissionError("Worker operator context changed")

    async def _abort(self, job: WorkerJob) -> None:
        assignment = self.service.recorded_assignment(job.task_id)
        if assignment is not None and assignment.state == "claimed":
            await self.service.cancel(assignment.assignment_id)
        path = self.service.budget_path(job.preview_id)
        if path.exists():
            try:
                DeveloperTaskBudget(path=path, bundle=developer_task_bundle_from_payload(json.loads(job.bundle_content)), create=False).abort()
            except TimeoutError:
                pass

    async def _execute(self, job: WorkerJob) -> None:
        try:
            if job.cancel_requested:
                await self._abort(job)
                self._update(job, state="cancelled", error="Operator cancelled worker admission")
                return
            command = job.commands[-1]
            assignment = self.service.recorded_assignment(job.task_id)
            run = self.service.recorded_run(job.task_id)
            if run is not None and run.state == "finalizing" and assignment is not None:
                self._update(job, state="running")
                run = await self.service.cancel(assignment.assignment_id)
                self._update(job, state=run.state, error=run.error)
                return
            self._validate(job)
            if job.state == "running":
                if assignment is None:
                    raise RuntimeError("Interrupted worker admission; automatic replay is blocked")
                if run is not None and run.state in {"waiting", "completed", "failed", "cancelled"}:
                    if command.kind == "run" or any(decision.request_id == command.request_id for decision in run.decisions):
                        self._update(job, state=run.state, error=run.error)
                        return
                    raise RuntimeError("Interrupted worker decision; automatic replay is blocked")
                if run is not None and run.state in {"ready", "running"}:
                    run = await self.service.consume(job.preview_id, activation=job.activation)
                    if run is None:
                        raise RuntimeError("Internal invariant violated: run is not None")
                    self._update(job, state=run.state, error=run.error)
                    return
            self._update(job, state="running")
            if command.kind == "approve":
                if assignment is None or command.request_id is None or type(command.value) is not bool:
                    raise ValueError("Saved worker approval identity is missing")
                run = await self.service.decide(assignment.assignment_id, request_id=command.request_id,
                                                approved=command.value)
            elif command.kind == "resume":
                if assignment is None or command.request_id is None:
                    raise ValueError("Saved worker response identity is missing")
                run = await self.service.respond(assignment.assignment_id, request_id=command.request_id, response=command.value)
            else:
                run = await self.service.consume(job.preview_id, proposal=command.proposal if assignment is None else None,
                                                 activation=job.activation)
            self._update(job, state=run.state if run is not None else "waiting", error=run.error if run else None)
        except asyncio.CancelledError:
            await self._abort(job)
            self._update(job, state="cancelled", error="Worker invocation cancelled")
            raise
        except (AssignmentCapacityError, LockTimeout) as error:
            self._update(job, state="queued", error=str(error))
        except _ReceiptError:
            raise
        except Exception as error:
            run = self.service.recorded_run(job.task_id)
            if run is not None and run.state == "finalizing":
                raise
            await self._abort(job)
            self._update(job, state="failed", error=str(error))