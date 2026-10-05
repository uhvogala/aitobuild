"""Bound model-facing tool results and retain complete session-private artifacts."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Annotated, Any, Awaitable, Callable
from uuid import uuid4

from agent_framework import Content, FunctionInvocationContext, function_middleware, tool
from pydantic import Field

MAX_TOOL_RESULT_BYTES = 6000
MAX_PROMPT_BYTES = 32000


def build_output_guard(directory: Path):
    @function_middleware
    async def bound_output(
        context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]],
    ) -> None:
        oversized_error = None
        try:
            await call_next()
        except Exception as error:
            if len(str(error).encode("utf-8")) <= MAX_TOOL_RESULT_BYTES:
                raise
            oversized_error = error
            context.result = [Content.from_text(json.dumps({"ok": False, "error": str(error)}))]
        contents = context.result
        if getattr(contents, "type", None) == "function_approval_request":
            return
        if isinstance(contents, list):
            if any(getattr(item, "type", None) == "function_approval_request" for item in contents):
                return
            text = "\n".join(getattr(item, "text", "") or "" for item in contents)
            has_media = any(getattr(item, "type", None) not in {"text", "function_result"} for item in contents)
            stored = json.dumps([item.to_dict() for item in contents], default=str) if has_media else text
        else:
            text = str(contents)
            stored = text
            has_media = False
        if len(stored.encode("utf-8")) <= MAX_TOOL_RESULT_BYTES and not has_media:
            return
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_id = uuid4().hex
        (directory / f"{output_id}.txt").write_text(stored, encoding="utf-8")
        try:
            structured = json.loads(text)
        except ValueError:
            structured = None
        summary: dict[str, Any] = {
            "ok": not (isinstance(structured, dict) and structured.get("ok") is False),
            "output_saved": True, "output_id": output_id,
            "total_chars": len(stored),
            "preview": text.encode("utf-8")[:1200].decode("utf-8", errors="ignore"),
            "next_action": "Use developer_read_output with this output_id and byte offsets to retrieve only the needed section. Full output was not sent to the model.",
        }
        if isinstance(structured, dict):
            for key in ("exit_code", "status", "shell_id", "next_cursor", "error_code"):
                if key in structured:
                    summary[key] = structured[key]
        summary_text = json.dumps(summary)
        if oversized_error is not None:
            raise RuntimeError(summary_text) from oversized_error
        context.result = [Content.from_text(summary_text)]

    return bound_output


def build_output_reader(directory: Path):
    @tool(name="developer_read_output", approval_mode="always_require", description=(
        "Read a bounded page from a saved tool output belonging to this Developer. Use only the "
        "output_id returned by an oversized tool result. Start at offset_bytes=0, then reuse "
        "next_offset_bytes if more detail is needed. Never read every page automatically. "
        "The saved file is complete; previews are not the full result."
    ))
    def developer_read_output(
        output_id: Annotated[str, Field(description="Exact 32-character output ID from a saved result.")],
        offset_bytes: Annotated[int, Field(ge=0, description="Byte offset; reuse next_offset_bytes.")] = 0,
        max_bytes: Annotated[int, Field(ge=256, le=4000, description="Page budget, default 2000 bytes.")] = 2000,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", output_id):
            raise ValueError("Use the exact output_id returned by a tool; paths are not accepted")
        if offset_bytes < 0 or not 256 <= max_bytes <= 4000:
            raise ValueError("offset_bytes must be >=0 and max_bytes must be 256..4000")
        path = directory / f"{output_id}.txt"
        if not path.is_file():
            raise ValueError("Saved output not found in this Developer session; do not use another Developer's ID")
        size = path.stat().st_size
        if offset_bytes > size:
            raise ValueError("offset_bytes exceeds output size; start at 0 or use next_offset_bytes")
        with path.open("rb") as stream:
            stream.seek(offset_bytes)
            text = stream.read(max_bytes).decode("utf-8", errors="replace")
            next_offset = stream.tell()
        return {"ok": True, "output_id": output_id, "text": text,
                "next_offset_bytes": next_offset, "total_bytes": size, "has_more": next_offset < size}

    return developer_read_output