"""#54/#61: `is_correction_task` is the one correction rule in src and in the trial harnesses (scripts/trials).

Modeled on the `_approve_admission` route guard (test_admission_route.py). The scan is static: it reads every
source line (so comments and docstrings count too), attribute access, and every string built from literals
(implicit concatenation, +, f-strings, str.join, % formatting). It fails on `CORRECTION_TASK_PREFIX` or the literal
correction prefix anywhere in src outside the small allowlist below. It also keeps the two rules apart: the
prefix-only `is_correction_task_id` may only be called from the allowlisted callers below, and the full rule
`is_correction_task(preview)` must never be handed a task ID string. A prefix assembled fully at runtime from
data cannot be caught statically; review covers that case."""
import ast
from pathlib import Path

import pytest

from test_admission_route import _fold

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
# The operator trial harnesses classify tasks too (#75 follow-up): they get the same scan with no allowlist, plus a
# ban on the hand-written `correction_from_review` source check. They must call is_correction_task(preview).
TRIALS = ROOT / "scripts" / "trials"
SOURCE_KEY = "correction_from_review"
NAME = "CORRECTION_TASK_PREFIX"
LITERAL = "published-correction"  # also catches the prefix without its trailing dash
# The only places in src that may name the constant or spell the prefix, all in aitobuild/developer_preview.py:
# (a) the module-level definition of the constant, (b) the body of `is_correction_task_id`, the prefix check that
# the full rule `is_correction_task` builds on, and (c) the body of `correction_task_id`, the one builder of
# correction task IDs. Anything else must call is_correction_task(preview).
ALLOWED_FILE = "aitobuild/developer_preview.py"
ALLOWED_FUNCTIONS = frozenset({"is_correction_task_id", "correction_task_id"})
ID_RULE = "is_correction_task_id"
FULL_RULE = "is_correction_task"
# Callers of the prefix-only rule, as (module, module-level function or "Class.method"). Every other src site has a
# preview, or a record classified by is_correction_record. Add an entry only for a site with no preview, and say why.
ID_RULE_CALLERS = frozenset({
    (ALLOWED_FILE, FULL_RULE),  # the full rule is prefix OR correction_from_review source
    # A record whose approved preview is absent from this worker's registry is classified by its stored verdict;
    # the prefix check only stops a stored False from undercutting a correction task ID (it can only add refusals).
    ("aitobuild/developer_delivery.py", "DeveloperDeliveryWorker.is_correction_record"),
})


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


def _function_lines(tree: ast.Module, rel: str, allowed: frozenset[tuple[str, str]]) -> set[int]:
    """Lines of allowlisted module-level functions and "Class.method"s, plus the module's import of the rule."""
    lines: set[int] = set()
    def span(node: ast.AST) -> range:
        return range(node.lineno, (node.end_lineno or node.lineno) + 1)  # type: ignore[attr-defined]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and (rel, node.name) in allowed:
            lines.update(span(node))
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and (rel, f"{node.name}.{member.name}") in allowed:
                    lines.update(span(member))
    if lines and rel != ALLOWED_FILE:  # a module with an allowlisted caller may import the rule
        for node in tree.body:
            if (isinstance(node, ast.ImportFrom) and node.module == "aitobuild.developer_preview"
                    and any(alias.name == ID_RULE for alias in node.names)):
                lines.update(span(node))
    return lines


def _stringish(node: ast.AST) -> bool:
    """An argument that is obviously a task ID string rather than a preview."""
    if isinstance(node, (ast.JoinedStr, ast.BinOp)) or (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str":
        return True
    if isinstance(node, ast.Name) and node.id.endswith("task_id"):
        return True
    if isinstance(node, ast.Attribute) and node.attr.endswith("task_id"):
        return True
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "task_id":
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == "task_id")


