# Local Simulation Kit

This folder provides a repeatable local workflow to exercise the aitobuild
Developer preview/execution pipeline without GitHub and without using your real
repository as the execution target.

It does not validate issue-to-PR delivery or complete product-team automation.
The subprocess baseline was verified on 2026-10-05; Docker, live-model, and MCP
paths remain integration checks in [MILESTONES.md](../MILESTONES.md).

## Future Real Repository

[uhvogala/aitobuild_example](https://github.com/uhvogala/aitobuild_example) is
reserved for future supervised live GitHub trials. This harness does not target
or modify it. Keep using copied fixtures until the M1 controls and M2 delivery
path are ready for an explicitly approved trial, then review its repository
layout/toolchain and use scoped branches, draft PRs and least-privilege access.

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
session image containing Python and pytest. Build the checked-in prepared image:

```bash
docker build -f .devcontainer/Dockerfile.developer -t aitobuild-developer:local .
```

This includes Python tooling, ripgrep, tmux, psutil, Node.js and Chromium/Playwright MCP.
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
fixture and trusted prompts; native task-policy parity and distributed recovery
still have gaps. `--auto-approve-agent-tools` grants tool approvals
without per-call human review and is not suitable for untrusted tasks.

1. Use [sim/.env.simulation.live.example](.env.simulation.live.example) for an ignored local env file; set a Foundry project or Azure `/openai/v1` endpoint, deployment name, and prepared image. Disable mock fallback.
2. Authenticate with Entra credentials (for example, `az login` directly in your terminal). Project endpoints require Entra; v1 endpoints also permit an API key stored only in an ignored local env file.
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
HTTP API persists native sessions, history and memory by `session_id`, and prevents
concurrent agent runs with the same identity. Pending approvals persist locally
and can be approved or rejected through `/internal/developer/agent/resume` with
`session_id`, `request_id` and a strict boolean `approved`, using the internal
token. It restores the saved operation, not caller-supplied arguments. Consumed
decisions cannot be replayed even after failure; distributed recovery remains
pending. The harness's automatic approval flag still bypasses per-call review.
An incomplete agent run now makes the simulation fail rather than appearing successful.

Require `runtime_mode=foundry` or `openai`, `ready_for_run=true`,
`developer_agent_run.completed=true`, no pending approvals, and actual
post-edit test evidence. Readiness alone does not prove provider connectivity
or correct model behavior.

## Agent Tool Evaluation

[evaluate_tools.py](evaluate_tools.py) reuses the copied-fixture simulation and
native Developer API. It makes billable model calls and auto-approves tools only
inside disposable Developer workspaces. It does not validate GitHub delivery.

```bash
uv run python -m sim.evaluate_tools --model grok-4.6 \
  --output sim/.run-artifacts/grok-tool-evaluation.json
uv run python -m sim.evaluate_tools --model Kimi-K2.7-Code \
  --invoke-timeout 360 --output sim/.run-artifacts/kimi-tool-evaluation.json
```

The default env file is the live example with the deployed Azure v1 endpoint.
Use `--env-file` for ignored local overrides. Use `--no-browser` to explicitly
exclude browser coverage; `--recovery` injects exactly one expected stale-text edit error.
Normal runs require zero unexpected tool errors, even if a retry later succeeds.

Eight stages cover file editing and independent pytest verification, detached
terminals/input/process signals, all seven native memory tools, app restart,
large-output paging, all 14 browser tools, Developer isolation and session stop.
Reports include tool traces, required/missing coverage, artifact checks, errors,
approval rounds, elapsed time and provider token/cache usage. Repeated tool calls
include legitimate reads and lifecycle operations, not just retries. Provider
timeouts/rate limits and incomplete approvals fail the stage without being
misrepresented as successful tool execution. Reports are saved after each stage.

The 2026-10-05 Kimi run passed 6/8 stages with 83% recorded tool coverage and
successful cleanup. Its recovered context-free patch and native-memory timeout
failed strict acceptance. Timeout responses currently omit partial traces/usage;
coverage and token totals can therefore undercount that stage. Earlier Grok
reports used an older harness and are not a controlled model comparison.

The default editing contract is now `developer_edit_file(path, old_text, new_text)`:
an exact unique replacement, not model-generated patch syntax. The legacy parser
is opt-in only. A focused same-task Grok/Kimi probe on 2026-10-05 passed both models
on their first edit attempt with zero tool errors, independent fixture pytest
verification and successful cleanup. The full suite has not been rerun with
this contract; historical patch failures remain in their original reports.

The file stage now also requires `developer_find_files` and `developer_search_files`,
checking the discovered path and definition line rather than model self-report.
Focused Grok/Kimi Docker-backed search probes both passed with zero tool errors,
unchanged source files and successful cleanup. Discovery honors ignore/hidden
settings before glob filtering; content search is literal by default with opt-in
Rust regex and case-insensitive matching. Pages have at most 100 entries, a bounded
byte budget and `next_offset`; line previews are limited to 400 UTF-8 bytes.
Content search skips files over 1 MiB. Queries have a 15-second budget and stay
within the Developer's policy-allowed private checkout; this is not hardened isolation.

Results over 6,000 UTF-8 bytes, oversized errors and tool media are saved in session-private files;
only a bounded preview/output ID reaches the model. `developer_read_output`
retrieves up to 4,000 bytes per page (2,000 default). Command logs remain in the
private persistent home. Prompts over 32,000 UTF-8 bytes return HTTP 413 before
model invocation. Native `FileHistoryProvider`, `FileSessionStore`, file memory
and history-aware compaction retain task state; chat completions still sends
relevant history, with provider-side prefix caching where available. These
limits are conservative context guards, not a hardened sandbox or secret store.

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
