from __future__ import annotations

from conftest import approve_preview, consent_service
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from threading import Barrier, Event

import pytest

from aitobuild.approval_consent import (
    ConsentAdmissionEvidence, ConsentBundle, ConsentItem, ConsentLimits, ConsentPresentation, ConsentProfile,
    ConsentProjection, ConsentSource, FileConsentStore, GitHubHuman, HumanConsent,
)
from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage
from aitobuild.dispatcher import DispatcherAgent
from aitobuild.consent_admission import ConsentTaskAdmission
from aitobuild.developer_preview import DeveloperPreviewRegistry, preview_approval_operation
from aitobuild.publish_approvals import snapshot_digest


@pytest.fixture
def consent_fixture(tmp_path):
    now = datetime.now(UTC) - timedelta(seconds=1)
    actor = GitHubHuman(host="github.com", user_id=123)
    policy = ApprovalPolicy(pm_dependency_links="automatic")
    profile = ConsentProfile(
        profile_id="publication", operator_id="service-operator", binding_revision="consent-v1",
        organization_revision="a" * 64, host="github.com", source_repository_id=100,
        repository_id=101, repository="fixture/widgets", approvers=(actor,),
        bundles=(ConsentBundle.PLAN_PUBLISH,),
        stages=(ApprovalStage.TASK_ADMISSION, ApprovalStage.PM_ISSUE_CREATION, ApprovalStage.PM_DEPENDENCY_LINKS),
        policy=policy,
    )
    presentation = ConsentPresentation(
        presentation_id="plan-one", profile_digest=profile.digest, prepared_digest="b" * 64,
        bundle=ConsentBundle.PLAN_PUBLISH, base_revision="c" * 40, thread_id="discussion-1",
        comment_id="proposal-1", items=(ConsentItem(key="first", content_digest="d" * 64),),
        stages=profile.stages, created_at=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=30),
    )
    source = ConsentSource(actor=actor, repository_id=100, thread_id="discussion-1", comment_id="approval-1",
                           reply_to_comment_id="proposal-1", body="Approved, create these issues.", updated_at=now)
    consent = HumanConsent(consent_id="consent-one", presentation_id=presentation.presentation_id,
                           presentation_digest=presentation.digest, profile_digest=profile.digest,
                           source=source, intent="approve", supporting_start=0, supporting_end=8,
                           interpretation_digest="e" * 64, operator_id=profile.operator_id, accepted_at=now)
    store = FileConsentStore(tmp_path / "consents.json", create=True)
    store.present(presentation, profile=profile)
    return store, profile, presentation, consent


def projection_for(profile, consent, stage, *, projection_id="creation", request_id="request-1", source="human"):
    decision = profile.policy.resolve(stage, actor_id=consent.source.actor.actor_id if source == "human" else profile.operator_id,
                                      operation={"request": request_id}, source=source, automatic_supported=True)
    return ConsentProjection(
        projection_id=projection_id, consent_id=consent.consent_id, consent_digest=consent.digest,
        binding_revision=profile.binding_revision, preview_id="preview-1", request_id=request_id,
        input_digest="f" * 64, operation_digest=decision.operation_digest, item_keys=("first",),
        decision=decision, recorded_at=consent.accepted_at,
    )


def test_admission_evidence_preserves_exact_human_through_json(consent_fixture):
    _, profile, _, consent = consent_fixture
    projection = projection_for(profile, consent, ApprovalStage.TASK_ADMISSION)
    evidence = ConsentAdmissionEvidence(consent=consent, projection=projection)
    restored = ConsentAdmissionEvidence.model_validate_json(evidence.model_dump_json())
    assert restored == evidence
    assert restored.digest == evidence.digest
    assert restored.projection.decision.actor_id == "github:github.com:123"
    assert restored.consent.source == consent.source


@pytest.fixture
def preview_consent_fixture(consent_fixture, repository_issue_body, tmp_path):
    store, profile, presentation, consent = consent_fixture
    registry = DeveloperPreviewRegistry(tmp_path / "previews.json")
    preview = DispatcherAgent(developer_preview_registry=registry).create_developer_preview(
        dedupe_key="consent-task", github_event="issues", action="opened", body=repository_issue_body,
    )
    bundle = deepcopy(preview.bundle_payload)
    bundle["issue_context"]["base_revision"] = presentation.base_revision
    decision = profile.policy.resolve(ApprovalStage.TASK_ADMISSION, actor_id=consent.source.actor.actor_id,
                                      operation=preview_approval_operation(preview.preview_id, bundle), source="human")
    projection = projection_for(profile, consent, ApprovalStage.TASK_ADMISSION).model_copy(update={
        "preview_id": preview.preview_id, "input_digest": snapshot_digest(bundle),
        "operation_digest": decision.operation_digest, "decision": decision,
    })
    evidence = ConsentAdmissionEvidence(consent=consent, projection=projection)
    return store, profile, presentation, registry, preview, evidence


def test_preview_consent_evidence_survives_restart_and_dispatch(preview_consent_fixture):
    _, _, presentation, registry, preview, evidence = preview_consent_fixture
    approved = approve_preview(registry, preview.preview_id, base_revision=presentation.base_revision,
                                approval_policy=evidence.projection.decision, consent_approval=evidence)
    restarted = DeveloperPreviewRegistry(registry._path)
    assert restarted.get(preview.preview_id) == approved
    assert restarted.get(preview.preview_id).consent_approval == evidence
    dispatched = restarted.claim_dispatch(preview.preview_id)
    assert dispatched.consent_approval == evidence
    assert DeveloperPreviewRegistry(registry._path).get(preview.preview_id) == dispatched
    assert approve_preview(restarted, preview.preview_id) == dispatched


