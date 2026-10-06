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
- Designated test GitHub repository: https://github.com/uhvogala/aitobuild_example. Staged supervised trials may start as soon as a concrete workflow slice is ready to test; full M2 delivery is not a prerequisite. Require approved task scope, explicit repository configuration, disposable target checkouts and the safeguards applicable to that slice. Never execute target tasks against the service checkout.

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
- GitHub defaults to mock. Optional allowlisted `gh_cli` adapters support operator-triggered verified-delivery draft publication and PM issue writes. The 2026-10-06 snapshot/recovery findings are fixed locally with regressions; live publication still needs a fresh explicitly approved trial and checked hosting/authentication/allowlist. Meetings are constructed, not executed.
- Scheduler ticks require a caller; meeting/scheduler state and non-repository trigger dedupe remain in memory. Previews, approved scope/base, repository issue identity/delivery aliases and metadata dispatch markers persist locally under the Developer state directory.
- Repository issues opened/assigned/edited require explicit human approval and an operator-supplied base SHA. Criteria must be listed under a Markdown Acceptance Criteria heading. Changed scope requires a new approved task; duplicate delivery/restart cannot replace approval. Local checkout preparation requires explicit trusted seed/name/ID configuration; native issue runs then use that same prepared checkout. Independent verification pins operator commands at preparation and runs them in a fresh constrained session. Publication captures immutable upload bytes/modes in the verified-digest walk; interrupted publishing reconciles exact remote side effects under the original budget. Failed/aborted tasks retain receipts and never automatically replay. Live acceptance remains pending.
- Delivery prepare/status endpoints create one private independent checkout and local task branch from the pinned base, sharing persistent budgets. Native implementation persists one session identity and implementing/awaiting_tool_approval/implemented/failed states, with a local task lock through invocation and restart-safe approval continuation. `implemented` is not verified or publishable. Failures/interruption/rejection preserve artifacts and abort the budget; do not blindly retry. Host Git metadata preparation is not a hardened hostile-repository sandbox. Preparation does not fetch or publish; the separate publish endpoint performs remote GitHub writes and needs explicit approved trial scope.
- Preview-bound native tasks persist policy, unique-path pre-write reservations and a shared absolute deadline. Use the constrained Docker profile: read-only repo/root, offline commands and interactive input, dropped capabilities, CPU/memory/PID limits, expiry and terminal-outcome cleanup. Browser/arbitrary MCP adapters and legacy execution of native-bound previews are rejected. Standalone/unbound execution remains a prototype; do not call this a hardened hostile-tenant sandbox.
- Verification accepts only preview_id, never caller commands. Require accepted=true/state=verified, confirmed integer exit 0 for all pinned commands, successful cleanup and unchanged checkout fingerprint. Missing plan, mock backend and changed/unreserved paths cannot qualify. Successful replay rechecks content without rerunning commands; failure/interruption/expiry aborts budgets and preserves evidence. verified is not semantic review or publication approval; publication must recheck the exact snapshot and required approval.
- Native pending approvals and sessions persist locally. Architect/PM toolsets are wired; PM operator approvals freeze content and are one-shot but remain in memory. Managed role run/resume, automatic review orchestration, distributed coordination and durable task/approval auditing remain pending. Published-draft Architect reviews are COMMENT-only and pin the recorded head SHA; do not claim semantic review from metadata/filenames alone.

Key endpoints:
- GET /health
- POST /webhook
- POST /internal/triggers
- POST /internal/developer/preview and /internal/developer/preview/approve
- POST /internal/developer/delivery/prepare and GET /internal/developer/delivery/{preview_id}
- POST /internal/developer/delivery/verify
- POST /internal/developer/delivery/publish
- GET /internal/pm/plans and POST /internal/pm/plan/approve
- GET /internal/pm/issue-writes and POST /internal/pm/issue-write/approve
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

Verification snapshot (2026-10-05): 325 tests pass with real Docker probes enabled
(323 passed/two skipped ordinarily); Ruff, mypy and the mock
copied-fixture simulation pass. The M2 extraction/durable preparation slice has
restart/concurrency/immutable-scope regressions. The local checkout worker has
API, pinned-base, timeout, failure-artifact and service-isolation coverage.
Native SDK issue writes/approval replay use mocked transport; the opt-in real
Docker probes prove independent success and failure with evidence/cleanup.
After publication fixes, the 2026-10-06 ordinary gates pass: 364 tests/two optional
Docker skips, Ruff and mypy for 37 source files. A 2026-10-05 supervised Grok
example-issue trial passed implementation plus independent five-test verification,
with scoped artifacts, unchanged deadline/reservations and cleanup. Publication
and COMMENT review are implemented but not live-certified; fixes and remaining
acceptance boundaries are recorded in PLAN.md. Hosted CI was not rechecked for these slices.
M1 constrained native Grok/Kimi approved-task fixtures pass with actual tests,
persisted budgets, zero unexpected errors and automatic cleanup. M2 delivery is next;
the designated repository has an approved baseline and trial issue #1. The previous
trial prohibited result publication; new publication trials need explicit approval
and must not reset its expired or aborted budgets.
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
