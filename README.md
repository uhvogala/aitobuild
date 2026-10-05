# aitobuild

An agentic product-team framework with Product Manager, Architect, and Developer
roles, built on Microsoft Agent Framework and FastAPI.

Current stage: foundation plus a local Developer execution prototype. This is
not yet an autonomous issue-to-PR service. The next delivery target is one
approved issue producing a tested draft PR in a disposable repository, with a
human retaining merge authority.

## Start here

- [MILESTONES.md](MILESTONES.md): current progress, ordered milestones, and exit criteria.
- [PLAN.md](PLAN.md): target architecture and implementation gaps.
- [sim/README.md](sim/README.md): local simulation and optional Docker/live-model setup.
- [.devcontainer/certs/README.md](.devcontainer/certs/README.md): host certificate setup.
- [agents.md](agents.md): repository conventions for contributors and coding agents.

## Current status (2026-10-05)

| Area | Implemented | Remaining |
| --- | --- | --- |
| Ingress and routing | Signed webhooks, authenticated internal APIs, deduplication, deterministic dispatch | Durable task worker and repository-specific task extraction |
| Developer execution | Preview approval, command/file runs, structured exact-text edits, Docker sessions | Complete issue-to-branch-to-PR delivery |
| Native model runtime | Foundry binding and Developer agent invocation | Reproducible live-model acceptance run and resumable approvals |
| GitHub integration | Webhook input and mock issue-proposal adapter | Real issue, branch, commit, and PR operations |
| Meetings and proactive scans | Lifecycle registry, workflow construction, deterministic scan output | Meeting execution and real repository analysis |
| Operations | Tick endpoint, policy checks, capability-audit tests | Background tick driver, durable state, CI, tracing, stronger isolation |

Verified locally: **180 tests pass**, Ruff and mypy pass. The original patch-repair
failures are fixed without relaxing ambiguous-context rejection. Prepared Docker
sessions, managed terminals/processes, native memory/restart and browser tools
have live integration evidence. Real Grok and Kimi evaluations retain strict
failure reports; neither is yet certified as a zero-error full-suite baseline.

