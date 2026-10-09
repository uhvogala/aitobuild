# Agent Instructions

## Project and Reference Docs

aitobuild is a supervised, opt-in product-team framework with configurable PM,
Architect and Developer roles. Approved tasks can produce verified draft PRs;
humans retain scope approval and merge authority. It is not an autonomous
issue-to-PR service.

- [README.md](README.md): current capabilities, setup, configuration and APIs.
- [PLAN.md](PLAN.md): target architecture, implementation notes and remaining work.
- [MILESTONES.md](MILESTONES.md): delivery milestones and acceptance criteria.
- [sim/README.md](sim/README.md): copied-fixture simulation and live-tool evaluation.

Check [Agent Framework Python samples](https://github.com/microsoft/agent-framework/tree/main/python/samples)
when framework behavior or APIs are unclear. Prefer native framework capabilities;
implement custom logic only for a confirmed gap.

## Setup and Quality Gates

Use Python 3.14+ and `uv` for all Python dependency operations:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
```

Add dependencies with `uv add <package>` and synchronize with `uv sync`. Do not
use ad-hoc `pip install` for project dependencies.

For mock-only copied-fixture execution:

```bash
env -u AITOBUILD_FOUNDRY_ENDPOINT -u AITOBUILD_FOUNDRY_API_KEY \
    uv run python sim/run_local_simulation.py --output sim/simulation-report.json
```

Require `succeeded=true`, zero command exit codes and successful cleanup. HTTP
success alone does not establish task completion. Normal API startup requires
`AITOBUILD_WEBHOOK_SECRET` and `AITOBUILD_INTERNAL_API_TOKEN`; internal endpoints
require `X-Internal-Token` by default.

## Architecture and Configuration

- Keep agent instances, prompts, model profiles, teams/coordinators, workflows
  and delegation configurable. Do not hardcode a team or universal contribution loop.
- [src/aitobuild/organization.py](src/aitobuild/organization.py) owns strict JSON
  definitions and immutable revisions. Definitions and run state have separate
  storage contracts. [config/organization.example.json](config/organization.example.json)
  is a definition probe, not an activated delivery recipe.
- Workflow JSON uses `python_graph` and maps directly to native `WorkflowBuilder`
  and `AgentExecutor`. Operations and Boolean predicates resolve through trusted
  operator registries, never inline code, expression languages or dynamic imports.
- Managed execution uses approved-task operation bindings. Direct configured
  agent nodes fail closed; role adapters bind scoped tools per run. Configuration
  cannot widen permissions, bypass approvals or verification, or reset budgets.
- Service and detached worker activation require explicit operator factories.
  Without them, default dispatch returns metadata rather than executing a graph.
  Definition storage, assignment and graph completion are not approval or activation.
- Managed PM planning/publication, Developer delivery, head-pinned Architect review
  and bounded same-PR correction are available. Explicit managed graphs can execute
  bounded native blocker meetings with durable receipts and exact-approved
  continuation; legacy meeting bootstrap only constructs workflows. Automatic
  dependency scheduling/blocker detection and hosted/distributed execution are not
  established capabilities.
- File-backed workers coordinate locally, not across distributed hosts. Journal
  capacity does not cover unmanaged HTTP runs; shared-token context is not multi-user
  authentication. Scheduler ticks need a caller; legacy meeting/scheduler state and
  non-repository trigger deduplication remain in memory.
- Managed meetings use configured native team members without persistent tools.
  Persist evidence/proposals and input digests; only an exact consumed operator
  decision permits unchanged-scope continuation under the original ledger/deadline.
  Scope change or unresolved blockers stop for human action. No model approval,
  write tools, uncertain discussion replay or budget reset. Combine meeting and
  delivery cleanup with `finally` so damaged meeting receipts cannot skip cleanup.

## Approval, Budget and Recovery Rules

- Actor identity comes from trusted operator/runtime context, never model output
  or request-supplied identity. Change operator binding revisions when registered
  behavior changes; active runs must refuse drift.
- Repository issue tasks require explicit human approval, repository identity,
  an operator-pinned base SHA and criteria under a Markdown Acceptance Criteria
  heading. Changed scope requires a new approved task.
- Preserve immutable approvals, revisions, receipts, original absolute deadlines
  and write reservations. Reopen initialized ledgers with `create=False`; missing,
  expired or aborted ledgers must not be recreated or reset.
- Persist consumed decisions and effect intents before writes. Only saved idle
  waiting checkpoints accept their exact one-shot decision. Interrupted running
  effects fail closed with cleanup/abort; finalizing retries recover metadata only.
  Never blindly replay failed tasks, uncertain writes or consumed approvals.
- Cancellation and shutdown must drain threaded stages before ownership/lock
  release. Busy controls return 409; other local owners observe durable cancellation
  intent. Failed worker slots require inspection and restart, not effect replay.
- PM task approval, exact issue creation and resolved dependency publication are
  separate gates. Actual issue IDs/numbers and final link-body bytes need their
  own saved approval. Complete receipts stage only unapproved Developer previews;
  each implementation needs its own approval and route.
- PM reconciliation uses remote reads and local metadata only, including after
  abort/expiry. It cannot create missing issues or approve links. List visibility
  lag is not authority to repeat POST/PATCH. Legacy PM approval tools remain
  separate in-memory prototypes; managed planning uses durable receipts.
- Architect reviews require a separate approved task and original zero-write
  ledger. Inspect complete immutable source AND base-diffs for every changed path;
  access counters are not semantic certification. Publish only the exact separately
  approved head-pinned COMMENT. Do not invent defects to exercise correction.
- Correction/re-review follow-ups remain separate unapproved tasks. Corrections
  stop verified until exact operator publication approval; update the same draft
  with a non-force head advance. Preserve lineage/round ceilings and original
  receipts. Reconciliation is read-only; every actual remote write needs a valid
  original budget and target/head checks. Do not merge automatically.
- Use shared durable writers for journals/receipts: atomic replacement, fsync,
  explicit permissions and symlink refusal. Runtime checks in product source must
  not use `assert`, which Python optimization removes.

## Scoped Developer Execution

- Never execute target tasks against the service checkout. Use private prepared
  checkouts from an explicitly configured trusted seed and pinned base. Preparation
  does not fetch or publish; correction seeds must already contain the reviewed
  head and public branch.
- Use the constrained Docker profile: read-only repository/root, offline commands
  and interactive input, dropped capabilities, CPU/memory/PID limits, expiry and
  mandatory cleanup. Do not describe host preparation or standalone/unbound runs
  as a hardened hostile-tenant sandbox. Browser/arbitrary MCP and legacy execution
  of native-bound previews are rejected.
- `DeveloperIsolationPolicy.allowed_paths` uses literal equality/prefixes, not
  globs. Keep baseline reads/discovery compatible with approved scope; independently
  restrict writes to authorized paths. File-discovery tool patterns may use globs.
- Use `developer_edit_file(path, old_text, new_text)` for exact unique-span edits.
  Do not normalize stale/ambiguous matches or bypass them with whole-file writes.
  Prefer bounded `developer_find_files` and `developer_search_files`; page only
  when needed. Token-prefix command filters are not shell isolation.
- Native approved-task runs bind `preview_id` to a fresh session and restore its
  frozen bundle on resume. Caller policy overrides and task switching are rejected.
  Service approval and native human input are distinct decision kinds.
- Independent verification uses only preparation-time operator commands and a
  fresh constrained session. Require `accepted=true`, `state=verified`, integer
  exit 0, successful cleanup and unchanged checkout fingerprint. Missing plans,
  mock execution and unreserved changes cannot qualify. `implemented` or `verified`
  is not publication approval or semantic review.
- Publication must recheck the exact verified snapshot, bytes/modes, target/head,
  approval and original budget. Read-only reconciliation may confirm an exact landed
  effect but must never invent missing effects or recreate remote objects.
- Recovered tool errors remain diagnostics, not automatic contribution rejection.
  Delivery requires correct scoped artifacts, passing final tests and independent
  verification, approvals, valid budgets and cleanup. Tool usability evaluations
  additionally require zero unexpected errors; do not apply that criterion to
  otherwise qualified contributions.

## Trials and Artifacts

Use [uhvogala/aitobuild_example](https://github.com/uhvogala/aitobuild_example) for
explicitly authorized, bounded real-repository trials. Require approved task scope,
explicit repository configuration, disposable checkouts and applicable safeguards.
Standing trial authorization does not bypass exact approvals, permit budget resets,
or authorize service pushes, merges or default activation.

Keep generated certificates, reports, sandboxes and credential files out of Git.
Preserve failed trial evidence; use fresh approvals/tasks/ledgers after terminal
failure rather than replaying old state. Canonicalize datetimes and tuples through
the existing JSON serializer when comparing persisted snapshots.

Use `uv run python -m sim.evaluate_tools --model <deployment>` for live tool
evaluations. Report actual artifact correctness, execution outcome and cleanup,
and distinguish supervised local acceptance from hosted/distributed deployment.
