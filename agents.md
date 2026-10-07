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

Organization/workflow direction:
- Scoped correction handoff gates (2026-10-07, from locally committed `823ea98`, continuation uncommitted): 684 tests pass/two optional Docker skips; Ruff/mypy (46 sources), diagnostics, compatible role/admission/service constructors and mock simulation pass. Opt-in native Architect correction proposals require complete source/diff inspection and save one bounded objective/explicit published paths in exact COMMENT approval. Authenticated `/internal/organization/corrections/offer` accepts only review preview ID and stages a completed/cleaned-up approved COMMENT review as a separate unapproved Developer task; model output cannot assign/approve/execute. Explicit Developer-only correction routes remain configurable. Atomic/fsynced receipts pin source run/COMMENT/head/scope/original route/revision; partial staging, duplicate/restart and changed activations retain original pins, while lost receipts/scope drift/stale heads fail closed. Fresh approval prepares a private checkout/branch and budget with literal narrowed paths/inherited ceilings, based on the reviewed head/published branch already present in an operator-provisioned seed. No fetch or reuse/reset of original Developer/review approvals or ledgers; missing initialized correction budgets cannot replay preparation. Twenty added native Architect mock/request/detached/HTTP/recovery/preparation cases pass. Handoff/preparation is not a native corrected artifact, independent verification or semantic/live acceptance; same-PR update, automatic correction loops, managed PM planning/writes and bounded meetings remain pending. No default activation, dependencies, live model/GitHub/Docker/hosted CI/push/remote write.
- Published-review routing gates (2026-10-07 continuation): 664 tests pass/two optional Docker skips; Ruff/mypy (46 sources), diagnostics, six service constructor call sites and mock simulation pass. Opt-in `PublishedReviewAdmission` stages saved published drafts as separate unapproved Architect tasks; atomic/fsynced receipts pin publication/head/scope/preview/original route/revision. Publication staging errors preserve successful delivery; authenticated `/internal/organization/reviews/offer` retries metadata only, never publication/approval/execution. Duplicates, partial staging and activation revision changes retain original pins; lost/corrupt receipts block Developer fallback. Fresh task approval initializes a separate approval/path/deadline-pinned ledger with no checkout/commands and zero file-write capacity. Missing initialized ledgers, expiry/abort/drift cannot reset budgets; frozen-owner cancellation survives damaged mutable preview metadata. Native source/diff inspection still needs exact saved COMMENT approval, with no model replay; counters are not semantic certification. Eighteen new request/detached/native-mock/HTTP/recovery cases pass. Default startup remains unchanged; no live model/GitHub/Docker/hosted CI/dependencies/commit/push/remote write. Scoped correction, managed PM planning/writes, bounded meetings and live acceptance remain next.
- Native PM coordination gates (2026-10-07 continuation): 646 tests pass/two optional Docker skips; Ruff/mypy (45 sources), diagnostics, all four service constructor call sites and mock fixture simulation pass. `NativeCoordinatorProposal.service_binding` registers a configured native PM as an explicit coordinator/event-scoped callback. Strict eligible-agent/rationale output is metadata; service context alone supplies PM identity and claim authority. An atomic/fsynced receipt pins approved content/time, scope/revision/route/team/workflow, coordinator/operator binding and original budget path/deadline. Save running before native invocation, proposed before assignment; reuse saved proposals after capacity deferral/restart without model replay or budget reset. Corrupt/drifted/uncertain receipts and cancellation/expiry fail closed with no assignment. Model tools are scoped read-only; no persistent tool profiles or native approval bypass. Seventeen added native mock-transport cases include request/detached admission, capacity reuse, interruption/corruption/pins, cancellation/expiry/runtime refusal. Callback execution does not claim PM journal capacity; existing worker bounds apply. Default startup/custom callbacks remain unchanged; no live model/GitHub/Docker/hosted CI/dependencies/commit/push/remote write. Managed PM planning/issue writes, automatic review/correction/meeting orchestration and live acceptance remain next.
- Scoped role gates (2026-10-07, from worker `51766cb`): 629 tests pass/two optional Docker skips; Ruff/mypy (45 sources), diagnostics and mock fixture simulation pass. `organization_roles.py` exposes explicitly registered native PM proposal and Architect review operations, not default routes. PM strict eligible-agent proposals are metadata only, with no actor/claim/issue-write authority. Architect uses a fresh approved review task/original ledger plus operator-pinned published head matching repository/issue/base/path scope; only immutable source/base-diff per-run reads are offered. Require complete contiguous inline source AND diff pages, but never infer semantic understanding from access counters. Exact saved target/body/evidence needs one-shot service approval; COMMENT checks head/target and original budget at write time. Restart avoids model replay; uncertain submitting effects fail closed; recovered errors remain diagnostics. Regular UTF-8 files <=1 MiB only, with explicit mode changes and binary/link/submodule refusal. Forty-two new native mock-transport/producer/CLI cases pass. No default activation/live model/GitHub/Docker/hosted CI/dependency/push/remote write. Automatic PM coordination, managed PM issue writes, correction/meeting loops and semantic live review remain next.
- Role templates may remain fixed initially; agent IDs/prompts/model profiles, teams/coordinators, native workflows and delegation must be configurable. Do not introduce a hardcoded contribution loop or fixed team structure.
- Strict JSON definitions and file-backed immutable revisions live in `src/aitobuild/organization.py`, with `config/organization.example.json` as a native-workflow probe. `organization_runtime.py` adds configured factories/native graph admission; `organization_assignments.py` adds durable ownership/capacity claims. `organization_runner.py` and `organization_delivery.py` add revision-pinned managed operation graphs and a configured native Developer adapter. These are library APIs; bootstrap/endpoints are unchanged. Storage/assignment/run completion is not approval or activation.
- Python-native factory/admission gates (2026-10-07): 452 tests pass/two optional Docker skips; Ruff and mypy (39 source files) pass. Actual native agents use mocked transports and distinct clients; conditional/switch routing, bounded feedback loops, fan-out/fan-in and SDK checkpoint writes pass without declarative/PowerFx/.NET imports. No new live model/GitHub/Docker/hosted CI trial. Preserve existing operator endpoints and safety ceilings while integrating configuration.
- Persisted-assignment gates (2026-10-07, historical baseline): 492 tests pass/two optional Docker skips; Ruff/mypy (40 source files) pass. Forty new tests cover authority/approval/scope/base pins, immutable owners, restart, cross-revision capacity, real process races, original ledger expiry/abort, corrupt state and interrupted/competing terminal transitions. AssignmentService reopens only original existing budgets; failure/cancellation aborts without resetting deadline/reservations. Actor identity must come from trusted operator/runtime context, not a model proposal. Claims enforce capacity only in the journal, not unmanaged HTTP runs; assignment completion is metadata, not verification/publication/cleanup. No GitHub assignee write, service activation or external trial. The subsequent managed runner slice is recorded below.
- Managed runner gates (2026-10-07, from `463b257`): 531 tests pass/two optional Docker skips; Ruff/mypy (42 source files) pass. Thirty-eight runner tests plus one assignment regression cover trusted actor identity, immutable ownership/approval/revision/scope/bindings, original budgets, checkpoint integrity/restart/parallel replies, one-shot decisions, duplicates, cancellation/thread draining, interruption, corruption and metadata-only finalization. Actual configured native Developer SDK calls use mocked model transport and existing scoped tools, with approve/reject/recovered exact-span scenarios; a disposable-target configured graph reaches mock draft publication through existing independent verification. No live model/GitHub/Docker/hosted CI trial. Service activation and broader scoped managed roles are next.
- Opt-in service gates (2026-10-07, from `8351092`): 549 tests pass/two optional Docker skips; Ruff/mypy (43 source files), diagnostics and legacy mock simulation pass (succeeded/artifact/exit 0). `organization_service.py` binds explicit repository/revision/event routes and trusted context through `create_app(..., managed_service_factory=...)`; approval/signed webhook/authenticated trigger delivery consumes approved configured tasks, including durable duplicate metadata. Optional authenticated task status/run/approve/resume/cancel controls cannot choose actor/revision/workflow/paths/budgets. Eighteen new cases cover HTTP decisions/restart/revision pins, target isolation, cancellation/expiry/interruption, races/capacity/frozen-owner cleanup; actual native SDK mock-transport and independent verification/mock-draft graphs run through service admission. Default server/endpoints remain unmanaged without a factory. No live model/GitHub/Docker/hosted CI or dependency/remote writes. Execution is request-scoped; detached worker/startup recovery and broader scoped roles remain next.
- Detached worker gates (2026-10-07, from service commit `9d13dcf`): 587 tests pass/two optional Docker skips; Ruff/mypy (44 sources), diagnostics and legacy mock simulation pass (succeeded/artifact/exit 0). `organization_worker.py` adds a separate atomic/fsynced journal pinning approved scope/base/time, activation/bindings and trusted one-shot commands before assignment. Optional `managed_worker_factory` alongside `managed_service_factory` owns bounded tasks via ASGI lifespan; approval/webhook/trigger/control requests enqueue before graph execution. Existing queued receipts recover; explicit bounded startup discovery admits only activated approved unowned tasks without worker receipts. Capacity contention defers original budgets; uncertain preclaim/native running effects fail closed, saved waits require exact decisions, finalizing/terminal recovery is metadata-only. Durable cancellation intent is polled by other local owners, and cancellation/shutdown drain threads before lock release. Authenticated `/internal/organization/worker` exposes failed slots requiring inspection/restart. Thirty-eight added cases include actual native SDK mocked transport and independent verification/mock draft delivery. Default startup remains unmanaged; no live model/GitHub/Docker/hosted CI, dependency changes, push or remote writes. Local locks/state are not distributed execution; broader scoped roles remain next.
- Workflow JSON maps validated nodes/edges to native WorkflowBuilder/AgentExecutor. Operations and synchronous Boolean predicates resolve only through trusted operator Python registries; no inline code/imports, expression language, PowerFx/.NET or custom workflow interpreter. Config cannot invoke arbitrary HTTP/MCP/dynamic references or write service state. Direct operations stay read-only; managed operation nodes use frozen approved-task context and role checks. Direct agent nodes fail closed in the managed runner; the configured native Developer uses its scoped delivery adapter/per-run tools. Agent tool profiles remain operator-owned and policy/approval enforcing. Accept only Python graphs, with no legacy format or migration layer. Active revisions/checkpoints/decisions and original deadlines are managed locally; automatic activation/distributed recovery remain pending.
- Omit nominal `skills` labels. Optional skill-file attachment is a separate future feature. Configuration cannot widen role/tool permissions, bypass scope/approval/verification or reset budgets. Prefer native Agent Framework orchestration behind validated application-owned bindings; definitions and run state use separate storage contracts so DB-backed definitions can follow.

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
- Default dispatch returns task metadata. An explicit managed service factory consumes approved routes using the guarded worker; adding an explicit worker factory enables durable local detached admission and bounded optional unowned-task startup discovery. Without it, invocation remains request-scoped. Shared internal-token context is not multi-user authentication. Trusted coordinator proposal callbacks are read-only and original-deadline bounded; never take actor identity from proposals. Journal capacity still does not cover unmanaged HTTP runs. Busy controls return 409; cancellation/shutdown must drain threaded stages before lock release. Other local owners poll durable cancellation intent; failed worker slots require inspection/restart. Only saved waiting states resume; uncertain running effects fail closed. Admission/run completion is metadata, not independent verification, publication approval or semantic review.
- GitHub defaults to mock. Optional allowlisted `gh_cli` adapters support operator-triggered verified-delivery draft publication and PM issue writes. The 2026-10-06 snapshot/recovery findings are fixed locally with regressions; live publication still needs a fresh explicitly approved trial and checked hosting/authentication/allowlist. Meetings are constructed, not executed.
- Scheduler ticks require a caller; meeting/scheduler state and non-repository trigger dedupe remain in memory. Previews, approved scope/base, repository issue identity/delivery aliases and metadata dispatch markers persist locally under the Developer state directory.
- Repository issues opened/assigned/edited require explicit human approval and an operator-supplied base SHA. Criteria must be listed under a Markdown Acceptance Criteria heading. Changed scope requires a new approved task; duplicate delivery/restart cannot replace approval. Local checkout preparation requires explicit trusted seed/name/ID configuration; native issue runs then use that same prepared checkout. Independent verification pins operator commands at preparation and runs them in a fresh constrained session. Publication captures immutable upload bytes/modes in the verified-digest walk; interrupted publishing reconciles exact remote side effects under the original budget. Failed/aborted tasks retain receipts and never automatically replay. Live acceptance remains pending.
- Delivery prepare/status endpoints create one private independent checkout and local task branch from the pinned base, sharing persistent budgets. Native implementation persists one session identity and implementing/awaiting_tool_approval/implemented/failed states, with a local task lock through invocation and restart-safe approval continuation. `implemented` is not verified or publishable. Failures/interruption/rejection preserve artifacts and abort the budget; do not blindly retry. Host Git metadata preparation is not a hardened hostile-repository sandbox. Preparation does not fetch or publish; the separate publish endpoint performs remote GitHub writes and needs explicit approved trial scope.
- Preview-bound native tasks persist policy, unique-path pre-write reservations and a shared absolute deadline. Use the constrained Docker profile: read-only repo/root, offline commands and interactive input, dropped capabilities, CPU/memory/PID limits, expiry and terminal-outcome cleanup. Browser/arbitrary MCP adapters and legacy execution of native-bound previews are rejected. Standalone/unbound execution remains a prototype; do not call this a hardened hostile-tenant sandbox.
- Verification accepts only preview_id, never caller commands. Require accepted=true/state=verified, confirmed integer exit 0 for all pinned commands, successful cleanup and unchanged checkout fingerprint. Missing plan, mock backend and changed/unreserved paths cannot qualify. Successful replay rechecks content without rerunning commands; failure/interruption/expiry aborts budgets and preserves evidence. verified is not semantic review or publication approval; publication must recheck the exact snapshot and required approval.
- Native pending approvals/sessions persist locally; managed Developer decisions retain exact saved content and distinguish service approval from native human input. Only saved idle waiting checkpoints resume; interrupted running states fail closed with cleanup/abort, and finalizing retries metadata only. Change operator binding revisions when registered behavior changes; active runs refuse drift. Cleanup is mandatory, but arbitrary synchronous callbacks are not preemptible; threaded delivery calls drain before ownership release. Architect/PM toolsets are wired; PM approvals remain one-shot in-memory. Broader managed roles, automatic review, distributed coordination and lifecycle auditing remain pending. Published-draft Architect reviews are COMMENT-only and head-pinned; do not infer semantic review from metadata/filenames.

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
- Optional factory-activated GET /internal/organization/tasks/{preview_id}
- Optional POST /internal/organization/tasks/run, /approve, /resume, /cancel
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
Fresh publication attempt (2026-10-06, `88720aa`) used approved issue #2 and new
Grok state; both five-test sessions and cleanup passed, but a model-added EOF
newline caused one exact-span error. Strict acceptance stopped before publication.
That stop incorrectly applied tool-evaluation criteria to a contribution: the
model recovered and verified its work. Its verified receipt remains; the supervisor aborted its original budget without
resetting deadline/reservations. No PR or merge occurred. CLI hosting/auth/allowlist
preflight passed with locally installed `gh` 2.102.0. Preserve reports and require
fresh task approval; never normalize spans or reset aborted budgets.
Live Grok/Kimi tool evaluation remains stricter than tool coverage: require
artifact correctness, completion, successful cleanup and zero unexpected errors.
This zero-error criterion applies only to tool development/usability evaluations,
not actual contributions. Agents may recover from tool errors while working.
Delivery acceptance requires completed correct scoped artifacts, passing final
tests and independent verification, successful cleanup, required approval and
valid budgets. Retain recovered errors as diagnostics; unresolved failures and
policy violations still block. Do not abort an otherwise qualified delivery
solely because its trace contains a recovered error.

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
