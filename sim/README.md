# Local Simulation Kit

This folder provides a repeatable local workflow to exercise the aitobuild
Developer preview/execution pipeline without GitHub and without using your real
repository as the execution target.

It does not validate issue-to-PR delivery or complete product-team automation.
The subprocess baseline was verified on 2026-10-05; Docker, live-model, and MCP
paths remain integration checks in [MILESTONES.md](../MILESTONES.md).

## What it includes

- .env.simulation: runnable default environment values.
- .env.simulation.example: starter template for local overrides.
- repo-fixture/: fake repository copied into a temporary sandbox for each run.
- payloads/: webhook, preview, and developer-run request payloads.
- run_local_simulation.py: main simulation harness.

## What the harness does

1. Loads AITOBUILD_* variables from the env file.
2. Copies repo-fixture into a temporary sandbox directory.
3. Starts the app in-process via FastAPI TestClient.
4. Checks Developer Agent runtime status; live mode requires a native runnable handle.
5. Sends a signed webhook event (preview gate expected).
6. Creates and approves a developer preview, then sends a matching second delivery (expected route: developer.async.webhook).
7. Executes POST /internal/developer/run against the sandbox repo using installed pytest, without a pip bootstrap.
8. Optional session mode: starts/stops `/internal/developer/session/*` and runs
  multiple `/internal/developer/run` calls in the same session.
  Commands are never rewritten by the harness; all execution commands come
  directly from `sim/payloads/developer-run.json`.
  The second session run is a no-op probe (`commands=[]`, `file_writes=[]`) to
  validate session reuse and cleanup without mutating command payload behavior.
9. Attempts an agent run only if ready (required in live-model mode).
10. Writes a JSON report with `succeeded`, then exits nonzero for execution failures, incomplete agent runs, or failed cleanup.

## Local Baseline

Prerequisites: Python 3.14+, `uv`, and the project's dev dependencies.
From the workspace root on Linux/macOS:

```bash
uv sync
env -u AITOBUILD_FOUNDRY_ENDPOINT -u AITOBUILD_FOUNDRY_API_KEY \
  uv run python sim/run_local_simulation.py --output sim/simulation-report.json
```

Clearing inherited provider settings keeps this run model-free. In PowerShell,
remove those two environment variables before running the Python command.
The harness loads env-file keys over inherited values, but keys absent from the
file remain inherited. Do not rely on `ALLOW_MOCK_MODEL=true` to prohibit live calls.

Require these values in the report:
- `runtime_status.runtime_mode`: `mock`.
- `runtime_status.ready_for_run`: `false` (expected without a live provider).
- `webhook_initial.route`: `developer.preview_required`.
- `webhook_replayed.route`: `developer.async.webhook`.
- `developer_run.accepted`: `true`, with command exit codes `0`.
- `generated_file_exists`: `true`.
- `succeeded`: `true` and process exit status `0`.

The fixture currently has one test. Passing it proves that the local execution
path works, not that a model implemented a feature. The run endpoint executes
commands before the requested file writes; verify again after actual code edits.

Optional env file (create local overrides from the example first):

```bash
uv run python sim/run_local_simulation.py --env-file sim/.env.simulation.local
```

Without `--output`, the report stays under the generated sandbox directory.
Sandbox directories are retained for inspection, not deleted automatically.

## Docker Sessions

Prerequisites: Docker daemon access, a host-visible bind mount path, and a
session image containing Python and pytest. The checked-in session configuration
selects `python:3.14-slim`, which does **not** include pytest. Prepare an image
with its toolchain and CA trust before attempting the session acceptance check.
Use `uv` for installing Python tools in that image; do not depend on host `.venv`
packages being available inside the session container. Dev container CA trust
is not automatically inherited by a separate session image.

Use [sim/.env.simulation.session](.env.simulation.session) as the basis for
an ignored local session env file, and set
`AITOBUILD_DEVELOPER_SESSION_CONTAINER_IMAGE` to your prepared image there:

```bash
uv run python sim/run_local_simulation.py --session --env-file sim/.env.simulation.session.local
```

Optional explicit session id:

```bash
uv run python sim/run_local_simulation.py --session \
  --env-file sim/.env.simulation.session.local --session-id demo-session-01
```

If Docker cannot mount the in-container workspace path (common with Docker Desktop host sharing rules),
set `AITOBUILD_DEVELOPER_SESSION_CONTAINER_BIND_PATH` to the host-visible workspace path.
When this variable is set, the simulation harness rebases it automatically to the copied sandbox repo path.

