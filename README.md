# aitobuild

Phase 1 foundation for the agent framework project.

## Quick start

```bash
uv sync
```

The `dev` dependency group is enabled by default, so tools like `pytest`, `ruff`, and `mypy` are installed automatically on sync.

## Local simulation (no GitHub, no real repo)

Use the simulation kit in `sim/` to run the Developer workflow end-to-end in a
temporary copied fixture repository.

Run:

```bash
uv run python sim/run_local_simulation.py
```

Optional:

```bash
uv run python sim/run_local_simulation.py --output sim/simulation-report.json
uv run python sim/run_local_simulation.py --env-file sim/.env.simulation.local
```

The simulation kit includes:

- `sim/.env.simulation` with required `AITOBUILD_*` keys for local runs.
- `sim/repo-fixture/` as the fake test repository.
- `sim/payloads/` sample request payloads.
- `sim/run_local_simulation.py` as the end-to-end harness.

For details, see `sim/README.md`.

## Useful commands

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv run uvicorn aitobuild.server:app --reload
```

## Foundation capabilities

- Unified trigger pipeline for webhook and internal events.
- FastAPI ingress with GitHub webhook signature verification.
- Trigger deduplication and deterministic dispatcher routing.
- Agent Framework runtime bootstrap with Foundry-first and mock fallback modes.
- Scheduler scaffolding for proactive architect scans and meeting bootstrap events.
- Shared escalation route and sink for consistent human/system escalation outputs.
- Developer isolation bundle contract for scoped software delivery tasks.

## Required environment variables

```bash
export AITOBUILD_WEBHOOK_SECRET="replace-me"
```

Optional runtime flags:

```bash
export AITOBUILD_FOUNDRY_ENDPOINT="https://example.foundry.azure.com"
# Foundry project endpoints use Entra credentials in this runtime path.
# Keep AITOBUILD_FOUNDRY_API_KEY unset and authenticate via Azure credentials.
export AITOBUILD_FOUNDRY_MODEL="gpt-4.1"
export AITOBUILD_ALLOW_MOCK_MODEL="true"
export AITOBUILD_SCHEDULER_ENABLED="true"
export AITOBUILD_SCHEDULER_KILL_SWITCH="false"
export AITOBUILD_ARCHITECT_SCAN_CRON="0 8 * * *"
export AITOBUILD_MEETING_TICK_CRON="*/30 * * * *"
export AITOBUILD_MAX_PROACTIVE_JOBS="2"
export AITOBUILD_SCHEDULER_QUIET_START_HOUR=""
export AITOBUILD_SCHEDULER_QUIET_END_HOUR=""
export AITOBUILD_REQUIRE_APPROVAL_FOR_REPO_WRITES="true"
export AITOBUILD_REQUIRE_INTERNAL_AUTH="true"
export AITOBUILD_INTERNAL_API_TOKEN="replace-me-internal-token"
export AITOBUILD_REQUIRE_DEVELOPER_PREVIEW="false"
export AITOBUILD_DEVELOPER_EXECUTION_MODE="mock"
export AITOBUILD_DEVELOPER_COMMAND_TIMEOUT_SECONDS="120"
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_IMAGE="python:3.14-slim"
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_WORKDIR="/workspace"
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_PREFIX="aitobuild-dev"
# Optional host-visible path for Docker bind mount source in container_session mode.
# Leave unset to use the current process workspace path.
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_BIND_PATH=""
# Max seconds to wait for each /internal/developer/agent/run invoke cycle.
export AITOBUILD_DEVELOPER_AGENT_INVOKE_TIMEOUT_SECONDS="180"
export AITOBUILD_DEVELOPER_ENABLE_MCP_ADAPTERS="false"
export AITOBUILD_DEVELOPER_ENABLE_AGENT_LIVE_LOGS="false"
export AITOBUILD_DEVELOPER_MCP_SHELL_TOOL_NAME="execute_command"
export AITOBUILD_DEVELOPER_MCP_FILESYSTEM_READ_TOOL_NAME="read_file"
export AITOBUILD_DEVELOPER_MCP_FILESYSTEM_WRITE_TOOL_NAME="write_file"
```

## API endpoints

- `GET /health`
- `POST /webhook`
- `POST /internal/triggers`
- `POST /internal/scheduler/tick`
- `POST /internal/developer/preview`
- `POST /internal/developer/preview/approve`
- `GET /internal/developer/previews`
- `POST /internal/developer/session/start`
- `POST /internal/developer/session/stop`
- `POST /internal/developer/run`
- `GET /internal/runtime/developer-agent`
- `POST /internal/developer/agent/run`
- `GET /internal/escalations`

Example internal trigger payload:

```json
{
	"origin": "manual_request",
	"event_type": "architect_scan_requested",
	"payload": {"repo": "aitobuild"}
}
```

`architect.proactive.scan` responses include `metadata.architect_scan` with
structured findings and issue proposal suggestions.
`developer.async.webhook` responses include `metadata.developer_task_bundle`
for isolated software delivery execution.

When `AITOBUILD_REQUIRE_DEVELOPER_PREVIEW=true`, webhook dispatch returns
`developer.preview_required` until a matching preview is approved.

Preview flow:

1. Create a preview via `POST /internal/developer/preview` with
	`github_event`, optional `action`, optional `delivery_id`, and optional `body`.
2. Approve via `POST /internal/developer/preview/approve` with `preview_id`.
3. Inspect pending queue via `GET /internal/developer/previews` (defaults to `pending_only=true`).
4. Re-deliver the webhook; dispatcher then routes to `developer.async.webhook`.

Developer practical run flow:

1. Create and approve a preview (`/internal/developer/preview` and `/internal/developer/preview/approve`).
2. Execute a constrained Developer run via `POST /internal/developer/run` with:
	- `preview_id` (required)
	- `commands` (optional list)
	- `file_writes` (optional list of `{path, content}`)
	- `dry_run` (optional, default `true`)
	- `approved` (optional, used for non-dry-run repo writes)
3. Inspect `command_outcomes` and `file_write_outcomes` in the response.

`/internal/developer/run` enforces the bundle's isolation policy for command prefixes and file paths.

Execution backends:

- `mock`: deterministic simulated command execution (safe default)
- `subprocess`: runs commands in the same runtime environment as the API process
- `container_session`: runs commands in a persistent Docker container per session

If you run aitobuild inside a container (for example this dev container), subprocess mode executes inside that container.

Container-session flow:

1. Start session with `POST /internal/developer/session/start` (optional `session_id`).
2. Execute multiple `POST /internal/developer/run` calls using the returned `session_id`.
3. Stop and clean up using `POST /internal/developer/session/stop`.

This keeps one container alive for the whole Developer task session rather than launching one per command.

MCP adapter mode:

- Set `AITOBUILD_DEVELOPER_ENABLE_MCP_ADAPTERS=true` to route Developer command/filesystem tools through MCP stdio clients inside the session container.
- This is fail-fast by design: if MCP dependencies, tool bindings, or session requirements are missing, startup or tool invocation errors immediately.

Agent Framework tool wiring:

- Runtime now provides role-specific tools to agents using `Agent(..., tools=[...])`.
- Tool functions are compatible with the `@tool(...)` pattern from the Agent Framework sample `02_add_tools.py` when `agent_framework.tool` is available.
- Current Developer tools include command execution, file read/write, context-matched patch apply, and session start/stop operations.
- `developer_apply_patch` accepts either the custom patch format (`*** Update File:` body with optional `*** Begin/End Patch` wrapper) or unified diff update format (`---`/`+++` with `@@` hunks).

Developer Agent runtime flow:

1. Check runtime readiness with `GET /internal/runtime/developer-agent`.
2. Execute one turn with `POST /internal/developer/agent/run` and payload:
	- `input` (required string)
	- `session_id` (optional string)
	- `create_session` (optional bool)
	- `auto_approve_tools` (optional bool, default `false`)
	- `max_approval_rounds` (optional int, default `3`)
3. If approvals are not auto-approved, inspect `pending_approval_requests` in the response and replay with another call.

`/internal/developer/agent/run` uses the runtime-bound native Developer Agent when available; in descriptor/mock mode it fails with `409` by design.

To bootstrap a meeting from scheduler ticks, first submit a `meeting_requested` internal trigger
and pass the returned `meeting_id` in `POST /internal/scheduler/tick` with `manual_meeting=true`.
Both internal endpoints require `X-Internal-Token` when internal auth is enabled.
Successful `meeting.bootstrap` dispatches include `metadata.meeting_kickoff` with
`workflow_built` (native GroupChatBuilder path) or `mock_started` (fallback path).

If a meeting deadline has passed when a `meeting_due` event arrives, the dispatcher emits
`escalation.route` and sends a structured escalation event to shared sinks.

## Developer Isolation Contract

Use `aitobuild.developer_isolation.build_developer_task_bundle(...)` to create a constrained
Developer task package with:

- objective and acceptance criteria
- workspace-relative context files only
- allowed tools and path/command limits
- bounded runtime and file-change caps

Default policy allows edits under `src/` and `tests/` and blocks paths like `.git/` and `.venv/`.

## Repository hygiene

Python bytecode and tool caches are intentionally ignored in git (`__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`).
If you run tests or lint locally, these files may appear on disk, but they should not appear in `git status`.
