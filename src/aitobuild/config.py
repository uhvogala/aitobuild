"""Application configuration for Phase 1 foundation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from os import environ
from pathlib import Path
import re
from typing import Any, get_args, get_type_hints

from aitobuild.approval_policy import ApprovalPolicy, ApprovalStage
from aitobuild.organization import OrganizationDefinition


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
class GitHubConfig:
    adapter: str = "mock"
    default_repository: str | None = None
    allowed_repositories: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class WebSearchConfig:
    adapter: str = "mock"


@dataclass(slots=True, frozen=True)
class ManagedWorkerConfig:
    queue_follow_ups: bool = False


@dataclass(slots=True, frozen=True)
class SecurityConfig:
    require_internal_auth: bool
    internal_api_token: str | None


@dataclass(slots=True, frozen=True)
class RepositorySourceConfig:
    repository: str
    repository_id: int
    path: str
    verification_commands: tuple[str, ...] = ()
    base_revision: str | None = None


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
    state_dir: str = ".aitobuild/developer"
    session_data_volume: str | None = None
    enable_browser: bool = False
    repository_sources: tuple[RepositorySourceConfig, ...] = ()


@dataclass(slots=True, frozen=True)
class AppConfig:
    webhook_secret: str
    runtime: RuntimeConfig
    scheduler: SchedulerConfig
    policy: PolicyConfig
    security: SecurityConfig
    developer: DeveloperConfig
    github: GitHubConfig
    web_search: WebSearchConfig
    approvals: ApprovalPolicy = field(default_factory=ApprovalPolicy)
    organization: OrganizationDefinition | None = None
    managed_worker: ManagedWorkerConfig = field(default_factory=ManagedWorkerConfig)


DEFAULT_SCAN_CRON = "0 8 * * *"
DEFAULT_MEETING_CRON = "*/30 * * * *"

_CONFIG_CLASSES = {"runtime": RuntimeConfig, "scheduler": SchedulerConfig, "policy": PolicyConfig,
                   "security": SecurityConfig, "developer": DeveloperConfig, "github": GitHubConfig,
                   "web_search": WebSearchConfig, "managed_worker": ManagedWorkerConfig}
# Environment names without their prefix: file settings and overrides both use "AITOBUILD_" + name
# (for example AITOBUILD_MANAGED_WORKER_QUEUE_FOLLOW_UPS).
_CONFIG_ENV = {
    "runtime": {"foundry_endpoint": "FOUNDRY_ENDPOINT", "foundry_api_key": "FOUNDRY_API_KEY",
                "foundry_model": "FOUNDRY_MODEL", "allow_mock_model": "ALLOW_MOCK_MODEL"},
    "scheduler": {"enabled": "SCHEDULER_ENABLED", "kill_switch": "SCHEDULER_KILL_SWITCH",
                  "architect_scan_cron": "ARCHITECT_SCAN_CRON", "meeting_tick_cron": "MEETING_TICK_CRON",
                  "max_concurrent_proactive_jobs": "MAX_PROACTIVE_JOBS", "quiet_window_start_hour": "SCHEDULER_QUIET_START_HOUR",
                  "quiet_window_end_hour": "SCHEDULER_QUIET_END_HOUR"},
    "policy": {"require_human_approval_for_repo_writes": "REQUIRE_APPROVAL_FOR_REPO_WRITES"},
    "security": {"require_internal_auth": "REQUIRE_INTERNAL_AUTH", "internal_api_token": "INTERNAL_API_TOKEN"},
    "github": {"adapter": "GITHUB_ADAPTER", "default_repository": "GITHUB_DEFAULT_REPOSITORY",
               "allowed_repositories": "GITHUB_ALLOWED_REPOS"},
    "web_search": {"adapter": "WEB_SEARCH_ADAPTER"},
    "managed_worker": {"queue_follow_ups": "MANAGED_WORKER_QUEUE_FOLLOW_UPS"},
    "developer": {"require_preview_before_dispatch": "REQUIRE_DEVELOPER_PREVIEW", "execution_mode": "DEVELOPER_EXECUTION_MODE",
                  "command_timeout_seconds": "DEVELOPER_COMMAND_TIMEOUT_SECONDS", "session_container_image": "DEVELOPER_SESSION_CONTAINER_IMAGE",
                  "session_container_workdir": "DEVELOPER_SESSION_CONTAINER_WORKDIR", "session_container_name_prefix": "DEVELOPER_SESSION_CONTAINER_PREFIX",
                  "session_container_bind_path": "DEVELOPER_SESSION_CONTAINER_BIND_PATH", "session_container_run_as_current_user": "DEVELOPER_SESSION_RUN_AS_CURRENT_USER",
                  "agent_invoke_timeout_seconds": "DEVELOPER_AGENT_INVOKE_TIMEOUT_SECONDS", "enable_mcp_adapters": "DEVELOPER_ENABLE_MCP_ADAPTERS",
                  "enable_agent_live_logs": "DEVELOPER_ENABLE_AGENT_LIVE_LOGS", "mcp_shell_tool_name": "DEVELOPER_MCP_SHELL_TOOL_NAME",
                  "mcp_filesystem_read_tool_name": "DEVELOPER_MCP_FILESYSTEM_READ_TOOL_NAME", "mcp_filesystem_write_tool_name": "DEVELOPER_MCP_FILESYSTEM_WRITE_TOOL_NAME",
                  "state_dir": "DEVELOPER_STATE_DIR", "session_data_volume": "DEVELOPER_SESSION_DATA_VOLUME",
                  "enable_browser": "DEVELOPER_ENABLE_BROWSER", "repository_sources": "DEVELOPER_REPOSITORY_SOURCES"},
}
_SECRET_SETTINGS = {"webhook_secret", "runtime.foundry_api_key", "security.internal_api_token"}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def _read_config_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("Configuration path must be a regular JSON file")
    with path.open("rb") as stream:
        content = stream.read(1048577)
    if len(content) > 1048576:
        raise ValueError("Configuration exceeds the 1 MiB limit")
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Configuration must contain valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object")
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Configuration cannot contain non-finite JSON constants: {value}")


def _file_setting(name: str, value: Any, expected: Any) -> str | None:
    reference = isinstance(value, dict) and set(value) == {"$env"}
    if name in _SECRET_SETTINGS and value is not None and not reference:
        raise ValueError(f"Secret setting requires an environment reference: {name}")
    if reference:
        variable = value["$env"]
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", variable):
            raise ValueError("Secret environment reference must name a variable")
        value = environ.get(variable)
    if value is None and type(None) in get_args(expected):
        return None
    if name == "github.allowed_repositories":
        if not isinstance(value, list) or not all(isinstance(item, str) and item and "," not in item for item in value):
            raise ValueError("github.allowed_repositories must be an array of repository names")
        return ",".join(value)
    if name == "developer.repository_sources":
        if not isinstance(value, list):
            raise ValueError("developer.repository_sources must be an array")
        return json.dumps(value, allow_nan=False)
    valid = (expected is bool and type(value) is bool or expected is int and type(value) is int or
             expected is str and isinstance(value, str) or expected == str | None and isinstance(value, str) or
             expected == int | None and type(value) is int)
    if not valid:
        raise ValueError(f"Configuration setting has the wrong type: {name}")
    return json.dumps(value) if type(value) is bool else str(value)


def _manifest_values() -> tuple[dict[str, str], ApprovalPolicy, OrganizationDefinition | None]:
    values: dict[str, str] = {}
    approvals: dict[str, Any] = {}
    organization = None
    selected = environ.get("AITOBUILD_CONFIG_FILE")
    if selected is not None:
        if not selected.strip():
            raise ValueError("AITOBUILD_CONFIG_FILE must name a JSON file")
        path = Path(selected)
        manifest = _read_config_json(path)
        if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            raise ValueError("Central configuration requires schema_version 1")
        if set(manifest) - {"schema_version", "settings", "approvals", "organization", "organization_file"}:
            raise ValueError("Unknown central configuration field")
        settings = manifest.get("settings", {})
        if not isinstance(settings, dict) or set(settings) - {*_CONFIG_ENV, "webhook_secret"}:
            raise ValueError("Unknown application settings section")
        if "webhook_secret" in settings:
            secret = _file_setting("webhook_secret", settings["webhook_secret"], str)
            if secret is not None:
                values["AITOBUILD_WEBHOOK_SECRET"] = secret
        for section, names in _CONFIG_ENV.items():
            group = settings.get(section, {})
            if not isinstance(group, dict) or set(group) - names.keys():
                raise ValueError(f"Unknown configuration field in {section}")
            hints = get_type_hints(_CONFIG_CLASSES[section])
            for name, value in group.items():
                encoded = _file_setting(f"{section}.{name}", value, hints[name])
                if encoded is not None:
                    values["AITOBUILD_" + names[name]] = encoded
        approvals = manifest.get("approvals", {})
        if not isinstance(approvals, dict):
            raise ValueError("Approval policy must be an object")
        if "organization" in manifest and "organization_file" in manifest:
            raise ValueError("Choose inline organization or organization_file, not both")
        if "organization" in manifest:
            organization = OrganizationDefinition.model_validate(manifest["organization"])
        if "organization_file" in manifest:
            reference = manifest["organization_file"]
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError("organization_file must name a JSON definition")
            organization = OrganizationDefinition.model_validate(_read_config_json(path.parent / reference))
    for stage in ApprovalStage:
        override = environ.get("AITOBUILD_APPROVAL_" + stage.value.upper())
        if override is not None:
            approvals[stage.value] = override
    return {**values, **environ}, ApprovalPolicy.model_validate(approvals), organization


def _as_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Boolean configuration must be true or false")


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


def _parse_repository_sources(raw: str) -> tuple[RepositorySourceConfig, ...]:
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("AITOBUILD_DEVELOPER_REPOSITORY_SOURCES must be a JSON array") from error
    if not isinstance(items, list):
        raise ValueError("AITOBUILD_DEVELOPER_REPOSITORY_SOURCES must be a JSON array")
    sources: list[RepositorySourceConfig] = []
    for item in items:
        if (not isinstance(item, dict) or not {"repository", "repository_id", "path"} <= set(item)
            or set(item) - {"repository", "repository_id", "path", "verification_commands", "base_revision"}):
            raise ValueError("Repository sources require repository, repository_id and path")
        repository, repository_id, path = item["repository"], item["repository_id"], item["path"]
        if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Repository source name must be owner/repository")
        if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id < 1:
            raise ValueError("Repository source ID must be a positive integer")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("Repository source path must be an absolute local path")
        if any(source.repository == repository.lower() or source.repository_id == repository_id for source in sources):
            raise ValueError("Repository source names and IDs must be unique")
        commands = item.get("verification_commands", [])
        if (not isinstance(commands, list) or len(commands) > 16
            or not all(isinstance(command, str) and command.strip() and "\x00" not in command
                   and len(command.encode("utf-8")) <= 8192 for command in commands)):
            raise ValueError("verification_commands must contain at most 16 nonempty command strings of at most 8192 bytes")
        base = item.get("base_revision")
        if base is not None and (not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base)):
            raise ValueError("Repository source base_revision must be a resolved lowercase commit SHA")
        sources.append(RepositorySourceConfig(repository.lower(), repository_id, path, tuple(commands), base))
    return tuple(sources)


def _parse_github_allowed_repos(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ()
    repos: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        repo = part.strip().lower()
        if not repo:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError(
                "AITOBUILD_GITHUB_ALLOWED_REPOS entries must be owner/name "
                "(letters, digits, underscore, hyphen, dot)"
            )
        if repo not in seen:
            seen.add(repo)
            repos.append(repo)
    return tuple(repos)


def _normalize_repository_name(repository: str) -> str:
    cleaned = repository.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", cleaned):
        raise ValueError("repository must be owner/name")
    return cleaned


def load_config() -> AppConfig:
    values, approvals, organization = _manifest_values()
    getenv = values.get
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
        ),
    )

    github_default_repository = getenv("AITOBUILD_GITHUB_DEFAULT_REPOSITORY")
    default_repository = (
        _normalize_repository_name(github_default_repository)
        if github_default_repository and github_default_repository.strip()
        else None
    )
    # Placeholder; allowlist is finalized after developer repository_sources are known.
    github_adapter_mode = (getenv("AITOBUILD_GITHUB_ADAPTER", "mock") or "mock").strip() or "mock"
    github_allowed_from_env = _parse_github_allowed_repos(getenv("AITOBUILD_GITHUB_ALLOWED_REPOS"))
    web_search = WebSearchConfig(
        adapter=(getenv("AITOBUILD_WEB_SEARCH_ADAPTER", "mock") or "mock").strip() or "mock",
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
        session_container_image=getenv("AITOBUILD_DEVELOPER_SESSION_CONTAINER_IMAGE", "aitobuild-developer:local")
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
        state_dir=getenv("AITOBUILD_DEVELOPER_STATE_DIR", ".aitobuild/developer").strip(),
        session_data_volume=getenv("AITOBUILD_DEVELOPER_SESSION_DATA_VOLUME", "").strip() or None,
        enable_browser=_as_bool(getenv("AITOBUILD_DEVELOPER_ENABLE_BROWSER"), default=False),
        repository_sources=_parse_repository_sources(getenv("AITOBUILD_DEVELOPER_REPOSITORY_SOURCES", "[]")),
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
    if not developer.state_dir:
        raise ValueError("AITOBUILD_DEVELOPER_STATE_DIR must be non-empty")
    if developer.enable_browser and developer.execution_mode != "container_session":
        raise ValueError("AITOBUILD_DEVELOPER_ENABLE_BROWSER requires container_session mode")

    if scheduler.max_concurrent_proactive_jobs < 1:
        raise ValueError("AITOBUILD_MAX_PROACTIVE_JOBS must be >= 1")

    if (scheduler.quiet_window_start_hour is None) != (scheduler.quiet_window_end_hour is None):
        raise ValueError(
            "AITOBUILD_SCHEDULER_QUIET_START_HOUR and AITOBUILD_SCHEDULER_QUIET_END_HOUR must both be set"
        )

    allowed_repositories: list[str] = list(github_allowed_from_env)
    seen_repos = set(allowed_repositories)
    if default_repository is not None and default_repository not in seen_repos:
        allowed_repositories.append(default_repository)
        seen_repos.add(default_repository)
    for source in developer.repository_sources:
        if source.repository not in seen_repos:
            allowed_repositories.append(source.repository)
            seen_repos.add(source.repository)

    github = GitHubConfig(
        adapter=github_adapter_mode,
        default_repository=default_repository,
        allowed_repositories=tuple(allowed_repositories),
    )

    if github.adapter not in {"mock", "gh", "gh_cli", "cli"}:
        raise ValueError("AITOBUILD_GITHUB_ADAPTER must be mock or gh_cli")
    if github.adapter in {"gh", "gh_cli", "cli"} and not github.allowed_repositories:
        raise ValueError(
            "AITOBUILD_GITHUB_ALLOWED_REPOS (or default/repository sources) is required for gh_cli"
        )
    if web_search.adapter not in {"mock", "duckduckgo", "ddg", "live"}:
        raise ValueError("AITOBUILD_WEB_SEARCH_ADAPTER must be mock or duckduckgo")

    return AppConfig(
        webhook_secret=webhook_secret,
        runtime=runtime,
        scheduler=scheduler,
        policy=policy,
        security=security,
        developer=developer,
        github=github,
        web_search=web_search,
        approvals=approvals,
        organization=organization,
        managed_worker=ManagedWorkerConfig(queue_follow_ups=_as_bool(
            getenv("AITOBUILD_MANAGED_WORKER_QUEUE_FOLLOW_UPS"), default=False)),
    )
