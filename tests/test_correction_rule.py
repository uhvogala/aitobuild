"""#54/#61: `is_correction_task` is the one correction rule in src.

Modeled on the `_approve_admission` route guard (test_admission_route.py). The scan is static: it reads every
source line (so comments and docstrings count too), attribute access, and every string built from literals
(implicit concatenation, +, f-strings, str.join, % formatting). It fails on `CORRECTION_TASK_PREFIX` or the literal
correction prefix anywhere in src outside the small allowlist below. A prefix assembled fully at runtime from
data cannot be caught statically; review covers that case."""
import ast
from pathlib import Path

import pytest

from test_admission_route import _fold

SRC = Path(__file__).resolve().parents[1] / "src"
NAME = "CORRECTION_TASK_PREFIX"
LITERAL = "published-correction"  # also catches the prefix without its trailing dash
# The only places in src that may name the constant or spell the prefix, all in aitobuild/developer_preview.py:
# (a) the module-level definition of the constant, (b) the body of `is_correction_task`, and (c) the body of
# `correction_task_id`, the one builder of correction task IDs. Anything else must call is_correction_task.
ALLOWED_FILE = "aitobuild/developer_preview.py"
ALLOWED_FUNCTIONS = frozenset({"is_correction_task", "correction_task_id"})


def _allowed_lines(tree: ast.Module, rel: str) -> set[int]:
    allowed: set[int] = set()
    if rel != ALLOWED_FILE:
        return allowed
    for node in tree.body:  # module level only: a nested or method namesake is not allowlisted
        if isinstance(node, ast.FunctionDef) and node.name in ALLOWED_FUNCTIONS:
            allowed.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == NAME):
            allowed.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return allowed


def _scan(text: str, rel: str) -> set[tuple[str, str]]:
    tree = ast.parse(text, filename=rel)
    allowed = _allowed_lines(tree, rel)
    found = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if (NAME in line or LITERAL in line) and number not in allowed:
            found.add((rel, f"{number}: {line.strip()}"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == NAME and node.lineno not in allowed:
            found.add((rel, f"{node.lineno}: attribute"))
        if isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr, ast.Call)):
            value = _fold(node)
            if value and (LITERAL in value or NAME in value) and node.lineno not in allowed:
                found.add((rel, f"{node.lineno}: string {value!r}"))
    return found


def _references() -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        found |= _scan(path.read_text(), path.relative_to(SRC).as_posix())
    return sorted(found)


STRAY = [
    'if task_id.startswith("published-correction-"):\n    pass',
    'if task_id.startswith(CORRECTION_TASK_PREFIX):\n    pass',
    'from aitobuild.developer_preview import CORRECTION_TASK_PREFIX',
    'if task_id.startswith(developer_preview.CORRECTION_TASK_PREFIX):\n    pass',
    'ok = task_id.startswith("published-" "correction-")',
    'ok = task_id.startswith("published-" + "correction-")',
    'ok = task_id.startswith(f"published-{\'correction-\'}")',
    'ok = task_id.startswith("-".join(["published", "correction", ""]))',
    'ok = task_id.startswith("%s-%s-" % ("published", "correction"))',
    'ok = task_id.startswith(("published-review-", "published-correction-"))',
    'ok = "published-correction" in task_id',
    '# a comment about published-correction- tasks',
]


@pytest.mark.parametrize("snippet", STRAY)
def test_scan_catches_each_static_spelling(snippet: str) -> None:
    assert _scan(snippet + "\n", "aitobuild/rogue.py"), snippet


@pytest.mark.parametrize("snippet", STRAY[:2])
def test_allowlist_covers_only_its_own_definitions(snippet: str) -> None:
    text = (SRC / ALLOWED_FILE).read_text()
    assert _scan(text, ALLOWED_FILE) == set()
    # A stray check elsewhere in the allowlisted file, or in a namesake method, is still flagged.
    assert _scan(text + "\n\ndef other(task_id):\n    " + snippet.replace("\n", "\n    ") + "\n", ALLOWED_FILE)
    nested = "\n\nclass Rogue:\n    def is_correction_task(self, task_id):\n        " + snippet.replace("\n", "\n        ") + "\n"
    assert _scan(text + nested, ALLOWED_FILE)
    assert _scan(snippet + "\n", "aitobuild/developer_delivery.py")
    # A namesake of an allowlisted function in another module is not allowlisted.
    copy = "def is_correction_task(task_id):\n    " + snippet.replace("\n", "\n    ") + "\n"
    assert _scan(copy, "aitobuild/developer_delivery.py")


def test_only_the_correction_rule_names_the_correction_prefix() -> None:
    assert _references() == []


def test_the_allowlisted_rule_and_builder_still_use_the_constant() -> None:
    from aitobuild.developer_preview import CORRECTION_TASK_PREFIX, correction_task_id, is_correction_task
    tree = ast.parse((SRC / ALLOWED_FILE).read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for name in ALLOWED_FUNCTIONS:
        assert any(isinstance(n, ast.Name) and n.id == NAME for n in ast.walk(functions[name])), name
    assert correction_task_id("ab") == CORRECTION_TASK_PREFIX + "ab"
    assert is_correction_task(correction_task_id("ab")) and not is_correction_task("ISSUE-7")
    preview = type("P", (), {"bundle_payload": {"task_id": "ISSUE-7"}, "source_payload": {}})()
    assert not is_correction_task(preview)
    preview.source_payload = {"correction_from_review": "dp-r"}
    assert is_correction_task(preview)
