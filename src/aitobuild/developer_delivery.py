"""Local preparation, native lifecycle and independent verification of approved issue tasks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
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
from aitobuild.policy import AgentRole
from aitobuild.tools.github import (
    GitHubAdapter,
    GitHubBlobChange,
    MockGitHubAdapter,
    _git_blob_sha,
    _tree_fingerprint,
)
from aitobuild.tools.bash import ContainerSessionBashAdapter, _normalize_session_id
from aitobuild.tools.shell import shell_request


ARCHITECT_REVIEW_BODY_MAX = 2000
ARCHITECT_REVIEW_BODY_PREFIX = "aitobuild Architect review\n\n"
_ARCHITECT_REVIEW_EVENTS = frozenset({"COMMENT"})


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
                if record.state == "failed":
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
        directory = self._task_dir(preview_id)
        with self.implementation_lock(preview_id):
            with FileLock(str(directory) + ".lock", timeout=10):
                record = self._load(directory)
            if record is None:
                raise ValueError("Delivery not found")
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
                if digest != record.verification["checkout_digest"]:
                    raise ValueError("Verified checkout changed; publication is blocked")
                tree_fingerprint = _tree_fingerprint(files)
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

    def get_published_pull_request(
        self, preview_id: str, *, github: GitHubAdapter,
    ) -> dict[str, Any]:
        """Resolve a published draft PR from delivery publication only."""
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

    def submit_architect_review(
        self,
        preview_id: str,
        *,
        github: GitHubAdapter,
        event: str,
        body: str,
    ) -> DeliveryPreparation:
        """Submit COMMENT against the published draft; persist last review.

        REQUEST_CHANGES stays disabled until a distinct reviewer GitHub identity
        is configured (same-token self-reviews 422 on GitHub).
        """
        directory = self._task_dir(preview_id)
        with FileLock(str(directory) + ".lock", timeout=10):
            record = self._load(directory)
            if record is None:
                raise ValueError("Delivery not found")
            publication = dict(self._require_published_publication(record))
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
        if live_head != expected_head:
            raise ValueError(
                "Published draft head SHA no longer matches delivery publication; "
                "call architect_get_published_pr and retry only if the delivery was republished"
            )
        if pull.head_ref != publication["branch"]:
            raise ValueError("Published pull request head ref does not match the delivery branch")
        if not pull.draft or pull.state != "open":
            raise ValueError("Published pull request must remain an open draft")
        review = github.submit_pr_review(
            role=AgentRole.ARCHITECT,
            repository=str(publication["repository"]),
            pull_number=int(publication["pull_number"]),
            event="COMMENT",
            body=review_body,
            commit_id=expected_head,
        )
        architect_review = {
            "event": "COMMENT",
            "body": review_body,
            "review_id": review.review_id,
            "html_url": review.html_url,
            "head_sha": expected_head,
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
        if record.state not in {"preparing", "prepared", "failed", "implementing", "awaiting_tool_approval", "implemented", "verifying", "verified", "publishing", "published"} or bundle.issue_context is None:
            raise ValueError("Invalid persisted delivery preparation state")
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
            record.state == "failed"
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
            if publication["base_sha"] != record.base_revision or publication["branch"] != record.branch:
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
                or record.state == "failed" and (not isinstance(record.error, str) or not record.error)):
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