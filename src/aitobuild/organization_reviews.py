"""Opt-in publication-bound review task staging and admission."""

from __future__ import annotations

from hashlib import sha256
from glob import escape
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from filelock import FileLock
from pydantic import Field, StrictStr, model_validator

from aitobuild.developer_delivery import DeveloperDeliveryWorker
from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.developer_preview import DeveloperPreview, DeveloperPreviewRegistry
from aitobuild.organization import DefinitionModel, DefinitionStore, EventName, Identifier
from aitobuild.organization_runner import ManagedRun, _sync_directory
from aitobuild.policy import AgentRole
from aitobuild.tools.github import GitHubAdapter


class PublishedReviewTarget(DefinitionModel):
    preview_id: Annotated[StrictStr, Field(min_length=1)]
    head_sha: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40}$")]


class CorrectionProposal(DefinitionModel):
    objective: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    paths: Annotated[tuple[StrictStr, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if (not self.objective.strip() or "\x00" in self.objective or len(self.objective.encode("utf-8")) > 2000 or
                len(set(self.paths)) != len(self.paths) or any(not path or "\x00" in path for path in self.paths)):
            raise ValueError("Correction proposals require a bounded objective and unique explicit paths")
        return self


class PublishedReviewRoute(DefinitionModel):
    repository: Annotated[StrictStr, Field(min_length=1)]
    repository_id: Annotated[int, Field(strict=True, gt=0)]
    organization_id: Identifier
    revision: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
    event: EventName


class _ReviewReceipt(DefinitionModel):
    task_id: StrictStr
    target: PublishedReviewTarget
    route: PublishedReviewRoute
    publication_content: StrictStr
    bundle_content: StrictStr
    state: Literal["staging", "staged"] = "staging"
    review_preview_id: StrictStr | None = None
    ledger_state: Literal["none", "creating", "ready"] = "none"
    approved_at: StrictStr | None = None
    budget_path: StrictStr | None = None
    deadline: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None = None


class _CorrectionReceipt(DefinitionModel):
    task_id: StrictStr
    review_preview_id: StrictStr
    target: PublishedReviewTarget
    route: PublishedReviewRoute
    run_content: StrictStr
    bundle_content: StrictStr
    state: Literal["staging", "staged"] = "staging"
    correction_preview_id: StrictStr | None = None


class _ReviewJournal(DefinitionModel):
    schema_version: Literal[1] = 1
    receipts: dict[str, _ReviewReceipt] = {}
    corrections: dict[str, _CorrectionReceipt] = {}


class PublishedReviewAdmission:
    def __init__(self, *, definitions: DefinitionStore, previews: DeveloperPreviewRegistry,
                 worker: DeveloperDeliveryWorker, github: GitHubAdapter, state_dir: Path,
                 routes: tuple[PublishedReviewRoute, ...], correction_routes: tuple[PublishedReviewRoute, ...] = ()) -> None:
        self._definitions = definitions
        self._previews = previews
        self._worker = worker
        self._github = github
        self._directory = state_dir.resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._path = self._directory / "reviews.json"
        self._routes = tuple(PublishedReviewRoute.model_validate(route.model_dump()) for route in routes)
        self._correction_routes = tuple(PublishedReviewRoute.model_validate(route.model_dump()) for route in correction_routes)
        if len({(route.repository, route.repository_id) for route in routes}) != len(routes):
            raise ValueError("Published review routes must be unambiguous")
        for route in routes:
            self._validate_route(route)
        if len({(route.repository, route.repository_id) for route in correction_routes}) != len(correction_routes):
            raise ValueError("Correction routes must be unambiguous")
        for route in correction_routes:
            self._validate_route(route, role=AgentRole.DEVELOPER)

    @property
    def routes(self) -> tuple[PublishedReviewRoute, ...]:
        return self._routes

    @property
    def correction_routes(self) -> tuple[PublishedReviewRoute, ...]:
        return self._correction_routes

    def validate_binding(self, *, definitions: DefinitionStore, previews: DeveloperPreviewRegistry, worker: DeveloperDeliveryWorker) -> None:
        if (definitions is not self._definitions or previews is not self._previews or worker is not self._worker):
            raise PermissionError("Published review admission must use the service-owned definitions, previews and worker")

    def _validate_route(self, activation: PublishedReviewRoute, *, role: AgentRole = AgentRole.ARCHITECT) -> None:
        snapshot = self._definitions.get(activation.organization_id, activation.revision)
        route = next((route for route in snapshot.definition.routes if activation.event in route.events), None) if snapshot else None
        if (snapshot is None or route is None or any(
                agent.role != role for agent in snapshot.definition.agents
                if agent.id in route.delegation.eligible_agents)):
            raise PermissionError("Published task requires a pinned route with role-scoped eligibility")

    def _load(self) -> _ReviewJournal:
        if self._path.resolve() != self._path:
            raise ValueError("Review journals cannot follow symlinks")
        if not self._path.exists():
            return _ReviewJournal()
        journal = _ReviewJournal.model_validate_json(self._path.read_text(encoding="utf-8"))
        for task_id, receipt in journal.receipts.items():
            payload = json.loads(receipt.bundle_content)
            if (not isinstance(payload, dict) or receipt.task_id != task_id or payload.get("task_id") != task_id or
                    (receipt.state == "staged") != (receipt.review_preview_id is not None) or
                    (receipt.ledger_state != "none") != (receipt.approved_at is not None and receipt.budget_path is not None) or
                receipt.ledger_state == "none" and (receipt.approved_at is not None or receipt.budget_path is not None) or
                receipt.state == "staging" and receipt.ledger_state != "none" or
                    (receipt.ledger_state == "ready") != (receipt.deadline is not None)):
                raise ValueError("Review journal task identity/state is invalid")
        for task_id, correction in journal.corrections.items():
            payload = json.loads(correction.bundle_content)
            if (not isinstance(payload, dict) or correction.task_id != task_id or payload.get("task_id") != task_id or
                    (correction.state == "staged") != (correction.correction_preview_id is not None)):
                raise ValueError("Correction journal task identity/state is invalid")
        return journal

    def _save(self, journal: _ReviewJournal) -> None:
        original = self._load()
        for task_id, receipt in original.receipts.items():
            updated = journal.receipts.get(task_id)
            mutable = {"state", "review_preview_id", "ledger_state", "approved_at", "budget_path", "deadline"}
            if (updated is None or receipt.model_dump(exclude=mutable) != updated.model_dump(exclude=mutable) or
                    receipt.state == "staged" and updated.review_preview_id != receipt.review_preview_id or
                    updated.ledger_state not in {"none": {"none", "creating"}, "creating": {"creating", "ready"}, "ready": {"ready"}}[receipt.ledger_state] or
                    receipt.approved_at is not None and (receipt.approved_at, receipt.budget_path) != (updated.approved_at, updated.budget_path) or
                    receipt.deadline is not None and receipt.deadline != updated.deadline):
                raise PermissionError("Review pins and staged receipts are immutable")
        for task_id, correction in original.corrections.items():
            updated_correction = journal.corrections.get(task_id)
            if (updated_correction is None or correction.model_dump(exclude={"state", "correction_preview_id"}) !=
                    updated_correction.model_dump(exclude={"state", "correction_preview_id"}) or
                    correction.state == "staged" and correction != updated_correction):
                raise PermissionError("Correction pins and staged receipts are immutable")
        temporary = self._path.with_suffix(".tmp")
        if temporary.resolve() != temporary:
            raise ValueError("Review temporary journals cannot follow symlinks")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(journal.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self._path)
        _sync_directory(self._directory)

    def _publication(self, preview_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self._worker.get(preview_id)
        if record is None or record.state != "published" or record.publication is None:
            raise PermissionError("Review staging requires a saved published delivery")
        pull = self._worker.get_published_pull_request(preview_id, github=self._github)
        if not pull["head_matches_publication"]:
            raise PermissionError("Review staging requires the exact live published head")
        return record.publication, record.bundle_payload

    def offer(self, published_preview_id: str) -> DeveloperPreview | None:
        record = self._worker.get(published_preview_id)
        issue = record.bundle_payload.get("issue_context") if record else None
        if not isinstance(issue, dict):
            raise PermissionError("Published review requires repository/issue identity")
        route = next((route for route in self._routes if (route.repository, route.repository_id) ==
                      (issue.get("repository"), issue.get("repository_id"))), None)
        if route is None:
            return None
        with FileLock(str(self._path) + ".lock", timeout=10):
            publication, payload = self._publication(published_preview_id)
            publication_content = json.dumps(publication, sort_keys=True, separators=(",", ":"), allow_nan=False)
            target = PublishedReviewTarget(preview_id=published_preview_id, head_sha=publication["head_sha"])
            journal = self._load()
            saved = [receipt for receipt in journal.receipts.values() if receipt.target.preview_id == published_preview_id]
            if len(saved) > 1:
                raise PermissionError("Published target has ambiguous review staging receipts")
            if saved:
                saved_receipt = saved[0]
                if (saved_receipt.target != target or saved_receipt.publication_content != publication_content or
                        (saved_receipt.route.repository, saved_receipt.route.repository_id, saved_receipt.route.organization_id) !=
                        (route.repository, route.repository_id, route.organization_id)):
                    raise PermissionError("Saved published review target/activation changed")
                route = saved_receipt.route
            self._validate_route(route)
            task_id = "published-review-" + sha256(json.dumps({"publication": publication_content,
                "target": target.model_dump(), "route": route.model_dump()}, sort_keys=True).encode()).hexdigest()
            bundle = json.loads(json.dumps(payload))
            bundle["task_id"] = task_id
            bundle["objective"] = "Review published draft " + str(publication["pull_number"]) + " at head " + target.head_sha
            bundle["policy"]["allowed_commands"] = []
            bundle["policy"]["max_file_changes"] = 0
            bundle_content = json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            receipt = journal.receipts.get(task_id)
            if receipt is None:
                receipt = _ReviewReceipt(task_id=task_id, target=target, route=route,
                                         publication_content=publication_content, bundle_content=bundle_content)
                journal.receipts[task_id] = receipt
                self._save(journal)
            elif (receipt.target != target or receipt.route != route or receipt.bundle_content != bundle_content or
                  receipt.publication_content != publication_content):
                raise PermissionError("Review staging cannot replace pinned scope or target")
            preview = self._previews.create_or_get(dedupe_key=task_id, bundle_payload=bundle,
                                                  source_payload={"published_review": target.model_dump(mode="json")})
            if receipt.state == "staged":
                if receipt.review_preview_id != preview.preview_id:
                    raise PermissionError("Staged review preview identity changed")
            else:
                journal.receipts[task_id] = receipt.model_copy(update={"state": "staged", "review_preview_id": preview.preview_id})
                self._save(journal)
            return preview

    def _receipt(self, preview_id: str) -> _ReviewReceipt | None:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ValueError("Review preview not found")
        with FileLock(str(self._path) + ".lock", timeout=10):
            receipt = self._load().receipts.get(str(preview.bundle_payload["task_id"]))
        if receipt is None:
            if str(preview.bundle_payload["task_id"]).startswith("published-review-"):
                raise PermissionError("Review task lacks its trusted staging receipt")
            return None
        if (receipt.state != "staged" or receipt.review_preview_id != preview_id or
                json.dumps(preview.bundle_payload, sort_keys=True, separators=(",", ":"), allow_nan=False) != receipt.bundle_content):
            raise PermissionError("Review staging is incomplete or approved scope differs")
        if not any((route.repository, route.repository_id, route.organization_id) ==
                   (receipt.route.repository, receipt.route.repository_id, receipt.route.organization_id) for route in self._routes):
            raise PermissionError("Saved review is outside this operator activation")
        self._validate_route(receipt.route)
        return receipt

    def route_for(self, preview_id: str) -> PublishedReviewRoute | None:
        receipt = self._receipt(preview_id)
        return receipt.route if receipt else None

    def target_for(self, preview_id: str) -> PublishedReviewTarget:
        receipt = self._receipt(preview_id)
        if receipt is None:
            raise PermissionError("Task is not a staged publication review")
        publication, _ = self._publication(receipt.target.preview_id)
        if json.dumps(publication, sort_keys=True, separators=(",", ":"), allow_nan=False) != receipt.publication_content:
            raise PermissionError("Published review target snapshot changed")
        return receipt.target

    def offer_correction(self, review_preview_id: str, run: ManagedRun) -> DeveloperPreview | None:
        review = self._receipt(review_preview_id)
        if review is None:
            raise PermissionError("Correction requires a trusted staged review")
        preview = self._previews.get(review_preview_id)
        if (run.state != "completed" or run.cleanup_succeeded is not True or
            (run.organization_id, run.revision) != (review.route.organization_id, review.route.revision) or preview is None or not preview.approved or
                run.scope_digest != sha256(review.bundle_content.encode()).hexdigest()):
            raise PermissionError("Correction requires a completed approved review with unchanged scope")
        candidates = [output for output in run.outputs if isinstance(output, dict) and "correction" in output]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise PermissionError("Correction requires one unambiguous completed proposal")
        output = candidates[0]
        target_payload = output.get("target")
        if not isinstance(target_payload, dict):
            raise PermissionError("Completed correction proposal requires explicit target pins")
        target = PublishedReviewTarget.model_validate({key: target_payload.get(key) for key in ("preview_id", "head_sha")})
        if target != self.target_for(review_preview_id) or output.get("metadata_only") is not True:
            raise PermissionError("Correction proposal differs from the completed reviewed target")
        self._worker.refuse_superseded(target.preview_id)
        published = self._worker.get(target.preview_id)
        assert published is not None and published.publication is not None
        if not published.architect_review or output.get("architect_review") != published.architect_review:
            raise PermissionError("Correction requires the saved approved COMMENT receipt")
        proposal = CorrectionProposal.model_validate(output["correction"])
        if not all(path in published.publication["changed_paths"] for path in proposal.paths):
            raise PermissionError("Correction proposal escapes the inspected published paths")
        issue = published.bundle_payload["issue_context"]
        route = next((route for route in self._correction_routes if (route.repository, route.repository_id) ==
                      (issue["repository"], issue["repository_id"])), None)
        if route is None:
            return None
        run_content = run.model_dump_json()
        with FileLock(str(self._path) + ".lock", timeout=10):
            journal = self._load()
            saved = [receipt for receipt in journal.corrections.values() if receipt.review_preview_id == review_preview_id]
            if len(saved) > 1:
                raise PermissionError("Review has ambiguous correction receipts")
            siblings = [receipt for receipt in journal.corrections.values()
                        if receipt.target.head_sha == target.head_sha and receipt.review_preview_id != review_preview_id
                        and not self._worker.correction_releases_parent(receipt.correction_preview_id)]
            if siblings:
                raise PermissionError(
                    f"Correction {siblings[0].correction_preview_id or siblings[0].task_id} already targets "
                    f"head {target.head_sha}; offer corrections only from the chain tip"
                )
            if saved:
                original = saved[0]
                if original.run_content != run_content or original.target != target or original.route.organization_id != route.organization_id:
                    raise PermissionError("Correction source/target/activation changed")
                route = original.route
            self._validate_route(route, role=AgentRole.DEVELOPER)
            task_id = "published-correction-" + sha256(json.dumps({"review": review_preview_id, "run": run_content,
                                                                  "route": route.model_dump()}, sort_keys=True).encode()).hexdigest()
            bundle = json.loads(json.dumps(published.bundle_payload))
            bundle["task_id"] = task_id
            bundle["objective"] = proposal.objective
            bundle["issue_context"]["base_revision"] = target.head_sha
            bundle["issue_context"]["base_branch"] = published.publication["branch"]
            bundle["policy"]["allowed_paths"] = [escape(path) for path in proposal.paths]
            bundle["policy"]["max_file_changes"] = min(len(proposal.paths), bundle["policy"]["max_file_changes"])
            developer_task_bundle_from_payload(bundle)
            content = json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            receipt = journal.corrections.get(task_id)
            if receipt is None:
                receipt = _CorrectionReceipt(task_id=task_id, review_preview_id=review_preview_id, target=target,
                                             route=route, run_content=run_content, bundle_content=content)
                journal.corrections[task_id] = receipt
                self._save(journal)
            elif receipt.bundle_content != content:
                raise PermissionError("Correction scope cannot replace its staging receipt")
            staged = self._previews.create_or_get(dedupe_key=task_id, bundle_payload=bundle,
                                                  source_payload={"correction_from_review": review_preview_id, "published_review": target.model_dump(mode="json")})
            if receipt.state == "staged":
                if receipt.correction_preview_id != staged.preview_id:
                    raise PermissionError("Correction preview identity changed")
            else:
                journal.corrections[task_id] = receipt.model_copy(update={"state": "staged", "correction_preview_id": staged.preview_id})
                self._save(journal)
            return staged

    def correction_route_for(self, preview_id: str) -> PublishedReviewRoute | None:
        receipt = self._correction_receipt(preview_id)
        if receipt is None:
            return None
        if self.target_for(receipt.review_preview_id) != receipt.target:
            raise PermissionError("Correction reviewed head is stale")
        return receipt.route

    def correction_publication_binding(self, preview_id: str) -> str:
        """Digest of the trusted correction and triggering review receipts (the reviewed head need not stay live)."""
        receipt = self._correction_receipt(preview_id)
        if receipt is None:
            raise PermissionError("Same-PR publication requires a trusted correction receipt")
        review = self._receipt(receipt.review_preview_id)
        if review is None or review.target != receipt.target:
            raise PermissionError("Correction receipt differs from its triggering review")
        stable = {
            "correction": receipt.model_dump(mode="json", include={"task_id", "review_preview_id", "target", "route",
                                                                   "run_content", "bundle_content",
                                                                   "correction_preview_id"}),
            "review": review.model_dump(mode="json", include={"task_id", "target", "route", "publication_content",
                                                              "bundle_content", "review_preview_id"}),
        }
        return sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _correction_receipt(self, preview_id: str) -> _CorrectionReceipt | None:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise ValueError("Correction preview not found")
        with FileLock(str(self._path) + ".lock", timeout=10):
            receipt = self._load().corrections.get(str(preview.bundle_payload["task_id"]))
        if receipt is None:
            if str(preview.bundle_payload["task_id"]).startswith("published-correction-"):
                raise PermissionError("Correction task lacks its trusted staging receipt")
            return None
        if (receipt.state != "staged" or receipt.correction_preview_id != preview_id or
                json.dumps(preview.bundle_payload, sort_keys=True, separators=(",", ":"), allow_nan=False) != receipt.bundle_content):
            raise PermissionError("Correction staging is incomplete or scope differs")
        if not any((route.repository, route.repository_id, route.organization_id) ==
                   (receipt.route.repository, receipt.route.repository_id, receipt.route.organization_id) for route in self._correction_routes):
            raise PermissionError("Correction is outside this operator activation")
        self._validate_route(receipt.route, role=AgentRole.DEVELOPER)
        return receipt

    def budget_path(self, preview_id: str) -> Path:
        path = self._directory / "budgets" / (sha256(preview_id.encode()).hexdigest() + ".json")
        if path.resolve() != path:
            raise ValueError("Review budgets cannot follow symlinks")
        return path

    def original_budget_path(self, preview_id: str) -> Path | None:
        with FileLock(str(self._path) + ".lock", timeout=10):
            matches = [receipt for receipt in self._load().receipts.values() if receipt.review_preview_id == preview_id]
        if not matches:
            return None
        if len(matches) != 1:
            raise PermissionError("Review ledger has ambiguous preview identity")
        receipt = matches[0]
        path = self.budget_path(preview_id)
        if receipt.ledger_state != "ready" or receipt.approved_at is None or receipt.budget_path != str(path):
            raise PermissionError("Review original approved ledger is not initialized/bound")
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("deadline") != receipt.deadline:
            raise PermissionError("Review original budget deadline changed")
        return path

    def prepare(self, preview_id: str) -> None:
        self.target_for(preview_id)
        preview = self._previews.get(preview_id)
        if preview is None or not preview.approved or preview.approved_at is None:
            raise PermissionError("Review execution requires separate task approval")
        with FileLock(str(self._path) + ".lock", timeout=10):
            journal = self._load()
            receipt = journal.receipts[str(preview.bundle_payload["task_id"])]
            path = self.budget_path(preview_id)
            approval = preview.approved_at.isoformat()
            create = receipt.ledger_state == "none"
            if create:
                receipt = receipt.model_copy(update={"ledger_state": "creating", "approved_at": approval, "budget_path": str(path)})
                journal.receipts[receipt.task_id] = receipt
                self._save(journal)
            elif (receipt.approved_at, receipt.budget_path) != (approval, str(path)):
                raise PermissionError("Review original approval/budget binding changed")
            budget = DeveloperTaskBudget(path=path, bundle=developer_task_bundle_from_payload(preview.bundle_payload), create=create)
            budget.remaining_seconds()
            deadline = json.loads(path.read_text(encoding="utf-8"))["deadline"]
            if receipt.ledger_state == "ready" and receipt.deadline != deadline:
                raise PermissionError("Review original budget deadline changed")
            if receipt.ledger_state != "ready":
                journal.receipts[receipt.task_id] = receipt.model_copy(update={"ledger_state": "ready", "deadline": deadline})
                self._save(journal)