@pytest.mark.parametrize("case", ["preview", "bundle", "decision", "base"])
def test_preview_refuses_consent_for_another_exact_operation(preview_consent_fixture, case):
    _, _, presentation, registry, preview, evidence = preview_consent_fixture
    policy = evidence.projection.decision
    base = presentation.base_revision
    if case == "preview":
        evidence = evidence.model_copy(update={"projection": evidence.projection.model_copy(update={"preview_id": "other"})})
    elif case == "bundle":
        evidence = evidence.model_copy(update={"projection": evidence.projection.model_copy(update={"input_digest": "0" * 64})})
    elif case == "decision":
        policy = None
    else:
        base = "0" * 40
    before = registry._path.read_bytes()
    with pytest.raises(PermissionError, match="exact preview"):
        approve_preview(registry, preview.preview_id, base_revision=base, approval_policy=policy, consent_approval=evidence)
    assert registry._path.read_bytes() == before
    assert not registry.get(preview.preview_id).approved


def test_existing_human_approval_cannot_gain_new_consent_evidence(preview_consent_fixture):
    _, _, presentation, registry, preview, evidence = preview_consent_fixture
    approve_preview(registry, preview.preview_id, base_revision=presentation.base_revision, approval_policy=evidence.projection.decision)
    with pytest.raises(PermissionError, match="consent evidence is immutable"):
        approve_preview(registry, preview.preview_id, approval_policy=evidence.projection.decision, consent_approval=evidence)
    assert registry.get(preview.preview_id).consent_approval is None


@pytest.fixture
def task_admission_fixture(preview_consent_fixture):
    store, profile, original, registry, preview, evidence = preview_consent_fixture
    policy = ApprovalPolicy(task_admission="automatic", developer_tools="automatic",
                            draft_publication="automatic", review_publication="automatic")
    stages = (ApprovalStage.TASK_ADMISSION, ApprovalStage.DEVELOPER_TOOLS,
              ApprovalStage.DRAFT_PUBLICATION, ApprovalStage.REVIEW_PUBLICATION)
    profile = profile.model_copy(update={"profile_id": "task-delivery", "policy": policy,
                                         "stages": stages, "bundles": (ConsentBundle.TASK_DELIVER,)})
    item = ConsentItem(key="delivery", content_digest=evidence.projection.input_digest)
    presentation = original.model_copy(update={"presentation_id": "task-one", "comment_id": "task-proposal",
                                               "profile_digest": profile.digest, "bundle": ConsentBundle.TASK_DELIVER,
                                               "items": (item,), "stages": stages,
                                               "prepared_digest": evidence.projection.operation_digest})
    source = evidence.consent.source.model_copy(update={"reply_to_comment_id": presentation.comment_id,
                                                       "body": "Approved, deliver this exact task with review."})
    consent = evidence.consent.model_copy(update={"presentation_id": presentation.presentation_id,
                                                "presentation_digest": presentation.digest,
                                                "profile_digest": profile.digest, "source": source})
    decision = policy.resolve(ApprovalStage.TASK_ADMISSION, actor_id=source.actor.actor_id,
                              operation=preview_approval_operation(preview.preview_id, {
                                  **preview.bundle_payload,
                                  "issue_context": {**preview.bundle_payload["issue_context"],
                                                    "base_revision": presentation.base_revision},
                              }), source="human", automatic_supported=True)
    projection = evidence.projection.model_copy(update={"consent_digest": consent.digest,
                                                       "item_keys": (item.key,), "decision": decision})
    store.present(presentation, profile=profile)
    store.accept(consent, profile=profile, current_source=source)
    inputs = {"profile": profile, "source": source}
    bridge = ConsentTaskAdmission(service=consent_service(registry._path.parent / "svc", registry, profile.policy), store=store, profile=profile,
                                   read_profile=lambda: inputs["profile"], read_source=lambda _: inputs["source"])
    return store, registry, bridge, projection, inputs


def test_task_consent_admission_is_journaled_before_approval_and_recovers(task_admission_fixture, monkeypatch):
    store, registry, bridge, projection, _ = task_admission_fixture
    approve = registry._approve_admission

    def checked_approve(*args, **kwargs):
        assert store.get_projection(projection.projection_id) == projection
        return approve(*args, **kwargs)

    monkeypatch.setattr(registry, "_approve_admission", checked_approve)
    admitted = bridge.approve(projection)
    assert admitted.approved
    assert admitted.approval_policy.source == "human"
    assert admitted.approval_policy.actor_id == "github:github.com:123"
    assert admitted.consent_approval.projection == projection
    assert bridge.approve(projection) == admitted
    restarted = DeveloperPreviewRegistry(registry._path)
    assert restarted.get(projection.preview_id) == admitted
    assert bridge.reconcile(projection.projection_id) == admitted


@pytest.mark.parametrize("case", ["source", "deleted", "profile", "consent", "input", "keys"])
def test_task_consent_admission_refuses_changed_authority_before_approval(task_admission_fixture, case):
    store, registry, bridge, projection, inputs = task_admission_fixture
    if case == "source":
        inputs["source"] = inputs["source"].model_copy(update={"body": "Stop"})
    elif case == "deleted":
        inputs["source"] = None
    elif case == "profile":
        inputs["profile"] = inputs["profile"].model_copy(update={"binding_revision": "changed"})
    else:
        updates = {"consent": {"consent_id": "missing"}, "input": {"input_digest": "0" * 64}, "keys": {"item_keys": ("other",)}}
        projection = projection.model_copy(update=updates[case])
    before = registry._path.read_bytes()
    with pytest.raises(PermissionError):
        bridge.approve(projection)
    assert not registry.get(projection.preview_id).approved
    assert registry._path.read_bytes() == before
    assert store.get_projection(projection.projection_id) is None


def test_task_consent_reconciliation_does_not_create_missing_approval(task_admission_fixture):
    store, registry, bridge, projection, inputs = task_admission_fixture
    store.record_projection(projection, profile=inputs["profile"], current_source=inputs["source"])
    before = registry._path.read_bytes()
    assert bridge.reconcile(projection.projection_id) is None
    assert not registry.get(projection.preview_id).approved
    assert registry._path.read_bytes() == before


