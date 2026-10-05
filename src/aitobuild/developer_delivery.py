"""Local-only preparation of disposable checkouts for approved issue tasks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Any

from filelock import FileLock

from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.developer_preview import DeveloperPreviewRegistry


@dataclass(frozen=True, slots=True)
class LocalRepositorySource:
    repository: str
    repository_id: int
    path: Path


@dataclass(frozen=True, slots=True)
class DeliveryPreparation:
    preview_id: str
    task_id: str
    state: str
    source_path: str
    checkout_path: str
    branch: str
    base_revision: str
    head_revision: str | None
    bundle_payload: dict[str, Any]
    approved_at: str
    updated_at: str
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class DeveloperDeliveryWorker:
    def __init__(
        self, *, preview_registry: DeveloperPreviewRegistry, state_dir: Path,
        service_root: Path, repository_sources: tuple[LocalRepositorySource, ...] = (),
        command_timeout_seconds: int = 120,
    ) -> None:
        self._previews = preview_registry
        self._state_dir = state_dir.resolve()
        self._service_root = service_root.resolve()
        self._sources = repository_sources
        self._command_timeout = command_timeout_seconds
        self._git_executable = shutil.which("git")

    def _task_dir(self, preview_id: str) -> Path:
        directory = self._state_dir / "deliveries" / sha256(preview_id.encode()).hexdigest()
        if directory.resolve() != directory:
            raise ValueError("Delivery artifacts must not be redirected through symlinks")
        return directory

    def get(self, preview_id: str) -> DeliveryPreparation | None:
        directory = self._task_dir(preview_id)
        if not directory.exists():
            return None
        with FileLock(str(directory) + ".lock", timeout=10):
            return self._load(directory)

    def prepare(self, preview_id: str) -> DeliveryPreparation:
        preview = self._previews.get(preview_id)
        if preview is None or not preview.approved or preview.approved_at is None:
            raise ValueError("Delivery preparation requires an existing human approval")
        bundle = developer_task_bundle_from_payload(preview.bundle_payload)
        issue = bundle.issue_context
        if issue is None or issue.base_revision is None:
            raise ValueError("Delivery preparation requires a repository issue and pinned base commit")
        matches = [source for source in self._sources
                   if source.repository == issue.repository and source.repository_id == issue.repository_id]
        if len(matches) != 1:
            raise ValueError("No unique operator-configured local repository source for the approved target")
        source_path = matches[0].path.resolve()
        directory = self._task_dir(preview_id)
        directory.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(directory) + ".lock", timeout=10):
            record = self._load(directory)
            if record is not None:
                if record.bundle_payload != preview.bundle_payload or record.source_path != str(source_path):
                    raise ValueError("Delivery identity/source differs from the immutable approved task")
                if record.state == "failed":
                    return record
            interrupted = record is not None and record.state == "preparing"
            fresh = record is None
            if record is None:
                record = DeliveryPreparation(
                    preview_id=preview_id, task_id=bundle.task_id, state="preparing",
                    source_path=str(source_path), checkout_path=str(directory / "repo"),
                    branch=f"aitobuild/issue-{issue.issue_number}-{sha256(bundle.task_id.encode()).hexdigest()[:16]}",
                    base_revision=issue.base_revision, head_revision=None,
                    bundle_payload=json.loads(json.dumps(preview.bundle_payload)),
                    approved_at=preview.approved_at.isoformat(), updated_at=datetime.now(tz=UTC).isoformat(),
                )
                self._save(directory, record)
            budget = None
            try:
                budget = DeveloperTaskBudget(
                    path=self._state_dir / "budgets" / (sha256(preview_id.encode()).hexdigest() + ".json"),
                    bundle=bundle, create=fresh,
                )
                if interrupted:
                    raise RuntimeError("Interrupted checkout preparation; inspect retained artifacts; automatic replay is blocked")
                budget.remaining_seconds()
                if record.state == "prepared":
                    self._verify_checkout(directory, record, budget)
                    return record
                if self._git_executable is None:
                    raise RuntimeError("Git is required for checkout preparation")
                if (source_path == self._service_root or source_path.is_relative_to(self._service_root)
                        or self._service_root.is_relative_to(source_path)):
                    raise ValueError("The service checkout cannot be a target repository source")
                if source_path.is_relative_to(self._state_dir) or self._state_dir.is_relative_to(source_path):
                    raise ValueError("Target source and delivery state directory must not overlap")
                top_level = Path(self._git(directory, budget, "-C", str(source_path), "rev-parse", "--show-toplevel").strip()).resolve()
                if top_level != source_path:
                    raise ValueError("Configured source must be the root of a target Git repository")
                common = Path(self._git(directory, budget, "-C", str(source_path), "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
                service_common = Path(self._git(directory, budget, "-C", str(self._service_root), "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
                if common == service_common or common.is_relative_to(self._service_root):
                    raise ValueError("A linked service worktree cannot be a target repository source")
                self._git(directory, budget, "check-ref-format", f"refs/heads/{issue.base_branch}")
                self._git(directory, budget, "-C", str(source_path), "cat-file", "-e", f"{issue.base_revision}^{{commit}}")
                self._git(directory, budget, "-C", str(source_path), "merge-base", "--is-ancestor", issue.base_revision, f"refs/heads/{issue.base_branch}")
                template = directory / "empty-template"
                template.mkdir()
                self._git(
                    directory, budget, "clone", "--local", "--no-hardlinks", "--dissociate", "--no-checkout",
                    "--no-recurse-submodules", f"--template={template}", "--", str(source_path), record.checkout_path,
                )
                checkout = Path(record.checkout_path)
                if (checkout / ".git" / "objects" / "info" / "alternates").exists():
                    raise ValueError("Disposable checkout must not borrow source Git objects")
                self._git(directory, budget, "-C", str(checkout), "config", "core.hooksPath", "/dev/null")
                self._git(directory, budget, "-C", str(checkout), "config", "core.fsmonitor", "false")
                self._git(directory, budget, "-C", str(checkout), "checkout", "--no-recurse-submodules", "-b", record.branch, record.base_revision, "--")
                self._verify_checkout(directory, record, budget)
                record = replace(record, state="prepared", head_revision=record.base_revision,
                                 updated_at=datetime.now(tz=UTC).isoformat())
                self._save(directory, record)
                return record
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                if budget is not None:
                    budget.abort()
                failed = replace(record, state="failed", error=str(error), updated_at=datetime.now(tz=UTC).isoformat())
                self._save(directory, failed)
                return failed

    def _verify_checkout(self, directory: Path, record: DeliveryPreparation, budget: DeveloperTaskBudget) -> None:
        checkout = directory / "repo"
        if checkout.is_symlink() or str(checkout) != record.checkout_path:
            raise ValueError("Disposable checkout identity is invalid")
        common = self._git(directory, budget, "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        if Path(common).resolve() != checkout / ".git" or (checkout / ".git").is_symlink():
            raise ValueError("Disposable checkout must own its Git directory")
        if (checkout / ".git" / "objects" / "info" / "alternates").exists():
            raise ValueError("Disposable checkout must not borrow source Git objects")
        head = self._git(directory, budget, "-C", str(checkout), "rev-parse", "HEAD").strip()
        branch = self._git(directory, budget, "-C", str(checkout), "symbolic-ref", "--short", "HEAD").strip()
        if head != record.base_revision or branch != record.branch:
            raise ValueError("Prepared checkout base/branch changed; automatic replay is blocked")
        if self._git(directory, budget, "-C", str(checkout), "status", "--porcelain", "--untracked-files=all").strip():
            raise ValueError("Prepared checkout was modified; automatic replay is blocked")

    def _git(self, directory: Path, budget: DeveloperTaskBudget, *arguments: str) -> str:
        if self._git_executable is None:
            raise RuntimeError("Git is required for checkout preparation")
        home = directory / "home"
        home.mkdir(exist_ok=True)
        command = [self._git_executable, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                   "-c", "protocol.allow=never", "-c", "protocol.file.allow=always", *arguments]
        timeout = min(self._command_timeout, budget.remaining_seconds())
        process = subprocess.Popen(
            command, cwd=directory, env={
                "PATH": os.defpath, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_LAZY_FETCH": "1",
            }, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        with (directory / "preparation.log").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"arguments": arguments, "exit_code": process.returncode,
                                  "timed_out": timed_out, "stdout": stdout, "stderr": stderr}) + "\n")
        if timed_out:
            raise TimeoutError("Git preparation exceeded its command/task deadline; see preparation.log")
        budget.remaining_seconds()
        if process.returncode:
            raise RuntimeError(f"Git preparation failed (exit {process.returncode}): {stderr.strip() or 'see preparation.log'}")
        return stdout

    def _load(self, directory: Path) -> DeliveryPreparation | None:
        path = directory / "state.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("record"), dict):
            raise ValueError("Invalid persisted delivery preparation")
        try:
            record = DeliveryPreparation(**data["record"])
        except TypeError as error:
            raise ValueError("Invalid persisted delivery preparation fields") from error
        for value in (record.preview_id, record.task_id, record.state, record.source_path, record.checkout_path,
                      record.branch, record.base_revision, record.approved_at, record.updated_at):
            if not isinstance(value, str) or not value:
                raise ValueError("Invalid persisted delivery preparation field type")
        if not isinstance(record.bundle_payload, dict):
            raise ValueError("Invalid persisted delivery scope")
        bundle = developer_task_bundle_from_payload(record.bundle_payload)
        if record.state not in {"preparing", "prepared", "failed"} or bundle.issue_context is None:
            raise ValueError("Invalid persisted delivery preparation state")
        if bundle.task_id != record.task_id or bundle.issue_context.base_revision != record.base_revision:
            raise ValueError("Persisted delivery preparation differs from the approved identity")
        expected_branch = f"aitobuild/issue-{bundle.issue_context.issue_number}-{sha256(bundle.task_id.encode()).hexdigest()[:16]}"
        if (directory != self._task_dir(record.preview_id) or record.checkout_path != str(directory / "repo")
                or record.branch != expected_branch):
            raise ValueError("Persisted delivery checkout identity is invalid")
        if (record.state == "prepared" and record.head_revision != record.base_revision
                or record.state == "failed" and (not isinstance(record.error, str) or not record.error)):
            raise ValueError("Invalid persisted delivery outcome")
        datetime.fromisoformat(record.approved_at)
        datetime.fromisoformat(record.updated_at)
        return record

    def _save(self, directory: Path, record: DeliveryPreparation) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "state.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "record": record.to_payload()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(directory / "state.json")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)