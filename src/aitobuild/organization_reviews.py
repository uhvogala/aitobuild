"""Opt-in publication-bound review task staging and admission."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from filelock import FileLock
from pydantic import Field, StrictStr

from aitobuild.developer_delivery import DeveloperDeliveryWorker
from aitobuild.developer_isolation import DeveloperTaskBudget, developer_task_bundle_from_payload
from aitobuild.developer_preview import DeveloperPreview, DeveloperPreviewRegistry
from aitobuild.organization import DefinitionModel, DefinitionStore, EventName, Identifier
from aitobuild.organization_runner import _sync_directory
from aitobuild.policy import AgentRole
from aitobuild.tools.github import GitHubAdapter


class PublishedReviewTarget(DefinitionModel):
    preview_id: Annotated[StrictStr, Field(min_length=1)]
    head_sha: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40}$")]


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


class _ReviewJournal(DefinitionModel):
    schema_version: Literal[1] = 1
    receipts: dict[str, _ReviewReceipt] = {}


class PublishedReviewAdmission:
    def __init__(self, *, definitions: DefinitionStore, previews: DeveloperPreviewRegistry,
                 worker: DeveloperDeliveryWorker, github: GitHubAdapter, state_dir: Path,
                 routes: tuple[PublishedReviewRoute, ...]) -> None:
        self._definitions = definitions
        self._previews = previews
        self._worker = worker
        self._github = github
        self._directory = state_dir.resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._path = self._directory / "reviews.json"
        self._routes = tuple(PublishedReviewRoute.model_validate(route.model_dump()) for route in routes)
        if len({(route.repository, route.repository_id) for route in routes}) != len(routes):
            raise ValueError("Published review routes must be unambiguous")
        for route in routes:
            self._validate_route(route)

    @property
    def routes(self) -> tuple[PublishedReviewRoute, ...]:
        return self._routes

    def validate_binding(self, *, definitions: DefinitionStore, previews: DeveloperPreviewRegistry, worker: DeveloperDeliveryWorker) -> None:
        if (definitions is not self._definitions or previews is not self._previews or worker is not self._worker):
            raise PermissionError("Published review admission must use the service-owned definitions, previews and worker")

    def _validate_route(self, activation: PublishedReviewRoute) -> None:
        snapshot = self._definitions.get(activation.organization_id, activation.revision)
        route = next((route for route in snapshot.definition.routes if activation.event in route.events), None) if snapshot else None
        if (snapshot is None or route is None or any(
                agent.role != AgentRole.ARCHITECT for agent in snapshot.definition.agents
                if agent.id in route.delegation.eligible_agents)):
            raise PermissionError("Published review requires a pinned route with Architect-only eligibility")

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