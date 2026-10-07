# Delivery Milestones

Last verified: 2026-10-06. This is the execution roadmap; [PLAN.md](PLAN.md)
describes the target architecture and [README.md](README.md) documents setup.
Statuses below are evidence-based, not completion estimates or promised dates.

## Direction

Build a supervised product-team framework with configurable agent instances,
teams, workflows and delegation. Role templates define permission ceilings, not
a fixed organization chart. First establish versioned file-backed definitions,
then configurable factories and recorded assignments, before implementing a
managed workflow runner. Its first delivery recipe is an approved GitHub issue
becoming a tested draft PR in an isolated task checkout. PM-led, rule-based or
human-selected delegation is configurable; PM assignment does not require the
later full backlog-planning milestone. Humans approve scope and retain merge
authority. Add review, bounded meetings and proactive recipes without hardcoding
one universal lifecycle. Reuse native Microsoft Agent Framework capabilities
before adding orchestration logic; validate against the pinned SDK, not an
assumed API. MCP is an optional integration path, not a prerequisite for the MVP.

The first working-system milestone is **M2**, not merely a healthy API or a
mock simulation. Full backlog autonomy is a later goal, not the current state.

## Future Live Repository

[uhvogala/aitobuild_example](https://github.com/uhvogala/aitobuild_example) is the
designated repository for supervised real GitHub trials. Staged trials may start
as soon as a concrete workflow slice is ready to test; full M2 delivery is not a
prerequisite. Keep copied fixtures for repeatable regression checks and validate
the safeguards required by each slice before testing it against this repository.
Before each trial, review its layout and toolchain, grant least-privilege access,
and use scoped task branches and draft PRs with human merge authority. Do not
execute task code against the aitobuild service checkout.

## Where We Left Off

| Area | Evidence | Status |
| --- | --- | --- |
| Organization configuration | Immutable definitions, instance factories, operator model/tool profiles and bounded native graph admission/binding | Library assembly implemented; persisted assignment and managed service integration pending |
| Local setup | Python 3.14+, uv, certificate bootstrap and 16 certificate tests | Implemented |
| Ingress/governance | Signed webhooks, internal auth, dedupe, previews, role policy | Local durable previews/issue-task dedupe; other ingress state in memory |
| Local Developer pipeline | Copied fixture, preview approval, subprocess tests and file write | Verified on 2026-10-05 |
| Developer model/tool path | Foundry/v1 clients, private tools, terminal/process/browser and file memory | Live Grok/Kimi evaluations; strict zero-error acceptance pending |
| Persistent Docker sessions | Private checkouts/home volumes, restart memory/history and scoped cleanup | Live integration evidence; prototype isolation only |
| MCP shell/filesystem | Native stdio transport adapter, configurable names | Optional; compatibility validation pending |
| GitHub output | Allowlisted gh CLI branch/commit/draft publication, immutable snapshot capture and exact remote reconciliation | Locally regression-verified prototype; live acceptance pending |
| Architect/PM/meetings | Wired role tools, operator-gated PM writes, head-bound COMMENT review, GroupChatBuilder construction | Managed role execution, reviewable target diff, durable PM state and meeting execution pending |
| Operations | Manual tick API, capability matrix tests and successful hosted CI baseline | M0 accepted; durable worker/state and tracing missing |

Current local gates with Python-native factories/admission: **452 passed, two optional Docker probes skipped**;
Ruff and mypy (39 source files) pass. Definition-foundation gates passed with 396
tests; publication-fix gates previously passed
with 364 tests. The prior 325-test Docker-enabled baseline
and supervised example-issue implementation/verification remain historical evidence,
not live publication acceptance. Both original patch-repair
failures are fixed, with ambiguous matching still rejected. Live tool evaluations
check artifact correctness, tool coverage, context guards and zero unexpected
errors; provider failures/timeouts are visible failures, not waived successes.

The simulation's obsolete pip bootstrap has been replaced with the pytest from
the uv-managed environment. It now records `succeeded` and exits nonzero for
execution failures, incomplete agent runs, or failed cleanup. It validates a
local pipeline, not GitHub delivery or production isolation.

## Restart Checklist

1. Read this document, then use the [README quick start](README.md#quick-start).
2. Run `uv sync --locked` and the three quality gates. M0 and the constrained native M1 profile are accepted; continue with the configurable M2 foundation below.
3. Clear inherited Foundry endpoint/API-key settings and run the [local simulation](sim/README.md#local-baseline).
4. Require the preview/replay routes, `accepted=true`, zero command exit codes, `generated_file_exists=true`, and `succeeded=true` in the report.
5. For API-only testing, configure both secrets, start Uvicorn on loopback, and check health plus authenticated Developer readiness. Mock readiness is expected to be false.
6. Follow the immediate work queue; preserve accepted M0/M1 gates. Keep all live execution in disposable target repositories.

## M2 Foundation: Configurable Organization and Workflow Definitions

Status: **definitions, factories and native admission implemented as library APIs;
persisted assignment/managed service execution pending**.
This foundation precedes automatic issue consumption, not existing operator
delivery endpoints. Agent roles remain fixed initially; instance IDs, prompts,
optional model profiles, team membership/coordinator, event routes and delegation
strategy are configurable. No nominal `skills` metadata is accepted.

Deliverables and acceptance sequence:
1. Strict versioned JSON definitions and a storage interface with a file-backed
	implementation. Content-addressed immutable snapshots survive reload; changed
	definitions create distinct revisions. Unknown fields, ambiguous routes and
	invalid membership/references fail closed. **Implemented**; the example Python
	graph assembles with the pinned SDK. The example is a probe, not delivery.
2. Configurable agent factories and operator-owned model/tool-profile registries;
	resolve references before creating instances, preserve role instructions and
	per-run Developer tools, and isolate memory by organization/revision/agent.
	Admit Python graph nodes/edges and exact agent/operation/predicate references,
	then bind pinned SDK `WorkflowBuilder`/`AgentExecutor` and native conditional,
	switch, feedback and fan-out/fan-in edges. Reject inline agents/imports,
	HTTP/MCP, dynamic references, team escape and service-state writes. Enforce
	operator byte/node/connection/iteration ceilings. **Implemented** as library
	APIs; no PowerFx/.NET, expression language, custom evaluator or organization
	compatibility layer. Only Python graph documents are accepted.
3. Structured delegation records for coordinator/rule/human strategies: task,
	organization revision, team, eligible selected agent, rationale, scope and
	ownership. Reject unknown/ineligible agents and capacity/approval violations.
	Assignment is not a GitHub assignee write. **Next**.
4. Managed native workflows with persisted run/assignment/checkpoint references,
	approvals, cancellation and budgets. New runs pin approved revisions; later
	config changes cannot silently alter active runs or reset failed budgets.
	Definitions stay separate from execution state; DB storage can later implement
	the same definition contract. **Pending**.

Local validation (2026-10-06): `uv run pytest` reports 396 passes/two optional
Docker skips; `uv run ruff check .` and `uv run mypy src` pass (38 source files).
Definition tests cover native example compatibility, version/field/reference
rejection, delegation modes, reload, canonical/concurrent saves, immutable
revisions, corruption/address rejection and interrupted-save recovery. No live
models, GitHub writes, real Docker probes or hosted CI were repeated.

Python-native factory/admission validation (2026-10-07): **452 passed/two optional Docker
skips**, Ruff and mypy (39 source files) pass. Actual SDK agent invocations use
mocked HTTP transports with distinct configured clients. Conditional edges,
ordered switch/default routing, bounded feedback loops, read-only operations,
single fan-out/fan-in aggregation and checkpoint writes pass. Fresh adapter imports
and branch execution pass with declarative/PowerFx/.NET modules blocked. Unsafe
graph/reference/field cases, non-Boolean predicates and iteration exhaustion fail
closed. The preceding 443-test declarative adapter was replaced. No live models,
GitHub/Docker trial or hosted CI was repeated; managed recovery is still pending.

The loader/store/factories are library APIs only. They do not activate service
routing, enforce capacity or execute delegation. Direct workflow operation bindings
are read-only until managed approved-task execution is available; native agent
toolsets remain operator-owned and must use existing policy/approval-enforcing
handlers. Legacy bootstrap/endpoints are unchanged. Caller-supplied native
checkpoint storage is supported, but managed checkpoint/run recovery, approvals,
deadlines and active-run revision pinning are acceptance criteria for the next
slices, not claims about these configured prototype workflows.

## M0: Reproducible Baseline

Status: **accepted on 2026-10-05**. The
[hosted baseline run](https://github.com/uhvogala/aitobuild/actions/runs/37308813245)
passed at `c6edaef`, including 180 tests, Ruff, mypy and the mock fixture, and
retained the `simulation-report` artifact. Certificate tests now generate their
own test CA instead of relying on the runner's default trust store. The
[CI workflow](.github/workflows/ci.yml) uses locked Python 3.14 dependencies.
M1/M2 are not accepted by this baseline result.

Deliverables:
- Maintain the repaired patch behavior while preserving rejection of ambiguous or unsafe patches.
- Run tests, Ruff, and mypy in CI using Python 3.14 and the uv lockfile.
- Include the fixture simulation and retain its report as a diagnostic artifact.
- Keep contributor/setup docs accurate and pin reproducible dependencies.

Exit criteria:
- A clean checkout runs `uv sync`, all three gates, and the fixture simulation successfully.
- CI enforces those checks; failures return nonzero rather than appearing successful.
- Generated certificates, sandbox copies, reports, and secrets stay untracked.

## M1: Constrained Live Developer Task

Status: **accepted for the constrained native profile on 2026-10-05**. Depends
on M0. Explicit task previews bind immutable policy/context to a fresh session;
saved native approvals survive restart and reject duplicate or altered decisions.
Cross-process-locked ledgers enforce unique pre-write file reservations and one
wall-clock deadline across sessions/restarts. Failed/timed-out tasks are aborted,
not silently retried. Canonical filesystem paths cannot bypass blocked scope.

The approved profile requires Docker (or inert mock tools), a read-only repository
and root filesystem, offline execution, dropped capabilities and bounded
CPU/memory/processes. It rejects browser/arbitrary MCP execution and legacy
execution of a native-bound preview. PID-1 expiry bounds detached work; success,
error, timeout and rejection have cleanup regressions. Docker probes verified
read-only writes, no outbound network/service credentials/socket, deadline die
events and removal. This is not hostile-tenant sandbox certification.

`uv run python -m sim.evaluate_tools --approved-task --model grok-4.6` and the
same command with `--model Kimi-K2.7-Code --invoke-timeout 360` passed: two scoped
files, correct artifacts, actual pytest exit 0, persisted reservations, completed
runs, zero unexpected tool errors and automatic container cleanup. Reports remain
under ignored `sim/.run-artifacts/`; earlier failed trials remain failures.
`uv run pytest`, Ruff, mypy and the mock fixture simulation pass locally. Hosted
CI has not yet run this slice. Broader standalone tool coverage, browser/MCP,
distributed coordination, quotas and unattended hardening remain outside M1
acceptance and must not be implied by this focused trial.

Deliverables:
- Prepare a session image with its test toolchain and CA trust; validate bind paths and non-root ownership.
- Validate the pinned Foundry client with Entra credentials and explicit failure when the provider or native agent is unavailable.
- Carry an approved task bundle into native agent execution and enforce the same scope, command, path, file-change, and time budgets on every execution path.
- Separate read-only actions from side-effecting commands; prefix matching and writable bind mounts alone are insufficient isolation.
- Implement explicit approve/reject and native session continuation, with approval decisions tied to the exact pending operation and task.
- Prove task cleanup on success, error, rejection, and timeout. Validate MCP server schemas separately if enabling MCP.

Exit criteria:
- One small fixture task changes code, verifies the changed code, and reports the actual diff/test results using a real model.
- Unapproved writes and out-of-scope paths/commands are rejected consistently across preview runs and native tools.
- Pending approval can be resumed or rejected without rerunning unrelated work; limits and failed cleanup are visible failures.
- No credentials or Docker socket are exposed to task code. Human merge authority remains unchanged.

## M2: Approved Issue to Draft PR

Status: **operator-driven slices implemented; not accepted end to end**. Depends
on M1. Automatic task consumption and live publication acceptance remain pending.
The 2026-10-06 review reproduced snapshot/recovery defects now fixed with local
regressions; live acceptance remains open. See [PLAN.md](PLAN.md#merged-pr-review-2026-10-06).

Preparation slice (2026-10-05): repository `issues` opened/assigned/edited events
extract target identity, title objective, Markdown acceptance criteria and full
issue context without service-specific context files. Human approval pins an
operator-supplied base SHA and the exact scope/policy. Local locked, atomic synced
preview storage persists approval, stable scope-derived task identity, delivery
aliases and metadata dispatch markers. Restart, concurrent duplicate delivery,
immutable scope/base, changed-scope reapproval and malformed-state rejection have
regressions. `dispatched` means metadata was returned, not that work ran.

Checkout-preparation slice (2026-10-05): authenticated prepare/status endpoints
consume approved issue snapshots with explicit name/ID/local-seed configuration.
The worker validates base commit membership in the configured base branch,
creates one private independent clone and deterministic local task branch, and
persists preparing/prepared/failed transitions, base/head and approved context.
It rejects service checkouts and linked worktrees, retains logs/partial checkouts,
and shares the existing absolute deadline without resetting it. Concurrent calls
and restart return one pristine prepared checkout; interrupted/modified/expired
preparation fails closed instead of replaying side effects. Timeout cleanup,
failure artifacts and configuration/auth regressions pass. This is host Git
metadata preparation from trusted local seeds, not target code execution or a
hardened hostile-repository sandbox. The publication prototype is described below;
its live acceptance requires a fresh approved trial.

Native implementation slice (2026-10-05): approved native issue runs now bind the
prepared checkout directly to file tools and constrained offline Docker commands.
One durable session owns the delivery; a local task lock spans model invocation.
Saved approvals and edits survive restart without reseeding or resetting the
deadline/reservations. Interruptions, failures, rejection and expiry block replay,
abort the budget and retain artifacts; terminal containers are cleaned up.
Native completion persists `implemented`, not verified/publishable. Real SDK
approval replay, exact target identity, concurrent app instances and terminal
outcomes have regressions. An opt-in actual Docker probe passes two offline
target tests and removes its container/temporary volume.

Independent verification slice (2026-10-05): preparation pins operator-configured
commands; the authenticated verify endpoint accepts only preview identity. A fresh
constrained Docker session runs the plan against the completed target using the
original budget/deadline. Durable evidence records confirmed integer exit codes,
bounded output, timestamps, cleanup and checkout fingerprint. Reserved-path,
mutation, expiry, concurrent invocation, interrupted recovery and malformed
evidence guards pass. Verified duplicate requests recheck integrity without
re-execution. Actual Docker probes demonstrate verified exit 0 and terminal exit 5
failure despite the Developer's own tests passing, with artifacts and cleanup.
`verified` is command-plan evidence, not publication approval.

GitHub publish slice (2026-10-05): authenticated publish accepts only preview
identity. Verified checkouts bind to an approved base SHA plus a content-addressed
tree fingerprint (blob SHAs and file modes). Publication is single-flight with
resume from interrupted `publishing`, persists `pull_number`/`head_sha` before the
terminal state, and create-or-updates the same draft PR under an `aitobuild/`
branch prefix. The 2026-10-06 fixes capture upload bytes/modes in the digest walk
and reconcile missing head/PR receipts against exact remote state without ref
overwrites. Failed/aborted tasks remain terminal; interrupted `publishing` may
resume only within the original deadline. Final PR identity/head/deadline checks
must pass; failures retain remote receipts. Live `gh` adapters get a request timeout; durable mock
`published` is refused on the HTTP path. PR bodies include verification evidence.
M2 is not accepted end to end until a designated live trial meets the exit
criteria below.

Local verification after publication fixes: `uv run pytest` (364 passed/two optional Docker
skips), `uv run ruff check .`, and `uv run mypy src` pass. The prior Docker-enabled
325-test run and mock copied-fixture simulation are retained baseline evidence.
The 2026-10-05 supervised Grok example-issue trial reached independent verification:
five tests passed in both sessions, two scoped files, unchanged budget/deadline,
zero unexpected tool errors and successful cleanup. It did not publish a task
result. New publication/review paths have no live acceptance evidence; M2 exit
criteria below are not met.

Fresh publication attempt (2026-10-06, service fixes at `88720aa`): explicitly
approved issue #2 used new Grok task/state at the unchanged baseline. Scoped
artifacts and both five-test sessions passed with cleanup, but one model-added
EOF newline caused an exact-span rejection that the model corrected. The
supervisor incorrectly used a tool-evaluation zero-error gate and stopped
publication despite recovered, verified work. Delivery grading now accepts
recovered mistakes; final correctness, scope, verification, cleanup and valid
approval/budgets remain mandatory. The verified receipt is retained and the
rejected trial budget is aborted without changing its deadline/reservations.
GitHub hosting/auth/allowlist preflight passed; `gh` 2.102.0 is installed locally,
not in the default image. See [trial findings](PLAN.md#supervised-publication-attempt-2026-10-06).
No PR exists; publication/restart live acceptance remains pending. Any new attempt
requires fresh approval/identity, not a replay or relaxed edit matching.

Deliverables:
- Extract repository, issue, objective, acceptance criteria, base revision, and relevant context from an explicitly supported webhook event/assignment policy.
- Add a managed native workflow runner that consumes approved configured assignments rather than merely returning `developer.async.webhook` metadata; delivery stages bind existing preparation, implementation, verification and publication operations.
- Create a disposable checkout/task branch; never execute against the service's own checkout by accident.
- Implement least-privilege GitHub operations for branch creation, verified commits, and draft PR creation/update.
- Persist task identity, approval, delivery dedupe, base/head revisions, and worker transitions so retries cannot create duplicate branches or PRs.
- Keep the original approved scope attached to the run; require renewed approval for material scope changes.

Exit criteria:
- One approved issue in a designated test repository produces one draft PR with scoped changes, a linked issue, and post-change test/lint/typecheck evidence.
- No commit/PR is published without required approval; failed verification blocks publication.
- Duplicate delivery and a worker restart do not duplicate external side effects.
- Failures report an actionable task state and preserve artifacts; the Developer cannot merge its own PR.

## M3: Architect Review and Bounded Meetings

Status: **scaffolding only**. Depends on M2.

Architect draft-PR review slice (2026-10-05): published deliveries expose
`architect_get_published_pr` / `architect_submit_published_pr_review` bound to
publication identity only (`preview_id` → repository/PR/head). Reviews allow
`COMMENT` only (framed, length-capped) until a distinct reviewer GitHub identity
exists; `REQUEST_CHANGES`/`APPROVE`/merge stay off on this path. Last review persists on the delivery; submit refuses
when the live head SHA diverges from publication. Meetings and Developer fix
loops remain later work.


Deliverables:
- Give the Architect read/review tools and repository analysis, not implementation-write privileges.
- Route review results into fix requests, human review, or blocker-resolution meetings.
- Execute native group-chat workflows with turn/deadline budgets and explicit resolution/escalation outcomes.
- Persist decisions, record approved changes, and resume the blocked task only after resolution.

Exit criteria:
- A test PR receives a useful review and the Developer can apply one scoped correction.
- A deliberately blocked task enters a bounded meeting and either resumes with an agreed plan or escalates visibly.
- `workflow_built` alone is never treated as a completed meeting, and the Architect cannot write implementation files or bypass merge policy.

## M4: Durable Operations and Safety

Status: **not operationally complete**. Persistence needed by M2 starts there;
this milestone broadens recovery and operating controls before unattended use.
Depends on M2 and M3 for integrated workflow validation.

Deliverables:
- Recover previews, native sessions, meetings, escalations, retries, and dedupe across restarts and multiple workers.
- Drive scheduler ticks with a managed process; enumerate due meetings and measure actual active-job concurrency rather than trusting caller counters.
- Add structured task/correlation traces, approval audit records, redaction, and actionable escalation destinations.
- Enforce per-task cost/runtime/resource/network limits, cancellation, least-privilege credentials, and sandbox cleanup.
- Make framework capability audit checks meaningful in CI and expose configuration/binding failures in diagnostics.

Exit criteria:
- Restart, duplicate delivery, provider timeout, permission rejection, and orphan-container exercises finish without duplicate writes or silent work loss.
- Kill switch and cancellation stop new work and bound in-flight work; a human can inspect and recover a failed task.
- Every published change is traceable to the issue, approved scope, worker run, verification, and reviewer decision.

## M5: Supervised Product-Team Pilot

Status: **future direction**. Depends on M4.

Deliverables:
- Add PM backlog tools for approved issue creation, decomposition, dependencies, and completion tracking.
- Feed real repository analysis into proactive Architect proposals rather than supplied health counters.
- Run a small feature from human request through planning, delivery, review, and human merge with bounded spend and explicit escalation.

Exit criteria:
- A bounded feature completes through all three roles with an auditable human-approved plan and a coherent set of PRs.
- The pilot measures task success, review quality, recovery behavior, cost, and human intervention before expanding autonomy.

## Immediate Work Queue

1. Preserve the configurable definition/storage regressions and existing M0/M1, exact-span and publication safeguards. Run CI for new slices; never target the service checkout.
2. Next implementation slice: persisted coordinator/rule/human assignments with revision/team/eligible-agent identity, scope/approval checks and atomic ownership/capacity claims. Keep GitHub assignee writes separate.
3. Add a native Python managed runner that pins configuration and assignment revisions and connects existing approved delivery stages without a hardcoded team structure. Preserve original deadlines/budgets and distinguish native human-input events from service approval. Use registered operations/predicates and native SDK orchestration/checkpoints, not an expression runtime or custom workflow interpreter.
4. Validate a configured approved issue-to-draft recipe in copied fixtures, including revision changes, restarts, duplicate assignment and recovery within original budgets.
5. Stage a freshly approved live draft trial using outcome-based contribution grading. Recheck GitHub hosting/authentication/allowlist; do not replay the previously aborted task. Recovered tool errors are diagnostics, not contribution blockers. Stage head-bound COMMENT review only with actual source/diff inspection before claiming semantic review.

Update this snapshot when a milestone's exit checks have actually run. Record
the date, commands, observed result, and any unvalidated external dependencies;
implementation alone does not change a milestone to accepted.