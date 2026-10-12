"""Pure logic of scripts/trials/pilot.py (no network, no containers)."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "trials"))

import pilot  # noqa: E402
from common import Recorder, check_policy  # noqa: E402

PLAN = {
    "planning_assignment_id": "a",
    "issues": [
        {"key": "stats-min-max", "dependencies": []},
        {"key": "stats-range", "dependencies": ["stats-min-max"]},
        {"key": "stats-docs", "dependencies": ["stats-range"]},
    ],
}


def args(*extra: str, tmp: Path):
    return pilot.parse_arguments(
        [*extra, "--work-root", str(tmp), "--allowed-issue-author", "uhvogala"]
    )


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
        ["resume", "--run-dir", "/tmp/x", "--brief-edit-what", "  ", "--brief-edit-why", "y"],
        ["resume", "--run-dir", "/tmp/x", "--brief-edit-what", "w", "--brief-edit-why", ""],
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
        pilot.parse_arguments(
            [
                "start",
                "--allowed-issue-author",
                "uhvogala",
                "--work-root",
                str(pilot.SERVICE_ROOT / "runs"),
            ]
        )


def test_resolve_automatic_stages() -> None:
    assert pilot.resolve_automatic_stages(["developer_tools", "developer_tools"]) == (
        "developer_tools",
    )
    with pytest.raises(ValueError):
        pilot.resolve_automatic_stages(["review_publication"])


def policy(**modes: Any) -> SimpleNamespace:
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


def test_open_merge_point_refuses_handoff() -> None:
    assert pilot.next_handoff(PLAN, [{"issue_key": None, "state": "OPEN"}]) is None
    merged = {"issue_key": None, "state": "MERGED"}
    assert pilot.next_handoff(PLAN, [merged, {"issue_key": "stats-range", "state": "OPEN"}]) is None


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


def test_closed_merge_point_blocks() -> None:
    closed = {"issue_key": None, "state": "CLOSED"}
    assert pilot.merged_keys(PLAN, [closed]) == set()
    assert pilot.next_handoff(PLAN, [closed]) is None
    point = {"pull_number": 1, "head_sha": "a" * 40}
    assert pilot.check_merge_point(point, {"state": "CLOSED", "headRefOid": "a" * 40}) == "CLOSED"


POINT = {"pull_number": 53, "head_sha": "a" * 40}


def test_merge_point_merged_at_reviewed_head() -> None:
    pull = {"state": "MERGED", "headRefOid": "a" * 40, "mergeCommit": {"oid": "c" * 40}}
    assert pilot.check_merge_point(POINT, pull) == "MERGED"
    assert pilot.check_merge_point(POINT, {"state": "OPEN", "headRefOid": "b" * 40}) == "OPEN"


def test_merge_point_merged_at_different_head_fails() -> None:
    pull = {"state": "MERGED", "headRefOid": "b" * 40, "mergeCommit": {"oid": "c" * 40}}
    with pytest.raises(pilot.MergePointError, match="not the reviewed head"):
        pilot.check_merge_point(POINT, pull)


def test_merge_point_without_merge_commit_fails() -> None:
    with pytest.raises(pilot.MergePointError, match="merge commit"):
        pilot.check_merge_point(
            POINT, {"state": "MERGED", "headRefOid": "a" * 40, "mergeCommit": None}
        )


def task_id(key: str) -> str:
    return "planned-issue-" + sha256(f"a:{key}".encode()).hexdigest()


def test_classify_planned_children() -> None:
    issue = pilot.planned_issue_for(task_id("stats-range"), PLAN)
    assert issue is not None and issue["key"] == "stats-range"
    assert pilot.classify_planned_child(task_id("stats-min-max"), 200, PLAN) == "deliver"
    assert pilot.classify_planned_child(task_id("stats-range"), 409, PLAN) == "blocked"


@pytest.mark.parametrize(
    ("task", "code"),
    [("stats-min-max", 409), ("stats-range", 500), ("unknown", 409), ("stats-range", 404)],
)
def test_other_child_answers_fail(task: str, code: int) -> None:
    with pytest.raises(RuntimeError):
        pilot.classify_planned_child(task_id(task), code, PLAN)


def test_brief_edit_action() -> None:
    action = pilot.brief_edit_action(
        "stats-range", "added empty-input behaviour", "ReviewBot empty-input nit"
    )
    assert action["kind"] == "brief_edit" and action["issue_key"] == "stats-range" and action["at"]


def test_recorder_save_uses_atomic_write_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aitobuild.durable_files as durable_files

    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        durable_files, "atomic_write_text", lambda path, content, **_: calls.append((path, content))
    )
    recorder = Recorder(tmp_path)
    recorder.report["result"] = "ok"
    recorder.save()
    assert len(calls) == 1 and calls[0][0] == tmp_path / "report.json"
    assert json.loads(calls[0][1])["result"] == "ok"


def test_start_without_merge_point_fails() -> None:
    with pytest.raises(RuntimeError, match="no merge point"):
        pilot.require_merge_points({"blocked_dependents": ["dp-1"]})
    pilot.require_merge_points({"merge_points": [{"pull_number": 1}]})


SIBLINGS = {
    "planning_assignment_id": "a",
    "issues": [
        {"key": "base", "dependencies": []},
        {"key": "left", "dependencies": ["base"]},
        {"key": "right", "dependencies": ["base"]},
    ],
}


def test_open_merge_point_is_the_only_blocker() -> None:
    # Without the open-merge-point guard "right" would be handed off while "left" is still unmerged.
    points: list[dict[str, Any]] = [
        {"issue_key": None, "state": "MERGED"},
        {"issue_key": "left", "state": "OPEN"},
    ]
    assert pilot.next_handoff(SIBLINGS, points) is None
    points[1]["state"] = "MERGED"
    assert pilot.next_handoff(SIBLINGS, points) == "right"


def git_repo(path: Path) -> tuple[str, str]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    run("init", "-q", "-b", "master")
    run("commit", "-q", "--allow-empty", "-m", "base")
    on_base = run("rev-parse", "HEAD")
    run("checkout", "-q", "-b", "side")
    run("commit", "-q", "--allow-empty", "-m", "side")
    off_base = run("rev-parse", "HEAD")
    return on_base, off_base


def test_merge_commit_must_be_on_base(tmp_path: Path) -> None:
    on_base, off_base = git_repo(tmp_path)
    pilot.require_merge_commit_on_base(tmp_path, on_base, "master")
    with pytest.raises(pilot.MergePointError, match="not on master"):
        pilot.require_merge_commit_on_base(tmp_path, off_base, "master")


def test_start_with_only_blocked_children_fails() -> None:
    listing = [{"preview_id": "dp-range", "bundle": {"task_id": task_id("stats-range")}}]
    report: dict[str, Any] = {"plan": PLAN}
    delivered: list[str] = []
    with pytest.raises(RuntimeError, match="no merge point"):
        pilot.deliver_planned_children(
            listing, report, status_of=lambda _: 409, deliver=delivered.append
        )
    assert report["blocked_dependents"] == ["dp-range"] and delivered == []


def test_start_delivers_admissible_child() -> None:
    listing = [
        {"preview_id": "dp-min", "bundle": {"task_id": task_id("stats-min-max")}},
        {"preview_id": "dp-range", "bundle": {"task_id": task_id("stats-range")}},
        {"preview_id": "dp-plan", "bundle": {"task_id": "pm-planning-x"}},
    ]
    report: dict[str, Any] = {"plan": PLAN}

    def deliver(preview_id: str) -> None:
        report.setdefault("merge_points", []).append({"preview_id": preview_id})

    pilot.deliver_planned_children(
        listing, report, status_of=lambda p: 409 if p == "dp-range" else 200, deliver=deliver
    )
    assert [p["preview_id"] for p in report["merge_points"]] == ["dp-min"]


def test_policy_rejects_unknown_key() -> None:
    with pytest.raises(SystemExit):
        check_policy(policy(max_unknown_thing=1), ("task_admission", "developer_tools"))


def test_policy_accepts_max_automatic_admissions() -> None:
    result = check_policy(
        policy(max_automatic_admissions=500), ("task_admission", "developer_tools")
    )
    assert result["modes"]["max_automatic_admissions"] == 500


def test_planned_issue_hash_matches_service() -> None:
    from aitobuild.developer_isolation import default_developer_isolation_policy
    from aitobuild.developer_isolation import build_developer_task_bundle
    from aitobuild.organization_planning import ManagedPlanning

    developer_policy = build_developer_task_bundle(
        task_id="p",
        objective="o",
        acceptance_criteria=["c"],
        constraints=[],
        context_files=[],
        policy=default_developer_isolation_policy(),
    ).to_payload()["policy"]
    scope = SimpleNamespace(
        repository="o/r", repository_id=1, base_branch="master", base_revision="a" * 40
    )
    draft = SimpleNamespace(
        key="stats-range", objective="Add value_range", acceptance_criteria=["works"]
    )
    plan = SimpleNamespace(
        proposal=SimpleNamespace(issues=[draft]), pins={"developer_policy": developer_policy}
    )
    effect = SimpleNamespace(
        key="stats-range", receipt={"number": 51, "issue_id": 9, "title": "t", "body": "b"}
    )
    service = SimpleNamespace(_target=lambda _plan: scope)
    developer_payload: Any = ManagedPlanning._developer_payload
    payload = developer_payload(
        service,
        plan,
        SimpleNamespace(assignment_id="a"),
        effect,  # type: ignore[arg-type]
    )
    issue = pilot.planned_issue_for(payload["task_id"], PLAN)
    assert issue is not None and issue["key"] == "stats-range"


def test_merged_keys_ignores_keyed_closed_point() -> None:
    points: list[dict[str, Any]] = [
        {"issue_key": None, "state": "MERGED"},
        {"issue_key": "stats-range", "state": "CLOSED"},
    ]
    assert pilot.merged_keys(PLAN, points) == {"stats-min-max"}


def seed_clone(tmp_path: Path) -> tuple[Path, Path]:
    def run(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    upstream, seed = tmp_path / "upstream", tmp_path / "seed"
    upstream.mkdir()
    run(upstream, "init", "-q", "-b", "master")
    run(upstream, "commit", "-q", "--allow-empty", "-m", "base")
    subprocess.run(
        ["git", "clone", "-q", "--branch", "master", str(upstream), str(seed)], check=True
    )
    run(upstream, "commit", "-q", "--allow-empty", "-m", "merged PR")
    return upstream, seed


def test_refresh_seed_base_with_master_checked_out(tmp_path: Path) -> None:
    upstream, seed = seed_clone(tmp_path)
    merged = pilot.git(upstream, "rev-parse", "HEAD")
    assert pilot.git(seed, "symbolic-ref", "--short", "HEAD") == "master"
    assert pilot.refresh_seed_base(seed, "master") == merged
    assert pilot.git(seed, "rev-parse", "HEAD") == merged


def test_refresh_seed_base_refuses_diverged_seed(tmp_path: Path) -> None:
    _, seed = seed_clone(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "local",
        ],
        check=True,
    )
    with pytest.raises(pilot.SeedError, match="(?s)diverged.*(fatal|forward)"):
        pilot.refresh_seed_base(seed, "master")


def test_refresh_seed_base_refuses_dirty_seed(tmp_path: Path) -> None:
    _, seed = seed_clone(tmp_path)
    (seed / "stray.txt").write_text("x")
    with pytest.raises(pilot.SeedError, match="uncommitted"):
        pilot.refresh_seed_base(seed, "master")


def test_refresh_seed_base_refuses_other_checkout(tmp_path: Path) -> None:
    upstream, seed = seed_clone(tmp_path)
    subprocess.run(["git", "-C", str(seed), "checkout", "-q", "-b", "other"], check=True)
    with pytest.raises(pilot.SeedError, match="must have master checked out"):
        pilot.refresh_seed_base(seed, "master")
    # the seed's own branch "other" must not have been fast-forwarded either
    assert pilot.git(seed, "rev-parse", "HEAD") != pilot.git(upstream, "rev-parse", "HEAD")


def test_pilot_requires_issue_author(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        pilot.parse_arguments(["start", "--work-root", str(tmp_path)])


@pytest.mark.parametrize("value", ["", "  ", "*", "uh*", "a,b", "uhvogala bot", "-x", "a" * 40])
def test_pilot_rejects_bad_issue_author(value: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        pilot.parse_arguments(
            ["start", "--work-root", str(tmp_path), "--allowed-issue-author", value]
        )


def test_early_failure_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*_: str) -> None:
        raise RuntimeError("gh unavailable")

    monkeypatch.setattr(pilot, "gh_json", no_network)
    monkeypatch.setattr(
        pilot, "datetime", type("Frozen", (), {"now": staticmethod(lambda tz: _Stamp())})
    )
    with pytest.raises(RuntimeError, match="gh unavailable"):
        pilot.main(["start", "--allowed-issue-author", "uhvogala", "--work-root", str(tmp_path)])
    report = json.loads((tmp_path / "artifacts" / "trial4-frozen" / "report.json").read_text())
    assert report["result"] == "fail" and "gh unavailable" in report["failure"]
    assert report["ended_at"] and report["sessions"][-1]["ended_at"]


class _Stamp:
    def strftime(self, _: str) -> str:
        return "frozen"
