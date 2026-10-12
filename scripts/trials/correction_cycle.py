"""Live supervised trials 3 and 1: one-service-run Developer -> draft PR -> Architect COMMENT -> correction -> re-review.

The harness drives the authenticated ASGI app (signed webhook ingress, gated internal endpoints) with the
real gh_cli adapter, a real Azure OpenAI v1 model and constrained Docker sessions. In human mode every
approval stays ``human`` and is given by the operator (this process) only through the normal endpoints.
IMPORTANT: the operator approves every pending human-stage request, including each Developer tool request,
automatically and without a person looking at it; each decision is recorded (with a digest and preview of the
exact data) in the report for audit afterwards. The only planned manual step is provisioning the reviewed head/published branch
into the private local seed before correction preparation (README "Complete Supervised Correction Cycle").

``--approval-mode automatic`` (trial 1) binds the central policy with ONLY ``task_admission`` and
``developer_tools`` automatic; publication stages stay human and the harness never approves admission or tool
requests itself in that mode. See scripts/trials/README.md for prerequisites and usage. Never point --work-root at the service checkout.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
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
    configure_trial_environment,
    finish,
    gh_json,
    git,
    now,
    positive,
    prepare_docker_wrapper,
    signed_issue_webhook,
)

SERVICE_ROOT = Path(__file__).resolve().parents[2]
VERIFY_COMMAND = "PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider"

ISSUE_BODY = """Add `clamp(value: int, low: int, high: int) -> int` to `src/demo_app/math_ops.py` and cover it in a new
`tests/test_clamp.py`.

`clamp` returns `low` when `value < low`, `high` when `value > high`, and `value` otherwise.

## Acceptance Criteria
- [ ] `clamp` exists in `src/demo_app/math_ops.py` with the signature above.
- [ ] `tests/test_clamp.py` covers a value below, inside and above the range.
- [ ] Existing tests still pass.

## Notes
- Seeded defect for the supervised correction-cycle trial ({trial_id}): implement only the behaviour listed above.
  Do not add input validation for inverted bounds (`low > high`) in this task.
- Expected review outcome: inverted bounds silently return a value. A reviewer should propose a scoped correction
  that raises `ValueError` when `low > high` and adds a test for it.
"""

AVERAGE_TITLE = "Add average(values) to math_ops"
AVERAGE_BODY = """Add `average(values: list[int]) -> float` to `src/demo_app/math_ops.py` returning the arithmetic mean of
`values`, and cover it in a new `tests/test_average.py`.

## Acceptance Criteria
- [ ] `average` exists in `src/demo_app/math_ops.py` with the signature above.
- [ ] `tests/test_average.py` covers a single value and several values.
- [ ] Existing tests still pass.