See [agent tool evaluation](sim/README.md#agent-tool-evaluation) for model comparison,
token/cache usage and artifact checks. Large tool outputs are saved to private
files with bounded previews and paged retrieval; oversized API prompts are rejected.

## Quick start

Prerequisites: Python 3.14 or newer, `uv`, and ripgrep (`rg`, 14+). Both checked-in
container images install ripgrep. Run from the repository root.
The dev container also needs Python 3 on the host for certificate export.

```bash
uv sync
```

The `dev` dependency group is enabled by default, so tools like `pytest`, `ruff`, and `mypy` are installed automatically on sync.

Start a local API with model fallback and simulated command execution:

```bash
export AITOBUILD_WEBHOOK_SECRET="local-webhook-secret"
export AITOBUILD_INTERNAL_API_TOKEN="local-internal-token"
export AITOBUILD_REQUIRE_INTERNAL_AUTH="true"
export AITOBUILD_REQUIRE_APPROVAL_FOR_REPO_WRITES="true"
export AITOBUILD_REQUIRE_DEVELOPER_PREVIEW="true"
export AITOBUILD_ALLOW_MOCK_MODEL="true"
export AITOBUILD_DEVELOPER_EXECUTION_MODE="mock"
unset AITOBUILD_FOUNDRY_ENDPOINT AITOBUILD_FOUNDRY_API_KEY
uv run uvicorn aitobuild.server:app --host 127.0.0.1 --port 8000 --reload
```

In another terminal:

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail -H 'X-Internal-Token: local-internal-token' \
	http://127.0.0.1:8000/internal/runtime/developer-agent
```

Expect health status `ok`, runtime mode `mock`, and `ready_for_run=false`.
The readiness endpoint checks native handle availability, not provider
connectivity or successful tool execution. Interactive API schemas are at
`http://127.0.0.1:8000/docs`.

These credentials are local examples only. Do not expose this prototype to
untrusted callers. Mock mode simulates commands but does **not** disable file
writes; use dry runs or the copied-fixture simulation for execution experiments.

## Local simulation (no GitHub, no real repo)

Use the simulation kit in `sim/` to run the Developer workflow end-to-end in a
temporary copied fixture repository.

Run:

```bash
env -u AITOBUILD_FOUNDRY_ENDPOINT -u AITOBUILD_FOUNDRY_API_KEY \
	uv run python sim/run_local_simulation.py --output sim/simulation-report.json
```

Expected: initial route `developer.preview_required`, replay route
`developer.async.webhook`, execution `accepted=true`, command exit code `0`,
`generated_file_exists=true`, and `succeeded=true`. The harness exits nonzero
for failed execution, incomplete agent approval, or failed session cleanup.
The default command uses the pytest installed by `uv sync`; it does not bootstrap pip.

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

For details, see [sim/README.md](sim/README.md).

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
- Agent Framework runtime bootstrap with Foundry project, Azure OpenAI v1 and explicit mock fallback modes.
- Scheduler scaffolding for proactive architect scans and meeting bootstrap events.
- Shared escalation route and sink for consistent human/system escalation outputs.
- Developer isolation bundle contract for scoped software delivery tasks.

## Required environment variables

```bash
export AITOBUILD_WEBHOOK_SECRET="replace-me"
export AITOBUILD_INTERNAL_API_TOKEN="replace-me-internal-token"
```

Both are required with the default internal-auth setting. Disabling internal
auth removes the token requirement, but is only appropriate for isolated tests.
The server does not automatically load simulation env files; the harness does.

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
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_IMAGE="aitobuild-developer:local"
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_WORKDIR="/workspace"
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_PREFIX="aitobuild-dev"
export AITOBUILD_DEVELOPER_SESSION_RUN_AS_CURRENT_USER="true"
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

For Azure OpenAI v1, set `AITOBUILD_FOUNDRY_ENDPOINT` to the resource URL ending
in `/openai/v1` and `AITOBUILD_FOUNDRY_MODEL` to the deployment name. This uses
the native chat-completions client with refreshable Entra auth, or an optional
API key from an ignored local environment file. Runtime mode is `openai`.

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
- `POST /internal/developer/session/stop-all`
- `POST /internal/developer/run`
- `GET /internal/runtime/developer-agent`
- `POST /internal/developer/agent/run`
- `GET /internal/escalations`

All `/internal/*` endpoints require `X-Internal-Token` by default. Webhooks
require `X-GitHub-Event`, `X-GitHub-Delivery`, and an HMAC-SHA256
`X-Hub-Signature-256` computed over the exact request body.

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

These route responses do not launch a Developer worker or open a PR.
Architect scan findings currently derive from supplied counters/flags, not
an autonomous repository inspection.

When `AITOBUILD_REQUIRE_DEVELOPER_PREVIEW=true`, webhook dispatch returns
`developer.preview_required` until a matching preview is approved.

Preview flow:

1. Create a preview via `POST /internal/developer/preview` with
	`github_event`, optional `action`, optional `delivery_id`, and optional `body`.
2. Approve via `POST /internal/developer/preview/approve` with `preview_id`.
3. Inspect pending queue via `GET /internal/developer/previews` (defaults to `pending_only=true`).
4. Deliver the webhook with the preview's matching delivery ID or dedupe key;
	dispatcher then routes to `developer.async.webhook`. An already accepted
	delivery is deduplicated; the simulator uses a second delivery ID for its preview/replay pair.

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

The default command policy covers normal development workflows: Python/uv,
Node package managers, Git, common build/compiler tools, shell scripts,
filesystem inspection/manipulation, downloads and archives. Task-specific
`allowed_command_prefixes` can still narrow this list. Matching uses complete
command tokens and accepts leading environment assignments; it does not parse
heredoc bodies or authorize every command inside a script. Permitting a tool
does not install it. Interpreters and shells can execute arbitrary code, so this
is an ergonomics policy, not a sandbox. Preview approval and existing file-write
checks remain unchanged; unscoped subprocess runs still use the API environment.

The seed repository is the API process working directory, captured at startup.
Session-bound runs copy it once into `.aitobuild/workspaces/<session-id>`; different
Developers have separate checkouts and private home-volume subdirectories.
Unscoped subprocess execution still uses the API working directory. Preview approval
and `approved=true` for live file writes are separate checks. Commands run before
the requested file writes, so a passing command does not validate those later
writes; run verification again after applying changes.

Execution backends:

- `mock`: deterministic simulated commands; file reads/writes still use the real workspace
- `subprocess`: runs commands in the same runtime environment as the API process
- `container_session`: runs commands in a persistent Docker container per session

If you run aitobuild inside a container (for example this dev container), subprocess mode executes inside that container.

Container-session flow:

1. Start session with `POST /internal/developer/session/start` (optional `session_id`).
2. Execute multiple `POST /internal/developer/run` calls using the returned `session_id`.
3. Stop and clean up using `POST /internal/developer/session/stop`.

This keeps one container alive for the whole Developer task session rather than launching one per command.

Build [.devcontainer/Dockerfile.developer](.devcontainer/Dockerfile.developer) as
`aitobuild-developer:local` for the prepared test/browser/terminal toolchain.
Docker sessions bind-mount a private checkout writable and are not a
security boundary for untrusted code.

MCP adapter mode:

- Set `AITOBUILD_DEVELOPER_ENABLE_MCP_ADAPTERS=true` to route Developer command/filesystem tools through MCP stdio clients inside the session container.
- This is fail-fast by design: if MCP dependencies, tool bindings, or session requirements are missing, startup or tool invocation errors immediately.
- Session images must include Node.js/`npx`; the adapters launch shell/filesystem
	servers through `docker exec ... npx -y`. Server packages, schemas, paths, and
	tool names still need integration validation; MCP is optional, not required
	for the verified local baseline.

Agent Framework tool wiring:

- Developer tools are bound per native `Agent.run(..., tools=[...])` to prevent cross-session access and duplicate registration.
- Tool functions are compatible with the `@tool(...)` pattern from the Agent Framework sample `02_add_tools.py` when `agent_framework.tool` is available.
- Targeted edits use `developer_edit_file(path, old_text, new_text)`. The non-empty old text must match exactly once; insertion repeats a unique existing anchor in the replacement. Stale or ambiguous matches leave the file unchanged. No diff syntax, line-prefix repair or newline normalization is involved.
- The legacy `developer_apply_patch` parser remains opt-in through `DeveloperToolContext.use_legacy_patch_tool` for compatibility; it is not advertised to models by default. Use `developer_write_file` for new files or an explicitly requested full-file replacement.
- A focused Grok/Kimi read-edit-test probe passed for both models with zero tool errors and independent pytest verification. This is editing evidence, not full-suite acceptance.
- `developer_find_files` discovers paths with glob filters; `developer_search_files` searches literal text or Rust regex and returns paths, line numbers and byte columns. Both use ripgrep, respect allowed roots and ignore files, and support bounded pages with `next_offset`. Hidden/ignored files are opt-in; content searches skip files over 1 MiB and return bounded line previews. Read selected files before exact-text edits.
- Shell `rg` is available in the prepared image and allowed by preview command policy. Restart existing Developer containers to pick up image changes; private checkouts and home data persist. The workspace dev-container package change takes effect on rebuild.

Developer Agent runtime flow:

1. Check runtime readiness with `GET /internal/runtime/developer-agent`.
2. Execute one turn with `POST /internal/developer/agent/run` and payload:
	- `input` (required string)
	- `session_id` (optional string)
	- `create_session` (optional bool)
	- `auto_approve_tools` (optional bool, default `false`)
	- `max_approval_rounds` (optional int, default `3`)
3. Inspect `pending_approval_requests`, `completed`, and
	`approval_round_limit_reached`. The endpoint can replay approvals within one
	call when `auto_approve_tools=true`, but does not accept manual approval
	responses. Native sessions/history persist by identity, but manual continuation
	of a pending approval is not implemented.

`/internal/developer/agent/run` uses the runtime-bound native Developer Agent when available; in descriptor/mock mode it fails with `409` by design.

To bootstrap a meeting from scheduler ticks, first submit a `meeting_requested` internal trigger
and pass the returned `meeting_id` in `POST /internal/scheduler/tick` with `manual_meeting=true`.
Both internal endpoints require `X-Internal-Token` when internal auth is enabled.
Successful `meeting.bootstrap` dispatches include `metadata.meeting_kickoff` with
`workflow_built` (native GroupChatBuilder path) or `mock_started` (fallback path).

`workflow_built` means constructed, not executed or resolved. The scheduler
only advances when `/internal/scheduler/tick` is called; no background scheduler
loop runs just because `AITOBUILD_SCHEDULER_ENABLED=true`.

If a meeting deadline has passed when a `meeting_due` event arrives, the dispatcher emits
`escalation.route` and sends a structured escalation event to shared sinks.

## Developer Isolation Contract

Use `aitobuild.developer_isolation.build_developer_task_bundle(...)` to create a constrained
Developer task package with:

- objective and acceptance criteria
- workspace-relative context files only
- allowed tools and path/command limits
- declared runtime and file-change budgets

Default policy allows edits under `src/` and `tests/` and blocks paths like `.git/` and `.venv/`.

Current checks are prototype guardrails, not complete sandbox enforcement.
The constrained run endpoint checks command prefixes and file-write counts;
native agent commands do not apply the same bundle command-prefix checks.
Shell commands can have side effects beyond their apparent prefix. The declared
task-wide runtime budget is not enforced as a total wall-clock budget; separate
command and invoke timeouts exist. Policy parity, approval scope, and resource
limits must be addressed before unattended execution.

## State and recovery

Deduplication, previews, meetings, escalation events, and session mappings are
in memory. Restarting the server loses that application state, and reload mode
can invalidate previews. Docker containers may outlive the API process; use
`/internal/developer/session/stop-all` to clean up containers discovered under
the configured name prefix. Native agent session persistence and recovery are
not implemented.

## Repository hygiene

Python bytecode and tool caches are intentionally ignored in git (`__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`).
If you run tests or lint locally, these files may appear on disk, but they should not appear in `git status`.
