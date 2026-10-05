from pathlib import Path

import pytest


@pytest.fixture
def evaluation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from sim import evaluate_tools
    return evaluate_tools


def test_model_success_claim_without_executed_tools_fails(evaluation) -> None:
    case = evaluation.grade_case(
        name="files", response={"completed": True, "output_text": "Everything passed!"},
        required_tools={"developer_edit_file"}, checks={"pytest": True}, elapsed_seconds=1,
    )
    assert not case["passed"]
    assert case["missing_successful_tools"] == ["developer_edit_file"]


def test_failed_tool_then_recovery_counts_both_calls(evaluation) -> None:
    case = evaluation.grade_case(name="recovery", response={
        "completed": True, "usage": {"total_token_count": 42}, "tool_trace": [
            {"name": "patch", "ok": False}, {"name": "patch", "ok": True},
        ],
    }, required_tools={"patch"}, checks={"disk": True}, elapsed_seconds=2, expected_errors=1)
    assert case["passed"]
    assert case["tool_errors"] == 1
    assert case["tool_calls"] == 2
    assert case["repeated_tool_calls"] == 1
    report = evaluation.summarize([case], {"patch", "read"})
    assert report["tool_coverage"] == 0.5
    assert not report["succeeded"]
    assert report["usage"]["total_token_count"] == 42


def test_correct_tool_call_without_observable_result_fails(evaluation) -> None:
    case = evaluation.grade_case(name="files", response={
        "completed": True, "tool_trace": [{"name": "write", "ok": True}],
    }, required_tools={"write"}, checks={"file_matches": False}, elapsed_seconds=0)
    assert not case["passed"]
    assert not evaluation.summarize([case], {"write"})["succeeded"]


def test_pending_approvals_are_not_completion(evaluation) -> None:
    case = evaluation.grade_case(name="pending", response={
        "completed": False, "tool_trace": [{"name": "read", "ok": True}],
    }, required_tools={"read"}, checks={"result": True}, elapsed_seconds=0)
    assert not case["passed"]


def test_unexpected_error_fails_even_after_successful_retry(evaluation) -> None:
    case = evaluation.grade_case(name="files", response={
        "completed": True, "tool_trace": [
            {"name": "patch", "ok": False}, {"name": "patch", "ok": True},
        ],
    }, required_tools={"patch"}, checks={"disk": True}, elapsed_seconds=0)
    assert not case["passed"]
    assert case["unexpected_tool_errors"] == 1