"""Agent Framework capability audit matrix for overlap prevention."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CapabilityDecision(StrEnum):
    REUSE_NATIVE = "reuse_native"
    WRAP_NATIVE = "wrap_native"
    CUSTOM_BUILD = "custom_build"


@dataclass(slots=True, frozen=True)
class CapabilityAuditItem:
    capability: str
    decision: CapabilityDecision
    rationale: str


def phase1_capability_matrix() -> tuple[CapabilityAuditItem, ...]:
    return (
        CapabilityAuditItem(
            capability="agent_roles_and_instructions",
            decision=CapabilityDecision.REUSE_NATIVE,
            rationale="Use Agent primitive with role-specific instructions.",
        ),
        CapabilityAuditItem(
            capability="chat_client_transport",
            decision=CapabilityDecision.REUSE_NATIVE,
            rationale="Use FoundryChatClient as the primary model transport.",
        ),
        CapabilityAuditItem(
            capability="meeting_bootstrap",
            decision=CapabilityDecision.WRAP_NATIVE,
            rationale="Wrap GroupChatBuilder with project-specific meeting metadata.",
        ),
        CapabilityAuditItem(
            capability="webhook_normalization",
            decision=CapabilityDecision.CUSTOM_BUILD,
            rationale="Project-specific payload normalization and policy metadata.",
        ),
        CapabilityAuditItem(
            capability="trigger_deduplication",
            decision=CapabilityDecision.CUSTOM_BUILD,
            rationale="Project-specific idempotency behavior for retries and scheduler events.",
        ),
        CapabilityAuditItem(
            capability="developer_durable_memory",
            decision=CapabilityDecision.REUSE_NATIVE,
            rationale="Use FileMemoryProvider with session-scoped native file storage.",
        ),
        CapabilityAuditItem(
            capability="developer_session_persistence",
            decision=CapabilityDecision.WRAP_NATIVE,
            rationale="Use FileSessionStore and FileHistoryProvider for native Developer sessions.",
        ),
        CapabilityAuditItem(
            capability="developer_browser",
            decision=CapabilityDecision.WRAP_NATIVE,
            rationale="Use MCPStdioTool with Microsoft Playwright MCP in the private Developer container.",
        ),
        CapabilityAuditItem(
            capability="developer_workspace_provisioning",
            decision=CapabilityDecision.CUSTOM_BUILD,
            rationale="Project-specific seed-once checkouts and per-Developer Docker volume subdirectories.",
        ),
        CapabilityAuditItem(
            capability="architect_pr_review_tools",
            decision=CapabilityDecision.CUSTOM_BUILD,
            rationale="Architect GitHub PR get/review tools with role-gated PR_REVIEW policy.",
        ),
        CapabilityAuditItem(
            capability="architect_memory",
            decision=CapabilityDecision.CUSTOM_BUILD,
            rationale="Architect-specific durable JSONL memory query/record tools separate from Developer FileMemoryProvider.",
        ),
        CapabilityAuditItem(
            capability="pm_backlog_tools",
            decision=CapabilityDecision.CUSTOM_BUILD,
            rationale="PM backlog/issue tools with human-approval gates over mock or gh CLI GitHub adapters.",
        ),
        CapabilityAuditItem(
            capability="shared_web_search",
            decision=CapabilityDecision.CUSTOM_BUILD,
            rationale="Cheap shared web_search snippet adapter; browser MCP remains optional and heavier.",
        ),
    )


def ensure_decision_exists(capability: str) -> CapabilityDecision:
    for item in phase1_capability_matrix():
        if item.capability == capability:
            return item.decision
    raise KeyError(f"Capability '{capability}' is missing from phase1 capability matrix")
