"""One-use exact publish approvals shared by correction and managed publication.

A snapshot pins everything a push will do (target repository/PR/branch/base, pinned
parent head, review receipt, tree fingerprint, per-path blobs and the unified diff).
Each staging draws a fresh nonce, so the approval digest an operator signs binds both
the snapshot content and that one staging: re-staging identical content after an
invalidation yields a new digest and never revives the old one. The push consumes the
approval exactly once; any recomputed snapshot that differs invalidates it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
from typing import Annotated, Any, Literal, Self

from filelock import FileLock
from pydantic import AwareDatetime, Field, JsonValue, StrictStr, ValidationError, model_validator

from aitobuild.organization import DefinitionModel, _sync_directory

_Hex64 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_IDENTITY = ("version", "preview_id", "snapshot", "content_digest", "nonce", "digest", "staged_at", "replaced_digest")
_APPROVAL = ("approved_by", "approved_at")


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    content = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(content.encode("utf-8")).hexdigest()


def approval_digest(content_digest: str, nonce: str) -> str:
    return sha256(f"aitobuild-publish-approval:{content_digest}:{nonce}".encode()).hexdigest()


class PublishApproval(DefinitionModel):
    version: Literal[1] = 1
    preview_id: Annotated[StrictStr, Field(min_length=1)]
    snapshot: dict[str, JsonValue]
    content_digest: _Hex64
    nonce: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]
    digest: _Hex64
    state: Literal["pending", "consuming", "consumed", "invalidated"]
    staged_at: AwareDatetime
    replaced_digest: _Hex64 | None = None
    approved_by: Annotated[StrictStr, Field(min_length=1)] | None = None
    approved_at: AwareDatetime | None = None
    invalidated_at: AwareDatetime | None = None
    invalidation_reason: Annotated[StrictStr, Field(min_length=1)] | None = None
    consumed_at: AwareDatetime | None = None
    consumed_head_sha: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40}$")] | None = None
    error: Annotated[StrictStr, Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def validate_identity_and_state(self) -> Self:
        if self.snapshot.get("preview_id") != self.preview_id or snapshot_digest(self.snapshot) != self.content_digest:
            raise ValueError("Publish approval snapshot differs from its preview identity or content digest")
        if approval_digest(self.content_digest, self.nonce) != self.digest:
            raise ValueError("Publish approval digest does not bind its snapshot and staging nonce")
        approved = self.approved_by is not None and self.approved_by.strip() == self.approved_by and self.approved_at is not None
        invalidated = self.invalidated_at is not None and self.invalidation_reason is not None
        consumed = self.consumed_at is not None and (self.consumed_head_sha is None) != (self.error is None)
        untouched = (self.approved_by, self.approved_at, self.invalidated_at, self.invalidation_reason,
                     self.consumed_at, self.consumed_head_sha, self.error) == (None,) * 7
        valid = {
            "pending": untouched,
            "invalidated": invalidated and (self.approved_by, self.approved_at, self.consumed_at, self.consumed_head_sha,
                                            self.error) == (None,) * 5,
            "consuming": approved and (self.invalidated_at, self.invalidation_reason, self.consumed_at,
                                       self.consumed_head_sha, self.error) == (None,) * 5,
            "consumed": approved and consumed and (self.invalidated_at, self.invalidation_reason) == (None, None),
        }[self.state]
        if not valid:
            raise ValueError("Publish approval state fields are inconsistent")
        return self


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _advance(current: PublishApproval, changes: dict[str, Any]) -> PublishApproval:
    """Build the next state through full validation (model_copy would skip it)."""
    return PublishApproval.model_validate(current.model_dump() | changes)


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

    def _read(self, path: Path, preview_id: str) -> PublishApproval | None:
        if not path.exists():
            return None
        try:
            approval = PublishApproval.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as error:
            raise ValueError("Invalid persisted publish approval") from error
        if approval.preview_id != preview_id:
            raise ValueError("Persisted publish approval belongs to another preview")
        return approval

    @staticmethod
    def _check_transition(original: PublishApproval | None, updated: PublishApproval) -> None:
        """Mirror the review-journal rule: pins are immutable and states only move forward."""
        def same(fields: tuple[str, ...]) -> bool:
            return original is not None and all(getattr(original, name) == getattr(updated, name) for name in fields)

        replacement = (updated.state == "pending" and updated.nonce != (original.nonce if original else None)
                       and updated.replaced_digest == (original.digest if original else None))
        allowed = {
            None: replacement,
            "pending": replacement and original is not None and updated.content_digest != original.content_digest
            or updated.state in {"consuming", "invalidated"} and same(_IDENTITY),
            "invalidated": replacement,
            "consuming": updated.state == "consumed" and same(_IDENTITY + _APPROVAL),
            "consumed": False,
        }[original.state if original else None]
        if not allowed:
            raise PermissionError("Publish approval pins are immutable and its state only moves forward")

    def _save(self, path: Path, original: PublishApproval | None, updated: PublishApproval) -> PublishApproval:
        self._check_transition(original, updated)
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        if temporary.is_symlink():
            raise ValueError("Publish approvals cannot follow symlinks")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(updated.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _sync_directory(self._directory)
        return updated

    def get(self, preview_id: str) -> PublishApproval | None:
        path = self._path(preview_id)
        with FileLock(str(path) + ".lock", timeout=10):
            return self._read(path, preview_id)

    def stage(self, preview_id: str, snapshot: dict[str, Any]) -> PublishApproval:
        """Save a pending exact snapshot; a changed snapshot replaces any unconsumed approval."""
        if snapshot.get("preview_id") != preview_id:
            raise ValueError("Publish snapshot must bind its own preview identity")
        content = snapshot_digest(snapshot)
        path = self._path(preview_id)
        with FileLock(str(path) + ".lock", timeout=10):
            current = self._read(path, preview_id)
            if current is not None and current.state in {"consuming", "consumed"}:
                if current.content_digest != content:
                    raise PermissionError("Publish approval was already consumed; a new exact change needs a new task")
                return current
            if current is not None and current.state == "pending" and current.content_digest == content:
                return current
            nonce = secrets.token_hex(16)
            staged = PublishApproval(
                preview_id=preview_id, snapshot=snapshot, content_digest=content, nonce=nonce,
                digest=approval_digest(content, nonce), state="pending", staged_at=_now(),
                replaced_digest=current.digest if current is not None else None,
            )
            return self._save(path, current, staged)

    def begin_consume(self, preview_id: str, *, digest: str, recomputed_content_digest: str, actor_id: str) -> PublishApproval:
        """Approve and consume in one step; only an interrupted identical consume may resume."""
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise PermissionError("Publish approval requires an operator identity")
        path = self._path(preview_id)
        with FileLock(str(path) + ".lock", timeout=10):
            current = self._read(path, preview_id)
            if current is None:
                raise PermissionError("Publish approval requires a staged exact snapshot")
            if current.digest != digest:
                raise PermissionError("Approved digest differs from the staged exact snapshot")
            if recomputed_content_digest != current.content_digest:
                if current.state == "pending":
                    self._save(path, current, _advance(current, {
                        "state": "invalidated", "invalidated_at": _now(), "invalidation_reason": "drift"}))
                raise PermissionError("Exact change drifted after staging; approval is invalidated")
            if current.state == "consumed":
                raise PermissionError("Publish approval was already consumed")
            if current.state == "invalidated":
                raise PermissionError("Publish approval was invalidated; stage the exact change again")
            if current.state == "consuming":
                if current.approved_by != actor_id.strip():
                    raise PermissionError("Interrupted publish approval belongs to another operator")
                return current
            return self._save(path, current, _advance(current, {
                "state": "consuming", "approved_by": actor_id.strip(), "approved_at": _now()}))

    def finish_consume(self, preview_id: str, *, digest: str, head_sha: str | None, error: str | None = None) -> PublishApproval:
        path = self._path(preview_id)
        with FileLock(str(path) + ".lock", timeout=10):
            current = self._read(path, preview_id)
            if current is None or current.digest != digest or current.state not in {"consuming", "consumed"}:
                raise PermissionError("Publish approval consumption lost its exact snapshot")
            if current.state == "consumed":
                return current
            return self._save(path, current, _advance(current, {
                "state": "consumed", "consumed_head_sha": head_sha, "error": error, "consumed_at": _now()}))

    def invalidate(self, preview_id: str, *, reason: str) -> PublishApproval | None:
        """Retire an unconsumed approval; an in-flight consume can never be invalidated."""
        path = self._path(preview_id)
        with FileLock(str(path) + ".lock", timeout=10):
            current = self._read(path, preview_id)
            if current is None or current.state in {"invalidated", "consumed"}:
                return current
            if current.state == "consuming":
                raise PermissionError("Publish approval is mid-consume; reconcile the push instead")
            return self._save(path, current, _advance(current, {
                "state": "invalidated", "invalidated_at": _now(), "invalidation_reason": reason}))
