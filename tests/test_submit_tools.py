from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient
import httpx
from openai import AsyncOpenAI
from pydantic import ValidationError
import pytest

from aitobuild.organization import parse_organization_definition
from aitobuild.organization_assignments import AssignmentProposal
from aitobuild.submit_tools import (
    build_submit_tool,
    ensure_framework_error_limit,
    run_with_reminder,
)


def _response(message):
    finish = "tool_calls" if message.get("tool_calls") else "stop"
    return httpx.Response(
        200,
        json={
            "id": "r",
            "object": "chat.completion",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        },
    )


def _call(index, arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"c{index}",
                "type": "function",
                "function": {
                    "name": "submit",
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                },
            }
        ],
    }


def run_script(script, *, max_attempts=3, configured_limit=None, extra_check=None):
    """Scripted model: each entry is submit-call arguments, or a plain string prefixed 'text:'."""
    bodies = []

    def reply(request):
        bodies.append(json.loads(request.content))
        index = len(bodies) - 1
        item = script[index] if index < len(script) else "text:done"
        if isinstance(item, str) and item.startswith("text:"):
            return _response({"role": "assistant", "content": item[5:]})
        return _response(_call(index, item))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as transport:
            async with AsyncOpenAI(api_key="k", http_client=transport) as openai:
                configuration = (
                    {}
                    if configured_limit is None
                    else {"max_consecutive_errors_per_request": configured_limit}
                )
                client = OpenAIChatCompletionClient(
                    model="m", async_client=openai, function_invocation_configuration=configuration
                )
                agent = Agent(client=client)
                session = agent.create_session()
                submit = build_submit_tool(
                    "submit",
                    AssignmentProposal,
                    session,
                    "result",
                    extra_check,
                    max_attempts=max_attempts,
                    description="Submit",
                )
                ensure_framework_error_limit(client, max_attempts)

                async def turn(message):
                    await agent.run(
                        message,
                        session=session,
                        tools=(submit.tool,),
                        middleware=[submit.middleware],
                        options={"store": False},
                    )

                try:
                    return await run_with_reminder(submit, "go", turn, lambda: None), submit, client
                except Exception as error:
                    return error, submit, client

    result, submit, client = asyncio.run(exercise())
    return result, submit, client, bodies


VALID = {"agent_id": "developer_one", "rationale": "Fits"}


def test_valid_submission_is_stored_once():
    result, submit, _, bodies = run_script([VALID])
    assert (
        result == AssignmentProposal(**VALID) and submit.submitted == VALID and submit.attempts == 0
    )
    assert len(bodies) == 2


def test_invalid_json_and_schema_errors_use_attempts_and_allow_retry():
    result, submit, _, bodies = run_script(
        ["{not json", {"agent_id": "Bad!", "rationale": "x"}, VALID]
    )
    assert result == AssignmentProposal(**VALID) and submit.attempts == 2
    assert [m["content"] for m in bodies[-1]["messages"] if m["role"] == "tool"][:2] == [
        "Error: Argument parsing failed.",
        "Error: Argument parsing failed.",
    ]


def test_attempts_exhaust_fail_closed_even_with_valid_followup():
    result, submit, _, bodies = run_script(["{}", "{}", "{}", VALID])
    assert isinstance(result, PermissionError) and "attempts" in str(result)
    assert submit.submitted is None and submit.attempts == 3 and len(bodies) == 3


def test_extra_check_rejections_count_and_different_resubmission_is_refused():
    def check(value):
        if value.agent_id == "outside":
            raise PermissionError("ineligible")

    other = {"agent_id": "developer_two", "rationale": "Changed"}
    result, submit, _, bodies = run_script(
        [{"agent_id": "outside", "rationale": "x"}, VALID, other, VALID], extra_check=check
    )
    # Only the valid submission is write-once; a later different body is refused and uses an attempt.
    assert (
        result == AssignmentProposal(**VALID) and submit.submitted == VALID and submit.attempts == 2
    )
    tool_results = [m["content"] for m in bodies[-1]["messages"] if m["role"] == "tool"]
    assert (
        tool_results[0].startswith("Error")
        and "submitted" in tool_results[1]
        and tool_results[2].startswith("Error")
    )
    assert "submitted" in tool_results[3]


def test_reminder_turn_runs_once_in_the_same_session_and_shares_the_attempt_count():
    result, submit, _, bodies = run_script(["text:" + json.dumps(VALID), "{}", VALID])
    assert result == AssignmentProposal(**VALID) and submit.attempts == 1
    users = [m["content"] for m in bodies[-1]["messages"] if m["role"] == "user"]
    assert users[0] == "go" and "submit" in users[1] and len(users) == 2

    result, submit, _, bodies = run_script(["text:no", "text:still no"])
    assert isinstance(result, ValueError) and submit.submitted is None and len(bodies) == 2


def test_attempts_above_framework_default_raise_its_consecutive_error_limit():
    result, submit, client, bodies = run_script(["{}", "{}", "{}", "{}", VALID], max_attempts=5)
    assert result == AssignmentProposal(**VALID) and submit.attempts == 4
    assert client.function_invocation_configuration["max_consecutive_errors_per_request"] == 5
    _, _, client, _ = run_script([VALID], max_attempts=2, configured_limit=7)
    assert client.function_invocation_configuration["max_consecutive_errors_per_request"] == 7


@pytest.mark.parametrize("value", [0, 11, -1, "3", 2.0, True])
def test_max_submit_attempts_fails_closed_on_definition_load(value):
    document = json.loads(
        (Path(__file__).resolve().parents[1] / "config/organization.example.json").read_text()
    )
    assert parse_organization_definition(json.dumps(document)).agents[0].max_submit_attempts == 3
    document["agents"][0]["max_submit_attempts"] = value
    with pytest.raises(ValidationError):
        parse_organization_definition(json.dumps(document))
    with pytest.raises(ValueError):
        build_submit_tool(
            "submit",
            AssignmentProposal,
            _Session(),
            "k",  # type: ignore[arg-type]
            max_attempts=value,
            description="d",
        )


class _Session:
    state: dict = {}
