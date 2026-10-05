"""Application configuration for Phase 1 foundation."""

from __future__ import annotations

from dataclasses import dataclass
from os import getenv


@dataclass(slots=True, frozen=True)
class RuntimeConfig:
    foundry_endpoint: str | None
    foundry_api_key: str | None
    foundry_model: str
    allow_mock_model: bool


@dataclass(slots=True, frozen=True)
class SchedulerConfig:
    enabled: bool
    kill_switch: bool
    architect_scan_cron: str
    meeting_tick_cron: str
    max_concurrent_proactive_jobs: int
    quiet_window_start_hour: int | None
    quiet_window_end_hour: int | None


@dataclass(slots=True, frozen=True)
class PolicyConfig:
    require_human_approval_for_repo_writes: bool


@dataclass(slots=True, frozen=True)
class SecurityConfig:
    require_internal_auth: bool
    internal_api_token: str | None


@dataclass(slots=True, frozen=True)
class DeveloperConfig:
    require_preview_before_dispatch: bool
    execution_mode: str
    command_timeout_seconds: int
    session_container_image: str
    session_container_workdir: str
    session_container_name_prefix: str
    session_container_bind_path: str | None
    session_container_run_as_current_user: bool
    agent_invoke_timeout_seconds: int
    enable_mcp_adapters: bool
    enable_agent_live_logs: bool
    mcp_shell_tool_name: str
    mcp_filesystem_read_tool_name: str
    mcp_filesystem_write_tool_name: str


@dataclass(slots=True, frozen=True)
class AppConfig:
    webhook_secret: str
    runtime: RuntimeConfig
    scheduler: SchedulerConfig
    policy: PolicyConfig
    security: SecurityConfig
    developer: DeveloperConfig


DEFAULT_SCAN_CRON = "0 8 * * *"
DEFAULT_MEETING_CRON = "*/30 * * * *"


