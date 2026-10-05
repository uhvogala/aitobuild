"""Cross-role shared tools: web_search and request_meeting."""

from __future__ import annotations

from typing import Annotated, Any, Callable

from agent_framework import tool
from pydantic import Field

from aitobuild.meetings import MeetingRegistry
from aitobuild.policy import ActionClass, AgentRole, assert_role_action_allowed
from aitobuild.tools.web_search import MockWebSearchAdapter, WebSearchAdapter


ToolFunc = Callable[..., Any]


def build_web_search_tool(
    *,
    role: AgentRole,
    adapter: WebSearchAdapter | None = None,
) -> ToolFunc:
    search_adapter: WebSearchAdapter = adapter or MockWebSearchAdapter()

    @tool(
        name="web_search",
        approval_mode="always_require",
        description=(
            "Search the public web for concise result snippets. Prefer this cheap default over a "
            "browser for factual lookups. Use an optional browser MCP only when full-page "
            "interaction is required (forms, authenticated pages, dynamic UI)."
        ),
    )
    def web_search(
        query: Annotated[str, Field(description="Search query string.")],
        max_results: Annotated[int, Field(ge=1, le=10, description="Maximum snippets to return.")] = 5,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        results = search_adapter.search(query=query, max_results=max_results)
        return {
            "query": query.strip(),
            "results": [item.to_dict() for item in results],
            "result_count": len(results),
            "guidance": (
                "Snippets are incomplete. Prefer repository tools for project truth. "
                "Escalate to browser MCP only for interactive or full-page needs."
            ),
        }

    return web_search


def build_request_meeting_tool(
    *,
    role: AgentRole,
    meeting_registry: MeetingRegistry | None = None,
) -> ToolFunc:
    registry = meeting_registry or MeetingRegistry()

    @tool(
        name="request_meeting",
        approval_mode="always_require",
        description=(
            "Request a bounded synchronous meeting when async work is blocked. Requires an agenda "
            "and at least two unique participants (pm, architect, developer)."
        ),
    )
    def request_meeting(
        agenda: Annotated[str, Field(description="Meeting agenda / blocker summary.")],
        participants: Annotated[
            list[str],
            Field(description="Participant roles, e.g. ['architect', 'developer']."),
        ],
        deadline: Annotated[
            str | None,
            Field(description="Optional ISO-8601 deadline for the meeting."),
        ] = None,
        meeting_id: Annotated[
            str | None,
            Field(description="Optional stable meeting id; generated when omitted."),
        ] = None,
    ) -> dict[str, Any]:
        assert_role_action_allowed(role, ActionClass.READ_ONLY)
        payload: dict[str, object] = {
            "agenda": agenda,
            "participants": participants,
        }
        if deadline is not None:
            payload["deadline"] = deadline
        if meeting_id is not None:
            payload["meeting_id"] = meeting_id
        record = registry.request_meeting(payload)
        return {
            "meeting_id": record.meeting_id,
            "agenda": record.agenda,
            "participants": list(record.participants),
            "state": record.state.value,
            "deadline": record.deadline.isoformat() if record.deadline else None,
            "requested_by": role.value,
        }

    return request_meeting
