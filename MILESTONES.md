# Delivery Milestones

Last verified: 2026-10-05. This is the execution roadmap; [PLAN.md](PLAN.md)
describes the target architecture and [README.md](README.md) documents setup.
Statuses below are evidence-based, not completion estimates or promised dates.

## Direction

Build a supervised product team, starting with one reliable Developer workflow:
an approved GitHub issue becomes a tested draft PR in an isolated task checkout.
Humans approve scope and retain merge authority. Add Architect review and
bounded meetings next, then PM planning and proactive work after delivery and
recovery are dependable. Reuse native Microsoft Agent Framework capabilities
before adding orchestration logic; validate against the pinned SDK, not an
assumed API. MCP is an optional integration path, not a prerequisite for the MVP.

The first working-system milestone is **M2**, not merely a healthy API or a
mock simulation. Full backlog autonomy is a later goal, not the current state.

## Future Live Repository

[uhvogala/aitobuild_example](https://github.com/uhvogala/aitobuild_example) is the
designated future repository for supervised real GitHub trials. It is not a
current execution target. Continue using disposable copied fixtures until the
M1 controls and M2 delivery path are ready for an explicitly approved trial.
Before activation, review its layout and toolchain, grant least-privilege access,
and use scoped task branches and draft PRs with human merge authority. Do not
execute task code against the aitobuild service checkout.

## Where We Left Off

| Area | Evidence | Status |
| --- | --- | --- |
| Local setup | Python 3.14+, uv, certificate bootstrap and 16 certificate tests | Implemented |
| Ingress/governance | Signed webhooks, internal auth, dedupe, previews, role policy | Local durable previews/issue-task dedupe; other ingress state in memory |
| Local Developer pipeline | Copied fixture, preview approval, subprocess tests and file write | Verified on 2026-10-05 |
| Developer model/tool path | Foundry/v1 clients, private tools, terminal/process/browser and file memory | Live Grok/Kimi evaluations; strict zero-error acceptance pending |
| Persistent Docker sessions | Private checkouts/home volumes, restart memory/history and scoped cleanup | Live integration evidence; prototype isolation only |
| MCP shell/filesystem | Native stdio transport adapter, configurable names | Optional; compatibility validation pending |
| GitHub output | Mock issue-proposal adapter only | Branch/commit/PR delivery missing |
| Architect/meetings | Metadata scan and GroupChatBuilder construction | Execution and review integration missing |
| Operations | Manual tick API, capability matrix tests and successful hosted CI baseline | M0 accepted; durable worker/state and tracing missing |

Current local gates: **242 tests pass**; Ruff and mypy pass. Both original patch-repair
failures are fixed, with ambiguous matching still rejected. Live tool evaluations
check artifact correctness, tool coverage, context guards and zero unexpected
errors; provider failures/timeouts are visible failures, not waived successes.

The simulation's obsolete pip bootstrap has been replaced with the pytest from
the uv-managed environment. It now records `succeeded` and exits nonzero for
execution failures, incomplete agent runs, or failed cleanup. It validates a
local pipeline, not GitHub delivery or production isolation.

## Restart Checklist

1. Read this document, then use the [README quick start](README.md#quick-start).
2. Run `uv sync --locked` and the three quality gates. M0 and the constrained native M1 profile are accepted; continue with M2.
3. Clear inherited Foundry endpoint/API-key settings and run the [local simulation](sim/README.md#local-baseline).
4. Require the preview/replay routes, `accepted=true`, zero command exit codes, `generated_file_exists=true`, and `succeeded=true` in the report.
5. For API-only testing, configure both secrets, start Uvicorn on loopback, and check health plus authenticated Developer readiness. Mock readiness is expected to be false.
6. Work through M0, then M1 and M2 in order. Keep all live execution in disposable target repositories.

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

Status: **not implemented end to end**. Depends on M1. This is the supervised MVP.

Preparation slice (2026-10-05): repository `issues` opened/assigned/edited events
extract target identity, title objective, Markdown acceptance criteria and full
issue context without service-specific context files. Human approval pins an
operator-supplied base SHA and the exact scope/policy. Local locked, atomic synced
preview storage persists approval, stable scope-derived task identity, delivery
aliases and metadata dispatch markers. Restart, concurrent duplicate delivery,
immutable scope/base, changed-scope reapproval and malformed-state rejection have
regressions. Repository issue tasks cannot use the existing execution endpoints
or legacy harness; the disposable-checkout worker and all publication operations
remain absent. `dispatched` means metadata was returned, not that work ran.
Base SHA repository membership must be verified by the future worker.

Local verification: `uv run pytest` (242 tests), `uv run ruff check .`, and
`uv run mypy src`; mock copied-fixture simulation has `succeeded=true`, actual
command exit 0 and the generated artifact. No hosted CI or new live-model/GitHub
trial was run for this slice. The designated live repository remains inactive;
M2 exit criteria below are not met.

Deliverables:
- Extract repository, issue, objective, acceptance criteria, base revision, and relevant context from an explicitly supported webhook event/assignment policy.
- Add a worker that consumes approved tasks rather than merely returning `developer.async.webhook` metadata.
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

1. Maintain the green exact-text edit and legacy patch regressions; validate broader live editing tasks without weakening unique-match rejection.
2. Maintain the green hosted M0 baseline and run CI for new slices.
3. Preserve the constrained approved-task Grok/Kimi acceptance; never use the framework repository as a target.
4. Extend the durable M2 preparation states with worker/verification/failure recovery and GitHub permissions, then implement the smallest disposable-checkout issue-to-draft-PR worker.
5. Keep the designated live repository inactive until the worker is ready and a supervised trial is explicitly approved.

Update this snapshot when a milestone's exit checks have actually run. Record
the date, commands, observed result, and any unvalidated external dependencies;
implementation alone does not change a milestone to accepted.