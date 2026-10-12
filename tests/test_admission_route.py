"""PLAN 2.4: the module-level organization_service.admit_task is the only code in src that may reach
DeveloperPreviewRegistry._approve_admission, by any route: attribute access, aliases, getattr strings or text."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
NAME = "_approve_admission"


def _fold(node: ast.AST) -> str | None:
    """Constant string value of a + concatenation or f-string made only of literals."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.FormattedValue) and node.format_spec is None:
        return _fold(node.value)
    if isinstance(node, ast.JoinedStr):
        parts = [_fold(value) for value in node.values]
        return None if any(part is None for part in parts) else "".join(p for p in parts if p is not None)
    return None


def _references() -> list[tuple[str, str]]:
    found = set()
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        tree = ast.parse(text, filename=str(path))
        rel = path.relative_to(SRC).as_posix()
        allowed: set[int] = set()  # line ranges that may mention the name
        for node in ast.walk(tree):
            if rel == "aitobuild/organization_service.py" and isinstance(node, ast.FunctionDef) and node.name == "admit_task" \
                    and node.col_offset == 0:
                allowed.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            if rel == "aitobuild/developer_preview.py" and isinstance(node, ast.FunctionDef) and node.name == NAME:
                allowed.add(node.lineno)  # the definition itself
        for number, line in enumerate(text.splitlines(), start=1):
            if NAME in line and number not in allowed:
                found.add((rel, f"{number}: {line.strip()}"))
        for node in ast.walk(tree):  # names assembled from string pieces, e.g. getattr(x, "_approve" + "_admission")
            if isinstance(node, (ast.BinOp, ast.JoinedStr)):
                value = _fold(node)
                if value and NAME in value and node.lineno not in allowed:
                    found.add((rel, f"{node.lineno}: assembled {value!r}"))
    return sorted(found)


def test_only_admit_task_references_approve_admission() -> None:
    assert _references() == []


def test_admit_task_still_reaches_approve_admission() -> None:
    tree = ast.parse((SRC / "aitobuild" / "organization_service.py").read_text())
    admit = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "admit_task")
    assert any(isinstance(n, ast.Attribute) and n.attr == NAME for n in ast.walk(admit))


def test_admit_task_refuses_preview_that_differs_from_stored(tmp_path) -> None:
    from dataclasses import replace

    import pytest

    from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage
    from aitobuild.developer_preview import DeveloperPreviewRegistry, preview_approval_operation
    from aitobuild.organization_service import OperatorPolicy, admit_task

    from aitobuild.dispatcher import DispatcherAgent

    previews = DeveloperPreviewRegistry(tmp_path / "previews.json")
    seed = DispatcherAgent(require_developer_preview=True).create_developer_preview(
        dedupe_key="seed", github_event="issues", action="opened", body={"issue": {"number": 1}})
    bundle = {key: value for key, value in seed.bundle_payload.items() if key != "issue_context"}
    stored = previews.create_or_get(dedupe_key="k", bundle_payload=bundle, source_payload={"action": "opened"})
    decision = ApprovalPolicy(task_admission="automatic").resolve(
        ApprovalStage.TASK_ADMISSION, actor_id="operator",
        operation=preview_approval_operation(stored.preview_id, stored.bundle_payload), automatic_supported=True)
    # Same bundle (so the decision digest still matches) but a different source_payload than the stored record.
    passed = replace(stored, source_payload={"action": "opened", "planning_assignment": "forged"})
    with pytest.raises(PermissionError, match="differs from its stored record"):
        admit_task(previews, stored.preview_id, OperatorPolicy(), decision=decision, expected=passed)
    assert not previews.get(stored.preview_id).approved
    approved = admit_task(previews, stored.preview_id, OperatorPolicy(), decision=decision, expected=stored)
    assert approved is not None and approved.approved
