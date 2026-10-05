# Agents Notes

## Agent Framework Docs (Check First)

Reference docs:
- https://github.com/microsoft/agent-framework/tree/main/python/samples

Working rule:
- If any Agent Framework behavior or API is unclear, check the docs above before implementing custom logic.
- Prefer native framework capabilities first; only build custom code when there is a confirmed gap.

## Project Basics

Project:
- Name: aitobuild
- Current phase: foundation plus local Developer execution prototype; not yet issue-to-PR automation
- Purpose: agentic product-team framework with PM, Architect, and Developer roles

Project orientation:
- [README.md](README.md): setup, current capabilities, and operational limitations
- [PLAN.md](PLAN.md): target architecture and implementation snapshot
- [MILESTONES.md](MILESTONES.md): ordered delivery milestones and acceptance criteria
- [sim/README.md](sim/README.md): disposable-fixture simulation and optional live-model checks
- Future live GitHub target: https://github.com/uhvogala/aitobuild_example. Documented only; do not activate it until the required M1 controls and M2 delivery path are ready for an explicitly approved trial.

Current foundation scope:
- FastAPI ingress with webhook and internal trigger endpoints
- Unified trigger/event pipeline and deduplication
- Dispatcher routing for webhook, proactive scan, and meeting bootstrap events
- Runtime bootstrap for FoundryChatClient with mock fallback mode
- Scheduler scaffolding for proactive Architect scans and meeting trigger events
- Policy guardrails for role permissions and approval-gated repo writes
- Developer preview/run endpoints, native agent tool wiring, and optional Docker/MCP sessions
- Host certificate export and trust bootstrap before dev container features

Important limits:
- Dispatch returns task metadata; it does not yet drive a GitHub branch/PR worker.
- GitHub writes are mock-only; meetings are constructed, not executed.
- Scheduler ticks require a caller; meeting/scheduler state and non-repository trigger dedupe remain in memory. Previews, approved scope/base, repository issue identity/delivery aliases and metadata dispatch markers persist locally under the Developer state directory.
- Repository issues opened/assigned/edited require explicit human approval and an operator-supplied base SHA. Criteria must be listed under a Markdown Acceptance Criteria heading. Changed scope requires a new approved task; duplicate delivery/restart cannot replace approval. Repository issue execution is blocked until the disposable target-checkout worker exists; no publication operations are implemented.
- Preview-bound native tasks persist policy, unique-path pre-write reservations and a shared absolute deadline. Use the constrained Docker profile: read-only repo/root, offline commands and interactive input, dropped capabilities, CPU/memory/PID limits, expiry and terminal-outcome cleanup. Browser/arbitrary MCP adapters and legacy execution of native-bound previews are rejected. Standalone/unbound execution remains a prototype; do not call this a hardened hostile-tenant sandbox.
- Native pending approvals and sessions persist locally; distributed coordination, durable task/approval auditing, and PM/Architect tool wiring remain pending.

Key endpoints:
- GET /health
- POST /webhook
- POST /internal/triggers
- POST /internal/developer/preview and /internal/developer/preview/approve
- POST /internal/developer/run
- GET /internal/runtime/developer-agent
- POST /internal/developer/agent/run
- POST /internal/developer/agent/resume
- POST /internal/developer/session/start, /stop, and /stop-all

Internal endpoints require `X-Internal-Token` by default. Normal startup requires
both `AITOBUILD_WEBHOOK_SECRET` and `AITOBUILD_INTERNAL_API_TOKEN`.

Quality gates (from repository root):
- `uv run pytest`
- `uv run ruff check .`
- `uv run mypy src`

The GitHub Actions baseline workflow uses Python 3.14 and `uv sync --locked`,
installs ripgrep, runs these gates and the mock fixture simulation, and retains
available reports on failure. Hosted run 37308813245 passed at `c6edaef` with
180 tests and a retained simulation artifact; M0 is accepted.

Verification snapshot (2026-10-05): 242 local tests pass; Ruff, mypy and the mock
copied-fixture simulation pass. The M2 extraction/durable preparation slice has
restart/concurrency/immutable-scope regressions, but no delivery worker, target
checkout or publication. Hosted CI has not run this slice.
M1 constrained native Grok/Kimi approved-task fixtures pass with actual tests,
persisted budgets, zero unexpected errors and automatic cleanup. M2 delivery is next;
the designated live GitHub repository remains inactive.
Live Grok/Kimi tool evaluation remains stricter than tool coverage: require
artifact correctness, completion, successful cleanup and zero unexpected errors.

Local workflow:
- Run `uv sync` with Python 3.14+.
- Use `uv run python sim/run_local_simulation.py --output sim/simulation-report.json`
	for copied-fixture execution; clear Foundry endpoint/API-key settings for a mock-only run.
- Require `succeeded=true` and zero command exit codes; do not infer success from HTTP status alone.
- Session-bound operations use private copied checkouts; unscoped subprocess runs use the API working directory.
- Pending native approvals resume via `/internal/developer/agent/resume` with the session ID, saved request ID and boolean decision. Decisions are consumed before execution; failed/interrupted continuations are not blindly replayable. Approval tests cover the native SDK with a mocked model transport, not full live acceptance.
- Native approved-task runs take `preview_id` on a fresh session. The server snapshots the approved bundle; later runs/resumes restore it even if the preview registry was lost. Caller-supplied policies and switching tasks in that session are rejected. No-preview runs retain prototype behavior.
- Use `uv run python -m sim.evaluate_tools --model <deployment>` for live tool evaluations.
- Models use `developer_edit_file(path, old_text, new_text)` for exact unique-span edits. Legacy patch parsing is opt-in, not a model-facing default; do not repair ambiguous matches or bypass stale edits with whole-file writes.
- Ripgrep is installed in both images. Use `developer_find_files` for glob discovery and `developer_search_files` for bounded literal/regex content search; prefer narrow paths/globs and reuse `next_offset` only when more results are needed.
- Preview command defaults cover normal multi-language development and shell workflows; explicit task allowlists remain configurable. Token-prefix checks are an ergonomics filter, not shell isolation, and are separate from native-agent command permissions.
- Large tool results are spilled to session-private outputs with bounded paging; API prompts over 32,000 UTF-8 bytes are rejected.
- Keep generated host certificates, reports, sandbox copies, and local credential files out of Git.

## Dependency Management (uv)

- Use `uv` for all Python dependency operations in this repo.
- Add packages with `uv add <package>`.
- Update installed environment with `uv sync` after dependency changes.
- Do not use ad-hoc `pip install` for project dependencies.
