"""Model content-filter refusals (#84): recognise them, retry the call once, then fail with a clear error.

Azure OpenAI refuses a call by its content filter in three shapes:

* prompt side, as an error body: HTTP 400 ``{"error": {"code": "content_filter", "param": "prompt",
  "innererror": {"content_filter_result": {"sexual": {"filtered": true, ...}}}}}``;
* response side, as a chat completion returned with HTTP 400 (the 071847 shape): ``choices[i].finish_reason ==
  "content_filter"`` and ``choices[i].content_filter_results.error.message == "Response content blocked by label
  'MultiSeverity_SexualScore'."``, with ``prompt_filter_results`` empty;
* response side, as an ordinary HTTP 200 completion whose ``finish_reason`` is ``content_filter``.

The retry lives in the model-call layer (the ``AsyncOpenAI`` client's ``request``), next to openai's own transport
retry, so the agent, workflow and run records see one call. Retrying once was Vertti's decision; a second refusal
raises :class:`ModelContentFilterError`, which the run records through the existing ``failed`` path as its error
string. Any other 400 is raised unchanged and is not retried.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI, BadRequestError

logger = logging.getLogger(__name__)

CONTENT_FILTER = "content_filter"
CONTENT_FILTER_RETRIES = 1
_LABEL = re.compile(r"label '([^']+)'")


@dataclass(frozen=True, slots=True)
class ContentFilterRefusal:
    """Which side the filter blocked (``prompt``, ``response`` or both) and the category labels it named."""

    prompt: bool
    response: bool
    labels: tuple[str, ...] = ()

    @property
    def side(self) -> str:
        if self.prompt and self.response:
            return "prompt and response"
        return "prompt" if self.prompt else "response" if self.response else "unknown side"

    def describe(self) -> str:
        what = "input" if self.prompt and not self.response else "output" if self.response and not self.prompt \
            else "call"
        text = f"Model {what} blocked by content filter ({self.side}"
        if self.labels:
            text += "; " + ", ".join(f"label '{label}'" for label in self.labels)
        return text + ")"


class ModelContentFilterError(Exception):
    """A model call the content filter refused again after its one retry."""

    def __init__(self, refusal: ContentFilterRefusal, *, attempts: int) -> None:
        self.refusal = refusal
        self.attempts = attempts
        super().__init__(f"{refusal.describe()} after {attempts} attempts")


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump()
        return dumped if isinstance(dumped, Mapping) else None
    return None


def _filter_labels(results: Any) -> tuple[list[str], bool]:
    """Labels named by one ``content_filter_results``-style mapping and whether it blocked anything."""
    results = _as_mapping(results)
    if not results:
        return [], False
    labels: list[str] = []
    blocked = False
    for key, value in results.items():
        item = _as_mapping(value)
        if item is None:
            continue
        if key == "error":
            blocked = blocked or item.get("code") == CONTENT_FILTER
            labels.extend(_LABEL.findall(str(item.get("message") or "")))
        elif item.get("filtered") is True:
            blocked = True
            labels.append(str(key))
    return labels, blocked


def _items(value: Any) -> Iterable[Any]:
    return value if isinstance(value, list | tuple) else ()


def classify_content_filter(body: Any) -> ContentFilterRefusal | None:
    """The content-filter refusal in a model response or error body, or None when it is not one."""
    data = _as_mapping(body)
    if data is None:
        return None
    if isinstance(data.get("error"), Mapping) and "choices" not in data:
        data = data["error"]
    labels: list[str] = []
    prompt = response = False
    for choice in _items(data.get("choices")):
        choice = _as_mapping(choice) or {}
        found, blocked = _filter_labels(choice.get("content_filter_results"))
        labels.extend(found)
        response = response or blocked or choice.get("finish_reason") == CONTENT_FILTER
    for entry in _items(data.get("prompt_filter_results")):
        entry = _as_mapping(entry) or {}
        found, blocked = _filter_labels(entry.get("content_filter_results"))
        labels.extend(found)
        prompt = prompt or blocked
    if data.get("code") == CONTENT_FILTER:
        inner = _as_mapping(data.get("innererror")) or {}
        found, _ = _filter_labels(inner.get("content_filter_result"))
        labels.extend(found)
        labels.extend(_LABEL.findall(str(data.get("message") or "")))
        if not response:
            prompt = True  # the error-format refusal is Azure's prompt-side shape
    if not (prompt or response):
        return None
    return ContentFilterRefusal(prompt=prompt, response=response, labels=tuple(dict.fromkeys(labels)))


def content_filter_refusal(error: BaseException) -> ContentFilterRefusal | None:
    """The refusal carried by a 400 ``BadRequestError``; None for every other error, including other 400s."""
    if isinstance(error, ModelContentFilterError):
        return error.refusal
    if not isinstance(error, BadRequestError):
        return None
    refusal = classify_content_filter(error.body)
    if refusal is None and error.code == CONTENT_FILTER:
        refusal = ContentFilterRefusal(prompt=True, response=False, labels=tuple(_LABEL.findall(error.message)))
    return refusal


class _Stats:
    """Process-wide counts for diagnostics; not part of any record."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {"refusals": 0, "retries": 0, "recovered": 0, "failed": 0}

    def add(self, key: str) -> None:
        with self._lock:
            self._counts[key] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        with self._lock:
            for key in self._counts:
                self._counts[key] = 0


