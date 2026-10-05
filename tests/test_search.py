import json
from pathlib import Path

import pytest

from aitobuild.developer_isolation import default_developer_isolation_policy, is_command_allowed
from aitobuild.tools.search import search_workspace


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/alpha.py").write_text("def add(left, right):\n    return left + right\n")
    (tmp_path / "src/beta.py").write_text("ADD = 1\n")
    (tmp_path / "tests/test_alpha.py").write_text("from alpha import add\n")
    return tmp_path


def query(repo: Path, *, kind="content", pattern="add", **kwargs):
    return search_workspace(workspace=repo, policy=default_developer_isolation_policy(),
                            kind=kind, pattern=pattern, **kwargs)


def test_file_globs_are_sorted_and_paged(repo: Path) -> None:
    first = query(repo, kind="files", pattern="**/*.py", max_results=2)
    assert [item["path"] for item in first["results"]] == ["src/alpha.py", "src/beta.py"]
    assert first["has_more"] and first["next_offset"] == 2
    second = query(repo, kind="files", pattern="**/*.py", max_results=2, offset=2)
    assert second["results"] == [{"path": "tests/test_alpha.py"}]
    assert not second["has_more"]


def test_literal_regex_case_and_glob_search(repo: Path) -> None:
    literal = query(repo, pattern="left + right", globs=["**/*.py", "!tests/**"])
    assert literal["results"][0]["line_number"] == 2
    assert literal["results"][0]["path"] == "src/alpha.py"
    regex = query(repo, pattern=r"^def\s+add", is_regex=True)
    assert regex["returned"] == 1 and regex["results"][0]["column_bytes"] == 1
    insensitive = query(repo, pattern="add", case_sensitive=False)
    assert insensitive["returned"] == 3
    assert query(repo, pattern="not-present")["results"] == []


def test_ignored_hidden_and_blocked_paths(repo: Path) -> None:
    (repo / ".gitignore").write_text("src/ignored.py\n")
    (repo / "src/ignored.py").write_text("add\n")
    (repo / "src/.hidden.py").write_text("add\n")
    (repo / "src/secrets").mkdir()
    (repo / "src/secrets/private.py").write_text("add\n")
    assert query(repo, kind="files", pattern="**/*.py")["returned"] == 3
    visible = query(repo, kind="files", pattern="**/*.py", include_hidden=True, include_ignored=True)
    assert visible["returned"] == 5
    assert all("secrets" not in item["path"] for item in visible["results"])


@pytest.mark.parametrize("path", ["../outside", "/tmp", "secrets"])
def test_search_rejects_invalid_roots(repo: Path, path: str) -> None:
    (repo / "secrets").mkdir(exist_ok=True)
    with pytest.raises(ValueError):
        query(repo, path=path)


def test_symlinks_do_not_leak_external_files(repo: Path, tmp_path: Path) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-external.txt"
    external.write_text("add SECRET\n")
    (repo / "src/link.py").symlink_to(external)
    assert all(item["path"] != "src/link.py" for item in query(repo)["results"])
    with pytest.raises(ValueError, match="symlink|escape"):
        query(repo, path="src/link.py")


def test_option_like_patterns_and_unusual_file_names_are_literal(repo: Path) -> None:
    name = "src/colon:name\nspace.py"
    (repo / name).write_text("--files\n")
    assert query(repo, pattern="--files")["results"][0]["path"] == name
    assert {item["path"] for item in query(repo, kind="files", pattern="**/*.py")["results"]} >= {name}


def test_invalid_regex_has_diagnosis(repo: Path) -> None:
    with pytest.raises(ValueError, match="regex parse error"):
        query(repo, pattern="[", is_regex=True)


def test_long_matches_and_many_results_are_bounded(repo: Path) -> None:
    (repo / "src/long.txt").write_text(("add " + "x" * 10000 + "\n") * 50)
    result = query(repo, pattern="add", globs=["**/*.txt"], max_results=100)
    assert len(json.dumps(result).encode()) < 6000
    assert result["has_more"] and all(item["text_truncated"] for item in result["results"])
    page = query(repo, pattern="add", globs=["**/*.txt"], offset=result["next_offset"])
    assert page["results"][0]["line_number"] == result["returned"] + 1


def test_large_and_binary_files_do_not_flood_content_search(repo: Path) -> None:
    (repo / "src/large.txt").write_text("add" + "x" * 1048576)
    (repo / "src/binary.bin").write_bytes(b"add\0binary")
    assert query(repo, globs=["**/*.txt", "**/*.bin"])["results"] == []


def test_preview_policy_allows_ripgrep_search_commands() -> None:
    policy = default_developer_isolation_policy()
    assert is_command_allowed("rg --files", policy=policy)
    assert is_command_allowed("rg --line-number def src/", policy=policy)
    assert not is_command_allowed("rg-unrelated-command", policy=policy)