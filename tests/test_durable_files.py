import ast
import os
import stat
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