CONTENT_FILTER_STATS = _Stats()


def _completion_refusal(result: Any) -> ContentFilterRefusal | None:
    """A successful (HTTP 200) chat completion that the filter cut off; streams are not inspected."""
    choices = getattr(result, "choices", None)
    if not isinstance(choices, list) or not any(getattr(c, "finish_reason", None) == CONTENT_FILTER for c in choices):
        return None
    return classify_content_filter(result) or ContentFilterRefusal(prompt=False, response=True)


class ContentFilterRetryMixin:
    """Retry a content-filter refusal exactly once at the ``AsyncOpenAI.request`` layer (#84)."""

    async def request(self, cast_to: Any, options: Any, *, stream: bool = False,
                      stream_cls: Any = None) -> Any:
        parent: Any = super()
        refusal: ContentFilterRefusal | None = None
        attempt = 0
        while True:
            attempt += 1
            # openai copies the options per call, so the retry sends the same request with a fresh idempotency key.
            try:
                result = await parent.request(cast_to, options, stream=stream, stream_cls=stream_cls)
            except BadRequestError as error:
                refusal = content_filter_refusal(error)
                if refusal is None:
                    raise
                cause: BaseException | None = error
            else:
                refusal = None if stream else _completion_refusal(result)
                if refusal is None:
                    if attempt > 1:
                        CONTENT_FILTER_STATS.add("recovered")
                        logger.warning("Model content-filter retry succeeded on attempt %d", attempt)
                    return result
                cause = None
            CONTENT_FILTER_STATS.add("refusals")
            if attempt > CONTENT_FILTER_RETRIES:
                CONTENT_FILTER_STATS.add("failed")
                logger.warning("%s; not retrying again", refusal.describe())
                raise ModelContentFilterError(refusal, attempts=attempt) from cause
            CONTENT_FILTER_STATS.add("retries")
            logger.warning("%s; retrying the model call once", refusal.describe())


_RETRYING: dict[type[Any], type[Any]] = {}


def content_filter_retrying(client_class: type[AsyncOpenAI]) -> type[AsyncOpenAI]:
    """``client_class`` with the one content-filter retry layered on its ``request`` (cached per class).

    Taking the class as an argument keeps a caller-supplied ``AsyncOpenAI`` subclass (for example a diagnostics
    wrapper) underneath, so it still sees every attempt.
    """
    retrying = _RETRYING.get(client_class)
    if retrying is None:
        retrying = type(f"ContentFilterRetrying{client_class.__name__}", (ContentFilterRetryMixin, client_class), {})
        _RETRYING[client_class] = retrying
    return retrying
