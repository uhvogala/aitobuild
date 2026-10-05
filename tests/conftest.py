from __future__ import annotations

import pytest

from aitobuild.config import (
    AppConfig,
    DeveloperConfig,
    PolicyConfig,
    RuntimeConfig,
    SchedulerConfig,
    SecurityConfig,
)


@pytest.fixture
def test_config() -> AppConfig:
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
        ),
    )
