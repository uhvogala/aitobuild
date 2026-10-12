"""Every trial script's repository source must load through aitobuild's real configuration in automatic mode."""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))

import common  # noqa: E402
import correction_cycle  # noqa: E402
import followups  # noqa: E402
import pilot  # noqa: E402

SCRIPTS = {
    "correction_cycle": correction_cycle.VERIFY_COMMAND,
    "followups": followups.VERIFY_COMMAND,
    "pilot": pilot.followups.VERIFY_COMMAND,
}


@pytest.fixture
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    path = tmp_path / "seed"
    path.mkdir()
    run = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*run, "init", "-q", "-b", "master"], check=True)
    subprocess.run([*run, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    return path


def load(seed: Path, tmp_path: Path, verify: str, author: str | None):
    from aitobuild.config import load_config

    base = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    source = common.repository_source(
        repository=common.DEFAULT_REPOSITORY,
        repository_id=1405327159,
        path=seed,
        verification_command=verify,
        base_revision=base,
        issue_author=author,
    )
    common.configure_trial_environment(
        automatic_stages=common.ALLOWED_AUTOMATIC_STAGES,
        values=common.trial_environment_values(
            source=source,
            run_dir=tmp_path / "run",
            endpoint="https://e.invalid/",
            model="m",
            invoke_timeout=600,
            secret="s" * 48,
            token="t" * 48,
        ),
    )
    return load_config()


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_script_config_loads_in_automatic_mode(
    script: str, seed: Path, tmp_path: Path, isolated_environment: None
) -> None:
    config = load(seed, tmp_path, SCRIPTS[script], "uhvogala")
    common.check_policy(config.approvals, common.ALLOWED_AUTOMATIC_STAGES)
    (source,) = config.developer.repository_sources
    assert source.allowed_issue_authors == ("uhvogala",) and not source.required_issue_labels


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_script_config_without_author_is_refused(
    script: str, seed: Path, tmp_path: Path, isolated_environment: None
) -> None:
    with pytest.raises(ValueError, match="allowed_issue_authors"):
        load(seed, tmp_path, SCRIPTS[script], None)


@pytest.mark.parametrize("module", [correction_cycle, followups])
def test_automatic_script_refuses_to_start_without_author(
    module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["x", "--approval-mode", "automatic", "--work-root", str(tmp_path)]
    )
    with pytest.raises(SystemExit, match="allowed-issue-author"):
        module.main()


@pytest.mark.parametrize("value", ["", " ", "*", "uh?", "a,b", "a b", "-lead", "x" * 40])
def test_issue_author_type_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        common.issue_author(value)


def test_issue_author_type_accepts_login() -> None:
    assert common.issue_author("uhvogala") == "uhvogala"


def test_require_issue_author() -> None:
    common.require_issue_author((), None)
    common.require_issue_author(common.ALLOWED_AUTOMATIC_STAGES, "uhvogala")
    with pytest.raises(SystemExit):
        common.require_issue_author(("task_admission",), None)


def test_record_early_failure(tmp_path: Path) -> None:
    recorder = common.Recorder(tmp_path)
    recorder.report["sessions"] = [{"command": "resume", "started_at": "t"}]
    common.record_early_failure(recorder, ValueError("boom"))
    assert (
        recorder.report["result"] == "failed" and recorder.report["failure"] == "ValueError: boom"
    )
    assert recorder.report["sessions"][-1]["ended_at"] == recorder.report["ended_at"]
