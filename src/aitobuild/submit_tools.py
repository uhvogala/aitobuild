"""Write-once structured submission tools for native agents.

Agents submit their final structured output through a typed tool instead of free text. Invalid calls
(schema or semantic) return a tool error so the model can retry, and each one uses up an attempt. Only a
valid submission is stored, once, in ``session.state``; a later different submission is refused.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from agent_framework import (
    AgentSession,
    FunctionInvocationContext,
    FunctionTool,
    MiddlewareFailure,
    function_middleware,
    tool,
)
from pydantic import BaseModel

from aitobuild.tool_outputs import MAX_TOOL_RESULT_BYTES

DEFAULT_MAX_SUBMIT_ATTEMPTS = 3
# The framework counts consecutive tool errors per client and offers no per-run override, so attempts are
# capped at its default instead of mutating a shared client (see #24 review B2).
MAX_SUBMIT_ATTEMPTS = 3
FRAMEWORK_ERROR_LIMIT_KEY = "max_consecutive_errors_per_request"
FRAMEWORK_DEFAULT_ERROR_LIMIT = 3

ModelT = TypeVar("ModelT", bound=BaseModel)
ExtraCheck = Callable[[Any], Any]


@dataclass(slots=True)
class SubmitTool(Generic[ModelT]):
    """A submit tool plus the middleware that counts its attempts in session state."""

    name: str
    model: type[ModelT]
    session: AgentSession
    key: str
    max_attempts: int
    tool: FunctionTool
    middleware: Any

    @property
    def attempts_key(self) -> str:
        return self.key + "_submit_attempts"

    @property
    def attempts(self) -> int:
        value = self.session.state.get(self.attempts_key, 0)
        return value if type(value) is int else self.max_attempts

    @property
    def remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    @property
    def submitted(self) -> Any:
        return self.session.state.get(self.key)

    def result(self) -> ModelT:
        saved = self.submitted
        if saved is None:
            error = self.session.state.get(self.key + "_submit_error")
            raise ValueError(
                f"Agent did not submit a valid {self.name} result" + (f": {error}" if error else "")
            )
        return self.model.model_validate(saved)


def require_framework_error_limit(client: Any, max_attempts: int) -> None:
    """Fail closed if the client's consecutive tool error limit would silently cap submit attempts.

    Read-only: the shared client's configuration is never changed.
    """
    config = getattr(client, "function_invocation_configuration", None) or {}
    limit = config.get(FRAMEWORK_ERROR_LIMIT_KEY, FRAMEWORK_DEFAULT_ERROR_LIMIT)
    if type(limit) is not int or limit < max_attempts:
        raise PermissionError(
            "Client tool error limit is below max_submit_attempts; lower the setting"
        )


def build_submit_tool(
    name: str,
    model: type[ModelT],
    session: AgentSession,
    key: str,
    extra_check: ExtraCheck | None = None,
    *,
    max_attempts: int = DEFAULT_MAX_SUBMIT_ATTEMPTS,
    description: str,
    persist: Callable[[], Awaitable[None]] | None = None,
) -> SubmitTool[ModelT]:
    """Build a write-once submit tool storing ``model`` JSON at ``session.state[key]``.

    ``extra_check`` receives the validated model and may return a replacement (normalized) model; it may be async.
    It raises to reject the call, which uses up an attempt like a schema error.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_SUBMIT_ATTEMPTS:
        raise ValueError("max_submit_attempts must be a small positive integer")
    if not name.strip() or not key.strip():
        raise ValueError("Submit tools require a name and a session key")

    async def submit(**arguments: Any) -> dict[str, Any]:
        value = model.model_validate(arguments)
        if extra_check is not None:
            checked = extra_check(value)
            if inspect.isawaitable(checked):
                checked = await checked
            if checked is not None:
                value = model.model_validate(
                    checked.model_dump() if isinstance(checked, BaseModel) else checked
                )
        data = value.model_dump(mode="json")
        if len(json.dumps(data).encode("utf-8")) > MAX_TOOL_RESULT_BYTES - 256:
            raise ValueError("Submission must fit inline")
        saved = session.state.get(key)
        if saved is not None and saved != data:
            raise PermissionError("A valid submission was already recorded and cannot be replaced")
        session.state[key] = data
        if persist is not None:
            await persist()
        return {"submitted": True, "write_once": True}

    submit_tool = tool(
        submit,
        name=name,
        description=description,
        schema=model.model_json_schema(),
        approval_mode="never_require",
    )
    attempts_key = key + "_submit_attempts"

    @function_middleware
    async def count_attempts(
        call: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        if call.function.name != name:
            await call_next()
            return
        used = session.state.get(attempts_key, 0)
        if type(used) is not int or used >= max_attempts:
            raise MiddlewareFailure(f"{name} has no submit attempts left")
        try:
            await call_next()
        except Exception as error:
            session.state[attempts_key] = used + 1
            session.state[key + "_submit_error"] = (str(error) or type(error).__name__)[:500]
            if persist is not None:
                await persist()
            if used + 1 >= max_attempts:
                raise MiddlewareFailure(
                    f"{name} used all {max_attempts} submit attempts: {str(error)[:500]}"
                ) from error
            raise
        if session.state.get(key) is None:
            session.state[attempts_key] = used + 1

    return SubmitTool(
        name=name,
        model=model,
        session=session,
        key=key,
        max_attempts=max_attempts,
        tool=submit_tool,
        middleware=count_attempts,
    )


async def run_with_reminder(
    submit: SubmitTool[ModelT],
    message: str,
    run: Callable[[str], Awaitable[Any]],
    revalidate: Callable[[], Any],
    remaining_seconds: Callable[[], float],
) -> ModelT:
    """Run once, then at most one reminder turn in the same session if no valid submission was recorded.

    Callers wrap this in their single invocation timeout and budget/deadline handling, so the reminder never
    creates a second ledger entry, receipt or invocation identity.
    """
    try:
        await run(message)
        if submit.submitted is None and submit.remaining > 0:
            checked = revalidate()
            if inspect.isawaitable(checked):
                await checked
            # The reminder is a model call too: an expired budget deadline must stop it (BudgetDeadlineExpired).
            remaining_seconds()
            await run(reminder_text(submit))
    except MiddlewareFailure as error:
        if submit.submitted is None and submit.remaining == 0:
            raise PermissionError(str(error)) from error
        raise
    return submit.result()


def reminder_text(submit: SubmitTool[Any]) -> str:
    return (
        f"You have not recorded a valid result yet. Call {submit.name} now with the structured result; "
        f"{submit.remaining} attempt(s) remain. Do not reply with free text instead."
    )
