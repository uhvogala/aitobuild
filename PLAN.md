# System Design Document: Agentic Product Team Framework

This document describes the target architecture, not a fully implemented system.
The implementation snapshot in section 7 distinguishes available capabilities
from remaining work. Operational milestones are tracked in [MILESTONES.md](MILESTONES.md).

## 1. Executive Summary
This document outlines a supervised, configurable product-team framework using
the **Microsoft Agent Framework (Python)**. PM, Architect and Developer are
initial role templates, not fixed team instances or mandatory workflow stages.
Humans approve scope and retain merge authority.

To prevent the chaotic, spaghetti-code outputs typical of naive autonomous coding systems, this framework enforces strict role separation and boundaries using two distinct communication paradigms:
1.  **Asynchronous (GitHub):** The immutable source of truth and state machine.
2.  **Synchronous (Meetings):** Ephemeral, structured group chats used exclusively for architectural planning and blocker resolution.

## 2. Core Architecture & Tech Stack

### 2.1 Technology Stack
* **Orchestration:** Python, Microsoft Agent Framework (`Agent`, `GroupChatBuilder`, `FoundryChatClient`).
* **Tooling Standard:** Model Context Protocol (MCP) for zero-friction integration with external systems.
* **Version Control & State:** GitHub (Issues, PRs, Webhooks).
* **Execution Environment:** Ephemeral, sandboxed Docker containers running MCP Bash and Filesystem servers.

### 2.2 Global Routing: The Dispatcher Pattern
The system is event-driven. The current implementation uses a deterministic Python dispatcher, not an LLM-based `DispatcherAgent`.
* **Input:** GitHub Webhooks and authenticated internal triggers through FastAPI.
* **Current behavior:** Dispatch validates and returns task metadata; it does not start an automatic contribution workflow.
* **Target behavior:** Resolve a versioned configured event route, team and workflow; record a validated assignment before invoking managed execution. PM-led, rule-based and human-selected delegation are selectable strategies, not fixed routing branches.

### 2.3 Configurable Organization and Workflows

Role templates remain permission ceilings; named agent instances are separate
identities with prompts, optional model-profile references and capacity settings.
Teams configure members and an optional coordinator. Routes select native workflow
definitions and eligible delegates. No `skills` labels are part of this schema;
optional attached skill files are a separate future feature.

Start with JSON organization definitions describing Python-native executor graphs.
Bind nodes and edges directly through Agent Framework `WorkflowBuilder`,
`AgentExecutor` and native checkpoints; routing, loops and parallel joins stay in
the SDK. Operations and routing predicates reference trusted, operator-owned Python
registries, never inline code, imports or expressions. Predicates are synchronous
and return strict booleans; operations consume a message and return a synchronous
or asynchronous result. No PowerFx, .NET or custom expression interpreter is used.
Direct workflow operations remain read-only; managed operation bindings use
frozen approved-task context and role checks. Config cannot grant permissions, skip verification,
write service state or expose arbitrary HTTP/MCP/file loading. Graph fields,
topology, exact bindings and routed-team membership are admitted before the SDK
builds the graph. Operator limits bound graph bytes, nodes, connections and runner
iterations, not callback duration. Only Python graph documents are accepted;
there is no organization-format compatibility or migration layer.

Definitions and execution state have separate storage contracts. Immutable,
content-addressed definition revisions let new runs adopt updates while existing
runs pin their approved snapshot. A file-backed definition store comes first;
a database backend can later implement the same contract. Assignment ownership,
scope approvals, budget ledgers, side-effect receipts and emergency revocation
remain enforced independently of workflow configuration.

## 3. The Agent Roster (Entities & Scopes)

Each agent is an isolated instance of the `Agent` class with specific instructions and constrained tool access (via MCP Plugins).

Configured instance IDs, prompts and model profiles distinguish agents sharing a
role. Team membership, coordinator and eligible delegates are configuration,
not role-derived globals. The roles below are reusable templates.

This roster is the target design. Today, role specifications exist for all
three roles and native toolsets are wired for the Developer, Architect and PM.
Only Developer execution has managed HTTP run/resume endpoints. MCP
shell/filesystem transport is optional; GitHub defaults to mock with an optional
allowlisted `gh_cli` adapter.

* **Product Manager (PM) Agent:**
    * *Scope:* Translates high-level Epics into atomic GitHub Issues with clear acceptance criteria.
    * *Tools:* GitHub Plugin (Read/Write Issues).
* **Architect Agent:**
    * *Scope:* System design, code reviews, and enforcing DRY principles. Cannot author implementation code.
    * *Tools:* GitHub Plugin (PR Review), Execution Plugin (Read-Only Filesystem, Bash execution for static analysis/testing), Memory (Vector Store/RAG for architectural history).
* **Developer Agent:**
    * *Scope:* Writes modular code to satisfy assigned issues. Cannot merge own PRs.
    * *Tools:* GitHub Plugin (Push branch, Open PR), Execution Plugin (Write Filesystem, Bash execution for running tests).

## 4. Communication & State Management

### 4.1 Asynchronous Layer (The Default State)
Routes choose configured teams and workflows. A coordinator (often a PM), an
explicit rule or a human proposes a structured assignment to an eligible agent.
The service validates and records task/revision/ownership/scope before execution;
a conversational handoff alone does not authorize writes. GitHub holds product
issues and contribution artifacts; local run/approval/budget/checkpoint state
tracks execution separately and will need durable backend support.

