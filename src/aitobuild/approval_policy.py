"""Immutable operator approval policy, independent of role permissions."""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
import json
from typing import Literal, Self, cast

from pydantic import Field, JsonValue, StrictInt, StrictStr, model_validator

from aitobuild.organization import DefinitionModel


ApprovalMode = Literal["human", "automatic", "disabled"]


class ApprovalStage(StrEnum):
    TASK_ADMISSION = "task_admission"
    DEVELOPER_TOOLS = "developer_tools"
    PM_ISSUE_CREATION = "pm_issue_creation"
    PM_DEPENDENCY_LINKS = "pm_dependency_links"
    DRAFT_PUBLICATION = "draft_publication"
    REVIEW_PUBLICATION = "review_publication"
    MEETING_CONTINUATION = "meeting_continuation"
    CORRECTION = "correction"
    MERGE = "merge"


class ApprovalDecision(DefinitionModel):
    stage: ApprovalStage
    mode: ApprovalMode
    source: Literal["human", "operator_policy"]
    actor_id: StrictStr = Field(min_length=1)
    policy_revision: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    operation_digest: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if not self.actor_id.strip() or self.mode == "disabled" or self.source == "operator_policy" and self.mode != "automatic":
            raise ValueError("Approval evidence requires a valid human or automatic operator decision")
        return self


class ApprovalPolicy(DefinitionModel):
    max_automatic_decisions: StrictInt = Field(default=32, ge=1, le=128)
    max_automatic_admissions: StrictInt = Field(default=32, ge=1, le=10000)
    task_admission: ApprovalMode = "human"
    developer_tools: ApprovalMode = "human"
    pm_issue_creation: ApprovalMode = "human"
    pm_dependency_links: ApprovalMode = "human"
    draft_publication: ApprovalMode = "human"
    review_publication: ApprovalMode = "human"
    meeting_continuation: ApprovalMode = "human"
    correction: ApprovalMode = "human"
    merge: ApprovalMode = "disabled"

    @property
    def revision(self) -> str:
        content = {"schema_version": 1, "stages": self.model_dump(mode="json")}
        return sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def mode(self, stage: ApprovalStage) -> ApprovalMode:
        if not isinstance(stage, ApprovalStage):
            raise TypeError("Approval stage must be a registered stage")
        return cast(ApprovalMode, getattr(self, stage.value))

    def resolve(
        self, stage: ApprovalStage, *, actor_id: str, operation: JsonValue,
        expected_revision: str | None = None, automatic_supported: bool = False,
        source: Literal["human", "operator_policy"] | None = None,
    ) -> ApprovalDecision:
        if not actor_id.strip():
            raise ValueError("Approval policy requires trusted operator identity")
        if expected_revision is not None and expected_revision != self.revision:
            raise PermissionError("Approval policy revision changed during active work")
        mode = self.mode(stage)
        if mode == "disabled":
            raise PermissionError(f"Approval stage is disabled: {stage.value}")
        if mode == "automatic" and not automatic_supported:
            raise PermissionError(f"Automatic approval adapter is unavailable: {stage.value}")
        source = source or ("operator_policy" if mode == "automatic" else "human")
        if source == "operator_policy" and mode != "automatic":
            raise PermissionError("Automatic decision requires automatic stage policy")
        content = json.dumps(operation, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return ApprovalDecision(stage=stage, mode=mode, source=source, actor_id=actor_id, policy_revision=self.revision,
                                operation_digest=sha256(content.encode()).hexdigest())