def _as_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _require_non_empty(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise ValueError(f"Missing required configuration: {name}")
    return value.strip()


def _optional_hour(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    parsed = int(value)
    if parsed < 0 or parsed > 23:
        raise ValueError("quiet window hour values must be within 0..23")
    return parsed


def load_config() -> AppConfig:
    webhook_secret = _require_non_empty("AITOBUILD_WEBHOOK_SECRET", getenv("AITOBUILD_WEBHOOK_SECRET"))

    runtime = RuntimeConfig(
        foundry_endpoint=getenv("AITOBUILD_FOUNDRY_ENDPOINT"),
        foundry_api_key=getenv("AITOBUILD_FOUNDRY_API_KEY"),
        foundry_model=getenv("AITOBUILD_FOUNDRY_MODEL", "gpt-4.1"),
        allow_mock_model=_as_bool(getenv("AITOBUILD_ALLOW_MOCK_MODEL"), default=True),
    )

    scheduler = SchedulerConfig(
        enabled=_as_bool(getenv("AITOBUILD_SCHEDULER_ENABLED"), default=True),
        kill_switch=_as_bool(getenv("AITOBUILD_SCHEDULER_KILL_SWITCH"), default=False),
        architect_scan_cron=getenv("AITOBUILD_ARCHITECT_SCAN_CRON", DEFAULT_SCAN_CRON),
        meeting_tick_cron=getenv("AITOBUILD_MEETING_TICK_CRON", DEFAULT_MEETING_CRON),
        max_concurrent_proactive_jobs=int(getenv("AITOBUILD_MAX_PROACTIVE_JOBS", "2")),
        quiet_window_start_hour=_optional_hour(getenv("AITOBUILD_SCHEDULER_QUIET_START_HOUR")),
        quiet_window_end_hour=_optional_hour(getenv("AITOBUILD_SCHEDULER_QUIET_END_HOUR")),
    )

    policy = PolicyConfig(
        require_human_approval_for_repo_writes=_as_bool(
            getenv("AITOBUILD_REQUIRE_APPROVAL_FOR_REPO_WRITES"),
            default=True,
        )
    )

    require_internal_auth = _as_bool(getenv("AITOBUILD_REQUIRE_INTERNAL_AUTH"), default=True)
    internal_api_token = getenv("AITOBUILD_INTERNAL_API_TOKEN")
    if require_internal_auth:
        internal_api_token = _require_non_empty("AITOBUILD_INTERNAL_API_TOKEN", internal_api_token)

    security = SecurityConfig(
        require_internal_auth=require_internal_auth,
        internal_api_token=internal_api_token,
    )

    developer = DeveloperConfig(
        require_preview_before_dispatch=_as_bool(
            getenv("AITOBUILD_REQUIRE_DEVELOPER_PREVIEW"),
            default=False,
        ),
        execution_mode=getenv("AITOBUILD_DEVELOPER_EXECUTION_MODE", "mock").strip().lower(),
        command_timeout_seconds=int(getenv("AITOBUILD_DEVELOPER_COMMAND_TIMEOUT_SECONDS", "120")),
        session_container_image=getenv("AITOBUILD_DEVELOPER_SESSION_CONTAINER_IMAGE", "python:3.14-slim")
        .strip(),
        session_container_workdir=getenv("AITOBUILD_DEVELOPER_SESSION_CONTAINER_WORKDIR", "/workspace")
        .strip(),
        session_container_name_prefix=getenv(
            "AITOBUILD_DEVELOPER_SESSION_CONTAINER_PREFIX", "aitobuild-dev"
        ).strip(),
        session_container_bind_path=(
            getenv("AITOBUILD_DEVELOPER_SESSION_CONTAINER_BIND_PATH", "").strip() or None
        ),
        session_container_run_as_current_user=_as_bool(
            getenv("AITOBUILD_DEVELOPER_SESSION_RUN_AS_CURRENT_USER"),
            default=True,
        ),
        agent_invoke_timeout_seconds=int(
            getenv("AITOBUILD_DEVELOPER_AGENT_INVOKE_TIMEOUT_SECONDS", "180")
        ),
        enable_mcp_adapters=_as_bool(getenv("AITOBUILD_DEVELOPER_ENABLE_MCP_ADAPTERS"), default=False),
        enable_agent_live_logs=_as_bool(
            getenv("AITOBUILD_DEVELOPER_ENABLE_AGENT_LIVE_LOGS"),
            default=False,
        ),
        mcp_shell_tool_name=getenv("AITOBUILD_DEVELOPER_MCP_SHELL_TOOL_NAME", "execute_command").strip(),
        mcp_filesystem_read_tool_name=getenv(
            "AITOBUILD_DEVELOPER_MCP_FILESYSTEM_READ_TOOL_NAME", "read_file"
        ).strip(),
        mcp_filesystem_write_tool_name=getenv(
            "AITOBUILD_DEVELOPER_MCP_FILESYSTEM_WRITE_TOOL_NAME", "write_file"
        ).strip(),
    )

    if developer.execution_mode not in {"mock", "subprocess", "container_session"}:
        raise ValueError(
            "AITOBUILD_DEVELOPER_EXECUTION_MODE must be one of: mock, subprocess, container_session"
        )
    if developer.command_timeout_seconds < 1:
        raise ValueError("AITOBUILD_DEVELOPER_COMMAND_TIMEOUT_SECONDS must be >= 1")
    if not developer.session_container_image:
        raise ValueError("AITOBUILD_DEVELOPER_SESSION_CONTAINER_IMAGE must be non-empty")
    if not developer.session_container_workdir.startswith("/"):
        raise ValueError("AITOBUILD_DEVELOPER_SESSION_CONTAINER_WORKDIR must be an absolute path")
    if not developer.session_container_name_prefix:
        raise ValueError("AITOBUILD_DEVELOPER_SESSION_CONTAINER_PREFIX must be non-empty")
    if developer.session_container_bind_path is not None and not developer.session_container_bind_path.startswith("/"):
        raise ValueError("AITOBUILD_DEVELOPER_SESSION_CONTAINER_BIND_PATH must be an absolute path")
    if developer.agent_invoke_timeout_seconds < 1:
        raise ValueError("AITOBUILD_DEVELOPER_AGENT_INVOKE_TIMEOUT_SECONDS must be >= 1")
    if not developer.mcp_shell_tool_name:
        raise ValueError("AITOBUILD_DEVELOPER_MCP_SHELL_TOOL_NAME must be non-empty")
    if not developer.mcp_filesystem_read_tool_name:
        raise ValueError("AITOBUILD_DEVELOPER_MCP_FILESYSTEM_READ_TOOL_NAME must be non-empty")
    if not developer.mcp_filesystem_write_tool_name:
        raise ValueError("AITOBUILD_DEVELOPER_MCP_FILESYSTEM_WRITE_TOOL_NAME must be non-empty")

    if scheduler.max_concurrent_proactive_jobs < 1:
        raise ValueError("AITOBUILD_MAX_PROACTIVE_JOBS must be >= 1")

    if (scheduler.quiet_window_start_hour is None) != (scheduler.quiet_window_end_hour is None):
        raise ValueError(
            "AITOBUILD_SCHEDULER_QUIET_START_HOUR and AITOBUILD_SCHEDULER_QUIET_END_HOUR must both be set"
        )

    return AppConfig(
        webhook_secret=webhook_secret,
        runtime=runtime,
        scheduler=scheduler,
        policy=policy,
        security=security,
        developer=developer,
    )
