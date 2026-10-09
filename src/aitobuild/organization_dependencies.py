"""Opt-in, read-only prerequisite reconciliation and unapproved dependent handoff."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from itertools import islice
import json
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock
from pydantic import Field, StrictStr

from aitobuild.developer_delivery import DeliveryPreparation, DeveloperDeliveryWorker
from aitobuild.developer_isolation import developer_task_bundle_from_payload
from aitobuild.developer_preview import DeveloperPreview
from aitobuild.durable_files import atomic_write_text
from aitobuild.organization import DefinitionModel
from aitobuild.organization_planning import ManagedPlanning, PlanRevision, PlanningRoute, PlanningScope, canonical
from aitobuild.policy import AgentRole


class DependencyHandoff(DefinitionModel):
    schema_version: Literal[1] = 1
    task_id: StrictStr
    planning_assignment_id: StrictStr
    plan_digest: StrictStr
    issue_key: StrictStr
    source_preview_id: StrictStr
    operator_id: StrictStr
    route: PlanningRoute
    base_revision: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    bundle_content: StrictStr
    prerequisites: tuple[dict[str, Any], ...] = Field(min_length=1)
    preview_id: StrictStr | None = None

    @property
    def identity(self) -> str:
        payload = json.loads(self.bundle_content)
        payload.pop("task_id")
        document = self.model_dump(mode="json", exclude={"task_id", "bundle_content", "preview_id"})
        document["bundle"] = payload
        return "dependent-issue-" + sha256(canonical(document).encode()).hexdigest()


class ManagedDependencies:
    def __init__(self, *, planning: ManagedPlanning, worker: DeveloperDeliveryWorker, state_dir: Path,
                 routes: tuple[PlanningRoute, ...], lookup_max_receipts: int = 100,
                 prerequisite_workers: tuple[DeveloperDeliveryWorker, ...] = ()) -> None:
        self.planning, self.worker, self.routes = planning, worker, routes
        if len(prerequisite_workers) > 8 or len({id(owner) for owner in (worker, *prerequisite_workers)}) != 1 + len(prerequisite_workers):
            raise ValueError("Prerequisite receipt owners must be explicit, unique and bounded")
        self.prerequisite_workers = prerequisite_workers
        if type(lookup_max_receipts) is not int or not 1 <= lookup_max_receipts <= 1000:
            raise ValueError("Dependency receipt lookup must have an explicit bounded capacity")
        self.lookup_max_receipts = lookup_max_receipts
        self.directory = state_dir.absolute()
        if self.directory.resolve() != self.directory or len({(route.repository, route.repository_id) for route in routes}) != len(routes):
            raise ValueError("Dependency handoff requires unambiguous routes and nonsymlinked storage")
        self.directory.mkdir(parents=True, exist_ok=True)
        for route in routes:
            snapshot = planning.admission.definitions.get(route.organization_id, route.revision)
            configured = next((item for item in snapshot.definition.routes if route.event in item.events), None) if snapshot else None
            if (configured is None or snapshot is None or any(agent.role != AgentRole.DEVELOPER for agent in snapshot.definition.agents
                    if agent.id in configured.delegation.eligible_agents)):
                raise ValueError("Dependency handoff requires an explicitly configured Developer-only route")

    def _path(self, task_id: str) -> Path:
        path = self.directory / (sha256(task_id.encode()).hexdigest() + ".json")
        if path.resolve() != path:
            raise PermissionError("Dependency handoff receipts cannot follow symlinks")
        return path

    def _load(self, task_id: str) -> DependencyHandoff:
        record = DependencyHandoff.model_validate_json(self._path(task_id).read_text())
        if record.task_id != task_id or record.identity != task_id or json.loads(record.bundle_content)["task_id"] != task_id:
            raise PermissionError("Dependency handoff address or immutable scope changed")
        return record

    def _source(self, assignment_id: str, issue_key: str) -> tuple[PlanRevision, DeveloperPreview, dict[str, DeveloperPreview]]:
        plan = self.planning.plans.get(assignment_id)
        publication = self.planning._load_publication(assignment_id, plan)
        if (plan.pins.get("assignment_id") != assignment_id or publication.state != "published" or
                publication.approved_at is None or publication.link_approved_at is None or
                publication.operator_id != self.planning.admission.operator_id or
                len(publication.effects) != len(plan.proposal.issues) or
                len(publication.developer_previews) != len(publication.effects) or any(effect.state != "linked" for effect in publication.effects)):
            raise PermissionError("Dependency handoff requires complete exact-approved PM publication receipts")
        children: dict[str, DeveloperPreview] = {}
        scope = self.planning._target(plan)
        plan.proposal.check_limits(scope.limits)
        for effect, preview_id in zip(publication.effects, publication.developer_previews, strict=True):
            preview = self.planning.admission.previews.get(preview_id)
            if preview is None or effect.receipt is None:
                raise PermissionError("Dependency source preview or issue receipt is missing")
            draft = next(item for item in plan.proposal.issues if item.key == effect.key)
            bundle = developer_task_bundle_from_payload(preview.bundle_payload)
            issue = bundle.issue_context
            expected_task = "planned-issue-" + sha256((assignment_id + ":" + effect.key).encode()).hexdigest()
            if (bundle.task_id != expected_task or bundle.objective != draft.objective or
                    bundle.acceptance_criteria != draft.acceptance_criteria or bundle.constraints or bundle.context_files or
                    canonical(preview.bundle_payload["policy"]) != canonical(plan.pins["developer_policy"]) or issue is None or
                    (issue.repository, issue.repository_id, issue.issue_number, issue.issue_id, issue.title, issue.body, issue.base_branch, issue.base_revision) !=
                    (scope.repository, scope.repository_id, effect.receipt["number"], effect.receipt["issue_id"], effect.receipt["title"],
                     effect.receipt["body"], scope.base_branch, scope.base_revision)):
                raise PermissionError("Published child scope or issue identity changed")
            current = self.planning.github.get_issue(repository=scope.repository, issue_number=issue.issue_number)
            if current.state not in ({"open"} if effect.key == issue_key else {"open", "closed"}):
                raise PermissionError("Dependent issue must remain open")
            if self.planning._receipt(replace(current, state="open"), effect.content, scope) != effect.receipt:
                raise PermissionError("Published issue bytes or identity changed before dependency handoff")
            children[effect.key] = preview
        dependent = next((item for item in plan.proposal.issues if item.key == issue_key), None)
        if dependent is None or not dependent.dependencies:
            raise PermissionError("Dependency handoff requires a known dependent planned issue")
        return plan, children[issue_key], children

    def _handoffs(self, source: DeveloperPreview) -> tuple[DependencyHandoff, ...]:
        paths = tuple(islice(self.directory.glob("*.json"), self.lookup_max_receipts + 1))
        if len(paths) > self.lookup_max_receipts:
            raise PermissionError("Dependency receipt lookup exceeded its operator-configured bound")
        records = []
        for path in paths:
            if path.resolve() != path:
                raise PermissionError("Dependency handoff receipts cannot follow symlinks")
            document = DependencyHandoff.model_validate_json(path.read_text())
            if path != self._path(document.task_id):
                raise PermissionError("Dependency handoff receipt address changed")
            record = self._load(document.task_id)
            if record.source_preview_id == source.preview_id:
                records.append(record)
        return tuple(records)

    def _validate_scope(self, record: DependencyHandoff, source: DeveloperPreview, plan: PlanRevision) -> None:
        expected = json.loads(canonical(source.bundle_payload))
        expected["task_id"] = record.task_id
        expected["issue_context"]["base_revision"] = record.base_revision
        draft = next((item for item in plan.proposal.issues if item.key == record.issue_key), None)
        if (record.plan_digest != plan.digest or record.source_preview_id != source.preview_id or
                canonical(expected) != record.bundle_content or
                record.operator_id != self.planning.admission.operator_id or
            draft is None or tuple(item.get("key") for item in record.prerequisites) != draft.dependencies or
                any(item.get("ready") is not True for item in record.prerequisites)):
            raise PermissionError("Handoff differs from its original approved plan and dependency scope")

    def _parent(self, plan: PlanRevision, dependency: str, source: DeveloperPreview) -> DeveloperPreview:
        candidates = []
        original = self._delivery(source.preview_id)
        if original is not None and original.state == "published":
            candidates.append(source)
        for record in self._handoffs(source):
            self._validate_scope(record, source, plan)
            if record.issue_key != dependency or record.preview_id is None:
                raise PermissionError("Parent handoff lineage is incomplete")
            preview = self.planning.admission.previews.get(record.preview_id)
            if preview is None or canonical(preview.bundle_payload) != record.bundle_content:
                raise PermissionError("Parent handoff preview is missing or changed")
            delivery = self._delivery(preview.preview_id)
            if delivery is not None and delivery.state == "published":
                candidates.append(preview)
        if len(candidates) > 1:
            raise PermissionError("Multiple prerequisite publications require operator reconciliation")
        return candidates[0] if candidates else source

    def _delivery(self, preview_id: str) -> DeliveryPreparation | None:
        records = [record for owner in (self.worker, *self.prerequisite_workers)
                   if (record := owner.get(preview_id)) is not None]
        if len(records) > 1:
            raise PermissionError("Prerequisite delivery has ambiguous receipt ownership")
        return records[0] if records else None

    def _prerequisites(self, plan: PlanRevision, issue_key: str, children: dict[str, DeveloperPreview], base_revision: str,
                       cache: dict[str, tuple[dict[str, Any], ...]] | None = None) -> tuple[dict[str, Any], ...]:
        if cache is None:
            cache = {}
        if issue_key in cache:
            return cache[issue_key]
        scope = self.planning._target(plan)
        draft = next(item for item in plan.proposal.issues if item.key == issue_key)
        evidence = []
        for dependency in draft.dependencies:
            preview = self._parent(plan, dependency, children[dependency])
            record = self._delivery(preview.preview_id)
            if record is None or record.state != "published":
                evidence.append({"key": dependency, "preview_id": preview.preview_id, "ready": False, "reason": "not_published"})
                continue
            publication = record.publication
            issue = developer_task_bundle_from_payload(preview.bundle_payload).issue_context
            verification = record.verification
            if (not preview.approved or preview.approved_at is None or record.approved_at != preview.approved_at.isoformat() or
                    publication is None or issue is None or record.preview_id != preview.preview_id or
                    canonical(record.bundle_payload) != canonical(preview.bundle_payload) or
                    publication.get("repository") != scope.repository or publication.get("issue_number") != issue.issue_number or
                    publication.get("base_ref") != scope.base_branch or record.head_revision != publication.get("head_sha")):
                raise PermissionError("Prerequisite delivery differs from the exact published child")
            if (not isinstance(verification, dict) or verification.get("cleanup_succeeded") is not True or
                    not isinstance(verification.get("commands"), list) or not verification["commands"] or
                    any(type(item.get("exit_code")) is not int or item["exit_code"] != 0 for item in verification["commands"])):
                raise PermissionError("Prerequisite lacks successful independent verification and cleanup")
            merged = self.planning.github.inspect_dependency_merge(repository=scope.repository, repository_id=scope.repository_id,
                pull_number=publication["pull_number"], head_sha=publication["head_sha"], head_branch=publication["branch"],
                base_branch=scope.base_branch, base_revision=base_revision)
            if merged["ready"] is True and preview.preview_id != children[dependency].preview_id:
                ancestors = self._prerequisites(plan, dependency, children, base_revision, cache)
                if any(item["ready"] is not True for item in ancestors):
                    raise PermissionError("Published parent handoff has unresolved transitive prerequisites")
            evidence.append({"key": dependency, "preview_id": preview.preview_id, "publication_content": canonical(publication), **merged})
        cache[issue_key] = tuple(evidence)
        return cache[issue_key]

    def offer(self, *, planning_assignment_id: str, issue_key: str, base_revision: str) -> dict[str, Any]:
        plan, source, children = self._source(planning_assignment_id, issue_key)
        scope = PlanningScope.model_validate(self.planning._target(plan).model_dump() | {"base_revision": base_revision})
        route = next((route for route in self.routes if (route.repository, route.repository_id) == (scope.repository, scope.repository_id)), None)
        if route is None:
            raise PermissionError("Dependent repository is not explicitly activated")
        if source.approved or self._delivery(source.preview_id) is not None:
            raise PermissionError("An admitted original dependent cannot be replaced by handoff")
        for sibling in self._handoffs(source):
            if sibling.preview_id is not None:
                delivery = self.worker.get(sibling.preview_id)
                if delivery is not None and delivery.state != "failed" and sibling.base_revision != base_revision:
                    raise PermissionError("Existing dependent delivery must be resolved before staging another base")
        evidence = self._prerequisites(plan, issue_key, children, base_revision)
        if any(item["ready"] is not True for item in evidence):
            return {"state": "waiting", "prerequisites": evidence, "handoff_preview": None, "implementation_approved": False}
        payload = json.loads(canonical(source.bundle_payload))
        payload["task_id"] = "dependency-probe"
        payload["issue_context"]["base_revision"] = base_revision
        record = DependencyHandoff(task_id="dependency-probe", planning_assignment_id=planning_assignment_id, plan_digest=plan.digest,
            issue_key=issue_key, source_preview_id=source.preview_id, operator_id=self.planning.admission.operator_id,
            route=route, base_revision=base_revision, bundle_content=canonical(payload), prerequisites=evidence)
        payload["task_id"] = record.identity
        record = record.model_copy(update={"task_id": record.identity, "bundle_content": canonical(payload)})
        developer_task_bundle_from_payload(payload)
        path = self._path(record.task_id)
        with FileLock(str(path) + ".lock", timeout=0):
            if path.exists():
                saved = self._load(record.task_id)
                if saved.model_dump(exclude={"preview_id"}) != record.model_dump(exclude={"preview_id"}):
                    raise PermissionError("Saved handoff cannot replace scope or prerequisite evidence")
                record = saved
            else:
                if self.planning.admission.previews.get_by_dedupe(record.task_id) is not None:
                    raise PermissionError("Lost handoff receipt cannot be recreated")
                atomic_write_text(path, record.model_dump_json())
            if record.preview_id is not None and self.planning.admission.previews.get(record.preview_id) is None:
                raise PermissionError("Lost handoff preview cannot be recreated")
            preview = self.planning.admission.previews.create_or_get(dedupe_key=record.task_id, task_key=record.task_id,
                bundle_payload=payload, source_payload={"dependency_handoff": record.task_id, "planning_assignment": planning_assignment_id})
            if record.preview_id is not None and record.preview_id != preview.preview_id:
                raise PermissionError("Saved handoff preview identity changed")
            if record.preview_id is None:
                record = record.model_copy(update={"preview_id": preview.preview_id})
                atomic_write_text(path, record.model_dump_json())
        return {"state": "staged", "prerequisites": evidence,
                "handoff_preview": {"preview_id": preview.preview_id, "approved": preview.approved, "bundle": preview.bundle_payload},
                "implementation_approved": preview.approved}

    def route_for(self, preview: DeveloperPreview, *, recheck: bool = True) -> PlanningRoute | None:
        task_id = str(preview.bundle_payload.get("task_id", ""))
        if task_id.startswith("planned-issue-"):
            if "planning_assignment" not in preview.source_payload:
                raise PermissionError("Planned task requires its trusted publication receipt")
            assignment_id = str(preview.source_payload["planning_assignment"])
            plan = self.planning.plans.get(assignment_id)
            saved = self.planning._load_publication(assignment_id, plan)
            if preview.preview_id not in saved.developer_previews:
                raise PermissionError("Planned task is not an exact published child")
            index = saved.developer_previews.index(preview.preview_id)
            key = saved.effects[index].key
            if next(item for item in plan.proposal.issues if item.key == key).dependencies:
                raise PermissionError("Original dependent requires a fresh dependency handoff and separate approval")
            return None
        if not task_id.startswith("dependent-issue-"):
            return None
        record = self._load(task_id)
        if (record.preview_id != preview.preview_id or canonical(preview.bundle_payload) != record.bundle_content or
                record.operator_id != self.planning.admission.operator_id or not any(
                    (route.repository, route.repository_id, route.organization_id) ==
                    (record.route.repository, record.route.repository_id, record.route.organization_id) for route in self.routes)):
            raise PermissionError("Handoff preview scope, identity or activation changed")
        if recheck:
            if record.route not in self.routes:
                raise PermissionError("Handoff activation changed before admission")
            plan, source, children = self._source(record.planning_assignment_id, record.issue_key)
            self._validate_scope(record, source, plan)
            if (plan.digest != record.plan_digest or source.preview_id != record.source_preview_id or source.approved or
                    self._prerequisites(plan, record.issue_key, children, record.base_revision) != record.prerequisites):
                raise PermissionError("Handoff prerequisite evidence changed before admission")
        return record.route