"""Container-side managed terminals and process inspection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import psutil

LOG_LIMIT = 16 * 1024 * 1024
KEYS = {"enter": "Enter", "interrupt": "C-c", "eof": "C-d", "tab": "Tab", "escape": "Escape"}


class ShellError(ValueError):
    def __init__(self, code: str, message: str, next_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.next_action = next_action


def _root() -> Path:
    root = Path.home() / ".aitobuild" / "shells"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _tmux(*arguments: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["tmux", "-S", str(_root() / "tmux.sock"), *arguments],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as error:
        raise ShellError("toolchain_missing", "tmux is not installed in this Developer image.",
                         "Rebuild .devcontainer/Dockerfile.developer and restart the container.") from error
    if result.returncode:
        raise ShellError("terminal_unavailable", result.stderr.strip()[:1000],
                         "Use developer_shell(action='list'). The container may have restarted; start a new shell if needed.")
    return result


def _metadata(shell_id: str) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"sh-[0-9a-f]{12}", shell_id):
        raise ShellError("invalid_shell_id", "shell_id must be the sh-... ID returned by start/list.",
                         "Use developer_shell(action='list'); do not invent shell IDs.")
    directory = _root() / shell_id
    try:
        metadata = json.loads((directory / "metadata.json").read_text())
    except FileNotFoundError as error:
        raise ShellError("shell_not_found", "No shell with that ID exists for this Developer.",
                         "Use developer_shell(action='list') in the same Developer session.") from error
    return directory, metadata


def _status(directory: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    exit_file = directory / "exit-code"
    if exit_file.exists():
        return {"status": "exited", "exit_code": int(exit_file.read_text())}
    try:
        state = _tmux("display-message", "-p", "-t", metadata["shell_id"],
                      "#{pane_dead}|#{pane_dead_status}|#{pane_pid}").stdout.strip().split("|")
    except ShellError:
        return {"status": "interrupted", "exit_code": None}
    if state[0] == "1":
        return {"status": "exited", "exit_code": int(state[1]) if state[1] else None}
    return {"status": "running", "exit_code": None, "pid": int(state[2])}


def _read(shell_id: str, cursor: int | None, max_chars: int, wait_seconds: float) -> dict[str, Any]:
    directory, metadata = _metadata(shell_id)
    status = _status(directory, metadata)
    if wait_seconds and status["status"] == "running":
        try:
            _tmux("wait-for", f"done-{shell_id}", timeout=wait_seconds)
        except (subprocess.TimeoutExpired, ShellError):
            pass
        status = _status(directory, metadata)
    logfile = directory / "output.log"
    size = logfile.stat().st_size if logfile.exists() else 0
    start = max(0, size - max_chars) if cursor is None else cursor
    if start < 0 or start > size:
        raise ShellError("invalid_cursor", "cursor is outside this shell's retained output.",
                         "Omit cursor to read the latest output; then reuse next_cursor.")
    with logfile.open("rb") as stream:
        stream.seek(start)
        output = stream.read(max_chars)
        next_cursor = stream.tell()
    text = output.decode("utf-8", errors="replace")
    if not output and cursor is None and status["status"] != "interrupted":
        try:
            text = _tmux("capture-pane", "-p", "-t", shell_id, "-S", "-100").stdout.rstrip()[-max_chars:]
        except ShellError:
            pass
    return {
        "ok": True, "shell_id": shell_id, **status,
        "output": text, "next_cursor": next_cursor,
        "output_truncated": next_cursor < size or (cursor is None and start > 0),
        "log_limit_reached": (directory / "log-limit").exists(),
        "log_path": str(logfile),
        "next_action": (
            "Read again with next_cursor to get only new output. Send input if the program is waiting; do not restart it."
            if status["status"] == "running" else
            "Check exit_code and output. Processes do not survive container shutdown; retained logs do."
        ),
    }


def _start(command: str, max_chars: int, wait_seconds: float) -> dict[str, Any]:
    if not command.strip() or len(command) > 8000:
        raise ShellError("invalid_command", "command must contain 1..8000 characters.",
                         "Use a short shell command, or run a checked-in script.")
    shell_id = "sh-" + uuid4().hex[:12]
    directory = _root() / shell_id
    directory.mkdir(mode=0o700)
    metadata = {"shell_id": shell_id, "command": command, "started_at": time.time()}
    (directory / "metadata.json").write_text(json.dumps(metadata))
    (directory / "output.log").touch()
    prefix = shlex.join(["tmux", "-S", str(_root() / "tmux.sock")])
    runner = (
        f"{prefix} wait-for ready-{shell_id}; "
        f"sh -c {shlex.quote(command)}; result=$?; "
        f"printf '%s' \"$result\" > {shlex.quote(str(directory / 'exit-code'))}; "
        f"{prefix} wait-for -S done-{shell_id}; exit \"$result\""
    )
    try:
        _tmux("new-session", "-d", "-s", shell_id, "-x", "200", "-y", "50",
              "-c", str(Path.cwd()), "sh", "-c", runner)
        _tmux("set-option", "-t", shell_id, "remain-on-exit", "on")
        _tmux("pipe-pane", "-o", "-t", shell_id,
              shlex.join([sys.executable, str(Path(__file__).resolve()), "--sink", str(directory)]))
        _tmux("wait-for", "-S", f"ready-{shell_id}")
    except BaseException:
        try:
            _tmux("kill-session", "-t", shell_id)
        except ShellError:
            pass
        raise
    return _read(shell_id, None, max_chars, wait_seconds)


def _sink(directory: Path) -> None:
    size = 0
    with (directory / "output.log").open("ab", buffering=0) as stream:
        while block := os.read(sys.stdin.fileno(), 4096):
            retained = block[:max(0, LOG_LIMIT - size)]
            stream.write(retained)
            size += len(retained)
            if len(retained) < len(block):
                (directory / "log-limit").touch()


def _process_info(process: psutil.Process) -> dict[str, Any]:
    with process.oneshot():
        created = process.create_time()
        times = process.cpu_times()
        return {
            "process_handle": f"{process.pid}:{created:.6f}", "pid": process.pid,
            "parent_pid": process.ppid(), "name": process.name(), "status": process.status(),
            "command": shlex.join(process.cmdline())[:400], "started_at": created,
            "cpu_seconds": round(times.user + times.system, 3),
            "rss_bytes": process.memory_info().rss,
            "cwd": process.cwd(),
        }


def _process_action(request: dict[str, Any]) -> dict[str, Any]:
    action = request["action"]
    if action == "list":
        processes = []
        for process in psutil.process_iter():
            try:
                if process.pid > 1 and process.pid != os.getpid() and process.uids().effective == os.geteuid():
                    processes.append(_process_info(process))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        query = str(request.get("query") or "").lower()
        processes = [item for item in processes if query in (item["name"] + item["command"]).lower()]
        return {"ok": True, "processes": processes[:50], "truncated": len(processes) > 50,
                "next_action": "Use an exact process_handle for inspect/signal; filter query if the list was truncated."}
    handle = str(request.get("process_handle") or "")
    if not re.fullmatch(r"[0-9]+:[0-9]+\.[0-9]{6}", handle):
        raise ShellError("invalid_process_handle", "Use the PID:creation-time handle from processes list.",
                         "List processes again; never guess a PID or reuse an old handle.")
    pid, created = handle.split(":")
    process = psutil.Process(int(pid))
    if process.pid <= 1 or process.pid == os.getpid() or process.uids().effective != os.geteuid():
        raise ShellError("protected_process", "The container init or another user's process cannot be targeted.",
                         "Select a process owned by this Developer from processes list.")
    if abs(process.create_time() - float(created)) > 0.000002:
        raise ShellError("stale_process_handle", "The PID has been reused; no signal was sent.",
                         "List processes again and inspect the new process before acting.")
    if action == "inspect":
        return {"ok": True, "process": _process_info(process),
                "children": [_process_info(child) for child in process.children(recursive=True)][:50]}
    signals = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "INT": signal.SIGINT}
    signal_name = request.get("signal", "TERM")
    if action != "signal" or signal_name not in signals:
        raise ShellError("invalid_process_action", "Use list, inspect, or signal with TERM, INT, or KILL.",
                         "Prefer TERM, then inspect; use KILL only if graceful shutdown fails.")
    process.send_signal(signals[signal_name])
    return {"ok": True, "process_handle": handle, "signal_sent": signal_name,
            "next_action": "Signal sent, not proof of exit. Inspect/list to confirm; use shell stop to stop an entire managed shell."}


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    try:
        if request.get("kind") == "processes":
            return _process_action(request)
        action = request.get("action")
        max_chars = int(request.get("max_output_chars", 6000))
        wait_seconds = float(request.get("wait_seconds", 0))
        if not 256 <= max_chars <= 24000 or not 0 <= wait_seconds <= 30:
            raise ShellError("invalid_limits", "Output must be 256..24000 chars; wait_seconds must be 0..30.",
                             "Use defaults for a concise, nonblocking read.")
        if action == "start":
            return _start(str(request.get("command") or "bash --noprofile --norc"), max_chars, wait_seconds)
        if action == "list":
            shells = []
            for path in sorted(_root().glob("sh-*/metadata.json"), reverse=True)[:50]:
                metadata = json.loads(path.read_text())
                shells.append({"shell_id": metadata["shell_id"], "command": metadata["command"][:300],
                               **_status(path.parent, metadata)})
            return {"ok": True, "shells": shells, "next_action": "Use read with a shell_id to reattach without restarting work."}
        shell_id = str(request.get("shell_id") or "")
        directory, metadata = _metadata(shell_id)
        if action == "send":
            if _status(directory, metadata)["status"] != "running":
                raise ShellError("shell_not_running", "The shell has exited or its container restarted.",
                                 "Read its retained output, or start a new shell; input was not sent.")
            text = str(request.get("text") or "")
            control = request.get("control", "enter")
            if len(text) > 8000 or control not in {None, *KEYS}:
                raise ShellError("invalid_input", "text is limited to 8000 chars; control must be enter/interrupt/eof/tab/escape or null.",
                                 "Send one prompt response at a time. Never send secrets through agent tools.")
            if text:
                _tmux("send-keys", "-l", "-t", shell_id, "--", text)
            if control:
                _tmux("send-keys", "-t", shell_id, KEYS[control])
        elif action == "stop":
            status = _status(directory, metadata)
            if status["status"] == "running":
                process = psutil.Process(status["pid"])
                children = process.children(recursive=True)
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                _tmux("kill-session", "-t", shell_id)
                (directory / "exit-code").write_text("137")
        elif action != "read":
            raise ShellError("invalid_action", "Use start, read, send, list, or stop.", "Use list to rediscover existing shells.")
        return _read(shell_id, request.get("cursor"), max_chars, wait_seconds)
    except ShellError as error:
        return {"ok": False, "error_code": error.code, "error": str(error), "next_action": error.next_action}
    except psutil.NoSuchProcess:
        return {"ok": False, "error_code": "process_exited", "error": "The process already exited; no signal was sent.",
                "next_action": "Read the shell result or list processes again."}
    except psutil.AccessDenied:
        return {"ok": False, "error_code": "process_access_denied", "error": "This Developer cannot inspect or signal that process.",
                "next_action": "List owned processes and select a current handle; do not change user or bypass permissions."}
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error_code": "shell_operation_failed", "error": str(error)[:1000],
                "next_action": "List shells/processes to inspect current state before retrying. Do not relaunch work blindly."}


if __name__ == "__main__":
    if sys.argv[1] == "--sink":
        _sink(Path(sys.argv[2]))
    elif sys.argv[1] == "--reply-file":
        reply_file = Path(sys.argv[2])
        reply_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        response = json.dumps(dispatch(json.loads(sys.argv[3])))
        temporary = reply_file.with_suffix(".tmp")
        temporary.write_text(response)
        temporary.replace(reply_file)
        print(response, flush=True)
    else:
        print(json.dumps(dispatch(json.loads(sys.argv[1]))))