### 4.2 Synchronous Layer (The Meeting Engine)
When isolated agents hit a blocker (e.g., failing tests, ambiguous requirements, or architectural disputes), they trigger the Synchronous Layer using the `request_meeting(agenda, participants)` tool.

Meetings are orchestrated using the `GroupChatBuilder`. The framework pauses the async workflow, synchronizes the isolated contexts, and drops the agents into a temporary chat environment. 

#### Meeting Topologies
1.  **The "Scrum Master" Routed Meeting (Complex Planning):**
    * *Use Case:* Sprint Planning or Epic Breakdown.
    * *Mechanism:* Uses an `orchestrator_agent` (LLM) to dynamically route the conversation between PM, Architect, and Dev based on the flow of discussion.
2.  **The State-Machine Sync (Deterministic Resolution):**
    * *Use Case:* Code Review Dispute (Architect + Dev).
    * *Mechanism:* Uses a Python `selection_func` to pass the mic strictly back and forth until the Architect either approves the code or outputs an agreed refactoring plan.

#### Termination Rules
Meetings are strictly bounded to prevent infinite token loops. A meeting terminates only when:
* An agent executes a state-changing GitHub tool (e.g., `create_github_issue`, `approve_pr`).
* A maximum turn limit is reached (triggering human escalation).

## 5. System Lifecycle & Workflow Example

The following is one possible configured recipe, not a mandatory pipeline or
hardcoded organization chart. Its delivery, review and meeting integration is
not yet operationally complete:

1.  **Trigger:** A human (or PM Agent) opens a new GitHub Epic: "Add Stripe Subscription Billing."
2.  **Dispatch:** Webhook fires. The `DispatcherAgent` routes the payload to the PM Agent.
3.  **Planning Sync (Meeting):** The PM Agent recognizes the complexity and calls `request_meeting(agenda="Stripe Integration Planning", participants=["Architect", "Dev"])`.
4.  **Architectural Scaffold:** The Architect proposes module boundaries without writing implementation files. The PM creates approved implementation issues, and the Developer implements any required interface definitions on a task branch. Architect repository-write access remains disallowed by the current role policy.
5.  **Execution (Async):** Configured delegation selects an eligible implementer for Issue #1. The approved workflow binds isolated preparation, implementation, independent verification and draft publication operations.
6.  **Code Review (Async -> Sync):** The Developer opens a PR. The Dispatcher routes this to the Architect Agent. The Architect uses a Bash MCP tool to run `npm run lint` and `npm test` against the PR branch. 
    * *If Pass:* A head-pinned review is recorded; human approval and merge authority remain separate. Current published-delivery reviews are COMMENT-only.
    * *If Fail:* The configured workflow requests a scoped correction or escalates to a bounded meeting/human. Retry limits and meeting participants are configured, not a fixed two-attempt/1:1 rule; changed scope still requires approval.
7.  **Completion:** Once all issues linked to the Epic are closed, the PM agent asynchronously updates the main Epic and pings the human overseer.

## 6. Implementation Phasing

* **Phase 1: Foundation.** Set up the Python FastAPI webhook server, the `DispatcherAgent`, and integrate the Microsoft Agent Framework.
* **Phase 2: Definitions.** Versioned organization/workflow configuration, file-backed immutable revisions and a DB-ready storage contract. No `skills` labels.
* **Phase 3: Factories and Delegation.** Configurable agent identities/model/tool profiles, validated native operation bindings and persisted coordinator/rule/human assignments.
* **Phase 4: Managed Workflows.** Native orchestration/checkpoints connecting approved delivery operations, pinned revisions, bounded recovery and independent verification. Validate the first issue-to-draft recipe, not a hardcoded universal loop.
* **Phase 5: Review and Coordination.** Configured review/fix/meeting recipes and managed scheduling with durable state and escalation.
* **Phase 6: Supervised Pilot.** Broaden PM backlog/planning and proactive proposals after delivery/recovery gates pass; evaluate before expanding autonomy.

## 7. Implementation Snapshot (2026-10-07)

### Implemented Building Blocks

These items describe code and local test coverage, not production certification.

