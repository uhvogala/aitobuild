"""Mock-first bash adapter for deterministic test behavior."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, TimeoutExpired, run
from typing import Protocol
from uuid import uuid4

from aitobuild.policy import ActionClass, AgentRole, assert_role_action_allowed


@dataclass(slots=True, frozen=True)
class BashResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str


class BashAdapter(Protocol):
    def run(self, *, role: AgentRole, command: str, session_id: str | None = None) -> BashResult:
        ...


class MockBashAdapter:
    def run(self, *, role: AgentRole, command: str, session_id: str | None = None) -> BashResult:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        return BashResult(command=command, exit_code=0, stdout="mock-ok", stderr="")


class SubprocessBashAdapter:
    """Execute commands in the local runtime environment workspace."""

    def __init__(self, *, workspace_root: Path, timeout_seconds: int = 120) -> None:
        self._workspace_root = workspace_root
        self._timeout_seconds = timeout_seconds

    def run(self, *, role: AgentRole, command: str, session_id: str | None = None) -> BashResult:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)

        try:
            completed: CompletedProcess[str] = run(
                command,
                cwd=self._workspace_root,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return BashResult(
                command=command,
                exit_code=124,
                stdout=stdout,
                stderr=(stderr + "\ncommand timed out").strip(),
            )
        except CalledProcessError as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return BashResult(
                command=command,
                exit_code=exc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        return BashResult(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class ContainerSessionBashAdapter:
    """Run commands in a persistent container per session id."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        image: str,
        container_workdir: str,
        container_name_prefix: str,
        bind_source_path: str | None = None,
        run_as_current_user: bool = True,
        timeout_seconds: int = 120,
    ) -> None:
        self._workspace_root = workspace_root
        self._image = image
        self._container_workdir = container_workdir
        self._container_name_prefix = container_name_prefix
        self._bind_source_path = Path(bind_source_path) if bind_source_path is not None else None
        self._run_as_current_user = run_as_current_user
        self._timeout_seconds = timeout_seconds
        self._containers: dict[str, str] = {}

        container_user: str | None = None
        if self._run_as_current_user and hasattr(os, "getuid") and hasattr(os, "getgid"):
            container_user = f"{os.getuid()}:{os.getgid()}"
        self._container_user = container_user

    def create_session(self, *, session_id: str | None = None) -> tuple[str, str]:
        raw_session = (session_id or f"sess-{uuid4()}").strip()
        if not raw_session:
            raise ValueError("session_id must be non-empty")
        normalized_session = _normalize_session_id(raw_session)

        existing = self._containers.get(normalized_session)
        if existing is not None:
            return normalized_session, existing

        container_name = _container_name_for_session(
            prefix=self._container_name_prefix,
            session_id=normalized_session,
        )
        running_container_names = set(
            _list_container_names(timeout_seconds=self._timeout_seconds)
        )
        if container_name in running_container_names:
            self._containers[normalized_session] = container_name
            return normalized_session, container_name

        all_container_names = set(
            _list_container_names(timeout_seconds=self._timeout_seconds, include_all=True)
        )
        if container_name in all_container_names:
            _run_process(
                command=["docker", "rm", "-f", container_name],
                timeout_seconds=self._timeout_seconds,
            )

        mount_source = self._bind_source_path or self._workspace_root
        command = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "-w",
            self._container_workdir,
            "-v",
            f"{mount_source}:{self._container_workdir}",
        ]
        if self._container_user is not None:
            # Keep writes on bind-mounted workspace owned by the caller, not root.
            command.extend(["--user", self._container_user, "-e", "HOME=/tmp"])

        command.extend([
            self._image,
            "tail",
            "-f",
            "/dev/null",
        ])

        completed = _run_process(command=command, timeout_seconds=self._timeout_seconds)
        if completed.exit_code != 0:
            raise RuntimeError(
                f"Failed to create container session: {completed.stderr or completed.stdout}"
            )

        self._containers[normalized_session] = container_name
        return normalized_session, container_name

    def get_container_name(self, *, session_id: str) -> str | None:
        normalized_session = _normalize_session_id(session_id)
        return self._containers.get(normalized_session)

    def list_session_ids(self) -> tuple[str, ...]:
        session_ids = set(self._containers.keys())
        for container_name in _list_container_names(timeout_seconds=self._timeout_seconds):
            discovered_session_id = _session_id_from_container_name(
                container_name=container_name,
                prefix=self._container_name_prefix,
            )
            if discovered_session_id is None:
                continue
            session_ids.add(discovered_session_id)
            self._containers.setdefault(discovered_session_id, container_name)

        return tuple(sorted(session_ids))

    def close_session(self, *, session_id: str) -> bool:
        normalized_session = _normalize_session_id(session_id)
        container_name = self._containers.pop(
            normalized_session,
            _container_name_for_session(
                prefix=self._container_name_prefix,
                session_id=normalized_session,
            ),
        )

        running_container_names = set(
            _list_container_names(timeout_seconds=self._timeout_seconds)
        )
        if container_name in running_container_names:
            stopped = _run_process(
                command=["docker", "stop", "--time", "5", container_name],
                timeout_seconds=self._timeout_seconds,
            )
            if stopped.exit_code == 0 or _is_container_not_found(stopped):
                return True

        all_container_names = set(
            _list_container_names(timeout_seconds=self._timeout_seconds, include_all=True)
        )
        if container_name not in all_container_names:
            return False

        removed = _run_process(
            command=["docker", "rm", "-f", container_name],
            timeout_seconds=self._timeout_seconds,
        )
        return removed.exit_code == 0 or _is_container_not_found(removed)

    def run(self, *, role: AgentRole, command: str, session_id: str | None = None) -> BashResult:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)

        if session_id is None or not session_id.strip():
            raise ValueError("session_id is required in container_session mode")

        normalized_session = _normalize_session_id(session_id)
        container_name = self._containers.get(normalized_session)
        if container_name is None:
            _, container_name = self.create_session(session_id=normalized_session)

        completed = _run_process(
            command=["docker", "exec", container_name, "sh", "-lc", command],
            timeout_seconds=self._timeout_seconds,
        )

        return BashResult(
            command=command,
            exit_code=completed.exit_code,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _normalize_session_id(session_id: str) -> str:
    cleaned = session_id.strip().lower().replace("_", "-")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    normalized = "".join(char for char in cleaned if char in allowed)
    normalized = normalized.strip("-")
    if not normalized:
        raise ValueError("session_id must contain alphanumeric characters")
    return normalized[:48]


def _run_process(*, command: list[str], timeout_seconds: int) -> BashResult:
    try:
        completed: CompletedProcess[str] = run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return BashResult(
            command=" ".join(command),
            exit_code=124,
            stdout=stdout,
            stderr=(stderr + "\ncommand timed out").strip(),
        )
    except CalledProcessError as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return BashResult(
            command=" ".join(command),
            exit_code=exc.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return BashResult(
        command=" ".join(command),
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _container_name_for_session(*, prefix: str, session_id: str) -> str:
    return f"{prefix}-{session_id}"


def _session_id_from_container_name(*, container_name: str, prefix: str) -> str | None:
    marker = f"{prefix}-"
    if not container_name.startswith(marker):
        return None

    candidate = container_name[len(marker) :].strip()
    if not candidate:
        return None
    try:
        return _normalize_session_id(candidate)
    except ValueError:
        return None


def _list_container_names(*, timeout_seconds: int, include_all: bool = False) -> tuple[str, ...]:
    command = ["docker", "ps", "--format", "{{.Names}}"]
    if include_all:
        command = ["docker", "ps", "-a", "--format", "{{.Names}}"]

    result = _run_process(command=command, timeout_seconds=timeout_seconds)
    if result.exit_code != 0:
        return ()

    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _is_container_not_found(result: BashResult) -> bool:
    combined = f"{result.stderr}\n{result.stdout}".lower()
    return "no such container" in combined