@pytest.mark.parametrize("case", ["source", "profile"])
def test_drift_after_projection_save_stops_task_approval(task_admission_fixture, monkeypatch, case):
    store, registry, bridge, projection, inputs = task_admission_fixture
    save = store._save

    def changed_after_save(journal):
        save(journal)
        if case == "source":
            inputs["source"] = None
        else:
            inputs["profile"] = inputs["profile"].model_copy(update={"binding_revision": "changed"})

    monkeypatch.setattr(store, "_save", changed_after_save)
    with pytest.raises(PermissionError):
        bridge.approve(projection)
    assert store.get_projection(projection.projection_id) == projection
    assert not registry.get(projection.preview_id).approved
    assert bridge.reconcile(projection.projection_id) is None


def test_lost_task_approval_response_recovers_metadata_without_live_authority(task_admission_fixture, monkeypatch):
    store, registry, bridge, projection, inputs = task_admission_fixture
    save = registry._save

    def saved_then_lost():
        save()
        raise OSError("Approval response lost")

    monkeypatch.setattr(registry, "_save", saved_then_lost)
    with pytest.raises(OSError, match="response lost"):
        bridge.approve(projection)
    restarted = DeveloperPreviewRegistry(registry._path)
    bound_profile = inputs["profile"]
    inputs["source"] = None
    inputs["profile"] = inputs["profile"].model_copy(update={"binding_revision": "changed"})
    bridge = ConsentTaskAdmission(service=consent_service(restarted._path.parent / "svc-restarted", restarted, bound_profile.policy), store=FileConsentStore(store.path),
                                   profile=bound_profile,
                                   read_profile=lambda: inputs["profile"], read_source=lambda _: inputs["source"])
    before = registry._path.read_bytes(), store.path.read_bytes()
    recovered = bridge.reconcile(projection.projection_id)
    assert recovered.approved
    assert recovered.dispatched_at is None
    assert recovered.consent_approval.projection == projection
    assert (registry._path.read_bytes(), store.path.read_bytes()) == before
    with pytest.raises(PermissionError):
        bridge.approve(projection)


@pytest.mark.parametrize("case", ["bundle", "policy", "actor"])
def test_corrupt_saved_preview_consent_refuses_on_restart(task_admission_fixture, case):
    _, registry, bridge, projection, _ = task_admission_fixture
    bridge.approve(projection)
    data = json.loads(registry._path.read_text())
    saved = data["previews"][0]
    if case == "bundle":
        saved["consent_approval"]["projection"]["input_digest"] = "0" * 64
    elif case == "policy":
        saved.pop("approval_policy")
    else:
        saved["consent_approval"]["consent"]["source"]["actor"]["user_id"] = 999
    registry._path.write_text(json.dumps(data))
    with pytest.raises(PermissionError):
        DeveloperPreviewRegistry(registry._path).get(projection.preview_id)


def test_missing_consent_journal_does_not_recreate_task_authority(task_admission_fixture):
    store, registry, bridge, projection, _ = task_admission_fixture
    store.path.unlink()
    with pytest.raises(FileNotFoundError):
        bridge.approve(projection)
    assert not store.path.exists()
    assert not registry.get(projection.preview_id).approved


@pytest.mark.parametrize("stage", ["task_admission", "developer_tools", "draft_publication", "review_publication"])
def test_task_consent_adapter_refuses_incompatible_stage_profiles(task_admission_fixture, stage):
    store, registry, _, _, inputs = task_admission_fixture
    profile = inputs["profile"]
    changed = profile.model_copy(update={"policy": profile.policy.model_copy(update={stage: "human"})})
    with pytest.raises(PermissionError, match="automatic delivery profile"):
        ConsentTaskAdmission(service=consent_service(registry._path.parent / "svc", registry, profile.policy), store=store, profile=changed,
                              read_profile=lambda: changed, read_source=lambda _: inputs["source"])


@pytest.mark.parametrize("case", ["prepared", "content", "repository", "base", "paths", "runtime", "publication_only"])
def test_valid_consent_records_cannot_authorize_a_different_task(task_admission_fixture, tmp_path, case):
    original_store, registry, _, projection, inputs = task_admission_fixture
    profile = inputs["profile"]
    consent = original_store.get_consent(projection.consent_id)
    presentation = original_store.get_presentation(consent.presentation_id)
    if case == "prepared":
        presentation = presentation.model_copy(update={"prepared_digest": "0" * 64})
    elif case == "content":
        presentation = presentation.model_copy(update={"items": (ConsentItem(key="delivery", content_digest="0" * 64),)})
    elif case == "repository":
        profile = profile.model_copy(update={"repository_id": 999})
    elif case == "base":
        presentation = presentation.model_copy(update={"base_revision": "0" * 40})
    elif case == "paths":
        presentation = presentation.model_copy(update={"limits": ConsentLimits(max_changed_paths=0)})
    elif case == "runtime":
        presentation = presentation.model_copy(update={"limits": ConsentLimits(max_seconds=60),
                                                       "expires_at": presentation.created_at + timedelta(seconds=60)})
    else:
        stages = (ApprovalStage.TASK_ADMISSION, ApprovalStage.PM_ISSUE_CREATION, ApprovalStage.PM_DEPENDENCY_LINKS)
        profile = profile.model_copy(update={"bundles": (ConsentBundle.TASK_DELIVER, ConsentBundle.PLAN_PUBLISH),
                                             "stages": profile.stages + stages[1:],
                                             "policy": profile.policy.model_copy(update={"pm_dependency_links": "automatic"})})
        presentation = presentation.model_copy(update={"bundle": ConsentBundle.PLAN_PUBLISH, "stages": stages})
    presentation = presentation.model_copy(update={"profile_digest": profile.digest})
    consent = consent.model_copy(update={"profile_digest": profile.digest, "presentation_digest": presentation.digest})
    projection = projection.model_copy(update={"consent_digest": consent.digest,
                                               "decision": projection.decision.model_copy(update={"policy_revision": profile.policy.revision})})
    store = FileConsentStore(tmp_path / "different-task-consent.json", create=True)
    store.present(presentation, profile=profile)
    store.accept(consent, profile=profile, current_source=consent.source)
    bridge = ConsentTaskAdmission(service=consent_service(registry._path.parent / "svc", registry, profile.policy), store=store, profile=profile,
                                   read_profile=lambda: profile, read_source=lambda _: consent.source)
    before = registry._path.read_bytes()
    with pytest.raises(PermissionError):
        bridge.approve(projection)
    assert registry._path.read_bytes() == before
    assert not registry.get(projection.preview_id).approved
    assert store.get_projection(projection.projection_id) is None