- Organization definition library: strict schema version 1 JSON with stable agent/team/workflow/route IDs, prompts, optional model/tool-profile references, capacity declarations and coordinator/rule/human delegation configuration; no skills or permission grants. File-backed `DefinitionStore` saves canonical SHA-256-addressed immutable snapshots with locked atomic/fsynced writes and integrity checks. The example is a harmless Python-native operation graph. Graph admission is implemented as a library API, not service activation.
- Unified trigger model with webhook and internal event normalization.
- FastAPI ingress endpoints: `/health`, `/webhook`, `/internal/triggers`, `/internal/scheduler/tick`.
- FastAPI internal developer preview endpoints: `/internal/developer/preview` and `/internal/developer/preview/approve`.
- FastAPI internal developer preview queue endpoint: `/internal/developer/previews`.
- FastAPI internal developer execution endpoint: `/internal/developer/run`.
- Webhook signature verification and trigger deduplication.
- Dispatcher routing for webhook, proactive scan, meeting request/due, and escalation flow.
- Scheduler cadence controls: cron evaluation, manual override, quiet windows, kill switch, proactive concurrency cap.
- Meeting lifecycle governance: request validation, due transition, bootstrap gating, and deadline checks.
- Shared escalation route and sink model with pluggable destinations.
- Internal endpoint authentication with `X-Internal-Token`.
- Mock-first adapters and policy guardrails for role/action restrictions.
- Runtime bootstrap with Foundry project and Azure OpenAI v1 chat-completions clients; live evaluations disable mock fallback.
- Runtime binding abstraction for Agent Framework classes with optional concrete `Agent` instantiation.
- `meeting.bootstrap` runtime kickoff hook that attempts native `GroupChatBuilder` workflow construction and safely falls back to mock startup when unavailable.
- Proactive Architect scan workflow output contract with structured findings and issue proposal suggestions.
- Configurable developer preview requirement before `developer.async.webhook` dispatch with approval gating.
- Repository issue extraction for explicitly supported opened/assigned/edited events: target identity, issue title/body, Markdown acceptance criteria, and approval-time base SHA. Extracted tasks always require human approval and contain no service-specific context file list.
- Local locked, atomic synced preview/task storage with immutable approved scope, stable scope-derived task IDs, delivery aliases and restart-safe metadata dispatch markers. Identical issue snapshots deduplicate across delivery IDs and concurrent registry instances; changed scope requires new approval.
- Operator-triggered checkout-preparation worker and authenticated prepare/verify/status endpoints. Explicit trusted local seed configuration matches repository name/ID; the worker checks approved base membership, rejects service/linked worktrees, creates a private independent clone/task branch, and persists state and artifacts. Native issue runs bind tools to that checkout with a durable session identity. Independent verification runs a pinned operator command plan in a fresh constrained Docker session, persists exit/output/cleanup evidence bound to a whole-checkout fingerprint, and blocks mutation or failed/interrupted replay. Preparation, implementation and verification share the original deadline and reservations.
- Architect and PM native toolsets are wired into runtime bootstrap. Architect tools provide source analysis, discovery/search, durable decision memory and published-delivery review. PM tools provide backlog reads, plan drafts, criteria and operator-approved issue creation/update/linking with immutable one-shot approval snapshots. GitHub defaults to mock; the optional `gh_cli` adapter enforces a repository allowlist. Web search and meeting request tools are shared across roles.
- Authenticated `POST /internal/developer/delivery/publish` accepts only preview identity, derives metadata from the approved issue and records `publishing`/`published` plus remote branch/commit/draft-PR identity. Publication captures immutable bytes/modes during the verified-digest walk. Interrupted `publishing` can reconcile the exact remote tree/base/message and same task-branch draft PR without overwriting refs; failed/aborted tasks remain terminal. Published-delivery Architect tools resolve PR/head from saved publication, pin review `commit_id` and permit COMMENT only. Live evidence is still required before acceptance.
- Operator-visible pending preview queue retrieval for approval workflows.
- Practical Developer run harness with dry-run/live modes and policy-enforced command/path checks.
- Configurable Developer execution backend (`mock` or `subprocess`) with CLI command timeout controls.
- Persistent container-session execution mode for Developer runs (single container per session with start/stop lifecycle).
- Session-bound Developer tool wiring using native per-run tools for files, terminal jobs, processes and optional browser MCP.
- MCP-backed Developer command/filesystem adapters with explicit fail-fast behavior (no local fallback in MCP mode).
- Developer Agent diagnostics endpoint (`/internal/runtime/developer-agent`) for runtime readiness, tool inventory, and session support visibility.
- Developer Agent run/resume endpoints with persistent native sessions/history/memory and saved approval requests. Manual approve/reject restores the exact saved operation after restart, rejects duplicate/cross-session decisions, and blocks task replacement while pending. Same-session protection remains process-local; interrupted continuations fail closed rather than automatically replaying side effects.
- Capability audit matrix for reuse/wrap/custom decisions.
- Host certificate export and pre-feature dev container trust bootstrap, with generated certificates excluded from Git.
- Local subprocess simulation repaired to use uv-installed pytest, with explicit success reporting and nonzero failure exits.
- Agent-driven fixture evaluations record tool coverage, artifact correctness, errors, latency and token/cache usage for deployed Grok/Kimi models. Oversized tool results spill to private files with paged retrieval; oversized prompts are rejected before invocation.
- Targeted edits use structured exact-text replacement with unique-match rejection; legacy free-form patch parsing is opt-in. Focused same-task Grok/Kimi editing probes pass with zero tool errors and independent pytest verification.
- Ripgrep-backed file discovery and literal/regex content search use allowed private roots, ignore-aware glob filtering and bounded pagination. Focused Grok/Kimi search probes pass with zero errors and unchanged fixture source.
- Preview command defaults cover common interpreters, package managers, Git, builds, shell scripts and filesystem utilities. Native runs can snapshot an approved preview's task context/policy in a fresh session; scoped repository tools and direct/managed-start commands reuse it after restart, with no caller-supplied policy or session rebinding. Standalone native prototypes remain available; token-prefix matching is not a security boundary.
- GitHub Actions baseline workflow uses Python 3.14, locked uv dependencies and ripgrep for pytest/Ruff/mypy plus the mock fixture simulation, retaining available reports on failure. Hosted run 37308813245 passed at `c6edaef` with 180 tests and a retained simulation report, accepting M0.
- Verification on 2026-10-05: 325 tests pass with both real Docker probes enabled (323 plus two skips normally); `ruff`, `mypy src` and the mock copied-fixture simulation pass. Native SDK approval replay regressions use mocked transport. Actual independent Docker pytest success/failure both preserve evidence and clean up; Developer tests passing alone cannot certify a delivery. Prior M1 Grok/Kimi acceptance is preserved by regressions, not rerun. M2 extraction, preparation, native implementation and independent verification are connected; the supervised example-repository Grok trial below now provides live evidence for this slice. GitHub task-result publication and hosted CI for the slice remain pending; full standalone tool coverage is not certified.
- Integrated baseline verification on 2026-10-06 at `740b385`: **347 passed, two optional Docker probes skipped**, Ruff passes, and mypy passes for 37 source files. Focused role/delivery/policy tests: 105 passed/two skipped. This gate run did not repeat live models, real Docker probes or hosted CI, and passing regressions do not waive the reproduced publication findings below.
- Publication-fix verification on 2026-10-06: **364 passed, two optional Docker probes skipped**, `uv run ruff check .`, `uv run mypy src` (37 source files) and whitespace checks pass. Regressions cover content/mode/removal after capture, missing PR/head receipts, uncertain ref creation, remote drift/denied lookup, expiry and terminal failed-budget replay. No live publication/model trial or hosted CI was repeated.
- Configurable-definition foundation verification on 2026-10-06: **396 passed, two optional Docker probes skipped**, Ruff, mypy (38 source files) and whitespace checks pass. Definition tests cover multiple instances per role, native example compatibility, configurable delegation, invalid versions/fields/references, immutable/canonical revisions, reload, concurrency, tampering and interrupted-save recovery. No automatic routing/runtime activation or external trials were added.
- Configured runtime/admission replacement (2026-10-07): `organization_runtime.py` resolves operator-owned model/tool profiles before assembly, keys handles by instance ID, preserves role instruction templates/per-run Developer tools and isolates memory by organization/revision/agent. `build_organization_workflows` binds validated JSON graphs to native `WorkflowBuilder`/`AgentExecutor`, registered read-only message operations and named Python predicates, with caller checkpoint storage and revision-qualified workflow names. Conditional edges, ordered switch/default routing, bounded feedback loops and fan-out/fan-in execute natively without declarative/PowerFx/.NET imports. Only Python graph documents are accepted; no compatibility layer is needed for this new feature. Gates: **452 passed/two optional Docker skips**, Ruff and mypy (39 source files). Tests cover actual SDK agent calls over mocked HTTP with distinct clients, strict Boolean routing, iteration exhaustion, single parallel aggregation, checkpoint writes and blocked non-Python workflow dependencies. The preceding 443-test declarative adapter was replaced, not extended with an expression interpreter. No live model/GitHub/Docker/hosted CI trial or managed runner was added.
- Persisted assignment slice (2026-10-07): `organization_assignments.py` adds strict proposals/immutable records, a separate `AssignmentStore` protocol and locked atomic/fsynced `FileAssignmentStore`. Trusted `AssignmentService` resolves coordinator/rule/human decisions from explicit definition revisions/routes and freezes approved preview scope/base, approval timestamp, eligible agent/actor/rationale and original budget path. Existing ledgers are reopened with `create=False`; missing, expired, aborted or changed-scope budgets fail closed. One task owner is preserved across teams/organizations/revisions, with atomic cross-revision organization/agent capacity using the most restrictive active ceiling. Terminal decisions serialize budget checks/abort with the journal transition; failure/cancellation keeps original deadlines/reservations and prevents reclaim. Gates: **492 passed/two optional Docker skips**, Ruff and mypy (40 source files). Forty new tests include real process races, restart/idempotency, changed scope/base/revision, corruption, concurrent terminal outcomes and interrupted-write retries. No GitHub assignee writes, service activation, live model/GitHub/Docker/hosted CI trial or managed runner was added.

