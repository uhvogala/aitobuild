import asyncio
import json
from pathlib import Path

from agent_framework import Content, FunctionInvocationContext, tool
import pytest

from aitobuild.tool_outputs import build_output_guard, build_output_reader


@tool
def example_tool() -> str:
    return "unused"


def test_large_result_is_saved_and_model_only_gets_bounded_preview(tmp_path: Path) -> None:
    original = "marker:" + "x" * 100000
    context = FunctionInvocationContext(function=example_tool, arguments={})

    async def execute() -> None:
        context.result = [Content.from_text(original)]

    asyncio.run(build_output_guard(tmp_path)(context, execute))
    summary_text = context.result[0].text
    assert len(summary_text) < 2000
    summary = json.loads(summary_text)
    assert summary["output_saved"]
    path = tmp_path / f"{summary['output_id']}.txt"
    assert path.read_text() == original
    reader = build_output_reader(tmp_path)
    first = reader(output_id=summary["output_id"], max_bytes=256)
    assert first["text"] == original[:256]
    assert first["has_more"] and first["next_offset_bytes"] == 256
    second = reader(output_id=summary["output_id"], offset_bytes=256, max_bytes=256)
    assert second["text"] == original[256:512]


def test_saved_outputs_are_session_private_and_paths_rejected(tmp_path: Path) -> None:
    reader = build_output_reader(tmp_path / "other-developer")
    with pytest.raises(ValueError, match="paths are not accepted"):
        reader(output_id="../../private")
    with pytest.raises(ValueError, match="this Developer session"):
        reader(output_id="a" * 32)


def test_large_command_failure_keeps_exit_and_status_metadata(tmp_path: Path) -> None:
    context = FunctionInvocationContext(function=example_tool, arguments={})

    async def execute() -> None:
        context.result = [Content.from_text(json.dumps({
            "ok": True, "status": "exited", "exit_code": 7, "shell_id": "sh-example",
            "output": "x" * 20000,
        }))]

    asyncio.run(build_output_guard(tmp_path)(context, execute))
    summary = json.loads(context.result[0].text)
    assert summary["exit_code"] == 7 and summary["status"] == "exited"
    assert summary["shell_id"] == "sh-example"


def test_small_tool_results_are_unchanged(tmp_path: Path) -> None:
    context = FunctionInvocationContext(function=example_tool, arguments={})
    contents = [Content.from_text("small")]

    async def execute() -> None:
        context.result = contents

    asyncio.run(build_output_guard(tmp_path)(context, execute))
    assert context.result is contents


def test_multibyte_result_budget_is_measured_in_bytes(tmp_path: Path) -> None:
    original = "\U0001f680" * 2000
    context = FunctionInvocationContext(function=example_tool, arguments={})

    async def execute() -> None:
        context.result = [Content.from_text(original)]

    asyncio.run(build_output_guard(tmp_path)(context, execute))
    summary = json.loads(context.result[0].text)
    assert summary["output_saved"]
    assert len(summary["preview"].encode("utf-8")) <= 1200
    assert (tmp_path / f"{summary['output_id']}.txt").read_text() == original


def test_large_exception_is_saved_without_hiding_failure(tmp_path: Path) -> None:
    original = "failure:" + "x" * 100000
    context = FunctionInvocationContext(function=example_tool, arguments={})

    async def execute() -> None:
        raise ValueError(original)

    with pytest.raises(RuntimeError) as captured:
        asyncio.run(build_output_guard(tmp_path)(context, execute))
    summary = json.loads(str(captured.value))
    assert summary["ok"] is False and summary["output_saved"]
    assert len(str(captured.value)) < 2000
    saved = json.loads((tmp_path / f"{summary['output_id']}.txt").read_text())
    assert saved["error"] == original


def test_tool_media_is_saved_instead_of_sent_to_model(tmp_path: Path) -> None:
    context = FunctionInvocationContext(function=example_tool, arguments={})
    media = Content.from_uri(uri="https://example.invalid/screenshot.png", media_type="image/png")

    async def execute() -> None:
        context.result = [media]

    asyncio.run(build_output_guard(tmp_path)(context, execute))
    assert context.result[0].type == "text"
    summary = json.loads(context.result[0].text)
    assert summary["output_saved"]
    saved = json.loads((tmp_path / f"{summary['output_id']}.txt").read_text())
    assert saved == [media.to_dict()]