def _scan_rules(tree: ast.Module, rel: str, text: str) -> set[tuple[str, str]]:
    """Keep the two rules apart: prefix-only calls only from ID_RULE_CALLERS, no string into the full rule."""
    callers = _function_lines(tree, rel, ID_RULE_CALLERS)
    definition = _function_lines(tree, rel, frozenset({(ALLOWED_FILE, ID_RULE)}))  # the rule's own body
    found = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if ID_RULE in line and number not in callers | definition:
            found.add((rel, f"{number}: prefix-only rule outside its allowlisted callers: {line.strip()}"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == ID_RULE and node.lineno not in callers:
            found.add((rel, f"{node.lineno}: prefix-only rule attribute"))
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None)
            if name == FULL_RULE and any(_stringish(argument) for argument in node.args):
                found.add((rel, f"{node.lineno}: full rule called with a task ID string"))
    return found


def _scan(text: str, rel: str) -> set[tuple[str, str]]:
    tree = ast.parse(text, filename=rel)
    allowed = _allowed_lines(tree, rel)
    found = _scan_rules(tree, rel, text)
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


def _scan_trial_script(text: str, rel: str) -> set[tuple[str, str]]:
    """A trial harness script: nothing is allowlisted (``rel`` is never ALLOWED_FILE), and any code use of the
    correction source key, e.g. ``"correction_from_review" in source``, is a hand-written copy of the full rule."""
    found = _scan(text, rel)
    for node in ast.walk(ast.parse(text, filename=rel)):
        if isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr, ast.Call)) and _fold(node) == SOURCE_KEY:
            found.add((rel, f"{node.lineno}: hand-written correction source check {SOURCE_KEY!r}"))
    return found


def _references() -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        found |= _scan(path.read_text(), path.relative_to(SRC).as_posix())
    return sorted(found)


def _trial_references() -> tuple[list[tuple[str, str]], set[str]]:
    """Findings in every trial harness script, and the names of the scripts scanned."""
    found: set[tuple[str, str]] = set()
    scanned: set[str] = set()
    for path in sorted(TRIALS.rglob("*.py")):
        found |= _scan_trial_script(path.read_text(), path.relative_to(ROOT).as_posix())
        scanned.add(path.name)
    return sorted(found), scanned


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


def test_trial_harnesses_use_the_shared_correction_rule() -> None:
    found, scanned = _trial_references()
    assert {"common.py", "correction_cycle.py", "followups.py", "pilot.py"} <= scanned
    assert found == []


@pytest.mark.parametrize("snippet", [
    *STRAY,
    'ok = "correction_from_review" in (source_payload or {})',
    'ok = "correction_from_review" not in source',
    'ok = source.get("correction_from_review") is not None',
    'ok = bool(source["correction_from_" "review"])',
])
def test_trial_scan_catches_hand_written_correction_checks(snippet: str) -> None:
    assert _scan_trial_script(snippet + "\n", "scripts/trials/common.py"), snippet


def test_trial_scan_allows_the_shared_rule() -> None:
    snippet = "from aitobuild.developer_preview import is_correction_task\nok = is_correction_task(preview)\n"
    assert _scan_trial_script(snippet, "scripts/trials/common.py") == set()
    # The allowlist of developer_preview.py does not carry over to a harness file of any name.
    text = (SRC / ALLOWED_FILE).read_text()
    assert _scan_trial_script(text, "scripts/trials/developer_preview.py")


