"""Mock-first bash adapter for deterministic test behavior."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
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
                cwd=(
                    prepare_developer_workspace(self._workspace_root, session_id)
                    if session_id else self._workspace_root
                ),
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
        data_volume_name: str | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._image = image
        self._container_workdir = container_workdir
        self._container_name_prefix = container_name_prefix
        self._bind_source_path = Path(bind_source_path) if bind_source_path is not None else None
        self._run_as_current_user = run_as_current_user
        self._timeout_seconds = timeout_seconds
        self._containers: dict[str, str] = {}
        workspace_key = str(self._bind_source_path or self._workspace_root.resolve())
        workspace_hash = sha256(workspace_key.encode()).hexdigest()[:16]
        self._workspace_hash = workspace_hash
        self._container_name_prefix = f"{container_name_prefix}-{workspace_hash}"
        self.data_volume_name = data_volume_name or f"{container_name_prefix}-data-{workspace_hash}"
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", self.data_volume_name):
            raise ValueError("Developer data volume must be a named Docker volume, not a path")
        self.home_dir = "/home/developer"

        container_user: str | None = None
        if self._run_as_current_user and hasattr(os, "getuid") and hasattr(os, "getgid"):
            container_user = f"{os.getuid()}:{os.getgid()}"
        self._container_user = container_user

    def create_session(self, *, session_id: str | None = None) -> tuple[str, str]:
        raw_session = (session_id or f"sess-{uuid4()}").strip()
        if not raw_session:
            raise ValueError("session_id must be non-empty")
        normalized_session = _normalize_session_id(raw_session)
        self._prepare_workspace(normalized_session)

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

        relative_workspace = self.get_workspace_root(normalized_session).relative_to(self._workspace_root)
        mount_source = self._resolve_bind_source() / relative_workspace
        self._prepare_data_volume(normalized_session)
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-w",
            self._container_workdir,
            "-v",
            f"{mount_source}:{self._container_workdir}",
            "--mount",
            f"type=volume,source={self.data_volume_name},target={self.home_dir},"
            f"volume-subpath={self.data_volume_subpath(normalized_session)}",
            "-e",
            f"HOME={self.home_dir}",
            "-e",
            f"UV_CACHE_DIR={self.home_dir}/.cache/uv",
            "-e",
            f"NPM_CONFIG_PREFIX={self.home_dir}/.local",
            "-e",
            f"PATH={self.home_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        ]
        if self._container_user is not None:
            # Keep writes on bind-mounted workspace owned by the caller, not root.
            command.extend(["--user", self._container_user])
        else:
            command.extend(["--user", "0:0"])

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

    def _resolve_bind_source(self) -> Path:
        base = self._bind_source_path or self._workspace_root
        if base != self._workspace_root or not Path("/.dockerenv").exists():
            return base
        inspected = _run_process(
            command=["docker", "inspect", os.uname().nodename, "--format", "{{json .Mounts}}"],
            timeout_seconds=self._timeout_seconds,
        )
        if inspected.exit_code != 0:
            return base
        try:
            mounts = json.loads(inspected.stdout)
            matching = [
                mount for mount in mounts
                if self._workspace_root.is_relative_to(mount["Destination"])
            ]
            if matching:
                mount = max(matching, key=lambda item: len(item["Destination"]))
                return Path(mount["Source"]) / self._workspace_root.relative_to(mount["Destination"])
        except (ValueError, TypeError, KeyError):
            pass
        return base

    def get_workspace_root(self, session_id: str) -> Path:
        return self._workspace_root / ".aitobuild" / "workspaces" / _normalize_session_id(session_id)

    def data_volume_subpath(self, session_id: str) -> str:
        return f"{self._workspace_hash}/{_normalize_session_id(session_id)}"

    def _prepare_workspace(self, session_id: str) -> None:
        prepare_developer_workspace(self._workspace_root, session_id)

    def _prepare_data_volume(self, session_id: str) -> None:
        created = _run_process(
            command=["docker", "volume", "create", self.data_volume_name],
            timeout_seconds=self._timeout_seconds,
        )
        if created.exit_code != 0:
            raise RuntimeError(f"Failed to create Developer data volume: {created.stderr}")
        owned = _run_process(
                command=[
                    "docker", "run", "--rm", "--user", "0",
                    "--mount", f"type=volume,source={self.data_volume_name},target=/aitobuild-data",
                    "--entrypoint", "python", self._image, "-c",
                    "import os,sys; from pathlib import Path; "
                    "home=Path('/aitobuild-data')/sys.argv[1]; "
                    "home.mkdir(parents=True,exist_ok=True,mode=0o700); "
                    "owner=sys.argv[2].split(':'); os.chown(home,int(owner[0]),int(owner[1]))",
                    self.data_volume_subpath(session_id), self._container_user or "0:0",
                ],
                timeout_seconds=self._timeout_seconds,
            )
        if owned.exit_code != 0:
            raise RuntimeError(f"Failed to prepare Developer home ownership: {owned.stderr}")

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
                removed = _run_process(
                    command=["docker", "rm", "-f", container_name],
                    timeout_seconds=self._timeout_seconds,
                )
                return removed.exit_code == 0 or _is_container_not_found(removed)

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
            command=["docker", "exec", container_name, "sh", "-c", command],
            timeout_seconds=self._timeout_seconds,
        )

        return BashResult(
            command=command,
            exit_code=completed.exit_code,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def prepare_developer_workspace(workspace_root: Path, session_id: str) -> Path:
    workspace_root = workspace_root.resolve()
    target = workspace_root / ".aitobuild" / "workspaces" / _normalize_session_id(session_id)
    if target.is_dir():
        return target
    if (workspace_root / ".git").is_file():
        raise ValueError("Developer workspace seed must be a standalone checkout, not a Git worktree")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="seed-", dir=target.parent))
    generated = shutil.ignore_patterns(
        ".aitobuild", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
        ".mypy_cache", "node_modules", "secrets", "aitobuild-sim-*", ".run-artifacts",
        "simulation-report.json",
    )

    def ignored(directory: str, names: list[str]) -> set[str]:
        excluded = set(generated(directory, names))
        excluded.update(
            name for name in names if name.startswith(".env")
            and name not in {".env.simulation", ".env.simulation.session"}
            and not name.endswith(".example")
        )
        if Path(directory) == workspace_root / ".devcontainer" / "certs":
            excluded.update(
                name for name in names if name == "host"
                or Path(name).suffix.lower() in {".crt", ".pem", ".cer", ".key"}
            )
        return excluded

    try:
        shutil.copytree(
            workspace_root, temporary / "repo", symlinks=True,
            ignore=ignored,
        )
        (temporary / "repo").rename(target)
    finally:
        shutil.rmtree(temporary)
    return target


def _normalize_session_id(session_id: str) -> str:
    normalized = session_id.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", normalized):
        raise ValueError(
            "session_id must be 1..48 lowercase letters/digits/hyphens, starting with a letter or digit; "
            "use a unique ID such as dev-one. IDs are never silently normalized or truncated."
        )
    return normalized


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