## Notes
- Supervised automatic-mode trial ({trial_id}).
"""

DEVELOPER_INSTRUCTIONS = (
    "Implement exactly the approved task bundle in the prepared checkout using the scoped file tools. "
    "Read files before editing, keep changes minimal and inside the allowed paths, and run the target tests "
    "with the shell tool when available. Do not access the network. When a correction objective is given, "
    "implement exactly that objective in the listed paths."
)
ARCHITECT_INSTRUCTIONS = (
    "Review the pinned published draft. Read every changed source file and the pinned-base diff first. "
    "If you find a correctness defect in the changed files, call architect_propose_correction exactly once with "
    "the affected published paths and one precise, bounded objective. If the change is correct, do not propose a "
    "correction. Finish with a concise review comment summarising what you inspected and found. Never approve or merge."
)


def organization_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "trialorg",
        "agents": [
            {"id": "planner", "role": "pm", "instructions": "Coordinate the trial team."},
            {"id": "developer_one", "role": "developer", "instructions": DEVELOPER_INSTRUCTIONS},
            {"id": "reviewer", "role": "architect", "instructions": ARCHITECT_INSTRUCTIONS},
        ],
        "teams": [
            {
                "id": "product",
                "members": ["planner", "developer_one", "reviewer"],
                "coordinator": "planner",
            }
        ],
        "workflows": [
            {
                "id": "delivery",
                "document": {
                    "format": "python_graph",
                    "start": "implement",
                    "nodes": [
                        {"id": "implement", "kind": "operation", "operation": "delivery_implement"},
                        {"id": "verify", "kind": "operation", "operation": "delivery_verify"},
                        {"id": "publish", "kind": "operation", "operation": "delivery_publish"},
                    ],
                    "edges": [
                        {"kind": "edge", "source": "implement", "target": "verify"},
                        {"kind": "edge", "source": "verify", "target": "publish"},
                    ],
                    "outputs": ["publish"],
                },
            },
            {
                "id": "correction",
                "document": {
                    "format": "python_graph",
                    "start": "implement",
                    "nodes": [
                        {"id": "implement", "kind": "operation", "operation": "delivery_implement"},
                        {"id": "verify", "kind": "operation", "operation": "delivery_verify"},
                    ],
                    "edges": [{"kind": "edge", "source": "implement", "target": "verify"}],
                    "outputs": ["verify"],
                },
            },
            {
                "id": "review",
                "document": {
                    "format": "python_graph",
                    "start": "review",
                    "nodes": [
                        {
                            "id": "review",
                            "kind": "operation",
                            "operation": "architect_review_published",
                        }
                    ],
                    "edges": [],
                    "outputs": ["review"],
                },
            },
        ],
        "routes": [
            {
                "id": "issue_intake",
                "events": ["github.issue.ready"],
                "team": "product",
                "workflow": "delivery",
                "delegation": {
                    "strategy": "rules",
                    "eligible_agents": ["developer_one"],
                    "target_agent": "developer_one",
                },
            },
            {
                "id": "review",
                "events": ["published.review"],
                "team": "product",
                "workflow": "review",
                "delegation": {
                    "strategy": "rules",
                    "eligible_agents": ["reviewer"],
                    "target_agent": "reviewer",
                },
            },
            {
                "id": "correction",
                "events": ["published.correction"],
                "team": "product",
                "workflow": "correction",
                "delegation": {
                    "strategy": "rules",
                    "eligible_agents": ["developer_one"],
                    "target_agent": "developer_one",
                },
            },
        ],
    }


def build_factories(
    *, repository: str, repository_id: int, state_root: Path, invoke_timeout: float
):
    from aitobuild.agent_tools import DeveloperToolContext
    from aitobuild.config import RuntimeConfig
    from aitobuild.organization import FileDefinitionStore, parse_organization_definition
    from aitobuild.organization_assignments import FileAssignmentStore
    from aitobuild.organization_delivery import (
        ManagedDeliveryBindings,
        NativeDeliveryImplementation,
    )
    from aitobuild.organization_reviews import PublishedReviewAdmission, PublishedReviewRoute
    from aitobuild.organization_roles import NativeManagedRoles
    from aitobuild.organization_runner import FileRunStore
    from aitobuild.organization_runtime import bootstrap_organization, create_model_profile
    from aitobuild.organization_service import ManagedOrganizationService, ManagedRoute
    from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker
    from aitobuild.policy import AgentRole

    holder: dict[str, Any] = {}

    def service_factory(context):
        github = context.tools.github_adapter
        adapter = context.tools.container_session_adapter
        if github is None or adapter is None:
            raise ValueError("Trial requires gh_cli GitHub and container_session execution")
        definitions = FileDefinitionStore(state_root / "definitions")
        snapshot = definitions.save(
            parse_organization_definition(json.dumps(organization_document()))
        )
        profile = create_model_profile(
            RuntimeConfig(
                foundry_endpoint=os.environ["AITOBUILD_FOUNDRY_ENDPOINT"],
                foundry_api_key=None,
                foundry_model=os.environ["AITOBUILD_FOUNDRY_MODEL"],
                allow_mock_model=False,
            )
        )

        def runtime_for(pinned):
            return bootstrap_organization(
                pinned,
                model_profiles={"default": profile},
                default_model_profile="default",
                state_dir=state_root / "agents",
            )

        def route(event: str) -> PublishedReviewRoute:
            return PublishedReviewRoute(
                repository=repository,
                repository_id=repository_id,
                organization_id=snapshot.organization_id,
                revision=snapshot.revision,
                event=event,
            )

        reviews = PublishedReviewAdmission(
            definitions=definitions,
            previews=context.previews,
            worker=context.worker,
            github=github,
            state_dir=state_root / "published-reviews",
            routes=(route("published.review"),),
            correction_routes=(route("published.correction"),),
            max_correction_rounds=1,
        )
        roles = NativeManagedRoles(
            runtime_for=runtime_for,
            state_dir=state_root / "roles",
            proposal_event="github.issue.ready",
            worker=context.worker,
            github=github,
            invoke_timeout_seconds=invoke_timeout,
            review_target_for=lambda task: reviews.target_for(task.assignment.preview_id),
            allow_correction_proposals=True,
        )
        tools = DeveloperToolContext(
            bash_adapter=context.tools.bash_adapter,
            filesystem_adapter=context.tools.filesystem_adapter,
            container_session_adapter=adapter,
            workspace_root=context.tools.workspace_root,
            require_human_approval_for_repo_writes=True,
        )
        native = NativeDeliveryImplementation(
            worker=context.worker,
            runtime_for=runtime_for,
            tools=tools,
            state_dir=state_root / "native-delivery",
            invoke_timeout_seconds=invoke_timeout,
        )
        bindings = ManagedDeliveryBindings(
            worker=context.worker,
            implementation=native,
            verification_adapter=adapter,
            github=github,
        )
        operations = {**roles.operations, **bindings.operations}

        async def cleanup(task):
            role = next(
                agent.role
                for agent in task.snapshot.definition.agents
                if agent.id == task.assignment.agent_id
            )
            await (bindings.cleanup(task) if role == AgentRole.DEVELOPER else roles.cleanup(task))

        service = ManagedOrganizationService(
            definitions=definitions,
            assignments=FileAssignmentStore(state_root / "assignments.json"),
            runs=FileRunStore(state_root / "runs"),
            previews=context.previews,
            worker=context.worker,
            routes=(
                ManagedRoute(
                    repository=repository,
                    repository_id=repository_id,
                    organization_id=snapshot.organization_id,
                    revision=snapshot.revision,
                    event="github.issue.ready",
                ),
            ),
            reviews=reviews,
            operations=operations,
            cleanup=cleanup,
            operator_id="trial-operator",
            binding_revision="trial3-correction-cycle-v1",
            automatic_review_followups=True,
            approval_policy=context.approval_policy,
            task_base_revision_for=context.task_base_revision_for,
        )
        holder["service"] = service
        holder["previews"] = context.previews
        return service

    def worker_factory(service, context):
        return ManagedOrganizationWorker(
            service=service,
            store=FileWorkerStore(state_root / "organization-worker.json"),
            max_workers=2,
            recover_approved=False,
            queue_follow_ups=context.queue_follow_ups,
        )

    return service_factory, worker_factory, holder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument(
        "--allow-other-repository",
        action="store_true",
        help="Required to target anything other than the disposable uhvogala/aitobuild_example",
    )
    parser.add_argument("--work-root", type=Path, default=Path("/workspace/trials"))
    parser.add_argument("--model", default="grok-4.6")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    add_issue_author_argument(parser)
    parser.add_argument(
        "--sudo-docker",
        action="store_true",
        help="Run docker through a PATH-local `sudo -n docker` wrapper",
    )
    parser.add_argument(
        "--approval-mode",
        choices=("human", "automatic"),
        default="human",
        help="automatic = trial 1: policy-automatic task_admission/developer_tools only",
    )
    parser.add_argument(
        "--automatic-stage",
        action="append",
        choices=ALL_STAGES,
        help="Stage to make automatic (repeatable; default task_admission and developer_tools). "
        "Only task_admission and developer_tools are accepted; any other stage is refused.",
    )
    parser.add_argument("--poll-seconds", type=positive, default=3.0)
    parser.add_argument("--stage-timeout", type=positive, default=1800.0)
    parser.add_argument("--invoke-timeout", type=positive, default=600.0)
    arguments = parser.parse_args()

    if arguments.repository != DEFAULT_REPOSITORY and not arguments.allow_other_repository:
        raise SystemExit(
            f"Refusing {arguments.repository}: pass --allow-other-repository to leave {DEFAULT_REPOSITORY}"
        )
    automatic_stages = (
        tuple(dict.fromkeys(arguments.automatic_stage or ALLOWED_AUTOMATIC_STAGES))
        if arguments.approval_mode == "automatic"
        else ()
    )
    if arguments.automatic_stage and arguments.approval_mode != "automatic":
        raise SystemExit("--automatic-stage requires --approval-mode automatic")
    refused = set(automatic_stages) - set(ALLOWED_AUTOMATIC_STAGES)
    if refused:
        raise SystemExit(
            f"Refusing automatic approval for {sorted(refused)}; only {ALLOWED_AUTOMATIC_STAGES} may be automatic"
        )
    require_issue_author(automatic_stages, arguments.allowed_issue_author)
    work_root = arguments.work_root.resolve()
    if work_root == SERVICE_ROOT or SERVICE_ROOT in work_root.parents:
        raise SystemExit("--work-root must live outside the service checkout")
    stamp = datetime.now(tz=HELSINKI).strftime("%Y%m%d-%H%M%S")
    automatic = arguments.approval_mode == "automatic"
    trial_id = f"{'trial1' if automatic else 'trial3'}-{stamp}"
    run_dir = work_root / "runs" / trial_id
    recorder = Recorder(work_root / "artifacts" / trial_id)
    report = recorder.report
    report.update(
        {
            "trial": "1: automatic-mode run"
            if automatic
            else "3: uninterrupted supervised correction cycle",
            "approval_mode": arguments.approval_mode,
            "trial_id": trial_id,
            "started_at": now(),
            "repository": arguments.repository,
            "model": arguments.model,
            "endpoint": arguments.endpoint,
            "service_commit": git(SERVICE_ROOT, "rev-parse", "HEAD"),
            "run_dir": str(run_dir),
            "result": "running",
        }
    )
    recorder.save()

    repo = gh_json("api", f"repos/{arguments.repository}")
    repository = {
        "id": repo["id"],
        "full_name": repo["full_name"],
        "default_branch": repo["default_branch"],
    }
    seed = run_dir / "seed"
    run_dir.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--branch",
            repository["default_branch"],
            f"https://github.com/{arguments.repository}.git",
            str(seed),
        ],
        check=True,
    )
    base = git(seed, "rev-parse", "HEAD")
    report["base_revision"] = base

    if arguments.sudo_docker:
        report["docker_access"] = {
            "mode": "PATH-local wrapper exec sudo -n /usr/bin/docker",
            "wrapper": prepare_docker_wrapper(run_dir / "bin"),
        }
    secret, token = secrets.token_hex(24), secrets.token_hex(24)
    configure_trial_environment(
        automatic_stages=automatic_stages,
        values=trial_environment_values(
            source=repository_source(
                repository=repository["full_name"],
                repository_id=repository["id"],
                path=seed,
                verification_command=VERIFY_COMMAND,
                base_revision=base if automatic else None,
                issue_author=arguments.allowed_issue_author,
            ),
            run_dir=run_dir,
            endpoint=arguments.endpoint,
            model=arguments.model,
            invoke_timeout=arguments.invoke_timeout,
            secret=secret,
            token=token,
        ),
    )
    # The app captures cwd as its service root (excluded from target sources; must be a Git checkout). Use a
    # throwaway clone of the service at this commit so nothing in the trial touches the developer's checkout.
    service_cwd = run_dir / "service"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-local", str(SERVICE_ROOT), str(service_cwd)], check=True
    )
    git(service_cwd, "checkout", "--quiet", "--detach", report["service_commit"])
    os.chdir(service_cwd)

    from fastapi.testclient import TestClient
    from aitobuild.app import create_app
    from aitobuild.config import load_config

    config = load_config()
    report["approval_policy"] = check_policy(config.approvals, automatic_stages)
    service_factory, worker_factory, holder = build_factories(
        repository=repository["full_name"],
        repository_id=repository["id"],
        state_root=run_dir / "state" / "organization",
        invoke_timeout=arguments.invoke_timeout,
    )

    app = create_app(
        config, managed_service_factory=service_factory, managed_worker_factory=worker_factory
    )
    if automatic:
        title, body = (
            f"[Trial 1 automatic mode {stamp}] {AVERAGE_TITLE}",
            AVERAGE_BODY.format(trial_id=trial_id),
        )
        report["seeded_defect"] = (
            "Not stated in the issue: average([]) behaviour is unspecified (ZeroDivisionError risk)"
        )
    else:
        title = f"[Trial 3 correction cycle {stamp}] Add clamp(value, low, high) to math_ops"
        body = ISSUE_BODY.format(trial_id=trial_id)
        report["seeded_defect"] = "Stated in the issue: no validation for low > high"
    issue_url = subprocess.run(
        ["gh", "issue", "create", "-R", arguments.repository, "--title", title, "--body", body],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    number = int(issue_url.rstrip("/").rsplit("/", 1)[1])
    issue = gh_json("api", f"repos/{arguments.repository}/issues/{number}")
    report["issue"] = {"number": number, "url": issue_url}
    recorder.event("issue.created", url=issue_url)
    recorder.save()

    try:
        with TestClient(app) as client:
            operator = Operator(
                client,
                token,
                recorder,
                arguments.poll_seconds,
                arguments.stage_timeout,
                automatic_stages,
            )
            hook = signed_issue_webhook(
                client,
                secret,
                repository,
                {
                    "id": issue["id"],
                    "number": issue["number"],
                    "state": issue["state"],
                    "title": issue["title"],
                    "body": issue["body"],
                },
            )
            delivery_id = (hook.get("metadata") or {}).get("preview_id")
            recorder.event("webhook", route=hook.get("route"), preview_id=delivery_id)
            if not delivery_id:
                raise RuntimeError(f"Webhook did not stage a preview: {hook}")

            report["phases"].append(
                {"phase": "delivery", "started_at": now(), "preview_id": delivery_id}
            )
            operator.admit(delivery_id, "Developer delivery task", base_revision=base, timeout=15)
            status = operator.drive(delivery_id, "delivery")
            report["phases"][-1].update(
                ended_at=now(), run_state=(status.get("managed_run") or {}).get("state")
            )
            record = client.get(
                "/internal/developer/delivery/" + delivery_id, headers=operator.headers
            ).json()
            publication = (record.get("delivery") or record).get("publication") or {}
            report["pull_request"] = {
                "number": publication.get("pull_number"),
                "branch": publication.get("branch"),
                "head_sha": publication.get("head_sha"),
                "url": publication.get("url") or publication.get("html_url"),
            }
            recorder.save()
            if (status.get("managed_run") or {}).get("state") != "completed":
                raise RuntimeError(
                    "Delivery did not complete: " + json.dumps(status, default=str)[:2000]
                )

            followup = operator.wait_follow_up(delivery_id, "delivery")
            if not followup or followup.get("state") != "staged":
                raise RuntimeError(f"Review follow-up was not staged automatically: {followup}")
            review_id = followup["next_preview_id"]
            report["phases"].append(
                {"phase": "review", "started_at": now(), "preview_id": review_id}
            )
            operator.admit(review_id, "Architect review task")
            status = operator.drive(review_id, "review")
            report["phases"][-1].update(
                ended_at=now(),
                run_state=(status.get("managed_run") or {}).get("state"),
                outputs=(status.get("managed_run") or {}).get("outputs"),
            )
            if (status.get("managed_run") or {}).get("state") != "completed":
                raise RuntimeError(
                    "Review did not complete: " + json.dumps(status, default=str)[:2000]
                )
            followup = operator.wait_follow_up(review_id, "review")
            if automatic and followup and followup.get("state") == "no_correction":
                report["review_outcome"] = "no_correction"
                report["evidence"] = collect_evidence(holder, report, config.approvals)
                report["result"] = "pass" if report["evidence"]["ok"] else "fail"
                return finish(report, recorder)
            if not followup or followup.get("state") != "staged":
                raise RuntimeError(
                    f"Correction was not staged automatically after the COMMENT: {followup}"
                )
            correction_id = followup["next_preview_id"]

            # Planned operator step: provision the reviewed head and published branch into the private seed.
            reviewed_head, branch = (
                report["pull_request"]["head_sha"],
                report["pull_request"]["branch"],
            )
            git(
                seed,
                "fetch",
                "--no-tags",
                "--quiet",
                "origin",
                f"+refs/heads/{branch}:refs/heads/{branch}",
            )
            if git(seed, "rev-parse", f"refs/heads/{branch}") != reviewed_head:
                raise RuntimeError("Remote published branch no longer matches the reviewed head")
            report["manual_steps"].append(
                {
                    "step": "provision reviewed head into local seed (planned)",
                    "branch": branch,
                    "head_sha": reviewed_head,
                    "at": now(),
                }
            )
            recorder.event("manual.seed_provisioned", branch=branch, head=reviewed_head)

            report["phases"].append(
                {"phase": "correction", "started_at": now(), "preview_id": correction_id}
            )
            operator.admit(correction_id, "Developer correction task")
            status = operator.drive(correction_id, "correction")
            report["phases"][-1].update(
                ended_at=now(), run_state=(status.get("managed_run") or {}).get("state")
            )
            if (status.get("managed_run") or {}).get("state") != "completed":
                raise RuntimeError(
                    "Correction did not complete: " + json.dumps(status, default=str)[:2000]
                )
            staged = operator.post(
                "/internal/organization/corrections/stage-publication",
                {"correction_preview_id": correction_id},
            )
            approval = staged["publish_approval"]
            report["correction_snapshot"] = {
                key: approval.get(key) for key in ("state", "digest")
            } | {
                "snapshot": {
                    key: (approval.get("snapshot") or {}).get(key)
                    for key in ("pull_number", "head_branch", "parent_head_sha")
                }
            }
            published = operator.post(
                "/internal/organization/corrections/publish",
                {"correction_preview_id": correction_id, "approval_digest": approval["digest"]},
            )
            report["approvals"].append(
                {
                    "stage": "correction",
                    "what": "same-PR correction publication (exact staged digest)",
                    "approval_digest": approval["digest"],
                    "at": now(),
                    "via": "POST /internal/organization/corrections/stage-publication + /publish",
                }
            )
            delivery = published.get("delivery") or {}
            report["correction_publication"] = {
                "accepted": published.get("accepted"),
                "head_sha": (delivery.get("publication") or {}).get("head_sha"),
                "pull_number": (delivery.get("publication") or {}).get("pull_number"),
            }
            recorder.event("correction.published", result=report["correction_publication"])
            recorder.save()
            rereview = published.get("review_preview") or {}
            if not published.get("accepted") or not rereview.get("preview_id"):
                raise RuntimeError(
                    "Correction publication did not stage a re-review: "
                    + json.dumps(published, default=str)[:2000]
                )

            rereview_id = rereview["preview_id"]
            report["phases"].append(
                {"phase": "re-review", "started_at": now(), "preview_id": rereview_id}
            )
            operator.admit(rereview_id, "Architect re-review task")
            status = operator.drive(rereview_id, "re-review")
            report["phases"][-1].update(
                ended_at=now(),
                run_state=(status.get("managed_run") or {}).get("state"),
                outputs=(status.get("managed_run") or {}).get("outputs"),
            )
            report["re_review_follow_up"] = operator.wait_follow_up(
                rereview_id, "re-review", timeout=30
            )
            if (status.get("managed_run") or {}).get("state") != "completed":
                raise RuntimeError(
                    "Re-review did not complete: " + json.dumps(status, default=str)[:2000]
                )
            report["worker_diagnostics"] = client.get(
                "/internal/organization/worker", headers=operator.headers
            ).json()
            report["evidence"] = collect_evidence(holder, report, config.approvals)
            report["result"] = "pass" if report["evidence"]["ok"] else "fail"
    except BaseException as error:  # noqa: BLE001 - every failure must land in the report
        report["result"] = "fail"
        report["failure"] = f"{type(error).__name__}: {error}"
        recorder.event("failure", error=report["failure"])
        raise
    finally:
        pr_number = (report.get("pull_request") or {}).get("number")
        if pr_number:
            try:
                pull = gh_json(
                    "pr",
                    "view",
                    str(pr_number),
                    "-R",
                    arguments.repository,
                    "--json",
                    "url,headRefOid,isDraft,state,reviews",
                )
                report["pull_request"].update(
                    url=pull["url"],
                    final_head=pull["headRefOid"],
                    is_draft=pull["isDraft"],
                    state=pull["state"],
                    reviews=[
                        {"state": item["state"], "commit": (item.get("commit") or {}).get("oid")}
                        for item in pull["reviews"]
                    ],
                )
            except (subprocess.CalledProcessError, KeyError) as error:
                report["pull_request"]["inspect_error"] = str(error)
        report["ended_at"] = now()
        recorder.save()
    return finish(report, recorder)


if __name__ == "__main__":
    sys.exit(main())
