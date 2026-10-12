"""Discussions consent adapter for exact task admission (#42); inactive until an operator wires it."""

from __future__ import annotations

from copy import deepcopy
from typing import Callable, Protocol

from aitobuild.approval_consent import (
    ConsentAdmissionEvidence, ConsentBundle, ConsentProfile, ConsentProjection, ConsentSource,
    FileConsentStore, HumanConsent,
)
from aitobuild.approval_policy import ApprovalStage
from aitobuild.developer_isolation import developer_task_bundle_from_payload
from aitobuild.developer_preview import (DeveloperPreview, DeveloperPreviewRegistry, check_consent_admission,
                                         preview_approval_operation)
from aitobuild.organization_service import Consent
from aitobuild.publish_approvals import snapshot_digest


class ConsentAdmitter(Protocol):
    @property
    def preview_registry(self) -> DeveloperPreviewRegistry: ...

    def admit_consent(self, preview: DeveloperPreview, evidence: Consent) -> DeveloperPreview: ...


class ConsentTaskAdmission:
    """Opt-in exact task admission; profile/source readers must be trusted operator adapters.

    Consent is evidence passed into the service's single admission route (PLAN 2.4); this adapter never approves
    a preview itself."""

    def __init__(self, *, service: ConsentAdmitter, store: FileConsentStore,
                 profile: ConsentProfile, read_profile: Callable[[], ConsentProfile],
                 read_source: Callable[[HumanConsent], ConsentSource | None]) -> None:
        self._service = service
        self._previews = service.preview_registry
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
        check_consent_admission(preview.preview_id, bundle, projection.decision, evidence)
        if preview.approved and preview.consent_approval != evidence:
            raise PermissionError("Existing task approval cannot acquire a different consent")
        self._store.record_projection(projection, profile=self._current_profile(),
                                      current_source=self._read_source(consent))
        saved = self._store.check_projection(projection.projection_id, profile=self._current_profile(),
                                             current_source=self._read_source(consent))
        if saved != projection:
            raise PermissionError("Task admission projection changed before approval")
        # The journal is evidence only: the live policy decision and the approval go through the one route.
        return self._service.admit_consent(preview, Consent(evidence=evidence, base_revision=presentation.base_revision))

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
        check_consent_admission(preview.preview_id, preview.bundle_payload, preview.approval_policy, evidence)
        if preview.consent_approval != evidence:
            raise PermissionError("Saved admission differs from its consent projection")
        return preview
