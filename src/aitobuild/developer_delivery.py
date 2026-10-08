"""Local preparation, native lifecycle and independent verification of approved issue tasks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from difflib import unified_diff
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from filelock import FileLock

from aitobuild.developer_isolation import (
    DeveloperTaskBudget, DeveloperTaskBundle, developer_task_bundle_from_payload, is_command_allowed, is_path_allowed,
)
from aitobuild.developer_preview import DeveloperPreviewRegistry
from aitobuild.publish_approvals import PublishApprovalStore, snapshot_digest
from aitobuild.policy import AgentRole
from aitobuild.tools.github import (
    GitHubAdapter,
    GitHubBlobChange,
    MockGitHubAdapter,
    pinned_changes,
    pinned_changes_from_snapshot,
    _git_blob_sha,
    _tree_fingerprint,
)
from aitobuild.tools.bash import ContainerSessionBashAdapter, _normalize_session_id
from aitobuild.tools.shell import shell_request


ARCHITECT_REVIEW_BODY_MAX = 2000
ARCHITECT_REVIEW_BODY_PREFIX = "aitobuild Architect review\n\n"
_ARCHITECT_REVIEW_EVENTS = frozenset({"COMMENT"})


CORRECTION_TASK_PREFIX = "published-correction-"


_UNBOUND_REVIEW_PREFIX = "Architect COMMENT already posted for head "
_RETIRABLE_STATES = frozenset({"failed", "prepared", "implemented", "verified"})


def _is_correction_record(record: Any) -> bool:
    return isinstance(getattr(record, "task_id", None), str) and record.task_id.startswith(CORRECTION_TASK_PREFIX)


def _refuse_correction_publication(item: Any) -> None:
    """Scoped corrections must update their pinned PR, never open a new one."""
    payload = getattr(item, "bundle_payload", None)
    if isinstance(payload, dict) and str(payload.get("task_id", "")).startswith(CORRECTION_TASK_PREFIX):
        raise PermissionError(
            "Scoped correction tasks cannot publish a new pull request; "
            "same-PR correction publication through the operator stage/publish endpoints is required"
        )


@dataclass(frozen=True, slots=True)
class LocalRepositorySource:
    repository: str
    repository_id: int
    path: Path
    verification_commands: tuple[str, ...] = ()


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
    session_id: str | None = None
    verification_commands: list[str] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    publication: dict[str, Any] | None = None
    architect_review: dict[str, Any] | None = None
    retirement: dict[str, Any] | None = None

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
        self._approvals = PublishApprovalStore(self._state_dir / "publish-approvals")
        self._service_root = service_root.resolve()
        self._sources = repository_sources
        self._command_timeout = command_timeout_seconds
        self._git_executable = shutil.which("git")

    def budget_path(self, preview_id: str) -> Path:
        return self._state_dir / "budgets" / (sha256(preview_id.encode()).hexdigest() + ".json")

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

    def implementation_lock(self, preview_id: str) -> FileLock:
        directory = self._task_dir(preview_id)
        if not directory.exists():
            raise ValueError("Repository issue requires a prepared delivery")
        return FileLock(str(directory) + ".implementation.lock", timeout=0)

    def begin_implementation(
        self, preview_id: str, *, bundle: DeveloperTaskBundle, session_id: str, resume: bool,
    ) -> DeliveryPreparation:
        directory = self._task_dir(preview_id)
        with FileLock(str(directory) + ".lock", timeout=10):
            record = self._load(directory)
            if record is None or record.bundle_payload != json.loads(json.dumps(bundle.to_payload())):
                raise ValueError("Delivery scope differs from the immutable approved task")
            issue = bundle.issue_context
            if issue is None or not any(source.repository == issue.repository and source.repository_id == issue.repository_id
                                        and str(source.path.resolve()) == record.source_path for source in self._sources):
                raise ValueError("Native delivery requires its operator-configured target source")
            budget = DeveloperTaskBudget(
                path=self._state_dir / "budgets" / (sha256(preview_id.encode()).hexdigest() + ".json"),
                bundle=bundle, create=False,
            )
            if record.state == "implementing":
                budget.abort()
                self._save(directory, replace(record, state="failed", error="Interrupted native implementation; automatic replay is blocked",
                                              updated_at=datetime.now(tz=UTC).isoformat()))
                raise ValueError("Interrupted native implementation; inspect retained artifacts")
            if record.state not in {"prepared", "awaiting_tool_approval"}:
                raise ValueError("Delivery is not ready for native implementation; automatic replay is blocked")
            if record.session_id is not None and record.session_id != session_id:
                raise ValueError("Delivery is bound to another native session")
            if resume != (record.state == "awaiting_tool_approval"):
                raise ValueError("Delivery requires its saved approval continuation")
            try:
                budget.remaining_seconds()
                self._verify_checkout(directory, record, budget, pristine=record.state == "prepared")
                record = replace(record, state="implementing", session_id=session_id, updated_at=datetime.now(tz=UTC).isoformat())
                self._save(directory, record)
                return record
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                budget.abort()
                self._save(directory, replace(record, state="failed", error=str(error), updated_at=datetime.now(tz=UTC).isoformat()))
                raise

    def finish_implementation(self, preview_id: str, *, session_id: str, pending: bool = False, error: str | None = None) -> None:
        directory = self._task_dir(preview_id)
        with FileLock(str(directory) + ".lock", timeout=10):
            record = self._load(directory)
            if record is None or record.session_id != session_id:
                raise ValueError("Native delivery session identity is invalid")
            if record.state != "implementing":
                return
            budget = None
            try:
                budget = DeveloperTaskBudget(
                    path=self._state_dir / "budgets" / (sha256(preview_id.encode()).hexdigest() + ".json"),
                    bundle=developer_task_bundle_from_payload(record.bundle_payload), create=False,
                )
                if error is None:
                    budget.remaining_seconds()
            except (ValueError, OSError) as unavailable:
                error = error or str(unavailable)
            if error is not None and budget is not None:
                budget.abort()
            state = "failed" if error is not None else "awaiting_tool_approval" if pending else "implemented"
            self._save(directory, replace(record, state=state, error=error, updated_at=datetime.now(tz=UTC).isoformat()))

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
        self._validate_verification_commands(list(matches[0].verification_commands), bundle)
        directory = self._task_dir(preview_id)
        directory.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(directory) + ".lock", timeout=10):
            record = self._load(directory)
            if record is not None:
                if (record.bundle_payload != preview.bundle_payload or record.source_path != str(source_path)
                    or record.verification_commands != list(matches[0].verification_commands)):
                    raise ValueError("Delivery identity/source differs from the immutable approved task")
                if record.state in {"failed", "retired"}:
                    return record
                if record.state in {"implementing", "awaiting_tool_approval", "implemented", "verifying", "verified", "publishing", "published"}:
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
                    verification_commands=list(matches[0].verification_commands),
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

    @staticmethod
    def _validate_verification_commands(commands: Any, bundle: DeveloperTaskBundle) -> None:
        if (not isinstance(commands, list) or len(commands) > 16
                or not all(isinstance(command, str) and command.strip() and "\x00" not in command
                           and len(command.encode("utf-8")) <= 8192 and is_command_allowed(command, policy=bundle.policy)
                           for command in commands)):
            raise ValueError("Verification commands must be bounded strings allowed by the approved task policy")

    def verify(self, preview_id: str, *, adapter: ContainerSessionBashAdapter) -> DeliveryPreparation:
        if not isinstance(adapter, ContainerSessionBashAdapter):
            raise ValueError("Independent verification requires constrained Docker execution")
        directory = self._task_dir(preview_id)
        with self.implementation_lock(preview_id):
            with FileLock(str(directory) + ".lock", timeout=10):
                record = self._load(directory)
            if record is None:
                raise ValueError("Delivery not found")
            if record.state == "failed":
                return record
            if record.state not in {"implemented", "verifying", "verified"}:
                raise ValueError("Independent verification requires completed native implementation")
            bundle = developer_task_bundle_from_payload(record.bundle_payload)
            issue = bundle.issue_context
            if issue is None or not any(
                source.repository == issue.repository and source.repository_id == issue.repository_id
                and str(source.path.resolve()) == record.source_path
                and list(source.verification_commands) == record.verification_commands for source in self._sources
            ):
                raise ValueError("Verification target/plan differs from the operator-configured preparation")
            if not record.verification_commands:
                raise ValueError("No verification plan was pinned at preparation; a new approved task is required")
            budget = None
            error = None
            created = False
            session_id = record.verification["session_id"] if record.state == "verifying" and record.verification else None
            try:
                budget = DeveloperTaskBudget(
                    path=self._state_dir / "budgets" / (sha256(preview_id.encode()).hexdigest() + ".json"),
                    bundle=bundle, create=False,
                )
                if record.state == "verifying":
                    raise RuntimeError("Interrupted independent verification; automatic replay is blocked")
                budget.remaining_seconds()
                self._verify_checkout(directory, record, budget, pristine=False)
                digest = self._checkout_digest(Path(record.checkout_path), budget)
                if record.state == "verified":
                    if record.verification is None or record.verification["checkout_digest"] != digest:
                        raise ValueError("Verified checkout changed; publication is blocked")
                    return record
                changed = set(self._git(directory, budget, "-C", record.checkout_path, "diff", "--no-ext-diff", "--no-textconv",
                                        "--no-renames", "--name-only", "-z", record.base_revision, "--").split("\x00"))
                changed.update(self._git(directory, budget, "-C", record.checkout_path, "ls-files", "--others", "-z").split("\x00"))
                changed.discard("")
                reservations = set(json.loads(budget.path.read_text())["reserved_paths"])
                if not changed or not changed <= reservations or not all(is_path_allowed(path, policy=bundle.policy) for path in changed):
                    raise ValueError("Target changes must be nonempty, within approved paths and reserved file budgets")
                session_id = "verify-" + uuid4().hex
                evidence: dict[str, Any] = {
                    "session_id": session_id, "started_at": datetime.now(tz=UTC).isoformat(),
                    "checkout_digest": digest, "commands": [], "cleanup_succeeded": False,
                }
                record = replace(record, state="verifying", verification=evidence, updated_at=datetime.now(tz=UTC).isoformat())
                self._save(directory, record)
                adapter.bind_session_workspace(session_id=session_id, workspace=Path(record.checkout_path))
                adapter.create_session(session_id=session_id, read_only_workspace=True,
                                       deadline=datetime.now(tz=UTC).timestamp() + budget.remaining_seconds())
                created = True
                for index, command in enumerate(record.verification_commands):
                    result = self._run_verification_command(directory, budget, adapter, session_id, command, index)
                    evidence["commands"].append(result)
                    self._save(directory, record)
                    if type(result.get("exit_code")) is not int or result["exit_code"] != 0 or result.get("error"):
                        raise RuntimeError(f"Verification command failed: {result.get('error') or result.get('exit_code')}")
                budget.remaining_seconds()
                self._verify_checkout(directory, record, budget, pristine=False)
                if self._checkout_digest(Path(record.checkout_path), budget) != digest:
                    raise ValueError("Target checkout changed during verification; publication is blocked")
            except BaseException as failure:
                error = str(failure) or type(failure).__name__
                if not isinstance(failure, Exception):
                    if budget is not None:
                        budget.abort()
                    self._save(directory, replace(record, state="failed", error=error, updated_at=datetime.now(tz=UTC).isoformat()))
                    raise
            finally:
                if session_id is not None:
                    try:
                        closed = adapter.close_session(session_id=session_id)
                        if created and not closed:
                            raise RuntimeError("Verification container cleanup failed")
                        if record.verification is not None:
                            record.verification["cleanup_succeeded"] = closed
                    except Exception as cleanup_error:
                        error = error or f"Verification cleanup failed: {cleanup_error}"
            if error is not None:
                if budget is not None:
                    budget.abort()
                record = replace(record, state="failed", error=error, updated_at=datetime.now(tz=UTC).isoformat())
            else:
                if budget is not None:
                    try:
                        budget.remaining_seconds()
                    except TimeoutError as expired:
                        budget.abort()
                        error = str(expired)
                if record.verification is not None:
                    record.verification["completed_at"] = datetime.now(tz=UTC).isoformat()
                record = replace(record, state="failed" if error else "verified", error=error, updated_at=datetime.now(tz=UTC).isoformat())
            self._save(directory, record)
            return record


    def publish(
        self,
        preview_id: str,
        *,
        github: GitHubAdapter,
        require_human_approval_for_repo_writes: bool,
        allow_mock_publication: bool = False,
    ) -> DeliveryPreparation:
        """Publish a verified delivery as a draft PR bound to the approved scope."""
        if isinstance(github, MockGitHubAdapter) and not allow_mock_publication:
            raise ValueError(
                "Publication requires a live GitHub adapter; mock publication is refused"
            )
        _refuse_correction_publication(self._previews.get(preview_id))
        directory = self._task_dir(preview_id)
        with self.implementation_lock(preview_id):
            with FileLock(str(directory) + ".lock", timeout=10):
                record = self._load(directory)
            if record is None:
                raise ValueError("Delivery not found")
            _refuse_correction_publication(record)
            if record.state == "failed":
                return record
            if record.state == "published":
                return record
            if record.state not in {"verified", "publishing"}:
                raise ValueError("Publication requires a verified delivery")
            preview = self._previews.get(preview_id)
            if preview is None or not preview.approved or preview.approved_at is None:
                raise ValueError("Publication requires an existing human approval")
            if preview.bundle_payload != record.bundle_payload:
                raise ValueError("Delivery scope differs from the immutable approved task")
            bundle = developer_task_bundle_from_payload(record.bundle_payload)
            issue = bundle.issue_context
            if issue is None or issue.base_revision != record.base_revision:
                raise ValueError("Publication requires the approved repository issue identity")
            if not any(
                source.repository == issue.repository
                and source.repository_id == issue.repository_id
                and str(source.path.resolve()) == record.source_path
                for source in self._sources
            ):
                raise ValueError("Publication requires its operator-configured target source")
            if record.verification is None or record.verification.get("cleanup_succeeded") is not True:
                raise ValueError("Publication requires successful verification evidence")
            budget = DeveloperTaskBudget(
                path=self._state_dir / "budgets" / (sha256(preview_id.encode()).hexdigest() + ".json"),
                bundle=bundle,
                create=False,
            )
            error: str | None = None
            try:
                budget.remaining_seconds()
                files, digest, tree_fingerprint = self._capture_publication(directory, record, bundle, budget)
                prior = record.publication if isinstance(record.publication, dict) else {}
                if prior.get("tree_fingerprint") not in (None, tree_fingerprint):
                    raise ValueError("Publication tree fingerprint changed; renew verification")
                title, body, commit_message = self._publication_metadata(bundle, record)
                base_ref = issue.base_branch
                publication = {
                    **{key: prior[key] for key in ("pull_number", "html_url", "head_sha") if key in prior},
                    "title": title,
                    "body": body,
                    "commit_message": commit_message,
                    "checkout_digest": digest,
                    "tree_fingerprint": tree_fingerprint,
                    "changed_paths": sorted(files),
                    "blob_shas": {
                        path: None if change is None else change.blob_sha
                        for path, change in sorted(files.items())
                    },
                    "file_modes": {
                        path: None if change is None else change.mode
                        for path, change in sorted(files.items())
                    },
                    "base_ref": base_ref,
                    "base_sha": record.base_revision,
                    "branch": record.branch,
                    "repository": issue.repository,
                    "issue_number": issue.issue_number,
                }
                record = replace(
                    record, state="publishing", publication=publication, error=None,
                    updated_at=datetime.now(tz=UTC).isoformat(),
                )
                self._save(directory, record)
                head_sha = publication.get("head_sha")
                reuse_head = (
                    isinstance(head_sha, str)
                    and re.fullmatch(r"[0-9a-f]{40}", head_sha) is not None
                    and prior.get("tree_fingerprint") == tree_fingerprint
                )
                if not reuse_head:
                    head_sha = github.upsert_branch_commit(
                        role=AgentRole.DEVELOPER,
                        repository=issue.repository,
                        branch=record.branch,
                        base_sha=record.base_revision,
                        commit_message=commit_message,
                        files=files,
                        approved=True,
                        require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
                        before_write=budget.remaining_seconds,
                    )
                    if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
                        raise RuntimeError("GitHub adapter returned an invalid head SHA")
                    publication = {**publication, "head_sha": head_sha}
                    record = replace(
                        record, publication=publication,
                        updated_at=datetime.now(tz=UTC).isoformat(),
                    )
                    self._save(directory, record)
                existing = publication.get("pull_number")
                if type(existing) is not int:
                    existing = None
                budget.remaining_seconds()
                pull = github.create_or_update_draft_pull_request(
                    role=AgentRole.DEVELOPER,
                    repository=issue.repository,
                    title=title,
                    body=body,
                    head_branch=record.branch,
                    base_ref=base_ref,
                    issue_number=issue.issue_number,
                    existing_pull_number=existing,
                    approved=True,
                    require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
                    before_write=budget.remaining_seconds,
                )
                publication = {
                    **publication,
                    "head_sha": head_sha,
                    "pull_number": pull.number,
                    "html_url": pull.html_url,
                    "draft": pull.draft,
                    "pull_head_sha": pull.head_sha,
                }
                record = replace(
                    record, state="publishing", publication=publication,
                    updated_at=datetime.now(tz=UTC).isoformat(),
                )
                self._save(directory, record)
                budget.remaining_seconds()
                if (not pull.draft or pull.state != "open" or pull.head_sha != head_sha
                        or pull.head_ref != record.branch or pull.base_ref != base_ref
                        or pull.repository != issue.repository):
                    raise RuntimeError("Publication requires an open draft PR at the verified head and target")
                publication = {**publication, "published_at": datetime.now(tz=UTC).isoformat()}
                record = replace(
                    record, state="published", head_revision=str(head_sha),
                    publication=publication, error=None,
                    updated_at=datetime.now(tz=UTC).isoformat(),
                )
            except BaseException as failure:
                error = str(failure) or type(failure).__name__
                if not isinstance(failure, Exception):
                    budget.abort()
                    self._save(
                        directory,
                        replace(
                            record, state="failed", error=error,
                            updated_at=datetime.now(tz=UTC).isoformat(),
                        ),
                    )
                    raise
            if error is not None:
                budget.abort()
                record = replace(
                    record, state="failed", error=error,
                    updated_at=datetime.now(tz=UTC).isoformat(),
                )
            self._save(directory, record)
            return record

    def _capture_publication(
        self, directory: Path, record: DeliveryPreparation, bundle: DeveloperTaskBundle, budget: DeveloperTaskBudget,
    ) -> tuple[dict[str, GitHubBlobChange | None], str, str]:
        """Capture the verified, scoped checkout changes exactly as they would be pushed."""
        budget.remaining_seconds()
        self._verify_checkout(directory, record, budget, pristine=False)
        changed = set(
            self._git(
                directory, budget, "-C", record.checkout_path,
                "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                "--name-only", "-z", record.base_revision, "--",
            ).split("\x00")
        )
        changed.update(
            self._git(
                directory, budget, "-C", record.checkout_path,
                "ls-files", "--others", "-z",
            ).split("\x00")
        )
        changed.discard("")
        reservations = set(json.loads(budget.path.read_text())["reserved_paths"])
        if (
            not changed
            or not changed <= reservations
            or not all(is_path_allowed(path, policy=bundle.policy) for path in changed)
        ):
            raise ValueError(
                "Publication changes must be nonempty, within approved paths and reserved file budgets"
            )
        files: dict[str, GitHubBlobChange | None] = {relative: None for relative in sorted(changed)}
        checkout = Path(record.checkout_path)
        digest = self._checkout_digest(checkout, budget, capture=files)
        if record.verification is None or digest != record.verification["checkout_digest"]:
            raise ValueError("Verified checkout changed; publication is blocked")
        tree_fingerprint = _tree_fingerprint(files)
        return files, digest, tree_fingerprint

    # ---- Same-PR correction publication -------------------------------------------------

    def _correction_link(self, record: DeliveryPreparation) -> tuple[str, str]:
        """Return (target delivery preview id, triggering review preview id) for a correction record."""
        preview = self._previews.get(record.preview_id)
        source = preview.source_payload if preview is not None else None
        target = source.get("published_review") if isinstance(source, dict) else None
        review_id = source.get("correction_from_review") if isinstance(source, dict) else None
        if (preview is None or preview.bundle_payload != record.bundle_payload or not isinstance(target, dict)
                or not isinstance(target.get("preview_id"), str) or not target["preview_id"]
                or target["preview_id"] == record.preview_id
                or target.get("head_sha") != record.base_revision
                or not isinstance(review_id, str) or not review_id):
            raise PermissionError("Correction chain link is missing or invalid; publication is refused")
        return target["preview_id"], review_id

    def _load_preview_record(self, preview_id: str) -> DeliveryPreparation | None:
        directory = self._task_dir(preview_id)
        with FileLock(str(directory) + ".lock", timeout=10):
            return self._load(directory)

    def correction_target(self, preview_id: str) -> dict[str, Any]:
        """Walk the correction chain back to the original publication, failing closed on any mismatch."""
        try:
            record = self._load_preview_record(preview_id)
        except ValueError as error:
            raise PermissionError("Correction delivery failed validation") from error
        if record is None or not _is_correction_record(record):
            raise PermissionError("Same-PR publication requires a scoped correction delivery")
        issue = developer_task_bundle_from_payload(record.bundle_payload).issue_context
        if issue is None:
            raise PermissionError("Correction delivery lacks repository identity")
        target_id, review_id = self._correction_link(record)
        expected_head = record.base_revision
        current_id = target_id
        pull_number: int | None = None
        base_ref: str | None = None
        seen = {preview_id}
        chain: list[str] = []
        while True:
            if current_id in seen or len(seen) > 64:
                raise PermissionError("Correction chain is cyclic or too long")
            seen.add(current_id)
            try:
                link = self._load_preview_record(current_id)
            except ValueError as error:
                raise PermissionError("Correction chain link failed validation") from error
            if link is None or link.state != "published" or not isinstance(link.publication, dict):
                raise PermissionError("Correction chain link is missing or unpublished")
            publication = link.publication
            link_issue = developer_task_bundle_from_payload(link.bundle_payload).issue_context
            if (link_issue is None or publication.get("head_sha") != expected_head
                    or publication.get("repository") != issue.repository
                    or link_issue.repository != issue.repository or link_issue.repository_id != issue.repository_id
                    or publication.get("branch") != issue.base_branch
                    or pull_number is not None and publication.get("pull_number") != pull_number
                    or base_ref is not None and publication.get("base_ref") != base_ref):
                raise PermissionError("Correction chain link repository/PR/branch/head mismatch")
            pull_number, base_ref = publication["pull_number"], publication["base_ref"]
            chain.append(current_id)
            if not _is_correction_record(link):
                break
            if publication.get("mode") != "advance" or publication.get("parent_head_sha") != link.base_revision:
                raise PermissionError("Correction chain link lacks its pinned parent head")
            expected_head = link.base_revision
            current_id, _ = self._correction_link(link)
        if type(pull_number) is not int or not isinstance(base_ref, str):
            raise PermissionError("Correction chain lacks a pinned pull request")
        return {"repository": issue.repository, "pull_number": pull_number, "head_branch": issue.base_branch,
                "base_ref": base_ref, "parent_head_sha": record.base_revision, "target_preview_id": target_id,
                "review_preview_id": review_id, "chain": chain}

    def _correction_records(self, relevant: Callable[[dict[str, Any]], bool]) -> list[DeliveryPreparation]:
        """Correction records a check depends on; only those (or unreadable files) fail the check closed.

        `relevant` sees the raw persisted record, so one corrupt record about another PR or parent
        head cannot block unrelated reviews, while anything that might belong to this chain does.
        """
        root = self._state_dir / "deliveries"
        records = []
        for state in sorted(root.glob("*/state.json")) if root.exists() else ():
            try:
                raw = json.loads(state.read_text(encoding="utf-8"))["record"]
                if not isinstance(raw, dict):
                    raise ValueError("Invalid persisted delivery preparation")
            except (ValueError, KeyError, TypeError, OSError) as error:
                raise PermissionError("An unreadable delivery record blocks correction checks; they fail closed") from error
            if not str(raw.get("task_id", "")).startswith(CORRECTION_TASK_PREFIX) or not relevant(raw):
                continue
            try:
                record = self._load(state.parent)
            except ValueError as error:
                raise PermissionError("A related correction record failed validation; correction checks fail closed") from error
            if record is not None and _is_correction_record(record):
                records.append(record)
        return records

    @staticmethod
    def correction_holds_parent(record: DeliveryPreparation | None) -> bool:
        """Whether a correction still claims its parent head.

        Retired corrections and failed ones whose push is known not to have landed release it;
        a failed push whose outcome is unknown or that pushed keeps holding the head.
        """
        if record is None:
            return True
        if record.state == "retired":
            return False
        if record.state == "failed":
            publication = record.publication
            return isinstance(publication, dict) and publication.get("mode") == "advance" and \
                publication.get("push_outcome") != "not_applied"
        return True

    def correction_releases_parent(self, preview_id: str | None) -> bool:
        """Offer-time sibling check: True only when a saved correction delivery provably released its head."""
        if preview_id is None:
            return False
        try:
            record = self._load_preview_record(preview_id)
        except ValueError as error:
            raise PermissionError("Sibling correction delivery failed validation; offering fails closed") from error
        return record is not None and _is_correction_record(record) and not self.correction_holds_parent(record)

    def competing_corrections(self, preview_id: str, *, repository: str, parent_head_sha: str) -> list[str]:
        """Other corrections on the same parent head that already staged an approval or pushed (first stage wins)."""
        competing = []
        for other in self._correction_records(lambda raw: raw.get("base_revision") == parent_head_sha):
            if other.preview_id == preview_id or other.base_revision != parent_head_sha:
                continue
            issue = developer_task_bundle_from_payload(other.bundle_payload).issue_context
            if issue is None or issue.repository != repository or not self.correction_holds_parent(other):
                continue
            pushed = isinstance(other.publication, dict) and other.publication.get("mode") == "advance"
            approval = self._approvals.get(other.preview_id)
            staged = approval is not None and approval.state != "invalidated" and other.state != "failed"
            if pushed or staged:
                competing.append(other.preview_id)
        return competing

    def superseded_by(self, preview_id: str) -> str | None:
        """Return the chain tip preview id when a correction has advanced past this delivery."""
        try:
            anchor = self._load_preview_record(preview_id)
        except ValueError as error:
            raise PermissionError("Published review target failed validation") from error
        anchor_publication = anchor.publication if anchor is not None and isinstance(anchor.publication, dict) else {}
        repository, pull_number = anchor_publication.get("repository"), anchor_publication.get("pull_number")

        def related(raw: dict[str, Any]) -> bool:
            publication = raw.get("publication")
            return isinstance(publication, dict) and publication.get("mode") == "advance" and (repository is None or (
                publication.get("repository") == repository and publication.get("pull_number") == pull_number))

        current, tip, seen = preview_id, None, {preview_id}
        records = self._correction_records(related)
        while True:
            successors = [
                other.preview_id for other in records
                if other.state in {"publishing", "published"} and isinstance(other.publication, dict)
                and other.publication.get("mode") == "advance" and other.publication.get("target_preview_id") == current
            ]
            if not successors:
                return tip
            if len(successors) > 1:
                raise PermissionError(f"Correction chain forked after {current}; review is refused")
            current = successors[0]
            if current in seen:
                raise PermissionError("Correction chain is cyclic")
            seen.add(current)
            tip = current

    def refuse_superseded(self, preview_id: str) -> None:
        tip = self.superseded_by(preview_id)
        if tip is not None:
            raise PermissionError(f"Published review target {preview_id} is superseded by {tip}; use the chain tip")

    def _correction_diff(
        self, directory: Path, record: DeliveryPreparation, files: dict[str, GitHubBlobChange | None],
        budget: DeveloperTaskBudget,
    ) -> str:
        listing = self._git(directory, budget, "-C", record.checkout_path, "ls-tree", "-z", record.base_revision,
                            "--", *sorted(files)).split("\x00")
        before: dict[str, tuple[str, str]] = {}
        for entry in filter(None, listing):
            meta, path = entry.split("\t", 1)
            mode, kind, blob = meta.split(" ")
            if kind == "blob":
                before[path] = (mode, blob)
        parts: list[str] = []
        for path, change in sorted(files.items()):
            old_mode, old_blob = before.get(path, (None, None))
            header = (f"diff --aitobuild a/{path} b/{path}\nmode {old_mode} -> {change.mode if change else None}\n"
                      f"blob {old_blob} -> {change.blob_sha if change else None}\n")
            try:
                old = self._git(directory, budget, "-C", record.checkout_path, "cat-file", "blob",
                                old_blob) if old_blob else ""
                new = change.content.decode("utf-8") if change is not None else ""
            except UnicodeDecodeError:
                parts.append(header + "Binary content changed\n")
                continue
            lines = unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile=f"a/{path}", tofile=f"b/{path}")
            parts.append(header + "".join(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines))
        return "".join(parts)

    def _correction_snapshot(
        self, preview_id: str, *, review_receipt_digest: str, resuming: bool,
    ) -> tuple[DeliveryPreparation, dict[str, Any], dict[str, GitHubBlobChange | None], DeveloperTaskBudget]:
        if not isinstance(review_receipt_digest, str) or re.fullmatch(r"[0-9a-f]{64}", review_receipt_digest) is None:
            raise PermissionError("Correction publication requires the triggering review receipt digest")
        directory = self._task_dir(preview_id)
        record = self._load_preview_record(preview_id)
        if record is None:
            raise ValueError("Delivery not found")
        if not _is_correction_record(record):
            raise PermissionError("Same-PR publication requires a scoped correction delivery")
        if record.state not in ({"verified", "publishing"} if resuming else {"verified"}):
            raise ValueError("Correction publication requires a verified correction delivery")
        preview = self._previews.get(preview_id)
        if preview is None or not preview.approved or preview.approved_at is None:
            raise ValueError("Publication requires an existing human approval")
        if preview.bundle_payload != record.bundle_payload:
            raise ValueError("Delivery scope differs from the immutable approved task")
        bundle = developer_task_bundle_from_payload(record.bundle_payload)
        issue = bundle.issue_context
        if issue is None or issue.base_revision != record.base_revision:
            raise ValueError("Publication requires the approved repository issue identity")
        if not any(source.repository == issue.repository and source.repository_id == issue.repository_id
                   and str(source.path.resolve()) == record.source_path for source in self._sources):
            raise ValueError("Publication requires its operator-configured target source")
        if record.verification is None or record.verification.get("cleanup_succeeded") is not True:
            raise ValueError("Publication requires successful verification evidence")
        target = self.correction_target(preview_id)
        budget = DeveloperTaskBudget(path=self.budget_path(preview_id), bundle=bundle, create=False)
        competing = self.competing_corrections(preview_id, repository=issue.repository,
                                               parent_head_sha=record.base_revision)
        tip = self.superseded_by(target["target_preview_id"])
        stale = None
        if competing:
            stale = f"Correction {competing[0]} already uses parent head {record.base_revision}; this correction is stale"
        elif tip is not None and tip != preview_id:
            stale = f"Correction target is superseded by {tip}; this correction is stale"
        if stale is not None:
            if record.state == "verified":
                budget.abort()
                self._save(directory, replace(record, state="failed", error=stale,
                                              updated_at=datetime.now(tz=UTC).isoformat()))
            raise PermissionError(stale)
        files, checkout_digest, fingerprint = self._capture_publication(directory, record, bundle, budget)
        title, body, commit_message = self._publication_metadata(bundle, record)
        diff = self._correction_diff(directory, record, files, budget)
        snapshot = {
            "kind": "same-pr-correction", "preview_id": preview_id, "task_id": record.task_id,
            **{key: target[key] for key in ("repository", "pull_number", "head_branch", "base_ref",
                                            "parent_head_sha", "target_preview_id", "review_preview_id", "chain")},
            "review_receipt_digest": review_receipt_digest, "title": title, "body": body,
            "commit_message": commit_message, "checkout_digest": checkout_digest, "tree_fingerprint": fingerprint,
            "changed_paths": sorted(files),
            "blob_shas": {path: None if change is None else change.blob_sha for path, change in sorted(files.items())},
            "file_modes": {path: None if change is None else change.mode for path, change in sorted(files.items())},
            "diff": diff, "diff_sha256": sha256(diff.encode("utf-8")).hexdigest(),
            "issue_number": issue.issue_number,
        }
        return record, snapshot, files, budget

    def stage_correction_publication(self, preview_id: str, *, review_receipt_digest: str) -> dict[str, Any]:
        """Stage one exact same-PR push snapshot for a single-use operator approval."""
        with self.implementation_lock(preview_id), FileLock(str(self._state_dir / "corrections.lock"), timeout=30):
            _, snapshot, _, _ = self._correction_snapshot(
                preview_id, review_receipt_digest=review_receipt_digest, resuming=False)
            staged = self._approvals.stage(preview_id, snapshot)
            return {"digest": staged.digest, "content_digest": staged.content_digest, "state": staged.state,
                    "snapshot": staged.snapshot}

    def approve_and_publish_correction(
        self,
        preview_id: str,
        *,
        approval_digest: str,
        review_receipt_digest: str,
        actor_id: str,
        github: GitHubAdapter,
        require_human_approval_for_repo_writes: bool,
        allow_mock_publication: bool = False,
    ) -> DeliveryPreparation:
        """Consume the exact approval and fast-forward the pinned PR head; never creates a PR or changes base."""
        if isinstance(github, MockGitHubAdapter) and not allow_mock_publication:
            raise ValueError("Publication requires a live GitHub adapter; mock publication is refused")
        directory = self._task_dir(preview_id)
        with self.implementation_lock(preview_id), FileLock(str(self._state_dir / "corrections.lock"), timeout=30):
            current = self._load_preview_record(preview_id)
            if current is not None and current.state == "published":
                approval = self._approvals.get(preview_id)
                if approval is None or approval.digest != approval_digest or approval.state != "consumed":
                    raise PermissionError("Correction was published under a different approval")
                return current
            resuming = current is not None and current.state == "publishing"
            if resuming:
                assert current is not None
                settled = self._resume_by_live_head(
                    directory, current, approval_digest=approval_digest, review_receipt_digest=review_receipt_digest,
                    actor_id=actor_id, github=github,
                    require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
                )
                if settled is not None:
                    return settled
                # The parent head is untouched and the budget is still valid: push again under the same approval.
            record, snapshot, files, budget = self._correction_snapshot(
                preview_id, review_receipt_digest=review_receipt_digest, resuming=resuming)
            self._approvals.begin_consume(preview_id, digest=approval_digest,
                                          recomputed_content_digest=snapshot_digest(snapshot), actor_id=actor_id)
            publication = {
                **{key: snapshot[key] for key in ("title", "body", "commit_message", "checkout_digest",
                                                  "tree_fingerprint", "changed_paths", "blob_shas", "file_modes",
                                                  "base_ref", "repository", "issue_number", "pull_number",
                                                  "parent_head_sha", "target_preview_id", "review_preview_id")},
                "mode": "advance", "branch": snapshot["head_branch"], "base_sha": record.base_revision,
                "approval_digest": approval_digest, "approved_by": actor_id.strip(),
            }
            record = replace(record, state="publishing", publication=publication, error=None,
                             updated_at=datetime.now(tz=UTC).isoformat())
            self._save(directory, record)
            push = {key: snapshot[key] for key in ("repository", "pull_number", "head_branch", "base_ref", "commit_message")}
            try:
                budget.remaining_seconds()
                pull = github.advance_draft_pull_request_head(
                    role=AgentRole.DEVELOPER, expected_head_sha=record.base_revision, files=files, approved=True,
                    before_write=budget.remaining_seconds,
                    require_human_approval_for_repo_writes=require_human_approval_for_repo_writes, **push,
                )
                self._require_advanced_pull(pull, record=record, snapshot=snapshot)
            except BaseException as failure:
                # Decide by the live outcome, never by the exception alone (88720aa convention).
                return self._settle_correction_push(
                    directory, record, failure, approval_digest=approval_digest, budget=budget, github=github,
                    files=files, push=push,
                    require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
                )
            return self._finish_correction_publication(directory, record, pull, approval_digest=approval_digest)

    @staticmethod
    def _require_advanced_pull(pull: Any, *, record: DeliveryPreparation, snapshot: dict[str, Any]) -> str:
        head_sha = (pull.head_sha or "").lower()
        if (re.fullmatch(r"[0-9a-f]{40}", head_sha) is None or head_sha == record.base_revision
                or pull.number != snapshot["pull_number"] or pull.repository != snapshot["repository"]
                or pull.head_ref != snapshot["head_branch"] or pull.base_ref != snapshot["base_ref"]
                or not pull.draft or pull.state != "open"):
            raise RuntimeError("Correction push did not leave the pinned open draft at a new head")
        return head_sha

    def _finish_correction_publication(
        self, directory: Path, record: DeliveryPreparation, pull: Any, *, approval_digest: str,
    ) -> DeliveryPreparation:
        assert record.publication is not None
        head_sha = (pull.head_sha or "").lower()
        publication = {**record.publication, "head_sha": head_sha, "html_url": pull.html_url, "draft": True,
                       "pull_head_sha": head_sha, "published_at": datetime.now(tz=UTC).isoformat()}
        record = replace(record, state="published", head_revision=head_sha, publication=publication,
                         error=None, updated_at=datetime.now(tz=UTC).isoformat())
        self._save(directory, record)
        self._approvals.finish_consume(record.preview_id, digest=approval_digest, head_sha=head_sha)
        return record

    def _resume_by_live_head(
        self, directory: Path, record: DeliveryPreparation, *, approval_digest: str, review_receipt_digest: str,
        actor_id: str, github: GitHubAdapter, require_human_approval_for_repo_writes: bool,
    ) -> DeliveryPreparation | None:
        """Settle an interrupted correction from GitHub first, before any budget or checkout check.

        Uses only the approved snapshot's pinned blob SHAs/modes, so a crash after the ref landed
        reconciles to published even after the budget expired. Returns None only when the parent
        head is untouched and the budget is still valid, meaning the caller may push again.
        """
        approval = self._approvals.get(record.preview_id)
        if approval is None or approval.digest != approval_digest or approval.state != "consuming":
            raise PermissionError("Interrupted correction publication requires its exact in-flight approval")
        if approval.approved_by != actor_id.strip():
            raise PermissionError("Interrupted publish approval belongs to another operator")
        snapshot = approval.snapshot
        publication = record.publication or {}
        if (snapshot.get("review_receipt_digest") != review_receipt_digest
                or snapshot.get("preview_id") != record.preview_id
                or snapshot.get("parent_head_sha") != record.base_revision
                or publication.get("branch") != snapshot.get("head_branch")
                or any(publication.get(key) != snapshot.get(key) for key in (
                    "blob_shas", "file_modes", "commit_message", "repository", "pull_number", "base_ref"))):
            raise PermissionError("Interrupted correction publication differs from its approved snapshot")
        changes = pinned_changes_from_snapshot(snapshot)
        push: dict[str, Any] = {key: snapshot[key] for key in ("repository", "pull_number", "head_branch", "base_ref", "commit_message")}
        outcome, pull = github.reconcile_advanced_head(  # unreadable: raises and the record stays publishing
            role=AgentRole.DEVELOPER, expected_head_sha=record.base_revision, changes=changes, approved=True,
            require_human_approval_for_repo_writes=require_human_approval_for_repo_writes, **push,
        )
        if outcome == "ours":
            self._require_advanced_pull(pull, record=record, snapshot=push)
            return self._finish_correction_publication(directory, record, pull, approval_digest=approval_digest)
        # Only read for terminal bookkeeping: an aborted budget may still be marked aborted again.
        budget = DeveloperTaskBudget(path=self.budget_path(record.preview_id),
                                     bundle=developer_task_bundle_from_payload(record.bundle_payload), create=False,
                                     allow_aborted=True)
        if outcome == "parent":
            try:
                budget.remaining_seconds()
            except Exception as expired:
                error = f"Push did not apply and the approved budget is gone: {expired}"
                self._fail_correction_push(directory, record, "not_applied", pull, error,
                                           approval_digest=approval_digest, budget=budget)
                raise PermissionError(error) from expired
            return None
        error = "Pull request head moved while the correction was interrupted"
        self._fail_correction_push(directory, record, "moved", pull, error, approval_digest=approval_digest, budget=budget)
        raise PermissionError(error)

    def _fail_correction_push(
        self, directory: Path, record: DeliveryPreparation, verdict: str, pull: Any, error: str, *,
        approval_digest: str, budget: DeveloperTaskBudget,
    ) -> None:
        """Terminal push failure: `not_applied` releases the parent head, `moved` keeps holding it."""
        assert record.publication is not None
        budget.abort()
        self._save(directory, replace(
            record, state="failed", error=error,
            publication={**record.publication, "push_outcome": verdict, "live_head_sha": (pull.head_sha or "").lower()},
            updated_at=datetime.now(tz=UTC).isoformat()))
        self._approvals.finish_consume(record.preview_id, digest=approval_digest, head_sha=None, error=error)

    def _settle_correction_push(
        self, directory: Path, record: DeliveryPreparation, failure: BaseException, *, approval_digest: str,
        budget: DeveloperTaskBudget, github: GitHubAdapter, files: dict[str, GitHubBlobChange | None],
        push: dict[str, Any], require_human_approval_for_repo_writes: bool,
    ) -> DeliveryPreparation:
        """Re-read the branch after any failure past begin_consume and settle by what actually happened.

        - head is our exact commit on the pinned open draft: the push landed, reconcile to published;
        - head is still the parent: the push did not apply, fail terminally and release the parent head;
        - head moved elsewhere: fail terminally, keep holding the parent (our commit may be underneath);
        - head cannot be read (or the PR is no longer an open draft at our head): stay publishing,
          resumable only under the same approval and operator.
        The original exception is always re-raised unless the push is reconciled as published.
        """
        assert record.publication is not None
        error = str(failure) or type(failure).__name__
        try:
            outcome, pull = github.reconcile_advanced_head(
                role=AgentRole.DEVELOPER, expected_head_sha=record.base_revision, changes=pinned_changes(files),
                approved=True,
                require_human_approval_for_repo_writes=require_human_approval_for_repo_writes, **push,
            )
            if outcome == "ours":
                self._require_advanced_pull(pull, record=record, snapshot=push)
        except Exception:
            raise failure from None
        if outcome == "ours":
            published = self._finish_correction_publication(directory, record, pull, approval_digest=approval_digest)
            if not isinstance(failure, Exception):
                raise failure  # records now match GitHub; still honour the interrupt or exit
            return published
        self._fail_correction_push(directory, record, "not_applied" if outcome == "parent" else "moved", pull, error,
                                   approval_digest=approval_digest, budget=budget)
        raise failure

    def retire_correction(
        self, preview_id: str, *, actor_id: str, github: GitHubAdapter, allow_mock_publication: bool = False,
    ) -> DeliveryPreparation:
        """Operator retire for an abandoned or failed correction so its parent head can take a new one.

        Only allowed while the live PR head still equals this correction's parent head, and never for
        a correction that is publishing or published (those must reconcile instead).
        """
        if isinstance(github, MockGitHubAdapter) and not allow_mock_publication:
            raise ValueError("Retiring a correction requires a live GitHub adapter; mock reads are refused")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise PermissionError("Retiring a correction requires an operator identity")
        directory = self._task_dir(preview_id)
        with self.implementation_lock(preview_id), FileLock(str(self._state_dir / "corrections.lock"), timeout=30):
            try:
                record = self._load_preview_record(preview_id)
            except ValueError as error:
                raise PermissionError("Correction delivery failed validation") from error
            if record is None or not _is_correction_record(record):
                raise PermissionError("Retiring requires a saved correction delivery")
            if record.state == "retired":
                return record
            if record.state not in _RETIRABLE_STATES:
                raise PermissionError(f"A {record.state} correction cannot be retired; reconcile or wait for it instead")
            target = self.correction_target(preview_id)
            pull = github.get_pull_request(repository=target["repository"], pull_number=target["pull_number"])
            live_head = (pull.head_sha or "").lower()
            if pull.head_ref != target["head_branch"] or live_head != record.base_revision:
                raise PermissionError("Retiring requires the live PR head to equal this correction's parent head")
            if record.state != "failed":
                self._approvals.invalidate(preview_id, reason="retired")
                budget_path = self.budget_path(preview_id)
                if budget_path.exists():
                    bundle = developer_task_bundle_from_payload(record.bundle_payload)
                    DeveloperTaskBudget(path=budget_path, bundle=bundle, create=False).abort()
            now = datetime.now(tz=UTC).isoformat()
            record = replace(record, state="retired", error=record.error or "Retired by operator",
                             retirement={"actor_id": actor_id.strip(), "retired_at": now,
                                         "previous_state": record.state, "live_head_sha": live_head},
                             updated_at=now)
            self._save(directory, record)
            return record

    def get_published_pull_request(
        self, preview_id: str, *, github: GitHubAdapter,
    ) -> dict[str, Any]:
        """Resolve a published draft PR from delivery publication only."""
        self.refuse_superseded(preview_id)
        directory = self._task_dir(preview_id)
        with FileLock(str(directory) + ".lock", timeout=10):
            record = self._load(directory)
            if record is None:
                raise ValueError("Delivery not found")
            publication = dict(self._require_published_publication(record))
            preview = record.preview_id
            prior_review = dict(record.architect_review) if record.architect_review else None
        pull = github.get_pull_request(
            repository=str(publication["repository"]),
            pull_number=int(publication["pull_number"]),
        )
        expected_head = str(publication["head_sha"]).lower()
        live_head = (pull.head_sha or "").lower() or None
        head_matches = live_head == expected_head
        if (pull.repository != publication["repository"] or pull.number != publication["pull_number"] or
                pull.base_ref != publication["base_ref"]):
            raise PermissionError("Published pull request repository/number/base differs from the pinned target")
        if pull.head_ref != publication["branch"]:
            raise ValueError("Published pull request head ref does not match the delivery branch")
        if not pull.draft or pull.state != "open":
            raise ValueError("Published pull request must remain an open draft")
        payload = pull.to_dict()
        payload["preview_id"] = preview
        payload["expected_head_sha"] = expected_head
        payload["head_matches_publication"] = head_matches
        if prior_review is not None:
            payload["architect_review"] = prior_review
        return payload

    def get_published_source(
        self, preview_id: str, *, github: GitHubAdapter, path: str, offset: int = 0,
        max_bytes: int = 24000,
    ) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(max_bytes) is not int or not 1 <= max_bytes <= 24000:
            raise ValueError("Review paging must use bounded integer byte offsets/limits")
        pull = self.get_published_pull_request(preview_id, github=github)
        if not pull["head_matches_publication"]:
            raise ValueError("Published draft head SHA no longer matches the review target")
        directory = self._task_dir(preview_id)
        with FileLock(str(directory) + ".lock", timeout=10):
            record = self._load(directory)
            if record is None:
                raise ValueError("Delivery not found")
            publication = dict(self._require_published_publication(record))
            bundle = developer_task_bundle_from_payload(record.bundle_payload)
            if (bundle.issue_context is None or publication["repository"] != bundle.issue_context.repository or
                    publication["head_sha"] != pull["expected_head_sha"] or
                    path not in publication["changed_paths"] or not is_path_allowed(path, policy=bundle.policy)):
                raise PermissionError("Review source must belong to the exact approved published changes")
            blob_sha = publication.get("blob_shas", {}).get(path)
            mode = publication.get("file_modes", {}).get(path)
            if path not in publication.get("blob_shas", {}) or path not in publication.get("file_modes", {}):
                raise ValueError("Review source lacks its immutable blob/mode pins")
        content = b"" if blob_sha is None and mode is None else github.get_blob(
            repository=publication["repository"], blob_sha=blob_sha,
        )
        if blob_sha is not None and (_git_blob_sha(content) != blob_sha or mode not in {"100644", "100755"}):
            raise ValueError("Review source hash/mode differs from the publication snapshot")
        if len(content) > 1048576 or offset > len(content):
            raise ValueError("Review source exceeds size/offset bounds")
        content.decode("utf-8")
        page = content[offset:offset + max_bytes].decode("utf-8", errors="ignore")
        consumed = len(page.encode("utf-8"))
        if offset < len(content) and (not consumed or content[offset] & 0xC0 == 0x80):
            raise ValueError("Review byte page must end/advance on a UTF-8 boundary")
        current = self.get_published_pull_request(preview_id, github=github)
        if not current["head_matches_publication"] or current["expected_head_sha"] != publication["head_sha"]:
            raise ValueError("Published draft head SHA changed during source inspection")
        truncated = offset + consumed < len(content)
        return {"preview_id": preview_id, "repository": publication["repository"], "head_sha": publication["head_sha"],
                "base_sha": publication["base_sha"], "path": path, "blob_sha": blob_sha, "mode": mode,
                "deleted": blob_sha is None, "content": page, "offset": offset, "total_bytes": len(content),
                "truncated": truncated, "next_offset": offset + consumed if truncated else None}

    def get_published_diff(
        self, preview_id: str, *, github: GitHubAdapter, path: str, offset: int = 0,
        max_bytes: int = 24000,
    ) -> dict[str, Any]:
        source = self.get_published_source(preview_id, github=github, path=path)
        before = github.get_file_at_commit(repository=source["repository"], commit_sha=source["base_sha"], path=path)
        if before is not None and (before.mode not in {"100644", "100755"} or len(before.content) > 1048576 or
                                   _git_blob_sha(before.content) != before.blob_sha):
            raise ValueError("Review base blob identity/size is invalid")
        old = before.content.decode("utf-8") if before else ""
        after = github.get_blob(repository=source["repository"], blob_sha=source["blob_sha"]) if not source["deleted"] else b""
        if len(after) > 1048576 or not source["deleted"] and _git_blob_sha(after) != source["blob_sha"]:
            raise ValueError("Review head blob identity/size is invalid")
        new = after.decode("utf-8")
        lines = unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                             fromfile=f"a/{path}", tofile=f"b/{path}")
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines).encode("utf-8")
        if type(offset) is not int or not 0 <= offset <= len(diff) or type(max_bytes) is not int or not 1 <= max_bytes <= 24000:
            raise ValueError("Review diff paging requires bounded integer byte offsets/limits")
        page = diff[offset:offset + max_bytes].decode("utf-8", errors="ignore")
        consumed = len(page.encode("utf-8"))
        if offset < len(diff) and (not consumed or diff[offset] & 0xC0 == 0x80):
            raise ValueError("Review diff byte page must advance on a UTF-8 boundary")
        current = self.get_published_pull_request(preview_id, github=github)
        if not current["head_matches_publication"] or current["expected_head_sha"] != source["head_sha"]:
            raise ValueError("Published draft head SHA changed during diff inspection")
        truncated = offset + consumed < len(diff)
        return {**{key: source[key] for key in ("preview_id", "repository", "head_sha", "base_sha", "path")},
                "before_blob_sha": before.blob_sha if before else None, "after_blob_sha": source["blob_sha"],
                "before_mode": before.mode if before else None, "after_mode": source["mode"],
                "diff": page, "offset": offset, "total_bytes": len(diff), "truncated": truncated,
                "next_offset": offset + consumed if truncated else None}

    def submit_architect_review(
        self,
        preview_id: str,
        *,
        github: GitHubAdapter,
        event: str,
        body: str,
        expected_target: dict[str, Any] | None = None,
        before_submit: Callable[[], Any] | None = None,
    ) -> DeliveryPreparation:
        """Submit COMMENT against the published draft; persist last review.

        REQUEST_CHANGES stays disabled until a distinct reviewer GitHub identity
        is configured (same-token self-reviews 422 on GitHub).
        """
        self.refuse_superseded(preview_id)
        directory = self._task_dir(preview_id)
        with FileLock(str(directory) + ".lock", timeout=10):
            record = self._load(directory)
            if record is None:
                raise ValueError("Delivery not found")
            publication = dict(self._require_published_publication(record))
            if isinstance(record.error, str) and record.error.startswith(
                    _UNBOUND_REVIEW_PREFIX + str(publication["head_sha"]).lower()):
                raise PermissionError(record.error)
            if expected_target is not None and expected_target != {
                "preview_id": record.preview_id,
                **{key: publication.get(key) for key in ("head_sha", "repository", "pull_number", "base_sha",
                                                       "changed_paths", "blob_shas", "file_modes")},
            }:
                raise PermissionError("Architect approved target changed before COMMENT submission")
        cleaned_event = str(event).strip().upper()
        if cleaned_event == "REQUEST_CHANGES":
            raise ValueError(
                "REQUEST_CHANGES requires a distinct Architect reviewer GitHub identity; "
                "this slice allows COMMENT only to avoid same-token self-review failures"
            )
        if cleaned_event not in _ARCHITECT_REVIEW_EVENTS:
            raise ValueError("Published-draft Architect review allows only COMMENT")
        review_body = self._normalize_architect_review_body(body)
        expected_head = str(publication["head_sha"]).lower()
        pull = github.get_pull_request(
            repository=str(publication["repository"]),
            pull_number=int(publication["pull_number"]),
        )
        live_head = (pull.head_sha or "").lower()
        if (pull.repository != publication["repository"] or pull.number != publication["pull_number"] or
                pull.base_ref != publication["base_ref"]):
            raise PermissionError("Published pull request repository/number/base differs from the pinned target")
        if live_head != expected_head:
            raise ValueError(
                "Published draft head SHA no longer matches delivery publication; "
                "call architect_get_published_pr and retry only if the delivery was republished"
            )
        if pull.head_ref != publication["branch"]:
            raise ValueError("Published pull request head ref does not match the delivery branch")
        if not pull.draft or pull.state != "open":
            raise ValueError("Published pull request must remain an open draft")
        if before_submit is not None:
            before_submit()
        review = github.submit_pr_review(
            role=AgentRole.ARCHITECT,
            repository=str(publication["repository"]),
            pull_number=int(publication["pull_number"]),
            event="COMMENT",
            body=review_body,
            commit_id=expected_head,
        )
        bound_commit = (review.commit_id or "").lower()
        if bound_commit != expected_head:
            # The COMMENT is already on GitHub: persist that fact so a retry refuses instead of reposting.
            unbound = (f"{_UNBOUND_REVIEW_PREFIX}{expected_head}: review {review.review_id} was bound to "
                       f"{bound_commit or 'no commit'}; inspect the pull request before any retry")
            with FileLock(str(directory) + ".lock", timeout=10):
                record = self._load(directory)
                if record is not None and record.state == "published":
                    self._save(directory, replace(record, error=unbound, updated_at=datetime.now(tz=UTC).isoformat()))
            raise RuntimeError("GitHub bound the Architect review to a different commit than the pinned head")
        architect_review = {
            "event": "COMMENT",
            "body": review_body,
            "review_id": review.review_id,
            "html_url": review.html_url,
            "head_sha": expected_head,
            "commit_id": bound_commit,
            "pull_number": int(publication["pull_number"]),
            "repository": str(publication["repository"]),
            "reviewed_at": datetime.now(tz=UTC).isoformat(),
        }
        with FileLock(str(directory) + ".lock", timeout=10):
            record = self._load(directory)
            if record is None:
                raise ValueError("Delivery not found")
            current = self._require_published_publication(record)
            if str(current["head_sha"]).lower() != expected_head:
                raise ValueError(
                    "Published draft head SHA changed while submitting the review; "
                    "call architect_get_published_pr after republication"
                )
            if int(current["pull_number"]) != int(publication["pull_number"]):
                raise ValueError("Published pull request identity changed during review submit")
            record = replace(
                record,
                architect_review=architect_review,
                updated_at=datetime.now(tz=UTC).isoformat(),
                error=None,
            )
            self._save(directory, record)
            return record

    @staticmethod
    def _normalize_architect_review_body(body: str) -> str:
        cleaned = str(body).replace("\x00", "").strip()
        if not cleaned:
            raise ValueError("Architect review body must be non-empty")
        max_content = ARCHITECT_REVIEW_BODY_MAX - len(ARCHITECT_REVIEW_BODY_PREFIX)
        if max_content <= 0 or len(cleaned) > max_content:
            raise ValueError(
                f"Architect review body exceeds {max_content} characters"
            )
        return f"{ARCHITECT_REVIEW_BODY_PREFIX}{cleaned}"

    @staticmethod
    def _require_published_publication(record: DeliveryPreparation) -> dict[str, Any]:
        if record.state != "published":
            raise ValueError("Architect review requires a published delivery")
        publication = record.publication
        if not isinstance(publication, dict):
            raise ValueError("Published delivery lacks publication identity")
        for key in ("repository", "branch", "head_sha"):
            if not isinstance(publication.get(key), str) or not publication[key]:
                raise ValueError("Published delivery lacks publication identity")
        if type(publication.get("pull_number")) is not int or publication["pull_number"] <= 0:
            raise ValueError("Published delivery lacks a draft pull request number")
        if publication.get("draft") is not True:
            raise ValueError("Published delivery must record a draft pull request")
        if re.fullmatch(r"[0-9a-f]{40}", str(publication["head_sha"]).lower()) is None:
            raise ValueError("Published delivery head SHA is invalid")
        return publication

    @staticmethod
    def _publication_metadata(
        bundle: DeveloperTaskBundle, record: DeliveryPreparation
    ) -> tuple[str, str, str]:
        issue = bundle.issue_context
        if issue is None:
            raise ValueError("Publication metadata requires issue context")
        title = f"aitobuild: {issue.title}".strip()
        if not title:
            raise ValueError("Publication title derived from issue metadata is empty")
        criteria = "\n".join(f"- {item}" for item in bundle.acceptance_criteria) or "- (none)"
        evidence = record.verification or {}
        command_lines = []
        for result in evidence.get("commands") or []:
            if isinstance(result, dict):
                command_lines.append(
                    f"- `{result.get('command')}` → exit {result.get('exit_code')}"
                )
        verification_block = "\n".join(command_lines) or "- (none)"
        body = (
            f"Automated draft for #{issue.issue_number}.\n\n"
            f"## Objective\n{bundle.objective}\n\n"
            f"## Acceptance criteria\n{criteria}\n\n"
            f"## Verification\n"
            f"- checkout digest: `{evidence.get('checkout_digest')}`\n"
            f"- base revision: `{record.base_revision}`\n"
            f"{verification_block}\n\n"
            f"Closes #{issue.issue_number}\n"
        )
        commit_message = f"aitobuild: implement #{issue.issue_number} {issue.title}".strip()
        return title[:200], body, commit_message[:200]

    def _run_verification_command(
        self, directory: Path, budget: DeveloperTaskBudget, adapter: ContainerSessionBashAdapter,
        session_id: str, command: str, index: int,
    ) -> dict[str, Any]:
        deadline = monotonic() + min(self._command_timeout, budget.remaining_seconds())
        request: dict[str, Any] = {"action": "start", "command": command, "cursor": 0}
        output = b""
        truncated = False
        result: dict[str, Any] = {"command": command, "exit_code": None, "output_path": f"verification-{index + 1:02d}.log"}
        while True:
            remaining = min(deadline - monotonic(), budget.remaining_seconds())
            if remaining <= 0:
                result["error"] = "Verification command deadline exceeded"
                break
            reply = shell_request(adapter, session_id, {**request, "wait_seconds": min(10, remaining), "max_output_chars": 6000})
            chunk = str(reply.get("output", "")).encode("utf-8")
            truncated = truncated or len(output) + len(chunk) > 65536 or bool(reply.get("output_truncated"))
            output = (output + chunk)[:65536]
            (directory / result["output_path"]).write_bytes(output)
            if reply.get("ok") is not True:
                result["error"] = str(reply.get("error", "Verification shell failed"))
                break
            if reply.get("status") != "running":
                result["exit_code"] = reply.get("exit_code")
                if reply.get("status") != "exited":
                    result["error"] = "Verification shell did not confirm process exit"
                break
            request = {"action": "read", "shell_id": reply["shell_id"], "cursor": reply.get("next_cursor", 0)}
        return {**result, "output": output[:6000].decode("utf-8", errors="ignore"), "output_truncated": truncated}

    @staticmethod
    def _checkout_digest(
        checkout: Path, budget: DeveloperTaskBudget, *,
        capture: dict[str, GitHubBlobChange | None] | None = None,
    ) -> str:
        def fail_walk(error: OSError) -> None:
            raise error

        entries = []
        for directory, directories, files in os.walk(checkout, followlinks=False, onerror=fail_walk):
            if Path(directory) == checkout:
                directories[:] = [name for name in directories if name != ".git"]
                files = [name for name in files if name != ".git"]
            for name in sorted(directories + files):
                budget.remaining_seconds()
                path = Path(directory) / name
                metadata = path.lstat()
                relative = path.relative_to(checkout).as_posix()
                entry = {"path": relative, "mode": stat.S_IMODE(metadata.st_mode)}
                captured = capture is not None and relative in capture
                if captured and not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Publication only supports regular file changes")
                if path.is_symlink():
                    entry.update(kind="symlink", content=os.readlink(path))
                elif path.is_file():
                    digest = sha256()
                    chunks = []
                    with path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            budget.remaining_seconds()
                            digest.update(chunk)
                            if captured:
                                chunks.append(chunk)
                    entry.update(kind="file", content=digest.hexdigest())
                    if captured and capture is not None:
                        content = b"".join(chunks)
                        mode: Literal["100644", "100755"] = (
                            "100755" if stat.S_IXUSR & metadata.st_mode else "100644"
                        )
                        capture[relative] = GitHubBlobChange(
                            mode=mode, content=content, blob_sha=_git_blob_sha(content),
                        )
                elif path.is_dir():
                    entry.update(kind="directory")
                else:
                    raise ValueError("Verification only supports regular files, directories and symlinks")
                entries.append(entry)
        return sha256(json.dumps(sorted(entries, key=lambda entry: entry["path"]), sort_keys=True).encode()).hexdigest()

    def _verify_checkout(self, directory: Path, record: DeliveryPreparation, budget: DeveloperTaskBudget, *, pristine: bool = True) -> None:
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
        if pristine and self._git(directory, budget, "-C", str(checkout), "status", "--porcelain", "--untracked-files=all").strip():
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
        if record.state not in {"preparing", "prepared", "failed", "implementing", "awaiting_tool_approval", "implemented", "verifying", "verified", "publishing", "published", "retired"} or bundle.issue_context is None:
            raise ValueError("Invalid persisted delivery preparation state")
        if record.state == "retired":
            retirement = record.retirement
            if (not _is_correction_record(record) or not isinstance(retirement, dict)
                    or set(retirement) != {"actor_id", "retired_at", "previous_state", "live_head_sha"}
                    or not isinstance(retirement["actor_id"], str) or not retirement["actor_id"]
                    or retirement["previous_state"] not in _RETIRABLE_STATES
                    or retirement["live_head_sha"] != record.base_revision
                    or not isinstance(retirement["retired_at"], str)):
                raise ValueError("Invalid persisted correction retirement")
            datetime.fromisoformat(retirement["retired_at"])
        elif record.retirement is not None:
            raise ValueError("Only retired corrections may carry a retirement receipt")
        self._validate_verification_commands(record.verification_commands, bundle)
        if record.session_id is not None:
            if not isinstance(record.session_id, str):
                raise ValueError("Invalid persisted native delivery session")
            _normalize_session_id(record.session_id)
        if record.state in {"implementing", "awaiting_tool_approval", "implemented", "verifying", "verified", "publishing", "published"} and record.session_id is None:
            raise ValueError("Missing persisted native delivery session")
        if record.state in {"verifying", "verified", "publishing", "published"}:
            evidence = record.verification
            if not isinstance(evidence, dict) or not isinstance(evidence.get("session_id"), str):
                raise ValueError("Invalid persisted verification evidence")
            _normalize_session_id(evidence["session_id"])
            if (evidence["session_id"] == record.session_id or not isinstance(evidence.get("commands"), list)
                    or not isinstance(evidence.get("checkout_digest"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", evidence["checkout_digest"]) is None):
                raise ValueError("Invalid persisted verification identity")
            timestamps = ("started_at", "completed_at") if record.state in {"verified", "publishing", "published"} else ("started_at",)
            for name in timestamps:
                timestamp = evidence.get(name)
                if not isinstance(timestamp, str) or not timestamp:
                    raise ValueError("Invalid persisted verification timestamp")
                datetime.fromisoformat(timestamp)
            if record.state in {"verified", "publishing", "published"} and (
                not record.verification_commands or evidence.get("cleanup_succeeded") is not True
                or len(evidence["commands"]) != len(record.verification_commands)
                or any(not isinstance(result, dict) or result.get("command") != command
                       or type(result.get("exit_code")) is not int or result["exit_code"] != 0 or result.get("error")
                       for command, result in zip(record.verification_commands, evidence["commands"], strict=True))
            ):
                raise ValueError("Persisted verification lacks successful command/cleanup evidence")
        if record.state in {"publishing", "published"} or (
            record.state in {"failed", "retired"}
            and isinstance(record.publication, dict)
            and isinstance(record.publication.get("pull_number"), int)
        ):
            publication = record.publication
            if not isinstance(publication, dict):
                raise ValueError("Invalid persisted publication payload")
            for key in (
                "title", "body", "commit_message", "checkout_digest", "tree_fingerprint",
                "base_ref", "base_sha", "branch", "repository",
            ):
                if not isinstance(publication.get(key), str) or not publication[key]:
                    raise ValueError("Invalid persisted publication field")
            if publication.get("mode") == "advance" or _is_correction_record(record):
                parent = publication.get("parent_head_sha")
                digest = publication.get("approval_digest")
                if (publication.get("mode") != "advance" or not _is_correction_record(record)
                        or publication["branch"] != bundle.issue_context.base_branch
                        or publication["base_sha"] != record.base_revision or parent != record.base_revision
                        or type(publication.get("pull_number")) is not int or publication["pull_number"] <= 0
                        or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                        or not isinstance(publication.get("target_preview_id"), str)
                        or not isinstance(publication.get("review_preview_id"), str)
                        or record.state == "published" and publication.get("head_sha") == parent
                        or "push_outcome" in publication and (
                            record.state not in {"failed", "retired"}
                            or publication["push_outcome"] not in {"not_applied", "moved"}
                            or not isinstance(publication.get("live_head_sha"), str)
                            or (publication["push_outcome"] == "not_applied") != (publication["live_head_sha"] == parent))):
                    raise ValueError("Correction publication identity differs from the pinned pull request")
            elif publication["base_sha"] != record.base_revision or publication["branch"] != record.branch:
                raise ValueError("Publication identity differs from the approved delivery")
            if record.verification is None:
                raise ValueError("Publication requires persisted verification evidence")
            if publication["checkout_digest"] != record.verification["checkout_digest"]:
                raise ValueError("Publication checkout digest differs from verification")
            if type(publication.get("issue_number")) is not int or publication["issue_number"] <= 0:
                raise ValueError("Invalid persisted publication issue number")
            if not isinstance(publication.get("changed_paths"), list) or not publication["changed_paths"]:
                raise ValueError("Publication must record scoped changed paths")
            if record.state == "published":
                published_at = publication.get("published_at")
                if not isinstance(published_at, str) or not published_at:
                    raise ValueError("Invalid persisted publication timestamp")
                datetime.fromisoformat(published_at)
                if publication.get("draft") is not True:
                    raise ValueError("Published delivery must record a draft pull request")
        if bundle.task_id != record.task_id or bundle.issue_context.base_revision != record.base_revision:
            raise ValueError("Persisted delivery preparation differs from the approved identity")
        expected_branch = f"aitobuild/issue-{bundle.issue_context.issue_number}-{sha256(bundle.task_id.encode()).hexdigest()[:16]}"
        if (directory != self._task_dir(record.preview_id) or record.checkout_path != str(directory / "repo")
                or record.branch != expected_branch):
            raise ValueError("Persisted delivery checkout identity is invalid")
        if (record.state in {"prepared", "implementing", "awaiting_tool_approval", "implemented", "verifying", "verified"}
            and record.head_revision != record.base_revision
                or record.state == "publishing"
                and record.head_revision not in {record.base_revision, None}
                and (
                    not isinstance(record.publication, dict)
                    or record.publication.get("head_sha") != record.head_revision
                )
                or record.state == "published" and (
                    not isinstance(record.head_revision, str)
                    or re.fullmatch(r"[0-9a-f]{40}", record.head_revision) is None
                    or not isinstance(record.publication, dict)
                    or record.publication.get("head_sha") != record.head_revision
                    or record.publication.get("pull_number") is None
                    or record.publication.get("draft") is not True
                )
                or record.state in {"failed", "retired"} and (not isinstance(record.error, str) or not record.error)):
            raise ValueError("Invalid persisted delivery outcome")
        if record.architect_review is not None:
            review = record.architect_review
            if not isinstance(review, dict):
                raise ValueError("Invalid persisted Architect review payload")
            if review.get("event") not in _ARCHITECT_REVIEW_EVENTS:
                raise ValueError("Invalid persisted Architect review event")
            for key in ("body", "head_sha", "repository", "reviewed_at"):
                if not isinstance(review.get(key), str) or not review[key]:
                    raise ValueError("Invalid persisted Architect review field")
            if type(review.get("pull_number")) is not int or review["pull_number"] <= 0:
                raise ValueError("Invalid persisted Architect review pull number")
            datetime.fromisoformat(review["reviewed_at"])
            if record.state != "published" or not isinstance(record.publication, dict):
                raise ValueError("Architect review requires a published delivery")
            if review["head_sha"] != record.publication.get("head_sha"):
                raise ValueError("Architect review head SHA differs from publication")
            if "commit_id" in review and review["commit_id"] != review["head_sha"]:
                raise ValueError("Architect review commit differs from its pinned head")
            if review["pull_number"] != record.publication.get("pull_number"):
                raise ValueError("Architect review pull number differs from publication")
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