Managed native runner slice (2026-10-07, starting at `463b257`):
`organization_runner.py` separates durable run state from definitions/assignments,
pins workflow/revision/scope/operator bindings and native session/checkpoint
integrity, and audits consumed trusted-actor decisions and cleanup. Assignment
ownership, immutable approval/base/scope and original existing budgets are
revalidated on active run/resume/operations/checkpoints. Coordinator identity comes
from a trusted runtime provider, never a model field. Native edges/predicates retain
configurable sequencing/branching/joins, with no fixed contribution loop. Saved
idle waiting boundaries resume; interrupted running states fail closed, cancellation
aborts unchanged budgets, threaded delivery calls drain before lock release and
finalizing retries write metadata only. Native human input is not service approval.
`organization_delivery.py` connects existing prepare/verify/publish services and a
configured native Developer adapter with durable SDK sessions/approval content and
existing per-run exact-span/offline Docker tools. Recovered errors remain diagnostics.
Gates: **531 passed/two optional Docker skips**, Ruff and mypy (42 source files);
38 runner tests plus one assignment regression include actual SDK/mock-transport
approve/reject/recovered-edit scenarios and a disposable-target mock draft graph
with independent evidence. No dependency change or live model/GitHub/Docker/hosted
CI trial or remote writes were performed for this slice.

