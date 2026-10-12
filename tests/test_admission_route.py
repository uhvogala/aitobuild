"""PLAN 2.4: organization_service.admit_task is the only caller of DeveloperPreviewRegistry.approve."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _approve_callers() -> list[tuple[str, str]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for func in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))] + [tree]:
            body = func.body if not isinstance(func, ast.Module) else [n for n in tree.body if not isinstance(
                n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            for stmt in body:
                for node in ast.walk(stmt):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "approve" and "preview" in ast.unparse(node.func.value).lower()):
                        name = getattr(func, "name", "<module>")
                        found.append((path.relative_to(SRC).as_posix(), name))
    return sorted(set(found))


def test_admit_task_is_the_only_registry_approve_caller() -> None:
    assert _approve_callers() == [("aitobuild/organization_service.py", "admit_task")]