def test_task_consent_expiry_blocks_consumption_but_not_metadata_reconciliation(task_admission_fixture, monkeypatch):
    store, registry, bridge, projection, inputs = task_admission_fixture
    admitted = bridge.approve(projection)
    consent = store.get_consent(projection.consent_id)
    expiry = store.get_presentation(consent.presentation_id).expires_at

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expiry

    monkeypatch.setattr("aitobuild.approval_consent.datetime", ExpiredClock)
    before = registry._path.read_bytes(), store.path.read_bytes()
    with pytest.raises(PermissionError, match="currently valid"):
        bridge.approve(projection)
    assert bridge.reconcile(projection.projection_id) == admitted
    assert (registry._path.read_bytes(), store.path.read_bytes()) == before


def test_shared_task_consent_admission_consumes_one_projection(task_admission_fixture):
    """Owner B reads the preview, then pauses until owner A has approved it; B must get A's admission as a replay."""
    store, registry, _, projection, inputs = task_admission_fixture
    b_read, a_done = Event(), Event()

    def bridge_for(previews, name):
        return ConsentTaskAdmission(service=consent_service(registry._path.parent / f"svc-{name}", previews,
                                                            inputs["profile"].policy),
                                    store=FileConsentStore(store.path), profile=inputs["profile"],
                                    read_profile=lambda: inputs["profile"], read_source=lambda _: inputs["source"])

    b_previews = DeveloperPreviewRegistry(registry._path)
    original_get = b_previews.get

    def get_then_pause(preview_id):
        preview = original_get(preview_id)
        if not b_read.is_set():
            b_read.set()
            assert a_done.wait(timeout=10)
        return preview

    b_previews.get = get_then_pause
    owner_b = bridge_for(b_previews, "b")
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(owner_b.approve, projection)
        assert b_read.wait(timeout=10)
        try:
            first = bridge_for(DeveloperPreviewRegistry(registry._path), "a").approve(projection)
        finally:
            a_done.set()
        replay = pending.result(timeout=15)
    assert first.approved and replay == first
    assert len(json.loads(store.path.read_text())["record"]["projections"]) == 1
    assert registry.get(projection.preview_id) == first


@pytest.mark.parametrize("case", ["consent", "digest", "time", "stage", "actor", "policy_source"])
def test_admission_evidence_refuses_mismatched_human_projections(consent_fixture, case):
    _, profile, _, consent = consent_fixture
    projection = projection_for(profile, consent, ApprovalStage.TASK_ADMISSION)
    if case in {"consent", "digest", "time"}:
        updates = {"consent": {"consent_id": "other-consent"}, "digest": {"consent_digest": "0" * 64},
                   "time": {"recorded_at": consent.accepted_at - timedelta(seconds=1)}}
        projection = projection.model_copy(update=updates[case])
    else:
        updates = {"stage": {"stage": ApprovalStage.PM_ISSUE_CREATION},
                   "actor": {"actor_id": "service-operator"},
                   "policy_source": {"source": "operator_policy", "mode": "automatic"}}
        projection = projection.model_copy(update={"decision": projection.decision.model_copy(update=updates[case])})
    with pytest.raises(PermissionError):
        ConsentAdmissionEvidence.model_validate({"consent": consent.model_dump(), "projection": projection.model_dump()})


def test_one_human_consent_retains_human_creation_and_policy_link_evidence(consent_fixture):
    store, profile, presentation, consent = consent_fixture
    assert store.accept(consent, profile=profile, current_source=consent.source) == consent
    admission = projection_for(profile, consent, ApprovalStage.TASK_ADMISSION,
                               projection_id="admission", request_id="preview-approval")
    creation = projection_for(profile, consent, ApprovalStage.PM_ISSUE_CREATION)
    links = projection_for(profile, consent, ApprovalStage.PM_DEPENDENCY_LINKS,
                           projection_id="links", request_id="link-request", source="operator_policy")
    for projection in (admission, creation, links):
        assert store.record_projection(projection, profile=profile, current_source=consent.source) == projection
    restarted = FileConsentStore(store.path)
    assert restarted.get_consent(consent.consent_id) == consent
    assert restarted.get_projection("creation").decision.source == "human"
    assert restarted.get_projection("links").decision.source == "operator_policy"
    assert creation.decision.actor_id == "github:github.com:123"
    assert links.decision.actor_id == profile.operator_id
    assert restarted.accept(consent, profile=profile, current_source=consent.source) == consent
    assert restarted.record_projection(creation, profile=profile, current_source=consent.source) == creation
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert Path(str(store.path) + ".lock").stat().st_mode & 0o777 == 0o600
    assert presentation.bundle == ConsentBundle.PLAN_PUBLISH


@pytest.mark.parametrize("case", ["author", "host", "operator", "reply", "thread", "repository", "presentation",
                                  "future", "expired", "edited", "deleted", "binding", "rejected"])
