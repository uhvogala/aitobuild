"""Bounded native blocker meetings for explicitly configured managed graphs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import uuid4

from agent_framework import Agent, AgentResponse, Message, WorkflowRunState
from agent_framework.orchestrations import GroupChatBuilder, GroupChatState
from filelock import FileLock
from pydantic import Field, StrictInt, StrictStr, model_validator

from aitobuild.agents import default_agent_specs
from aitobuild.durable_files import atomic_write_text
from aitobuild.organization import DefinitionModel, Identifier
from aitobuild.organization_runner import (
    ManagedOperation, ManagedTaskContext, RunStore, WorkflowDecision, WorkflowInput, workflow_input_digest,
)
from aitobuild.organization_runtime import OrganizationRuntime
from aitobuild.organization import DefinitionSnapshot
from aitobuild.policy import AgentRole
from aitobuild.tool_outputs import MAX_PROMPT_BYTES


class BlockerRequest(DefinitionModel):
    agenda: Annotated[StrictStr, Field(min_length=1, max_length=4000)]
    evidence: Annotated[StrictStr, Field(min_length=1, max_length=16000)]


class MeetingLimits(DefinitionModel):
    max_rounds: Annotated[StrictInt, Field(ge=2, le=16)] = 4
    max_transcript_bytes: Annotated[StrictInt, Field(ge=1024, le=64000)] = 32000


class MeetingBinding(DefinitionModel):
    participants: tuple[Identifier, ...]
    resolver: Identifier
    limits: MeetingLimits = MeetingLimits()

    @model_validator(mode="after")
    def validate_participants(self) -> Self:
        if (not 2 <= len(self.participants) <= 8 or
                len(set(self.participants)) != len(self.participants) or
                self.resolver not in self.participants or
                self.limits.max_rounds < len(self.participants)):
            raise ValueError("Meetings require distinct configured participants and a participating resolver")
        return self


class MeetingResolution(DefinitionModel):
    outcome: Literal["continue", "scope_change", "escalate"]
    rationale: Annotated[StrictStr, Field(min_length=1, max_length=4000)]
    plan: Annotated[StrictStr, Field(min_length=1, max_length=8000)]


class MeetingMessage(DefinitionModel):
    author: StrictStr
    text: Annotated[StrictStr, Field(min_length=1, max_length=MAX_PROMPT_BYTES)]


class MeetingReceipt(DefinitionModel):
    schema_version: Literal[1] = 1
    meeting_id: Identifier
    pins: dict[str, Any]
    request: BlockerRequest
    binding: MeetingBinding
    nonce: StrictStr
    state: Literal["blocked", "running", "proposed", "resolved", "rejected", "escalated", "failed", "cancelled"]
    transcript: tuple[MeetingMessage, ...] = ()
    resolution: MeetingResolution | None = None
    decision: WorkflowDecision | None = None
    error: StrictStr | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if not self.pins or not self.nonce:
            raise ValueError("Meeting receipt requires immutable task pins and nonce")
        if len(self.transcript) > self.binding.limits.max_rounds + 1:
            raise ValueError("Meeting transcript exceeds its round ceiling")
        if sum(len(item.text.encode()) for item in self.transcript) > self.binding.limits.max_transcript_bytes:
            raise ValueError("Meeting transcript exceeds its byte ceiling")
        if self.transcript and (self.transcript[0].author != "task" or any(
                item.author not in self.binding.participants for item in self.transcript[1:])):
            raise ValueError("Transcript participants cannot escape the configured meeting")
        if self.state in {"proposed", "resolved", "rejected", "escalated"} and self.resolution is None:
            raise ValueError("Meeting outcome requires its saved proposal")
        if self.state in {"proposed", "resolved", "rejected"} and (
                self.resolution is None or self.resolution.outcome != "continue"):
            raise ValueError("Only a continuation proposal accepts approval")
        if self.state == "escalated" and (self.resolution is None or self.resolution.outcome == "continue"):
            raise ValueError("Escalation cannot authorize continuation")
        if (self.state in {"resolved", "rejected"}) != (self.decision is not None):
            raise ValueError("Meeting decisions exist only for consumed continuation approvals")
        if self.decision is not None and (self.decision.kind != "service_approval" or
                type(self.decision.value) is not bool or self.decision.value != (self.state == "resolved")):
            raise ValueError("Meeting decision must be an exact Boolean service approval")
        return self


class FileMeetingStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory.resolve()
        self._directory.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        path = self._directory / (sha256(run_id.encode()).hexdigest() + ".json")
        if path.resolve() != path:
            raise ValueError("Meeting receipts cannot follow symlinks")
        return path

    def invocation_lock(self, run_id: str) -> FileLock:
        return FileLock(str(self._path(run_id)) + ".invoke.lock", timeout=0)

    def get(self, run_id: str) -> MeetingReceipt | None:
        path = self._path(run_id)
        with FileLock(str(path) + ".state.lock", timeout=10):
            if not path.exists():
                return None
            receipt = MeetingReceipt.model_validate_json(path.read_text(encoding="utf-8"))
            if receipt.pins.get("run_id") != run_id:
                raise ValueError("Meeting receipt address differs from the pinned run")
            return receipt

    def save(self, receipt: MeetingReceipt) -> None:
        receipt = MeetingReceipt.model_validate(receipt.model_dump())
        path = self._path(str(receipt.pins["run_id"]))
        with FileLock(str(path) + ".state.lock", timeout=10):
            if path.exists():
                old = MeetingReceipt.model_validate_json(path.read_text(encoding="utf-8"))
                mutable = {"state", "transcript", "resolution", "decision", "error"}
                if old.model_dump(exclude=mutable) != receipt.model_dump(exclude=mutable):
                    raise PermissionError("Meeting task, scope, participants, limits and deadline are immutable")
                transitions = {"blocked": {"running", "failed", "cancelled"},
                               "running": {"running", "proposed", "escalated", "failed", "cancelled"},
                               "proposed": {"resolved", "rejected", "failed", "cancelled"}}
                if old == receipt:
                    return
                if receipt.state not in transitions.get(old.state, set()):
                    raise PermissionError("Meeting terminal receipts cannot replay or rewind")
                if old.state == "proposed" and (old.transcript != receipt.transcript or old.resolution != receipt.resolution):
                    raise PermissionError("Saved meeting proposals cannot change after publication")
                if receipt.transcript[:len(old.transcript)] != old.transcript:
                    raise PermissionError("Meeting transcript evidence is append-only")
            atomic_write_text(path, receipt.model_dump_json())


class NativeManagedMeetings:
    def __init__(
        self, *, runtime_for: Callable[[DefinitionSnapshot], OrganizationRuntime], state_dir: Path,
        runs: RunStore, bindings: Mapping[str, MeetingBinding], owner_role: AgentRole = AgentRole.DEVELOPER,
        invoke_timeout_seconds: float = 180,
    ) -> None:
        if not bindings or not math.isfinite(invoke_timeout_seconds) or invoke_timeout_seconds <= 0:
            raise ValueError("Managed meetings require explicit workflow bindings and a finite positive timeout")
        self._runtime = runtime_for
        self.store = FileMeetingStore(state_dir / "receipts")
        self._runs = runs
        self._bindings = {key: MeetingBinding.model_validate(value.model_dump()) for key, value in bindings.items()}
        self._role = owner_role
        self._timeout = invoke_timeout_seconds

    @property
    def operations(self) -> Mapping[str, ManagedOperation]:
        return {"meeting_resolve_blocker": ManagedOperation(self.resolve, role=self._role, on_response=self.approve)}

    def _pins(self, context: ManagedTaskContext, binding: MeetingBinding) -> dict[str, Any]:
        assignment = context.revalidate()
        context.remaining_seconds()
        team = next(team for team in context.snapshot.definition.teams if team.id == assignment.team_id)
        if not set(binding.participants) <= set(team.members):
            raise PermissionError("Meeting participants must belong to the pinned task team")
        return {"run_id": context.run.run_id, "assignment_id": assignment.assignment_id,
                "preview_id": assignment.preview_id, "revision": assignment.revision,
                "scope_digest": assignment.scope_digest, "bundle_content": assignment.bundle_content,
                "owner": assignment.agent_id, "workflow_id": context.run.workflow_id,
                "binding_revision": context.run.binding_revision, "budget_path": assignment.budget_path,
                "deadline": json.loads(Path(assignment.budget_path).read_text(encoding="utf-8"))["deadline"],
                "invoke_timeout_seconds": self._timeout}

    def _check(self, context: ManagedTaskContext, receipt: MeetingReceipt) -> None:
        binding = self._bindings.get(context.run.workflow_id)
        if binding is None or receipt.binding != binding or receipt.pins != self._pins(context, binding):
            raise PermissionError("Meeting scope, bindings, participants or original deadline changed")

    def _approval(self, receipt: MeetingReceipt) -> dict[str, Any]:
        data = receipt.model_dump(mode="json", exclude={"state", "decision", "error"})
        return {"meeting": data, "digest": sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}

    def _agents(self, context: ManagedTaskContext, binding: MeetingBinding) -> dict[str, Agent]:
        runtime = self._runtime(context.snapshot)
        if runtime.snapshot != context.snapshot:
            raise PermissionError("Meeting requires the pinned native runtime")
        templates = {spec.role: spec.instructions for spec in default_agent_specs()}
        agents = {}
        for identifier in binding.participants:
            configured = next(agent for agent in context.snapshot.definition.agents if agent.id == identifier)
            source = runtime.agents[identifier]
            if configured.tool_profile is not None or runtime.modes[identifier] == "mock" or not isinstance(source, Agent):
                raise PermissionError("Meeting participants require native models without persistent tool profiles")
            agents[identifier] = Agent(
                client=source.client, name=identifier, tools=[],
                instructions=templates[configured.role] + "\nConfigured guidance:\n" + configured.instructions +
                "\nThis is read-only blocker discussion. No commands, filesystem, GitHub, implementation or approval "
                "tools are available. Propose a plan within the unchanged approved task; do not claim execution.",
                default_options={"store": False, "max_tokens": 2048},
            )
        return agents

    def _transcript(self, messages: list[Message]) -> tuple[MeetingMessage, ...]:
        if any(message.role not in {"user", "assistant"} or not message.text.strip() or
               any(content.type != "text" for content in message.contents) for message in messages):
            raise PermissionError("Meeting accepts plain discussion only, not tools, native input or hidden content")
        return tuple(MeetingMessage(author="task" if message.role == "user" else message.author_name or "",
                                    text=message.text) for message in messages)

    async def resolve(self, context: ManagedTaskContext, value: Any) -> WorkflowInput:
        binding = self._bindings.get(context.run.workflow_id)
        if binding is None:
            raise PermissionError("Meeting workflow has no explicit operator binding")
        request = BlockerRequest.model_validate(value)
        with self.store.invocation_lock(context.run.run_id):
            saved = self.store.get(context.run.run_id)
            if saved is not None:
                self._check(context, saved)
                raise PermissionError("Saved meeting invocations cannot replay; use their exact idle approval")
            receipt = MeetingReceipt(meeting_id="meeting-" + uuid4().hex, pins=self._pins(context, binding),
                                     request=request, binding=binding, nonce=uuid4().hex, state="blocked")
            self.store.save(receipt)
            try:
                agents = self._agents(context, binding)
                prompt = "Resolve this blocker without changing approval, criteria, paths or budget.\n" + request.model_dump_json() + \
                         "\nAuthoritative approved task:\n" + context.assignment.bundle_content
                if len(prompt.encode()) > min(MAX_PROMPT_BYTES, binding.limits.max_transcript_bytes):
                    raise ValueError("Meeting input exceeds its byte ceiling")
                receipt = receipt.model_copy(update={"state": "running"})
                self.store.save(receipt)

                def capture(messages: list[Message]) -> bool:
                    nonlocal receipt
                    self._check(context, receipt)
                    receipt = MeetingReceipt.model_validate(receipt.model_dump() | {
                        "transcript": self._transcript(messages)})
                    self.store.save(receipt)
                    return False

                def select(state: GroupChatState) -> str:
                    self._check(context, receipt)
                    return list(state.participants)[state.current_round % len(binding.participants)]

                workflow = GroupChatBuilder(participants=list(agents.values()), selection_func=select,
                                            termination_condition=capture,
                                            max_rounds=binding.limits.max_rounds).build()
                async with asyncio.timeout(min(self._timeout, context.remaining_seconds())):
                    result = await workflow.run(prompt)
                    if result.get_final_state() != WorkflowRunState.IDLE:
                        raise PermissionError("Meeting cannot accept native input requests or incomplete execution")
                    outputs = result.get_outputs()
                    if len(outputs) != 1 or not isinstance(outputs[0], AgentResponse) or \
                            len(receipt.transcript) != binding.limits.max_rounds + 1:
                        raise ValueError("Native meeting requires terminal completion and a complete saved conversation")
                    self._check(context, receipt)
                    response = await agents[binding.resolver].run(
                        "Return only JSON: {outcome: continue|scope_change|escalate, rationale: string, plan: string}. "
                        "This is a proposal, never approval. Scope change or unresolved blockers cannot resume this task.\n" +
                        json.dumps([item.model_dump() for item in receipt.transcript]),
                    )
                    final = response.messages[-1] if response.messages else None
                    if response.user_input_requests or final is None or final.role != "assistant":
                        raise PermissionError("Meeting resolver requires final assistant JSON without native input")
                    resolution = MeetingResolution.model_validate_json(final.text)
                self._check(context, receipt)
                receipt = MeetingReceipt.model_validate(receipt.model_dump() | {
                    "resolution": resolution, "state": "proposed" if resolution.outcome == "continue" else "escalated"})
                self.store.save(receipt)
                if receipt.state == "escalated":
                    raise PermissionError("Meeting requires human escalation or a newly approved scope: " + resolution.rationale)
                return WorkflowInput("Approve the exact saved blocker-resolution plan within unchanged scope",
                                     self._approval(receipt), "service_approval")
            except BaseException as error:
                saved = self.store.get(context.run.run_id)
                if saved is not None and saved.state in {"blocked", "running"}:
                    self.store.save(saved.model_copy(update={
                        "state": "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                        "error": (str(error) or type(error).__name__)[:2000]}))
                raise

    def approve(self, context: ManagedTaskContext, original: Any, approved: Any) -> dict[str, Any]:
        with self.store.invocation_lock(context.run.run_id):
            receipt = self.store.get(context.run.run_id)
            if receipt is None or receipt.state != "proposed" or self._approval(receipt) != original:
                raise PermissionError("Meeting continuation requires its exact saved proposal")
            self._check(context, receipt)
            pending = [item for item in context.run.pending if item.kind == "service_approval" and item.data == original]
            recorded = self._runs.get(context.assignment.assignment_id)
            decisions = [] if recorded is None else [decision for decision in recorded.decisions if
                len(pending) == 1 and decision.request_id == pending[0].request_id and
                decision.kind == "service_approval" and decision.value == approved and
                decision.data_digest == workflow_input_digest(original)]
            if type(approved) is not bool or len(decisions) != 1 or recorded is None or recorded.run_id != context.run.run_id:
                raise PermissionError("Meeting approval must already be consumed by the trusted managed runner")
            self.store.save(receipt.model_copy(update={"state": "resolved" if approved else "rejected", "decision": decisions[0]}))
            if not approved:
                raise PermissionError("Operator rejected the meeting resolution")
            return {"meeting_id": receipt.meeting_id, "state": "resolved", "plan": receipt.resolution.plan if receipt.resolution else ""}

    def continuation(self, context: ManagedTaskContext) -> str:
        if context.run.workflow_id not in self._bindings:
            return ""
        receipt = self.store.get(context.run.run_id)
        if receipt is None or receipt.state != "resolved" or receipt.decision is None or receipt.resolution is None:
            raise PermissionError("Developer continuation requires an approved resolved meeting")
        self._check(context, receipt)
        recorded = self._runs.get(context.assignment.assignment_id)
        if recorded is None or receipt.decision not in recorded.decisions or \
            receipt.decision.data_digest != workflow_input_digest(self._approval(receipt)):
            raise PermissionError("Meeting continuation lost its consumed operator decision")
        return "Operator-approved meeting plan (does not change authoritative task scope):\n" + receipt.resolution.model_dump_json()

    async def cleanup(self, context: ManagedTaskContext) -> bool:
        receipt = self.store.get(context.run.run_id)
        if receipt is not None and receipt.state in {"blocked", "running", "proposed"}:
            self.store.save(receipt.model_copy(update={"state": "cancelled", "error": "Managed task ended before resolution approval"}))
        return True
