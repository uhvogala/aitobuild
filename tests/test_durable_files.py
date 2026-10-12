import ast
import os
import stat
import tempfile
from pathlib import Path

import pytest

from aitobuild.durable_files import atomic_write_text

SOURCE = Path(__file__).resolve().parents[1] / "src" / "aitobuild"


def test_atomic_write_replaces_with_explicit_mode_and_leaves_no_temporaries(tmp_path) -> None:
    target = tmp_path / "journal.json"
    atomic_write_text(target, "one")
    assert target.read_text() == "one" and stat.S_IMODE(target.stat().st_mode) == 0o600
    atomic_write_text(target, "two", mode=0o640)
    assert target.read_text() == "two" and stat.S_IMODE(target.stat().st_mode) == 0o640
    assert sorted(path.name for path in tmp_path.iterdir()) == ["journal.json"]


def test_atomic_write_refuses_symlinked_target_and_parent(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    linked = tmp_path / "linked.json"
    linked.symlink_to(outside)
    with pytest.raises(ValueError, match="symlinks"):
        atomic_write_text(linked, "evil")
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (tmp_path / "alias").symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        atomic_write_text(tmp_path / "alias" / "journal.json", "evil")
    assert outside.read_text() == "keep" and list(real_dir.iterdir()) == []


def test_a_planted_tmp_symlink_cannot_redirect_the_write(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    (tmp_path / "journal.tmp").symlink_to(outside)
    (tmp_path / ".journal.json.tmp").symlink_to(outside)
    atomic_write_text(tmp_path / "journal.json", "new")
    assert outside.read_text() == "keep" and (tmp_path / "journal.json").read_text() == "new"


def test_a_failed_write_keeps_the_old_file_and_removes_the_temporary(tmp_path, monkeypatch) -> None:
    target = tmp_path / "journal.json"
    atomic_write_text(target, "old")

    def disk_full(descriptor: int) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(OSError, match="No space"):
        atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["journal.json"]


def test_every_durable_store_uses_the_shared_helper() -> None:
    copies = [path.name for path in SOURCE.rglob("*.py")
              if path.name not in {"durable_files.py", "shell_runtime.py"} and 'with_suffix(".tmp")' in path.read_text()]
    assert copies == []


def test_no_check_relies_on_assert_which_python_O_strips() -> None:
    offenders = [f"{path.relative_to(SOURCE)}:{node.lineno}" for path in sorted(SOURCE.rglob("*.py"))
                 for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Assert)]
    assert offenders == []


def test_atomic_write_fsyncs_the_directory_after_replace(tmp_path, monkeypatch):
    synced = []
    real = os.fsync

    def record(descriptor):
        synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real(descriptor)

    monkeypatch.setattr("aitobuild.durable_files.os.fsync", record)
    atomic_write_text(tmp_path / "state.json", "{}")
    assert synced == [False, True]  # the file, then its directory entry


@pytest.mark.parametrize("failing", ["fchmod", "fdopen"])
def test_atomic_write_closes_the_descriptor_when_setup_fails(failing, tmp_path, monkeypatch):
    opened, closed = [], []
    real_mkstemp, real_close = tempfile.mkstemp, os.close

    def mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        opened.append(descriptor)
        return descriptor, name

    def close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr("aitobuild.durable_files.tempfile.mkstemp", mkstemp)
    monkeypatch.setattr("aitobuild.durable_files.os.close", close)
    monkeypatch.setattr(f"aitobuild.durable_files.os.{failing}", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("setup")))
    with pytest.raises(OSError, match="setup"):
        atomic_write_text(tmp_path / "state.json", "{}")
    assert opened and closed == opened
    assert list(tmp_path.iterdir()) == []