def test_invalid_human_consent_refuses_without_saving(consent_fixture, case):
    store, profile, _, consent = consent_fixture
    live = consent.source
    if case in {"author", "host", "reply", "thread", "repository"}:
        updates = {"author": {"actor": GitHubHuman(host="github.com", user_id=999)},
                   "host": {"actor": GitHubHuman(host="other.com", user_id=123)},
                   "reply": {"reply_to_comment_id": "old-proposal"}, "thread": {"thread_id": "other-thread"},
                   "repository": {"repository_id": 999}}
        live = live.model_copy(update=updates[case])
        consent = consent.model_copy(update={"source": live})
    elif case == "operator":
        consent = consent.model_copy(update={"operator_id": "model-claimed-operator"})
    elif case == "presentation":
        consent = consent.model_copy(update={"presentation_digest": "0" * 64})
    elif case == "future":
        consent = consent.model_copy(update={"accepted_at": datetime.now(UTC) + timedelta(seconds=10)})
    elif case == "expired":
        consent = consent.model_copy(update={"accepted_at": datetime.now(UTC) + timedelta(days=1)})
    elif case == "edited":
        live = live.model_copy(update={"body": "Do not proceed"})
    elif case == "deleted":
        live = None
    elif case == "binding":
        profile = profile.model_copy(update={"binding_revision": "changed"})
    elif case == "rejected":
        consent = consent.model_copy(update={"intent": "reject"})
    before = store.path.read_bytes()
    with pytest.raises((ValueError, PermissionError)):
        store.accept(consent, profile=profile, current_source=live)
    assert store.path.read_bytes() == before
    assert store.get_consent(consent.consent_id) is None


def test_consumed_presentation_comment_and_request_cannot_be_reused(consent_fixture):
    store, profile, presentation, consent = consent_fixture
    store.accept(consent, profile=profile, current_source=consent.source)
    with pytest.raises(ValueError, match="duplicate"):
        store.accept(consent.model_copy(update={"consent_id": "another"}), profile=profile, current_source=consent.source)
    second = presentation.model_copy(update={"presentation_id": "second", "comment_id": "second-proposal"})
    store.present(second, profile=profile)
    reused = consent.model_copy(update={"consent_id": "second", "presentation_id": second.presentation_id,
                                       "presentation_digest": second.digest,
                                       "source": consent.source.model_copy(update={"reply_to_comment_id": second.comment_id})})
    with pytest.raises(ValueError, match="duplicate"):
        store.accept(reused, profile=profile, current_source=reused.source)
    projection = projection_for(profile, consent, ApprovalStage.PM_ISSUE_CREATION)
    store.record_projection(projection, profile=profile, current_source=consent.source)
    with pytest.raises(ValueError, match="duplicate"):
        store.record_projection(projection.model_copy(update={"projection_id": "duplicate"}),
                                profile=profile, current_source=consent.source)
    with pytest.raises(ValueError, match="duplicate direct human"):
        store.record_projection(projection.model_copy(update={"projection_id": "different", "request_id": "different"}),
                                profile=profile, current_source=consent.source)


@pytest.mark.parametrize("case", ["unlisted", "merge", "policy", "mode", "actor", "provenance", "binding", "digest", "scope", "future"])
def test_projection_cannot_expand_or_fabricate_authority(consent_fixture, case):
    store, profile, _, consent = consent_fixture
    store.accept(consent, profile=profile, current_source=consent.source)
    projection = projection_for(profile, consent, ApprovalStage.PM_ISSUE_CREATION)
    if case in {"unlisted", "merge", "policy", "mode", "actor", "provenance"}:
        updates = {"unlisted": {"stage": ApprovalStage.DRAFT_PUBLICATION}, "merge": {"stage": ApprovalStage.MERGE},
                   "policy": {"policy_revision": "0" * 64}, "mode": {"mode": "automatic"},
                   "actor": {"actor_id": "operator"}, "provenance": {"mode": "automatic", "source": "operator_policy"}}
        projection = projection.model_copy(update={"decision": projection.decision.model_copy(update=updates[case])})
    else:
        updates = {"binding": {"binding_revision": "changed"}, "digest": {"consent_digest": "0" * 64},
                   "scope": {"item_keys": ("other",)}, "future": {"recorded_at": datetime.now(UTC) + timedelta(seconds=10)}}
        projection = projection.model_copy(update=updates[case])
    before = store.path.read_bytes()
    with pytest.raises((ValueError, PermissionError)):
        store.record_projection(projection, profile=profile, current_source=consent.source)
    assert store.path.read_bytes() == before
    assert store.get_projection(projection.projection_id) is None


@pytest.mark.parametrize("case", ["delivery", "limits", "human_links", "automatic_creation", "disabled", "amendment", "merge"])
def test_profile_and_presentation_refuse_unsupported_actions(consent_fixture, case):
    store, profile, presentation, _ = consent_fixture
    if case == "delivery":
        presentation = presentation.model_copy(update={"bundle": ConsentBundle.PLAN_DELIVER})
    elif case == "limits":
        presentation = presentation.model_copy(update={"limits": ConsentLimits(max_effects=33)})
    elif case in {"human_links", "automatic_creation", "disabled"}:
        update = {"human_links": {"pm_dependency_links": "human"}, "automatic_creation": {"pm_issue_creation": "automatic"},
                  "disabled": {"pm_dependency_links": "disabled"}}
        profile = profile.model_copy(update={"policy": ApprovalPolicy.model_validate(update[case])})
        presentation = presentation.model_copy(update={"profile_digest": profile.digest})
    elif case == "amendment":
        profile = profile.model_copy(update={"bundles": (ConsentBundle.AMEND_ITEMS,)})
    else:
        profile = profile.model_copy(update={"stages": profile.stages + (ApprovalStage.MERGE,)})
    before = store.path.read_bytes()
    with pytest.raises((ValueError, PermissionError)):
        store.present(presentation, profile=profile)
    assert store.path.read_bytes() == before


