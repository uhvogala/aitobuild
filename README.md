# aitobuild

An agentic product-team framework with Product Manager, Architect, and Developer
roles, built on Microsoft Agent Framework and FastAPI.

Current stage: supervised, opt-in issue-to-draft execution. This is
not an autonomous issue-to-PR service. Approved tasks can produce tested draft
PRs in disposable repositories, with humans retaining merge authority.
Configurable organization definitions, instance
factories and native workflow admission are available as library APIs. Persisted
delegation and revision-pinned managed delivery graphs are also library APIs;
opt-in service routing and detached local workers consume approved configured tasks.
Team structure and lifecycle are not fixed in code.

## Start here

- [MILESTONES.md](MILESTONES.md): current progress, ordered milestones, and exit criteria.
- [PLAN.md](PLAN.md): target architecture, implementation notes and remaining work.
- [sim/README.md](sim/README.md): local simulation and optional Docker/live-model setup.
- [.devcontainer/certs/README.md](.devcontainer/certs/README.md): host certificate setup.
- [agents.md](agents.md): repository conventions for contributors and coding agents.

## Current status

| Area | Implemented | Remaining |
| --- | --- | --- |
| Organization configuration | Immutable revisions, native graphs, durable assignments/runs, opt-in workers, native PM coordination and bounded correction follow-ups | Broader live configuration coverage and distributed controls |
| Ingress and routing | Signed webhooks, internal auth, issue extraction, durable scope approval/dedupe and activated task queueing | Hosted GitHub webhook delivery and deployment activation |
| Developer execution | Preview approval, scoped exact-text edits, independent verification and supervised live same-PR correction delivery | Stronger isolation and broader live coverage |
| Native model runtime | Native PM planning, operator-triggered merged-prerequisite handoffs, approved Developer delivery and head-pinned Architect review | Broader PM semantics, automatic dependency scheduling and hosted model execution |
| GitHub integration | Verified drafts, immutable reads, head-pinned COMMENTs and durable exact-approved PM issue publication | Hosted delivery and broader live coverage |
| Meetings and proactive scans | Opt-in bounded native blocker meetings, durable transcripts/proposals, exact-approved continuation through supervised local delivery/review and deterministic scans | Automatic blocker detection, repeated meetings, hosted/distributed acceptance and real repository scans |
| Operations | Tick endpoint, policy checks, local durable previews/issue-task state and CI quality gates | Background tick driver, broader durable state, tracing, stronger isolation |

The managed PM-to-Developer-to-Architect flow is validated for bounded supervised
local execution. PM issue creation and resolved dependency links need separate
exact approvals; staged Developer tasks remain unapproved until a human approves
their implementation. Review and correction follow-ups also need their own task
and publication approvals. Automatic dependency scheduling, hosted webhook delivery,
distributed execution and automatic blocker detection remain outside the supported flow.
Explicit managed graphs can resolve reported blockers through bounded read-only
meetings before continuing the unchanged approved task.
An explicitly bound dependency handoff can inspect merged prerequisites read-only
and stage a fresh unapproved dependent at a new operator-pinned base. It does not
approve work, start a scheduler or merge PRs.

