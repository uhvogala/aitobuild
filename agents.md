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
- Scheduler ticks require a caller; application state is in memory.
- Native agent commands and preview-run policies are not equivalent. Do not describe prototype checks or Docker bind mounts as hardened isolation.
- PM and Architect tool wiring, durable approvals, and session recovery remain pending.

Key endpoints:
- GET /health
- POST /webhook
- POST /internal/triggers
- POST /internal/developer/preview and /internal/developer/preview/approve
- POST /internal/developer/run
- GET /internal/runtime/developer-agent
- POST /internal/developer/agent/run
- POST /internal/developer/session/start, /stop, and /stop-all

Internal endpoints require `X-Internal-Token` by default. Normal startup requires
both `AITOBUILD_WEBHOOK_SECRET` and `AITOBUILD_INTERNAL_API_TOKEN`.

Quality gates (from repository root):
- `uv run pytest`
- `uv run ruff check .`
- `uv run mypy src`

Verification snapshot (2026-10-05): 104 tests pass and two existing patch-repair
tests fail; Ruff and mypy pass. Treat fixing that baseline as M0 work, not as a
reason to remove or relax tests. Refresh the milestone snapshot when reverified.

Local workflow:
- Run `uv sync` with Python 3.14+.
- Use `uv run python sim/run_local_simulation.py --output sim/simulation-report.json`
	for copied-fixture execution; clear Foundry endpoint/API-key settings for a mock-only run.
- Require `succeeded=true` and zero command exit codes; do not infer success from HTTP status alone.
- Real subprocess and non-MCP file operations act on the API process working directory.
- Keep generated host certificates, reports, sandbox copies, and local credential files out of Git.

## Dependency Management (uv)

- Use `uv` for all Python dependency operations in this repo.
- Add packages with `uv add <package>`.
- Update installed environment with `uv sync` after dependency changes.
- Do not use ad-hoc `pip install` for project dependencies.
