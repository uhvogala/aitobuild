"""Pure logic of scripts/trials/pilot.py (no network, no containers)."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))

import pilot  # noqa: E402
from common import check_policy  # noqa: E402

PLAN = {
    "planning_assignment_id": "a",
    "issues": [
        {"key": "stats-min-max", "dependencies": []},
        {"key": "stats-range", "dependencies": ["stats-min-max"]},
        {"key": "stats-docs", "dependencies": ["stats-range"]},
    ],
}


def args(*extra: str, tmp: Path):
    return pilot.parse_arguments([*extra, "--work-root", str(tmp)])


def test_start_defaults_lock_repository_and_stages(tmp_path: Path) -> None:
    parsed = args("start", tmp=tmp_path)
    assert parsed.repository == "uhvogala/aitobuild_example"
    assert parsed.automatic_stages == ("task_admission", "developer_tools")


@pytest.mark.parametrize(
    "argv",
    [
        ["start", "--repository", "someone/else"],
        ["start", "--run-dir", "/tmp/x"],
        ["resume"],
        ["start", "--automatic-stage", "draft_publication"],
        ["start", "--automatic-stage", "merge"],
        ["start", "--automatic-stage", "correction"],
        ["start", "--poll-seconds", "0"],
        ["start", "--stage-timeout", "-1"],
        ["resume", "--run-dir", "/tmp/x", "--brief-edit-what", "w"],
        ["start", "--brief-edit-what", "w", "--brief-edit-why", "y"],
    ],
)
def test_refused_arguments(argv: list[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        args(*argv, tmp=tmp_path)


def test_override_and_resume_accepted(tmp_path: Path) -> None:
    parsed = args(
        "resume",
        "--run-dir",
        "/tmp/x",
        "--repository",
        "o/r",
        "--allow-other-repository",
        "--brief-edit-what",
        "w",
        "--brief-edit-why",
        "y",
        tmp=tmp_path,
    )
    assert parsed.repository == "o/r" and parsed.brief_edit_why == "y"


def test_work_root_inside_service_refused() -> None:
    with pytest.raises(SystemExit):
        pilot.parse_arguments(["start", "--work-root", str(pilot.SERVICE_ROOT / "runs")])


def test_resolve_automatic_stages() -> None:
    assert pilot.resolve_automatic_stages(["developer_tools", "developer_tools"]) == (
        "developer_tools",
    )
    with pytest.raises(ValueError):
        pilot.resolve_automatic_stages(["review_publication"])


def policy(**modes: str) -> SimpleNamespace:
    base = {
        "task_admission": "automatic",
        "developer_tools": "automatic",
        "draft_publication": "human",
        "review_publication": "human",
        "correction": "human",
        "merge": "disabled",
    }
    full = {stage: base.get(stage, "human") for stage in pilot.ALL_STAGES} | modes
    return SimpleNamespace(model_dump=lambda mode: full, revision="r1")


def test_policy_check() -> None:
    stages = ("task_admission", "developer_tools")
    assert check_policy(policy(), stages)["revision"] == "r1"
    for bad in (
        {"merge": "human"},
        {"draft_publication": "automatic"},
        {"correction": "automatic"},
    ):
        with pytest.raises(SystemExit):
            check_policy(policy(**bad), stages)


def test_next_handoff_waits_for_open_merge_point() -> None:
    assert pilot.next_handoff(PLAN, [{"issue_key": None, "state": "OPEN"}]) is None


def test_next_handoff_walks_the_chain() -> None:
    first = {"issue_key": None, "state": "MERGED"}
    assert pilot.merged_keys(PLAN, [first]) == {"stats-min-max"}
    assert pilot.next_handoff(PLAN, [first]) == "stats-range"
    second = {"issue_key": "stats-range", "state": "MERGED"}
    assert pilot.next_handoff(PLAN, [first, second]) == "stats-docs"
    assert pilot.next_handoff(PLAN, [first, {**second, "state": "OPEN"}]) is None
    assert (
        pilot.next_handoff(PLAN, [first, second, {"issue_key": "stats-docs", "state": "MERGED"}])
        is None
    )


def test_closed_unmerged_point_blocks() -> None:
    assert pilot.next_handoff(PLAN, [{"issue_key": None, "state": "CLOSED"}]) is None


def test_brief_edit_action() -> None:
    action = pilot.brief_edit_action(
        "stats-range", "added empty-input behaviour", "ReviewBot empty-input nit"
    )
    assert action["kind"] == "brief_edit" and action["issue_key"] == "stats-range" and action["at"]