GitHub defaults to mock mode. Real-repository trials use
[uhvogala/aitobuild_example](https://github.com/uhvogala/aitobuild_example) with
approved scope, explicit repository configuration and disposable target checkouts.
Never execute target tasks against the service checkout. Original deadlines,
receipts and consumed approvals are preserved; failed or uncertain effects must
not be blindly replayed.

Implementation and acceptance history is recorded in [PLAN.md](PLAN.md#implementation-notes).
See [agent tool evaluation](sim/README.md#agent-tool-evaluation) for model comparison,
token/cache usage and artifact checks. The broad standalone tool suite is not fully
certified. Large outputs use private files and bounded paging; oversized API prompts
are rejected.

The [CI workflow](.github/workflows/ci.yml) runs pytest, Ruff, mypy and the mock
fixture simulation on Python 3.14 with locked uv dependencies and ripgrep. It
retains available simulation reports for 14 days, including failures, and needs
no Azure credentials.

## Quick start

### Organization definition foundation

[config/organization.example.json](config/organization.example.json) describes
named agents (multiple instances per role), prompts, teams/coordinator, Python-native
workflow graphs and event routes with coordinator/rule/human
delegation. Optional `model_profile` and `tool_profile` references resolve against
operator registries, not inline credentials/callables. Tool profiles are restricted
to their declared role and role-policy ceiling; operators must register existing
policy/approval-enforcing tool handlers. Developer tools are still bound per
approved run, not permanently attached by config. Capacity is enforced for durable
assignment claims, not by the existing role-based HTTP runtime. There are no
`skills` labels or configurable permission grants.

Validate and persist an immutable revision through the library API:

```python
from pathlib import Path
from aitobuild.organization import FileDefinitionStore, load_organization_definition

definition = load_organization_definition(Path("config/organization.example.json"))
store = FileDefinitionStore(Path("/tmp/aitobuild-definitions"))
snapshot = store.save(definition)
restored = store.get(snapshot.organization_id, snapshot.revision)
```

Use a trusted service-owned storage directory. Changed definitions create new
revisions; callers must select a revision explicitly. Storage does not activate
or approve it. The example workflow is a harmless definition probe, not a delivery
recipe. Existing service bootstrap/dispatch and execution safeguards are unchanged.
Recorded delegation and managed run/revision pinning are library APIs; opt-in service
activation is described below. Delivery acceptance follows [MILESTONES.md](MILESTONES.md).

Assemble configured instances and the harmless native example in mock mode:

```python
from aitobuild.config import RuntimeConfig
from aitobuild.organization_runtime import (
	WorkflowOperation, bootstrap_organization, build_organization_workflows, create_model_profile,
)

profile = create_model_profile(RuntimeConfig(
	foundry_endpoint=None, foundry_api_key=None,
	foundry_model="test-model", allow_mock_model=True,
))
runtime = bootstrap_organization(
	snapshot, model_profiles={"operator_default": profile},
	default_model_profile="operator_default",
	state_dir=Path("/tmp/aitobuild-agent-state"),
)
workflows = build_organization_workflows(runtime, operations={
	"definition_probe": WorkflowOperation(lambda message: message),
})
```

Native agent invocations require native model profiles, not mock handles. Role
templates precede configured guidance, and Developer memory is isolated by
organization/revision/agent. Workflow construction is not managed execution,
delivery acceptance or approval; task scope/budgets/verification remain separate.

To select the `DeepSeek-V4.1-Flash` deployment on an already configured Foundry
endpoint, set:

```bash
export AITOBUILD_FOUNDRY_MODEL="DeepSeek-V4.1-Flash"
```

Alternatively, register a named native model profile created with
`RuntimeConfig(foundry_model="DeepSeek-V4.1-Flash", ...)` and reference that profile
from a configured agent's `model_profile`. Deployment names pass through unchanged;
selecting this model does not change the application default, activate a workflow
or relax tool/approval policy. Provider availability requires a live check.

Workflow documents use `format: "python_graph"`, a `start` node, `nodes`, `edges`
and explicit `outputs`. Nodes reference configured agents or registered operations.
Edges map directly to SDK `add_edge`, `add_fan_out_edges`, `add_fan_in_edges` and
`add_switch_case_edge_group`; conditional feedback edges support bounded loops.
`condition` names resolve through the caller's `predicates` registry, never an
expression string. Switch `cases` are ordered condition/target pairs with an
optional `default` target. Predicates are synchronous Python callables returning
an actual boolean. Native execution uses `WorkflowBuilder` and `AgentExecutor`.

Operations consume one incoming message and return a value (or an awaitable).
Native fan-in passes a list; agents retain SDK message/response types rather than
implicit string conversion. Operation results are forwarded to connected nodes
and published only when selected in `outputs`. Direct bindings are registered
read-only operations; managed operations use the separate approved-task runner below.
Unknown fields/kinds, inline agents/files, HTTP/MCP, dynamic references, invalid
topology and routed-team escapes fail closed. Config cannot write service state.
Operator ceilings bound document bytes, node/connection counts and native runner
iterations, not arbitrary synchronous callback duration. Optional direct-workflow
SDK checkpoint storage is caller-owned and does not certify managed execution.

No PowerFx, .NET or expression evaluator is used or required. Only Python graph
documents are accepted. Literal payloads, including strings starting with `=`,
are just data.

### Durable assignment claims

[src/aitobuild/organization_assignments.py](src/aitobuild/organization_assignments.py)
provides `AssignmentService`, `AssignmentProposal` and an `AssignmentStore` protocol
with a locked, atomic/fsynced `FileAssignmentStore`. Definition revisions and the
assignment journal are separate. This is a trusted operator/runtime library API,
not an HTTP endpoint or automatic coordinator invocation.

`assign` requires an explicit organization revision, event and approved preview.
The configured route determines team, workflow, eligible agents and strategy.
Coordinator decisions require the configured `coordinator_id`; human decisions
require `human_id`; rules select their configured target without overrides.
Proposals contain only `agent_id` and a nonblank rationale. Identity arguments must
come from authenticated operator/runtime context, never model-controlled fields.

The service reopens the original budget with `create=False` through an operator
`budget_path_for(preview_id)` locator. Prepare the approved task's budget first;
missing, aborted, expired or scope-mismatched ledgers cannot qualify. Claims freeze
the full approved bundle/base, approval timestamp, revision, route/team/workflow,
eligible selection, rationale, actor and budget path. No approval or budget is
created/reset by assignment.

One task/preview has one owner across routes, teams, organizations and revisions.
Identical claims are idempotent; changed assignments fail. Active capacity is counted
by organization/agent across revisions, using the most restrictive active capacity
ceiling. `finish` records an immutable completed/failed/cancelled outcome; failed and
cancelled tasks abort the original budget without changing deadline/reservations.
The transition guard and journal update share the ownership transaction. Terminal
records release capacity for other tasks, never reclaim the original task.

Assignment completion is metadata, not independent verification, semantic review,
publication approval or sandbox cleanup. Existing operator endpoints are unchanged;
managed execution is described below. No GitHub assignee write occurs.

### Managed native workflows

[src/aitobuild/organization_runner.py](src/aitobuild/organization_runner.py) adds
`ManagedWorkflowRunner`, registered `ManagedOperation` bindings and a separate
`RunStore` contract with atomic/fsynced `FileRunStore`. Runs pin assignment/scope,
definition/workflow revision, operator `binding_revision`, native session identity,
checkpoint identity/integrity, consumed decisions and cleanup receipts. Changed
definitions cannot replace active revisions; changed operator bindings fail closed.

Supply `actor_provider` from trusted operator/runtime context. `runner.assign`
binds coordinator identity from that provider, never a proposal or graph input.
Ownership, frozen approval/base/scope and the original existing budget are
revalidated on each active run/resume, operation and checkpoint save. Establish the
approved task ledger first (normally `DeveloperDeliveryWorker.prepare`) and assign
using `budget_path_for=worker.budget_path`; no missing/aborted budget is created/reset.

`start(assignment_id, input=...)` records one immutable invocation. Duplicate calls
return the receipt without replacing input or replaying completed work. `resume`
answers saved native `human_input`; `approve` accepts only a trusted operator's
Boolean `service_approval` decision. These kinds are not interchangeable. Decisions
are persisted before continuation, and native tool approval retains exact arguments.
Restart resumes only saved idle waiting checkpoints. An interrupted `running`
receipt fails closed with cleanup/abort, not blind side-effect replay. `finalizing`
recovers terminal/assignment metadata only. Cancel in-process asyncio tasks and
await them; `cancel` handles idle/recovered runs. Busy runs reject competing calls.
Threaded delivery calls drain before ownership is released; worker limits still apply.

[src/aitobuild/organization_delivery.py](src/aitobuild/organization_delivery.py)
provides `ManagedDeliveryBindings` for configurable `delivery_prepare`,
`delivery_implement`, `delivery_verify` and optional `delivery_publish` nodes.
Definitions choose ordering/branches/joins through native SDK edges, not a fixed
team or universal contribution loop. `NativeDeliveryImplementation` resolves the
selected configured Developer using `runtime_for(pinned_snapshot)`, persists SDK
sessions/approval content and binds existing per-run exact-span tools to the private
prepared target and constrained offline Docker adapter. Browser/MCP and legacy edits
are rejected. Recovered tool errors remain diagnostics, not automatic rejection.
Verification/publication reuse guarded worker APIs with preview identity, not
graph-supplied commands or target/publication metadata. Publication requires an
explicit operator GitHub binding; mock publication is opt-in for fixtures only.

Managed graphs currently admit operation nodes, including that native Developer
adapter; direct configured `agent` nodes fail closed.
`completed` means the selected graph and cleanup finished, not independent
verification, semantic review or publication approval; implement-only graphs stop
at `implemented`. Default HTTP workflows remain unmanaged without explicit
activation; journal capacity does not govern unmanaged HTTP runs. Distributed
controls remain pending.

### Opt-in managed service

[src/aitobuild/organization_service.py](src/aitobuild/organization_service.py) connects
the managed runner to the app through
`create_app(config, managed_service_factory=operator_factory)`. The normal server
entrypoint and configuration alone do not activate it. The factory receives
`ManagedServiceContext` with the app-owned preview registry, delivery worker, tools
and state directory. Register immutable definitions, assignment/run stores,
`ManagedDeliveryBindings`/`NativeDeliveryImplementation`, predicates and mandatory
cleanup in the returned `ManagedOrganizationService`; supply a trusted
`runtime_for(pinned_snapshot)` and native model profiles for native delivery.
Existing constrained-container, role, scope and publication gates still apply.

Operator-owned `ManagedRoute` records select exact repository name/ID, organization,
explicit definition revision and configured event. There is no request-supplied
revision, workflow, actor, command plan, seed path or budget override. Internal
authentication is required for activation. `operator_id` identifies the trusted
shared-token operator context, not a body field or multi-user identity system.
Rules use their configured target. Human delegation waits for a strict
`AssignmentProposal` with only `agent_id`/`rationale`. Coordinator delegation invokes
an operator-registered read-only proposal callback under the prepared task's original
deadline; the configured coordinator ID is bound by service context, never returned
by the model. Callback failure/cancellation aborts that ledger. These trusted callbacks
must preserve role/tool ceilings; arbitrary operator Python is not sandboxed.

For activated repository tasks, authenticated preview approval and signed webhook/
authenticated trigger delivery invoke the configured graph automatically. Preparation
uses only the approved target/base and original worker ledger. Duplicate dispatch
metadata, including a durable `dedupe` result, resolves existing ownership and run
receipts; the dispatch marker cannot suppress recovery after an earlier service
interruption. New activation revisions apply to unclaimed tasks, not existing runs.
Unactivated repositories and apps without a factory keep metadata-only dispatch.

Optional internal controls (all require `X-Internal-Token`):
- `GET /internal/organization/tasks/{preview_id}` returns `managed_run` or null.
- `POST /internal/organization/tasks/run` takes `preview_id` and, for human delegation,
	an optional `proposal` containing only `agent_id` and `rationale`.
- `POST /internal/organization/tasks/approve` takes `assignment_id`, saved
	`request_id` and an actual Boolean `approved` for service approval.
- `POST /internal/organization/tasks/resume` takes `assignment_id`, saved
	`request_id` and JSON `response` for native human input only.
- `POST /internal/organization/tasks/cancel` takes `assignment_id` for an idle or
	recovered invocation. Busy invocations return 409; active task cancellation drains
	guarded delivery calls before releasing locks.

Activated approval responses and dispatch metadata include `managed_run` when a run
exists. Approval/dispatch HTTP success is not delivery success: inspect its state,
pending requests and guarded verification/publication receipts. A waiting human
selection has no run yet; status does not certify budgets or delivery. Cancellation
uses frozen assignment ownership even if mutable preview state becomes unreadable.
Failed/expired/interrupted runs retain terminal evidence and never get fresh budgets.

Without a worker factory, execution awaits the graph in the calling request.
The optional detached worker follows below. Capacity excludes unmanaged HTTP runs.

### Opt-in detached local worker

[src/aitobuild/organization_worker.py](src/aitobuild/organization_worker.py) adds a
separate `WorkerStore`/atomic, fsynced `FileWorkerStore` journal. Add a worker factory
alongside the managed service factory:

```python
from aitobuild.app import create_app
from aitobuild.organization_worker import FileWorkerStore, ManagedOrganizationWorker

def worker_factory(service, context):
	return ManagedOrganizationWorker(
		service=service,
		store=FileWorkerStore(context.state_dir / "organization-worker.json"),
		max_workers=2,
		recover_approved=False,
	)

app = create_app(
	config,
	managed_service_factory=operator_factory,
	managed_worker_factory=worker_factory,
)
```

Serve that app with ASGI lifespan enabled. FastAPI starts bounded worker tasks and
drains them on shutdown. Approval, signed webhook and authenticated trigger handling
durably enqueue before returning; they do not await graph execution. Responses/status
include `managed_admission` plus the existing `managed_run` when available. A queued
receipt is not execution success, verification, publication approval or semantic
review. The normal server still supplies neither factory.

Admission freezes the approved bundle/base, approval time, activation revision,
binding revision and trusted operator command history before assignment exists.
Definition updates cannot silently change queued work. Human selections and exact
service-approval/native-input decisions are durably queued, one-shot and distinct;
request bodies cannot supply actor identity or widen scope. Before active work, the
worker and runner recheck approval, ownership, bindings and original budgets. Known
capacity/lock contention defers the same receipt without resetting its ledger.

Existing queued receipts recover automatically. `recover_approved=True` additionally
discovers activated approved tasks with no assignment or worker receipt in a bounded
startup scan (`startup_limit`, default/max 500); it does not adopt old terminal tasks
or replay uncertain effects. Corrupt state fails closed. Saved idle native waits
require their exact decision; interrupted unclaimed admission or native running work
fails with cleanup/abort, never automatic replay. Ready runs may safely start, and
saved finalizing/terminal results recover metadata only.

Detached `cancel` accepts exactly one of `preview_id` or `assignment_id`, including
pre-assignment tasks. Durable cancellation intent survives restart. Active local
cancellation and shutdown drain guarded threaded stages before ownership release.
Another process's busy cancellation returns 409 while retaining intent; its owning
worker observes that intent on the polling interval. Idle waiting intent is recovered
on startup. `GET /internal/organization/worker` requires internal authentication and
reports live worker slots, active previews and retained lifecycle errors. A failed
receipt write stops that slot; inspect evidence and restart rather than replaying
effects or resetting budgets.

This is local file-lock coordination, not distributed execution, hostile-tenant
certification or multi-user authentication.

### Scoped native PM and Architect operations

[src/aitobuild/organization_roles.py](src/aitobuild/organization_roles.py) exposes
`NativeManagedRoles.operations` for explicit operator-owned managed graphs. Use
`pm_propose_assignment` for a configured PM's strict `agent_id`/`rationale` proposal
against a pinned `proposal_event`. It returns metadata only: no assignment claim,
actor identity, issue write or approval is taken from the model.

`architect_review_published` requires delivery/GitHub bindings and an operator-owned
`review_target_for(context)` returning `PublishedReviewTarget(preview_id=..., head_sha=...)`.
Use a separately approved review task and original ledger, matching the published
repository/issue/base and containing all changed paths. Never reset the Developer's
completed/expired ledger or reuse its immutable assignment for a different owner.
Register the adapter's `cleanup`, operations and binding revision with the runner;
selected roles must use configured native agents without persistent tool profiles.
No default bootstrap, route or universal contribution loop is installed.

The native Architect has read-only access to the whole repository at the pinned
review head, independent of the Developer's allowed edit paths. It chooses context
needed to assess architecture, style, existing patterns, reuse and compatibility:
`architect_find_repository_files` discovers tracked paths, `architect_search_repository_files`
searches literal text with file/line continuation offsets, and
`architect_read_repository_source` reads any regular UTF-8 repository file.
Repository reads are hash-verified and commit-pinned, never taken from a mutable
checkout. They cannot select another repository/head or access host files.

Discovery supports complete trees up to 10,000 entries and pages of at most 50 paths;
truncated/ambiguous trees fail closed rather than silently hiding context. Source
files support up to 1 MiB, with context pages up to 4,000 bytes. Search scans at most
10 files per call, returns at most 20 matches and explicitly identifies skipped
binary files. Symlinks/submodules are discoverable but not traversed. Original task
deadlines and inline result limits still apply; smaller pages may be needed.

Publication-bound source/diff reads remain separate, paged at up to 1,000 bytes,
using immutable head blobs and the pinned base commit rather than truncated PR
patches. Mode-only changes are reported even when the text diff is empty.
Complete contiguous source AND diff access for every changed path is required
before a proposal. Context reads do not replace that minimum, and access counters
do not certify semantic understanding. Whole-repository reads do not expand write,
command, correction or publication permissions.

Legacy workspace Architect tools also default to repository-wide read scope.
Runtime paths `.git/`, `.venv/`, `secrets/` and `.aitobuild/` remain excluded;
traversal and symlinks escaping the workspace are rejected. Explicit operator
policy restrictions still apply. These workspace tools are not the immutable,
GitHub-bound native review tools described above.

The exact saved target/body requires a separate one-shot service approval. Resume
does not rerun the model; it rechecks saved evidence, head/scope and the original
budget immediately before COMMENT publication. `submitting`/uncertain effects
cannot be replayed. Recovered tool errors remain diagnostics, not automatic rejection.
APPROVE, REQUEST_CHANGES, merge, implementation writes and arbitrary source targets
are unavailable. General Architect tools also expose `architect_read_published_source`
and `architect_read_published_diff` under their existing tool-approval policy.

Existing operator service/worker factories may register these operations.
Published-review admission below adds opt-in HTTP routing, not default activation.
Managed PM publication and bounded blocker meetings are described below.

### Managed Blocker Meetings

[src/aitobuild/organization_meetings.py](src/aitobuild/organization_meetings.py)
provides `NativeManagedMeetings`, strict `BlockerRequest`/`MeetingBinding` contracts
and atomic/fsynced `FileMeetingStore` receipts. Register it explicitly in an approved
task graph; the legacy meeting bootstrap still only constructs workflows.
The graph's preceding trusted operation must supply an agenda and blocker evidence,
not arbitrary actor, task, model or budget overrides. Automatic mid-implementation
blocker detection is not supplied.

```python
from aitobuild.organization_meetings import NativeManagedMeetings, MeetingBinding, MeetingLimits

meetings = NativeManagedMeetings(
	runtime_for=runtime_for,
	state_dir=context.state_dir / "meetings",
	runs=runs,
	bindings={blocked_workflow_id: MeetingBinding(
		participants=participant_ids,
		resolver=resolver_id,
		limits=MeetingLimits(max_rounds=4, max_transcript_bytes=32000),
	)},
	invoke_timeout_seconds=180,
)
native = NativeDeliveryImplementation(
	worker=context.worker, runtime_for=runtime_for, tools=context.tools,
	state_dir=context.state_dir / "native",
	continuation_for=meetings.continuation,
)

async def cleanup(task_context):
	try:
		await meetings.cleanup(task_context)
	finally:
		await delivery.cleanup(task_context)

operations = {**delivery.operations, **meetings.operations}
```

Construct `delivery` with that `native` implementation, and register `operations`,
the combined cleanup and a new operator binding revision in the service/runner.
Configured `meeting_resolve_blocker` nodes receive strict `BlockerRequest` data.
Participants/resolver must be named members of the frozen task team, use native
model profiles and have no persistent tool profiles. Native `GroupChatBuilder`
uses bounded round-robin discussion; a configured participant then emits the strict
`continue`, `scope_change` or `escalate` proposal. No meeting participant receives
filesystem, command, GitHub, implementation or approval tools.

Blocker evidence, task/revision/scope/binding pins, original absolute deadline,
transcript and proposal are durable. A `continue` proposal waits for its exact
one-shot service approval through the existing managed task controls. The consumed
decision pins the saved input digest; Developer sessions also pin the approved
guidance and revalidate it on tool calls/resumes. Task scope, approvals, verification
and publication gates remain unchanged. Rejection, scope change, unresolved output,
timeout, cancellation or interrupted execution stop the graph; scope change needs
a newly approved task. Saved idle approvals resume without rerunning the discussion.
Missing/corrupt/drifted receipts cannot authorize continuation or recreate budgets.

One meeting invocation per managed run is supported. Repeated meetings, automatic
blocker detection, automatic failed-task recovery and hosted/distributed acceptance
remain separate work. Supervised local meeting-to-reviewed-draft execution and
rejection/scope-change stops are accepted; this does not certify general tool use
or enable default activation.

### Managed PM Planning and Issue Publication

[src/aitobuild/organization_planning.py](src/aitobuild/organization_planning.py)
provides opt-in admission, strict planning contracts, immutable file-backed plans
and `ManagedPlanning`. Extend an existing explicit operator-owned service factory;
normal startup and the example organization are unchanged. Configure a PM-only
event route whose native graph uses `pm_plan_issues`. Agent/team IDs, prompts,
models, delegation, limits and graph ordering are configuration-owned.

```python
from aitobuild.organization_planning import (
	ManagedPlanning, PlanningAdmission, PlanningLimits, PlanningRoute, PlanningScope,
)

admission = PlanningAdmission(
	definitions=definitions, previews=context.previews,
	state_dir=context.state_dir / "planning-admission",
	routes=(PlanningRoute(
		repository=target_repository, repository_id=target_repository_id,
		organization_id=snapshot.organization_id, revision=snapshot.revision,
		event=planning_event,
	),),
	operator_id=operator_id,
)
planning = ManagedPlanning(
	admission=admission, github=publication_github,
	state_dir=context.state_dir / "planning", lookup_max_pages=10,
)
preview = admission.offer(
	request_id=operator_request_id, objective=planning_objective,
	scope=PlanningScope(
		repository=target_repository, repository_id=target_repository_id,
		base_revision=operator_base_sha, base_branch=operator_base_branch,
		limits=PlanningLimits(max_issues=4, max_plan_bytes=12000),
	),
	developer_policy=operator_child_policy,
)
```

These names represent trusted factory inputs, not HTTP/model fields. Register
`NativeManagedRoles(..., planning=planning)` operations and cleanup with the runner,
and `ManagedOrganizationService(..., planning=admission)` using the same definitions,
app-owned previews and operator identity. Change the operator binding revision when
registered behavior changes. No new public write endpoint or activation flag exists.

1. Approve the offered preview through the existing authenticated preview endpoint.
   It pins repository ID/name, base SHA/branch, immutable definition revision,
   request, limits and child policy. Planning has no checkout, commands or file
   writes. Its separate original deadline includes approval waits; lost initialized
   ledgers, expiry, abort and drift cannot reset it.
2. The native PM receives only a read-only planning-request/target inspection tool.
   Strict output contains bounded issue keys, titles, objectives, acceptance criteria,
   labels and acyclic dependencies. Complete inline inspection and final proposal
   are saved in an immutable digest-bound plan. Changes need a fresh task/approval;
   model output cannot choose actors, repositories, approvals or implementation.
3. Inspect task status and approve the exact saved creation request through
   `/internal/organization/tasks/approve`. Its nonce binds target pins and exact
   title/body/labels, including stable markers. The consumed decision and per-issue
   intent persist before POST. Target identity/base and the original budget are
   rechecked immediately before each write, not only at proposal time.
4. Dependencies stop at a second saved approval after creation. It contains actual
   issue IDs/numbers, original bodies and exact resolved body bytes. Approval replaces
   only the planned dependency section with Markdown `Depends on #N` references,
   not GitHub native blocked-by relations. Original live content is rechecked
   immediately before PATCH. Restart uses saved content without model/creation replay.
5. Only fully confirmed issue/link receipts stage deterministic Developer previews.
   They remain unapproved, undispatched and unassigned, with real issue identity,
   pinned repository/base and child policy. Implementation requires a separate
   approval and configured Developer route; planning never prepares or executes it.

A batch is not transactional: cancellation, rejection, expiry or drift can leave
partial approved effects. Preserve them for inspection. `planning.reconcile(assignment_id)`
uses remote reads and local metadata only. It confirms exact landed effects and
recovers partial preview staging, even after budget abort/expiry, but cannot create
missing issues, approve unresolved links, repeat uncertain writes or restart models.
Missing, ambiguous, drifted or incomplete effects fail closed. Bounded full issue
list paging refuses truncated lookup. Local locks are not distributed recovery;
GitHub PATCH has no transactional compare-and-swap against concurrent writers.

GitHub list visibility can lag new writes: a temporary reconciliation refusal is
not authority to repeat a POST/PATCH. Inspect later via remote reads under the same receipts;
never reset the original ledger. Legacy `PlanDraftStore` and
`IssueWriteApprovalStore` remain separate in-memory prototype tools.

### Managed Dependency Handoffs

[src/aitobuild/organization_dependencies.py](src/aitobuild/organization_dependencies.py)
provides `ManagedDependencies`. Inside the same operator-owned factory used for PM
planning, configure a Developer-only event route and pass the binding to the service:

```python
from aitobuild.organization_dependencies import ManagedDependencies

dependencies = ManagedDependencies(
	planning=planning, worker=context.worker,
	state_dir=context.state_dir / "dependency-handoffs",
	routes=(PlanningRoute(
		repository=target_repository, repository_id=target_repository_id,
		organization_id=snapshot.organization_id, revision=snapshot.revision,
		event=developer_event,
	),),
	lookup_max_receipts=100,
)
# Pass planning=admission and dependencies=dependencies to ManagedOrganizationService.
```

No default activation changes. The binding must share the service-owned planning
admission, previews, definitions, worker and operator. Configured delegation and
native graph operations still decide execution; readiness does not select a team
or replace a contribution workflow. Change the operator binding revision when
registered behavior changes.

For prerequisites published by earlier worker instances, explicitly pass their
trusted receipt owners as `prerequisite_workers=(archived_worker,)`. At most eight
historical owners are supported, for saved delivery reads only; ambiguous receipt
ownership fails closed. Preserve their immutable preview/publication metadata.
This does not reopen old execution ledgers or reuse implementation approvals.
New preparation/execution always uses the active service-owned worker and a fresh
task budget; never copy expired ledgers into fresh execution storage.

Call the authenticated operator endpoint with only these fields:

```json
{
  "planning_assignment_id": "<saved PM assignment>",
  "issue_key": "<dependent plan key>",
  "base_revision": "<40-character current base-branch SHA>"
}
```

`POST /internal/organization/dependencies/offer` inspects exact saved issue/link
receipts and all prerequisite deliveries using GitHub reads only. Missing or
unpublished prerequisites and unmerged PRs return `state=waiting` without a new
preview. A ready prerequisite requires a saved approved, independently verified
publication; exact repository/issue/PR/head/branches; GitHub-reported `merged_by`
of type `User`; and merge ancestry in the proposed current base-branch head.
Closed prerequisite issues are allowed only with unchanged published content.
The dependent issue must remain open. Missing, ambiguous, malformed or drifted
evidence refuses staging; an altered PR head, including a correction advance,
does not silently replace the original publication evidence.

When all prerequisites qualify, the response contains `state=staged` and a fresh
`handoff_preview`. Original plan, issue content, dependent preview, approvals and
ledgers remain unchanged. The handoff preserves objective, criteria and policy,
changes only task identity and pinned base, and persists its evidence before local
preview staging. Exact interrupted local staging can recover without remote writes
or model/effect replay; lost completed receipts/previews cannot be recreated.
Published handoffs can be prerequisites for later plan issues, with bounded receipt
lookup and acyclic transitive inspection. Competing publications fail closed.

Approve the fresh preview separately through `/internal/developer/preview/approve`
and use the existing configured request/direct/detached admission flow. Readiness
is rechecked before approval and preparation, including standalone preparation;
stale branch/head/issue evidence cannot start work. With this binding enabled, the
original dependency-bearing preview is not an alternate execution route. Admission
uses a fresh ledger and a private trusted seed that already contains the new base.
The seed must be outside both the service checkout and delivery state. Nested or
linked service checkouts are refused before target execution.
Owned/terminal runs retain frozen recovery rather than depending on a mutable live
branch. Verification, publication and review remain separate gates. No automatic
approval, merge, background tick driver or distributed locking is provided.

### Native PM Coordinator Binding

`NativeCoordinatorProposal` bridges a configured native PM to the service's trusted
read-only coordinator callback. Inside an operator-owned service factory, register
its scoped binding in `ManagedOrganizationService(..., coordinators=...)`:

```python
from aitobuild.organization_roles import NativeCoordinatorProposal

native_pm = NativeCoordinatorProposal(
	runtime_for=runtime_for,
	state_dir=context.state_dir / "native-pm-coordinator",
	previews=context.previews,
	budget_path_for=context.worker.budget_path,
	event="github.issue.ready",
	coordinator_id="planner",
	binding_revision="pm-bindings-v1",
)
coordinators = {"planner": native_pm.service_binding}
```

The pinned route must use coordinator delegation and name this PM as its team
coordinator. `.service_binding` checks the activated coordinator/event before
execution. Existing custom callbacks remain supported. Native PM bindings refuse
mock runtimes, snapshot drift and persistent tool profiles. The only per-run tool
inspects the approved task/eligibility; model output is strict `agent_id`/`rationale`
JSON. Actor identity comes from service context, never output or request fields.

An atomic/fsynced per-preview receipt pins the definition, route/team/workflow,
coordinator/operator binding, exact approved content/time, ledger path and original
deadline. It saves `running` before the native call and `proposed` before assignment.
Capacity contention can reuse a saved proposal after restart without another model
request, new deadline or reservation reset. Corrupt, changed or uncertain `running`
receipts fail closed; cancellation, expiry and proposal failures abort the original
ledger and cannot claim or replay. Retained SDK sessions and bounded tool diagnostics
remain evidence, not assignment authority or independent verification.

The service alone validates eligibility and creates the Developer assignment under
trusted PM identity; the callback creates no separate PM assignment or issue write.
Journal capacity applies to the assigned owner, not coordinator calls; detached
worker bounds still govern execution. Default startup remains unmanaged.

### Published Review Admission

`PublishedReviewAdmission` stages a saved published draft as a separate, unapproved
review task. Add an Architect-only eligible-agent route to the pinned native graph
definition; the example issue workflow does not define `published.review`.
Inside the operator-owned service factory:

```python
from aitobuild.organization_reviews import PublishedReviewAdmission, PublishedReviewRoute

github = context.tools.github_adapter
if github is None:
	raise ValueError("Published review requires a configured GitHub adapter")
reviews = PublishedReviewAdmission(
	definitions=definitions,
	previews=context.previews,
	worker=context.worker,
	github=github,
	state_dir=context.state_dir / "published-reviews",
	routes=(PublishedReviewRoute(
		repository="owner/repository",
		repository_id=123,
		organization_id=snapshot.organization_id,
		revision=snapshot.revision,
		event="published.review",
	),),
)
```

Pass `reviews=reviews` to `ManagedOrganizationService`. Register
`NativeManagedRoles.operations` and its cleanup binding, with
`review_target_for=lambda task: reviews.target_for(task.assignment.preview_id)`.
These bindings must share the service-owned definitions, previews and delivery
worker. Reviewer/team/delegation/workflow remain definition-controlled; there is
no fixed team or contribution loop. Advance operator binding revisions when behavior changes.

Successful `/internal/developer/delivery/publish` automatically stages review
metadata only when this binding is present. The response includes `review_preview`;
a staging failure preserves the published result and reports `review_staging_error`.
Authenticated `POST /internal/organization/reviews/offer` accepts only
`{"published_preview_id": "..."}` to retry metadata staging without publication
replay, task execution or approval. Live identity/open-draft/head checks still apply.

Atomic/fsynced receipts pin the published snapshot, exact head, scope, task identity
and original route/revision. Duplicates and partial staging recover the same preview,
including after activation revision changes. Missing/corrupt receipts never permit
a reserved review task to fall through to ordinary Developer routing.

Approve the new task through `/internal/developer/preview/approve`; only then does
service/worker admission initialize its separate deadline-pinned ledger and run the
Architect graph. It creates no Developer checkout, offers no commands and has zero
file-write capacity; the Developer ledger remains untouched. Missing initialized
ledgers, expiry, abort or changed deadlines cannot create a replacement budget.
Frozen-owner cancellation can still abort the original review ledger when mutable
preview metadata is damaged. The exact saved COMMENT body/target needs another
one-shot `/internal/organization/tasks/approve` decision; restart does not rerun the model.

Access counters do not certify semantic review. Scoped correction handoff is
described below; meetings do not replace a separately approved head-pinned review.

### Scoped Correction Handoff

Enable `allow_correction_proposals=True` on the explicitly registered
`NativeManagedRoles` binding and advance its operator binding revision. After
complete source/diff inspection, the per-run `architect_propose_correction` tool
can save one bounded objective and explicit published paths. It cannot assign,
approve, execute or widen task authority. The exact proposal is included in the
saved COMMENT approval; changed content or scope is refused on restart. Existing
comment-only reviews expose no new tool.

Add a Developer-only eligible-agent route to the pinned definition, then supply
`correction_routes=(PublishedReviewRoute(..., event="published.correction"),)` to
`PublishedReviewAdmission`. The route controls organization/revision, delegation,
owner and native graph; there is no hardcoded team, correction loop or workflow.
Correction routes are separate from ordinary issue and Architect review routes.

Authenticated `POST /internal/organization/corrections/offer` accepts only
`{"review_preview_id": "..."}`. It requires a completed, cleaned-up approved review,
its saved COMMENT receipt and unchanged live published target. The response contains
`correction_preview`, initially unapproved. Offer/retry is metadata-only: no model
replay, checkout, commands, approval, budget initialization or remote writes.

Atomic/fsynced receipts save staging intent before preview creation and preserve
the completed review, proposal, target, original route/revision and exact scope.
Partial staging/duplicates recover the same preview; missing receipts, changed
scope and stale heads cannot fall through to the ordinary Developer route.
The task copies the original publication's policy ceilings, narrows file scope to
literal proposed paths and caps unique file changes at their count. It uses the
reviewed head as its base and the published task branch as its base branch.

Approve it separately through `/internal/developer/preview/approve`. Existing
managed Developer graphs can then prepare a fresh private checkout/branch and
budget, never reuse the original Developer or review task's approval/ledger.
The configured local seed must already contain the exact reviewed commit and
published branch; preparation does not fetch, and missing heads fail closed.
Deleted initialized budgets cannot be recreated by replaying preparation.
Register the existing delivery operations for implementation and independent
verification; a completed metadata graph is neither an implemented correction
nor a verified/publishable artifact.

Same-PR correction publication (operator-only): each verified `published-correction-*`
delivery is its own record holding one round and never rewrites the original. Authenticated
`POST /internal/organization/corrections/stage-publication {correction_preview_id}` returns one
exact snapshot (unified diff, PR number, pinned branch/base, pinned parent head, chain and the
triggering Architect review receipt digest), its content digest and a one-use approval digest
bound to a fresh nonce, so re-staging after drift never revives an invalidated digest. `POST
/internal/organization/corrections/publish {correction_preview_id, approval_digest}` consumes that
one-use approval, recomputes the snapshot (any drift invalidates it) and fast-forwards the pinned
draft head with `force=false`; it never creates a PR or changes base, then stages the next
Architect review at the new head. The chain walk fails closed on a missing/invalid link or a
repo/PR/branch/head mismatch; only the first staged correction on a parent head may proceed (later
siblings fail as terminal stale) and older preview IDs are refused as "superseded by <tip>".
The adapter re-checks the original task budget before every blob, tree, commit, ref and PR write
(ordinary publish included), re-reads the PR target and branch head right before the ref
update, and re-reads the live ref after it, so a head moved by someone else is never reported
as our success. Reconciliation, ordinary publish recovery and advance retries share one read-only rule: a
remote head counts as ours only when its sole parent
is the pinned head, its message matches, and its tree equals the parent tree plus exactly the
pinned blob/mode changes, so a push that landed settles as `published` even after the budget is
gone (a truncated tree listing stays `publishing`). After any failure past consume, the live branch head decides the outcome: our exact commit
(tree, sole parent and message) on the open draft reconciles to `published`; the unchanged parent
fails terminally with `push_outcome: not_applied` and releases that parent head; a different head
fails terminally with `push_outcome: moved` and keeps holding it; an unreadable head stays
`publishing`, resumable only under the same approval and operator. A resume settles from GitHub
first, using only the approved snapshot's blob SHAs and modes and no budget or checkout check:
ours ends `published`, an untouched parent pushes again only while the budget is still valid
(otherwise it fails as `not_applied` and releases the head), a moved head fails and holds, and an
unreadable head stays `publishing`. Authenticated `POST
/internal/organization/corrections/retire {correction_preview_id}` (live adapter only) retires a
failed or abandoned unpublished correction only while the live PR head still equals its parent,
invalidating any pending approval; both sibling checks skip released and retired corrections.
Re-offering that completed review after release or retirement creates the next linked attempt
with a new task/preview and `replaces_correction`; an attempt still holding the head is reused.
The replacement stays unapproved and needs fresh task and exact publication approval. Old
receipts, approvals and aborted budgets remain unchanged; partial replacement staging recovers
the same preview, not another attempt.
An Architect COMMENT that GitHub binds to another commit is recorded, so a retry refuses instead
of posting a duplicate. Only correction records related to the checked PR or parent head can
block those checks; files that cannot be parsed at all still fail closed.
A push that landed before the PR closed or became ready stays `publishing` for operator
inspection; the service does not reopen the PR or change its state.
Managed `delivery_publish` and the legacy publish endpoint refuse correction tasks.

### Complete Supervised Correction Cycle

Register `NativeDeliveryImplementation` and `ManagedDeliveryBindings` against the
same worker and scoped tools. Configure the Developer correction graph to use
`delivery_implement`, then `delivery_verify`, through native graph edges, and finish
with verified evidence. Publication is a separate operator action using the exact
staged digest above; task/tool/COMMENT approval does not authorize it. There is no
`delivery_publish_correction` operation or correction-target callback on the binding.
No fixed team or universal contribution recipe is introduced; normal issue
publication uses `delivery_publish`, which refuses corrections.

Implementation retains exact saved tool approvals, scoped exact-span edits, cleanup
and the original correction ledger. Independent verification uses the preparation-time
operator command plan, a fresh constrained session, integer exit zero, successful
cleanup and the unchanged checkout digest. Recovered errors remain diagnostics.

Correction publication extends the reviewed head and updates the same pinned open
draft PR, using its original public branch/base and a non-force update. It never
publishes the private checkout branch or replaces the original delivery/review receipts.
Exact parent/tree/message reconciliation supports interrupted publication; unrelated
heads, target drift, changed artifacts and invalid budgets fail closed. Budget checks
precede every correction upload and ref update, including reconciliation object
writes. The head/target are rechecked immediately before the non-force PATCH, and
the live ref is checked again afterward before reporting our commit as published.
Failed/aborted runs are not replayable; only uncertain publication may reconcile
under its saved consuming approval and original operator/budget.

Set `automatic_review_followups=True` on the explicit managed service binding to stage
a correction after a completed approved COMMENT, or a fresh head-pinned review after
published delivery. A verified-only correction does not stage its next review until
the operator publishes it. Follow-ups are separate **unapproved** tasks, not model assignment,
approval or execution authority. Each task and COMMENT still needs fresh approval.
Before each correction's preparation, the operator must provision its reviewed head
and published branch in the local seed; the service does not fetch them.

Set `max_correction_rounds` on review admission (integer 0-32, default 1). The root
review's approved scope pins that ceiling, root identity and round; configuration
changes cannot reset an active cycle. At the ceiling, another correction proposal
records human escalation instead of creating a task. No proposal means no correction;
neither outcome certifies semantic review or authorizes merge.

Task responses/status and operator publication responses include saved `follow_up`
metadata when present. Authenticated
`POST /internal/organization/followups/offer` accepts only `{"preview_id": "..."}` and
retries staging after interruption/error without replaying models, publication,
approval or budgets. Staging failures preserve completed delivery and visible errors.

### API setup

Prerequisites: Python 3.14 or newer, `uv`, and ripgrep (`rg`, 14+). Both checked-in
container images install ripgrep. Run from the repository root.
The dev container also needs Python 3 on the host for certificate export.

```bash
uv sync
```

The `dev` dependency group is enabled by default, so tools like `pytest`, `ruff`, and `mypy` are installed automatically on sync.

Start a local API with model fallback and simulated command execution:

```bash
export AITOBUILD_WEBHOOK_SECRET="local-webhook-secret"
export AITOBUILD_INTERNAL_API_TOKEN="local-internal-token"
export AITOBUILD_REQUIRE_INTERNAL_AUTH="true"
export AITOBUILD_REQUIRE_APPROVAL_FOR_REPO_WRITES="true"
export AITOBUILD_REQUIRE_DEVELOPER_PREVIEW="true"
export AITOBUILD_ALLOW_MOCK_MODEL="true"
export AITOBUILD_DEVELOPER_EXECUTION_MODE="mock"
unset AITOBUILD_FOUNDRY_ENDPOINT AITOBUILD_FOUNDRY_API_KEY
uv run uvicorn aitobuild.server:app --host 127.0.0.1 --port 8000 --reload
```

In another terminal:

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail -H 'X-Internal-Token: local-internal-token' \
	http://127.0.0.1:8000/internal/runtime/developer-agent
```

Expect health status `ok`, runtime mode `mock`, and `ready_for_run=false`.
The readiness endpoint checks native handle availability, not provider
connectivity or successful tool execution. Interactive API schemas are at
`http://127.0.0.1:8000/docs`.

These credentials are local examples only. Do not expose this prototype to
untrusted callers. Mock mode simulates commands but does **not** disable file
writes; use dry runs or the copied-fixture simulation for execution experiments.

## Local simulation (no GitHub, no real repo)

Use the simulation kit in `sim/` to run the Developer workflow end-to-end in a
temporary copied fixture repository.

Run:

```bash
env -u AITOBUILD_FOUNDRY_ENDPOINT -u AITOBUILD_FOUNDRY_API_KEY \
	uv run python sim/run_local_simulation.py --output sim/simulation-report.json
```

Expected: initial route `developer.preview_required`, replay route
`developer.async.webhook`, execution `accepted=true`, command exit code `0`,
`generated_file_exists=true`, and `succeeded=true`. The harness exits nonzero
for failed execution, incomplete agent approval, or failed session cleanup.
The default command uses the pytest installed by `uv sync`; it does not bootstrap pip.

Optional:

```bash
uv run python sim/run_local_simulation.py --output sim/simulation-report.json
uv run python sim/run_local_simulation.py --env-file sim/.env.simulation.local
```

The simulation kit includes:

- `sim/.env.simulation` with required `AITOBUILD_*` keys for local runs.
- `sim/repo-fixture/` as the fake test repository.
- `sim/payloads/` sample request payloads.
- `sim/run_local_simulation.py` as the end-to-end harness.

For details, see [sim/README.md](sim/README.md).

## Useful commands

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv run uvicorn aitobuild.server:app --reload
```

## Foundation capabilities

- Unified trigger pipeline for webhook and internal events.
- FastAPI ingress with GitHub webhook signature verification.
- Trigger deduplication and deterministic dispatcher routing.
- Agent Framework runtime bootstrap with Foundry project, Azure OpenAI v1 and explicit mock fallback modes.
- Scheduler scaffolding for proactive architect scans and meeting bootstrap events.
- Shared escalation route and sink for consistent human/system escalation outputs.
- Developer isolation bundle contract for scoped software delivery tasks.

## Required environment variables

```bash
export AITOBUILD_WEBHOOK_SECRET="replace-me"
export AITOBUILD_INTERNAL_API_TOKEN="replace-me-internal-token"
```

Both are required with the default internal-auth setting. Disabling internal
auth removes the token requirement, but is only appropriate for isolated tests.
The server does not automatically load simulation env files; the harness does.

Optional runtime flags:

```bash
export AITOBUILD_FOUNDRY_ENDPOINT="https://example.foundry.azure.com"
# Foundry project endpoints use Entra credentials in this runtime path.
# Keep AITOBUILD_FOUNDRY_API_KEY unset and authenticate via Azure credentials.
export AITOBUILD_FOUNDRY_MODEL="gpt-4.1"
export AITOBUILD_ALLOW_MOCK_MODEL="true"
export AITOBUILD_SCHEDULER_ENABLED="true"
export AITOBUILD_SCHEDULER_KILL_SWITCH="false"
export AITOBUILD_ARCHITECT_SCAN_CRON="0 8 * * *"
export AITOBUILD_MEETING_TICK_CRON="*/30 * * * *"
export AITOBUILD_MAX_PROACTIVE_JOBS="2"
export AITOBUILD_SCHEDULER_QUIET_START_HOUR=""
export AITOBUILD_SCHEDULER_QUIET_END_HOUR=""
export AITOBUILD_REQUIRE_APPROVAL_FOR_REPO_WRITES="true"
export AITOBUILD_REQUIRE_INTERNAL_AUTH="true"
export AITOBUILD_INTERNAL_API_TOKEN="replace-me-internal-token"
export AITOBUILD_REQUIRE_DEVELOPER_PREVIEW="false"
export AITOBUILD_DEVELOPER_EXECUTION_MODE="mock"
export AITOBUILD_DEVELOPER_COMMAND_TIMEOUT_SECONDS="120"
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_IMAGE="aitobuild-developer:local"
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_WORKDIR="/workspace"
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_PREFIX="aitobuild-dev"
export AITOBUILD_DEVELOPER_SESSION_RUN_AS_CURRENT_USER="true"
# Optional host-visible path for Docker bind mount source in container_session mode.
# Leave unset to use the current process workspace path.
export AITOBUILD_DEVELOPER_SESSION_CONTAINER_BIND_PATH=""
# Max seconds to wait for each /internal/developer/agent/run invoke cycle.
export AITOBUILD_DEVELOPER_AGENT_INVOKE_TIMEOUT_SECONDS="180"
export AITOBUILD_DEVELOPER_ENABLE_MCP_ADAPTERS="false"
export AITOBUILD_DEVELOPER_ENABLE_AGENT_LIVE_LOGS="false"
export AITOBUILD_DEVELOPER_MCP_SHELL_TOOL_NAME="execute_command"
export AITOBUILD_DEVELOPER_MCP_FILESYSTEM_READ_TOOL_NAME="read_file"
export AITOBUILD_DEVELOPER_MCP_FILESYSTEM_WRITE_TOOL_NAME="write_file"
# GitHub adapters for Architect/PM tools (default mock).
export AITOBUILD_GITHUB_ADAPTER="mock"
export AITOBUILD_GITHUB_DEFAULT_REPOSITORY="uhvogala/aitobuild"
# Comma-separated owner/name allowlist required for gh_cli (also auto-includes default + developer repository sources).
export AITOBUILD_GITHUB_ALLOWED_REPOS="uhvogala/aitobuild"
export AITOBUILD_WEB_SEARCH_ADAPTER="mock"
```

For Azure OpenAI v1, set `AITOBUILD_FOUNDRY_ENDPOINT` to the resource URL ending
in `/openai/v1` and `AITOBUILD_FOUNDRY_MODEL` to the deployment name. This uses
the native chat-completions client with refreshable Entra auth, or an optional
API key from an ignored local environment file. Runtime mode is `openai`.

## API endpoints

- `GET /health`
- `POST /webhook`
- `POST /internal/triggers`
- `POST /internal/scheduler/tick`
- `POST /internal/developer/preview`
- `POST /internal/developer/preview/approve`
- `GET /internal/developer/previews`
- `GET /internal/pm/plans`
- `POST /internal/pm/plan/approve`
- `GET /internal/pm/issue-writes`
- `POST /internal/pm/issue-write/approve`
- `POST /internal/developer/delivery/prepare`
- `GET /internal/developer/delivery/{preview_id}`
- `POST /internal/developer/session/start`
- `POST /internal/developer/session/stop`
- `POST /internal/developer/session/stop-all`
- `POST /internal/developer/run`
- `GET /internal/runtime/developer-agent`
- `POST /internal/developer/agent/run`
- `POST /internal/developer/agent/resume`
- `GET /internal/escalations`

All `/internal/*` endpoints require `X-Internal-Token` by default. Webhooks
require `X-GitHub-Event`, `X-GitHub-Delivery`, and an HMAC-SHA256
`X-Hub-Signature-256` computed over the exact request body.

Example internal trigger payload:

```json
{
	"origin": "manual_request",
	"event_type": "architect_scan_requested",
	"payload": {"repo": "aitobuild"}
}
```

`architect.proactive.scan` responses include `metadata.architect_scan` with
structured findings and issue proposal suggestions.
`developer.async.webhook` responses include `metadata.developer_task_bundle`
for isolated software delivery execution.

These route responses do not launch a Developer worker or open a PR.
Architect scan findings currently derive from supplied counters/flags, not
an autonomous repository inspection.

When `AITOBUILD_REQUIRE_DEVELOPER_PREVIEW=true`, legacy fixture webhook dispatch
returns `developer.preview_required` until a matching preview is approved.
Repository issue tasks always require approval, regardless of that flag.

Preview flow:

1. Create a preview via `POST /internal/developer/preview` with
	`github_event`, optional `action`, optional `delivery_id`, and optional `body`.
2. Approve via `POST /internal/developer/preview/approve` with `preview_id`.
3. Inspect pending queue via `GET /internal/developer/previews` (defaults to `pending_only=true`).
4. Deliver the webhook with the preview's matching delivery ID or dedupe key;
	dispatcher then routes to `developer.async.webhook`. An already accepted
	delivery is deduplicated; the simulator uses a second delivery ID for its preview/replay pair.

### Durable repository issue preparation

Repository issue admission supports GitHub `issues` events with `opened`, `assigned`, or
`edited` actions. Assignment creates a reviewable preview, not authorization to
execute; `assigned` must include an assignee login. Other repository webhook
events, closed issues, and PRs are rejected. Payloads must provide repository
`id`, `full_name`, and `default_branch`, plus issue `id`, `number`, `state`,
`title`, and `body`. The title becomes the objective; the full issue body is
retained as target context. The body must contain a Markdown `Acceptance Criteria`
heading with bullet, checkbox, or numbered list items. Missing criteria are
rejected rather than replaced with a generic objective. Service source files
are not added as target context. The existing default isolation policy is part
of the scope shown for human review; arbitrary scope overrides are not supported
for extracted repository issue previews.

Webhooks do not supply a base commit SHA. Approve the preview through
`POST /internal/developer/preview/approve` with `preview_id` and `base_revision`,
a resolved 40-character target commit SHA supplied by the operator. The service
validates SHA syntax and pins it in the approved bundle. The checkout-preparation
worker additionally verifies that the commit belongs to the configured seed's
approved base branch before creating a private checkout.
The approved issue context, criteria, constraints, and policy cannot be replaced.
Changed issue scope with a new delivery creates a new pending task; changing
scope under an existing delivery or replacing an approved base is rejected.

Previews, approvals, stable scope-derived task IDs, delivery aliases, and dispatch
markers are saved under `AITOBUILD_DEVELOPER_STATE_DIR` (default
`.aitobuild/developer/previews.json`) using local file locks and atomic synced
saves. Identical issue snapshots share one preview across delivery IDs. A pending
delivery can be replayed after approval, including after restart; an already
dispatched task returns `dedupe` without creating another task. Inspect the queue
with `pending_only=false` to recover `approved` or `dispatched` records. These
states describe metadata routing, not worker execution or completion.

Repository issue bundles require the prepared-target native execution path below.
The legacy run endpoint and harness remain blocked for issue tasks. Preparation
does not commit, push or publish; publication uses the guarded delivery path and
requires explicit approval. Other trigger dedupe, legacy meetings and scheduler
state remain in memory; managed meeting receipts are local durable files, not
distributed storage.

### Prepare an approved target checkout

Configure trusted local Git seeds on the server before startup. Requests cannot
supply or replace source paths. The repository name and numeric GitHub ID must
match the approved issue context exactly; the default configuration enables no
sources. The service checkout, overlapping paths and linked service worktrees
are rejected as target sources.

```bash
export AITOBUILD_DEVELOPER_REPOSITORY_SOURCES='[{"repository":"fixture/widgets","repository_id":101,"path":"/absolute/path/to/target-seed","verification_commands":["PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider"]}]'
```

After creating and approving an issue preview with a resolved base SHA, prepare
it through the authenticated operator endpoint:

```bash
curl --fail -H "X-Internal-Token: $AITOBUILD_INTERNAL_API_TOKEN" \
	-H 'Content-Type: application/json' \
	-d '{"preview_id":"dp-..."}' \
	http://127.0.0.1:8000/internal/developer/delivery/prepare
```

Require `accepted=true` and `delivery.state=prepared`, not just HTTP 200. Inspect
saved state with `GET /internal/developer/delivery/{preview_id}`. Preparation
creates a task-local independent Git clone at the approved base and one
deterministic local task branch. It performs no remote fetch/push, submodule
update, target test/build command, delivery commit or PR operation. Git metadata
operations run on the host with a sanitized environment, hooks/fsmonitor disabled
and local-only transport, not through an agent sandbox; use trusted seeds.

Delivery records and `preparation.log` live in
`.aitobuild/developer/deliveries/<preview-id-hash>/` by default. The record exposes
the private `checkout_path`, base/head revisions, approved snapshot and errors.
Preparation shares the preview's persisted absolute deadline and file budget;
Git command timeouts terminate their process group and retain output. Repeated
prepare calls validate and return the same pristine checkout rather than cloning
again. Interrupted preparation, changed checkout state, verification errors or
expiry become terminal failures with artifacts retained and the budget aborted.
Failed records are not silently retried; automatic recovery is not implemented.

The API fixture checks exercise approval-to-checkout and restart recovery:

```bash
uv run pytest tests/test_app.py -k delivery_api -v
```

### Implement an approved issue

Use `AITOBUILD_DEVELOPER_EXECUTION_MODE=container_session` and the prepared
Developer image for actual target commands; `mock` remains a test-only backend.
In a dev container, keep Developer state under the mounted service workspace so
Docker can resolve the host path. Start a fresh native session with the approved,
prepared preview:

```bash
curl --fail -H "X-Internal-Token: $AITOBUILD_INTERNAL_API_TOKEN" \
	-H 'Content-Type: application/json' \
	-d '{"preview_id":"dp-...","session_id":"issue-demo","input":"Implement the approved issue and run its tests","auto_approve_tools":false}' \
	http://127.0.0.1:8000/internal/developer/agent/run
```

File tools edit the prepared checkout directly; commands use that same checkout
read-only in the offline constrained container. No service-seeded copy is made.
One native session owns the delivery. A local task lock prevents concurrent
invocations, including across application instances. Saved approvals resume via
`/internal/developer/agent/resume` with `session_id`, `request_id` and `approved`.
Restart restores the immutable task, target checkout, reservations and original
deadline; approval waits do not reset that deadline.

Delivery states are `implementing`, `awaiting_tool_approval`, `implemented` and
`failed`, in addition to preparation states. `implemented` only means the native
turn finished: it is not independent verification or publication approval.
After implementation begins, prepare returns the existing lifecycle record
without reseeding; use the status endpoint to inspect progress. Rejection,
failure, interruption or expiry blocks replay, retains artifacts and aborts the
budget. Terminal outcomes close the task container. Browser/arbitrary MCP and
legacy issue execution remain prohibited.

### Independently verify the target

Configure `verification_commands` before preparing the task. Preparation pins
the ordered plan; commands must fit the approved policy. An empty plan permits
preparation/implementation but cannot be verified. Requests cannot supply or
replace commands, source paths or policies; changing the pinned plan requires a
new approved task, not resetting the existing task's budget.

After the native delivery reaches `implemented`, trigger the server-owned gate:

```bash
curl --fail -H "X-Internal-Token: $AITOBUILD_INTERNAL_API_TOKEN" \
	-H 'Content-Type: application/json' \
	-d '{"preview_id":"dp-..."}' \
	http://127.0.0.1:8000/internal/developer/delivery/verify
```

Require `accepted=true` and `delivery.state=verified`, not HTTP 200 or model text.
The verifier runs every pinned command in a fresh private Docker session, using
the same read-only target, offline/resource-limited profile and original task
deadline. Only confirmed process exits with integer code 0 and successful
container cleanup qualify. Mock execution cannot certify a task.

The durable record carries command results, bounded output previews/log paths,
the verifier session, timestamps and a whole-checkout fingerprint. Bounded logs
live beside the delivery state. Changed files must be allowed and already
reserved against the task budget. The fingerprint must remain unchanged during
verification. Duplicate successful requests check integrity without rerunning
commands, including after restart; changed verified content becomes a failure.
Status stays readable during `verifying`, while the task lock blocks duplicate
execution. Failed commands, missing exits, expiry, interruption or cleanup errors
preserve artifacts, abort the original budget and block automatic replay.

`verified` certifies this snapshot against the operator's command plan, not
semantic completeness, review approval or permission to publish. There is no self-merge.

### Verified Draft Publication

`POST /internal/developer/delivery/publish` accepts only `preview_id` and requires
internal authentication, the immutable human-approved issue and a live allowlisted
`gh_cli` adapter. Configure `AITOBUILD_GITHUB_ADAPTER=gh_cli` and
`AITOBUILD_GITHUB_ALLOWED_REPOS` with the target repository. Install and
authenticate `gh` against the intended GitHub host before using it; the CLI is
not included in the service container. Real publication requires explicit scope
approval; task completion alone does not authorize it.

Publication captures immutable upload bytes/modes during the checkout digest
walk and compares the digest to independent verification before remote writes.
It records a deterministic task branch, tree fingerprint, head and draft PR receipt.
Require `accepted=true` and `delivery.state=published`. The returned PR must be
open, draft and at the expected head/target within the original deadline.

Interrupted `publishing` can reconcile an exact remote commit (tree, approved
base parent and message) and the existing task-head/base draft PR when a local
receipt is missing. It does not overwrite mismatched refs or update closed/ready
PRs. Failed/expired/aborted tasks remain terminal, retain available receipts and
cannot be replayed by resetting their budgets. Publication and head-pinned
Architect COMMENT review are prototypes awaiting fresh live acceptance, not
an automatic issue-to-PR workflow.

Developer prototype run flow (non-issue previews only):

1. Create and approve a preview (`/internal/developer/preview` and `/internal/developer/preview/approve`).
2. Execute a constrained Developer run via `POST /internal/developer/run` with:
	- `preview_id` (required)
	- `commands` (optional list)
	- `file_writes` (optional list of `{path, content}`)
	- `dry_run` (optional, default `true`)
	- `approved` (optional, used for non-dry-run repo writes)
3. Inspect `command_outcomes` and `file_write_outcomes` in the response.

`/internal/developer/run` enforces the bundle's isolation policy for command prefixes and file paths.

The default command policy covers normal development workflows: Python/uv,
Node package managers, Git, common build/compiler tools, shell scripts,
filesystem inspection/manipulation, downloads and archives. Task-specific
`allowed_command_prefixes` can still narrow this list. Matching uses complete
command tokens and accepts leading environment assignments; it does not parse
heredoc bodies or authorize every command inside a script. Permitting a tool
does not install it. Interpreters and shells can execute arbitrary code, so this
is an ergonomics policy, not a sandbox. Preview approval and existing file-write
checks remain unchanged; unscoped subprocess runs still use the API environment.

The seed repository is the API process working directory, captured at startup.
Session-bound runs copy it once into `.aitobuild/workspaces/<session-id>`; different
Developers have separate checkouts and private home-volume subdirectories.
Unscoped subprocess execution still uses the API working directory. Preview approval
and `approved=true` for live file writes are separate checks. Commands run before
the requested file writes, so a passing command does not validate those later
writes; run verification again after applying changes.

Execution backends:

- `mock`: deterministic simulated commands; file reads/writes still use the real workspace
- `subprocess`: runs commands in the same runtime environment as the API process
- `container_session`: runs commands in a persistent Docker container per session

If you run aitobuild inside a container (for example this dev container), subprocess mode executes inside that container.

Container-session flow:

1. Start session with `POST /internal/developer/session/start` (optional `session_id`).
2. Execute multiple `POST /internal/developer/run` calls using the returned `session_id`.
3. Stop and clean up using `POST /internal/developer/session/stop`.

This keeps one container alive for the whole Developer task session rather than launching one per command.

Build [.devcontainer/Dockerfile.developer](.devcontainer/Dockerfile.developer) as
`aitobuild-developer:local` for the prepared test/browser/terminal toolchain.
Docker sessions bind-mount a private checkout writable and are not a
security boundary for untrusted code.

MCP adapter mode:

- Set `AITOBUILD_DEVELOPER_ENABLE_MCP_ADAPTERS=true` to route Developer command/filesystem tools through MCP stdio clients inside the session container.
- This is fail-fast by design: if MCP dependencies, tool bindings, or session requirements are missing, startup or tool invocation errors immediately.
- Session images must include Node.js/`npx`; the adapters launch shell/filesystem
	servers through `docker exec ... npx -y`. Server packages, schemas, paths, and
	tool names still need integration validation; MCP is optional, not required
	for native delivery, and is unavailable for preview-bound native tasks.

Agent Framework tool wiring:

- Developer tools are bound per native `Agent.run(..., tools=[...])` to prevent cross-session access and duplicate registration.
- Tool functions are compatible with the `@tool(...)` pattern from the Agent Framework sample `02_add_tools.py` when `agent_framework.tool` is available.
- Targeted edits use `developer_edit_file(path, old_text, new_text)`. The non-empty old text must match exactly once; insertion repeats a unique existing anchor in the replacement. Stale or ambiguous matches leave the file unchanged. No diff syntax, line-prefix repair or newline normalization is involved.
- The legacy `developer_apply_patch` parser remains opt-in through `DeveloperToolContext.use_legacy_patch_tool` for compatibility; it is not advertised to models by default. Use `developer_write_file` for new files or an explicitly requested full-file replacement.
- `developer_find_files` discovers paths with glob filters; `developer_search_files` searches literal text or Rust regex and returns paths, line numbers and byte columns. Both use ripgrep, respect allowed roots and ignore files, and support bounded pages with `next_offset`. Hidden/ignored files are opt-in; content searches skip files over 1 MiB and return bounded line previews. Read selected files before exact-text edits.
- `DeveloperIsolationPolicy.allowed_paths` uses literal equality/prefixes, not globs. File-discovery patterns are separate from this policy; keep reads/discovery within scope and restrict writes to authorized paths.
- Shell `rg` is available in the prepared image and allowed by preview command policy. Rebuild images after toolchain changes; new Developer sessions use the rebuilt image. Do not reset an existing task's ledger to adopt image changes.

Developer Agent runtime flow:

1. Check runtime readiness with `GET /internal/runtime/developer-agent`.
2. Execute one turn with `POST /internal/developer/agent/run` and payload:
	- `input` (required string)
	- `session_id` (optional string)
	- `preview_id` (optional approved preview, bound only to a fresh session)
	- `create_session` (optional bool)
	- `auto_approve_tools` (optional bool, default `false`)
	- `max_approval_rounds` (optional int, default `3`)
3. Inspect `pending_approval_requests`, `completed`, and
	`approval_round_limit_reached`. The endpoint can replay approvals within one
	call when `auto_approve_tools=true`. For human review, leave it false and
	submit a decision to `POST /internal/developer/agent/resume`:

```json
{"session_id": "<returned-session-id>", "request_id": "<pending-request-id>", "approved": true}
```

Use `approved=false` to reject. Both endpoints require `X-Internal-Token` when
internal authentication is enabled. The server restores the saved native request;
the caller cannot replace its arguments or supply a new task input. New prompts
are blocked while approval is pending. Unknown, cross-session or consumed
request IDs are rejected. Approval resumes the same native session without
replaying the original task, including after a server restart.

To carry an approved task into native execution, start a fresh session with
`preview_id` from `/internal/developer/preview/approve`. The server snapshots
that preview's task bundle, adds its objective/acceptance criteria/constraints
once to the initial task context, and restores its policy on later runs/resumes.
It does not accept caller-supplied policies or allow switching previews on the
same session. Responses include `task_id` and `preview_id`. Repository file and
search tools use the task's path scope; excluded repository tool categories are
not exposed. Direct commands and managed-shell starts use the task's prefixes,
which are also shown in tool descriptions. Without `preview_id`, the existing
standalone prototype remains available and is not an approved task workflow.

For a repository-specific task, `/internal/developer/preview` accepts an optional
`task_bundle` from `build_developer_task_bundle(...).to_payload()`, alongside the
usual event fields. It must still be explicitly approved before a native run.
Run/resume requests cannot replace that bundle or its policy.

`/internal/developer/agent/run` uses the runtime-bound native Developer Agent when available; in descriptor/mock mode it fails with `409` by design.

To bootstrap a meeting from scheduler ticks, first submit a `meeting_requested` internal trigger
and pass the returned `meeting_id` in `POST /internal/scheduler/tick` with `manual_meeting=true`.
Both internal endpoints require `X-Internal-Token` when internal auth is enabled.
Successful `meeting.bootstrap` dispatches include `metadata.meeting_kickoff` with
`workflow_built` (native GroupChatBuilder path) or `mock_started` (fallback path).

`workflow_built` means constructed, not executed or resolved. The scheduler
only advances when `/internal/scheduler/tick` is called; no background scheduler
loop runs just because `AITOBUILD_SCHEDULER_ENABLED=true`.

If a meeting deadline has passed when a `meeting_due` event arrives, the dispatcher emits
`escalation.route` and sends a structured escalation event to shared sinks.

## Developer Isolation Contract

Use `aitobuild.developer_isolation.build_developer_task_bundle(...)` to create a constrained
Developer task package with:

- objective and acceptance criteria
- workspace-relative context files only
- allowed tools and path/command limits
- declared runtime and file-change budgets

Default policy allows edits under `src/` and `tests/` and blocks paths like `.git/` and `.venv/`.

The accepted M1 profile is preview-bound native execution in a private Docker
session. It persists a ledger per preview: unique repository paths are reserved
before writes, repeated edits share a slot, and failed reservations are not
refunded. One wall-clock deadline starts at binding and includes approval waits
and restarts. Missing/corrupt ledgers and aborted/expired tasks fail closed;
another session cannot reset the same preview's budget. Legacy run endpoints
cannot execute an already native-bound preview.

Commands and interactive shell input operate with a read-only repository/root
filesystem, no network, dropped capabilities, no privilege escalation, and
bounded CPU/memory/processes. Writes go through approved scoped file tools.
The container's PID 1 exits at the task deadline even without the API; completion,
failure, timeout and rejected approval clean up the task container. Private
checkouts/home data remain available. Dependencies must already be in the image;
test/build outputs must use private scratch storage, not repository writes.
Browser and arbitrary MCP adapters are rejected for this profile. Native memory
uses the SDK's separate traversal/symlink-checked store and is deadline-guarded,
not charged against repository file counts.

Standalone native runs and the unbound preview execution prototype do not have
this contract. Prefix checks are an ergonomics filter, not shell parsing; shell
commands can read the copied repository and modify private scratch storage.
This Docker profile is not a hardened hostile-tenant sandbox. Distributed
coordination, auditing, scratch/disk quotas and broader network/browser policy
remain required before unattended execution.

## State and recovery

Non-repository trigger deduplication, legacy meetings, escalation events and active
session mappings are in memory. Repository previews, managed runs/decisions and
managed blocker meetings persist in local stores. Restart loses the in-memory
state. Docker containers may outlive the API process; use
`/internal/developer/session/stop-all` to clean up containers discovered under
the configured name prefix. Native AgentSession, history, memory and pending
approvals persist in the configured local state directory; these are trusted
plaintext files, not a distributed worker store. Decisions are consumed before
continuation executes: a failed or interrupted continuation cannot be blindly
replayed. Inspect its private checkout/history before deciding how to recover.
The active-session guard is process-local; cross-worker coordination and durable
task/approval auditing remain pending.
An approved bundle snapshot persists with a bound native session, but the
preview registry itself is still in memory. Scope changes require a fresh,
newly approved task session; there is no automatic task rebinding.

## Repository hygiene

Python bytecode and tool caches are intentionally ignored in git (`__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`).
If you run tests or lint locally, these files may appear on disk, but they should not appear in `git status`.