def test_accepted_evidence_is_immutable_and_live_changes_block_projections(consent_fixture):
    store, profile, presentation, consent = consent_fixture
    with pytest.raises(PermissionError, match="immutable"):
        store.present(presentation.model_copy(update={"prepared_digest": "0" * 64}), profile=profile)
    store.accept(consent, profile=profile, current_source=consent.source)
    changed = consent.model_copy(update={"interpretation_digest": "0" * 64})
    with pytest.raises(PermissionError, match="immutable"):
        store.accept(changed, profile=profile, current_source=consent.source)
    projection = projection_for(profile, consent, ApprovalStage.PM_ISSUE_CREATION)
    with pytest.raises(PermissionError, match="Live consent"):
        store.record_projection(projection, profile=profile, current_source=None)
    assert store.get_consent(consent.consent_id) == consent
    assert store.get_projection(projection.projection_id) is None


def test_consent_store_refuses_missing_tampered_and_symlinked_state(consent_fixture, tmp_path):
    store, profile, _, consent = consent_fixture
    store.accept(consent, profile=profile, current_source=consent.source)
    wrapper = json.loads(store.path.read_text())
    wrapper["record"]["consents"][0]["interpretation_digest"] = "0" * 64
    store.path.write_text(json.dumps(wrapper))
    with pytest.raises(PermissionError, match="integrity"):
        FileConsentStore(store.path)
    store.path.unlink()
    with pytest.raises(FileNotFoundError):
        FileConsentStore(store.path)
    linked = tmp_path / "linked.json"
    linked.symlink_to(store.path)
    with pytest.raises(ValueError, match="symlink"):
        FileConsentStore(linked, create=True)
    real = tmp_path / "real"
    real.mkdir()
    parent = tmp_path / "parent"
    parent.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        FileConsentStore(parent / "state.json", create=True)


def test_strict_contracts_require_actual_ids_aware_times_and_support(consent_fixture):
    _, profile, presentation, consent = consent_fixture
    for update in ({"user_id": True}, {"user_id": "123"}, {"host": "https://github.com"}):
        with pytest.raises(ValueError):
            GitHubHuman.model_validate(profile.approvers[0].model_dump() | update)
    with pytest.raises(ValueError):
        ConsentPresentation.model_validate(presentation.model_dump() | {"created_at": datetime.now()})
    with pytest.raises(ValueError):
        HumanConsent.model_validate(consent.model_dump() | {"supporting_end": 10000})
    with pytest.raises(ValueError):
        ConsentSource.model_validate(consent.source.model_dump() | {"body": "\u00e9" * 9000})
    assert presentation.digest == ConsentPresentation.model_validate_json(presentation.model_dump_json()).digest
    for version in (True, "1", 2):
        with pytest.raises(ValueError):
            ConsentPresentation.model_validate(presentation.model_dump() | {"schema_version": version})


def test_projection_effect_limit_is_durable_and_idempotent(consent_fixture):
    store, profile, presentation, consent = consent_fixture
    limited = presentation.model_copy(update={"presentation_id": "limited", "comment_id": "limited-proposal",
                                               "limits": ConsentLimits(max_effects=1)})
    store.present(limited, profile=profile)
    source = consent.source.model_copy(update={"reply_to_comment_id": limited.comment_id})
    consent = consent.model_copy(update={"presentation_id": limited.presentation_id,
                                         "presentation_digest": limited.digest, "source": source})
    store.accept(consent, profile=profile, current_source=source)
    creation = projection_for(profile, consent, ApprovalStage.PM_ISSUE_CREATION)
    store.record_projection(creation, profile=profile, current_source=source)
    restarted = FileConsentStore(store.path)
    before = store.path.read_bytes()
    assert restarted.record_projection(creation, profile=profile, current_source=source) == creation
    links = projection_for(profile, consent, ApprovalStage.PM_DEPENDENCY_LINKS,
                           projection_id="links", request_id="links", source="operator_policy")
    with pytest.raises(ValueError, match="effect limit"):
        restarted.record_projection(links, profile=profile, current_source=source)
    assert store.path.read_bytes() == before
    assert restarted.get_projection("links") is None


def test_concurrent_consumers_accept_the_comment_only_once(consent_fixture):
    store, profile, _, consent = consent_fixture
    barrier = Barrier(2)

    def accept(identity):
        local = FileConsentStore(store.path)
        candidate = consent.model_copy(update={"consent_id": identity})
        barrier.wait(timeout=10)
        try:
            return local.accept(candidate, profile=profile, current_source=candidate.source).consent_id
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(accept, ("first-consumer", "second-consumer")))
    accepted = tuple(identity for identity in results if identity is not None)
    assert len(accepted) == 1
    restarted = FileConsentStore(store.path)
    assert restarted.get_consent(accepted[0]).source == consent.source


def test_landed_save_recovers_evidence_without_consuming_again(consent_fixture, monkeypatch):
    import aitobuild.approval_consent as consent_module

    store, profile, _, consent = consent_fixture
    original_write = consent_module.atomic_write_text

    def lost_response(path, content):
        original_write(path, content)
        raise OSError("lost local save response")

    monkeypatch.setattr(consent_module, "atomic_write_text", lost_response)
    with pytest.raises(OSError, match="lost local save"):
        store.accept(consent, profile=profile, current_source=consent.source)
    restarted = FileConsentStore(store.path)
    assert restarted.get_consent(consent.consent_id) == consent
    assert restarted.accept(consent, profile=profile, current_source=consent.source) == consent


