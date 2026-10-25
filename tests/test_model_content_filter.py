"""#84: content-filter refusals are their own model error and the model call is retried exactly once."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_framework.openai import OpenAIChatCompletionClient
import httpx
from openai import AsyncOpenAI, BadRequestError
import pytest

from aitobuild.model_content_filter import (
    CONTENT_FILTER_STATS, ModelContentFilterError, classify_content_filter, content_filter_refusal,
    content_filter_retrying,
)
from aitobuild.organization_runner import FileRunStore, ManagedOperation, _failure_label
from test_organization_runner import runner_fixture

# The 071847 Architect review refusal, verbatim apart from the id: output side, empty prompt_filter_results.
BODY_071847: dict[str, Any] = {
    "id": "ab0b2bd5-ec2a-4a6f-9996-ae72a045f68e", "model": "grok-4.6",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "content_filter",
                 "content_filter_results": {"error": {
                     "code": "content_filter",
                     "message": "Response content blocked by label 'MultiSeverity_SexualScore'."}}}],
    "usage": {"prompt_tokens": 5276, "completion_tokens": 155, "total_tokens": 5431},
    "created": 1792384068, "object": "chat.completion", "prompt_filter_results": None,
}
# Azure's prompt-side refusal: an error body with the filter categories in innererror.
BODY_PROMPT: dict[str, Any] = {"error": {
    "code": "content_filter", "param": "prompt", "status": 400,
    "message": "The response was filtered due to the prompt triggering Azure OpenAI's content management policy.",
    "innererror": {"code": "ResponsibleAIPolicyViolation", "content_filter_result": {
        "hate": {"filtered": False, "severity": "safe"}, "jailbreak": {"filtered": True, "detected": True},
        "sexual": {"filtered": False, "severity": "safe"}, "violence": {"filtered": True, "severity": "medium"}}},
}}
BODY_PROMPT_RESULTS: dict[str, Any] = {
    "object": "chat.completion", "choices": [],
    "prompt_filter_results": [{"prompt_index": 0, "content_filter_results": {
        "self_harm": {"filtered": True, "severity": "high"}, "hate": {"filtered": False, "severity": "safe"}}}],
}
BODY_OTHER_400: dict[str, Any] = {"error": {
    "code": "context_length_exceeded", "param": "messages", "type": "invalid_request_error",
    "message": "This model's maximum context length is 128000 tokens."}}


def _ok(content: str = "fine", finish_reason: str = "stop", **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "ok", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": finish_reason, **extra}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def _filtered_200() -> httpx.Response:
    return _ok("", "content_filter", content_filter_results={
        "sexual": {"filtered": True, "severity": "medium"}, "hate": {"filtered": False, "severity": "safe"}})


def _bad_request(body: dict[str, Any]) -> BadRequestError:
    request = httpx.Request("POST", "https://example.invalid/openai/v1/chat/completions")
    data = body.get("error", body)
    return BadRequestError(f"Error code: 400 - {body}", response=httpx.Response(400, json=body, request=request),
                           body=data)


@pytest.fixture(autouse=True)
def _fresh_stats():
    CONTENT_FILTER_STATS.reset()
    yield
    CONTENT_FILTER_STATS.reset()


# Classification ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("prompt_results", [None, []], ids=["null", "empty-list"])
def test_071847_shape_is_an_output_side_refusal_with_its_label(prompt_results):
    body = dict(BODY_071847, prompt_filter_results=prompt_results)
    assert body["usage"]["completion_tokens"] == 155
    refusal = classify_content_filter(body)
    assert refusal is not None and refusal.response and not refusal.prompt
    assert refusal.labels == ("MultiSeverity_SexualScore",)
    assert refusal.describe() == "Model output blocked by content filter (response; label 'MultiSeverity_SexualScore')"


def test_prompt_side_error_body_is_an_input_refusal_with_filtered_categories():
    refusal = content_filter_refusal(_bad_request(BODY_PROMPT))
    assert refusal is not None and refusal.prompt and not refusal.response
    assert refusal.labels == ("jailbreak", "violence")
    assert refusal.describe().startswith("Model input blocked by content filter (prompt; label 'jailbreak'")


def test_prompt_filter_results_mark_the_input_side():
    refusal = classify_content_filter(BODY_PROMPT_RESULTS)
    assert refusal is not None and refusal.prompt and not refusal.response and refusal.labels == ("self_harm",)


def test_unfiltered_prompt_results_and_other_400s_are_not_refusals():
    clean = dict(BODY_071847, choices=[{"index": 0, "finish_reason": "stop", "content_filter_results": {
        "sexual": {"filtered": False, "severity": "safe"}}}],
        prompt_filter_results=[{"prompt_index": 0, "content_filter_results": {"hate": {"filtered": False}}}])
    assert classify_content_filter(clean) is None
    assert content_filter_refusal(_bad_request(BODY_OTHER_400)) is None
    assert content_filter_refusal(ValueError("content_filter")) is None


# The model-call layer: real OpenAIChatCompletionClient over a mocked HTTP transport ----------------------------


async def _call(replies: list[httpx.Response]) -> tuple[Any, int]:
    calls = 0

    def reply(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return replies[min(calls, len(replies)) - 1]

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        async with content_filter_retrying(AsyncOpenAI)(api_key="k", http_client=http, max_retries=1) as openai:
            client = OpenAIChatCompletionClient(model="m", async_client=openai)
            try:
                return await client.get_response("hello"), calls
            except Exception as error:  # noqa: BLE001 - the test inspects the raised chain
                return error, calls


def test_refusal_is_retried_once_and_the_retry_succeeds(caplog):
    caplog.set_level(logging.WARNING, "aitobuild.model_content_filter")
    response, calls = asyncio.run(_call([httpx.Response(400, json=BODY_071847), _ok("reviewed")]))
    assert calls == 2 and response.text == "reviewed"
    assert CONTENT_FILTER_STATS.snapshot() == {"refusals": 1, "retries": 1, "recovered": 1, "failed": 0}
    assert "retrying the model call once" in caplog.text


@pytest.mark.parametrize("second", ["400", "200"])
def test_refusal_is_retried_once_then_fails_with_the_content_filter_error(second):
    retry = httpx.Response(400, json=BODY_071847) if second == "400" else _filtered_200()
    error, calls = asyncio.run(_call([httpx.Response(400, json=BODY_071847), retry]))
    assert calls == 2
    assert isinstance(error, Exception) and not isinstance(error, ModelContentFilterError)  # framework wrapper
    label = _failure_label(error)
    expected = "label 'MultiSeverity_SexualScore'" if second == "400" else "label 'sexual'"
    assert label.startswith(f"Model output blocked by content filter (response; {expected}) after 2 attempts")
    assert "Error code: 400" not in label and "<- aitobuild.ModelContentFilterError" in label
    assert CONTENT_FILTER_STATS.snapshot() == {"refusals": 2, "retries": 1, "recovered": 0, "failed": 1}


def test_http_200_completion_cut_by_the_filter_is_retried_once():
    response, calls = asyncio.run(_call([_filtered_200(), _ok("second try")]))
    assert calls == 2 and response.text == "second try"
    assert CONTENT_FILTER_STATS.snapshot()["recovered"] == 1


def test_prompt_side_refusal_is_retried_once_then_fails_as_input_blocked():
    error, calls = asyncio.run(_call([httpx.Response(400, json=BODY_PROMPT)]))
    assert calls == 2
    assert _failure_label(error).startswith("Model input blocked by content filter (prompt; label 'jailbreak', "
                                            "label 'violence') after 2 attempts")


def test_non_filter_400_is_not_retried_and_keeps_the_generic_label():
    error, calls = asyncio.run(_call([httpx.Response(400, json=BODY_OTHER_400), _ok()]))
    assert calls == 1
    label = _failure_label(error)
    assert "context_length_exceeded" in label and "content filter" not in label
    assert label.endswith("[exception chain: agent_framework.ChatClientException <- openai.BadRequestError]")
    assert CONTENT_FILTER_STATS.snapshot() == {"refusals": 0, "retries": 0, "recovered": 0, "failed": 0}


def test_unwrapped_client_refusal_still_gets_the_clear_label():
    """Without the retry layer (for example another transport), the 071847 400 is still labelled clearly."""

    async def call() -> Exception:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json=BODY_071847))) as http:
            async with AsyncOpenAI(api_key="k", http_client=http, max_retries=0) as openai:
                try:
                    await OpenAIChatCompletionClient(model="m", async_client=openai).get_response("hello")
                except Exception as error:  # noqa: BLE001
                    return error
        raise AssertionError("expected a refusal")

    label = _failure_label(asyncio.run(call()))
    assert label == ("Model output blocked by content filter (response; label 'MultiSeverity_SexualScore') "
                     "[exception chain: agent_framework.ChatClientException <- openai.BadRequestError]")


def test_retrying_class_keeps_a_caller_subclass_underneath():
    seen: list[str] = []

    class Diag(AsyncOpenAI):
        async def request(self, cast_to, options, *, stream=False, stream_cls=None):  # type: ignore[override]
            seen.append(options.url)
            return await super().request(cast_to, options, stream=stream, stream_cls=stream_cls)

    retrying = content_filter_retrying(Diag)
    assert issubclass(retrying, Diag) and content_filter_retrying(Diag) is retrying

    async def call() -> int:
        replies = iter([httpx.Response(400, json=BODY_071847), _ok()])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(replies))) as http:
            async with retrying(api_key="k", http_client=http, max_retries=0) as openai:
                await OpenAIChatCompletionClient(model="m", async_client=openai).get_response("hello")
        return len(seen)

    assert asyncio.run(call()) == 2


# The run record: existing failed path, error stays a string ----------------------------------------------------


def test_071847_review_refused_twice_fails_the_run_with_the_content_filter_error(tmp_path):
    async def call_model(context, message):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json=BODY_071847))) as http:
            async with content_filter_retrying(AsyncOpenAI)(api_key="k", http_client=http, max_retries=1) as openai:
                await OpenAIChatCompletionClient(model="m", async_client=openai).get_response("review")
        return message

    runner, assignment, *_ = runner_fixture(tmp_path, {"definition_probe": ManagedOperation(call_model)})
    failed = asyncio.run(runner.start(assignment.assignment_id, input="hello"))
    assert failed.state == "failed" and isinstance(failed.error, str)
    assert failed.error == (
        "Model output blocked by content filter (response; label 'MultiSeverity_SexualScore') after 2 attempts "
        "[exception chain: agent_framework.ChatClientException <- aitobuild.ModelContentFilterError "
        "<- openai.BadRequestError]")
    saved = FileRunStore(tmp_path / "runs").get(assignment.assignment_id)
    assert saved is not None and saved.error == failed.error and saved.state == "failed"
