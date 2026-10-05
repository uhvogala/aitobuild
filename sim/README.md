# Local Simulation Kit

This folder provides a repeatable local workflow to exercise the aitobuild
Developer pipeline end-to-end without GitHub and without using your real
repository as the execution target.

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
4. Sends a signed webhook event (preview gate expected).
5. Creates and approves a developer preview.
6. Replays webhook event (expected route: developer.async.webhook).
7. Executes POST /internal/developer/run against the sandbox repo.
8. Optional session mode: starts/stops `/internal/developer/session/*` and runs
  multiple `/internal/developer/run` calls in the same session.
  Commands are never rewritten by the harness; all execution commands come
  directly from `sim/payloads/developer-run.json`.
  The second session run is a no-op probe (`commands=[]`, `file_writes=[]`) to
  validate session reuse and cleanup without mutating command payload behavior.
9. Checks Developer Agent runtime status and attempts agent run only if ready.
10. Writes a JSON report.

## Run

From workspace root:

uv run python sim/run_local_simulation.py

Optional report path:

uv run python sim/run_local_simulation.py --output sim/simulation-report.json

Optional env file:

uv run python sim/run_local_simulation.py --env-file sim/.env.simulation.local

Session mode (persistent container per run):

uv run python sim/run_local_simulation.py --session --env-file sim/.env.simulation.session

Optional explicit session id:

uv run python sim/run_local_simulation.py --session --session-id demo-session-01

If Docker cannot mount the in-container workspace path (common with Docker Desktop host sharing rules),
set `AITOBUILD_DEVELOPER_SESSION_CONTAINER_BIND_PATH` to the host-visible workspace path.
When this variable is set, the simulation harness rebases it automatically to the copied sandbox repo path.

Container sessions run as the current local user by default (`AITOBUILD_DEVELOPER_SESSION_RUN_AS_CURRENT_USER=true`)
to avoid root-owned files in bind-mounted repos. Set it to `false` only when root in the container is explicitly required.

Live-model mode (real model calls, fail-fast if unavailable):

1. Copy `sim/.env.simulation.live.example` to `sim/.env.simulation.live.local` and set
   your real model provider values.
2. Run:

uv run python sim/run_local_simulation.py \
  --live-model \
  --session \
  --env-file sim/.env.simulation.live.local \
  --agent-max-approval-rounds 12 \
  --live-output \
  --auto-approve-agent-tools

Optional custom prompt:

uv run python sim/run_local_simulation.py --live-model --agent-prompt "Implement a tiny improvement and explain it"

Complex prompt example (forces multi-file edits + test run):

uv run python sim/run_local_simulation.py \
  --live-model \
  --session \
  --env-file sim/.env.simulation.live.local \
  --agent-max-approval-rounds 20 \
  --live-output \
  --auto-approve-agent-tools \
  --agent-prompt "Perform a non-trivial refactor in the sandbox repo: (1) update src/demo_app/math_ops.py to include add, subtract, increment, and decrement with clear docstrings; (2) create src/demo_app/advanced_ops.py with multiply and divide_checked (raise ValueError on divide-by-zero), and update src/demo_app/__init__.py exports; (3) expand tests/test_math_ops.py and add tests/test_advanced_ops.py with normal and error-path coverage; (4) run pytest -q tests; (5) summarize exactly which files changed and include command exit code plus key test output lines."

Useful live debugging flags:

- `--live-output`: print endpoint responses as each step completes.
  This also sets `AITOBUILD_DEVELOPER_ENABLE_AGENT_LIVE_LOGS=true` for the in-process app.
- `--agent-max-approval-rounds N`: raise or lower auto-approval loop cap for
  `/internal/developer/agent/run` (valid range: 1..20).

If `developer_agent_run.output_text` says `Let me try to start a session first`,
the model is asking to call the `developer_start_session` tool before continuing.
If `approval_round_limit_reached=true`, increase `--agent-max-approval-rounds`
or run another turn to continue from the returned `session_id`.

## Notes

- This workflow does not call GitHub APIs.
- It does not run against your real workspace repo files.
- Session mode requires docker daemon access.
- If session mode needs tool bootstrap, add it explicitly to
  `sim/payloads/developer-run.json` commands.
- Live-model mode requires a working Foundry configuration and native runnable
  Developer agent handle; simulation exits early if either is missing.
- If runtime_status.ready_for_run is false, Developer Agent native execution is
  not currently bound in your environment; the simulation still validates the
  preview and constrained developer execution pipeline.