Container sessions run as the current local user by default (`AITOBUILD_DEVELOPER_SESSION_RUN_AS_CURRENT_USER=true`)
to avoid root-owned files in bind-mounted repos. Set it to `false` only when root in the container is explicitly required.

## Live Model

This makes billable model calls and can execute tools. Use only a disposable
fixture and trusted prompts; native command policies and manual approval/session
continuation still have gaps. `--auto-approve-agent-tools` grants tool approvals
without per-call human review and is not suitable for untrusted tasks.

1. Use [sim/.env.simulation.live.example](.env.simulation.live.example) for an ignored local env file; set the real Foundry project endpoint/model, disable mock fallback, and select your prepared session image.
2. Authenticate with Entra credentials (for example, `az login` directly in your terminal). Keep `AITOBUILD_FOUNDRY_API_KEY` unset; the configured runtime uses `DefaultAzureCredential`.
3. Meet the Docker prerequisites above, then run:

```bash
uv run python sim/run_local_simulation.py \
  --live-model \
  --session \
  --env-file sim/.env.simulation.live.local \
  --agent-max-approval-rounds 12 \
  --live-output \
  --auto-approve-agent-tools
```

Optional custom prompt:

```bash
uv run python sim/run_local_simulation.py --live-model --session \
  --env-file sim/.env.simulation.live.local --auto-approve-agent-tools \
  --agent-prompt "Implement a tiny improvement in the fixture, run pytest, and report the changed files and test output."
```

Complex prompt example (forces multi-file edits + test run):

```bash
uv run python sim/run_local_simulation.py \
  --live-model \
  --session \
  --env-file sim/.env.simulation.live.local \
  --agent-max-approval-rounds 20 \
  --live-output \
  --auto-approve-agent-tools \
  --agent-prompt "Perform a non-trivial refactor in the sandbox repo: (1) update src/demo_app/math_ops.py to include add, subtract, increment, and decrement with clear docstrings; (2) create src/demo_app/advanced_ops.py with multiply and divide_checked (raise ValueError on divide-by-zero), and update src/demo_app/__init__.py exports; (3) expand tests/test_math_ops.py and add tests/test_advanced_ops.py with normal and error-path coverage; (4) run pytest -q tests; (5) summarize exactly which files changed and include command exit code plus key test output lines."
```

Useful live debugging flags:

- `--live-output`: print endpoint responses as each step completes.
  This also sets `AITOBUILD_DEVELOPER_ENABLE_AGENT_LIVE_LOGS=true` for the in-process app.
- `--agent-max-approval-rounds N`: raise or lower auto-approval loop cap for
  `/internal/developer/agent/run` (valid range: 1..20).

If `developer_agent_run.output_text` says `Let me try to start a session first`,
the model is asking to call the `developer_start_session` tool before continuing.
If `approval_round_limit_reached=true`, increase `--agent-max-approval-rounds`
for a fresh run and inspect the retained sandbox before retrying. The current
HTTP API recreates a native session on each call and cannot reliably replay
manual approval decisions or resume pending work from a returned `session_id`.
An incomplete agent run now makes the simulation fail rather than appearing successful.

Require `runtime_mode=foundry`, `ready_for_run=true`,
`developer_agent_run.completed=true`, no pending approvals, and actual
post-edit test evidence. Readiness alone does not prove provider connectivity
or correct model behavior.

## MCP Status

MCP is disabled in the default simulation. Enabling it requires
`container_session` mode, Agent Framework MCP support, and Node.js/`npx` in the
session image. The adapter launches shell/filesystem server packages through
`docker exec ... npx -y`; package availability, schemas, tool names, and file
paths still need real integration validation. Do not assume a bare Python image
or the current tool-name defaults constitute a working MCP deployment.

## Notes

- This workflow does not call GitHub APIs.
- It does not run against your real workspace repo files.
- Session mode requires docker daemon access.
- Prepare session tools before the run instead of bootstrapping pip on every command.
- Live-model mode requires a working Foundry configuration and native runnable
  Developer agent handle; simulation exits early if either is missing.
- If runtime_status.ready_for_run is false, Developer Agent native execution is
  not currently bound in your environment; the simulation still validates the
  preview and constrained developer execution pipeline.
- Inspect `reason`, command stderr, `pending_approval_requests`, and cleanup outcomes on failure. Reports and live logs can contain prompts/code/provider details; review and redact before sharing.
- Local env overrides, reports, and sandbox directories are ignored by Git. Fixture writes and Docker bind mounts are real; this is not a security sandbox for untrusted input.
