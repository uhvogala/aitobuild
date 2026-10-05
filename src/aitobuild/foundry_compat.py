"""Foundry Responses API compatibility helpers for Agent Framework tools."""

from __future__ import annotations

import copy
from typing import Any

from agent_framework import FileMemoryProvider


def inline_json_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON Schema with ``$ref`` / ``$defs`` expanded in place.

    Azure Foundry's Responses API (notably with grok-4.6) rejects function-tool
    parameter schemas that contain ``$defs`` / ``$ref``. Inlining keeps the same
    shape while remaining API-compatible.
    """
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", None) or schema.pop("definitions", None) or {}

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                key = ref.rsplit("/", 1)[-1]
                if key not in defs:
                    return {k: resolve(v) for k, v in node.items()}
                resolved = copy.deepcopy(defs[key])
                for key_name, value in node.items():
                    if key_name != "$ref":
                        resolved[key_name] = value
                return resolve(resolved)
            return {key_name: resolve(value) for key_name, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


def _schema_needs_ref_inline(schema: dict[str, Any]) -> bool:
    return "$defs" in schema or "definitions" in schema or "$ref" in schema


def sanitize_function_tool_schemas_for_foundry(tools: list[Any]) -> None:
    """Inline ``$ref``/``$defs`` in FunctionTool parameter schemas in-place."""
    for tool_item in tools:
        parameters = getattr(tool_item, "parameters", None)
        if not callable(parameters):
            continue
        schema = parameters()
        if not isinstance(schema, dict) or not _schema_needs_ref_inline(schema):
            continue
        inlined = inline_json_schema_refs(schema)
        if inlined == schema:
            continue
        schema.clear()
        schema.update(inlined)


class FoundryCompatibleFileMemoryProvider(FileMemoryProvider):
    """FileMemoryProvider that sanitizes tool schemas for Foundry Responses API.

    Upstream ``file_memory_replace_lines`` exposes a nested Pydantic model whose
    JSON Schema uses ``$defs``/``$ref``. Foundry returns HTTP 400 for that shape
    with grok-4.6; inlining the schema keeps all seven memory tools enabled.
    """

    async def before_run(self, **kwargs: Any) -> None:
        await super().before_run(**kwargs)
        context = kwargs.get("context")
        tools = getattr(context, "tools", None)
        if isinstance(tools, list):
            sanitize_function_tool_schemas_for_foundry(tools)


__all__ = [
    "FoundryCompatibleFileMemoryProvider",
    "inline_json_schema_refs",
    "sanitize_function_tool_schemas_for_foundry",
]
