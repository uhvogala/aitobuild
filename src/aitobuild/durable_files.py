"""The one durable write path for aitobuild journals, receipts and stores."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def refuse_symlinks(path: Path) -> None:
    """Refuse a symlinked target or parent directory; journals must never write through links."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"Durable writes cannot follow symlinks: {path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"Durable write target is not a regular file: {path}")


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Write `content` so readers see the old or the new file, never a partial one.

    A uniquely named temporary file in the same directory (created with O_EXCL, so a planted
    symlink cannot redirect it) is written, fsynced and given an explicit mode, then atomically
    replaces the target, and the directory entry is fsynced.
    """
    refuse_symlinks(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        refuse_symlinks(path)
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    sync_directory(path.parent)
