"""Live supervised trial 3: one-service-run Developer -> draft PR -> Architect COMMENT -> correction -> re-review.

The harness drives the authenticated ASGI app (signed webhook ingress, gated internal endpoints) with the
real gh_cli adapter, a real Azure OpenAI v1 model and constrained Docker sessions. Every approval stays
``human`` and is given by the operator (this process) only through the normal endpoints; each decision is
recorded in the report. The only planned manual step is provisioning the reviewed head/published branch
into the private local seed before correction preparation (README "Complete Supervised Correction Cycle").

See scripts/trials/README.md for prerequisites and usage. Never point --work-root at the service checkout.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

HELSINKI = ZoneInfo("Europe/Helsinki")
SERVICE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENDPOINT = "https://aitobuild-agents2-resource.cognitiveservices.azure.com/openai/v1"
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


def now() -> str:
    return datetime.now(tz=HELSINKI).isoformat(timespec="seconds")


class Recorder:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.events = directory / "events.jsonl"
        self.report: dict[str, Any] = {"approvals": [], "stalls": [], "manual_steps": [], "phases": []}

    def event(self, kind: str, **fields: Any) -> None:
        line = {"at": now(), "kind": kind, **fields}
        with self.events.open("a") as handle:
            handle.write(json.dumps(line, default=str) + "\n")
        print(f"[{line['at']}] {kind} " + json.dumps(fields, default=str)[:400], flush=True)

    def save(self) -> None:
        (self.directory / "report.json").write_text(json.dumps(self.report, indent=2, default=str))


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True).stdout.strip()


def gh_json(*arguments: str) -> Any:
    return json.loads(subprocess.run(["gh", *arguments], check=True, capture_output=True, text=True).stdout)


def prepare_docker_wrapper(directory: Path) -> str:
    """Box user lacks docker socket access: expose a PATH-local ``docker`` that runs ``sudo -n docker``."""
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / "docker"
    wrapper.write_text('#!/bin/sh\nexec sudo -n /usr/bin/docker "$@"\n')
    wrapper.chmod(0o755)
    os.environ["PATH"] = f"{directory}:{os.environ['PATH']}"
    return str(wrapper)


def organization_document() -> dict[str, Any]:
    return {
        "schema_version": 1, "id": "trialorg",
        "agents": [
            {"id": "planner", "role": "pm", "instructions": "Coordinate the trial team."},
            {"id": "developer_one", "role": "developer", "instructions": DEVELOPER_INSTRUCTIONS},
            {"id": "reviewer", "role": "architect", "instructions": ARCHITECT_INSTRUCTIONS},
        ],
        "teams": [{"id": "product", "members": ["planner", "developer_one", "reviewer"], "coordinator": "planner"}],
        "workflows": [
            {"id": "delivery", "document": {
                "format": "python_graph", "start": "implement",
                "nodes": [{"id": "implement", "kind": "operation", "operation": "delivery_implement"},
                          {"id": "verify", "kind": "operation", "operation": "delivery_verify"},
                          {"id": "publish", "kind": "operation", "operation": "delivery_publish"}],
                "edges": [{"kind": "edge", "source": "implement", "target": "verify"},
                          {"kind": "edge", "source": "verify", "target": "publish"}],
                "outputs": ["publish"]}},
            {"id": "correction", "document": {
                "format": "python_graph", "start": "implement",
                "nodes": [{"id": "implement", "kind": "operation", "operation": "delivery_implement"},
                          {"id": "verify", "kind": "operation", "operation": "delivery_verify"}],
                "edges": [{"kind": "edge", "source": "implement", "target": "verify"}],
                "outputs": ["verify"]}},
            {"id": "review", "document": {
                "format": "python_graph", "start": "review",
                "nodes": [{"id": "review", "kind": "operation", "operation": "architect_review_published"}],
                "edges": [], "outputs": ["review"]}},
        ],
        "routes": [
            {"id": "issue_intake", "events": ["github.issue.ready"], "team": "product", "workflow": "delivery",
             "delegation": {"strategy": "rules", "eligible_agents": ["developer_one"], "target_agent": "developer_one"}},
            {"id": "review", "events": ["published.review"], "team": "product", "workflow": "review",
             "delegation": {"strategy": "rules", "eligible_agents": ["reviewer"], "target_agent": "reviewer"}},
            {"id": "correction", "events": ["published.correction"], "team": "product", "workflow": "correction",
             "delegation": {"strategy": "rules", "eligible_agents": ["developer_one"], "target_agent": "developer_one"}},
        ],
    }


def build_factories(*, repository: str, repository_id: int, state_root: Path, invoke_timeout: float):
    from aitobuild.agent_tools import DeveloperToolContext
    from aitobuild.config import RuntimeConfig
    from aitobuild.organization import FileDefinitionStore, parse_organization_definition
    from aitobuild.organization_assignments import FileAssignmentStore
    from aitobuild.organization_delivery import ManagedDeliveryBindings, NativeDeliveryImplementation
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
        snapshot = definitions.save(parse_organization_definition(json.dumps(organization_document())))
        profile = create_model_profile(RuntimeConfig(
            foundry_endpoint=os.environ["AITOBUILD_FOUNDRY_ENDPOINT"], foundry_api_key=None,
            foundry_model=os.environ["AITOBUILD_FOUNDRY_MODEL"], allow_mock_model=False))

        def runtime_for(pinned):
            return bootstrap_organization(pinned, model_profiles={"default": profile}, default_model_profile="default",
                                          state_dir=state_root / "agents")

        def route(event: str) -> PublishedReviewRoute:
            return PublishedReviewRoute(repository=repository, repository_id=repository_id,
                                        organization_id=snapshot.organization_id, revision=snapshot.revision, event=event)

        reviews = PublishedReviewAdmission(
            definitions=definitions, previews=context.previews, worker=context.worker, github=github,
            state_dir=state_root / "published-reviews", routes=(route("published.review"),),
            correction_routes=(route("published.correction"),), max_correction_rounds=1)
        roles = NativeManagedRoles(
            runtime_for=runtime_for, state_dir=state_root / "roles", proposal_event="github.issue.ready",
            worker=context.worker, github=github, invoke_timeout_seconds=invoke_timeout,
            review_target_for=lambda task: reviews.target_for(task.assignment.preview_id),
            allow_correction_proposals=True)
        tools = DeveloperToolContext(
            bash_adapter=context.tools.bash_adapter, filesystem_adapter=context.tools.filesystem_adapter,
            container_session_adapter=adapter, workspace_root=context.tools.workspace_root,
            require_human_approval_for_repo_writes=True)
        native = NativeDeliveryImplementation(worker=context.worker, runtime_for=runtime_for, tools=tools,
                                              state_dir=state_root / "native-delivery", invoke_timeout_seconds=invoke_timeout)
        bindings = ManagedDeliveryBindings(worker=context.worker, implementation=native, verification_adapter=adapter,
                                           github=github)
        operations = {**roles.operations, **bindings.operations}

        async def cleanup(task):
            role = next(agent.role for agent in task.snapshot.definition.agents if agent.id == task.assignment.agent_id)
            await (bindings.cleanup(task) if role == AgentRole.DEVELOPER else roles.cleanup(task))

        service = ManagedOrganizationService(
            definitions=definitions, assignments=FileAssignmentStore(state_root / "assignments.json"),
            runs=FileRunStore(state_root / "runs"), previews=context.previews, worker=context.worker,
            routes=(ManagedRoute(repository=repository, repository_id=repository_id, organization_id=snapshot.organization_id,
                                 revision=snapshot.revision, event="github.issue.ready"),),
            reviews=reviews, operations=operations, cleanup=cleanup, operator_id="trial-operator",
            binding_revision="trial3-correction-cycle-v1", automatic_review_followups=True,
            approval_policy=context.approval_policy, task_base_revision_for=context.task_base_revision_for)
        holder["service"] = service
        return service

    def worker_factory(service, context):
        return ManagedOrganizationWorker(service=service, store=FileWorkerStore(state_root / "organization-worker.json"),
                                         max_workers=2, recover_approved=False, queue_follow_ups=context.queue_follow_ups)

    return service_factory, worker_factory, holder


class Operator:
    def __init__(self, client, token: str, recorder: Recorder, poll: float, timeout: float) -> None:
        self.client = client
        self.headers = {"X-Internal-Token": token}
        self.recorder = recorder
        self.poll = poll
        self.timeout = timeout
        self.decided: set[str] = set()

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(path, headers=self.headers, json=body)
        if response.status_code != 200:
            raise RuntimeError(f"{path} -> {response.status_code}: {response.text[:2000]}")
        return response.json()

    def task(self, preview_id: str) -> dict[str, Any]:
        response = self.client.get(f"/internal/organization/tasks/{preview_id}", headers=self.headers)
        if response.status_code != 200:
            raise RuntimeError(f"task status {preview_id} -> {response.status_code}: {response.text[:2000]}")
        return response.json()

    def approve_preview(self, preview_id: str, what: str, base_revision: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"preview_id": preview_id}
        if base_revision is not None:
            body["base_revision"] = base_revision
        result = self.post("/internal/developer/preview/approve", body)
        self.recorder.report["approvals"].append({"stage": "task_admission", "what": what, "preview_id": preview_id,
                                                  "base_revision": base_revision, "at": now(), "via": "POST /internal/developer/preview/approve"})
        self.recorder.event("approve.task_admission", what=what, preview_id=preview_id,
                            admission=(result.get("managed_admission") or {}).get("state"))
        self.recorder.save()
        return result

    def drive(self, preview_id: str, label: str) -> dict[str, Any]:
        """Approve each exact pending service request (human stage) until the run is terminal."""
        deadline = time.monotonic() + self.timeout
        last = None
        while time.monotonic() < deadline:
            status = self.task(preview_id)
            job = status.get("managed_admission") or {}
            run = status.get("managed_run") or {}
            state = (job.get("state"), run.get("state"))
            if state != last:
                self.recorder.event("status", label=label, job=job.get("state"), run=run.get("state"),
                                    error=job.get("error") or run.get("error"))
                last = state
            if job.get("state") in {"failed", "cancelled"} or run.get("state") in {"failed", "cancelled"}:
                return status
            if run.get("state") == "completed" and job.get("state") in {None, "completed"}:
                return status
            if run.get("state") == "waiting" and job.get("state") == "waiting":
                for item in run.get("pending", []):
                    if item["request_id"] in self.decided or item["kind"] != "service_approval":
                        continue
                    self.decided.add(item["request_id"])
                    summary = json.dumps(item.get("data"), default=str)
                    self.recorder.report["approvals"].append({
                        "stage": item.get("approval_stage"), "what": f"{label}: {item.get('prompt')}",
                        "request_id": item["request_id"], "data_sha256": hashlib.sha256(summary.encode()).hexdigest(),
                        "data_preview": summary[:1500], "at": now(), "via": "POST /internal/organization/tasks/approve"})
                    self.recorder.event("approve." + str(item.get("approval_stage")), label=label, prompt=item.get("prompt"),
                                        data=summary[:600])
                    self.post("/internal/organization/tasks/approve", {
                        "assignment_id": run["assignment_id"], "request_id": item["request_id"], "approved": True})
                    self.recorder.save()
            time.sleep(self.poll)
        raise TimeoutError(f"{label} did not finish within {self.timeout}s")

    def wait_follow_up(self, preview_id: str, label: str, timeout: float = 120) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            followup = self.task(preview_id).get("follow_up")
            if followup and followup.get("state") != "pending":
                self.recorder.event("follow_up", label=label, follow_up=followup)
                return followup
            time.sleep(self.poll)
        return None


def signed_issue_webhook(client, secret: str, repository: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any]:
    payload = {"action": "opened", "repository": repository, "issue": issue, "sender": {"login": "trial-operator"}}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = client.post("/webhook", content=body, headers={
        "X-GitHub-Event": "issues", "X-GitHub-Delivery": str(uuid4()), "X-Hub-Signature-256": signature,
        "Content-Type": "application/json"})
    if response.status_code != 200:
        raise RuntimeError(f"webhook -> {response.status_code}: {response.text[:2000]}")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", default="uhvogala/aitobuild_example")
    parser.add_argument("--work-root", type=Path, default=Path("/workspace/trials"))
    parser.add_argument("--model", default="grok-4.6")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--sudo-docker", action="store_true", help="Run docker through a PATH-local `sudo -n docker` wrapper")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--stage-timeout", type=float, default=1800.0)
    parser.add_argument("--invoke-timeout", type=float, default=600.0)
    arguments = parser.parse_args()

    work_root = arguments.work_root.resolve()
    if work_root == SERVICE_ROOT or SERVICE_ROOT in work_root.parents:
        raise SystemExit("--work-root must live outside the service checkout")
    stamp = datetime.now(tz=HELSINKI).strftime("%Y%m%d-%H%M%S")
    trial_id = f"trial3-{stamp}"
    run_dir = work_root / "runs" / trial_id
    recorder = Recorder(work_root / "artifacts" / trial_id)
    report = recorder.report
    report.update({"trial": "3: uninterrupted supervised correction cycle", "trial_id": trial_id, "started_at": now(),
                   "repository": arguments.repository, "model": arguments.model, "endpoint": arguments.endpoint,
                   "service_commit": git(SERVICE_ROOT, "rev-parse", "HEAD"), "run_dir": str(run_dir), "result": "running"})
    recorder.save()

    repo = gh_json("api", f"repos/{arguments.repository}")
    repository = {"id": repo["id"], "full_name": repo["full_name"], "default_branch": repo["default_branch"]}
    seed = run_dir / "seed"
    run_dir.mkdir(parents=True)
    subprocess.run(["git", "clone", "--quiet", "--branch", repository["default_branch"],
                    f"https://github.com/{arguments.repository}.git", str(seed)], check=True)
    base = git(seed, "rev-parse", "HEAD")
    report["base_revision"] = base

    if arguments.sudo_docker:
        report["docker_access"] = {"mode": "PATH-local wrapper exec sudo -n /usr/bin/docker",
                                   "wrapper": prepare_docker_wrapper(run_dir / "bin")}
    secret, token = secrets.token_hex(24), secrets.token_hex(24)
    os.environ.update({
        "AITOBUILD_WEBHOOK_SECRET": secret, "AITOBUILD_INTERNAL_API_TOKEN": token,
        "AITOBUILD_REQUIRE_INTERNAL_AUTH": "true", "AITOBUILD_REQUIRE_APPROVAL_FOR_REPO_WRITES": "true",
        "AITOBUILD_REQUIRE_DEVELOPER_PREVIEW": "true", "AITOBUILD_ALLOW_MOCK_MODEL": "false",
        "AITOBUILD_FOUNDRY_ENDPOINT": arguments.endpoint, "AITOBUILD_FOUNDRY_MODEL": arguments.model,
        "AITOBUILD_DEVELOPER_EXECUTION_MODE": "container_session",
        "AITOBUILD_DEVELOPER_SESSION_CONTAINER_IMAGE": "aitobuild-developer:local",
        "AITOBUILD_DEVELOPER_STATE_DIR": str(run_dir / "state" / "developer"),
        "AITOBUILD_DEVELOPER_AGENT_INVOKE_TIMEOUT_SECONDS": str(int(arguments.invoke_timeout)),
        "AITOBUILD_DEVELOPER_REPOSITORY_SOURCES": json.dumps([{
            "repository": repository["full_name"], "repository_id": repository["id"], "path": str(seed),
            "verification_commands": [VERIFY_COMMAND]}]),
        "AITOBUILD_GITHUB_ADAPTER": "gh_cli", "AITOBUILD_GITHUB_DEFAULT_REPOSITORY": repository["full_name"],
        "AITOBUILD_GITHUB_ALLOWED_REPOS": repository["full_name"], "AITOBUILD_WEB_SEARCH_ADAPTER": "mock",
        "AITOBUILD_SCHEDULER_ENABLED": "false", "AITOBUILD_MANAGED_WORKER_QUEUE_FOLLOW_UPS": "true",
    })
    for key in ("AITOBUILD_FOUNDRY_API_KEY", "AITOBUILD_CONFIG_FILE"):
        os.environ.pop(key, None)
    # The app captures cwd as its service root (excluded from target sources; must be a Git checkout). Use a
    # throwaway clone of the service at this commit so nothing in the trial touches the developer's checkout.
    service_cwd = run_dir / "service"
    subprocess.run(["git", "clone", "--quiet", "--no-local", str(SERVICE_ROOT), str(service_cwd)], check=True)
    git(service_cwd, "checkout", "--quiet", "--detach", report["service_commit"])
    os.chdir(service_cwd)

    from fastapi.testclient import TestClient
    from aitobuild.app import create_app
    from aitobuild.config import load_config

    config = load_config()
    if any(stage != "human" for stage in (config.approvals.task_admission, config.approvals.developer_tools,
                                          config.approvals.draft_publication, config.approvals.review_publication,
                                          config.approvals.correction)):
        raise SystemExit("Trial 3 requires every approval stage to stay human")
    service_factory, worker_factory, holder = build_factories(
        repository=repository["full_name"], repository_id=repository["id"], state_root=run_dir / "state" / "organization",
        invoke_timeout=arguments.invoke_timeout)

    app = create_app(config, managed_service_factory=service_factory, managed_worker_factory=worker_factory)
    title = f"[Trial 3 correction cycle {stamp}] Add clamp(value, low, high) to math_ops"
    issue_url = subprocess.run(["gh", "issue", "create", "-R", arguments.repository, "--title", title,
                                "--body", ISSUE_BODY.format(trial_id=trial_id)], check=True, capture_output=True, text=True).stdout.strip()
    number = int(issue_url.rstrip("/").rsplit("/", 1)[1])
    issue = gh_json("api", f"repos/{arguments.repository}/issues/{number}")
    report["issue"] = {"number": number, "url": issue_url}
    recorder.event("issue.created", url=issue_url)
    recorder.save()

    try:
        with TestClient(app) as client:
            operator = Operator(client, token, recorder, arguments.poll_seconds, arguments.stage_timeout)
            hook = signed_issue_webhook(client, secret, repository, {
                "id": issue["id"], "number": issue["number"], "state": issue["state"], "title": issue["title"], "body": issue["body"]})
            delivery_id = (hook.get("metadata") or {}).get("preview_id")
            recorder.event("webhook", route=hook.get("route"), preview_id=delivery_id)
            if not delivery_id:
                raise RuntimeError(f"Webhook did not stage a preview: {hook}")

            report["phases"].append({"phase": "delivery", "started_at": now(), "preview_id": delivery_id})
            operator.approve_preview(delivery_id, "Developer delivery task", base_revision=base)
            status = operator.drive(delivery_id, "delivery")
            report["phases"][-1].update(ended_at=now(), run_state=(status.get("managed_run") or {}).get("state"))
            record = client.get("/internal/developer/delivery/" + delivery_id, headers=operator.headers).json()
            publication = (record.get("delivery") or record).get("publication") or {}
            report["pull_request"] = {"number": publication.get("pull_number"), "branch": publication.get("branch"),
                                      "head_sha": publication.get("head_sha"), "url": publication.get("url") or publication.get("html_url")}
            recorder.save()
            if (status.get("managed_run") or {}).get("state") != "completed":
                raise RuntimeError("Delivery did not complete: " + json.dumps(status, default=str)[:2000])

            followup = operator.wait_follow_up(delivery_id, "delivery")
            if not followup or followup.get("state") != "staged":
                raise RuntimeError(f"Review follow-up was not staged automatically: {followup}")
            review_id = followup["next_preview_id"]
            report["phases"].append({"phase": "review", "started_at": now(), "preview_id": review_id})
            operator.approve_preview(review_id, "Architect review task")
            status = operator.drive(review_id, "review")
            report["phases"][-1].update(ended_at=now(), run_state=(status.get("managed_run") or {}).get("state"),
                                        outputs=(status.get("managed_run") or {}).get("outputs"))
            if (status.get("managed_run") or {}).get("state") != "completed":
                raise RuntimeError("Review did not complete: " + json.dumps(status, default=str)[:2000])
            followup = operator.wait_follow_up(review_id, "review")
            if not followup or followup.get("state") != "staged":
                raise RuntimeError(f"Correction was not staged automatically after the COMMENT: {followup}")
            correction_id = followup["next_preview_id"]

            # Planned operator step: provision the reviewed head and published branch into the private seed.
            reviewed_head, branch = report["pull_request"]["head_sha"], report["pull_request"]["branch"]
            git(seed, "fetch", "--no-tags", "--quiet", "origin", f"+refs/heads/{branch}:refs/heads/{branch}")
            if git(seed, "rev-parse", f"refs/heads/{branch}") != reviewed_head:
                raise RuntimeError("Remote published branch no longer matches the reviewed head")
            report["manual_steps"].append({"step": "provision reviewed head into local seed (planned)", "branch": branch,
                                           "head_sha": reviewed_head, "at": now()})
            recorder.event("manual.seed_provisioned", branch=branch, head=reviewed_head)

            report["phases"].append({"phase": "correction", "started_at": now(), "preview_id": correction_id})
            operator.approve_preview(correction_id, "Developer correction task")
            status = operator.drive(correction_id, "correction")
            report["phases"][-1].update(ended_at=now(), run_state=(status.get("managed_run") or {}).get("state"))
            if (status.get("managed_run") or {}).get("state") != "completed":
                raise RuntimeError("Correction did not complete: " + json.dumps(status, default=str)[:2000])
            staged = operator.post("/internal/organization/corrections/stage-publication", {"correction_preview_id": correction_id})
            approval = staged["publish_approval"]
            report["correction_snapshot"] = {key: approval.get(key) for key in ("state", "digest")} | {
                "snapshot": {key: (approval.get("snapshot") or {}).get(key) for key in ("pull_number", "head_branch", "parent_head_sha")}}
            published = operator.post("/internal/organization/corrections/publish",
                                      {"correction_preview_id": correction_id, "approval_digest": approval["digest"]})
            report["approvals"].append({"stage": "correction", "what": "same-PR correction publication (exact staged digest)",
                                        "approval_digest": approval["digest"], "at": now(),
                                        "via": "POST /internal/organization/corrections/stage-publication + /publish"})
            delivery = published.get("delivery") or {}
            report["correction_publication"] = {"accepted": published.get("accepted"),
                                                "head_sha": (delivery.get("publication") or {}).get("head_sha"),
                                                "pull_number": (delivery.get("publication") or {}).get("pull_number")}
            recorder.event("correction.published", result=report["correction_publication"])
            recorder.save()
            rereview = published.get("review_preview") or {}
            if not published.get("accepted") or not rereview.get("preview_id"):
                raise RuntimeError("Correction publication did not stage a re-review: " + json.dumps(published, default=str)[:2000])

            rereview_id = rereview["preview_id"]
            report["phases"].append({"phase": "re-review", "started_at": now(), "preview_id": rereview_id})
            operator.approve_preview(rereview_id, "Architect re-review task")
            status = operator.drive(rereview_id, "re-review")
            report["phases"][-1].update(ended_at=now(), run_state=(status.get("managed_run") or {}).get("state"),
                                        outputs=(status.get("managed_run") or {}).get("outputs"))
            report["re_review_follow_up"] = operator.wait_follow_up(rereview_id, "re-review", timeout=30)
            if (status.get("managed_run") or {}).get("state") != "completed":
                raise RuntimeError("Re-review did not complete: " + json.dumps(status, default=str)[:2000])
            report["worker_diagnostics"] = client.get("/internal/organization/worker", headers=operator.headers).json()
            report["result"] = "pass"
    except BaseException as error:  # noqa: BLE001 - every failure must land in the report
        report["result"] = "fail"
        report["failure"] = f"{type(error).__name__}: {error}"
        recorder.event("failure", error=report["failure"])
        raise
    finally:
        number = (report.get("pull_request") or {}).get("number")
        if number:
            try:
                pull = gh_json("pr", "view", str(number), "-R", arguments.repository, "--json", "url,headRefOid,isDraft,state,reviews")
                report["pull_request"].update(url=pull["url"], final_head=pull["headRefOid"], is_draft=pull["isDraft"],
                                              state=pull["state"], reviews=[{"state": item["state"], "commit": (item.get("commit") or {}).get("oid")}
                                                                            for item in pull["reviews"]])
            except (subprocess.CalledProcessError, KeyError) as error:
                report["pull_request"]["inspect_error"] = str(error)
        report["ended_at"] = now()
        recorder.save()
    print(json.dumps({"result": report["result"], "report": str(recorder.directory / "report.json")}))
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
