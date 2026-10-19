"""Trial 4 (small M5 pilot): a PM plan of dependent issues, delivered one human merge point at a time.

``start`` offers a PM planning request (issue creation and dependency links are approved by the operator through the
authenticated endpoints), then delivers and reviews every independent planned child up to a draft PR and STOPS at
that merge point. The service merge stage stays disabled: a person merges (ReviewBot's okay on the exact head in
Common stands in for a human merge review and is recorded in ``report.metrics``).

``resume --run-dir`` restarts the service on the same saved state after the merge, refreshes every merge point
from GitHub, provisions the merged base into the local seed (operator action), offers the next dependent issue whose
prerequisites are all merged through ``POST /internal/organization/dependencies/offer``, delivers and reviews it and
stops at the next merge point. Edits a person made to the PM-written brief before resuming are operator steps:
record them with ``--brief-edit-what`` and ``--brief-edit-why``.

Safety locks are shared with the other harnesses: only ``task_admission``/``developer_tools`` may be automatic,
publication and correction stay human, merge is disabled, and ``--repository`` is locked to the disposable example
unless ``--allow-other-repository`` is given. Like the other harnesses, every pending human-stage request is
approved by this script without a person looking at it; each approval is recorded for audit.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import followups  # noqa: E402 - script-local shared module
from common import (  # noqa: E402
    record_early_failure,
    add_issue_author_argument,
    repository_source,
    require_issue_author,
    trial_environment_values,
    ALL_STAGES,
    ALLOWED_AUTOMATIC_STAGES,
    DEFAULT_ENDPOINT,
    DEFAULT_REPOSITORY,
    HELSINKI,
    Operator,
    Recorder,
    check_policy,
    collect_evidence,
    pin_session_policy,
    runs_needing_policy_override,
    configure_trial_environment,
    gh_json,
    git,
    now,
    positive,
    prepare_docker_wrapper,
)

SERVICE_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCY_EVENT = "github.issue.ready"
MERGE_GATE = "ReviewBot okays the exact head in Common (a bot stands in for the human merge review); a person merges"
PLANNER_INSTRUCTIONS = (
    "Plan the operator's objective for the pinned target as exactly the issues and dependencies it names. Inspect "
    "the target first. Issue keys must be lowercase identifiers. Each issue needs a precise objective and checkable "
    "acceptance criteria limited to src/ and tests/."
)
OBJECTIVE = (
    "Add a small statistics feature to src/demo_app in three issues with keys stats-min-max, stats-range and "
    "stats-docs. stats-min-max: `minimum(values: list[int]) -> int` and `maximum(values: list[int]) -> int` in a "
    "new src/demo_app/stats.py with tests/test_stats.py; stats-range: `value_range(values: list[int]) -> int` in "
    "stats.py built on minimum/maximum with tests; stats-docs: add docstrings with usage examples to every function "
    "in src/demo_app/stats.py and a test that every public stats function has a docstring. stats-range depends on "
    "stats-min-max and stats-docs depends on stats-range. M5 pilot {trial_id}."
)


# --- pure logic (covered by tests/test_trial_pilot.py) ---------------------------------------------------------


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("command", choices=("start", "resume"))
    parser.add_argument("--run-dir", type=Path, help="Saved run to resume (resume only)")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--allow-other-repository", action="store_true")
    parser.add_argument("--work-root", type=Path, default=Path("/workspace/trials"))
    parser.add_argument("--model", default="grok-4.6")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    add_issue_author_argument(parser)
    parser.add_argument("--sudo-docker", action="store_true")
    parser.add_argument("--automatic-stage", action="append", choices=ALL_STAGES)
    parser.add_argument("--poll-seconds", type=positive, default=3.0)
    parser.add_argument("--pickup-seconds", type=positive, default=90.0)
    parser.add_argument("--stage-timeout", type=positive, default=1800.0)
    parser.add_argument("--invoke-timeout", type=positive, default=600.0)
    parser.add_argument(
        "--brief-edit-what", help="resume only: what a person changed in the PM-written brief"
    )
    parser.add_argument("--brief-edit-why", help="resume only: why the brief was edited")
    parser.add_argument(
        "--policy-override-actor",
        help="resume only (#65): the person who decides the run continues under a changed approval policy",
    )
    parser.add_argument(
        "--policy-override-reason", help="resume only (#65): why the approval policy changes"
    )
    arguments = parser.parse_args(argv)
    if arguments.repository != DEFAULT_REPOSITORY and not arguments.allow_other_repository:
        parser.error(
            f"refusing {arguments.repository}: pass --allow-other-repository to leave {DEFAULT_REPOSITORY}"
        )
    if (arguments.command == "resume") != (arguments.run_dir is not None):
        parser.error("--run-dir is required for resume and not accepted for start")
    for name in ("brief_edit_what", "brief_edit_why", "policy_override_actor", "policy_override_reason"):
        value = getattr(arguments, name)
        if value is not None and not value.strip():
            parser.error(f"--{name.replace('_', '-')} must not be blank")
    if (arguments.brief_edit_what is None) != (arguments.brief_edit_why is None):
        parser.error("--brief-edit-what and --brief-edit-why must be given together")
    if arguments.brief_edit_what is not None and arguments.command != "resume":
        parser.error("brief edits are recorded on resume")
    if (arguments.policy_override_actor is None) != (arguments.policy_override_reason is None):
        parser.error("--policy-override-actor and --policy-override-reason must be given together")
    if arguments.policy_override_actor is not None and arguments.command != "resume":
        parser.error("a policy override applies only to a resume")
    try:
        arguments.automatic_stages = resolve_automatic_stages(arguments.automatic_stage)
    except ValueError as error:
        parser.error(str(error))
    try:
        require_issue_author(arguments.automatic_stages, arguments.allowed_issue_author)
    except SystemExit as error:
        parser.error(str(error))
    work_root = arguments.work_root.resolve()
    if work_root == SERVICE_ROOT or SERVICE_ROOT in work_root.parents:
        parser.error(
            "--work-root must live outside the service checkout (keeps runs and secrets out of git)"
        )
    return arguments


def policy_override(arguments: argparse.Namespace) -> dict[str, str] | None:
    actor = getattr(arguments, "policy_override_actor", None)
    if actor is None:
        return None
    return {"actor": actor, "reason": arguments.policy_override_reason}


def resolve_automatic_stages(requested: Iterable[str] | None) -> tuple[str, ...]:
    stages = tuple(dict.fromkeys(requested or ALLOWED_AUTOMATIC_STAGES))
    refused = set(stages) - set(ALLOWED_AUTOMATIC_STAGES)
    if refused:
        raise ValueError(f"refusing automatic approval for {sorted(refused)}")
    return stages


def merged_keys(plan: dict[str, Any], merge_points: Iterable[dict[str, Any]]) -> set[str]:
    """Issue keys whose PRs are merged. Start-phase merge points carry no key: the independent issues count as
    merged only once every keyless merge point is merged."""
    points = list(merge_points)
    merged = {p["issue_key"] for p in points if p.get("issue_key") and p.get("state") == "MERGED"}
    keyless = [p for p in points if not p.get("issue_key")]
    if keyless and all(p.get("state") == "MERGED" for p in keyless):
        merged |= {issue["key"] for issue in plan["issues"] if not issue["dependencies"]}
    return merged


def next_handoff(plan: dict[str, Any], merge_points: Iterable[dict[str, Any]]) -> str | None:
    """The first planned dependent issue, in plan order, that is not delivered yet and whose prerequisites all merged.
    ``None`` while an open merge point is still waiting for its merge or when the plan is finished."""
    points = list(merge_points)
    if any(p.get("state") != "MERGED" for p in points):
        return None
    merged = merged_keys(plan, points)
    delivered = {p["issue_key"] for p in points if p.get("issue_key")}
    for issue in plan["issues"]:
        key, dependencies = issue["key"], issue["dependencies"]
        if dependencies and key not in delivered and set(dependencies) <= merged:
            return str(key)
    return None


class MergePointError(RuntimeError):
    """A merge point that cannot be trusted: merged at an unreviewed head or without a merge commit."""


def check_merge_point(point: dict[str, Any], pull: dict[str, Any]) -> str:
    """Return the PR state after checking a MERGED PR merged exactly the head that was reviewed and okayed."""
    state = str(pull["state"])
    if state != "MERGED":
        return state
    if pull.get("headRefOid") != point.get("head_sha"):
        raise MergePointError(
            f"PR #{point.get('pull_number')} merged at {pull.get('headRefOid')}, "
            f"not the reviewed head {point.get('head_sha')}"
        )
    if not (pull.get("mergeCommit") or {}).get("oid"):
        raise MergePointError(f"PR #{point.get('pull_number')} is MERGED without a merge commit")
    return state


def planned_issue_for(task_id: str, plan: dict[str, Any]) -> dict[str, Any] | None:
    """Map a ``planned-issue-*`` task id back to its plan issue (same derivation as organization_planning)."""
    assignment = plan["planning_assignment_id"]
    for issue in plan["issues"]:
        digest = sha256(f"{assignment}:{issue['key']}".encode()).hexdigest()
        if task_id == "planned-issue-" + digest:
            return dict(issue)
    return None


def classify_planned_child(task_id: str, status_code: int, plan: dict[str, Any]) -> str:
    """``deliver`` for an admissible child, ``blocked`` only for a 409 on a planned dependent; anything else fails."""
    issue = planned_issue_for(task_id, plan)
    if issue is None:
        raise RuntimeError(f"Planned child {task_id} does not map to an issue of this plan")
    if status_code == 200:
        return "deliver"
    if status_code == 409 and issue["dependencies"]:
        return "blocked"
    raise RuntimeError(f"Planned child {issue['key']} answered {status_code}")


class SeedError(RuntimeError):
    """The local seed cannot be fast-forwarded safely to the merged base."""


def refresh_seed_base(seed: Path, branch: str) -> str:
    """Bring the seed's checked-out base branch up to the merged remote base.

    Git refuses to fetch into the checked-out branch, so fetch into the remote-tracking ref and fast-forward the
    clean seed checkout to it. A dirty, detached or diverged seed fails instead of being reset."""
    status = git(seed, "status", "--porcelain")
    if status:
        raise SeedError(f"Seed has uncommitted changes:\n{status}")
    head = subprocess.run(
        ["git", "-C", str(seed), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != branch:
        raise SeedError(f"Seed must have {branch} checked out, not {head or 'a detached HEAD'}")
    git(
        seed,
        "fetch",
        "--no-tags",
        "--quiet",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    )
    merged = subprocess.run(
        ["git", "-C", str(seed), "merge", "--ff-only", "--quiet", f"refs/remotes/origin/{branch}"],
        capture_output=True,
        text=True,
    )
    if merged.returncode != 0:
        raise SeedError(
            f"Seed {branch} has diverged from origin/{branch}; refusing to reset it: {merged.stderr.strip()}"
        )
    return git(seed, "rev-parse", f"refs/heads/{branch}")


def require_merge_commit_on_base(seed: Path, merge_commit: str, branch: str) -> None:
    """The merge commit GitHub reports must be reachable from the fetched base branch."""
    ancestry = subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "merge-base",
            "--is-ancestor",
            merge_commit,
            f"refs/heads/{branch}",
        ],
        capture_output=True,
    )
    if ancestry.returncode != 0:
        raise MergePointError(f"Merge commit {merge_commit} is not on {branch}")


def require_merge_points(report: dict[str, Any]) -> None:
    """``start`` must end at a merge point; a plan whose children were all blocked or skipped is a failure."""
    if not report.get("merge_points"):
        raise RuntimeError("start reached no merge point")


def deliver_planned_children(
    listing: Iterable[dict[str, Any]],
    report: dict[str, Any],
    *,
    status_of: Callable[[str], int],
    deliver: Callable[[str], Any],
) -> None:
    """Deliver every admissible planned child to its merge point, skip planned dependents (409 until a dependency
    handoff exists) and fail unless ``start`` ends at a merge point."""
    for item in listing:
        task_id = str(item["bundle"].get("task_id", ""))
        if not task_id.startswith("planned-issue-"):
            continue
        preview_id = item["preview_id"]
        if classify_planned_child(task_id, status_of(preview_id), report["plan"]) == "blocked":
            report.setdefault("blocked_dependents", []).append(preview_id)
            continue
        deliver(preview_id)
    require_merge_points(report)


def brief_edit_action(issue_key: str, what: str, why: str) -> dict[str, Any]:
    return {
        "at": now(),
        "kind": "brief_edit",
        "issue_key": issue_key,
        "what": what,
        "why": why,
        "note": "the PM wrote this brief; a person edited it before the dependency handoff",
    }


# --- live run ---------------------------------------------------------------------------------------------------


def build(repository: str, repository_id: int, state_root: Path, invoke_timeout: float):
    """followups.build_factories plus the dependency handoff binding (ManagedDependencies)."""
    from aitobuild.organization_dependencies import ManagedDependencies
    from aitobuild.organization_planning import PlanningRoute
    import aitobuild.organization_service as service_module

    base = service_module.ManagedOrganizationService
    planning_holder: dict[str, Any] = {}

    class WithDependencies(base):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            planning = kwargs["planning_publication"]
            route = planning.admission.routes[0]
            planning_holder["planning"] = planning
            super().__init__(
                **kwargs,
                dependencies=ManagedDependencies(
                    planning=planning,
                    worker=kwargs["worker"],
                    state_dir=state_root / "dependency-handoffs",
                    routes=(
                        PlanningRoute(
                            repository=repository,
                            repository_id=repository_id,
                            organization_id=route.organization_id,
                            revision=route.revision,
                            event=DEPENDENCY_EVENT,
                        ),
                    ),
                    lookup_max_receipts=100,
                ),
            )

    service_factory, worker_factory, holder = followups.build_factories(
        repository=repository,
        repository_id=repository_id,
        state_root=state_root,
        invoke_timeout=invoke_timeout,
        service_class=WithDependencies,
        planner_instructions=PLANNER_INSTRUCTIONS,
    )
    holder["planning_holder"] = planning_holder
    return service_factory, worker_factory, holder


def phase(
    operator: Operator,
    hands: followups.HandsOff,
    report: dict[str, Any],
    preview_id: str,
    label: str,
    what: str,
    *,
    human_admission: bool = False,
    correction_admission: str | None = None,
) -> dict[str, Any]:
    report["phases"].append(
        {
            "phase": label,
            "started_at": now(),
            "preview_id": preview_id,
            # #65: the policy this phase runs under, so evidence never judges it by a later session's policy.
            "policy_revision": (report.get("approval_policy") or {}).get("revision"),
        }
    )
    # Since #31 planning tasks never auto-admit, so the planning phase admits as a human (as in followups, #35).
    if correction_admission is not None:
        # A correction or a correction's re-review: human-gated unless `correction` is automatic (Oct 12 rule, #39).
        hands.correction_child(preview_id, what, reason=correction_admission)
    else:
        hands.child(preview_id, what, human_admission=human_admission)
    status = operator.drive(preview_id, label)
    report["phases"][-1].update(
        ended_at=now(), run_state=(status.get("managed_run") or {}).get("state")
    )
    return status


def deliver_to_merge_point(
    operator: Operator,
    hands: followups.HandsOff,
    report: dict[str, Any],
    client: Any,
    seed: Path,
    preview_id: str,
    label: str,
    issue_key: str | None,
) -> dict[str, Any]:
    status = phase(operator, hands, report, preview_id, f"delivery:{label}", f"Developer {label}")
    if (status.get("managed_run") or {}).get("state") != "completed":
        raise RuntimeError(
            f"Delivery {label} did not complete: " + json.dumps(status, default=str)[:1500]
        )
    record = client.get(
        "/internal/developer/delivery/" + preview_id, headers=operator.headers
    ).json()
    publication = (record.get("delivery") or {}).get("publication") or {}
    point: dict[str, Any] = {
        "label": label,
        "issue_key": issue_key,
        "preview_id": preview_id,
        **{k: publication.get(k) for k in ("pull_number", "branch", "head_sha")},
    }
    followup = operator.wait_follow_up(preview_id, f"delivery:{label}")
    if not followup or followup.get("state") != "staged":
        raise RuntimeError(f"Review follow-up not staged for {label}: {followup}")
    review_id = followup["next_preview_id"]
    phase(operator, hands, report, review_id, f"review:{label}", f"Architect review {label}")
    followup = operator.wait_follow_up(review_id, f"review:{label}")
    point["review_outcome"] = (followup or {}).get("state")
    if followup and followup.get("state") == "staged":
        correction_id, branch = followup["next_preview_id"], point["branch"]
        git(
            seed,
            "fetch",
            "--no-tags",
            "--quiet",
            "origin",
            f"+refs/heads/{branch}:refs/heads/{branch}",
        )
        hands.action(
            "seed_provisioning",
            f"reviewed head into seed ({label})",
            branch=branch,
            head_sha=point["head_sha"],
        )
        phase(
            operator,
            hands,
            report,
            correction_id,
            f"correction:{label}",
            f"Correction {label}",
            correction_admission="correction",
        )
        staged = operator.post(
            "/internal/organization/corrections/stage-publication",
            {"correction_preview_id": correction_id},
        )["publish_approval"]
        published = operator.post(
            "/internal/organization/corrections/publish",
            {"correction_preview_id": correction_id, "approval_digest": staged["digest"]},
        )
        report["approvals"].append(
            {
                "stage": "correction",
                "what": f"same-PR correction ({label})",
                "approval_digest": staged["digest"],
                "at": now(),
            }
        )
        rereview = (published.get("review_preview") or {}).get("preview_id")
        if rereview:
            phase(
                operator,
                hands,
                report,
                rereview,
                f"re-review:{label}",
                f"Architect re-review {label}",
                correction_admission="correction_rereview",
            )
    pull = gh_json(
        "pr",
        "view",
        str(point["pull_number"]),
        "-R",
        report["repository"],
        "--json",
        "url,headRefOid,isDraft,state",
    )
    point.update(
        url=pull["url"],
        head_sha=pull["headRefOid"],
        is_draft=pull["isDraft"],
        state=pull["state"],
        ready_to_merge_at=now(),
        merge_gate=MERGE_GATE,
    )
    report.setdefault("merge_points", []).append(point)
    return point


def build_trial_environment(
    arguments: Any,
    *,
    repository: dict[str, Any],
    seed: Path,
    base: str,
    run_dir: Path,
    secret: str,
    token: str,
) -> dict[str, str]:
    """The trial's whole service environment; tests load it through aitobuild's real configuration."""
    return trial_environment_values(
        source=repository_source(
            repository=repository["full_name"],
            repository_id=repository["id"],
            path=seed,
            verification_command=followups.VERIFY_COMMAND,
            base_revision=base,
            issue_author=arguments.allowed_issue_author,
        ),
        run_dir=run_dir,
        endpoint=arguments.endpoint,
        model=arguments.model,
        invoke_timeout=arguments.invoke_timeout,
        secret=secret,
        token=token,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    stages = arguments.automatic_stages
    work_root = arguments.work_root.resolve()
    if arguments.command == "start":
        trial_id = "trial4-" + datetime.now(tz=HELSINKI).strftime("%Y%m%d-%H%M%S")
        run_dir = work_root / "runs" / trial_id
    else:
        run_dir = arguments.run_dir.resolve()
        trial_id = run_dir.name
    recorder = Recorder(work_root / "artifacts" / trial_id)
    report_path = recorder.directory / "report.json"
    if arguments.command == "resume":
        recorder.report = json.loads(report_path.read_text())
    report = recorder.report
    for key in ("operator_actions", "worker_pickups", "sessions", "transient_retries"):
        report.setdefault(key, [])
    report["sessions"].append({"command": arguments.command, "started_at": now()})
    report["metrics"] = {
        "bot_stands_in_for_human_merge": True,
        "merge_gate": MERGE_GATE,
        "service_merge_stage": "disabled",
    }
    try:
        repo = gh_json("api", f"repos/{arguments.repository}")
        report.update(
            trial="4: small M5 pilot",
            trial_id=trial_id,
            repository=repo["full_name"],
            model=arguments.model,
            run_dir=str(run_dir),
            result="running",
        )
        seed, service_cwd = run_dir / "seed", run_dir / "service"
        branch = repo["default_branch"]
        if arguments.command == "start":
            run_dir.mkdir(parents=True)
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--branch",
                    branch,
                    f"https://github.com/{repo['full_name']}.git",
                    str(seed),
                ],
                check=True,
            )
            report["service_commit"] = git(SERVICE_ROOT, "rev-parse", "HEAD")
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(SERVICE_ROOT), str(service_cwd)],
                check=True,
            )
            git(service_cwd, "checkout", "--quiet", "--detach", report["service_commit"])
        else:
            refresh_seed_base(seed, branch)
            for point in report.get("merge_points", []):
                pull = gh_json(
                    "pr",
                    "view",
                    str(point["pull_number"]),
                    "-R",
                    repo["full_name"],
                    "--json",
                    "state,headRefOid,mergeCommit",
                )
                point["state"] = check_merge_point(point, pull)
                if point["state"] == "MERGED":
                    point["merge_commit"] = pull["mergeCommit"]["oid"]
                    require_merge_commit_on_base(seed, point["merge_commit"], branch)
            report["operator_actions"].append(
                {
                    "at": now(),
                    "kind": "seed_provisioning",
                    "what": "merged base branch into seed before dependency handoff",
                    "head_sha": git(seed, "rev-parse", f"refs/heads/{branch}"),
                }
            )
        base = git(seed, "rev-parse", f"refs/heads/{branch}")
        report.setdefault("bases", []).append(base)
        recorder.save()
        if arguments.sudo_docker:
            prepare_docker_wrapper(run_dir / "bin")
        token = secrets.token_hex(24)  # per process, never written to the report
        configure_trial_environment(
            automatic_stages=stages,
            values=build_trial_environment(
                arguments,
                repository=repo,
                seed=seed,
                base=base,
                run_dir=run_dir,
                secret=secrets.token_hex(24),
                token=token,
            ),
        )
        os.chdir(service_cwd)

        from fastapi.testclient import TestClient

        from aitobuild.app import create_app
        from aitobuild.config import load_config
        from aitobuild.developer_isolation import default_developer_isolation_policy
        from aitobuild.organization_planning import PlanningLimits, PlanningScope

        config = load_config()
        pin_session_policy(report, check_policy(config.approvals, stages), policy_override(arguments))
        service_factory, worker_factory, holder = build(
            repo["full_name"],
            repo["id"],
            run_dir / "state" / "organization",
            arguments.invoke_timeout,
        )
        app = create_app(
            config, managed_service_factory=service_factory, managed_worker_factory=worker_factory
        )
    except BaseException as error:  # noqa: BLE001 - failures before the service starts are recorded too
        record_early_failure(recorder, error)
        raise

    try:
        with TestClient(app) as client:
            operator = Operator(
                client, token, recorder, arguments.poll_seconds, arguments.stage_timeout, stages
            )
            hands = followups.HandsOff(operator, report, arguments.pickup_seconds)
            override = policy_override(arguments)
            if override is not None:
                # #65: the harness override is the human decision; record it on each open run through the service's
                # own override before anything resumes it.
                for assignment_id in runs_needing_policy_override(
                    report, operator.task, report["approval_policy"]["revision"]
                ):
                    operator.post(
                        "/internal/organization/tasks/override_policy",
                        {
                            "assignment_id": assignment_id,
                            "reason": f"{override['actor']}: {override['reason']}",
                        },
                    )
                    hands.action(
                        "approval_policy_override",
                        f"override for {assignment_id}",
                        endpoint="POST /internal/organization/tasks/override_policy",
                    )
            if arguments.command == "start":
                planning = holder["planning_admission"].offer(
                    request_id=trial_id,
                    objective=OBJECTIVE.format(trial_id=trial_id),
                    scope=PlanningScope(
                        repository=repo["full_name"],
                        repository_id=repo["id"],
                        base_revision=base,
                        base_branch=branch,
                        limits=PlanningLimits(max_issues=3, max_plan_bytes=14000),
                    ),
                    developer_policy=default_developer_isolation_policy(),
                )
                hands.action(
                    "planning_offer_in_process",
                    "PM planning request",
                    preview_id=planning.preview_id,
                    endpoint="none (PlanningAdmission.offer)",
                )
                status = phase(
                    operator, hands, report, planning.preview_id, "planning", "PM planning task",
                    human_admission=True,
                )
                run = status.get("managed_run") or {}
                if run.get("state") != "completed":
                    raise RuntimeError(
                        "Planning did not complete: " + json.dumps(status, default=str)[:1500]
                    )
                plan = holder["planning_holder"]["planning"].plans.get(run["assignment_id"])
                report["plan"] = {
                    "planning_assignment_id": run["assignment_id"],
                    "issues": [
                        {"key": i.key, "title": i.title, "dependencies": list(i.dependencies)}
                        for i in plan.proposal.issues
                    ],
                }
                recorder.save()
                hands.planned_children()  # waits until planning has staged its children
                listing = client.get(
                    "/internal/developer/previews",
                    params={"pending_only": "false", "limit": 500},
                    headers=operator.headers,
                ).json()["items"]
                deliver_planned_children(
                    listing,
                    report,
                    status_of=lambda preview_id: (
                        client.get(
                            f"/internal/organization/tasks/{preview_id}", headers=operator.headers
                        ).status_code
                    ),
                    deliver=lambda preview_id: deliver_to_merge_point(
                        operator, hands, report, client, seed, preview_id, preview_id[:11], None
                    ),
                )
            else:
                handoff_key = next_handoff(report["plan"], report.get("merge_points", []))
                if handoff_key is None:
                    raise SystemExit(
                        "Nothing to hand off: an open merge point is unmerged or the plan is finished"
                    )
                if arguments.brief_edit_what is not None:
                    report["operator_actions"].append(
                        brief_edit_action(
                            handoff_key, arguments.brief_edit_what, arguments.brief_edit_why
                        )
                    )
                offered = operator.post(
                    "/internal/organization/dependencies/offer",
                    {
                        "planning_assignment_id": report["plan"]["planning_assignment_id"],
                        "issue_key": handoff_key,
                        "base_revision": base,
                    },
                )
                hands.action(
                    "dependency_offer",
                    f"handoff {handoff_key}",
                    endpoint="POST /internal/organization/dependencies/offer",
                    state=offered.get("state"),
                )
                if offered.get("state") != "staged":
                    raise RuntimeError(
                        f"Dependency handoff for {handoff_key} was not staged: {offered}"
                    )
                handoff = offered["handoff_preview"]["preview_id"]
                deliver_to_merge_point(
                    operator, hands, report, client, seed, handoff, handoff_key, handoff_key
                )
            report["evidence"] = collect_evidence(holder, report, config.approvals)
            report["result"] = "paused_at_merge_point" if report["evidence"]["ok"] else "fail"
    except BaseException as error:  # noqa: BLE001 - every failure must land in the report
        report["result"] = "fail"
        report["failure"] = f"{type(error).__name__}: {error}"
        recorder.event("failure", error=report["failure"])
        raise
    finally:
        report["sessions"][-1]["ended_at"] = now()
        recorder.save()
    print(json.dumps({"result": report["result"], "report": str(report_path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