Opt-in managed service integration (2026-10-07, from `8351092`):
`organization_service.py` binds exact trusted repository/revision/event activations,
app-owned previews/worker/tools and context-local operator/coordinator identity.
`create_app(..., managed_service_factory=...)` enables request-scoped consumption
after authenticated approval and signed webhook/authenticated trigger delivery,
including durable duplicate dispatch metadata. Existing claims always use their
pinned revision; preparation reuses the original approved target/ledger. Optional
authenticated status/run/approve/resume/cancel controls reject identity/workflow/
revision/path overrides and retain distinct native input/service approval. Rules,
human decisions and registered read-only coordinator callbacks remain configurable.
Coordinator failure/cancellation aborts the original prepared budget; cancellation
authorization uses frozen ownership so corrupt mutable previews cannot prevent cleanup.
Gates: **549 passed/two optional Docker skips**, Ruff/mypy (43 source files), clean
editor diagnostics and a successful legacy mock fixture simulation with artifact and
exit 0. Eighteen added cases include HTTP admission/decisions/restart/expiry/interruption,
target isolation, coordinator identity, concurrency/capacity and actual native SDK over
mocked model transport. The existing verified mock draft graph also passes through
service admission. No live model/GitHub/Docker/hosted CI, dependency or remote write.

Opt-in detached worker lifecycle (2026-10-07, from service commit `9d13dcf`):
`organization_worker.py` adds a separate strict atomic/fsynced admission journal,
immutable approved scope/activation/binding pins and one-shot trusted command history.
`create_app(..., managed_service_factory=..., managed_worker_factory=...)` enqueues
approved tasks/decisions before returning; ASGI lifespan owns bounded worker slots
and drains threaded stages on cancellation/shutdown. Explicit bounded startup
discovery admits only activated approved tasks without existing ownership/receipts;
existing queued jobs recover under original pins/budgets. Capacity/lock contention
defers, while uncertain unclaimed/running effects fail closed without replay.
Saved waiting decisions and metadata-only finalizing/terminal recovery remain distinct.
Cancellation intent is atomic/durable; other local owners observe it by polling.
Authenticated worker diagnostics expose failed slots; receipt failure requires
inspection/restart, not blind effect retry. Default startup still supplies no factory.
Gates: **587 passed/two optional Docker skips**, Ruff/mypy (44 source files), clean
editor diagnostics and mock fixture success/artifact/exit 0. Thirty-eight new cases
cover queued revision/scope/binding/budget pins, decisions, startup, corruption/save
interruption, duplicate local owners, capacity, cancellation/thread draining and
actual native SDK mocked transport plus independent verification/mock draft delivery.
No live model/GitHub/Docker/hosted CI, dependency change, push or remote write.

Scoped native role operations (2026-10-07, from worker commit `51766cb`):
`organization_roles.py` resolves pinned configured PM/Architect owners without
persistent tool profiles. `pm_propose_assignment` emits strict eligible-owner
metadata only, never actor identity, ownership or issue writes. Architect review
uses a separately approved task/original ledger and operator-pinned publication
head, matching repository/issue/base/path scope. Native per-run read tools retrieve
immutable hash-verified UTF-8 blobs and exact pinned-base diffs with bounded inline
contiguous pages; binary/link/submodule targets are refused and mode changes remain
explicit. Complete source/diff access is required but does not prove semantic quality.
Exact target/body/evidence persist; a distinct one-shot service approval publishes
COMMENT with a write-time original-budget check. Restart never reruns the review
model, and uncertain submitting effects fail closed. Recovered errors stay diagnostics.
Default service activation/routes and direct-agent admission are unchanged; operators
register these library operations explicitly, not a fixed contribution loop.
Gates: **629 passed/two optional Docker skips**, Ruff/mypy (45 sources), clean diagnostics
and mock fixture success/artifact/exit 0. Forty-two new cases cover native mock
transports, exact approvals/evidence/paging/drift/expiry, immutable source/diffs and
GET-only CLI identity/allowlist/size/link refusals. No live trial, push or remote write.

Native PM coordination continuation (2026-10-07): `NativeCoordinatorProposal`
connects the native configured PM to existing service/worker admission via an
explicit coordinator/event-scoped `CoordinatorBinding`. The model emits only
strict eligible-agent/rationale metadata; service context supplies trusted PM
identity and assignment authority. Native calls use one scoped read-only tool.
Atomic/fsynced receipts pin approved scope/time, original ledger/deadline,
definition/route/team/workflow, coordinator and operator binding. Saved proposals
survive capacity deferral/restart without model replay or budget reset; interrupted,
corrupt or changed receipts fail closed. Cancellation, expiry and malformed output
cannot create ownership. Existing custom callbacks and default bootstrap are unchanged.
Gates: **646 passed/two optional Docker skips**, Ruff/mypy (45 sources), diagnostics,
four-call-site API compatibility and mock simulation success/artifact/exit 0.
Seventeen new native mocked-transport cases cover request/detached admission and
capacity, interruption, pins, cancellation and runtime/expiry refusal. Coordinator
callbacks do not claim PM journal capacity; bounded workers remain the local limit.
No live model/GitHub/Docker/hosted CI, dependency changes, commit, push or remote write.

Published-review admission continuation (2026-10-07): opt-in `PublishedReviewAdmission`
stages saved published drafts as separate unapproved Architect tasks using atomic,
fsynced immutable target/head/scope/route/revision receipts. Successful publication
offers metadata; staging errors preserve the publication result. An authenticated
metadata-only offer endpoint retries staging without remote publication replay.
Duplicates/partial staging and changed activation revisions retain the original
preview and pins; lost/corrupt receipts cannot fall through to Developer routing.
Fresh task approval routes through service or detached workers without a Developer
checkout. Separate review ledgers pin approval/path/deadline and refuse recreation,
expiry/abort or deadline drift, including owned-run restart. No commands/file writes
are offered; frozen-owner cancellation survives damaged mutable preview metadata.
Native Architect source/diff inspection still requires exact saved COMMENT approval;
model calls do not replay on approval restart, and counters do not prove understanding.
Gates: **664 passed/two optional Docker skips**, Ruff/mypy (46 sources), diagnostics,
six-call-site service compatibility and successful mock fixture simulation. Eighteen
new regressions cover request/detached native mocked transport, HTTP staging/approval,
metadata recovery and scope/revision/ledger/cleanup boundaries. Default startup is
unchanged; no live model/GitHub/Docker/hosted CI, dependency change, commit, push or
remote write. Scoped correction, managed PM planning/writes, bounded meetings and
semantic live acceptance remain pending.

