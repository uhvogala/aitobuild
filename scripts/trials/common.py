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
from pathlib import Path
import subprocess
import time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

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


class Operator:
    def __init__(
        self,
        client,
        token: str,
        recorder: Recorder,
        poll: float,
        timeout: float,
        automatic_stages: tuple[str, ...] = (),
    ) -> None:
        self.automatic_stages = automatic_stages
        self.automatic = bool(automatic_stages)
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
        response = self.client.get(
            f"/internal/organization/tasks/{preview_id}", headers=self.headers
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"task status {preview_id} -> {response.status_code}: {response.text[:2000]}"
            )
        return response.json()

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


def collect_evidence(holder: dict[str, Any], report: dict[str, Any], policy: Any) -> dict[str, Any]:
    """Read saved preview/run state and classify every recorded decision as automatic or human."""
    service, previews = holder["service"], holder["previews"]
    modes = report["approval_policy"]["modes"]
    phases, problems = [], []
    for phase in report["phases"]:
        preview = previews.get(phase["preview_id"])
        run = service.status(phase["preview_id"])
        admission = (
            preview.approval_policy.model_dump(mode="json") if preview.approval_policy else None
        )
        decisions = [
            {
                "request_id": item.request_id,
                "kind": item.kind,
                "actor_id": item.actor_id,
                "policy": item.approval_policy.model_dump(mode="json")
                if item.approval_policy
                else None,
            }
            for item in (run.decisions if run else ())
        ]
        count = sum(
            1
            for item in decisions
            if item["policy"] and item["policy"]["source"] == "operator_policy"
        )
        phases.append(
            {
                "phase": phase["phase"],
                "preview_id": phase["preview_id"],
                "admission": admission,
                "run_policy_revision": run.approval_policy_revision if run else None,
                "automatic_decisions": count,
                "decisions": decisions,
            }
        )
        if modes.get("task_admission") == "automatic":
            if (
                not admission
                or admission["source"] != "operator_policy"
                or admission["policy_revision"] != policy.revision
            ):
                problems.append(
                    f"{phase['phase']}: admission not an automatic policy decision at the pinned revision"
                )
        for item in decisions:
            stage, source = (
                (item["policy"] or {}).get("stage"),
                (item["policy"] or {}).get("source"),
            )
            want = "operator_policy" if modes.get(stage) == "automatic" else "human"
            if (
                item["policy"] is None
                or source != want
                or item["policy"]["policy_revision"] != policy.revision
            ):
                problems.append(
                    f"{phase['phase']}: decision {item['request_id']} {stage} source={source}, expected {want}"
                )
        if count > policy.max_automatic_decisions:
            problems.append(
                f"{phase['phase']}: {count} automatic decisions exceed max {policy.max_automatic_decisions}"
            )
    return {
        "ok": not problems,
        "problems": problems,
        "max_automatic_decisions": policy.max_automatic_decisions,
        "policy_revision": policy.revision,
        "phases": phases,
    }


def positive(value: str) -> float:
    """argparse type for strictly positive, finite seconds."""
    number = float(value)
    if not number > 0 or number == float("inf"):
        raise ValueError("must be a positive number")
    return number


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
