"""Dispatcher routes normalized internal events to role workflows."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import re
from typing import Any

from aitobuild.developer_isolation import DeveloperIssueContext, build_developer_task_bundle
from aitobuild.developer_preview import DeveloperPreview, DeveloperPreviewRegistry
from aitobuild.escalation import EscalationCategory, EscalationRouter, EscalationSeverity
from aitobuild.events import EventOrigin, EventType, InternalEvent, make_internal_event
from aitobuild.meetings import (
    MeetingDeadlineExceededError,
    MeetingRegistry,
    MeetingValidationError,
)
from aitobuild.triggers import DispatchResult, Dispatcher


@dataclass(slots=True)
class DispatcherAgent(Dispatcher):
    """Foundation router for webhook and internal trigger events."""

    meeting_registry: MeetingRegistry = field(default_factory=MeetingRegistry)
    escalation_router: EscalationRouter = field(default_factory=EscalationRouter)
    developer_preview_registry: DeveloperPreviewRegistry = field(default_factory=DeveloperPreviewRegistry)
    require_developer_preview: bool = False

    def route(self, event: InternalEvent) -> DispatchResult:
        event_type = event.envelope.event_type
        origin = event.envelope.origin

        if event_type is EventType.WEBHOOK_EVENT_RECEIVED and origin is EventOrigin.GITHUB_WEBHOOK:
            return self._route_developer_webhook(event)

        if event_type is EventType.ARCHITECT_SCAN_REQUESTED:
            return DispatchResult(accepted=True, route="architect.proactive.scan")

        if event_type is EventType.MEETING_REQUESTED:
            return self._route_meeting_requested(event.payload)

        if event_type is EventType.MEETING_DUE:
            return self._route_meeting_due(event.payload)

        if event_type is EventType.APPROVAL_REQUIRED:
            return DispatchResult(accepted=True, route="approval.queue")

        return DispatchResult(
            accepted=False,
            route="unsupported",
            reason=f"Unsupported event route for origin={origin} event_type={event_type}",
        )

    def _route_developer_webhook(self, event: InternalEvent) -> DispatchResult:
        payload = event.payload
        github_event = str(payload.get("github_event", "unknown"))
        action = str(payload.get("action", "unknown"))
        body = payload.get("body")
        body_dict: dict[str, Any] = body if isinstance(body, dict) else {}

        if "repository" in body_dict:
            return self._route_repository_issue(event, github_event, action, body_dict)

        issue_number = _extract_issue_or_pr_number(body_dict)
        task_id = f"WEBHOOK-{sha256(event.envelope.dedupe_key.encode()).hexdigest()}"
        objective = (
            f"Handle GitHub webhook '{github_event}' action '{action}'"
            if issue_number is None
            else f"Handle GitHub webhook '{github_event}' action '{action}' for item #{issue_number}"
        )

        try:
            bundle = build_developer_task_bundle(
                task_id=task_id,
                objective=objective,
                acceptance_criteria=(
                    "Implement only the requested behavior implied by the webhook context.",
                    "Preserve test, lint, and typecheck pass status.",
                    "Emit status updates through approved automation pathways.",
                ),
                constraints=(
                    "Follow developer isolation policy allowlists.",
                    "Do not modify blocked paths or unscoped files.",
                ),
                context_files=(
                    "src/aitobuild/dispatcher.py",
                    "src/aitobuild/developer_isolation.py",
                    "tests/test_dispatcher.py",
                ),
            )
        except ValueError as exc:
            return DispatchResult(
                accepted=False,
                route="developer.invalid",
                reason=str(exc),
            )

        bundle_payload = bundle.to_payload()
        if self.require_developer_preview:
            try:
                preview = self.developer_preview_registry.create_or_get(
                    dedupe_key=event.envelope.dedupe_key,
                    bundle_payload=bundle_payload,
                    source_payload={
                        "github_event": github_event,
                        "action": action,
                        "body": body_dict,
                    },
                )
            except ValueError as exc:
                return DispatchResult(accepted=False, route="developer.invalid", reason=str(exc))
            if not preview.approved:
                return DispatchResult(
                    accepted=False,
                    route="developer.preview_required",
                    reason="Developer preview approval required before dispatch",
                    metadata={
                        "preview_id": preview.preview_id,
                        "developer_task_bundle": preview.bundle_payload,
                        "github_event": github_event,
                        "action": action,
                    },
                )
            bundle_payload = preview.bundle_payload

        return DispatchResult(
            accepted=True,
            route="developer.async.webhook",
            metadata={
                "developer_task_bundle": bundle_payload,
                "github_event": github_event,
                "action": action,
            },
        )

    def create_developer_preview(
        self,
        *,
        dedupe_key: str,
        github_event: str,
        action: str,
        body: dict[str, Any],
    ) -> DeveloperPreview:
        if "repository" in body:
            return self._create_repository_issue_preview(dedupe_key, github_event, action, body)
        payload: dict[str, Any] = {
            "github_event": github_event,
            "action": action,
            "body": body,
        }

        event = make_internal_event(
            origin=EventOrigin.GITHUB_WEBHOOK, event_type=EventType.WEBHOOK_EVENT_RECEIVED,
            payload=payload, dedupe_key=dedupe_key,
        )
        result = self._route_developer_webhook(event)
        if result.metadata is None:
            raise ValueError(result.reason or "Unable to create developer preview")
        return self.developer_preview_registry.create_or_get(
            dedupe_key=dedupe_key,
            bundle_payload=result.metadata["developer_task_bundle"],
            source_payload=payload,
        )

    def approve_developer_preview(
        self, preview_id: str, *, base_revision: str | None = None,
    ) -> DeveloperPreview | None:
        return self.developer_preview_registry.approve(preview_id, base_revision=base_revision)

    def uses_durable_dedupe(self, event: InternalEvent) -> bool:
        body = event.payload.get("body")
        return (
            event.envelope.origin is EventOrigin.GITHUB_WEBHOOK
            and event.envelope.event_type is EventType.WEBHOOK_EVENT_RECEIVED
            and isinstance(body, dict) and "repository" in body
        )

    def _route_repository_issue(
        self, event: InternalEvent, github_event: str, action: str, body: dict[str, Any],
    ) -> DispatchResult:
        if github_event != "issues" or action not in {"opened", "assigned", "edited"}:
            return DispatchResult(accepted=False, route="developer.unsupported", reason="Only issues opened/assigned/edited are supported for repository tasks")
        try:
            preview = self._create_repository_issue_preview(
                event.envelope.dedupe_key, github_event, action, body,
            )
        except ValueError as exc:
            return DispatchResult(accepted=False, route="developer.invalid", reason=str(exc))
        metadata = {
            "preview_id": preview.preview_id, "task_id": preview.bundle_payload["task_id"],
            "task_state": preview.state, "developer_task_bundle": preview.bundle_payload,
            "github_event": github_event, "action": action,
        }
        if not preview.approved:
            return DispatchResult(accepted=False, route="developer.preview_required", reason="Repository issue tasks require approval and a resolved base commit", metadata=metadata)
        dispatched = self.developer_preview_registry.claim_dispatch(preview.preview_id)
        metadata["task_state"] = "dispatched"
        if dispatched is None:
            return DispatchResult(accepted=False, route="dedupe", reason="Approved issue task already dispatched", metadata=metadata)
        return DispatchResult(accepted=True, route="developer.async.webhook", metadata=metadata)

    def _create_repository_issue_preview(
        self, dedupe_key: str, github_event: str, action: str, body: dict[str, Any],
    ) -> DeveloperPreview:
        if github_event != "issues" or action not in {"opened", "assigned", "edited"}:
            raise ValueError("Only issues opened/assigned/edited are supported for repository tasks")
        context, criteria = _extract_repository_issue(body, action=action)
        bundle = build_developer_task_bundle(
            task_id="pending", objective=context.title, acceptance_criteria=criteria,
            constraints=(
                "Execute only in a disposable checkout of the approved target repository and base commit.",
                "Require successful verification before publication; preserve failure artifacts.",
                "Publish draft PRs only; never merge the task PR.",
            ),
            context_files=(), issue_context=context,
        )
        task_key = "ISSUE-" + sha256(json.dumps(bundle.to_payload(), sort_keys=True).encode()).hexdigest()
        bundle = replace(bundle, task_id=task_key)
        return self.developer_preview_registry.create_or_get(
            dedupe_key=dedupe_key, task_key=task_key, bundle_payload=bundle.to_payload(),
            source_payload={"github_event": github_event, "action": action, "body": body},
        )

    def list_developer_previews(self, *, pending_only: bool, limit: int) -> tuple[DeveloperPreview, ...]:
        return self.developer_preview_registry.list_previews(pending_only=pending_only, limit=limit)

    def _route_meeting_requested(self, payload: dict[str, object]) -> DispatchResult:
        try:
            record = self.meeting_registry.request_meeting(payload)
        except MeetingValidationError as exc:
            return DispatchResult(accepted=False, route="meeting.invalid", reason=str(exc))

        return DispatchResult(
            accepted=True,
            route="meeting.requested",
            metadata={"meeting_id": record.meeting_id},
        )

    def _route_meeting_due(self, payload: dict[str, object]) -> DispatchResult:
        meeting_id_raw = payload.get("meeting_id")
        if not isinstance(meeting_id_raw, str) or not meeting_id_raw.strip():
            return DispatchResult(
                accepted=False,
                route="meeting.pending",
                reason="meeting_due requires a non-empty meeting_id",
            )
        meeting_id = meeting_id_raw.strip()

        try:
            self.meeting_registry.mark_due(meeting_id)
            self.meeting_registry.mark_bootstrapped(meeting_id)
        except MeetingDeadlineExceededError as exc:
            escalation = self.escalation_router.escalate(
                source="meeting.lifecycle",
                category=EscalationCategory.DEADLINE_EXCEEDED,
                severity=EscalationSeverity.HIGH,
                summary="Meeting deadline exceeded before bootstrap",
                details={"meeting_id": meeting_id, "reason": str(exc)},
            )
            return DispatchResult(
                accepted=True,
                route="escalation.route",
                reason=str(exc),
                metadata={
                    "meeting_id": meeting_id,
                    "escalation_reason": "deadline_exceeded",
                    "escalation_id": escalation.escalation_id,
                },
            )
        except MeetingValidationError as exc:
            return DispatchResult(accepted=False, route="meeting.pending", reason=str(exc))

        return DispatchResult(
            accepted=True,
            route="meeting.bootstrap",
            metadata={"meeting_id": meeting_id},
        )


def _extract_issue_or_pr_number(body: dict[str, Any]) -> int | None:
    issue = body.get("issue")
    if isinstance(issue, dict) and isinstance(issue.get("number"), int):
        return issue["number"]

    pull_request = body.get("pull_request")
    if isinstance(pull_request, dict) and isinstance(pull_request.get("number"), int):
        return pull_request["number"]

    if isinstance(body.get("number"), int):
        return body["number"]

    return None


def _extract_repository_issue(
    body: dict[str, Any], *, action: str,
) -> tuple[DeveloperIssueContext, tuple[str, ...]]:
    repository, issue = body.get("repository"), body.get("issue")
    if not isinstance(repository, dict) or not isinstance(issue, dict) or "pull_request" in issue:
        raise ValueError("Repository task requires a GitHub issue, not a pull request")
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", full_name):
        raise ValueError("repository.full_name must be owner/repository")
    for data, key in ((repository, "id"), (issue, "id"), (issue, "number")):
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"Repository and issue {key} must be positive integers")
    branch, title, description = repository.get("default_branch"), issue.get("title"), issue.get("body")
    if not isinstance(branch, str) or not branch.strip() or not isinstance(title, str) or not title.strip():
        raise ValueError("Repository default branch and issue title are required")
    if issue.get("state") != "open" or not isinstance(description, str):
        raise ValueError("Repository task requires an open issue with a body")
    if action == "assigned":
        assignee = body.get("assignee")
        if not isinstance(assignee, dict) or not isinstance(assignee.get("login"), str) or not assignee["login"].strip():
            raise ValueError("Assigned issue event requires an assignee login")
    criteria: list[str] = []
    in_criteria = False
    for line in description.splitlines():
        heading = re.fullmatch(r"#{1,6}\s+(.+?)\s*#*", line.strip())
        if heading:
            in_criteria = heading[1].strip().rstrip(":").casefold() == "acceptance criteria"
        elif in_criteria:
            item = re.fullmatch(r"\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)(\S.*)", line)
            if item:
                criteria.append(item[1].strip())
    if not criteria:
        raise ValueError("Issue body requires an Acceptance Criteria heading with list items")
    return DeveloperIssueContext(
        repository=full_name.lower(), repository_id=repository["id"], issue_number=issue["number"],
        issue_id=issue["id"], title=title.strip(), body=description, base_branch=branch.strip(),
    ), tuple(criteria)