Scoped correction handoff continuation (2026-10-07, from committed
`823ea98`): native Architect bindings can opt into one read-only scoped correction
proposal after complete published source/diff inspection. Objective/explicit paths
become part of exact saved COMMENT approval, with replacement/scope drift refusal.
A separately configured Developer activation stages the completed approved review
as a fresh unapproved task via an authenticated ID-only metadata offer. Atomic,
fsynced receipts pin source run/COMMENT/target/scope and original correction revision;
partial staging and duplicates cannot replay the model or replace approvals.
Missing receipts, changed scope and stale heads refuse ordinary Developer fallback.
The task is based on the reviewed head/published branch with literal narrowed paths
and inherited policy ceilings. Approval prepares a fresh private checkout/branch and
budget only when the operator's local seed already contains that head; no fetch,
original ledger reset or preparation replay after ledger loss. Existing registered
delivery operations remain responsible for native implementation/verification.
The correction-specific tests prove staging/preparation, not a corrected native
artifact, semantic review or live delivery. Same-PR update/reconciliation and
automatic correction loops are not introduced. Gates: **684 passed/two optional
Docker skips**, Ruff/mypy (46 sources), diagnostics, role/admission/service constructor
compatibility and successful mock fixture simulation. Twenty new parameterized
cases include actual native Architect mocked transport and request/detached/HTTP
handoff/recovery. No dependency/live model/GitHub/Docker/hosted CI/push/remote write.
This continuation is committed as `7d7ea13`; native correction artifact acceptance, managed
PM planning/writes, bounded meetings and live review remain pending.

### Current Limits

- Configured factories/native admission/assignment/runner libraries have opt-in app integration through operator-owned service/worker factories; the default server still uses legacy handles and metadata dispatch. Service-only activation awaits execution in the request; optional workers durably enqueue with lifespan-owned execution and explicit bounded unowned-task startup discovery. Explicit repository/revision/event and actor/binding registries cannot be selected by HTTP callers. Internal shared-token identity is not multi-user authentication. Journal capacity does not govern unmanaged HTTP runs. Managed operation graphs pin active revisions; direct agent nodes remain denied. Scoped Developer delivery, PM proposals and Architect review operations are explicit libraries, not automatically activated orchestration. Trusted read-only coordinator callbacks are original-deadline bounded, not arbitrary-code isolation. Run/assignment/admission completion is metadata, not verification/publication approval; native input is distinct from service approval. Only saved waiting checkpoints resume; interrupted running effects cannot be blindly replayed. Busy controls return 409; mandatory cleanup/thread draining and original budgets remain enforced. Cross-process cancellation is polling-based; failed worker slots require inspection/restart. Local locks/state are not distributed execution or hostile-tenant certification. Live delivery/hosted CI acceptance remains pending.
- `developer.async.webhook` returns task metadata by default. Detached consumption exists only for explicitly service/worker-factory-activated approved repository routes, not default startup.
- Supported repository issue events produce target-specific bundles and require approval-time base SHA pinning. Checkout preparation validates membership in the explicitly configured local seed/base branch, not live GitHub repository identity or remote freshness. No automatic remote lookup/fetch occurs. Legacy fixture payloads without repository context retain prototype routing; unsupported repository webhook events and missing issue criteria are rejected.
- GitHub branch/commit/draft-PR publication is implemented behind the optional allowlisted `gh_cli` adapter; the reproduced snapshot/recovery gaps are fixed locally with regressions at `88720aa`. Live acceptance remains pending. The operator installed checksum-verified `gh` 2.102.0 locally for the fresh trial and verified `uhvogala` on github.com; this is not a default image dependency. No live task-result publication has been tested.
- General PM draft/write-approval stores remain in memory. Explicit native PM coordinator bindings now feed saved read-only proposals into trusted service assignments; managed PM planning/issue writes remain pending. Opt-in publication-to-Architect staging/routing and scoped correction handoff retain separate task and exact COMMENT approvals. Native correction artifact acceptance, same-PR update, automatic correction/meeting orchestration and live semantic acceptance remain pending. Reads support regular UTF-8 files up to 1 MiB; binary/link/submodule review needs another approved adapter, not a bypass.
- Meetings construct workflows without executing them, and proactive scans interpret supplied metadata rather than inspecting a repository.
- Scheduler ticks require an external caller; meeting/scheduler state and non-repository trigger dedupe remain in memory. Previews, approvals and repository issue task/delivery identities persist locally. `dispatched` records metadata routing, not worker completion; inspect the saved queue after restart. Distributed coordination and full lifecycle auditing remain pending.
- Native runs without a bound preview and unbound legacy runs remain prototypes. Approved native tasks persist unique-path reservations and an absolute deadline, fail closed after abort, and use offline read-only repository execution. Browser/arbitrary MCP adapters are excluded; native memory has a separate scoped SDK store. Distributed coordination, durable auditing and scratch/disk quotas remain pending.
- The constrained Docker profile has resource/capability/network restrictions and expiry/cleanup checks, but is not a hardened hostile-tenant sandbox. Prefix matching is not shell parsing; dependency installation and build outputs cannot write the repository mount.
- The capability matrix tests check entries, not duplicate implementations; the successful CI baseline does not prove no-duplicate capability enforcement.
- Docker, MCP, and live-model paths need fresh integration evidence before being considered operational.