def test_expiry_blocks_new_consumption_but_keeps_read_only_evidence(consent_fixture, monkeypatch):
    import aitobuild.approval_consent as consent_module

    store, profile, presentation, consent = consent_fixture
    store.accept(consent, profile=profile, current_source=consent.source)

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return presentation.expires_at + timedelta(seconds=1)

    monkeypatch.setattr(consent_module, "datetime", ExpiredClock)
    restarted = FileConsentStore(store.path)
    assert restarted.get_consent(consent.consent_id) == consent
    projection = projection_for(profile, consent, ApprovalStage.PM_ISSUE_CREATION)
    with pytest.raises(PermissionError, match="not currently valid"):
        restarted.record_projection(projection, profile=profile, current_source=consent.source)
    assert restarted.get_projection(projection.projection_id) is None


def test_lock_symlink_and_reused_presentation_comment_are_refused(consent_fixture, tmp_path):
    store, profile, presentation, _ = consent_fixture
    with pytest.raises(ValueError, match="duplicate"):
        store.present(presentation.model_copy(update={"presentation_id": "another"}), profile=profile)
    lock = Path(str(store.path) + ".lock")
    lock.unlink()
    target = tmp_path / "other-lock"
    target.touch()
    lock.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        FileConsentStore(store.path)
    missing = tmp_path / "missing" / "journal.json"
    with pytest.raises(FileNotFoundError):
        FileConsentStore(missing)
    assert not missing.parent.exists()

def _sibling(registry, preview, *, key, task_suffix, issue_id=None):
    """Another stored preview for the same (or another) issue; same bundle shape as the consented task."""
    bundle = deepcopy(preview.bundle_payload)
    bundle["task_id"] = str(bundle["task_id"]) + task_suffix
    if issue_id is not None:
        bundle["issue_context"]["issue_id"] = issue_id
    return registry.create_or_get(dedupe_key=key, bundle_payload=bundle, source_payload=preview.source_payload)


def _consent_for(projection, consent, preview, base, policy, projection_id):
    bundle = deepcopy(preview.bundle_payload)
    bundle["issue_context"]["base_revision"] = base
    decision = policy.resolve(ApprovalStage.TASK_ADMISSION, actor_id=consent.source.actor.actor_id,
                              operation=preview_approval_operation(preview.preview_id, bundle), source="human",
                              automatic_supported=True)
    other = projection.model_copy(update={"projection_id": projection_id, "preview_id": preview.preview_id,
                                          "input_digest": snapshot_digest(bundle),
                                          "operation_digest": decision.operation_digest, "decision": decision})
    return ConsentAdmissionEvidence(consent=consent, projection=other)


def test_consent_goes_through_the_service_route(task_admission_fixture, monkeypatch):
    from aitobuild.organization_service import ManagedOrganizationService

    _, _, bridge, projection, _ = task_admission_fixture
    seen = []
    original = ManagedOrganizationService._admit
    monkeypatch.setattr(ManagedOrganizationService, "_admit",
                        lambda self, preview, evidence: seen.append(evidence.kind) or original(self, preview, evidence))
    assert bridge.approve(projection).approved
    assert seen == ["consent"]


@pytest.mark.parametrize("prefix", ["published-review-", "published-correction-"])
def test_consent_refuses_review_and_correction_tasks(task_admission_fixture, prefix):
    from aitobuild.organization_service import Consent

    store, registry, bridge, projection, inputs = task_admission_fixture
    preview = registry.get(projection.preview_id)
    bundle = deepcopy(preview.bundle_payload) | {"task_id": prefix + "0" * 64}
    staged = registry.create_or_get(dedupe_key="staged-" + prefix, bundle_payload=bundle,
                                    source_payload={"published_review": {"preview_id": "dp-x", "head_sha": "a" * 40}})
    evidence = ConsentAdmissionEvidence(consent=store.get_consent(projection.consent_id), projection=projection)
    with pytest.raises(PermissionError, match="review or correction"):
        bridge._service.admit_consent(staged, Consent(evidence=evidence, base_revision="c" * 40))
    assert not registry.get(staged.preview_id).approved


def test_consent_decision_must_match_the_live_policy(task_admission_fixture):
    store, registry, _, projection, inputs = task_admission_fixture
    live = inputs["profile"].policy.model_copy(update={"max_automatic_admissions": 7})  # new policy revision
    bridge = ConsentTaskAdmission(service=consent_service(registry._path.parent / "svc-live", registry, live), store=store,
                                  profile=inputs["profile"], read_profile=lambda: inputs["profile"],
                                  read_source=lambda _: inputs["source"])
    with pytest.raises(PermissionError, match="live approval policy"):
        bridge.approve(projection)
    assert not registry.get(projection.preview_id).approved


def _automatic(registry, policy, preview):
    bundle = deepcopy(preview.bundle_payload)
    bundle["issue_context"]["base_revision"] = "c" * 40
    return policy.resolve(ApprovalStage.TASK_ADMISSION, actor_id="configured-operator",
                          operation=preview_approval_operation(preview.preview_id, bundle), automatic_supported=True)


def test_consent_does_not_count_toward_the_automatic_limit(task_admission_fixture):
    store, registry, bridge, projection, inputs = task_admission_fixture
    policy = inputs["profile"].policy
    preview = registry.get(projection.preview_id)
    limit = 2
    first = _sibling(registry, preview, key="first-issue", task_suffix="-first", issue_id=901)
    assert approve_preview(registry, first.preview_id, base_revision="c" * 40, approval_policy=_automatic(registry, policy, first),
                           max_automatic_admissions=limit).approved  # limit - 1 automatic admissions used
    assert bridge.approve(projection).approved  # human consent in between
    last = _sibling(registry, preview, key="last-issue", task_suffix="-last", issue_id=902)
    # Still room for exactly one more automatic admission: the consent did not take a slot.
    assert approve_preview(registry, last.preview_id, base_revision="c" * 40, approval_policy=_automatic(registry, policy, last),
                           max_automatic_admissions=limit).approved
    extra = _sibling(registry, preview, key="extra-issue", task_suffix="-extra", issue_id=903)
    with pytest.raises(PermissionError, match="limit reached"):
        approve_preview(registry, extra.preview_id, base_revision="c" * 40, approval_policy=_automatic(registry, policy, extra),
                        max_automatic_admissions=limit)