def test_the_allowlisted_rule_and_builder_still_use_the_constant() -> None:
    from aitobuild.developer_preview import (CORRECTION_TASK_PREFIX, correction_task_id, is_correction_task,
                                             is_correction_task_id)
    tree = ast.parse((SRC / ALLOWED_FILE).read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for name in ALLOWED_FUNCTIONS:
        assert any(isinstance(n, ast.Name) and n.id == NAME for n in ast.walk(functions[name])), name
    # The full rule is built on the prefix check, not on its own copy of the prefix.
    assert any(isinstance(n, ast.Name) and n.id == ID_RULE for n in ast.walk(functions[FULL_RULE]))
    assert correction_task_id("ab") == CORRECTION_TASK_PREFIX + "ab"
    assert is_correction_task_id(correction_task_id("ab")) and not is_correction_task_id("ISSUE-7")
    preview = type("P", (), {"bundle_payload": {"task_id": "ISSUE-7"}, "source_payload": {}})()
    assert not is_correction_task(preview)
    preview.source_payload = {"correction_from_review": "dp-r"}
    assert is_correction_task(preview)
    preview = type("P", (), {"bundle_payload": {"task_id": correction_task_id("ab")}, "source_payload": {}})()
    assert is_correction_task(preview)
    # Two rules, two signatures: the full rule refuses a string, the prefix rule refuses anything else.
    for wrong in (correction_task_id("ab"), "ISSUE-7", None):
        with pytest.raises(TypeError):
            is_correction_task(wrong)
    with pytest.raises(TypeError):
        is_correction_task_id(preview)


@pytest.mark.parametrize("snippet", [
    "ok = is_correction_task_id(task_id)",
    "ok = developer_preview.is_correction_task_id(record.task_id)",
    "rule = is_correction_task_id",
    "from aitobuild.developer_preview import is_correction_task_id",
    "ok = is_correction_task(task_id)",
    "ok = is_correction_task(record.task_id)",
    "ok = is_correction_task(assignment.task_id)",
    'ok = is_correction_task(str(payload["task_id"]))',
    'ok = is_correction_task(payload["task_id"])',
    'ok = is_correction_task(bundle.get("task_id", ""))',
    'ok = is_correction_task("ISSUE-7")',
    'ok = is_correction_task(f"{prefix}x")',
    'ok = developer_preview.is_correction_task(record.task_id)',
])
def test_scan_keeps_the_two_rules_apart(snippet: str) -> None:
    assert _scan(snippet + "\n", "aitobuild/rogue.py"), snippet


def test_full_rule_calls_with_a_preview_are_not_flagged() -> None:
    for snippet in ("ok = is_correction_task(preview)", "ok = is_correction_task(self._previews.get(preview_id))",
                    "ok = worker.is_correction_record(record)"):
        assert _scan(snippet + "\n", "aitobuild/rogue.py") == set(), snippet


def test_prefix_rule_callers_are_only_the_allowlisted_ones() -> None:
    tree = ast.parse((SRC / ALLOWED_FILE).read_text())
    rule = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == ID_RULE)
    assert _scan_rules(tree, ALLOWED_FILE, (SRC / ALLOWED_FILE).read_text()) == set()
    # The definition line is allowed, a second use in the same file outside the full rule is not.
    stray = (SRC / ALLOWED_FILE).read_text() + "\n\ndef other(task_id):\n    return is_correction_task_id(task_id)\n"
    assert _scan(stray, ALLOWED_FILE)
    assert rule.lineno not in _function_lines(tree, ALLOWED_FILE, ID_RULE_CALLERS)
    # A namesake of the allowlisted caller in another module is not allowlisted.
    assert _scan("def is_correction_task(p):\n    return is_correction_task_id(p)\n", "aitobuild/developer_delivery.py")
    # The allowlisted method may use it; the same method name on another class, a module-level namesake, or a
    # second method of the same class may not, and neither may an import in a module with no allowlisted caller.
    method = ("from aitobuild.developer_preview import is_correction_task_id\n\n"
              "class DeveloperDeliveryWorker:\n    def is_correction_record(self, r):\n"
              "        return is_correction_task_id(r.task_id)\n")
    assert _scan(method, "aitobuild/developer_delivery.py") == set()
    assert _scan(method.replace("DeveloperDeliveryWorker", "Other"), "aitobuild/developer_delivery.py")
    assert _scan(method, "aitobuild/organization_service.py")
    assert _scan(method + "\n    def other(self, r):\n        return is_correction_task_id(r.task_id)\n",
                 "aitobuild/developer_delivery.py")
    assert _scan("def is_correction_record(r):\n    return is_correction_task_id(r.task_id)\n",
                 "aitobuild/developer_delivery.py")
