"""Shared building blocks for live operator trials (recording, operator client, environment, policy checks).

Operator caveat: in human mode ``Operator.drive`` approves EVERY pending human-stage request (including each
Developer tool request) automatically, without a person looking at it. Each approval is recorded with a digest
and preview of the exact data so it can be audited afterwards, but nothing is reviewed before it is approved.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from aitobuild.developer_preview import (
    PLANNED_CHILD_TASK_PREFIXES,
    PLANNING_TASK_PREFIX,
    REVIEW_TASK_PREFIX,
    is_correction_task,
)

HELSINKI = ZoneInfo("Europe/Helsinki")
DEFAULT_REPOSITORY = "uhvogala/aitobuild_example"
DEFAULT_ENDPOINT = "https://aitobuild-agents2-resource.cognitiveservices.azure.com/openai/v1"
#: The only stages a trial may set to automatic (ReviewBot condition). Everything else stays human; merge disabled.
ALLOWED_AUTOMATIC_STAGES = ("task_admission", "developer_tools")
POLICY_LIMIT_KEYS = frozenset({"max_automatic_decisions", "max_automatic_admissions"})
HUMAN_STAGES = ("draft_publication", "review_publication")
ALL_STAGES = (
    "task_admission",
    "developer_tools",
    "pm_issue_creation",
    "pm_dependency_links",
    "draft_publication",
    "review_publication",
    "meeting_continuation",
    "correction",
    "merge",
)


def now() -> str:
    return datetime.now(tz=HELSINKI).isoformat(timespec="seconds")


class Recorder:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.events = directory / "events.jsonl"
        self.report: dict[str, Any] = {
            "approvals": [],
            "stalls": [],
            "manual_steps": [],
            "phases": [],
        }

    def event(self, kind: str, **fields: Any) -> None:
        line = {"at": now(), "kind": kind, **fields}
        with self.events.open("a") as handle:
            handle.write(json.dumps(line, default=str) + "\n")
        print(f"[{line['at']}] {kind} " + json.dumps(fields, default=str)[:400], flush=True)

    def save(self) -> None:
        from aitobuild.durable_files import atomic_write_text

        atomic_write_text(
            self.directory / "report.json", json.dumps(self.report, indent=2, default=str)
        )


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def gh_json(*arguments: str) -> Any:
    return json.loads(
        subprocess.run(["gh", *arguments], check=True, capture_output=True, text=True).stdout
    )


def prepare_docker_wrapper(directory: Path) -> str:
    """Box user lacks docker socket access: expose a PATH-local ``docker`` that runs ``sudo -n docker``."""
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / "docker"
    wrapper.write_text('#!/bin/sh\nexec sudo -n /usr/bin/docker "$@"\n')
    wrapper.chmod(0o755)
    os.environ["PATH"] = f"{directory}:{os.environ['PATH']}"
    return str(wrapper)


def queued_without_worker(diagnostics: dict[str, Any]) -> str | None:
    """The reason a queued job cannot run, from ``/internal/organization/worker`` (#69), or ``None``.

    Fails closed: a worker status without the health signal is treated as no live loop."""
    health = diagnostics.get("health")
    if not isinstance(health, dict) or not isinstance(health.get("workers_alive"), int):
        return "the worker status has no loop health signal"
    if health["workers_alive"] > 0:
        return None
    errors = "; ".join(str(error)[:300] for error in (diagnostics.get("errors") or [])[:3])
    return (
        f"no managed worker loop is alive (status {health.get('status')}, "
        f"0/{health.get('workers_expected')} loops)" + (f": {errors}" if errors else "")
    )


#: Waits before each retry of a ``transient`` 503 (#72); their count is the per-call retry bound.
TRANSIENT_RETRY_DELAYS = (2.0, 5.0, 10.0)


def is_transient_failure(response: Any) -> bool:
    """The service's GitHub transport/availability answer (#72): 503 with ``transient: true``. A 409 never is."""
    if response.status_code != 503:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("transient") is True


class Operator:
    def __init__(
        self,
        client,
        token: str,
        recorder: Recorder,
        poll: float,
        timeout: float,
        automatic_stages: tuple[str, ...] = (),
        transient_retry_delays: tuple[float, ...] = TRANSIENT_RETRY_DELAYS,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self.automatic_stages = automatic_stages
        self.automatic = bool(automatic_stages)
        self.client = client
        self.headers = {"X-Internal-Token": token}
        self.recorder = recorder
        self.poll = poll
        self.timeout = timeout
        self.decided: set[str] = set()
        self.transient_retry_delays = transient_retry_delays
        self._sleep = sleep

    def _request(self, method: str, path: str, label: str, body: dict[str, Any] | None = None,
                 landed: Callable[[], bool] | None = None) -> dict[str, Any]:
        """The harness's one HTTP helper. Anything but 200 is fatal, with one exception (#72): a ``transient`` 503
        is retried, at most ``len(transient_retry_delays)`` times. A read retries as is; a write retries only when
        its ``landed`` re-read of state shows nothing landed, and a write without one never retries. A 409 (a
        policy refusal) always stays fatal. Each retry is counted in ``report.transient_retries``."""
        for attempt in range(len(self.transient_retry_delays) + 1):
            if method == "GET":
                response = self.client.get(path, headers=self.headers)
            else:
                response = self.client.post(path, headers=self.headers, json=body)
            if response.status_code == 200:
                return response.json()
            failure = f"{label} -> {response.status_code}: {response.text[:2000]}"
            if (not is_transient_failure(response) or attempt >= len(self.transient_retry_delays)
                    or (method != "GET" and landed is None)):
                raise RuntimeError(failure)
            if method != "GET" and landed is not None and landed():
                raise RuntimeError(failure + " (state re-read shows the write landed; not retried)")
            delay = self.transient_retry_delays[attempt]
            self.recorder.report.setdefault("transient_retries", []).append(
                {"at": now(), "method": method, "path": path, "attempt": attempt + 1, "delay_seconds": delay,
                 "detail": str(response.json().get("detail"))[:500]}
            )
            self.recorder.event("transient_retry", method=method, path=path, attempt=attempt + 1)
            self.recorder.save()
            self._sleep(delay)
        raise RuntimeError("Internal invariant violated: transient retry loop exited")

    def post(self, path: str, body: dict[str, Any], *, landed: Callable[[], bool] | None = None) -> dict[str, Any]:
        return self._request("POST", path, path, body, landed)

    def task(self, preview_id: str) -> dict[str, Any]:
        return self._request("GET", f"/internal/organization/tasks/{preview_id}", f"task status {preview_id}")

    def _decision_landed(self, preview_id: str, request_id: str) -> bool:
        """Re-read state after a transient decision failure: did the decision land? It did not only while the job
        (if any) and the run still wait on exactly this request."""
        status = self.task(preview_id)
        job = status.get("managed_admission") or {}
        run = status.get("managed_run") or {}
        pending = job.get("state") in {None, "waiting"} and run.get("state") == "waiting" and any(
            item.get("request_id") == request_id for item in run.get("pending", []))
        return not pending

    def admit(
        self, preview_id: str, what: str, base_revision: str | None = None, timeout: float = 120
    ) -> None:
        """Human mode: approve the task. Automatic mode: never approve; wait for policy admission by the worker,
        and only if nothing picked the task up, send the authenticated run trigger (recorded, not an approval)."""
        if not self.automatic:
            self.approve_preview(preview_id, what, base_revision)
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.task(preview_id).get("managed_admission"):
                self.recorder.event("admission.automatic_pickup", what=what, preview_id=preview_id)
                return
            time.sleep(self.poll)
        result = self.post("/internal/organization/tasks/run", {"preview_id": preview_id})
        self.recorder.report["manual_steps"].append(
            {
                "step": "operator run trigger (POST /internal/organization/tasks/run; no approval value)",
                "what": what,
                "preview_id": preview_id,
                "at": now(),
                "admission": (result.get("managed_admission") or {}).get("state"),
            }
        )
        self.recorder.event("trigger.run", what=what, preview_id=preview_id)
        self.recorder.save()

    def approve_preview(
        self, preview_id: str, what: str, base_revision: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"preview_id": preview_id}
        if base_revision is not None:
            body["base_revision"] = base_revision
        result = self.post("/internal/developer/preview/approve", body)
        self.recorder.report["approvals"].append(
            {
                "stage": "task_admission",
                "what": what,
                "preview_id": preview_id,
                "base_revision": base_revision,
                "at": now(),
                "via": "POST /internal/developer/preview/approve",
            }
        )
        self.recorder.event(
            "approve.task_admission",
            what=what,
            preview_id=preview_id,
            admission=(result.get("managed_admission") or {}).get("state"),
        )
        self.recorder.save()
        return result

    def require_live_worker(self, preview_id: str, label: str) -> None:
        """#69: a queued job with no live managed worker loop can never run; fail the run now with the reason
        instead of waiting out the stage timeout (pilot trial4-20261018-234433 sat 22 minutes on one)."""
        response = self.client.get("/internal/organization/worker", headers=self.headers)
        if response.status_code == 404:
            return  # no detached worker in this app: nothing to check
        if response.status_code != 200:
            raise RuntimeError(
                f"worker status -> {response.status_code}: {response.text[:2000]}"
            )
        diagnostics = response.json()
        problem = queued_without_worker(diagnostics)
        if problem is None:
            return
        self.recorder.report.setdefault("stalls", []).append(
            {
                "at": now(),
                "label": label,
                "preview_id": preview_id,
                "detail": problem,
                "worker_errors": list(diagnostics.get("errors") or [])[:8],
            }
        )
        self.recorder.save()
        raise RuntimeError(f"{label}: job {preview_id} is queued but {problem}")

    def drive(self, preview_id: str, label: str) -> dict[str, Any]:
        """Approve each exact pending service request (human stage) until the run is terminal."""
        deadline = time.monotonic() + self.timeout
        last = None
        while time.monotonic() < deadline:
            status = self.task(preview_id)
            job = status.get("managed_admission") or {}
            run = status.get("managed_run") or {}
            if job.get("state") == "queued":
                self.require_live_worker(preview_id, label)
            state = (job.get("state"), run.get("state"))
            if state != last:
                self.recorder.event(
                    "status",
                    label=label,
                    job=job.get("state"),
                    run=run.get("state"),
                    error=job.get("error") or run.get("error"),
                )
                last = state
            if job.get("state") in {"failed", "cancelled"} or run.get("state") in {
                "failed",
                "cancelled",
            }:
                return status
            if run.get("state") == "completed" and job.get("state") in {None, "completed"}:
                return status
            if run.get("state") == "waiting" and job.get("state") == "waiting":
                for item in run.get("pending", []):
                    if item["request_id"] in self.decided or item["kind"] != "service_approval":
                        continue
                    if self.automatic and item.get("approval_stage") in self.automatic_stages:
                        self.recorder.report["stalls"].append(
                            {
                                "at": now(),
                                "label": label,
                                "pending_stage": item.get("approval_stage"),
                                "detail": "policy-automatic stage waited for a human",
                            }
                        )
                        self.recorder.save()
                        raise RuntimeError(
                            f"{label}: {item.get('approval_stage')} waited for a human in automatic mode"
                        )
                    self.decided.add(item["request_id"])
                    summary = json.dumps(item.get("data"), default=str)
                    self.recorder.report["approvals"].append(
                        {
                            "stage": item.get("approval_stage"),
                            "what": f"{label}: {item.get('prompt')}",
                            "request_id": item["request_id"],
                            "data_sha256": hashlib.sha256(summary.encode()).hexdigest(),
                            "data_preview": summary[:1500],
                            "at": now(),
                            "via": "POST /internal/organization/tasks/approve",
                        }
                    )
                    self.recorder.event(
                        "approve." + str(item.get("approval_stage")),
                        label=label,
                        prompt=item.get("prompt"),
                        data=summary[:600],
                    )
                    self.post(
                        "/internal/organization/tasks/approve",
                        {
                            "assignment_id": run["assignment_id"],
                            "request_id": item["request_id"],
                            "approved": True,
                        },
                        landed=lambda request_id=item["request_id"]: self._decision_landed(preview_id, request_id),
                    )
                    self.recorder.save()
            time.sleep(self.poll)
        raise TimeoutError(f"{label} did not finish within {self.timeout}s")

    def wait_follow_up(
        self, preview_id: str, label: str, timeout: float = 120
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            followup = self.task(preview_id).get("follow_up")
            if followup and followup.get("state") != "pending":
                self.recorder.event("follow_up", label=label, follow_up=followup)
                return followup
            time.sleep(self.poll)
        return None


def signed_issue_webhook(
    client, secret: str, repository: dict[str, Any], issue: dict[str, Any]
) -> dict[str, Any]:
    payload = {
        "action": "opened",
        "repository": repository,
        "issue": issue,
        "sender": {"login": "trial-operator"},
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": str(uuid4()),
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"webhook -> {response.status_code}: {response.text[:2000]}")
    return response.json()


def finish(report: dict[str, Any], recorder: Recorder) -> int:
    print(
        json.dumps({"result": report["result"], "report": str(recorder.directory / "report.json")})
    )
    return 0 if report["result"] == "pass" else 1


#: Report kind of a task the harness admitted as a scripted human step (followups.HandsOff.correction_child).
#: Defined only here; followups.py imports it, so the harness and the evidence check cannot drift apart.
SCRIPTED_HUMAN_ADMISSION = "scripted_human_admission"


def reviews_correction(source_payload: dict[str, Any] | None, preview_of: Any) -> bool:
    """Whether a staged review task reviews a correction: its ``published_review`` target is a correction preview
    (the full ``is_correction_task`` rule). A missing target or preview is not a correction re-review."""
    target = (source_payload or {}).get("published_review")
    if not isinstance(target, dict) or not isinstance(target.get("preview_id"), str):
        return False
    parent = preview_of(target["preview_id"])
    return parent is not None and is_correction_task(parent)


def expected_evidence_kind(
    task_id: str,
    source_payload: dict[str, Any] | None = None,
    correction_automatic: bool = False,
    rereviews_correction: bool = False,
) -> str:
    """The admit_task evidence kind a task gets under automatic task admission: planning (#31) and corrections
    (correction: human) stay human-admitted. A correction's re-review inherits from the correction (Vertti's Oct 12
    rule, #39): ``inherited_review`` only when ``correction`` is automatic, otherwise a human admission."""
    if task_id.startswith(PLANNING_TASK_PREFIX):
        return "human"
    task = SimpleNamespace(bundle_payload={"task_id": task_id}, source_payload=dict(source_payload or {}))
    if is_correction_task(task):  # the one shared correction rule (#54/#61), never a hand-written copy here
        # correction: automatic lets the service admit corrections automatically; otherwise they stay human.
        return "operator_policy" if correction_automatic else "human"
    if task_id.startswith(REVIEW_TASK_PREFIX):
        if rereviews_correction and not correction_automatic:
            return "human"
        return "inherited_review"
    if task_id.startswith(PLANNED_CHILD_TASK_PREFIXES):
        return "planned_child"
    return "operator_policy"


def admission_problem(
    phase: str,
    task_id: str,
    admission: dict[str, Any] | None,
    evidence_kind: str | None,
    modes: dict[str, Any],
    revision: str,
    source_payload: dict[str, Any] | None = None,
    rereviews_correction: bool = False,
    scripted_human: dict[str, Any] | None = None,
) -> str | None:
    """Verdict on one phase's task admission. With automatic task admission, planning tasks must be human-admitted
    (#31) and corrections must be human-admitted (no policy decision, evidence kind human) and every other task must be an automatic policy decision at the
    pinned revision through its expected evidence kind (planned child, inherited review, operator policy).

    A correction's re-review under a non-automatic ``correction`` stage must be human-admitted through the harness's
    recorded ``scripted_human_admission`` step (gate passed, not auto-admitted). A scripted human step never counts as
    an automatic admission: any task expected to be admitted automatically (review inheritance included) that carries
    one fails."""
    if modes.get("task_admission") != "automatic":
        return None
    correction_automatic = modes.get("correction") == "automatic"
    want = expected_evidence_kind(
        task_id, source_payload, correction_automatic, rereviews_correction
    )
    if want == "human":
        if admission is not None or evidence_kind != "human":
            return f"{phase}: planning/correction task not human-admitted (evidence={evidence_kind})"
        if task_id.startswith(REVIEW_TASK_PREFIX) and rereviews_correction:
            if (
                scripted_human is None
                or scripted_human.get("kind") != SCRIPTED_HUMAN_ADMISSION
                or scripted_human.get("auto_admitted") is not False
                or scripted_human.get("evidence_kind") != "human"
            ):
                return f"{phase}: correction re-review has no recorded scripted human admission after its gate"
        return None
    if scripted_human is not None:
        return f"{phase}: scripted human admission recorded where {want} admission was expected"
    if (
        not admission
        or admission.get("source") != "operator_policy"
        or admission.get("policy_revision") != revision
    ):
        return f"{phase}: admission not an automatic policy decision at the pinned revision"
    if evidence_kind != want:
        return f"{phase}: admitted as {evidence_kind}, expected {want}"
    return None


def pin_session_policy(
    report: dict[str, Any], policy: dict[str, Any], override: dict[str, str] | None = None
) -> dict[str, Any]:
    """#65: pin this session's approval policy (``check_policy`` output) on the report.

    The first session pins it. A later session with the same policy just records it; a different one is refused
    unless ``override`` names the human ``actor`` and a ``reason``, which is recorded once with both revisions. Every
    revision a session ran under is kept in ``approval_policies`` so each phase is judged by its own policy."""
    prior = report.get("pinned_policy") or report.get("approval_policy")
    revision = policy["revision"]
    if prior is not None and prior["revision"] != revision:
        if override is None:
            raise SystemExit(
                f"Refusing to resume under a different approval policy: pinned {prior['revision']}, these flags give "
                f"{revision}. Pass --policy-override-actor and --policy-override-reason to record a human override."
            )
        if not all(isinstance(override.get(key), str) and override[key].strip() for key in ("actor", "reason")):
            raise SystemExit("A policy override needs a human actor and a reason")
        report.setdefault("policy_overrides", []).append(
            {
                "at": now(),
                "evidence_kind": "human",
                "actor": override["actor"].strip(),
                "reason": override["reason"].strip(),
                "previous_revision": prior["revision"],
                "revision": revision,
            }
        )
    elif override is not None:
        raise SystemExit("--policy-override-* given but the approval policy did not change; nothing to override")
    policies = report.setdefault("approval_policies", {})
    if prior is not None:
        policies.setdefault(prior["revision"], prior["modes"])
        report.setdefault("start_policy_revision", prior["revision"])
    policies.setdefault(revision, policy["modes"])
    report.setdefault("start_policy_revision", revision)
    report["pinned_policy"] = policy
    report["approval_policy"] = policy
    sessions = report.get("sessions") or []
    if sessions:
        sessions[-1]["approval_policy_revision"] = revision
    return policy


def phase_policy_revision(report: dict[str, Any], phase: dict[str, Any]) -> str:
    """The policy a phase ran under: its own stamp, else (phases from before #65) the run's start policy."""
    return (
        phase.get("policy_revision")
        or report.get("start_policy_revision")
        or report["approval_policy"]["revision"]
    )


def recorded_policy_modes(report: dict[str, Any], revision: str | None) -> dict[str, Any] | None:
    if revision is None:
        return None
    policies = report.get("approval_policies") or {}
    if revision in policies:
        return policies[revision]
    current = report.get("approval_policy") or {}
    return current.get("modes") if current.get("revision") == revision else None


def run_decision_revision(run: Any, index: int, fallback: str) -> str | None:
    """The run's pinned policy for its decision at ``index`` (start revision or the override in force)."""
    if run is None:
        return fallback
    by_index = getattr(run, "policy_revision_for_decision", None)
    return by_index(index) if callable(by_index) else run.approval_policy_revision


def runs_needing_policy_override(report: dict[str, Any], task_of: Any, revision: str) -> list[str]:
    """Assignment IDs of the report's non-finished runs still pinned to another policy than ``revision``; with a
    recorded harness override, each gets the service's own human override before it is resumed."""
    found = []
    for phase in report.get("phases", []):
        run = (task_of(phase["preview_id"]) or {}).get("managed_run") or {}
        if not run or run.get("state") in {"completed", "failed", "cancelled", "finalizing"}:
            continue
        overrides = run.get("policy_overrides") or []
        pinned = overrides[-1]["revision"] if overrides else run.get("approval_policy_revision")
        if pinned != revision and run["assignment_id"] not in found:
            found.append(run["assignment_id"])
    return found


#: Follow-up outcomes of a review run that make a merge point a reviewed one (#83). ``None`` (no follow-up, as after
#: a failed review), ``error`` and ``escalated`` are not.
REVIEWED_OUTCOMES = frozenset({"no_correction", "staged"})


def incomplete_run(label: str, run: Any) -> str | None:
    """#83: a problem when the service-recorded run of a phase did not complete; ``None`` when it completed.

    ``run`` is the service's own record (``ManagedRun`` or its status JSON), never the harness's lists."""
    if isinstance(run, dict):
        state, error = run.get("state"), run.get("error")
    else:
        state, error = getattr(run, "state", None), getattr(run, "error", None)
    if state == "completed":
        return None
    detail = f": {str(error)[:300]}" if error else ""
    return f"{label}: service-recorded run is {state or 'missing'}, not completed{detail}"


def require_completed(status: dict[str, Any], label: str) -> dict[str, Any]:
    """#83: stop the session unless the service-recorded run behind ``status`` completed."""
    problem = incomplete_run(label, status.get("managed_run"))
    if problem is not None:
        raise RuntimeError(problem + " | status: " + json.dumps(status, default=str)[:1500])
    return status


def collect_evidence(holder: dict[str, Any], report: dict[str, Any], policy: Any) -> dict[str, Any]:
    """Read saved preview/run state and classify every recorded decision as automatic or human.

    #65: each phase is judged by the policy recorded for that phase, and each run decision by the policy the run
    was pinned to when it was made, never by the latest session's policy."""
    service, previews = holder["service"], holder["previews"]
    report.setdefault("approval_policies", {}).setdefault(
        policy.revision, policy.model_dump(mode="json")
    )
    phases, problems = [], []
    scripted = {
        item["preview_id"]: item
        for item in report.get("scripted_human_admissions", [])
        if item.get("kind") == SCRIPTED_HUMAN_ADMISSION
    }
    inherited: list[str] = []
    for phase in report["phases"]:
        revision = phase_policy_revision(report, phase)
        modes = recorded_policy_modes(report, revision)
        preview = previews.get(phase["preview_id"])
        run = service.status(phase["preview_id"])
        # #83: every phase counts only when the service-recorded run completed, whatever the harness wrote.
        unfinished = incomplete_run(phase["phase"], run)
        if unfinished is not None:
            problems.append(unfinished)
        if modes is None:
            problems.append(f"{phase['phase']}: phase policy {revision} was never recorded")
            continue
        admission = (
            preview.approval_policy.model_dump(mode="json") if preview.approval_policy else None
        )
        decisions = [
            {
                "request_id": item.request_id,
                "kind": item.kind,
                "actor_id": item.actor_id,
                "pinned_policy_revision": run_decision_revision(run, index, revision),
                "policy": item.approval_policy.model_dump(mode="json")
                if item.approval_policy
                else None,
            }
            for index, item in enumerate(run.decisions if run else ())
        ]
        count = sum(
            1
            for item in decisions
            if item["policy"] and item["policy"]["source"] == "operator_policy"
        )
        task_id = str((preview.bundle_payload if preview else {}).get("task_id", ""))
        source_payload = preview.source_payload if preview else None
        rereview = task_id.startswith(REVIEW_TASK_PREFIX) and reviews_correction(
            source_payload, previews.get
        )
        script = scripted.get(phase["preview_id"])
        evidence_kind = preview.evidence_kind if preview else None
        admission_path = (
            SCRIPTED_HUMAN_ADMISSION
            if script is not None
            else "automatic"
            if admission and admission.get("source") == "operator_policy"
            else "human"
        )
        if admission_path == "automatic" and evidence_kind == "inherited_review" and unfinished is None:
            # A scripted human step is never counted here, and neither is a review whose run did not complete (#83).
            inherited.append(phase["phase"])
        phases.append(
            {
                "phase": phase["phase"],
                "preview_id": phase["preview_id"],
                "policy_revision": revision,
                "admission": admission,
                "evidence_kind": evidence_kind,
                "admission_path": admission_path,
                "run_state": getattr(run, "state", None),
                "rereviews_correction": rereview,
                "run_policy_revision": run.approval_policy_revision if run else None,
                "run_policy_overrides": [
                    item.model_dump(mode="json") for item in getattr(run, "policy_overrides", ())
                ],
                "automatic_decisions": count,
                "decisions": decisions,
            }
        )
        problem = admission_problem(
            phase["phase"],
            task_id,
            admission,
            evidence_kind,
            modes,
            revision,
            source_payload,
            rereview,
            script,
        )
        if problem:
            problems.append(problem)
        if run is not None and run.approval_policy_revision != revision:
            problems.append(
                f"{phase['phase']}: run started under {run.approval_policy_revision}, phase policy {revision}"
            )
        for item in decisions:
            stage, source = (
                (item["policy"] or {}).get("stage"),
                (item["policy"] or {}).get("source"),
            )
            pinned = item["pinned_policy_revision"]
            decision_modes = recorded_policy_modes(report, pinned) or {}
            want = "operator_policy" if decision_modes.get(stage) == "automatic" else "human"
            if (
                item["policy"] is None
                or source != want
                or item["policy"]["policy_revision"] != pinned
            ):
                problems.append(
                    f"{phase['phase']}: decision {item['request_id']} {stage} source={source}, expected {want} under {pinned}"
                )
        limit = modes.get("max_automatic_decisions", policy.max_automatic_decisions)
        if count > limit:
            problems.append(f"{phase['phase']}: {count} automatic decisions exceed max {limit}")
    for point in report.get("merge_points", []):
        # #83: a merge point is a reviewed one only with the review run's follow-up outcome.
        if point.get("review_outcome") not in REVIEWED_OUTCOMES:
            problems.append(
                f"merge point {point.get('label')} (PR #{point.get('pull_number')}): review_outcome "
                f"{point.get('review_outcome')!r} is not a completed review"
            )
    return {
        "ok": not problems,
        "problems": problems,
        "max_automatic_decisions": policy.max_automatic_decisions,
        "policy_revision": policy.revision,
        "phases": phases,
        "inherited_reviews": inherited,
        "scripted_human_admissions": sorted(scripted),
    }


def positive(value: str) -> float:
    """argparse type for strictly positive, finite seconds."""
    number = float(value)
    if not number > 0 or number == float("inf"):
        raise ValueError("must be a positive number")
    return number


GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}")


def issue_author(value: str) -> str:
    """argparse type for one exact GitHub login: no blanks, wildcards, lists or other patterns."""
    # The pattern alone allows up to 77 characters when hyphens are used; GitHub logins are at most 39.
    if len(value) > 39 or not GITHUB_LOGIN.fullmatch(value):
        raise ValueError("must be exactly one GitHub login (no wildcards, blanks or lists)")
    return value


def add_issue_author_argument(parser: Any) -> None:
    parser.add_argument(
        "--allowed-issue-author",
        type=issue_author,
        default=None,
        help="The one GitHub login whose issues automatic task admission may accept; required for automatic runs.",
    )


def require_issue_author(automatic_stages: tuple[str, ...], author: str | None) -> None:
    """Automatic runs refuse to start unless the operator named the issue author explicitly."""
    if automatic_stages and author is None:
        raise SystemExit("--allowed-issue-author is required for automatic runs")


def repository_source(
    *,
    repository: str,
    repository_id: int,
    path: Path,
    verification_command: str,
    base_revision: str | None,
    issue_author: str | None,
) -> dict[str, Any]:
    """The single repository source every trial binds; automatic admission only accepts the named author."""
    source: dict[str, Any] = {
        "repository": repository,
        "repository_id": repository_id,
        "path": str(path),
        "verification_commands": [verification_command],
    }
    if base_revision is not None:
        source["base_revision"] = base_revision
    if issue_author is not None:
        source["allowed_issue_authors"] = [issue_author]
    return source


def trial_environment_values(
    *,
    source: dict[str, Any],
    run_dir: Path,
    endpoint: str,
    model: str,
    invoke_timeout: float,
    secret: str,
    token: str,
) -> dict[str, str]:
    """Process environment shared by every trial script (secrets stay in this process only)."""
    repository = source["repository"]
    return {
        "AITOBUILD_WEBHOOK_SECRET": secret,
        "AITOBUILD_INTERNAL_API_TOKEN": token,
        "AITOBUILD_REQUIRE_INTERNAL_AUTH": "true",
        "AITOBUILD_REQUIRE_APPROVAL_FOR_REPO_WRITES": "true",
        "AITOBUILD_REQUIRE_DEVELOPER_PREVIEW": "true",
        "AITOBUILD_ALLOW_MOCK_MODEL": "false",
        "AITOBUILD_FOUNDRY_ENDPOINT": endpoint,
        "AITOBUILD_FOUNDRY_MODEL": model,
        "AITOBUILD_DEVELOPER_EXECUTION_MODE": "container_session",
        "AITOBUILD_DEVELOPER_SESSION_CONTAINER_IMAGE": "aitobuild-developer:local",
        "AITOBUILD_DEVELOPER_STATE_DIR": str(run_dir / "state" / "developer"),
        "AITOBUILD_DEVELOPER_AGENT_INVOKE_TIMEOUT_SECONDS": str(int(invoke_timeout)),
        "AITOBUILD_DEVELOPER_REPOSITORY_SOURCES": json.dumps([source]),
        "AITOBUILD_GITHUB_ADAPTER": "gh_cli",
        "AITOBUILD_GITHUB_DEFAULT_REPOSITORY": repository,
        "AITOBUILD_GITHUB_ALLOWED_REPOS": repository,
        "AITOBUILD_WEB_SEARCH_ADAPTER": "mock",
        "AITOBUILD_SCHEDULER_ENABLED": "false",
        "AITOBUILD_MANAGED_WORKER_QUEUE_FOLLOW_UPS": "true",
    }


def record_early_failure(recorder: Recorder, error: BaseException) -> None:
    """A failure before the service starts still lands in report.json with an end time and the error."""
    recorder.report["result"] = "fail"
    recorder.report["failure"] = f"{type(error).__name__}: {error}"
    recorder.report["ended_at"] = now()
    sessions = recorder.report.get("sessions") or []
    if sessions and "ended_at" not in sessions[-1]:
        sessions[-1]["ended_at"] = recorder.report["ended_at"]
    try:
        recorder.save()
    except Exception as save_error:  # noqa: BLE001 - the caller re-raises the original failure, not this one
        error.add_note(
            f"Saving the early failure record also failed: {type(save_error).__name__}: {save_error}"
        )


def configure_trial_environment(
    *, automatic_stages: tuple[str, ...], values: dict[str, str]
) -> None:
    """Set the process environment for one trial; only allowlisted stages may become automatic."""
    refused = set(automatic_stages) - set(ALLOWED_AUTOMATIC_STAGES)
    if refused:
        raise SystemExit(
            f"Refusing automatic approval for {sorted(refused)}; only {ALLOWED_AUTOMATIC_STAGES} may be automatic"
        )
    for key in (
        "AITOBUILD_FOUNDRY_API_KEY",
        "AITOBUILD_CONFIG_FILE",
        *("AITOBUILD_APPROVAL_" + stage.upper() for stage in ALL_STAGES),
    ):
        os.environ.pop(key, None)
    os.environ.update(values)
    os.environ.update(
        {"AITOBUILD_APPROVAL_" + stage.upper(): "automatic" for stage in automatic_stages}
    )


def check_policy(policy: Any, automatic_stages: tuple[str, ...]) -> dict[str, Any]:
    """Fail closed unless exactly ``automatic_stages`` are automatic, merge is disabled and the rest are human."""
    modes = policy.model_dump(mode="json")
    expected = {
        stage: "automatic"
        if stage in automatic_stages
        else "disabled"
        if stage == "merge"
        else "human"
        for stage in ALL_STAGES
    }
    actual = {stage: modes.get(stage) for stage in ALL_STAGES}
    unknown = set(modes) - set(ALL_STAGES) - POLICY_LIMIT_KEYS
    if actual != expected or unknown or set(automatic_stages) - set(ALLOWED_AUTOMATIC_STAGES):
        raise SystemExit(f"Unexpected approval policy {modes}; expected {expected}")
    return {"modes": modes, "revision": policy.revision}
