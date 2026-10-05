from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any
import pytest

from aitobuild.config import (
    AppConfig,
    DeveloperConfig,
    GitHubConfig,
    PolicyConfig,
    RuntimeConfig,
    SchedulerConfig,
    SecurityConfig,
    WebSearchConfig,
)


@pytest.fixture
def repository_issue_body() -> dict[str, Any]:
    return {
        "repository": {"id": 101, "full_name": "fixture/widgets", "default_branch": "main"},
        "issue": {
            "id": 202, "number": 7, "state": "open", "title": "Handle empty widget names",
            "body": "Reject empty names.\n\n## Acceptance Criteria\n- [ ] Empty names are rejected.\n- Existing names still work.\n\n## Notes\n- Keep the API stable.",
        },
        "assignee": {"login": "fixture-developer"},
    }


@pytest.fixture
def local_issue_repository(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "target-source"
    source.mkdir()
    subprocess.run(["git", "-c", "init.templateDir=", "init", "--initial-branch=main", str(source)], check=True, capture_output=True)
    (source / "README.md").write_text("Fixture baseline\n")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True, capture_output=True)
    subprocess.run([
        "git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "Fixture baseline",
    ], check=True, capture_output=True)
    revision = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return source, revision


@pytest.fixture
def test_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        webhook_secret="test-secret",
        runtime=RuntimeConfig(
            foundry_endpoint=None,
            foundry_api_key=None,
            foundry_model="gpt-4.1",
            allow_mock_model=True,
        ),
        scheduler=SchedulerConfig(
            enabled=True,
            kill_switch=False,
            architect_scan_cron="0 8 * * *",
            meeting_tick_cron="*/30 * * * *",
            max_concurrent_proactive_jobs=2,
            quiet_window_start_hour=None,
            quiet_window_end_hour=None,
        ),
        policy=PolicyConfig(require_human_approval_for_repo_writes=True),
        security=SecurityConfig(
            require_internal_auth=True,
            internal_api_token="internal-test-token",
        ),
        developer=DeveloperConfig(
            require_preview_before_dispatch=False,
            execution_mode="mock",
            command_timeout_seconds=120,
            session_container_image="python:3.14-slim",
            session_container_workdir="/workspace",
            session_container_name_prefix="aitobuild-dev",
            session_container_bind_path=None,
            session_container_run_as_current_user=True,
            agent_invoke_timeout_seconds=180,
            enable_mcp_adapters=False,
            enable_agent_live_logs=False,
            mcp_shell_tool_name="execute_command",
            mcp_filesystem_read_tool_name="read_file",
            mcp_filesystem_write_tool_name="write_file",
            state_dir=str(tmp_path / "developer-state"),
        ),
        github=GitHubConfig(adapter="mock", default_repository="uhvogala/aitobuild_example"),
        web_search=WebSearchConfig(adapter="mock"),
    )
