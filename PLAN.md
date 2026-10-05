Here is the comprehensive design document for your automated agentic software development framework, synthesizing all the architectural decisions, topologies, and technologies we've established.

---

# System Design Document: Agentic Product Team Framework

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
The system is event-driven. Rather than a monolithic loop, a lightweight `DispatcherAgent` handles incoming events.
* **Input:** GitHub Webhooks (FastAPI/Flask endpoint).
* **Logic:** The Dispatcher parses the raw JSON payload, identifies the event type (e.g., "Issue Opened", "Test Failed", "PR Comment"), and invokes the appropriate Agent's isolated asynchronous workflow.

## 3. The Agent Roster (Entities & Scopes)

Each agent is an isolated instance of the `Agent` class with specific instructions and constrained tool access (via MCP Plugins).

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
4.  **Architectural Scaffold:** In the meeting, the Architect establishes the module boundaries. The meeting terminates by the Architect pushing interface definitions (`IStripeService.ts`) to a new branch and generating 3 separate implementation issues via the GitHub MCP.
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

## 7. Foundation Implementation Status (2026-04-10)

### Completed Items

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
- Runtime bootstrap with Foundry-first and mock fallback mode.
- Runtime binding abstraction for Agent Framework classes with optional concrete `Agent` instantiation.
- `meeting.bootstrap` runtime kickoff hook that attempts native `GroupChatBuilder` workflow construction and safely falls back to mock startup when unavailable.
- Proactive Architect scan workflow output contract with structured findings and issue proposal suggestions.
- Configurable developer preview requirement before `developer.async.webhook` dispatch with approval gating.
- Operator-visible pending preview queue retrieval for approval workflows.
- Practical Developer run harness with dry-run/live modes and policy-enforced command/path checks.
- Configurable Developer execution backend (`mock` or `subprocess`) with CLI command timeout controls.
- Persistent container-session execution mode for Developer runs (single container per session with start/stop lifecycle).
- Agent Framework role-tool wiring using `Agent(..., tools=[...])` for Developer command/file/session operations.
- MCP-backed Developer command/filesystem adapters with explicit fail-fast behavior (no local fallback in MCP mode).
- Developer Agent diagnostics endpoint (`/internal/runtime/developer-agent`) for runtime readiness, tool inventory, and session support visibility.
- Developer Agent run endpoint (`/internal/developer/agent/run`) with optional session creation and approval replay support.
- Capability audit matrix for reuse/wrap/custom decisions.
- Quality gates passing: `pytest`, `ruff`, and `mypy`.

### Pending Items

- Strengthen native Agent Framework binding coverage with additional provider-specific constructor support and richer runtime diagnostics.
- Extend `GroupChatBuilder` kickoff from workflow construction to managed execution/session lifecycle controls.
- Validate MCP server/tool-name compatibility and approval semantics across target environments.
- Add end-to-end Developer async loop from approved preview to branch/PR workflow lifecycle (branch creation, commit, PR open/update).
- Add CI-enforced no-duplicate capability audit checks for `reuse_native` items.
- Add structured audit/trace logging for ingress, dispatch decisions, meeting transitions, and escalation events.
- Formalize `approval_required` as an explicit workflow transition state across adapters and dispatcher decisions.
- Define executable sandbox policy contract module for later container orchestration phase.

### Next Execution Order

1. Developer async loop: connect approved preview execution to branch/PR lifecycle.
2. Governance: enforce capability-audit overlap checks in CI.
3. Observability: add structured audit/trace logging across trigger, meeting, and escalation lifecycle.