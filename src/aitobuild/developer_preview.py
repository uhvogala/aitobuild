"""Developer bundle preview registry and approval lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from threading import RLock
from typing import Any, Callable, Iterator
from uuid import uuid4

from filelock import FileLock

from aitobuild.approval_consent import (
    ConsentAdmissionEvidence, ConsentBundle, ConsentProfile, ConsentProjection, ConsentSource,
    FileConsentStore, HumanConsent,
)
from aitobuild.durable_files import atomic_write_text
from aitobuild.approval_policy import ApprovalDecision, ApprovalStage
from aitobuild.publish_approvals import snapshot_digest
from aitobuild.developer_isolation import developer_task_bundle_from_payload


@dataclass(slots=True, frozen=True)
class DeveloperPreview:
    preview_id: str
    dedupe_key: str
    bundle_payload: dict[str, object]
    source_payload: dict[str, object]
    approved: bool
    created_at: datetime
    approved_at: datetime | None = None
    dispatched_at: datetime | None = None
    approval_policy: ApprovalDecision | None = None
    consent_approval: ConsentAdmissionEvidence | None = None

    @property
    def state(self) -> str:
        if self.dispatched_at:
            return "dispatched"
        return "approved" if self.approved else "awaiting_approval"



PER_ISSUE_TASK_PREFIXES = ("ISSUE-", "planned-issue-", "dependent-issue-")
PLANNED_CHILD_TASK_PREFIXES = ("planned-issue-", "dependent-issue-")


def is_planned_child(preview: Any) -> bool:
    """The one rule for a PM-planned child (or its dependent handoff): it names a planning assignment or carries a
    planned/dependent task ID. Automatic admission sends such tasks through the plan check instead of the
    issue allowlist, so both the service and the app's base binding must use this same rule."""
    task_id = str(preview.bundle_payload.get("task_id", ""))
    return "planning_assignment" in preview.source_payload or task_id.startswith(PLANNED_CHILD_TASK_PREFIXES)


def _webhook_issue_identity(bundle: dict[str, Any]) -> tuple[object, object] | None:
    """Repository/issue IDs of a webhook or PM-planned issue task (staged review/correction tasks are not counted here).

    Planned children and their dependent handoffs share the key, so a handoff on a new base after a failed delivery
    never auto-admits the same issue again."""
    context = bundle.get("issue_context")
    if not str(bundle.get("task_id", "")).startswith(PER_ISSUE_TASK_PREFIXES) or not isinstance(context, dict):
        return None
    return context.get("repository_id"), context.get("issue_id")


REVIEW_TASK_PREFIX = "published-review-"


def published_review_target(preview: Any) -> tuple[str, str] | None:
    """(reviewed delivery preview id, exact published head) of a staged Architect review task, else None.

    Corrections also carry a published_review target but are not reviews; they never qualify here."""
    if not str(preview.bundle_payload.get("task_id", "")).startswith(REVIEW_TASK_PREFIX):
        return None
    if "correction_from_review" in preview.source_payload:
        raise PermissionError("A review task cannot carry a correction source")
    target = preview.source_payload.get("published_review")
    if (not isinstance(target, dict) or not isinstance(target.get("preview_id"), str) or not target["preview_id"]
            or not isinstance(target.get("head_sha"), str) or not target["head_sha"]):
        raise PermissionError("Review admission requires its exact published review target")
    return target["preview_id"], target["head_sha"]


def _review_identity(bundle: dict[str, Any], source: dict[str, Any]) -> tuple[object, object] | None:
    if not str(bundle.get("task_id", "")).startswith(REVIEW_TASK_PREFIX):
        return None
    target = source.get("published_review")
    return (target.get("preview_id"), target.get("head_sha")) if isinstance(target, dict) else ("", "")


class AutomaticAdmissionDeferred(PermissionError):
    """Automatic admission does not apply; the preview stays unapproved and waits for a human approval."""

class DeveloperPreviewRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = RLock()
        self._file_lock = FileLock(str(path) + ".lock", timeout=10) if path else None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._by_id: dict[str, DeveloperPreview] = {}
        self._by_dedupe: dict[str, str] = {}
        self._by_task: dict[str, str] = {}

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[None]:
        with self._lock:
            if self._file_lock:
                self._file_lock.acquire()
            try:
                self._load()
                yield
                if write:
                    self._save()
            finally:
                if self._file_lock:
                    self._file_lock.release()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Unsupported developer preview state version")
        if not isinstance(data.get("previews"), list) or not isinstance(data.get("deliveries"), dict) or not isinstance(data.get("tasks"), dict):
            raise ValueError("Invalid developer preview state structure")
        for item in data["previews"]:
            if not isinstance(item, dict) or not isinstance(item.get("approved"), bool):
                raise ValueError("Invalid persisted preview approval")
            if not isinstance(item.get("bundle_payload"), dict) or not isinstance(item.get("source_payload"), dict):
                raise ValueError("Invalid persisted preview scope")
            bundle = developer_task_bundle_from_payload(item["bundle_payload"])
            if item["approved"] != bool(item.get("approved_at")) or (item.get("dispatched_at") and not item["approved"]):
                raise ValueError("Invalid persisted preview lifecycle")
            if item["approved"] and bundle.issue_context is not None and bundle.issue_context.base_revision is None:
                raise ValueError("Approved issue task is missing its pinned base revision")
            if item.get("approval_policy") is not None:
                decision = ApprovalDecision.model_validate(item["approval_policy"])
                if (not item["approved"] or decision.stage != ApprovalStage.TASK_ADMISSION
                        or decision.operation_digest != snapshot_digest(preview_approval_operation(item["preview_id"], item["bundle_payload"]))):
                    raise PermissionError("Saved task policy differs from its exact approved bundle")
            if item.get("consent_approval") is not None:
                evidence = ConsentAdmissionEvidence.model_validate(item["consent_approval"])
                _check_consent_admission(item["preview_id"], item["bundle_payload"],
                                         ApprovalDecision.model_validate(item["approval_policy"])
                                         if item.get("approval_policy") is not None else None, evidence)
        self._by_id = {
            item["preview_id"]: DeveloperPreview(
                preview_id=item["preview_id"], dedupe_key=item["dedupe_key"],
                bundle_payload=item["bundle_payload"], source_payload=item["source_payload"],
                approved=item["approved"], created_at=datetime.fromisoformat(item["created_at"]),
                approved_at=datetime.fromisoformat(item["approved_at"]) if item["approved_at"] else None,
                dispatched_at=datetime.fromisoformat(item["dispatched_at"]) if item.get("dispatched_at") else None,
                approval_policy=ApprovalDecision.model_validate(item["approval_policy"]) if item.get("approval_policy") is not None else None,
                consent_approval=ConsentAdmissionEvidence.model_validate(item["consent_approval"])
                if item.get("consent_approval") is not None else None,
            )
            for item in data["previews"]
        }
        self._by_dedupe = data["deliveries"]
        self._by_task = data["tasks"]
        if any(not isinstance(key, str) or not isinstance(value, str) or value not in self._by_id
               for index in (self._by_dedupe, self._by_task) for key, value in index.items()):
            raise ValueError("Invalid persisted task/delivery identity index")

    def _save(self) -> None:
        if self._path is None:
            return
        data = {
            "version": 1, "deliveries": self._by_dedupe, "tasks": self._by_task,
            "previews": [
                {
                    "preview_id": preview.preview_id, "dedupe_key": preview.dedupe_key,
                    "bundle_payload": preview.bundle_payload, "source_payload": preview.source_payload,
                    "approved": preview.approved, "created_at": preview.created_at.isoformat(),
                    "approved_at": preview.approved_at.isoformat() if preview.approved_at else None,
                    "dispatched_at": preview.dispatched_at.isoformat() if preview.dispatched_at else None,
                    **({"approval_policy": preview.approval_policy.model_dump(mode="json")} if preview.approval_policy is not None else {}),
                          **({"consent_approval": preview.consent_approval.model_dump(mode="json")}
                              if preview.consent_approval is not None else {}),
                }
                for preview in self._by_id.values()
            ],
        }
        atomic_write_text(self._path, json.dumps(data))

    def create_or_get(
        self,
        *,
        dedupe_key: str,
        bundle_payload: dict[str, object],
        source_payload: dict[str, object],
        task_key: str | None = None,
    ) -> DeveloperPreview:
        bundle = json.loads(json.dumps(bundle_payload))
        source = json.loads(json.dumps(source_payload))
        with self._transaction(write=True):
            existing_id = self._by_dedupe.get(dedupe_key)
            if existing_id is None and task_key:
                existing_id = self._by_task.get(task_key)
            if existing_id is not None:
                existing = self._by_id[existing_id]
                if _unresolved_scope(existing.bundle_payload) != _unresolved_scope(bundle):
                    raise ValueError("Task scope changed; create a new preview with renewed approval")
                incoming_context = bundle.get("issue_context")
                if isinstance(incoming_context, dict) and incoming_context.get("base_revision"):
                    if existing.bundle_payload.get("issue_context") != incoming_context:
                        raise ValueError("Task base revision changed; renewed approval is required")
                self._by_dedupe[dedupe_key] = existing_id
                return deepcopy(existing)

            preview = DeveloperPreview(
                preview_id=f"dp-{uuid4()}", dedupe_key=dedupe_key,
                bundle_payload=bundle, source_payload=source, approved=False,
                created_at=datetime.now(tz=UTC),
            )
            self._by_id[preview.preview_id] = preview
            self._by_dedupe[dedupe_key] = preview.preview_id
            if task_key:
                self._by_task[task_key] = preview.preview_id
            return deepcopy(preview)

    def _approve_admission(self, preview_id: str, *, base_revision: str | None = None,
                approval_policy: ApprovalDecision | None = None,
                max_automatic_admissions: int | None = None,
                expected: DeveloperPreview | None = None,
                consent_approval: ConsentAdmissionEvidence | None = None) -> DeveloperPreview | None:
        """Private: call only through organization_service.admit_task (PLAN 2.4; enforced by tests)."""
        with self._transaction(write=True):
            preview = self._by_id.get(preview_id)
            if preview is None:
                return None
            if expected is not None and preview != expected:
                raise PermissionError("Admitted preview differs from its stored record")
            bundle = deepcopy(preview.bundle_payload)
            context = bundle.get("issue_context")
            if isinstance(context, dict):
                revision = base_revision if base_revision is not None else context.get("base_revision")
                if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
                    raise ValueError("Issue approval requires a resolved 40-character base commit SHA")
                revision = revision.lower()
                if preview.approved and context.get("base_revision") != revision:
                    raise ValueError("Approved base revision is immutable; renewed approval is required")
                context["base_revision"] = revision
            elif base_revision is not None:
                raise ValueError("base_revision requires a repository issue task")
            if consent_approval is not None:
                consent_approval = ConsentAdmissionEvidence.model_validate(consent_approval.model_dump(mode="json"))
                _check_consent_admission(preview_id, bundle, approval_policy, consent_approval)
            if preview.approved:
                if approval_policy is not None and preview.approval_policy != approval_policy:
                    raise PermissionError("Saved task approval policy is immutable")
                if consent_approval is not None and preview.consent_approval != consent_approval:
                    raise PermissionError("Saved task consent evidence is immutable")
                return deepcopy(preview)
            if approval_policy is not None:
                if (approval_policy.stage != ApprovalStage.TASK_ADMISSION
                        or approval_policy.operation_digest != snapshot_digest(preview_approval_operation(preview_id, bundle))):
                    raise PermissionError("Task approval policy must bind the exact pinned bundle")
                if approval_policy.source == "operator_policy" and max_automatic_admissions is not None:
                    # Counted inside the same write transaction, from saved approvals only: replays of an
                    # already-approved preview returned above and never count twice.
                    admitted = sum(1 for saved in self._by_id.values() if saved.approved and saved.approval_policy is not None
                                   and saved.approval_policy.stage == ApprovalStage.TASK_ADMISSION
                                   and saved.approval_policy.source == "operator_policy")
                    issue = _webhook_issue_identity(bundle)
                    if issue is not None and any(
                            saved.approved and saved.approval_policy is not None and saved.approval_policy.source == "operator_policy"
                            and _webhook_issue_identity(saved.bundle_payload) == issue for saved in self._by_id.values()):
                        raise AutomaticAdmissionDeferred("This issue was already admitted automatically; "
                                                         "a later task for it needs a human approval")
                    review = _review_identity(bundle, preview.source_payload)
                    if review is not None and any(
                            saved.approved and saved.approval_policy is not None and saved.approval_policy.source == "operator_policy"
                            and _review_identity(saved.bundle_payload, saved.source_payload) == review for saved in self._by_id.values()):
                        raise AutomaticAdmissionDeferred("This published head already had an automatic review; "
                                                         "another review of it needs a human approval")
                    if admitted >= max_automatic_admissions:
                        raise PermissionError("Automatic task admission limit reached; a human approval is required")
            approved_preview = replace(
                preview, bundle_payload=bundle, approved=True, approved_at=datetime.now(tz=UTC),
                approval_policy=approval_policy, consent_approval=consent_approval,
            )
            self._by_id[preview_id] = approved_preview
            return deepcopy(approved_preview)

    def claim_dispatch(self, preview_id: str) -> DeveloperPreview | None:
        with self._transaction(write=True):
            preview = self._by_id[preview_id]
            if not preview.approved:
                raise ValueError("Task dispatch requires approval")
            if preview.dispatched_at:
                return None
            dispatched = replace(preview, dispatched_at=datetime.now(tz=UTC))
            self._by_id[preview_id] = dispatched
            return deepcopy(dispatched)

    def get(self, preview_id: str) -> DeveloperPreview | None:
        with self._transaction():
            return deepcopy(self._by_id.get(preview_id))

    def get_by_dedupe(self, dedupe_key: str) -> DeveloperPreview | None:
        with self._transaction():
            preview_id = self._by_dedupe.get(dedupe_key)
            return deepcopy(self._by_id.get(preview_id)) if preview_id else None

    def is_approved_for_dedupe(self, dedupe_key: str) -> bool:
        preview = self.get_by_dedupe(dedupe_key)
        return bool(preview and preview.approved)

    def list_previews(self, *, pending_only: bool, limit: int) -> tuple[DeveloperPreview, ...]:
        with self._transaction():
            previews = tuple(self._by_id.values())
            if pending_only:
                previews = tuple(preview for preview in previews if not preview.approved)
            if limit < len(previews):
                previews = previews[-limit:]
            return deepcopy(previews)