def test_automatic_admission_defers_after_a_consent_admission_of_the_same_issue(task_admission_fixture):
    from aitobuild.developer_preview import AutomaticAdmissionDeferred

    store, registry, bridge, projection, inputs = task_admission_fixture
    preview = registry.get(projection.preview_id)
    assert bridge.approve(projection).approved
    twin = _sibling(registry, preview, key="same-issue-later", task_suffix="-later")  # same (repository_id, issue_id)
    with pytest.raises(AutomaticAdmissionDeferred, match="automatically or by consent"):
        approve_preview(registry, twin.preview_id, base_revision="c" * 40,
                        approval_policy=_automatic(registry, inputs["profile"].policy, twin), max_automatic_admissions=32)
    assert not registry.get(twin.preview_id).approved


@pytest.mark.parametrize("earlier", ["automatic", "consent"])
def test_consent_keeps_the_per_issue_dedupe(task_admission_fixture, earlier):
    store, registry, bridge, projection, inputs = task_admission_fixture
    policy = inputs["profile"].policy
    consent = store.get_consent(projection.consent_id)
    preview = registry.get(projection.preview_id)
    twin = _sibling(registry, preview, key="same-issue", task_suffix="-twin")  # same (repository_id, issue_id)
    if earlier == "automatic":
        bundle = deepcopy(twin.bundle_payload)
        bundle["issue_context"]["base_revision"] = "c" * 40
        decision = policy.resolve(ApprovalStage.TASK_ADMISSION, actor_id="configured-operator",
                                  operation=preview_approval_operation(twin.preview_id, bundle), automatic_supported=True)
        approve_preview(registry, twin.preview_id, base_revision="c" * 40, approval_policy=decision)
    else:
        evidence = _consent_for(projection, consent, twin, "c" * 40, policy, "twin-projection")
        approve_preview(registry, twin.preview_id, base_revision="c" * 40, approval_policy=evidence.projection.decision,
                        consent_approval=evidence)
    with pytest.raises(PermissionError, match="already admitted by consent or automatically"):
        bridge.approve(projection)
    assert not registry.get(projection.preview_id).approved


def _consent_args(store, projection):
    from aitobuild.organization_service import Consent

    evidence = ConsentAdmissionEvidence(consent=store.get_consent(projection.consent_id), projection=projection)
    return Consent(evidence=evidence, base_revision="c" * 40)


def test_replay_still_compares_the_expected_preview(task_admission_fixture):
    from dataclasses import replace

    from aitobuild.organization_service import admit_task

    store, registry, bridge, projection, _ = task_admission_fixture
    before = registry.get(projection.preview_id)  # unapproved read, as a concurrent owner would hold it
    admitted = bridge.approve(projection)
    consent = _consent_args(store, projection)
    stripped = replace(admitted, approved=False, approved_at=None, approval_policy=None, consent_approval=None)
    for expected in (before, stripped):  # differ only in the fields approval writes
        assert admit_task(registry, projection.preview_id, consent, base_revision="c" * 40,
                          decision=projection.decision, expected=expected) == admitted
    forged = replace(before, source_payload=before.source_payload | {"forged": True})
    with pytest.raises(PermissionError, match="differs from its stored record"):
        admit_task(registry, projection.preview_id, consent, base_revision="c" * 40, decision=projection.decision,
                   expected=forged)


def test_replay_refuses_a_different_decision(task_admission_fixture):
    store, registry, bridge, projection, inputs = task_admission_fixture
    before = registry.get(projection.preview_id)
    bridge.approve(projection)
    automatic = _automatic(registry, inputs["profile"].policy, before)
    with pytest.raises(PermissionError):
        approve_preview(registry, before.preview_id, base_revision="c" * 40, approval_policy=automatic, expected=before)


@pytest.mark.parametrize("case", ["planning", "non_repo", "edited", "human_mode", "pinned_base", "decision_source"])
def test_service_refuses_consent_for(task_admission_fixture, case):
    from aitobuild.approval_policy import ApprovalPolicy
    from aitobuild.organization_service import admit_task

    store, registry, bridge, projection, inputs = task_admission_fixture
    preview = registry.get(projection.preview_id)
    service = bridge._service
    consent = _consent_args(store, projection)
    bundle = deepcopy(preview.bundle_payload)
    source = preview.source_payload
    expected = {"planning": "Planning and non-repository", "non_repo": "Planning and non-repository",
                "edited": "edited task", "human_mode": "automatic task-admission policy",
                "pinned_base": "already pinned task base", "decision_source": "live human decision"}[case]
    if case == "planning":
        bundle["task_id"] = "pm-planning-" + "0" * 64
    elif case == "non_repo":
        bundle.pop("issue_context")
    elif case == "edited":
        source = source | {"action": "edited"}
    elif case == "pinned_base":
        bundle["issue_context"]["base_revision"] = "d" * 40
    elif case == "human_mode":
        service = consent_service(registry._path.parent / "svc-human", registry, ApprovalPolicy())
    if case == "decision_source":
        decision = _automatic(registry, inputs["profile"].policy, preview)  # operator_policy, not human
        with pytest.raises(PermissionError, match=expected):
            admit_task(registry, preview.preview_id, consent, base_revision="c" * 40, decision=decision, expected=preview)
        target = preview
    else:
        target = registry.create_or_get(dedupe_key="refuse-" + case, bundle_payload=bundle, source_payload=source)
        with pytest.raises(PermissionError, match=expected):
            service.admit_consent(target, consent)
    assert not registry.get(target.preview_id).approved
