"""Live trial 2: hands-off follow-ups from a managed PM plan, queued by the detached worker on its own.

One service run: PM planning task -> exact issue creation (human pm_issue_creation approval) -> staged
planned Developer child -> delivery and draft PR (human draft_publication) -> head-pinned Architect review
(human review_publication COMMENT) -> correction if proposed (human same-PR publication) -> re-review.

The worker is bound with ``queue_follow_ups=True`` and the service with ``automatic_review_followups=True`` and
``planning_publication=planning``. The harness never queues children itself: for every child it first waits for
the worker. Each point where the operator had to act instead (in-process planning offer, run trigger, task approval
that enqueued work, seed provisioning) is recorded in ``report["operator_actions"]``. That list is what the trial measures.

Approvals: ``--approval-mode automatic`` (recommended for this trial; same refusal rules as correction_cycle.py)
makes ONLY task_admission and developer_tools automatic, so children can be admitted without an operator call and
"hands-off" measures the worker. In human mode the operator approves EVERY pending human-stage request, including
each Developer tool request, automatically and without a person looking at it (recorded for audit).
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
import time
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
    REVIEWED_OUTCOMES,
    SCRIPTED_HUMAN_ADMISSION,
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
    require_completed,
)
from correction_cycle import (  # noqa: E402
    ARCHITECT_INSTRUCTIONS,
    DEVELOPER_INSTRUCTIONS,
    SERVICE_ROOT,
    VERIFY_COMMAND,
)

PLANNING_EVENT = "pm.planning.requested"
PLANNER_INSTRUCTIONS = (
    "Plan the operator's objective for the pinned target as the smallest set of independent issues (prefer one). "
    "Inspect the target first. Each issue needs a precise objective and checkable acceptance criteria limited to "
    "src/ and tests/. Do not add dependencies unless strictly required."
)
OBJECTIVE = (
    "Add `cube(value: int) -> int` to src/demo_app/math_ops.py returning value ** 3, with tests in "
    "tests/test_cube.py covering positive, negative and zero inputs. Supervised hands-off follow-up trial {trial_id}."
)


def organization_document(planner_instructions: str = PLANNER_INSTRUCTIONS) -> dict[str, Any]:
    delivery = {
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
    }
    correction = {
        "format": "python_graph",
        "start": "implement",
        "nodes": [
            {"id": "implement", "kind": "operation", "operation": "delivery_implement"},
            {"id": "verify", "kind": "operation", "operation": "delivery_verify"},
        ],
        "edges": [{"kind": "edge", "source": "implement", "target": "verify"}],
        "outputs": ["verify"],
    }

    def single(operation: str) -> dict[str, Any]:
        return {
            "format": "python_graph",
            "start": "node",
            "nodes": [{"id": "node", "kind": "operation", "operation": operation}],
            "edges": [],
            "outputs": ["node"],
        }

    def rules(agent: str) -> dict[str, Any]:
        return {"strategy": "rules", "eligible_agents": [agent], "target_agent": agent}

    return {
        "schema_version": 1,
        "id": "trialorg",
        "agents": [
            {"id": "planner", "role": "pm", "instructions": planner_instructions},
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
            {"id": "planning", "document": single("pm_plan_issues")},
            {"id": "delivery", "document": delivery},
            {"id": "correction", "document": correction},
            {"id": "review", "document": single("architect_review_published")},
        ],
        "routes": [
            {
                "id": "planning",
                "events": [PLANNING_EVENT],
                "team": "product",
                "workflow": "planning",
                "delegation": rules("planner"),
            },
            # Native PM Coordinator Binding: the PM proposes the Developer for each planned child.
            {
                "id": "issue_intake",
                "events": ["github.issue.ready"],
                "team": "product",
                "workflow": "delivery",
                "delegation": {"strategy": "coordinator", "eligible_agents": ["developer_one"]},
            },
            {
                "id": "review",
                "events": ["published.review"],
                "team": "product",
                "workflow": "review",
                "delegation": rules("reviewer"),
            },
            {
                "id": "correction",
                "events": ["published.correction"],
                "team": "product",
                "workflow": "correction",
                "delegation": rules("developer_one"),
            },
        ],
    }


def build_factories(
    *,
    repository: str,
    repository_id: int,
    state_root: Path,
    invoke_timeout: float,
    service_class: Any = None,
    planner_instructions: str = PLANNER_INSTRUCTIONS,
):
    """``service_class`` may be a ManagedOrganizationService subclass (pilot.py adds the dependency binding)."""
    from aitobuild.agent_tools import DeveloperToolContext
    from aitobuild.config import RuntimeConfig
    from aitobuild.organization import FileDefinitionStore, parse_organization_definition
    from aitobuild.organization_assignments import FileAssignmentStore
    from aitobuild.organization_delivery import (
        ManagedDeliveryBindings,
        NativeDeliveryImplementation,
    )
    from aitobuild.organization_planning import ManagedPlanning, PlanningAdmission, PlanningRoute
    from aitobuild.organization_reviews import PublishedReviewAdmission, PublishedReviewRoute
    from aitobuild.organization_roles import NativeCoordinatorProposal, NativeManagedRoles
    from aitobuild.organization_runner import FileRunStore
    from aitobuild.organization_runtime import bootstrap_organization, create_model_profile
    from aitobuild.organization_service import ManagedOrganizationService, ManagedRoute
    from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker
    from aitobuild.policy import AgentRole

    holder: dict[str, Any] = {}
    operator_id = "trial-operator"
    binding = "trial2-followups-v1"

    def service_factory(context):
        github = context.tools.github_adapter
        adapter = context.tools.container_session_adapter
        if github is None or adapter is None:
            raise ValueError("Trial requires gh_cli GitHub and container_session execution")
        definitions = FileDefinitionStore(state_root / "definitions")
        snapshot = definitions.save(
            parse_organization_definition(json.dumps(organization_document(planner_instructions)))
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

        def pins(event: str) -> dict[str, Any]:
            return {
                "repository": repository,
                "repository_id": repository_id,
                "organization_id": snapshot.organization_id,
                "revision": snapshot.revision,
                "event": event,
            }

        admission = PlanningAdmission(
            definitions=definitions,
            previews=context.previews,
            state_dir=state_root / "planning-admission",
            routes=(PlanningRoute(**pins(PLANNING_EVENT)),),
            operator_id=operator_id,
        )
        planning = ManagedPlanning(
            admission=admission,
            github=github,
            state_dir=state_root / "planning",
            lookup_max_pages=10,
        )
        reviews = PublishedReviewAdmission(
            definitions=definitions,
            previews=context.previews,
            worker=context.worker,
            github=github,
            state_dir=state_root / "published-reviews",
            routes=(PublishedReviewRoute(**pins("published.review")),),
            correction_routes=(PublishedReviewRoute(**pins("published.correction")),),
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
            planning=planning,
        )
        native_pm = NativeCoordinatorProposal(
            runtime_for=runtime_for,
            state_dir=state_root / "native-pm-coordinator",
            previews=context.previews,
            budget_path_for=context.worker.budget_path,
            event="github.issue.ready",
            coordinator_id="planner",
            binding_revision=binding,
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

        async def cleanup(task):
            role = next(
                a.role for a in task.snapshot.definition.agents if a.id == task.assignment.agent_id
            )
            await (bindings.cleanup(task) if role == AgentRole.DEVELOPER else roles.cleanup(task))

        service = (service_class or ManagedOrganizationService)(
            definitions=definitions,
            assignments=FileAssignmentStore(state_root / "assignments.json"),
            runs=FileRunStore(state_root / "runs"),
            previews=context.previews,
            worker=context.worker,
            routes=(ManagedRoute(**pins("github.issue.ready")),),
            reviews=reviews,
            operations={**roles.operations, **bindings.operations},
            cleanup=cleanup,
            operator_id=operator_id,
            binding_revision=binding,
            automatic_review_followups=True,
            coordinators={"planner": native_pm.service_binding},
            planning=admission,
            planning_publication=planning,
            approval_policy=context.approval_policy,
            task_base_revision_for=context.task_base_revision_for,
        )
        holder.update(service=service, previews=context.previews, planning_admission=admission)
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


class HandsOff:
    """Waits for the worker before any operator action and records every action the operator had to take."""

    def __init__(self, operator: Operator, report: dict[str, Any], pickup_seconds: float) -> None:
        self.operator, self.report, self.pickup = operator, report, pickup_seconds
        report.setdefault("operator_actions", [])
        report.setdefault("worker_pickups", [])

    def action(self, kind: str, what: str, **fields: Any) -> None:
        self.report["operator_actions"].append({"at": now(), "kind": kind, "what": what, **fields})
        self.operator.recorder.event("operator_action", action=kind, what=what, **fields)
        self.operator.recorder.save()

    def child(self, preview_id: str, what: str, *, human_admission: bool = False) -> None:
        """Wait for worker pickup, then admit. human_admission skips the wait: since #31 planning tasks
        never auto-admit, so the operator always approves them through the gated approval endpoint."""
        if human_admission:
            result = self.operator.approve_preview(preview_id, what)
            self.action(
                "human_task_approval_enqueued",
                what,
                preview_id=preview_id,
                admission=(result.get("managed_admission") or {}).get("state"),
                endpoint="POST /internal/developer/preview/approve (human planning admission, #31)",
            )
            return
        deadline = time.monotonic() + self.pickup
        while time.monotonic() < deadline:
            if self.operator.task(preview_id).get("managed_admission"):
                self.report["worker_pickups"].append(
                    {"at": now(), "what": what, "preview_id": preview_id}
                )
                self.operator.recorder.event("worker_pickup", what=what, preview_id=preview_id)
                return
            time.sleep(self.operator.poll)
        if self.operator.automatic:
            self.operator.post("/internal/organization/tasks/run", {"preview_id": preview_id})
            self.action(
                "run_trigger",
                what,
                preview_id=preview_id,
                endpoint="POST /internal/organization/tasks/run",
            )
        else:
            result = self.operator.approve_preview(preview_id, what)
            self.action(
                "human_task_approval_enqueued",
                what,
                preview_id=preview_id,
                admission=(result.get("managed_admission") or {}).get("state"),
                endpoint="POST /internal/developer/preview/approve (approval endpoint did the enqueue)",
            )

    def correction_automatic(self) -> bool:
        """Whether this session's pinned policy makes the ``correction`` stage automatic."""
        modes = (self.report.get("approval_policy") or {}).get("modes") or {}
        return modes.get("correction") == "automatic"

    def correction_child(self, preview_id: str, what: str, *, reason: str) -> None:
        """Admit a correction or a correction's re-review (``reason``: ``correction`` or ``correction_rereview``).

        Vertti's Oct 12 rule and #39: a correction's re-review inherits eligibility from the correction, so both are
        admitted automatically only when ``correction`` is automatic. Then the worker's own pickup is used (``child``).
        Otherwise the harness waits the pickup gate, records that nothing admitted the task, and admits it through
        ``admit_task(Human)`` (the human approval endpoint) as a scripted human step. It never sends the run trigger or
        any automatic/agent request for it, so there is no 409 path."""
        if self.correction_automatic():
            self.child(preview_id, what)
            return
        deadline, polls = time.monotonic() + self.pickup, 0
        admission = None
        while True:
            admission = self.operator.task(preview_id).get("managed_admission")
            polls += 1
            if admission or time.monotonic() >= deadline:
                break
            time.sleep(self.operator.poll)
        entry: dict[str, Any] = {
            "at": now(),
            "kind": SCRIPTED_HUMAN_ADMISSION,
            "reason": reason,
            "what": what,
            "preview_id": preview_id,
            "gate_seconds": self.pickup,
            "polls": polls,
            "auto_admitted": bool(admission),
        }
        records = self.report.setdefault("scripted_human_admissions", [])
        if admission:
            entry["unexpected_admission"] = admission
            records.append(entry)
            self.operator.recorder.event("scripted_human_admission_refused", **entry)
            self.operator.recorder.save()
            raise RuntimeError(f"{what} {preview_id} was admitted without a human: {admission}")
        result = self.operator.approve_preview(preview_id, what)
        entry.update(
            evidence_kind="human",
            admission=(result.get("managed_admission") or {}).get("state"),
            endpoint="POST /internal/developer/preview/approve (admit_task(Human), scripted human step)",
        )
        records.append(entry)
        self.action(
            SCRIPTED_HUMAN_ADMISSION,
            what,
            preview_id=preview_id,
            reason=reason,
            admission=entry["admission"],
            endpoint=entry["endpoint"],
        )

    def planned_children(self, timeout: float = 180) -> list[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.operator.client.get(
                "/internal/developer/previews",
                params={"pending_only": "false", "limit": 500},
                headers=self.operator.headers,
            )
            items = [
                item["preview_id"]
                for item in response.json()["items"]
                if str(item["bundle"].get("task_id", "")).startswith("planned-issue-")
            ]
            if items:
                return items
            time.sleep(self.operator.poll)
        raise RuntimeError("Planning published no staged Developer child")


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
            verification_command=VERIFY_COMMAND,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
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
    parser.add_argument("--sudo-docker", action="store_true")
    parser.add_argument("--approval-mode", choices=("human", "automatic"), default="automatic")
    parser.add_argument(
        "--automatic-stage",
        action="append",
        choices=ALL_STAGES,
        help="Only task_admission and developer_tools are accepted; any other stage is refused.",
    )
    parser.add_argument("--poll-seconds", type=positive, default=3.0)
    parser.add_argument(
        "--pickup-seconds",
        type=positive,
        default=90.0,
        help="How long to wait for the worker before the operator acts (recorded)",
    )
    parser.add_argument("--stage-timeout", type=positive, default=1800.0)
    parser.add_argument("--invoke-timeout", type=positive, default=600.0)
    arguments = parser.parse_args()

    if arguments.repository != DEFAULT_REPOSITORY and not arguments.allow_other_repository:
        raise SystemExit(
            f"Refusing {arguments.repository}: pass --allow-other-repository to leave {DEFAULT_REPOSITORY}"
        )
    if arguments.automatic_stage and arguments.approval_mode != "automatic":
        raise SystemExit("--automatic-stage requires --approval-mode automatic")
    automatic_stages = (
        tuple(dict.fromkeys(arguments.automatic_stage or ALLOWED_AUTOMATIC_STAGES))
        if arguments.approval_mode == "automatic"
        else ()
    )
    refused = set(automatic_stages) - set(ALLOWED_AUTOMATIC_STAGES)
    if refused:
        raise SystemExit(f"Refusing automatic approval for {sorted(refused)}")
    require_issue_author(automatic_stages, arguments.allowed_issue_author)
    work_root = arguments.work_root.resolve()
    if work_root == SERVICE_ROOT or SERVICE_ROOT in work_root.parents:
        raise SystemExit("--work-root must live outside the service checkout")

    stamp = datetime.now(tz=HELSINKI).strftime("%Y%m%d-%H%M%S")
    trial_id = f"trial2-{stamp}"
    run_dir = work_root / "runs" / trial_id
    recorder = Recorder(work_root / "artifacts" / trial_id)
    report = recorder.report
    report.update(
        trial="2: hands-off follow-ups",
        trial_id=trial_id,
        approval_mode=arguments.approval_mode,
        started_at=now(),
        repository=arguments.repository,
        model=arguments.model,
        service_commit=git(SERVICE_ROOT, "rev-parse", "HEAD"),
        run_dir=str(run_dir),
        result="running",
    )
    recorder.save()

    repo = gh_json("api", f"repos/{arguments.repository}")
    seed = run_dir / "seed"
    run_dir.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--branch",
            repo["default_branch"],
            f"https://github.com/{repo['full_name']}.git",
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
        values=build_trial_environment(
            arguments,
            repository=repo,
            seed=seed,
            base=base,
            run_dir=run_dir,
            secret=secret,
            token=token,
        ),
    )
    service_cwd = run_dir / "service"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-local", str(SERVICE_ROOT), str(service_cwd)], check=True
    )
    git(service_cwd, "checkout", "--quiet", "--detach", report["service_commit"])
    os.chdir(service_cwd)

    from fastapi.testclient import TestClient

    from aitobuild.app import create_app
    from aitobuild.config import load_config
    from aitobuild.developer_isolation import default_developer_isolation_policy
    from aitobuild.organization_planning import PlanningLimits, PlanningScope

    config = load_config()
    report["approval_policy"] = check_policy(config.approvals, automatic_stages)
    service_factory, worker_factory, holder = build_factories(
        repository=repo["full_name"],
        repository_id=repo["id"],
        state_root=run_dir / "state" / "organization",
        invoke_timeout=arguments.invoke_timeout,
    )
    app = create_app(
        config, managed_service_factory=service_factory, managed_worker_factory=worker_factory
    )

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
            hands = HandsOff(operator, report, arguments.pickup_seconds)

            # 1. Planning request: there is no public endpoint, so the trusted operator offers it in-process.
            planning = holder["planning_admission"].offer(
                request_id=trial_id,
                objective=OBJECTIVE.format(trial_id=trial_id),
                scope=PlanningScope(
                    repository=repo["full_name"],
                    repository_id=repo["id"],
                    base_revision=base,
                    base_branch=repo["default_branch"],
                    limits=PlanningLimits(max_issues=2, max_plan_bytes=12000),
                ),
                developer_policy=default_developer_isolation_policy(),
            )
            hands.action(
                "planning_offer_in_process",
                "PM planning request",
                preview_id=planning.preview_id,
                endpoint="none (PlanningAdmission.offer)",
            )
            report["phases"].append(
                {"phase": "planning", "started_at": now(), "preview_id": planning.preview_id}
            )
            hands.child(planning.preview_id, "PM planning task", human_admission=True)
            status = operator.drive(planning.preview_id, "planning")
            report["phases"][-1].update(
                ended_at=now(), run_state=(status.get("managed_run") or {}).get("state")
            )
            require_completed(status, "PM planning task")  # #83: the service-recorded run must have completed
            children = hands.planned_children()
            report["planned_children"] = children
            delivery_id = children[0]

            report["phases"].append(
                {"phase": "delivery", "started_at": now(), "preview_id": delivery_id}
            )
            hands.child(delivery_id, "Planned Developer child")
            status = operator.drive(delivery_id, "delivery")
            report["phases"][-1].update(
                ended_at=now(), run_state=(status.get("managed_run") or {}).get("state")
            )
            record = client.get(
                "/internal/developer/delivery/" + delivery_id, headers=operator.headers
            ).json()
            publication = (record.get("delivery") or {}).get("publication") or {}
            report["pull_request"] = {
                key: publication.get(key) for key in ("pull_number", "branch", "head_sha")
            }
            require_completed(status, "Planned Developer child")  # #83: the service-recorded run must have completed

            followup = operator.wait_follow_up(delivery_id, "delivery")
            if not followup or followup.get("state") != "staged":
                raise RuntimeError(f"Review follow-up was not staged: {followup}")
            review_id = followup["next_preview_id"]
            report["phases"].append(
                {"phase": "review", "started_at": now(), "preview_id": review_id}
            )
            hands.child(review_id, "Architect review")
            status = operator.drive(review_id, "review")
            report["phases"][-1].update(
                ended_at=now(), run_state=(status.get("managed_run") or {}).get("state")
            )
            require_completed(status, "Architect review")  # #83: the service-recorded run must have completed
            followup = operator.wait_follow_up(review_id, "review")
            # #83: a completed review counts only with its follow-up outcome (as pilot.deliver_to_merge_point);
            # without one (``None``, ``error``, ``escalated``) the session stops before any evidence is collected.
            report["review_outcome"] = (followup or {}).get("state")
            if report["review_outcome"] not in REVIEWED_OUTCOMES:
                raise RuntimeError(f"Architect review has no review outcome: {followup}")
            if followup and followup.get("state") == "staged":
                correction_id = followup["next_preview_id"]
                branch, head = report["pull_request"]["branch"], report["pull_request"]["head_sha"]
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
                    "reviewed head into local seed (service never fetches)",
                    branch=branch,
                    head_sha=head,
                )
                report["phases"].append(
                    {"phase": "correction", "started_at": now(), "preview_id": correction_id}
                )
                hands.correction_child(correction_id, "Correction", reason="correction")
                status = operator.drive(correction_id, "correction")
                report["phases"][-1].update(
                    ended_at=now(), run_state=(status.get("managed_run") or {}).get("state")
                )
                require_completed(status, "Correction")  # #83: the service-recorded run must have completed
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
                        "what": "same-PR correction publication",
                        "approval_digest": staged["digest"],
                        "at": now(),
                    }
                )
                rereview_id = (published.get("review_preview") or {}).get("preview_id")
                if rereview_id:
                    report["phases"].append(
                        {"phase": "re-review", "started_at": now(), "preview_id": rereview_id}
                    )
                    hands.correction_child(
                        rereview_id, "Architect re-review", reason="correction_rereview"
                    )
                    status = operator.drive(rereview_id, "re-review")
                    report["phases"][-1].update(
                        ended_at=now(), run_state=(status.get("managed_run") or {}).get("state")
                    )
                    require_completed(status, "Architect re-review")  # #83: the service-recorded run must have completed
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
        report["ended_at"] = now()
        recorder.save()
    return finish(report, recorder)


if __name__ == "__main__":
    sys.exit(main())
