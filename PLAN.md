# System Design Document: Agentic Product Team Framework

This document describes the target architecture, not a fully implemented system.
The implementation snapshot in section 7 distinguishes available capabilities
from remaining work. Operational milestones are tracked in [MILESTONES.md](MILESTONES.md).

## 1. Executive Summary
This document outlines the architecture for an autonomous, agent-driven software development framework. The system simulates a human product team (Product Manager, Architect, Developer) using Large Language Models operating within the **Microsoft Agent Framework (Python)**. 

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
* **Logic:** The Dispatcher parses the raw JSON payload, identifies the event type (e.g., "Issue Opened", "Test Failed", "PR Comment"), and invokes the appropriate Agent's isolated asynchronous workflow.

## 3. The Agent Roster (Entities & Scopes)

Each agent is an isolated instance of the `Agent` class with specific instructions and constrained tool access (via MCP Plugins).

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
Agents work in isolation. When the Dispatcher assigns an issue to the Developer Agent, it enters a solitary loop: reading the issue, writing files, and running bash commands. Context is kept minimal and focused solely on the task. The state of the project is entirely managed by the GitHub Kanban board and Git tree.

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

The following illustrates a complete, end-to-end feature lifecycle:

1.  **Trigger:** A human (or PM Agent) opens a new GitHub Epic: "Add Stripe Subscription Billing."
2.  **Dispatch:** Webhook fires. The `DispatcherAgent` routes the payload to the PM Agent.
3.  **Planning Sync (Meeting):** The PM Agent recognizes the complexity and calls `request_meeting(agenda="Stripe Integration Planning", participants=["Architect", "Dev"])`.
4.  **Architectural Scaffold:** The Architect proposes module boundaries without writing implementation files. The PM creates approved implementation issues, and the Developer implements any required interface definitions on a task branch. Architect repository-write access remains disallowed by the current role policy.
5.  **Execution (Async):** The Dispatcher routes Issue #1 to the Developer Agent. The Developer uses the Filesystem and Bash MCP tools to write the implementation and run unit tests.
6.  **Code Review (Async -> Sync):** The Developer opens a PR. The Dispatcher routes this to the Architect Agent. The Architect uses a Bash MCP tool to run `npm run lint` and `npm test` against the PR branch. 
    * *If Pass:* Architect approves via GitHub MCP. PR is merged.
    * *If Fail:* Architect leaves inline comments. If the Developer fails to fix the issues after 2 attempts, the Orchestrator forces a 1:1 "Code Review Sync" meeting to resolve the dispute.
7.  **Completion:** Once all issues linked to the Epic are closed, the PM agent asynchronously updates the main Epic and pings the human overseer.

## 6. Implementation Phasing

* **Phase 1: Foundation.** Set up the Python FastAPI webhook server, the `DispatcherAgent`, and integrate the Microsoft Agent Framework.
* **Phase 2: Tooling.** Configure the off-the-shelf MCP Servers (GitHub, Bash, Filesystem) and wrap them as standard plugins for the agents.
* **Phase 3: The Async Loop.** Implement the Developer workflow (Issue assignment -> File modification -> PR generation) without the Architect.
* **Phase 4: The Sync Engine.** Implement `GroupChatBuilder` for code review disputes (Architect + Dev) with strict termination conditions.
* **Phase 5: Full Autonomy.** Introduce the PM Agent and allow the system to ingest raw text prompts and manage its own backlog end-to-end.

## 7. Implementation Snapshot (2026-10-06)

### Implemented Building Blocks

These items describe code and local test coverage, not production certification.

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

### Current Limits

- `developer.async.webhook` returns a task bundle; no worker consumes it automatically.
- Supported repository issue events produce target-specific bundles and require approval-time base SHA pinning. Checkout preparation validates membership in the explicitly configured local seed/base branch, not live GitHub repository identity or remote freshness. No automatic remote lookup/fetch occurs. Legacy fixture payloads without repository context retain prototype routing; unsupported repository webhook events and missing issue criteria are rejected.
- GitHub branch/commit/draft-PR publication is implemented behind the optional allowlisted `gh_cli` adapter; the reproduced snapshot/recovery gaps are fixed locally with regressions. Live acceptance remains pending. The CLI is not installed in the current service container. No live task-result publication has been tested.
- PM draft/write-approval stores remain in memory; restart loses those records. Architect/PM tool binding is implemented, but managed role run/resume and automatic published-target review orchestration are not wired into the HTTP workflow. Published-PR reads expose metadata and filenames, not a reviewable diff or target file content.
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

### Pending Items

- Trial finding: the GitHub MCP connection targets an enterprise host, while the example repo is on github.com; authenticated repository lookup there returned 404. Explicitly configure hosting/API endpoint and verify identity before publication. Operator bootstrap used github.com access, with credentials kept inside the process.
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