### Supervised Example Trial (2026-10-05)

- Target: `uhvogala/aitobuild_example`, repository ID `1405327159`. Preflight found it empty. With explicit human approval, published a minimal five-file Python baseline at `f54d7dd42960d208d04fa1058ec333401c73eef5` on `master` and created [issue #1](https://github.com/uhvogala/aitobuild_example/issues/1). Scope: add integer `multiply`, preserve `add`, and test positive, negative, mixed-sign and zero inputs; only `src/demo_app/math_ops.py` and `tests/test_multiply.py` may change.
- Attempt 1: live `grok-4.6` requested `developer_find_files(glob="**/*")`. The one-off supervisor incorrectly allowed only writes/pytest and rejected this read-only request. The service correctly failed the task, aborted its budget with zero reserved paths, cleaned up and blocked replay. No edits occurred; verification was not reached. Retained [failed report](sim/.run-artifacts/example-trial-20261005/report.json) and durable state are not reclassified as success.
- Attempt 2: human-approved read-only scope clarification produced a new immutable task/preview, separate state directory and native session at the same base. Mock fallback, browser and MCP were disabled. The operator checked each saved approval before resuming. Grok completed 13 successful tool calls, including 12 approval rounds, with zero unexpected tool errors. About 56.5 seconds elapsed; SDK-reported total usage was 45,683 tokens across turns. This small task reached the supervisor's approval-round cap, suggesting discovery/approval overhead needs attention.
- Result: exactly the two scoped files changed; existing `add`, baseline tests and configuration were preserved. Developer and independent `python -B -m pytest -p no:cacheprovider -q` each passed **5 tests**, confirmed integer exit **0**. The fresh verifier session saved a snapshot fingerprint and successful cleanup receipt. The original absolute deadline was unchanged; only the two scoped paths were reserved; the new budget was not aborted. No trial container remained, the trusted seed stayed pristine, and remote `master` still pointed at the baseline. No task-result commit, push, PR or merge occurred.
- Reporting caveat: the one-off runner incorrectly indexed an absent `aborted` field after successful verification. Success budgets omit that optional field. The [raw report](sim/.run-artifacts/example-trial-20261005-v2/report.json) preserves the reporting error and authoritative `accepted=true`/`verified` receipt; the [review summary](sim/.run-artifacts/example-trial-20261005-v2/review.json) confirms every acceptance check using saved evidence only. No model/verifier rerun or budget reset was used to repair reporting.
- Boundary: this was operator-driven API execution from a real GitHub issue, not live webhook delivery, an automatic async worker, hosted CI, publication acceptance or hostile-tenant certification. Full M2 remains open.

### Merged PR Review (2026-10-06)

- Integrated PR #2 (Architect/PM tools), #3 (verified-delivery draft publication) and #5 (published-draft Architect review) by fast-forward to `740b385`. Local trial findings were preserved; no new commit or remote write was made during this review. COMMENT-only review deliberately avoids same-token APPROVE/REQUEST_CHANGES self-review failures.
- **P1, verified snapshot race, fixed locally:** the review reproduced publication of bytes changed after the digest check. Publication now hashes and captures upload bytes and executable modes in the same walk, compares that digest to verification, and uses only the immutable capture. Regressions mutate content, permissions or remove the file after capture; none changes the uploaded snapshot. See [publication implementation](src/aitobuild/developer_delivery.py).
- **P2, remote side-effect recovery, fixed locally:** missing PR numbers now reconcile the same repository/head/base open draft; missing head receipts reconcile only an exact tree, approved base parent and commit message. Unknown or mismatched refs are not overwritten. Interrupted `publishing` uses the original valid budget; terminal `failed` returns its retained receipt without restoring or replaying an aborted budget. Regressions include uncertain ref success, local receipt-save failure, ready/closed PR refusal, drift and expiry. See [GitHub adapter](src/aitobuild/tools/github.py) and [publication regressions](tests/test_dispatcher.py).
- Next supervised trial should cover a fresh approved issue through implementation, verification, draft publication and head-bound COMMENT review, after `gh` authentication/hosting/allowlist are checked. The previous trial explicitly prohibited task-result publication and its deadline must not be renewed; require new explicit approval. No self-merge. Full M2/M3 acceptance remains open.

### Supervised Publication Attempt (2026-10-06)

- Human explicitly approved a fresh multiply issue, one task branch/commit and one draft PR after independent verification; no merge, Architect review or service push. Created [issue #2](https://github.com/uhvogala/aitobuild_example/issues/2) at the unchanged baseline `f54d7dd42960d208d04fa1058ec333401c73eef5`. Both previous tasks/budgets were left untouched. GitHub CLI hosting, owner identity, repository ID, push permission and single-repository allowlist were checked first; credentials stayed inside the operator process.
- New preview `dp-804107c6-bbc7-4836-ac66-89411433405b` used separate state/session and constrained Grok execution. About 54.6 seconds, 13 approval rounds and 14 tool calls. Exactly the two approved files changed, existing `add` stayed unchanged, and Developer plus independent verification each passed **5 tests**, integer exit **0**, with successful cleanup and unchanged deadline/reservations.
- **Incorrect supervisor gate stopped publication:** the first `developer_edit_file` added a trailing newline to `old_text` absent from the baseline and read-tool result. Exact matching correctly rejected it without writing; the model reread, fixed the span and completed verified work. The supervisor incorrectly applied the tool-usability evaluation's zero-error criterion to a contribution. Operator clarification: recoverable mistakes are allowed during actual work; acceptance depends on the final outcome, not a flawless trace. Keep errors visible and exact matching unchanged. This was neither duplicate execution nor a reproduced publication defect.
- Raw [report](sim/.run-artifacts/example-publication-20261006/report.json), [evidence review](sim/.run-artifacts/example-publication-20261006/review.json) and [supervisor abort receipt](sim/.run-artifacts/example-publication-20261006/supervisor-abort.json) are retained. The service's successful `verified` receipt is preserved, but the rejected trial's original budget is now aborted, with identical deadline and reserved paths. No task container remained; trusted seed and remote `master` were unchanged and the repository has no PR. No model/verifier replay or budget reset occurred.
- Harness finding: a bare grading assertion gave an empty error string, and supervisor-level rejection initially left the verified budget active. The ignored one-off harness now records failed check names and aborts genuinely rejected unpublished budgets; saved-evidence review labels the separate abort receipt without rewriting the raw report. Delivery grading now allows recovered tool errors while requiring completion, correct scoped artifacts, successful final tests/independent verification, cleanup, approval and a valid unchanged budget. Focused checks accept recovered errors and reject incomplete work, invalid artifacts, failed verification and aborted budgets. The previous abort is not undone; future execution needs fresh approved identity. Publication/restart acceptance and M2 remain open.

### Pending Items

- Trial finding: the GitHub MCP connection targets an enterprise host, while the example repo is on github.com; authenticated repository lookup there returned 404. The publication-trial CLI explicitly checked github.com identity and target configuration, with credentials kept inside the process. Repeat host/identity checks for every future trial; the MCP host mismatch remains.
- Tool-usability finding: preserve EOF termination exactly when constructing `old_text`. Measure this in focused tool evaluations; it is not a delivery blocker when the agent recovers and verifies its work. Do not weaken unique exact-span rejection or reuse the aborted publication trial.
- Keep tool-usability evaluation separate from contribution acceptance. Zero unexpected errors is an evaluation criterion, not a production gate; unresolved failures, policy violations, missing approval, failed final verification/cleanup and aborted or expired budgets still block delivery.
- Trial finding: repository issue extraction supplies broad default paths/commands even when criteria specify exact files. Add operator-owned policy narrowing before immutable approval, and validated scope-aware approval handling. Do not replace approved policy afterward or simply auto-approve every tool to reduce the 12-round overhead.
- Trial harness: validate all presented read/discovery/search/write/command approvals and schema-aware evidence grading before another run. Preserve raw failures and missing-field reporting errors; any new task attempt needs explicit approval and separate durable identity/state, never a reset aborted budget.
- Validate the pinned Agent Framework/Foundry path with real tool calls, approval handling, and visible binding failures before adding provider support.
- Extend `GroupChatBuilder` kickoff from workflow construction to managed execution/session lifecycle controls.
- Validate MCP server/tool-name compatibility and approval semantics across target environments.
- Validate the locally fixed publication snapshot/recovery path in a fresh explicitly approved trial. Add an automatic approved-task worker and managed Architect/PM execution; published-target source/diff inspection is required for meaningful Architect review.
- Add CI-enforced no-duplicate capability audit checks for `reuse_native` items.
- Add structured audit/trace logging for ingress, dispatch decisions, meeting transitions, and escalation events.
- Formalize `approval_required` as an explicit workflow transition state across adapters and dispatcher decisions.
- Enforce the existing isolation contract consistently across preview runs, native agent tools, and container execution.
- Persist task, dedupe, approval, and session state and define retry/recovery behavior.
- Retain exact-edit and legacy patch regressions without weakening unique-match rejection; extend real-model editing acceptance beyond the focused fixture probe.

### Next Execution Order

1. Preserve the accepted CI-enforced baseline (M0).
2. Validate one constrained, live Developer task in a disposable repository (M1).
3. Connect an approved GitHub issue to a worker, task branch, verified commit, and draft PR (M2).
4. Add Architect review and bounded, executed blocker-resolution meetings (M3).
5. Add durable recovery, scheduling, audit traces, and operational controls (M4).
6. Introduce PM backlog planning for a supervised product-team pilot (M5).

See [MILESTONES.md](MILESTONES.md) for dependencies, acceptance criteria, and the immediate work queue.

The designated test GitHub repository is
[uhvogala/aitobuild_example](https://github.com/uhvogala/aitobuild_example).
Staged supervised trials may start as soon as a concrete workflow slice is ready
to test; full M2 delivery is not a prerequisite. Require approved task scope,
explicit repository configuration, disposable target checkouts and safeguards
appropriate to the slice. Routine simulations remain copied-fixture based; the
supervised issue implementation/verification trial above used the actual example
repository. Draft publication/review are implemented prototypes; their live
acceptance remains pending after the locally verified publication fixes above.