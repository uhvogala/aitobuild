"""One-use exact publish approvals shared by correction and managed publication.

A snapshot pins everything a push will do (target repository/PR/branch/base, pinned
parent head, review receipt, tree fingerprint, per-path blobs and the unified diff).
Operators approve the snapshot digest; the push consumes it exactly once. Any
recomputed snapshot that differs invalidates the approval.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any

from filelock import FileLock

_STATES = frozenset({"pending", "consuming", "consumed", "invalidated"})


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    content = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(content.encode("utf-8")).hexdigest()


class PublishApprovalStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, preview_id: str) -> Path:
        if not isinstance(preview_id, str) or not preview_id:
            raise ValueError("Publish approval requires a preview identity")
        path = self._directory / (sha256(preview_id.encode()).hexdigest() + ".json")
        if path.is_symlink() or (self._directory.exists() and self._directory.resolve() != self._directory.absolute()):
            raise ValueError("Publish approvals cannot follow symlinks")
        return path

    def _read(self, path: Path, preview_id: str) -> dict[str, Any] | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1 or data.get("preview_id") != preview_id
                or data.get("state") not in _STATES or not isinstance(data.get("snapshot"), dict)
                or not isinstance(data.get("digest"), str) or re.fullmatch(r"[0-9a-f]{64}", data["digest"]) is None
                or snapshot_digest(data["snapshot"]) != data["digest"]
                or data["snapshot"].get("preview_id") != preview_id):
            raise ValueError("Invalid persisted publish approval")
        if data["state"] in {"consuming", "consumed"} and (
                not isinstance(data.get("approved_by"), str) or not data["approved_by"]):
            raise ValueError("Consumed publish approval lacks its approving operator")
        return data

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        if temporary.is_symlink():
            raise ValueError("Publish approvals cannot follow symlinks")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        descriptor = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def get(self, preview_id: str) -> dict[str, Any] | None:
        path = self._path(preview_id)
        with FileLock(str(path) + ".lock", timeout=10):
            return self._read(path, preview_id)

    def stage(self, preview_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Save a pending exact snapshot; a changed snapshot invalidates any unconsumed approval."""
        if snapshot.get("preview_id") != preview_id:
            raise ValueError("Publish snapshot must bind its own preview identity")
        digest = snapshot_digest(snapshot)
        path = self._path(preview_id)
        with FileLock(str(path) + ".lock", timeout=10):
            current = self._read(path, preview_id)
            if current is not None and current["state"] in {"consuming", "consumed"}:
                if current["digest"] != digest:
                    raise PermissionError("Publish approval was already consumed; a new exact change needs a new task")
                return current
            if current is not None and current["digest"] == digest and current["state"] == "pending":
                return current
            data = {"version": 1, "preview_id": preview_id, "digest": digest, "snapshot": snapshot,
                    "state": "pending", "staged_at": datetime.now(tz=UTC).isoformat(),
                    "replaced_digest": current["digest"] if current is not None else None}
            self._write(path, data)
            return data

    def begin_consume(self, preview_id: str, *, digest: str, recomputed_digest: str, actor_id: str) -> dict[str, Any]:
        """Approve and consume in one step; only an interrupted identical consume may resume."""
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise PermissionError("Publish approval requires an operator identity")
        path = self._path(preview_id)
        with FileLock(str(path) + ".lock", timeout=10):
            current = self._read(path, preview_id)
            if current is None:
                raise PermissionError("Publish approval requires a staged exact snapshot")
            if current["digest"] != digest:
                raise PermissionError("Approved digest differs from the staged exact snapshot")
            if recomputed_digest != digest:
                if current["state"] == "pending":
                    self._write(path, {**current, "state": "invalidated",
                                       "invalidated_at": datetime.now(tz=UTC).isoformat()})
                raise PermissionError("Exact change drifted after staging; approval is invalidated")
            if current["state"] == "consumed":
                raise PermissionError("Publish approval was already consumed")
            if current["state"] == "invalidated":
                raise PermissionError("Publish approval was invalidated; stage the exact change again")
            if current["state"] == "consuming":
                if current["approved_by"] != actor_id.strip():
                    raise PermissionError("Interrupted publish approval belongs to another operator")
                return current
            data = {**current, "state": "consuming", "approved_by": actor_id.strip(),
                    "approved_at": datetime.now(tz=UTC).isoformat()}
            self._write(path, data)
            return data

    def finish_consume(self, preview_id: str, *, digest: str, head_sha: str | None, error: str | None = None) -> None:
        path = self._path(preview_id)
        with FileLock(str(path) + ".lock", timeout=10):
            current = self._read(path, preview_id)
            if current is None or current["digest"] != digest or current["state"] not in {"consuming", "consumed"}:
                raise PermissionError("Publish approval consumption lost its exact snapshot")
            if current["state"] == "consumed":
                return
            self._write(path, {**current, "state": "consumed", "consumed_head_sha": head_sha, "error": error,
                               "consumed_at": datetime.now(tz=UTC).isoformat()})