def _unresolved_scope(bundle: dict[str, Any]) -> dict[str, Any]:
    scope = deepcopy(bundle)
    context = scope.get("issue_context")
    if isinstance(context, dict):
        context["base_revision"] = None
    return scope


def preview_approval_operation(preview_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
    return {"preview_id": preview_id, "bundle": bundle}


def _check_consent_admission(preview_id: str, bundle: dict[str, Any],
                             decision: ApprovalDecision | None, evidence: ConsentAdmissionEvidence) -> None:
    projection = evidence.projection
    if (projection.preview_id != preview_id or projection.decision != decision or
            projection.input_digest != snapshot_digest(bundle) or
            projection.operation_digest != snapshot_digest(preview_approval_operation(preview_id, bundle))):
        raise PermissionError("Consent admission must bind the exact preview, bundle and decision")


class ConsentTaskAdmission:
    """Opt-in exact task admission; profile/source readers must be trusted operator adapters."""

    def __init__(self, *, previews: DeveloperPreviewRegistry, store: FileConsentStore,
                 profile: ConsentProfile, read_profile: Callable[[], ConsentProfile],
                 read_source: Callable[[HumanConsent], ConsentSource | None]) -> None:
        self._previews = previews
        self._store = store
        self._profile = ConsentProfile.model_validate(profile.model_dump(mode="json"))
        required = {ApprovalStage.TASK_ADMISSION, ApprovalStage.DEVELOPER_TOOLS,
                    ApprovalStage.DRAFT_PUBLICATION, ApprovalStage.REVIEW_PUBLICATION}
        if (ConsentBundle.TASK_DELIVER not in self._profile.bundles or
                not required.issubset(self._profile.stages) or
                any(self._profile.policy.mode(stage) != "automatic" for stage in required)):
            raise PermissionError("Task consent admission requires its configured automatic delivery profile")
        self._read_profile = read_profile
        self._read_source = read_source

    def _current_profile(self) -> ConsentProfile:
        profile = ConsentProfile.model_validate(self._read_profile().model_dump(mode="json"))
        if profile != self._profile:
            raise PermissionError("Task consent profile changed during admission")
        return profile

    def approve(self, projection: ConsentProjection) -> DeveloperPreview:
        projection = ConsentProjection.model_validate(projection.model_dump(mode="json"))
        consent = self._store.get_consent(projection.consent_id)
        if consent is None:
            raise PermissionError("Task admission requires saved accepted human consent")
        presentation = self._store.get_presentation(consent.presentation_id)
        if presentation is None or presentation.bundle != ConsentBundle.TASK_DELIVER:
            raise PermissionError("This adapter supports only exact task-delivery consent")
        profile = self._current_profile()
        profile.check(presentation)
        evidence = ConsentAdmissionEvidence(consent=consent, projection=projection)
        preview = self._previews.get(projection.preview_id)
        if preview is None:
            raise PermissionError("Consented preview is missing")
        bundle = deepcopy(preview.bundle_payload)
        context = bundle.get("issue_context")
        if not isinstance(context, dict):
            raise PermissionError("Task consent requires an exact repository issue")
        if context.get("base_revision") not in {None, presentation.base_revision}:
            raise PermissionError("Task consent base differs from the frozen presentation")
        context["base_revision"] = presentation.base_revision
        task = developer_task_bundle_from_payload(bundle)
        if (context.get("repository"), context.get("repository_id")) != (profile.repository, profile.repository_id):
            raise PermissionError("Task consent repository differs from the configured target")
        if (task.policy.max_file_changes > presentation.limits.max_changed_paths or
                task.policy.max_runtime_minutes * 60 > presentation.limits.max_seconds):
            raise PermissionError("Task budget exceeds its advertised consent limits")
        item = presentation.items[0]
        if (item.content_digest != snapshot_digest(bundle) or
                projection.item_keys != (item.key,) or
                presentation.prepared_digest != snapshot_digest(preview_approval_operation(preview.preview_id, bundle))):
            raise PermissionError("Task consent differs from its frozen preview scope")
        _check_consent_admission(preview.preview_id, bundle, projection.decision, evidence)
        if preview.approved and preview.consent_approval != evidence:
            raise PermissionError("Existing task approval cannot acquire a different consent")
        self._store.record_projection(projection, profile=self._current_profile(),
                                      current_source=self._read_source(consent))
        saved = self._store.check_projection(projection.projection_id, profile=self._current_profile(),
                                             current_source=self._read_source(consent))
        if saved != projection:
            raise PermissionError("Task admission projection changed before approval")
        approved = self._previews._approve_admission(preview.preview_id, base_revision=presentation.base_revision,
                                          approval_policy=projection.decision, consent_approval=evidence)
        if approved is None:
            raise PermissionError("Consented preview disappeared before admission")
        return approved

    def reconcile(self, projection_id: str) -> DeveloperPreview | None:
        """Inspect landed approval metadata without revalidating or consuming live authority."""
        projection = self._store.get_projection(projection_id)
        if projection is None:
            return None
        consent = self._store.get_consent(projection.consent_id)
        if consent is None:
            raise PermissionError("Admission projection lost its accepted human consent")
        evidence = ConsentAdmissionEvidence(consent=consent, projection=projection)
        preview = self._previews.get(projection.preview_id)
        if preview is None or not preview.approved:
            return None
        _check_consent_admission(preview.preview_id, preview.bundle_payload, preview.approval_policy, evidence)
        if preview.consent_approval != evidence:
            raise PermissionError("Saved admission differs from its consent projection")
        return preview
