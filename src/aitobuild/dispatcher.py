"""Dispatcher routes normalized internal events to role workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aitobuild.developer_isolation import build_developer_task_bundle
from aitobuild.developer_preview import DeveloperPreview, DeveloperPreviewRegistry
from aitobuild.escalation import EscalationCategory, EscalationRouter, EscalationSeverity
from aitobuild.events import EventOrigin, EventType, InternalEvent
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

        issue_number = _extract_issue_or_pr_number(body_dict)
        task_id = f"WEBHOOK-{event.envelope.correlation_id.upper()}"
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

        if self.require_developer_preview:
            preview = self.developer_preview_registry.create_or_get(
                dedupe_key=event.envelope.dedupe_key,
                bundle_payload=bundle.to_payload(),
                source_payload={
                    "github_event": github_event,
                    "action": action,
                    "body": body_dict,
                },
            )
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

        return DispatchResult(
            accepted=True,
            route="developer.async.webhook",
            metadata={
                "developer_task_bundle": bundle.to_payload(),
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
        payload: dict[str, Any] = {
            "github_event": github_event,
            "action": action,
            "body": body,
        }

        bundle = build_developer_task_bundle(
            task_id=f"PREVIEW-{dedupe_key.upper().replace(':', '-')}"[:80],
            objective=f"Preview developer task for webhook '{github_event}' action '{action}'",
            acceptance_criteria=(
                "Confirm bundle scope is sufficient and safe.",
                "Confirm acceptance criteria are implementation-ready.",
            ),
            constraints=(
                "Use isolation policy defaults unless explicit override is approved.",
            ),
            context_files=(
                "src/aitobuild/dispatcher.py",
                "src/aitobuild/developer_isolation.py",
                "tests/test_dispatcher.py",
            ),
        )

        return self.developer_preview_registry.create_or_get(
            dedupe_key=dedupe_key,
            bundle_payload=bundle.to_payload(),
            source_payload=payload,
        )

    def approve_developer_preview(self, preview_id: str) -> DeveloperPreview | None:
        return self.developer_preview_registry.approve(preview_id)

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
