"""Inactive consent evidence contracts; trusted adapters must supply verified inputs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
import json
from pathlib import Path
from typing import Annotated, Iterator, Literal, Self

from filelock import FileLock
from pydantic import AfterValidator, AwareDatetime, Field, StrictInt, StrictStr, model_validator

from aitobuild.approval_policy import ApprovalDecision, ApprovalPolicy, ApprovalStage
from aitobuild.durable_files import atomic_write_text, refuse_symlinks
from aitobuild.organization import DefinitionModel, Identifier
from aitobuild.publish_approvals import snapshot_digest


Digest = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
BaseRevision = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{40}$")]
NonBlank = Annotated[StrictStr, Field(min_length=1, max_length=512, pattern=r"\S")]
Host = Annotated[StrictStr, Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]+$")]


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


ConsentTime = Annotated[AwareDatetime, AfterValidator(_utc)]


class _Record(DefinitionModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1

    @property
    def digest(self) -> str:
        return snapshot_digest(self.model_dump(mode="json"))


class ConsentBundle(StrEnum):
    PLAN_PUBLISH = "plan_publish"
    PLAN_DELIVER = "plan_deliver"
    TASK_DELIVER = "task_deliver"
    AMEND_ITEMS = "amend_items"


class GitHubHuman(DefinitionModel):
    host: Host
    user_id: StrictInt = Field(gt=0)

    @property
    def actor_id(self) -> str:
        return f"github:{self.host}:{self.user_id}"


class ConsentLimits(DefinitionModel):
    max_tasks: StrictInt = Field(default=8, ge=1, le=128)
    max_effects: StrictInt = Field(default=32, ge=1, le=10000)
    max_model_calls: StrictInt = Field(default=32, ge=1, le=10000)
    max_changed_paths: StrictInt = Field(default=32, ge=0, le=10000)
    max_correction_rounds: StrictInt = Field(default=0, ge=0, le=32)
    max_seconds: StrictInt = Field(default=3600, ge=1, le=86400)


class ConsentProfile(_Record):
    profile_id: Identifier
    operator_id: NonBlank
    binding_revision: NonBlank
    organization_revision: Digest
    host: Host
    source_repository_id: StrictInt = Field(gt=0)
    repository_id: StrictInt = Field(gt=0)
    repository: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$")]
    approvers: Annotated[tuple[GitHubHuman, ...], Field(min_length=1)]
    bundles: Annotated[tuple[ConsentBundle, ...], Field(min_length=1)]
    stages: Annotated[tuple[ApprovalStage, ...], Field(min_length=1)]
    policy: ApprovalPolicy
    limits: ConsentLimits = ConsentLimits()

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if (len(set(self.bundles)) != len(self.bundles) or len(set(self.stages)) != len(self.stages) or
                len({actor.actor_id for actor in self.approvers}) != len(self.approvers)):
            raise ValueError("Consent profile bindings must be unique")
        if any(actor.host != self.host for actor in self.approvers):
            raise ValueError("Approvers must belong to the configured GitHub host")
        if ApprovalStage.MERGE in self.stages:
            raise ValueError("Consent cannot authorize merge")
        if ConsentBundle.AMEND_ITEMS in self.bundles:
            raise ValueError("Item amendments require a separate registered authority contract")
        if any(self.policy.mode(stage) == "disabled" for stage in self.stages):
            raise ValueError("Consent cannot override disabled approval stages")
        return self

    def check(self, presentation: ConsentPresentation) -> None:
        if presentation.profile_digest != self.digest or presentation.bundle not in self.bundles:
            raise PermissionError("Presentation differs from its bound consent profile")
        stages = set(presentation.stages)
        if not stages.issubset(self.stages):
            raise PermissionError("Presentation stages exceed the consent profile")
        planning = {ApprovalStage.TASK_ADMISSION, ApprovalStage.PM_ISSUE_CREATION, ApprovalStage.PM_DEPENDENCY_LINKS}
        delivery = {ApprovalStage.TASK_ADMISSION, ApprovalStage.DEVELOPER_TOOLS,
                    ApprovalStage.DRAFT_PUBLICATION, ApprovalStage.REVIEW_PUBLICATION}
        required = planning if presentation.bundle == ConsentBundle.PLAN_PUBLISH else delivery
        if presentation.bundle == ConsentBundle.PLAN_DELIVER:
            required = planning | delivery
        optional = {ApprovalStage.CORRECTION, ApprovalStage.MEETING_CONTINUATION}
        allowed = required if presentation.bundle == ConsentBundle.PLAN_PUBLISH else required | optional
        if not required.issubset(stages) or not stages.issubset(allowed):
            raise PermissionError("Presentation stages do not match its advertised bundle")
        if ApprovalStage.PM_ISSUE_CREATION in stages and self.policy.pm_issue_creation != "human":
            raise PermissionError("Advertised issue creation requires direct human provenance")
        for stage in stages - {ApprovalStage.TASK_ADMISSION, ApprovalStage.PM_ISSUE_CREATION}:
            if self.policy.mode(stage) != "automatic":
                raise PermissionError("Advertised derived stages require compatible automatic policy")
        if presentation.bundle != ConsentBundle.PLAN_PUBLISH and self.policy.task_admission != "automatic":
            raise PermissionError("Advertised delivery requires automatic descendant admission")
        for name, limit in presentation.limits.model_dump().items():
            if limit > getattr(self.limits, name):
                raise PermissionError("Presentation limits exceed the consent profile")


class ConsentItem(DefinitionModel):
    key: Identifier
    content_digest: Digest


class ConsentPresentation(_Record):
    presentation_id: Identifier
    profile_digest: Digest
    prepared_digest: Digest
    bundle: ConsentBundle
    base_revision: BaseRevision
    thread_id: NonBlank
    comment_id: NonBlank
    items: Annotated[tuple[ConsentItem, ...], Field(min_length=1)]
    stages: Annotated[tuple[ApprovalStage, ...], Field(min_length=1)]
    limits: ConsentLimits = ConsentLimits()
    created_at: ConsentTime
    expires_at: ConsentTime

    @model_validator(mode="after")
    def validate_presentation(self) -> Self:
        if len({item.key for item in self.items}) != len(self.items) or len(set(self.stages)) != len(self.stages):
            raise ValueError("Presented items and stages must be unique")
        if len(self.items) > self.limits.max_tasks:
            raise ValueError("Presented items exceed the task limit")
        if self.bundle == ConsentBundle.TASK_DELIVER and len(self.items) != 1:
            raise ValueError("Task delivery covers exactly one task")
        duration = (self.expires_at - self.created_at).total_seconds()
        if not 0 < duration <= self.limits.max_seconds:
            raise ValueError("Presentation expiry exceeds its original lifetime")
        if (ApprovalStage.CORRECTION in self.stages) != (self.limits.max_correction_rounds > 0):
            raise ValueError("Correction authority requires an explicit bounded round limit")
        return self


class ConsentSource(_Record):
    actor: GitHubHuman
    repository_id: StrictInt = Field(gt=0)
    thread_id: NonBlank
    comment_id: NonBlank
    reply_to_comment_id: NonBlank
    body: Annotated[StrictStr, Field(min_length=1, max_length=16000)]
    updated_at: ConsentTime

    @model_validator(mode="after")
    def validate_body(self) -> Self:
        if not self.body.strip() or len(self.body.encode()) > 16000:
            raise ValueError("Consent source must contain bounded nonblank UTF-8 text")
        return self


class HumanConsent(_Record):
    consent_id: Identifier
    presentation_id: Identifier
    presentation_digest: Digest
    profile_digest: Digest
    source: ConsentSource
    intent: Literal["approve"]
    supporting_start: StrictInt = Field(ge=0)
    supporting_end: StrictInt = Field(gt=0)
    interpretation_digest: Digest
    operator_id: NonBlank
    accepted_at: ConsentTime

    @model_validator(mode="after")
    def validate_support(self) -> Self:
        if not 0 <= self.supporting_start < self.supporting_end <= len(self.source.body):
            raise ValueError("Approval interpretation requires an exact source text span")
        if not self.source.body[self.supporting_start:self.supporting_end].strip():
            raise ValueError("Approval interpretation support cannot be blank")
        return self


class ConsentProjection(_Record):
    projection_id: Identifier
    consent_id: Identifier
    consent_digest: Digest
    binding_revision: NonBlank
    preview_id: NonBlank
    request_id: NonBlank
    input_digest: Digest
    operation_digest: Digest
    item_keys: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    decision: ApprovalDecision
    recorded_at: ConsentTime

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if len(set(self.item_keys)) != len(self.item_keys):
            raise ValueError("Projection item keys must be unique")
        if self.operation_digest != self.decision.operation_digest:
            raise ValueError("Projection must bind the exact approval operation")
        return self


class ConsentAdmissionEvidence(_Record):
    consent: HumanConsent
    projection: ConsentProjection

    @model_validator(mode="after")
    def validate_admission(self) -> Self:
        projection = self.projection
        decision = projection.decision
        if (projection.consent_id != self.consent.consent_id or
                projection.consent_digest != self.consent.digest or
                projection.recorded_at < self.consent.accepted_at):
            raise PermissionError("Admission evidence differs from its accepted human consent")
        if (decision.stage != ApprovalStage.TASK_ADMISSION or decision.source != "human" or
                decision.actor_id != self.consent.source.actor.actor_id):
            raise PermissionError("Consent admission requires the exact direct human decision")
        return self


class _Journal(_Record):
    profiles: tuple[ConsentProfile, ...] = ()
    presentations: tuple[ConsentPresentation, ...] = ()
    consents: tuple[HumanConsent, ...] = ()
    projections: tuple[ConsentProjection, ...] = ()

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        profiles = {profile.digest: profile for profile in self.profiles}
        indexes = (tuple(profile.digest for profile in self.profiles),
                   tuple(item.presentation_id for item in self.presentations),
                   tuple((profiles[item.profile_digest].host, profiles[item.profile_digest].source_repository_id,
                          item.thread_id, item.comment_id) for item in self.presentations
                         if item.profile_digest in profiles),
                   tuple(item.consent_id for item in self.consents),
                   tuple(item.projection_id for item in self.projections),
                   tuple(item.presentation_id for item in self.consents),
                   tuple((item.source.actor.host, item.source.repository_id, item.source.comment_id) for item in self.consents),
                   tuple((item.preview_id, item.request_id) for item in self.projections))
        if any(len(set(index)) != len(index) for index in indexes):
            raise ValueError("Consent history contains duplicate identities or consumed sources/requests")
        presentations = {item.presentation_id: item for item in self.presentations}
        consents = {item.consent_id: item for item in self.consents}
        for presentation in self.presentations:
            profile = profiles.get(presentation.profile_digest)
            if profile is None:
                raise ValueError("Presentation is missing its immutable profile")
            profile.check(presentation)
        for consent in self.consents:
            parent_presentation = presentations.get(consent.presentation_id)
            profile = profiles.get(consent.profile_digest)
            if parent_presentation is None or profile is None:
                raise ValueError("Consent is missing its presentation/profile")
            _check_consent(consent, parent_presentation, profile)
        for projection in self.projections:
            parent_consent = consents.get(projection.consent_id)
            if parent_consent is None:
                raise ValueError("Projection is missing its accepted human consent")
            _check_projection(projection, parent_consent, presentations[parent_consent.presentation_id],
                              profiles[parent_consent.profile_digest])
        for consent in self.consents:
            projections = tuple(item for item in self.projections if item.consent_id == consent.consent_id)
            presentation = presentations[consent.presentation_id]
            if len(projections) > presentation.limits.max_effects:
                raise ValueError("Consent projection reservations exceed the effect limit")
            for stage in (ApprovalStage.TASK_ADMISSION, ApprovalStage.PM_ISSUE_CREATION):
                if sum(item.decision.source == "human" and item.decision.stage == stage for item in projections) > 1:
                    raise ValueError("Consent cannot project duplicate direct human stage decisions")
        return self


def _check_consent(consent: HumanConsent, presentation: ConsentPresentation, profile: ConsentProfile) -> None:
    if (consent.presentation_digest != presentation.digest or consent.profile_digest != profile.digest or
            presentation.profile_digest != profile.digest or consent.operator_id != profile.operator_id):
        raise PermissionError("Consent differs from its exact presentation/profile/operator")
    source = consent.source
    if source.actor not in profile.approvers or source.actor.host != profile.host:
        raise PermissionError("Consent author is not an authorized host-qualified human")
    if (source.repository_id != profile.source_repository_id or source.thread_id != presentation.thread_id or
            source.reply_to_comment_id != presentation.comment_id or source.comment_id == presentation.comment_id):
        raise PermissionError("Consent source differs from the presented repository/thread/reply")
    if not presentation.created_at <= source.updated_at <= consent.accepted_at < presentation.expires_at:
        raise PermissionError("Consent source or acceptance is outside the original presentation lifetime")


def _check_projection(projection: ConsentProjection, consent: HumanConsent,
                      presentation: ConsentPresentation, profile: ConsentProfile) -> None:
    decision = projection.decision
    if projection.consent_digest != consent.digest or projection.binding_revision != profile.binding_revision:
        raise PermissionError("Projection differs from its human consent or binding revision")
    if not set(projection.item_keys).issubset(item.key for item in presentation.items):
        raise PermissionError("Projection includes items outside the consent")
    if decision.stage not in presentation.stages or decision.policy_revision != profile.policy.revision:
        raise PermissionError("Projection stage or policy differs from its consent")
    if decision.mode != profile.policy.mode(decision.stage):
        raise PermissionError("Projection cannot replace the bound approval mode")
    if not consent.accepted_at <= projection.recorded_at < presentation.expires_at:
        raise PermissionError("Projection is outside the original consent lifetime")
    if decision.source == "human":
        if (decision.stage not in {ApprovalStage.TASK_ADMISSION, ApprovalStage.PM_ISSUE_CREATION} or
                decision.actor_id != consent.source.actor.actor_id or
                set(projection.item_keys) != {item.key for item in presentation.items}):
            raise PermissionError("Direct human projection exceeds the advertised exact human actions")
    elif decision.actor_id != profile.operator_id:
        raise PermissionError("Derived policy projection must retain the trusted operator")
    if decision.stage == ApprovalStage.PM_ISSUE_CREATION and decision.source != "human":
        raise PermissionError("Issue creation must retain direct human consent provenance")
    if (decision.stage == ApprovalStage.TASK_ADMISSION and decision.source != "human" and
            presentation.bundle == ConsentBundle.PLAN_PUBLISH):
        raise PermissionError("Publication-only consent cannot authorize child delivery")


class FileConsentStore:
    """Store evidence only; no method authenticates GitHub actors or approves managed tasks."""

    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = path.absolute()
        self._check_paths()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.exists():
            raise FileNotFoundError("Initialized consent journal is missing")
        with self._lock():
            if not self.path.exists():
                if not create:
                    raise FileNotFoundError("Initialized consent journal is missing")
                self._save(_Journal())
            self._load()

    def _check_paths(self) -> None:
        for path in (self.path, Path(str(self.path) + ".lock")):
            if path.resolve() != path:
                raise ValueError("Consent storage cannot follow symlinks")
            refuse_symlinks(path)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._check_paths()
        with FileLock(str(self.path) + ".lock", timeout=10, mode=0o600):
            self._check_paths()
            yield

    def _load(self) -> _Journal:
        self._check_paths()
        wrapper = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(wrapper, dict) or set(wrapper) != {"digest", "record"}:
            raise ValueError("Invalid consent journal envelope")
        journal = _Journal.model_validate(wrapper["record"])
        if wrapper["digest"] != journal.digest:
            raise PermissionError("Consent journal integrity differs from its saved digest")
        return journal

    def _save(self, journal: _Journal) -> None:
        journal = _Journal.model_validate(journal.model_dump(mode="json"))
        self._check_paths()
        atomic_write_text(self.path, json.dumps({"digest": journal.digest, "record": journal.model_dump(mode="json")},
                                               sort_keys=True, separators=(",", ":"), allow_nan=False))

    def present(self, presentation: ConsentPresentation, *, profile: ConsentProfile) -> ConsentPresentation:
        profile = ConsentProfile.model_validate(profile.model_dump(mode="json"))
        presentation = ConsentPresentation.model_validate(presentation.model_dump(mode="json"))
        profile.check(presentation)
        with self._lock():
            journal = self._load()
            existing = next((item for item in journal.presentations if item.presentation_id == presentation.presentation_id), None)
            if existing is not None:
                if existing != presentation:
                    raise PermissionError("Consent presentations are immutable")
                return existing
            now = datetime.now(UTC)
            if not presentation.created_at <= now < presentation.expires_at:
                raise PermissionError("Cannot present future or expired consent")
            profiles = journal.profiles if profile in journal.profiles else journal.profiles + (profile,)
            self._save(journal.model_copy(update={"profiles": profiles, "presentations": journal.presentations + (presentation,)}))
            return presentation

    def accept(self, consent: HumanConsent, *, profile: ConsentProfile,
               current_source: ConsentSource | None) -> HumanConsent:
        consent = HumanConsent.model_validate(consent.model_dump(mode="json"))
        with self._lock():
            journal = self._load()
            presentation = next((item for item in journal.presentations if item.presentation_id == consent.presentation_id), None)
            if presentation is None:
                raise PermissionError("Consent presentation is missing")
            self._check_live(consent, presentation, profile, current_source)
            existing = next((item for item in journal.consents if item.consent_id == consent.consent_id), None)
            if existing is not None:
                if existing != consent:
                    raise PermissionError("Accepted human consent is immutable")
                return existing
            self._save(journal.model_copy(update={"consents": journal.consents + (consent,)}))
            return consent

    @staticmethod
    def _check_live(consent: HumanConsent, presentation: ConsentPresentation, profile: ConsentProfile,
                    current_source: ConsentSource | None) -> None:
        profile = ConsentProfile.model_validate(profile.model_dump(mode="json"))
        profile.check(presentation)
        _check_consent(consent, presentation, profile)
        now = datetime.now(UTC)
        if not consent.accepted_at <= now < presentation.expires_at:
            raise PermissionError("Human consent is not currently valid")
        if current_source is None or current_source != consent.source:
            raise PermissionError("Live consent source was edited, removed or changed")

    def record_projection(self, projection: ConsentProjection, *, profile: ConsentProfile,
                          current_source: ConsentSource | None) -> ConsentProjection:
        projection = ConsentProjection.model_validate(projection.model_dump(mode="json"))
        with self._lock():
            journal = self._load()
            consent = next((item for item in journal.consents if item.consent_id == projection.consent_id), None)
            if consent is None:
                raise PermissionError("Projection requires an accepted human consent")
            presentation = next(item for item in journal.presentations if item.presentation_id == consent.presentation_id)
            self._check_live(consent, presentation, profile, current_source)
            _check_projection(projection, consent, presentation, profile)
            if projection.recorded_at > datetime.now(UTC):
                raise PermissionError("Projection cannot record a future decision")
            existing = next((item for item in journal.projections if item.projection_id == projection.projection_id), None)
            if existing is not None:
                if existing != projection:
                    raise PermissionError("Consent projections are immutable")
                return existing
            self._save(journal.model_copy(update={"projections": journal.projections + (projection,)}))
            return projection

    def get_consent(self, consent_id: str) -> HumanConsent | None:
        with self._lock():
            return next((item for item in self._load().consents if item.consent_id == consent_id), None)

    def get_presentation(self, presentation_id: str) -> ConsentPresentation | None:
        with self._lock():
            return next((item for item in self._load().presentations if item.presentation_id == presentation_id), None)

    def get_projection(self, projection_id: str) -> ConsentProjection | None:
        with self._lock():
            return next((item for item in self._load().projections if item.projection_id == projection_id), None)

    def check_projection(self, projection_id: str, *, profile: ConsentProfile,
                         current_source: ConsentSource | None) -> ConsentProjection:
        with self._lock():
            journal = self._load()
            projection = next((item for item in journal.projections if item.projection_id == projection_id), None)
            if projection is None:
                raise PermissionError("Consent projection has not been durably recorded")
            consent = next(item for item in journal.consents if item.consent_id == projection.consent_id)
            presentation = next(item for item in journal.presentations if item.presentation_id == consent.presentation_id)
            self._check_live(consent, presentation, profile, current_source)
            return projection