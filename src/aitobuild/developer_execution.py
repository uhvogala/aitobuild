"""Developer execution harness for practical isolated task runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aitobuild.developer_isolation import DeveloperTaskBundle, is_command_allowed, is_path_allowed
from aitobuild.policy import AgentRole
from aitobuild.tools import BashAdapter, BashResult, FilesystemAdapter, MockBashAdapter, MockFilesystemAdapter


@dataclass(slots=True, frozen=True)
class PlannedFileWrite:
    path: str
    content: str


@dataclass(slots=True, frozen=True)
class CommandOutcome:
    command: str
    executed: bool
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    rejected_reason: str | None


@dataclass(slots=True, frozen=True)
class FileWriteOutcome:
    path: str
    executed: bool
    rejected_reason: str | None


@dataclass(slots=True, frozen=True)
class DeveloperExecutionResult:
    accepted: bool
    reason: str | None
    command_outcomes: tuple[CommandOutcome, ...]
    file_write_outcomes: tuple[FileWriteOutcome, ...]


class DeveloperExecutionEngine:
    def __init__(
        self,
        *,
        filesystem_adapter: FilesystemAdapter | None = None,
        bash_adapter: BashAdapter | None = None,
    ) -> None:
        self._filesystem = filesystem_adapter or MockFilesystemAdapter()
        self._bash = bash_adapter or MockBashAdapter()

    def execute(
        self,
        *,
        bundle: DeveloperTaskBundle,
        commands: tuple[str, ...],
        file_writes: tuple[PlannedFileWrite, ...],
        workspace_root: Path,
        dry_run: bool,
        approved: bool,
        require_human_approval_for_repo_writes: bool,
        session_id: str | None = None,
    ) -> DeveloperExecutionResult:
        if bundle.issue_context is not None:
            return DeveloperExecutionResult(
                accepted=False,
                reason="Repository issue execution requires the disposable-checkout delivery worker",
                command_outcomes=(), file_write_outcomes=(),
            )
        command_outcomes: list[CommandOutcome] = []
        write_outcomes: list[FileWriteOutcome] = []
        accepted = True
        reason: str | None = None

        if len(file_writes) > bundle.policy.max_file_changes:
            return DeveloperExecutionResult(
                accepted=False,
                reason=(
                    f"file write count exceeds max_file_changes={bundle.policy.max_file_changes}"
                ),
                command_outcomes=(),
                file_write_outcomes=(),
            )

        for command in commands:
            if not is_command_allowed(command, policy=bundle.policy):
                accepted = False
                reject_reason = f"command is outside allowed policy prefixes: {command}"
                reason = reason or reject_reason
                command_outcomes.append(
                    CommandOutcome(
                        command=command,
                        executed=False,
                        exit_code=None,
                        stdout=None,
                        stderr=None,
                        rejected_reason=reject_reason,
                    )
                )
                continue

            if dry_run:
                command_outcomes.append(
                    CommandOutcome(
                        command=command,
                        executed=False,
                        exit_code=None,
                        stdout="dry-run",
                        stderr="",
                        rejected_reason=None,
                    )
                )
                continue

            try:
                result = self._bash.run(
                    role=AgentRole.DEVELOPER,
                    command=command,
                    session_id=session_id,
                )
            except ValueError as exc:
                accepted = False
                reject_reason = str(exc)
                reason = reason or reject_reason
                command_outcomes.append(
                    CommandOutcome(
                        command=command,
                        executed=False,
                        exit_code=None,
                        stdout=None,
                        stderr=None,
                        rejected_reason=reject_reason,
                    )
                )
                continue

            command_outcomes.append(_command_outcome_from_result(result))
            if result.exit_code != 0:
                accepted = False
                reason = reason or f"command failed with exit_code={result.exit_code}: {command}"

        for write in file_writes:
            try:
                normalized_path = _normalize_workspace_relative_path(write.path)
            except ValueError as exc:
                accepted = False
                message = str(exc)
                reason = reason or message
                write_outcomes.append(
                    FileWriteOutcome(path=write.path, executed=False, rejected_reason=message)
                )
                continue

            if not is_path_allowed(normalized_path, policy=bundle.policy):
                accepted = False
                reject_reason = f"path is outside allowed policy paths: {normalized_path}"
                reason = reason or reject_reason
                write_outcomes.append(
                    FileWriteOutcome(path=normalized_path, executed=False, rejected_reason=reject_reason)
                )
                continue

            if dry_run:
                write_outcomes.append(
                    FileWriteOutcome(path=normalized_path, executed=False, rejected_reason=None)
                )
                continue

            target = workspace_root / normalized_path
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._filesystem.write_text(
                    role=AgentRole.DEVELOPER,
                    path=target,
                    content=write.content,
                    approved=approved,
                    require_human_approval_for_repo_writes=require_human_approval_for_repo_writes,
                    session_id=session_id,
                )
            except PermissionError as exc:
                accepted = False
                message = str(exc)
                reason = reason or message
                write_outcomes.append(
                    FileWriteOutcome(path=normalized_path, executed=False, rejected_reason=message)
                )
                continue

            write_outcomes.append(
                FileWriteOutcome(path=normalized_path, executed=True, rejected_reason=None)
            )

        return DeveloperExecutionResult(
            accepted=accepted,
            reason=reason,
            command_outcomes=tuple(command_outcomes),
            file_write_outcomes=tuple(write_outcomes),
        )


def _normalize_workspace_relative_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        raise ValueError("file write path must be non-empty")
    if normalized.startswith("/"):
        raise ValueError("file write path must be workspace-relative")
    if ".." in normalized.split("/"):
        raise ValueError("file write path must not traverse parent directories")
    return normalized


def _command_outcome_from_result(result: BashResult) -> CommandOutcome:
    return CommandOutcome(
        command=result.command,
        executed=True,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        rejected_reason=None,
    )
