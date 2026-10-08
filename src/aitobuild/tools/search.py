"""Scoped, streamed ripgrep queries with bounded model-facing pages."""

from __future__ import annotations

import base64
from contextlib import closing
import json
from pathlib import Path, PurePosixPath
from subprocess import PIPE, Popen
from tempfile import TemporaryFile
from threading import Event, Timer
from time import monotonic
from typing import Any, Generator, IO, Literal

from aitobuild.developer_isolation import DeveloperIsolationPolicy, is_path_allowed


def _scopes(workspace: Path, path: str, policy: DeveloperIsolationPolicy) -> list[str]:
    relative = Path(path.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Search path must be workspace-relative without parent traversal")
    target = workspace / relative
    if not target.resolve().is_relative_to(workspace) or target.is_symlink():
        raise ValueError("Search path must not escape the Developer workspace or be a symlink")
    if not target.exists():
        raise ValueError("Search path does not exist in this Developer workspace")
    if relative != Path("."):
        normalized = relative.as_posix() + ("/" if target.is_dir() else "")
        if not is_path_allowed(normalized, policy=policy):
            raise ValueError("Search path is outside allowed policy paths")
        return [relative.as_posix()]
    return [allowed.rstrip("/") for allowed in policy.allowed_paths
            if (workspace / allowed).exists() and not (workspace / allowed.rstrip("/")).is_symlink()
            and (workspace / allowed).resolve().is_relative_to(workspace)]


def _decode(value: dict[str, Any]) -> str:
    if "text" in value:
        return str(value["text"])
    return base64.b64decode(value["bytes"]).decode("utf-8", errors="replace")


def _records(stream: IO[bytes], kind: Literal["files", "content"]) -> Generator[dict[str, Any], None, None]:
    if kind == "files":
        pending = b""
        while chunk := stream.read(4096):
            parts = (pending + chunk).split(b"\0")
            pending = parts.pop()
            for part in parts:
                yield {"path": part.decode("utf-8", errors="replace")}
        return
    for line in stream:
        event = json.loads(line)
        if event["type"] != "match":
            continue
        data = event["data"]
        text = _decode(data["lines"]).rstrip("\r\n")
        if "\0" in text:
            continue
        preview = text.encode("utf-8")[:400].decode("utf-8", errors="ignore")
        yield {"path": _decode(data["path"]), "line_number": data["line_number"],
               "column_bytes": data["submatches"][0]["start"] + 1,
               "text": preview, "text_truncated": preview != text}


def _run_rg(
    command: list[str], workspace: Path, kind: Literal["files", "content"], deadline: float,
) -> Generator[dict[str, Any], None, None]:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("Search exceeded its deadline; narrow the path, glob or pattern")
    timed_out = Event()
    with TemporaryFile() as errors:
        try:
            process = Popen(command, cwd=workspace, stdout=PIPE, stderr=errors)
        except FileNotFoundError as error:
            raise RuntimeError("ripgrep is required; rebuild the prepared Developer image or install rg locally") from error

        def expire() -> None:
            timed_out.set()
            if process.poll() is None:
                process.kill()

        timer = Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        exhausted = False
        try:
            if process.stdout is None:
                raise RuntimeError("Internal invariant violated: process.stdout is not None")
            yield from _records(process.stdout, kind)
            exhausted = True
        finally:
            if not exhausted and process.poll() is None:
                process.kill()
            process.wait()
            timer.cancel()
            if process.stdout is None:
                raise RuntimeError("Internal invariant violated: process.stdout is not None")
            process.stdout.close()
        errors.seek(0)
        diagnosis = errors.read(2000).decode("utf-8", errors="replace").strip()
        if timed_out.is_set() or process.returncode == 124:
            raise TimeoutError("Search exceeded its deadline; narrow the path, glob or pattern")
        if process.returncode not in (0, 1):
            raise ValueError(f"ripgrep search failed: {diagnosis or process.returncode}")


def _glob_match(path: str, pattern: str) -> bool:
    if pattern.startswith("/"):
        pattern = pattern[1:]
    elif "/" not in pattern:
        pattern = f"**/{pattern}"
    return PurePosixPath(path).full_match(pattern)


def search_workspace(
    *, workspace: Path, policy: DeveloperIsolationPolicy, kind: Literal["files", "content"],
    pattern: str, path: str = ".", globs: list[str] | None = None,
    is_regex: bool = False, case_sensitive: bool = True,
    include_hidden: bool = False, include_ignored: bool = False,
    offset: int = 0, max_results: int = 20, command_prefix: tuple[str, ...] = (),
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    if not pattern or len(pattern.encode("utf-8")) > 1000:
        raise ValueError("pattern must contain 1..1000 UTF-8 bytes")
    if not 0 <= offset <= 10000 or not 1 <= max_results <= 100:
        raise ValueError("offset must be 0..10000 and max_results must be 1..100")
    globs = globs or []
    if len(globs) > 20 or any(not glob or len(glob) > 512 for glob in globs):
        raise ValueError("Use at most 20 non-empty globs, each at most 512 characters")
    workspace = workspace.resolve()
    scopes = _scopes(workspace, path, policy)
    results: list[dict[str, Any]] = []
    has_more = False
    if scopes:
        command = [*command_prefix, "rg", "--no-config", "--color=never", "--sort=path", "--no-require-git",
                   "--files", "--null"]
        if include_hidden:
            command.append("--hidden")
        if include_ignored:
            command.append("--no-ignore")
        command.extend(f"--glob=!{blocked.rstrip('/')}/**" for blocked in policy.blocked_paths)
        command.extend(f"--glob=!**/{blocked}/**" for blocked in (".git", ".venv", ".aitobuild", "secrets"))
        command.extend(["--", *sorted(scopes)])
        deadline = monotonic() + timeout_seconds
        includes = [glob for glob in globs if not glob.startswith("!")]
        excludes = [glob[1:] for glob in globs if glob.startswith("!")]

        def matching_files() -> Generator[dict[str, Any], None, None]:
            with closing(_run_rg(command, workspace, "files", deadline)) as files:
                for record in files:
                    relative = Path(record["path"])
                    target = (workspace / relative).resolve()
                    if (relative.is_absolute() or ".." in relative.parts
                            or not target.is_relative_to(workspace)
                            or not is_path_allowed(relative.as_posix(), policy=policy)
                            or not is_path_allowed(target.relative_to(workspace).as_posix(), policy=policy)):
                        continue
                    name = relative.as_posix()
                    if ((kind == "files" and not _glob_match(name, pattern))
                            or (includes and not any(_glob_match(name, glob) for glob in includes))
                            or any(_glob_match(name, glob) for glob in excludes)):
                        continue
                    if kind == "content" and target.stat().st_size > 1048576:
                        continue
                    yield {"path": name}

        def matches() -> Generator[dict[str, Any], None, None]:
            with closing(matching_files()) as files:
                if kind == "files":
                    yield from files
                    return
                content_command = [*command_prefix, "rg", "--no-config", "--color=never", "--sort=path",
                                   "--json", "--max-filesize=1M", f"--regexp={pattern}",
                                   "--case-sensitive" if case_sensitive else "--ignore-case"]
                if not is_regex:
                    content_command.append("--fixed-strings")
                batch: list[str] = []
                for file in files:
                    batch.append(file["path"])
                    if len(batch) == 64:
                        with closing(_run_rg([*content_command, "--", *batch], workspace, "content", deadline)) as hits:
                            yield from hits
                        batch = []
                if batch:
                    with closing(_run_rg([*content_command, "--", *batch], workspace, "content", deadline)) as hits:
                        yield from hits

        seen = 0
        result_bytes = 0
        with closing(matches()) as records:
            for record in records:
                if seen < offset:
                    seen += 1
                    continue
                size = len(json.dumps(record).encode("utf-8")) + 2
                if len(results) == max_results or (results and result_bytes + size > 4800):
                    has_more = True
                    break
                results.append(record)
                result_bytes += size
    return {"ok": True, "results": results, "returned": len(results), "has_more": has_more,
            "next_offset": offset + len(results) if has_more else None, "order": "path",
            "max_file_bytes": 1048576 if kind == "content" else None,
            "next_action": ("Repeat the same query with next_offset for another page; do not fetch every page automatically."
                            if has_more else "Read a selected file for exact edit context. Hidden/ignored files are opt-in; content search skips files over 1 MiB.")}