# System Design Document: Agentic Product Team Framework

This document describes the target architecture, not a fully implemented system.
The implementation snapshot in section 7 distinguishes available capabilities
from remaining work. Operational milestones are tracked in [MILESTONES.md](MILESTONES.md).

## Implementation Notes

This section records completed work and acceptance checkpoints, including failures
and supervised recovery. Earlier counts, pending work and commit status describe
their checkpoint, not the current system. Current setup and usage belong in
[README.md](README.md); contributor rules belong in [agents.md](agents.md).

Budget-expired PR adoption (#66, 2026-10-18): pilot trial 4 kill point 2 opened
example PR #86 and was killed before the record saved it; the child's 30-minute
budget expired before the restart, and `BudgetDeadlineExpired` (raised by the
assignment revalidation in the runner's restart path) refused the run before
`publish()` reached adoption, leaving the record at `publishing`. By Vertti's
decision this is now the single runtime-budget exemption on ordinary deliveries,
and it covers only adopting that already-open PR. On a `running` receipt whose
revalidation fails with exactly `BudgetDeadlineExpired`, the runner proves the
interrupted continuation as before (checkpoint, last decision, data digest,
`interrupted_publication`) under a context that checks every pin except a live
deadline (same unaborted ledger, expired at exactly that deadline), then calls the
operation's `adopt_after_budget_expiry` hook; only `delivery_publish` declares one.
`DeveloperDeliveryWorker.adopt_after_budget_expiry` re-checks the approved task,
the exact approval snapshot field by field, the recomputed title/body/commit
message, this task's marker, the saved verified head, the live branch head, and a
read-only `find_adoptable_draft_pull_request` lookup that applies the #51 rule
(one open PR, exact title/body, draft, our author); the PR head must also equal the
saved head. It never calls a model, runs the Developer, pushes, or creates or
updates a PR. Success records `publication.adopted_after_budget_expiry {deadline,
adopted_at}` (validated on load; absent on older records), the run receipt
`adopted_after_budget_expiry {request_id, deadline, adopted_at}` (immutable), and
the assignment completes only against that exact expired deadline. Without a
matching PR, or on any mismatch, the run fails as `TimeoutError` (with the refusal
reason appended once the hook ran) and the record stays `publishing`; every path
that needs the model, a push or a new PR stays fail-closed on an expired budget.
This changes the delivery state model (`publishing` -> `published` without a live
budget) and needs Vertti's explicit okay before merge.

Interrupted draft publication resume (2026-10-13): pilot trial 4 killed the service
right after the developer branch push; GitHub had opened the draft PR but no record
held its number, and restart failed closed on the run-level replay block. A run left
`running` now resumes only when its last decision is the recorded affirmative service
approval for an operation that declares an interrupted-continuation check, the pinned
checkpoint still holds exactly that request with the same data digest, and the check
proves the effect is in flight. For `delivery_publish` that means the delivery record
is `publishing` and matches the run on preview id, approval time, pinned base, bundle,
native session and the approved publication snapshot (head SHA well-formed once
recorded). The runner then replays only that recorded response into the worker's
existing `publish` resume path: no new decision, admission or approval is recorded,
and every other interrupted state keeps "automatic replay is blocked". Corrections are
excluded (`is_correction_task`, #54). Before any PR create or update on that resume, the
live task branch must still equal the saved verified head, under the original approval
and budget checks; a moved branch refuses with no PR write, keeps the saved evidence and
leaves the branch untouched. This is same-run recovery only and does not touch #41
cross-run resume.

Draft PR bodies now carry `<!-- aitobuild-delivery:<sha256(preview id, bundle digest)> -->`.
An existing PR on the task branch is adopted only when the expected body carries that
marker, title and body match the exact approved receipt, it is an open draft, and its
author is the adapter's own writer login; anything else is refused, never adopted or
duplicated. The same author check (#51) now guards marker adoption of planned issues
after an uncertain create and in `reconcile`, which also records `adopted` for effects
it found by marker without a saved receipt. PRs opened before this change carry no
marker and are not adopted automatically.

Worker abort lock errors (#22): removed the broad `TimeoutError` suppression that
also swallowed `filelock.Timeout`. Abort reopens only the original ledger with
`create=False, allow_aborted=True`; repeat aborts preserve deadlines and reservations
without recreating budgets. Regression tests cover lock failure during both reopen
and abort, repeat abort, and the exact `BudgetDeadlineExpired` exception from an
expired `reserve_paths` call. Lock failures are no longer reported as successful
abort; existing worker error/deferral handling remains responsible for them.

Automatic policy visibility (#55): startup and authenticated service/worker
diagnostics share one policy-model-derived warning listing every automatic field
and the pinned revision. Default and all-human policies remain silent. Tests cover
every registered surface, future model fields, multiple automatic stages, internal
authentication and both worker configurations. This adds visibility only; exact
approval gates, trusted operation bindings and original budgets are unchanged.

Independent PM implementation handoff (2026-10-11): extended the opt-in detached
follow-up queue with an explicit service-owned `planning_publication` binding.
Read-only lookup joins the exact completed planning output to the immutable plan,
approved source payload/time, consumed issue/link decisions, complete publication
receipts and exact staged child payloads. Only independently ready planned issues
qualify; dependent originals cannot bypass the merged-prerequisite adapter, even
if approved. New implementation admission rechecks the current issue identity/body
and retains its separate task decision, pinned base, original child policy and
ledger. Missing or inconsistent receipts stop instead of being recreated. The
existing plan issue ceiling bounds fan-out; teams, delegation, routes and graph
operations remain operator-configured. Dependency/publication bindings cannot
silently use different planning owners.

Focused fixtures cover human waiting and automatic independent-child admission,
multiple independent children, restart before child admission, and safe deferral
on receipt-lock contention. Refusals cover missing/incomplete publications, missing
link approval or previews, changed source/child scope, changed run/output/binding,
issue drift/closure, unbound admission, disabled tasks and dependent originals.
The native PM transport is mocked and Developer queueing uses a scoped metadata
probe; this is not a full live three-role pilot or qualified delivery certificate.
Existing native delivery/review/correction fixtures remain separate coverage.
No dependency readiness scheduling, merge, seed fetching or default activation
was added; broader live/hosted acceptance remains open.

Final local validation: `uv run pytest --tb=short` passed 1,125 tests with two
optional skips and three existing SDK warnings. `uv run ruff check .`,
`uv run mypy src` (52 source files), editor diagnostics and `git diff --check`
passed. Mock-only `sim/run_local_simulation.py` reported `succeeded=true`, a
generated artifact, integer command exit 0 and no active session; its report is
outside Git and its sandbox is ignored. Documentation links/fences passed. The
user authorized committing/pushing the combined follow-up queue and independent
PM handoff after these checks; no target repository/model trials, merge or service
activation were performed.

Receipt-backed detached follow-up queueing (2026-10-11): the central configuration slice was
committed and pushed as `04489aa`. The next bounded slice adds an opt-in
`settings.managed_worker.queue_follow_ups` setting and exact factory binding.
Completed, cleaned-up worker parents can admit only their saved review/correction
children, with the full source run, approved scope and receipt relationship
validated. Children still require separate configured task admission and original
head/base, role, budget, verification and publication gates. Pending human approval
leaves them unqueued; disabled or invalid admission stops the slot without
rewriting the completed parent. Restart can recover missing child admission from
the durable parent/child receipts, but never replaces terminal children or replays
parent effects. Existing correction round ceilings bound the chain.

The native SDK fixture now exercises automatic head-pinned COMMENT, same-PR
verified correction and re-review, including restart before child admission and
metadata-only restart after all three tasks. The chain exposed an admission digest
bug: reconstructing a task bundle dropped staged payload fields. Admission now
binds the exact staged payload fields with only the operator-approved base pin,
matching the registry's immutable approval payload. Tests refuse changed source
runs/scopes and substituted children. These are mocked-model/GitHub disposable
fixture checks, not live automatic-mode acceptance. PM dependency scheduling,
blocker detection, seed fetching, merge and hosted/distributed execution remain
separate work.

Final local gates: `uv run pytest --tb=short` passed 1,103 tests with two optional
skips and three existing SDK warnings; `uv run ruff check .`, `uv run mypy src`
(52 source files), editor diagnostics and `git diff --check` passed. Mock-only
`sim/run_local_simulation.py` reported `succeeded=true`, a generated artifact,
integer command exit 0 and no active session. Its report remains outside Git and
the copied sandbox is ignored. Documentation links/fences passed. This follow-up
slice was later committed (`8929623`); no live repository effects, service activation or
merge were performed at that checkpoint.

Central configuration and exact operator-policy decisions (2026-10-09): added the
strict versioned application manifest and safe example, environment-backed secrets,
environment overrides and optional organization loading without activation. Shared
stage policy supports human/automatic/disabled modes with manual defaults and
disabled merge. Explicit factories must bind the effective policy exactly; missing
or unsupported adapters cannot silently activate legacy behavior.

Managed runs pin the immutable policy revision and persist exact decisions before
native continuation. Automatic decision ceilings survive human waits and restart;
disabled stages, drift, unbound free-text input and uncertain effects fail closed.
Native tool evidence includes the SDK request arguments. Automatic admission saves
the exact task/repository/operator-pinned base and policy decision atomically,
without replacing an existing base or resetting a ledger. Draft publication now
has a separate exact verified-byte/target/metadata approval boundary and rechecks
both its snapshot and managed context before remote writes. Same-PR correction
reuses the existing nonce-bound approval store, persists policy before the push,
and retains lineage/head/budget/reconciliation safeguards.

Native SDK fixtures exercise PM issue creation and actual-ID dependency links,
Developer tool decisions, Architect COMMENT publication, bounded meetings and
verified correction publication through request and detached execution. Tests
also cover altered approval evidence, factory mismatch, disabled operations,
verified-byte mutation, policy drift and restart without replay. These are local
mocked-model/GitHub integrations, not new live automatic-mode certification. Merge,
automatic dependency/blocker scheduling and hosted/distributed unattended lifecycle
remain separate work. Earlier supervised live receipts and approvals were not replayed.

Final local verification: `uv run pytest` passed 1,094 tests with two optional
skips; `uv run ruff check .`, `uv run mypy src`, editor diagnostics and whitespace
checks passed. Mock-only `sim/run_local_simulation.py` reported `succeeded=true`,
a generated fixture artifact, integer command exit 0 and no active session.
Documentation links/fences passed. No new live model/GitHub effects, service
activation, merge, commit or push were performed for this configuration slice.

Whole-repository Architect context and dependency live acceptance (2026-10-09):
the operator independently merged prerequisite PR 36. Native GET-only waiting and
exact human-merge/ancestry checks were accepted at new base
`4b0900bb2a1fc496101ab79c55b8d75373229936`. Dependency bindings now accept up to
eight explicitly configured historical receipt readers; new execution remains on
the active worker, without reopening old ledgers. Direct/detached/archived fixture
coverage preserves approvals/budgets and refuses ambiguous receipt ownership.

The first live preparation correctly refused a seed inside the service checkout;
its failed worker record and aborted original ledger remain intact. A new explicit
task/revision with an external pinned seed used native `DeepSeek-V4.1-Flash`, nine
saved tool decisions, README-only exact edits, independent constrained Docker
pytest (two tests, integer exit 0, cleanup) and exact inspected publication intent
to produce draft PR 37 at `8ba834fecdf9ea40e94707edca3f82eed3e587c4`. A separate
zero-write Architect task posted exact COMMENT 5470318145 after complete README
source/diff inspection and restart. Its changed-files-only context restriction was
a genuine review capability gap identified by the user, not an agent choice.

Native Architect tools now discover/search/read the whole immutable repository at
the review head, independent of Developer edit paths, with bounded pages and no
host checkout access. Context evidence remains separate from mandatory complete
changed-file source/diff receipts. Native SDK regressions cover unchanged files
outside Developer policy, zero-write access, discovery/search pagination,
context-only refusal, head drift and corrupt blobs. Read-only Git tree metadata
does not widen publication-write pin types or allow symlink/submodule traversal.

The legacy workspace Architect default also contained a fixed path whitelist.
Its read-only policy now covers the repository through the existing literal
empty-prefix contract; search normalizes that root to `.` rather than passing an
empty path to ripgrep. Regression checks read/discover/search docs, config and
hidden CI files while preserving runtime exclusions, workspace escape checks,
explicit operator policies and Developer write scope. Focused adjacent gates:
134 passed and Ruff clean.

A fresh separately approved review under explicit binding `whole-repository-review-v1`
read the implementation, both test files, package initializer and project config
at the exact published head, as well as complete README source/diff. The agent
recovered two requests above the existing 1,000-byte published-reader limit; these
diagnostics remain saved. The supervisor initially misapplied a zero-tool-error
criterion to the qualified review; additive metadata-only continuation preserved
that report and the original idle checkpoint/ledger, without model/effect replay.
Exact COMMENT 5470648081 was approved after restart and added once; the earlier
COMMENT stayed unchanged. Independent read-only audits accepted actual remote
base/parent/bytes/modes, real context blob identities, exact decisions, original
separate deadlines/reservations, terminal idempotency, unchanged old evidence,
draft/default branch and cleanup with no retained containers.

Final full gates: 1,005 passed, two optional Docker skips, three existing SDK warnings;
Ruff clean and mypy clean across 51 source files. Generated evidence stays outside
Git. PR 37 remains draft/unmerged. This is recovered supervised local live
acceptance, not uninterrupted first-attempt success, zero-error tool usability,
automatic dependency scheduling, hosted/distributed deployment, a full measured
product-team pilot or generic semantic certification. No dependencies, default
activation or service push were added.

Dependency handoff implementation (2026-10-09): opt-in `ManagedDependencies`
reconciles exact saved PM issue/link receipts and qualified prerequisite delivery
publications through read-only GitHub merge/ancestry evidence. It stages a separate
unapproved task at a current operator-pinned base; original previews, approvals and
ledgers are untouched. Bounded handoff lineage supports later dependent issues.
Service and authenticated staging/approval/preparation controls reject bypass,
stale evidence, ambiguous publications, missing/tampered receipts, operator/route
drift and symlinks. Exact interrupted local staging recovers metadata only;
owned terminal runs recover frozen state without effect replay. Focused coverage
includes native mocked PM publication, strict CLI read fixtures, authenticated
ASGI controls, chained/all-parent readiness and direct/detached admission from a
real copied Git seed with production saved verification/publication records.
Verifier and merge responses remain fixtures: no new live merge, dependent delivery
trial, automatic scheduler or hosted/distributed acceptance is claimed. Live
positive handoff acceptance requires an independently human-merged prerequisite;
existing trial drafts are not merged by this feature. Corrected/otherwise advanced
PR heads stop rather than guessing a replacement publication lineage.
Full local gates: 977 passed, two optional Docker skips, three existing SDK
warnings; Ruff clean and mypy clean across 51 source files. Editor diagnostics and
diff checks pass. The mock-only copied-fixture simulation returned `succeeded=true`
and integer command exit 0 with no persistent session allocated. Generated reports
remain outside Git. No dependencies, default activation, commits, pushes or remote
merges were added by this slice.

Managed meeting live acceptance (2026-10-09): `DeepSeek-V4.1-Flash` executed a
fresh supervised PM -> bounded three-role meeting -> Developer -> Architect
sequence through authenticated local ASGI and detached workers, real GitHub and
constrained Docker. Exact PM creation/resolved-link gates published issues 34/35;
only the original implementation child was approved. Three configured discussion
rounds and a separate resolver produced an unchanged-scope plan. Restart consumed
its exact digest-pinned approval without discussion replay, followed by seven
separate native tool approvals, correct two-file artifacts, independent pytest
exit 0/cleanup and exact verified-snapshot draft publication:
[PR 36](https://github.com/uhvogala/aitobuild_example/pull/36), head
`b6a3167a41b41e228265238ae3e71cb8de441b2c`.

The first readiness call timed out; a separately retained tool-free probe passed
before admission. The first Architect task failed closed on a body exceeding the
1,972-character publication limit, with cleanup and its original ledger aborted.
Native review guidance now derives and states that limit; exact-limit/overflow
regressions pass. A fresh separately approved zero-write review task, new binding
and original ledger inspected complete immutable source AND base diffs, found no
defect and reached its own exact COMMENT gate. Restart published
[the approved COMMENT](https://github.com/uhvogala/aitobuild_example/pull/36#pullrequestreview-5469346772)
without model replay; no correction was invented.

Two fresh live stop cases also passed: exact continuation rejection and a
scope-changing resolver proposal. Both retained terminal receipts, original
aborted deadlines, zero reservations and successful cleanup; admission-time
private checkouts stayed untouched with no implementation, verification or
publication. The rejection harness initially assumed approval created no checkout;
an additive metadata-only audit corrected that expectation without repeating a
meeting or consumed decision. All original failures remain preserved.

The additive `sim/.run-artifacts/example-e2e-20261009-meetings-deepseek-v1/acceptance-report.json`
records all three accepted cases and evidence hashes. Accepted native diagnostics
are empty. Exact draft bytes/modes/head/base, original budgets, idle and terminal
restart, duplicate-effect prevention, unapproved/unexecuted dependent issue 35,
unchanged previous draft/master/evidence and absence of trial containers passed.
Full gates: 940 passed, two optional Docker skips, three existing SDK warnings;
Ruff, mypy over 50 source files and touched editor diagnostics clean. This is
recovered supervised local acceptance, not an uninterrupted run, general tool
certification, automatic blocker detection/repeated meetings or hosted/distributed
M3/M4/M5 completion. Service changes remain uncommitted; no service push or merge,
dependency changes, default-model change or default activation.

Managed blocker-resolution slice (2026-10-09): opt-in configured task graphs now
execute native read-only group chats with bounded rounds, transcript bytes and the
original task deadline. Durable receipts pin blocker evidence, configured team,
scope/revision/bindings, transcript and strict continuation/scope-change/escalation
outcomes. Exact saved service approval consumes an input-data digest before
continuation; native Developer sessions pin/revalidate approved guidance without
changing their frozen task. Rejection/escalation/cancellation/timeout/interrupted
work fail closed without replay or ledger recreation. Mocked native DeepSeek
transport, direct/service/detached paths and an independently executed fixture
assertion cover meeting-to-draft continuation and post-approval plan tampering.
Deployment `DeepSeek-V4.1-Flash` is selectable through existing runtime/model-profile
configuration; the application default is unchanged. Live provider, complete live
meeting-to-Architect acceptance, automatic blocker detection/repeated meetings and
hosted/distributed execution are not certified by these fixture checks.
Verification: 938 tests passed, two optional Docker skips and three existing SDK
warnings; Ruff, mypy across 50 source files and editor diagnostics were clean.
The model-free copied-fixture report at
`sim/.run-artifacts/managed-meetings-20261009.json` records `succeeded=true`, an
executed command exit 0 and the expected generated artifact; no container session
was used. That fixture-only checkpoint was uncommitted and made no remote writes
or default activation; subsequent live acceptance is recorded above.

Foundation work established signed ingress/internal authentication, routing and
deduplication, policy checks, model bootstrap, escalation and scheduler/meeting
construction. Hosted baseline CI passed pytest/Ruff/mypy and a retained mock
simulation artifact on Python 3.14. Tool development added exact-span editing,
bounded search/paging, prepared Docker sessions, native approval/memory persistence
and independent success/failure verification with cleanup. Constrained Grok/Kimi
approved-task fixtures passed; broad standalone zero-error tool certification is
still incomplete. Recovered tool errors are diagnostics, not automatic contribution
failure; a qualified delivery still needs correct artifacts, final tests,
independent verification, approvals, valid original budgets and cleanup.

Configuration work added strict immutable organization revisions, configurable
agents/teams/models and native Python workflow graphs, followed by durable
assignment/run journals, opt-in service activation, detached local workers and
scoped native PM coordination/Architect review. Review admission, bounded correction
handoff, exact operator-only same-PR publication and durable follow-up recovery
completed the supervised local lifecycle. Storage writers enforce atomic/fsynced
replacement, explicit permissions and symlink refusal; runtime guards survive
Python optimization. Subsequent PM planning/publication and three-role live
acceptance are recorded below. No checkpoint certifies hosted/distributed operation
or automatic dependency scheduling.

Merged baseline (2026-10-08): `69aa8d4` contains PRs #11/#13; the bounded
follow-ups are now committed locally as `307b86a`. Committed baseline gates: 853 tests pass/two optional Docker
skips, Ruff and mypy (48 sources). Issues #9/#10/#12 are resolved upstream.
Read-only exact reconciliation supersedes older notes about recovery object
uploads; write-time budget and pre/post-PATCH guards remain enforced. Historical
consolidation counts and then-open blocker descriptions below are not current.
Both real Docker probes and a fresh scoped Grok implementation/independent
five-test verification trial passed. A separate approved fresh issue #4 task then
published [draft PR #5](https://github.com/uhvogala/aitobuild_example/pull/5) with
exact tree/bytes/modes/head/base checks and restart deduplication. A configured
native Architect inspected complete immutable source/diffs and submitted only
the exact operator-approved COMMENT, pinned to that head. An initial five-minute
review expired and failed closed; a freshly approved 30-minute task completed
without changing the prior deadline/abort. A reporting-only native Message
serialization error was recovered from persisted metadata, without model replay.
No genuine defect/correction was proposed. Live same-PR correction and combined
configured-service acceptance were pending at that slice; old deadlines are never reset.
Subsequent supervised service acceptance (2026-10-08): signed webhook injection
through the authenticated ASGI app/TestClient and detached worker produced
draft PR #7 for fresh issue #6 with live Grok, Docker and GitHub adapters. Idle
checkpoint restart, duplicate delivery, five-test independent verification,
exact remote bytes/modes/parent/head/base and cleanup passed. An exact-span edit
error was recovered. The first Architect task failed on GitHub read errors/model
timeout and remains failed/aborted; a separately approved new revision/task/session
and original 30-minute ledger completed full source/diff inspection and exact
final-only COMMENT submission. Saved-approval and terminal restart/deduplication
preserved ledgers and exactly one review. Reports are in
`sim/.run-artifacts/example-service-20261008`. Hosted webhook delivery, distributed
operations and live same-PR correction remain unverified. No merge, service
commit/push, dependencies or default activation.
Fresh full service repeat (2026-10-08): separately approved issue #8 produced
draft PR #9 at `f4c7d00b6754f7e31c1ea25fb06ab06eb0b5c013`. Both native roles
completed their first attempt with zero saved diagnostics. Ten checked native
approvals, independent five-test verification, four complete source/diff
inspections, exact separately approved final-only COMMENT, idle/terminal restart,
duplicate admission, remote pins and cleanup passed. Exactly one COMMENT remains
at review `5457488010`, with original Developer/review ledgers and prior trial
evidence unchanged. Metadata-only audit mistakes were corrected without model
replay or COMMENT resubmission. Evidence:
`sim/.run-artifacts/example-service-20261008-fresh/report.json`. This is still
local signed-webhook/TestClient acceptance, not hosted or distributed execution;
no correction, merge, service commit/push, dependencies or default activation.
Bounded live same-PR correction publication (2026-10-08): a new explicitly
synthetic known-fault issue #10/draft PR #11 was inspected by the native Architect,
with exact final-only COMMENT and a two-path signed-multiplication proposal.
Three failed review attempts remain intact; the successful fresh review used the
correctly wired role timeout. A first correction was cancelled after the trial
supervisor rejected harmless `true`; operator retirement now permits its abandoned
idle state with an already-aborted original budget, without resets. A linked fresh
task completed six independent tests and consumed its exact nonce-bound approval
to advance the same PR from `a99e973116ebd31800af11de56c51e78c77ffbcb` to
`9ddc9dcedacb70bb075bf2c3fd91f5444b36a3e8`. Exact remote pins, preserved tests,
restart/duplicate publication, original ledgers, retired predecessor, one-round
limit and cleanup passed. Corrected-head Architect review failed closed on provider
`429 pd_decode_pushback`; full correction/re-review acceptance was pending at that checkpoint.
Report: `sim/.run-artifacts/example-correction-20261008/final-correction-report.json`.
Standing user authorization is consumed through scoped service approvals and exact
publication digests, not approval bypass or default activation. No merge or service
commit/push; hosted delivery and distributed operation remain unverified.
Subsequent corrected-head recovery (2026-10-08): a fresh operator-bound native
Architect task, revision, session and original zero-write 30-minute ledger
completed four source/diff inspections and exact final-only COMMENT `5459666675`.
It identified no remaining functional defect. Original failed task/aborted ledger,
all prior runs/budgets and the one-round root scope remain unchanged. Idle and
terminal restart, two exact head-pinned reviews, exact remote snapshot, consumed
publication approval and cleanup passed all 30 independent audit checks.
The supervised correction/re-review sequence passed through operator recovery;
the original configured-service review remains failed, not silently retried.
No production retry endpoint or default activation was added. Reporter datetime
and audit-wrapper/timestamp errors were recovered metadata-only, with the failed
harness report retained. The timestamp/wrapper audit was subsequently repaired
and tested offline against saved snapshots and negative mutations. Report:
`sim/.run-artifacts/example-correction-20261008/final-correction-recovery-report.json`.
Nineteen native review contract cases passed again. No service push or merge;
hosted delivery/distributed execution and live managed PM acceptance remain separate.

Managed PM planning slice (2026-10-09, uncommitted over `c690958`): explicit
PM-only revision/event admission offers an unapproved repository/base-pinned task.
Fresh approval initializes a zero-write original-deadline ledger without checkout
or commands. A configured native PM uses a read-only request/target inspection
tool and returns a bounded strict acyclic plan. Immutable plans save original
scope/revision/operator/binding/approval/budget pins and complete inspection evidence.
Exact creation approval pins title/body/labels and marker bytes; a separate resolved
link approval pins actual issue IDs/numbers and final body bytes. Decisions and
per-effect intents persist before writes. Target, content and original budget are
checked at write time; restart cannot replay models or reuse consumed approvals.

Durable receipts and bounded marker scans reconcile only exact landed effects.
Remote read-only reconciliation can finish metadata/preview staging after budget
abort/expiry, but cannot replay writes or approve unresolved links. Missing,
ambiguous, drifted and incomplete effects require operator inspection; a batch is
not atomic. Complete receipts stage separate unapproved Developer previews with
real issue identity and pinned child policy, never assignment/preparation/execution.
Normal startup, legacy in-memory PM tools and dependencies are unchanged. Forty-nine
native mock/request/detached/HTTP/recovery contracts pass; this is local acceptance,
not live PM semantics, hosted delivery or distributed execution.
Final local gates: **902 passed/two optional Docker skips**, Ruff and mypy
(49 source files), editor diagnostics and standalone ignored-harness lint pass.

Bounded live PM acceptance (2026-10-09): standing operator authorization was
consumed through the unchanged task/creation/resolved-link approvals in twelve
fresh isolated native Grok/GitHub trials. Request-scoped and detached success,
rejection at both publication gates, cancellation, real one-minute deadline expiry,
landed/absent POST/PATCH response loss, partial preview staging and a real synthetic
concurrent body edit passed. Independent read-only audit confirmed exact plan and
remote bytes/IDs, consumed decisions, unchanged original deadlines/reservations,
preserved failed/cancelled/aborted outcomes, no duplicate issues/previews, untouched
historical evidence/master/prior draft heads and no trial containers. Fifteen
open unassigned demo issues and nine unapproved/unprepared Developer previews are
retained; no implementation or merge occurred.

Discovery: immediate GitHub issue-list reads temporarily missed fresh markers,
causing two supervisor audit refusals. Direct GET verified known receipts and later
GET-only reconciliation recovered metadata under unchanged original ledgers, without
model or POST/PATCH replay. Original failed reports and supervisor-validation
mistakes remain recorded, not normalized into an error-free run. No product source
change was required; 49 focused regressions/Ruff/mypy and supervisor lint pass again.
Authoritative evidence: `sim/.run-artifacts/example-planning-20261009/live-report.json`.
This is bounded supervised local ASGI acceptance with explicitly injected failures,
not hosted/distributed acceptance or the full live product-team pilot. No service
commit/push, dependency change or default activation.

Latest bounded three-role E2E acceptance (2026-10-09): managed PM implementation
committed locally as `fe5d1a5` after fresh 902 tests/two optional Docker skips,
Ruff/mypy49. A fresh configured native PM plan created/linked issues #31/#32;
its original implementation preview, not a replacement webhook/task, drove native
Developer implementation through ten exact tool approvals and independent two-test
verification into draft PR #33 at `58ab116d64cc3afd287e3bceaff6f504612b683b`.
Architect complete source/base-diff inspection found no functional defect and
submitted one exact final-only head-pinned COMMENT `5468186817`. Independent audit
confirmed exact plans/IDs/link bodies, original handoff identity, remote parent/tree/
bytes/modes/base/head, consumed decisions, terminal restart/duplicate admission,
independent original ledgers/reservations and cleanup. Accepted PM/Developer/
Architect runs retain zero saved native diagnostics. Prior trial JSON evidence,
failed/aborted predecessors, master and earlier draft heads remain unchanged.

The initial supervisor stopped before planning approval on an uninitialized-ledger
lookup. A later publication audit used a nonexistent preview field; its completed
PM effects recovered metadata-only, with original failure retained. Dataclass/JSON
timestamps also needed canonical comparison. Two native Developer attempts then
failed/aborted following discovery policy and narrow supervisor-command refusal;
these are preserved, never replayed or relabelled successful. `allowed_paths` uses
literal prefixes, not globs. A fresh correctly configured prefix policy succeeded,
with exact source/test write restrictions enforced independently. No product source
change was needed during trials; supervisor lint/diagnostics pass. Evidence:
`sim/.run-artifacts/example-e2e-20261009-v4/final-report.json`.
This accepts one bounded supervised local three-role chain, not an uninterrupted
sequence of all earlier attempts, automatic dependency scheduling, dependent docs
execution, a new correction cycle, hosted/distributed delivery or full M5. The
dependent preview remains unapproved/unassigned/unprepared. No service push/merge,
dependencies or default activation. Do not replay consumed trial controls.

## 1. Executive Summary
This document outlines a policy-controlled, configurable product-team framework using
the **Microsoft Agent Framework (Python)**. PM, Architect and Developer are
initial role templates, not fixed team instances or mandatory workflow stages.
Human involvement is a deployment policy, not a universal lifecycle requirement.
Manual defaults preserve the accepted supervised profile; explicitly configured
automatic operation is the target, including merge through a separate trusted
publication operation rather than agent-held merge authority.

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

### 2.4 Central Configuration and Approval Policy

One versioned operator-owned JSON manifest is the configuration entry point.
It gathers runtime/models, repository sources, execution, scheduling, security,
organization definitions and per-stage approval policy. Secrets are environment
references; explicit environment overrides remain available. Unknown fields,
duplicate keys, invalid types and unsupported modes fail closed. Configuration
does not execute imports or replace trusted operation registries.

Approval stages cover task admission, Developer tools, PM issue creation and
dependency links, draft publication, Architect review publication, meeting
continuation, corrections and merge. Each uses `human`, `automatic` or `disabled`;
all current gates default to `human`, and merge defaults to `disabled` until a
supported trusted merge binding exists. Automatic mode is an operator policy
decision for the exact saved operation, not permission for a model to approve
itself. Disabled means refusal, not approval bypass.

Task admission has exactly one route: the module-level `admit_task` in
`organization_service.py`, the only code that may reach the private
`DeveloperPreviewRegistry._approve_admission`. A static test scans `src` for any
other reference: source text, attribute access and aliases, every string constant
(including implicit concatenation), and names assembled from literals with `+`,
f-strings, `str.join` or `%`. A name built fully at runtime cannot be caught
statically and is left to review. Human approvals (the HTTP approve
endpoint) and configured admissions both enter it with typed evidence, a closed
tagged union: `Human`, `OperatorPolicy`, `PlannedChild` (#31) and
`InheritedReview` (#37). The configured checks run in `ManagedOrganizationService._admit`,
which applies the trusted base binding `_configured_task_base` (`app.py`),
resolves the decision with `ApprovalPolicy.resolve`, and passes the exact preview
it evaluated; `admit_task` refuses unless that preview equals the stored one,
since the decision digest does not cover `source_payload`. The automatic
admission limit and dedupe depend on the resolved decision source, not on the
evidence variant, with one deliberate exception for `Consent` evidence (its
per-issue dedupe, described below). A new admission source (for example Discussions consent from
#42) adds an evidence variant and its checks to this route; it must not reach
`_approve_admission` itself or add a parallel path. Changing the route itself
requires an explicit update to this section first.

Rule: planning and correction tasks are human-admitted. `ManagedOrganizationService._admit`
refuses every automatic source (`OperatorPolicy`, `PlannedChild`, `InheritedReview`) for a
`pm-planning-*` task (#31), and in the same place refuses them for a correction task
(`published-correction-*` or a `correction_from_review` source) unless the operator configured
`correction: automatic`. That mode keeps automatic correction admission available for the
configured correction cycle. The check reads the same policy snapshot whose revision the
admission decision is bound to. Both refusals run before evidence dispatch, so they cover
every non-`Human` source; `Consent` is refused for planning and for corrections in every mode,
including `correction: automatic`.

Rule: consent admission requires `task_admission` to be `automatic`. Under
`human` mode a consent is refused, and only the HTTP approve endpoint
(`Human` evidence) admits a task.

Deliberate exception for `Consent` evidence (#42): a consent decision is
human-sourced (`source="human"`), so it never counts toward
`max_automatic_admissions`, like any human approval. It is still reached through
automated reading of a discussion comment rather than a person acting in the
service, so it shares the per-issue `(repository_id, issue_id)` dedupe with
automatic admissions, in both directions: a consent admission is refused when the
issue was already admitted by consent or automatically, and an automatic
admission defers when the issue was already admitted by consent. A later task
for that issue needs a direct human approval.

Policy revisions and the effective stage decision belong in immutable run/effect
evidence. Changes apply to new work; active runs refuse drift rather than silently
adopting a more permissive policy. Automatic continuation consumes the same exact
one-shot checkpoints as human input. Scope/head pins, role permissions, independent
verification, budgets, expiry, cancellation and cleanup remain mandatory. Failed
tasks and uncertain effects are never blindly replayed. Configured automatic
scope changes still create distinct authorized tasks and original ledgers.

Central settings, shared policy resolution, native service/worker continuation,
task admission and exact draft/correction publication adapters are implemented.
Merge and the complete unattended lifecycle remain staged work. Exposing a mode
in the schema is not proof of runtime support or live acceptance; unregistered
stages fail closed. See the current supported-stage table and factory requirements
in [README.md](README.md#central-operator-configuration).

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

### 4.3 Proposed GitHub Discussions Interface

Status: feature plan only; not implemented or activated. This proposal uses the
pulled `b5d494e` baseline, including the approval-model change merged as `b663498`
from [PR #31](https://github.com/uhvogala/aitobuild/pull/31), the bounded submit
follow-ups and meeting-timeout test/trial-harness fixes. These additions do not
implement consent intake or activate the Discussions workflow.
Discussions hold ideas, clarification and agent responses;
Issues and PRs remain the delivery artifacts. The goal is to offer bounded PM
planning from an ordinary conversation without requiring users to call Python
or use a special approval command.

#### Existing Contracts and Change Boundary

The recent contributions provide reusable correction/follow-up/pilot harnesses,
merged-prerequisite pause/resume, signed issue ingress through managed admission,
author/label allowlists, durable automatic-admission caps, consistent deadline
errors and stronger durable-file/publication recovery checks. None adds a
Discussions adapter or freeform external human decision intake. A harness's
unreviewed scripted approvals are not an authorization model for Discussions.
Merge remains unsupported; references to merge-like approver gates describe the
desired identity restriction, not an existing aitobuild merge operation.

Reuse `PlanningAdmission.offer`, `ManagedPlanning`, configured native agents and
graphs, existing exact stage requests, shared durable writers and the managed
local worker. Do not replace task/approval/run/budget storage or install a fixed
PM-to-Developer lifecycle. The example organization remains a definition probe.

The single-approval experience additionally needs a trusted human-consent bridge,
consent-bound publication adapters and a durable managed issue-amendment operation.
PR #31 requires human planning admission and an exact human issue-creation decision
before automatic child admission. A generic automatic PM decision does not meet
that contract. Preserve direct human provenance for the advertised issue creation;
use policy evidence for deterministic links and future effects within the consent.
Read-only planning reconciliation cannot update issues. Keep these additions
localized to planning publication, item-revision lookup and the decision bridge,
following the formal approval contracts in section 4.4.

New work belongs primarily in a Discussions GraphQL adapter, conversation/decision
receipt store, configured native conversation operation and trusted intake bridge.
Keep application ingress, strict central configuration and explicit service/worker
factory extensions small and additive. Existing issue ingress, APIs and policies
must behave unchanged when the integration is absent or disabled.

There is one necessary cross-cutting addition: service decisions currently bind
the shared operator actor, not an external GitHub commenter. Verified external
human evidence must survive preview admission, service approval and detached
queueing. Preserve the actual GitHub approver in human decision evidence and the
executing service operator separately; do not silently attribute every approval
to the shared internal token. Add only a trusted adapter capability for this
evidence, not caller-selectable `actor_id` fields on existing HTTP endpoints.

#### User Experience

1. A user opens a discussion in an enabled category, optionally using a category
    form for the problem, desired outcome, constraints and success criteria.
2. The configured conversation agent, normally a PM, asks questions and publishes
    a versioned brief with open questions. Other configured roles may contribute
    through explicit routing; one App can label responses with agent/role names.
3. Under an explicit bounded read-only planning policy, the configured PM prepares
    the complete proposed plan without asking the user to approve planning first.
    Save its offer, exact proposed issue titles/bodies/labels and dependency graph.
    Present one versioned plan explaining the target, scope, limits and actions:
    create these issues, resolve these links and stage implementation previews.
    Preparation grants no issue-write or implementation authority.
4. An authorized user replies naturally, for example, "Approved, create the work
    items for this plan." Trusted intake records one human consent for that exact
    bounded publication workflow, not a separate user approval for each stage.
5. The service creates the approved issues and resolves the approved dependency
    edges using their actual GitHub IDs/numbers. The trusted bridge records human
    planning admission and the exact human creation decision from the same consent;
    a consent-bound policy adapter handles deterministic links. No further human
    prompt is needed merely because issue numbers became available.
6. Completed planning posts issue links and staged Developer previews. Existing
    independent follow-up admission and merged-prerequisite handoff rules apply.
    The presented consent determines its scope; this initial publication workflow
    does not also authorize implementation, PR publication or merge. An explicitly
    presented optional plan-and-delivery bundle can cover the bounded delivery
    actions in section 4.4; a generic "Approved" never adds an undisclosed bundle.
7. Later discussion or comments on a linked issue can request revisions. The PM
    proposes a concrete change set against the current items. One authorized
    approval of that amendment permits the listed edits and dependency changes
    to the existing issues, rather than requiring a replacement discussion or
    creating duplicate issues for every clarification.

Conversation can evolve created work items through approved amendments. Preserve
old plan/approval/effect receipts as history, not as a prohibition on editing the
current GitHub items. Active execution scopes remain immutable: an item amendment
cannot silently change what an already-approved Developer run is implementing.
New or changed work needs a revised task binding, while unchanged tasks keep their
original authority and budgets. Each consent may cover several related stages
of its explicitly presented workflow, never an unspecified future plan.

#### Single Approval and Exact Effect Evidence

Record a human plan consent containing the verified approver, source comment,
immutable plan digest, target/base, issue keys and exact content, dependency edges,
allowed deterministic link-body template, child policy, limits, expiry and
configuration/binding revisions. This is one user-facing decision covering a
closed set of related publication effects. Do not ask for additional human
approval of mechanical steps already covered by that decision.

New GitHub issue numbers cannot be approved as literal values before creation.
Instead, consent authorizes a precise transformation: substitute only IDs/numbers
from confirmed creation receipts for this plan's approved issue keys in its saved
dependency template. Save the resulting exact body bytes and resolved identities
before PATCH. Refuse missing/ambiguous receipts, extra edges, model-rewritten
content or concurrent issue drift. Material changes require a new amendment;
number resolution alone does not.

Preserve existing per-stage/effect checkpoints, original budgets, intent-before-write
and exact evidence. The trusted consent bridge supplies the human planning-admission
decision and one exact human `pm_issue_creation` decision only after proving that
the saved request is a permitted projection of the presented plan. Record both
against the same verified consent without inventing additional human comments.
A registered policy adapter resolves deterministic link requests, saving exact
resolved bytes and linking its decision to that consent. All adapters refuse
requests outside the consent, even in automatic mode. Read-only preparation is
separately operator-configured; it does not auto-admit a managed planning task.

Single-approval mode is an explicit opt-in workflow binding; it does not weaken
manual defaults or change unrelated deployments. Activation requires the exact
consent-aware adapters and compatible stage policy. Do not silently reuse a broad
automatic approval handler that ignores consent. Disabled stages still refuse;
automatic decision/admission ceilings still apply. Existing deployments using
manual stage prompts remain supported, but those prompts are not the intended
experience for this configured Discussions workflow.

#### Identity and Freeform Approval

Operator configuration contains enabled repository/category bindings, allowed
conversation participants and a separate approver allowlist scoped by repository
and supported approval stage. Conversation participation alone grants no approval
rights. Pin approvers to host-qualified GitHub user IDs; optional login entries
resolve through trusted GitHub reads before activation and retain that binding.
Do not authorize using display names, comment text, reactions, `authorAssociation`
or model-supplied identities. Reject bot, missing/ghost and unverifiable actors.

Freeform approvals are supported; exact commands and user-entered digests are not
required. A configured native verifier (the PM or a separate agent) receives only
the bounded human comment and trusted presentation/context of the pending action.
It returns strict `approve`, `reject` or `clarify` intent with supporting text spans.
It has no decision/write tools and cannot select actors, change the action or
approve its own proposal. Model output is interpretation evidence, not authority.
Do not substitute a confidence score for an explicit, attributable approval.

Trusted code establishes identity and target before interpretation. A direct reply
to the App's saved plan/amendment comment is the preferred binding. A top-level
freeform comment may qualify only when its reference is explicit and exactly one
presented pending consent matches; otherwise ask for clarification. Bind that
presentation to the plan/change-set digest and its allowed operations, with exact
preview/assignment/request, stage, target/base/head and policy/binding pins saved
for each derived effect. Users need not type these machine identifiers.

"Looks interesting", quoted approvals, approval of an older proposal, conflicting
wording, conditional approval and unclear pronouns do not advance work. Request
clarification, or a fresh revised proposal when scope changes. An explicit rejection
is recorded against its exact request; do not silently turn it into cancellation
of an unrelated task. One human comment can authorize one presented bounded plan
or amendment spanning several related stages. Do not treat it as approval of other
proposals merely because the wording also expresses broader enthusiasm.

Immediately before acceptance/consumption, re-read the live comment and referenced
proposal, verifying author, reply relationship, body/version, deletion status,
allowlist/policy binding, exact pending request and original budget. Edits or
deletions invalidate unconsumed evidence and require a fresh evaluation; never
reinterpret a consumed comment to enlarge its accepted consent. Covered effect
decisions use that saved consent, not another model interpretation. Deleting a consumed
approval cannot undo an already-landed effect: preserve the receipt and report it.
The interval between a GitHub read and a write is not transactional; document
this limit rather than claiming atomic cross-platform approval/revocation.

The top-level freeform consent is a `human` decision, not `operator_policy`, merely
because a model classified it. Its exact planning admission and issue-creation
projections also retain direct human provenance. Automatic decisions for derived
links and future delivery effects are separately attributed to the consent-bound
policy, retaining the real consenting human and exact consent relationship.
Persist both forms of evidence before
enqueueing/consuming their gates. Restart at an accepted idle boundary uses saved
evidence without another model call; interrupted interpretation/uncertain effects
fail closed. Active allowlist or binding drift refuses continuation rather than
silently changing who may approve.

#### Continued Discussion and Item Amendments

Subscribe to comments on managed, linked issues as well as their originating
discussion. Both feed the configured conversation workflow and may propose edits
to existing work items. The issue thread is the natural home for task-specific
clarification; the discussion remains available for broader cross-issue changes.
Only operator-bound managed issue identities qualify, and `issue_comment` events
on PRs must not enter this issue-amendment path.

The PM saves a proposed amendment listing affected issue IDs/numbers, the current
approved item revision and live title/body/labels, exact desired replacements,
dependency changes, rationale, task impact and effect/budget limits. Present the
change set in the originating thread. One clear approval from an authorized user,
such as "Yes, apply these changes", authorizes the listed updates and deterministic
link adjustments. Separate issue-update authority must be registered; approval of
the original creation plan alone is not standing permission for later edits.

Apply changes to the existing issues, preserving their identity and links. Store
append-only item revisions and per-update intents/receipts; never rewrite original
creation plans or human decisions. Preserve unrelated live content. Concurrent
human edits require rereading and a revised proposal rather than overwriting them.
An approved amendment has its own bounded task/ledger, not an extension or reset
of the original planning or delivery budget. Uncertain updates reconcile read-only;
no new amendment may be used to blindly replay a failed or uncertain effect.

Planning child lookup and dependency handoff must understand the latest confirmed,
approved item revision. Explicitly distinguish authorized amendments from arbitrary
remote drift; do not simply drop existing exact-content checks. The original plan,
publication lineage and all historical receipts remain available for audit.

For unstarted work, changes to objective, criteria, dependencies or scope stage a
new revision-pinned preview and mark the old unstarted preview superseded; preserve
its evidence and prevent execution under stale approval. A new task needs its own
authority, which can be included in the same presented amendment only through an
explicitly supported bounded admission adapter. Publication-only amendments do
not authorize implementation implicitly.

For active work, descriptive discussion can continue, but a scope-changing
amendment is blocked until a coordinated stop/drain or completion establishes a
safe handoff. Do not silently mutate the run, auto-cancel unrelated work or reset
its ledger. Completed work may gain a separately scoped follow-up task; changing
the issue text does not retroactively change verified artifacts or certify new
requirements. Deleting issues, rewriting PRs or merging is outside this feature.

#### Configuration and Agent Boundaries

Add a strict, opt-in Discussions configuration with repository IDs/category IDs,
participant and stage-specific approver bindings, configured agent/team/workflow
references, GraphQL credential references, operator-owned planning target/base
resolution, consent-bound publication/amendment bindings and limits. Missing
bindings refuse activation. Secrets remain
environment-backed; explicit factories must bind the effective policy exactly.
Discussions can live in the delivery repository or a configured product repository;
cross-repository target mappings are operator-owned, never inferred from comments.

Keep conversation/reply authorization separate from delivery approval. An operator
explicitly authorizes bounded App responses under a revisioned discussion policy,
with exact reply intents and receipts; otherwise remote replies cannot be written.
This standing authorization covers discussion comments only, never issue/PR writes.
Enforce durable per-thread/global model-call, response-byte, turn, time and
outstanding-offer limits across restarts, with a stop control. Conversation limits
are separate from original planning/delivery ledgers; none can reset another.

Use native Agent Framework sessions and configured role instructions; keep replies,
structured briefs and interpretation outputs within their scoped adapters. Agents
cannot read credentials, choose arbitrary repositories, run commands or execute
code from discussion text. Treat human and bot content as untrusted data. Any
repository context is explicitly bound read-only, and permissions of conversation
participants bound what may be disclosed. Do not post secrets, private tool logs
or content from another access scope. Sending discussion content to the configured
model is an explicit deployment/privacy decision.

#### Transport, Receipts and Recovery

Use the GitHub GraphQL Discussions API for paged thread reads and comment/reply
mutations. Prefer a GitHub App installed only on configured repositories, with
Discussions read/write permissions for conversation; issue publication keeps its
separate approval-enforcing adapter and required permissions. Subscribe to
`discussion`, `discussion_comment` and `issue_comment` for managed linked issues;
validate HMAC signatures and the installed repository identity. Persist intake
before acknowledging and run model work in the background, not inside webhook
response latency.

Discussion events go to a distinct configured intake path; do not pass them
through the issue-task extractor or label an unsigned/internal payload as a
verified GitHub actor. Reuse transport signature validation without widening
existing issue automatic-admission guards. Ignore App-authored events for model
invocation, deduplicate delivery IDs and handle edits/deletions and out-of-order
events through authoritative reads. Bounded reconciliation covers missed events;
GitHub currently documents discussion webhooks as public preview.

Separate durable conversation/intake state from definition and managed-run storage.
Save source discussion/comment IDs and URLs, exact bounded source bytes/digests,
human author IDs, context selection, brief/item revisions, agent/binding pins,
verifier output, human consents, derived stage/effect relationships, superseded
unstarted previews, presentation/request/operation pins, deadlines, consumed
decisions and remote reply/update intents/receipts. Use shared atomic/fsynced
writers, explicit file permissions, symlink refusal and local locks; do not claim
distributed ownership.

Offer identities derive from discussion identity plus the frozen brief/target
revision, not webhook delivery IDs or mutable thread timestamps. Duplicate
notifications or bot status comments must not create fresh offers. Partial local
offer staging recovers metadata through existing idempotent contracts. Never
recreate missing initialized ledgers or previews from thread text. After uncertain
comment publication, use GET-only reconciliation of exact saved markers/content;
GraphQL `clientMutationId` is correlation, not an exactly-once guarantee. Ambiguous
or absent effects need inspection, not blind mutation retries.

#### Delivery Slices and Acceptance

1. Add strict contracts/configuration, mocked GraphQL transport and durable intake,
    conversation/presentation/decision receipts. Cover pagination, identity,
    signatures, disabled/unbound activation, dedupe, stale events and bot loops.
2. Add configured bounded native conversation and automatic unapproved offers.
    Add explicitly authorized read-only plan preparation before the single human
    prompt. Validate readiness, exact target/base/child-policy binding, offer dedupe,
    full plan presentations and restart without model/write replay.
3. Add whitelisted freeform plan consent and exact consent-bound PM publication.
    Prove that one human approval creates the approved issues and resolved links,
    with no second prompt for issue-number substitution. Propagate actual human
    admission/creation evidence and derived link-policy evidence through
    preview/service/worker receipts. Publication-only consent must not trigger
    PR #31's automatic child eligibility as implicit delivery authorization.
4. Add the durable managed amendment operation and linked-issue comment intake.
    Prove single-approval edits of existing issues, approved item revision lookup,
    dependency updates, stale-preview supersession and active-task handoff refusal.
    Other delivery stages retain their current interfaces until their bounded
    consent adapters are explicitly registered and tested; merge stays excluded.
5. Extend the reusable trial harness with a freshly authorized disposable-target
    Discussions scenario, not its approve-everything helper. Demonstrate human
    conversation, one approved plan producing linked issues, continued discussion
    and issue-thread amendments updating those same items, and separate Developer
    admission. Preserve artifacts and clean up local resources. This is a scoped
    live integration trial, not hosted/distributed certification.

Required negative coverage includes non-allowlisted/renamed/spoofed actors,
quoted or injected approval text, ambiguity and conditional/negative intent,
multiple/stale pending requests, changed/removed source comments or presentations,
consumed-comment reuse, stage escalation, scope/base/head or policy drift,
automatic/disabled mode mismatches, expiry/cancellation, queue-time revocation,
tampered/lost receipts and uncertain remote writes. No such case may publish
issues, authorize implementation or reset budgets. Also refuse additions outside
the consent, fabricated issue-number mappings, changed link templates, attempts
to treat creation consent as later update authority, stale amendment baselines,
concurrent human edits, unlinked/PR comment intake and silent active-scope changes.
Positive cases must show exactly one user approval per plan or amendment, the real
whitelisted human, exact source/consent/effect relationships, approved updates
without duplicate issues and unchanged historical evidence/budgets. Restart must
not repeat an approval prompt, a model call or an uncertain effect.

Run focused adapter/admission/worker tests after each runtime slice, then repository
pytest/Ruff/mypy gates and the mock copied-fixture simulation. Recheck baseline CI
on the selected implementation revision; the previously observed `5c1c2fb` meeting
timeout/drain failure is historical evidence, not proof of a current failure or fix.
Update current-state setup docs only as capabilities become implemented. The
minimum accepted feature is conversation -> frozen offer -> verified freeform
plan consent -> automatically created/linked issues and unapproved implementation
previews -> approved amendments from Discussions or issue comments updating those
same items, with unchanged delivery verification, original budgets, role ceilings
and merge refusal. One user-facing approval is not the same as one unaudited write.

### 4.4 Proposed Bounded Human Consent and Delivery Approval

Status: consent evidence foundation implemented as an inactive library; runtime
approval integration and delivery remain proposed, not activated or permission to
merge. It extends section 4.3 to remove repeated human work without erasing exact
approvals, verification, configurable workflows or immutable execution scopes.
The evidence types below now have an initial implementation; `PreparedPlan`,
`ConsentUsage`, promotion and managed decision adapters remain proposed contracts.
These are not new HTTP APIs or activated JSON settings.

#### PR #31 Baseline and Compatibility

PR #31 is merged as `b663498` and its `_check_planned_child` implementation is now
in the local `b5d494e` checkout. Retain that guard rather than implementing a second
human-plan eligibility path. The pull also includes bounded submit follow-ups and
the meeting-timeout test fix; validate the selected baseline with the repository
gates rather than treating the old CI failure as an unresolved prerequisite.

The code establishes these rules:

- Unapproved `pm-planning-*` tasks and tasks without repository issue context
  cannot enter through automatic task admission; they need human admission.
- Planned/dependent children qualify through trusted stored planning evidence,
  not issue-author/label lookup: a completed parent, matching published plan,
  human-admitted planning preview and exactly one approved human creation decision
  matching `workflow_input_digest(ManagedPlanning.approval_data(plan, publication))`.
- Child admission remains an `operator_policy` decision with the frozen child
  base and the existing durable admission cap. Generic automatic issue creation
  does not qualify a child through this human-plan path.
- Existing route/publication/handoff checks still establish actual child ownership,
  complete effects and remote identity/content. The new guard does not replace them.

Retain these protections. Human plan approval establishes child eligibility under
the PR's configured policy, not blanket authority for future work. For newly
consent-bound plans, require an additional explicit delivery grant before child
admission; otherwise a publication-only consent could accidentally start work.
Legacy workflows retain their documented configured behavior, but their old
decisions must not be upgraded into new broader consents by inference.

#### PR Baseline Alignment

This branch is rebased onto master `f7918b7e8b7ee8c7cd5b19225926592bf9049aae`
(2026-10-12), which contains every PR below plus the single admission route
(#43, `32399942ba11fb63fd7e913a816289e6b65340ec`) and its guard follow-ups (#46).
Heads are the reviewed heads that were merged; the merge commit follows.

| PR, merged head and merge commit | Effect on this plan |
| --- | --- |
| [#36](https://github.com/uhvogala/aitobuild/pull/36), `51f42c4f9f389d8af4205873e8d54e77756d632b` → `b0946e2` | Bounded asynchronous lock acquisition after local cancellation supports the stop/drain contract. Cross-worker contention still refuses immediately; this adds no retry or approval authority. |
| [#37](https://github.com/uhvogala/aitobuild/pull/37), `6b876f4b917b08b6287b35ba04cf08bf153586a2` → `3c7f978` | A staged Architect review derives automatic admission eligibility from its exact admitted, published parent. Reuse that lineage path and its one-automatic-review-per-parent/head guard; it does not authorize COMMENT publication, corrections or single-consent delivery by itself. |
| [#38](https://github.com/uhvogala/aitobuild/pull/38), `1d6df5896956ceecbff1cdfa8fbd90ca1e66bb1f` → `a5418b2` | Durable correction-proposal failure counts/errors, reminder revalidation and real script-entrypoint allowlist tests reinforce existing bounds. They do not change the human approval model or activate automatic correction admission. |
| [#39](https://github.com/uhvogala/aitobuild/pull/39), `711985d3550f9431b483012dbfd57f19e6d968f0` → `4d404cf` | A shared planned-child classifier, public validated read-only receipt accessors and exact dependent-handoff assignment checks provide the preferred admission foundation. They retain PR #31's direct human planning/creation requirement. |
| [#40](https://github.com/uhvogala/aitobuild/pull/40), `c292132aa6e574d7d8572c376b08fdb4efbd6849` → `325e57b` | The pilot forwards `human_admission=True` only for planning to the merged #35 helper, which calls the gated approval endpoint and records a human operator action. Other phases retain `False`. This fixes harness compatibility with #31, not correction/re-review admission or attributable production consent. |

PRs #37 and #39 explicitly require separate human approval-model sign-off before
merge. ReviewBot or CI success does not supply it. Their shared changes in
`app.py`, `developer_preview.py` and `organization_service.py` overlap: prefer
integrating #39's shared classification and validated readers first, then rebasing
#37 onto that result. Preserve one classifier, not two slightly different rules.
The independent #36/#38 safeguards can be integrated separately after review.
Do not automatically merge, rebase or cherry-pick any of them as planning work.

Merged #40 is independent of #37/#39 and depends on the `HandsOff.child` argument
from merged [#35](https://github.com/uhvogala/aitobuild/pull/35). The fresh
implementation baseline contains both; do not apply #40 alone to `b5d494e`. Neither
change makes planning automatically admissible or approves PM issue creation.
Their trial operator call is not a model/request-controlled human-approval flag.
The production single-consent bridge must supply the exact attributable human
admission evidence through the existing gate, followed by the separate exact
human creation projection covered by the same accepted presentation.

If these PRs are accepted, update the consent integration as follows:

- Use `is_planned_child`, `published_review_target`,
    `ManagedPlanning.stored_publication` and `ManagedDependencies.stored_handoff`
    at their existing ownership boundaries. Avoid private receipt-loader calls,
    copied prefix tests or a second automatic-review admission branch. The public
    readers retain loader validation; their names alone do not grant authority.
- Before projecting review admission for a consent-bound parent, join its saved
    delivery grant, permitted review stage, exact published target/head and original
    lineage. Then apply #37's staged receipt, admitted-parent bundle/base checks and
    shared planned-child/allowlist eligibility path. Parent eligibility is necessary,
    not permission to enlarge a `plan_publish` consent into delivery or review.
- Keep review admission and head-pinned COMMENT publication separate exact
    decisions. Reviews consume the existing automatic-admission cap; preserve #37's
    per-parent/head dedupe across restarts and rejection. A fresh preview ID cannot
    replenish authority or turn a failed/uncertain review into a safe retry.
- With #37's normal configured callback, a correction parent does not qualify a
    re-review automatically. This is a current implementation gap, not the desired
    policy: add configurable consent-aware lineage admission for both corrections
    and re-reviews, with the trusted provisioning needed to run them. Until those
    adapters exist, report the unsupported automatic profile before presentation;
    do not advertise it and then require undisclosed manual steps. Never fall back
    to an issue allowlist for correction tasks.
- Use #36's bounded local lock wait for cancellation/revocation integration and
    preserve durable cancellation intent, thread drain and immediate busy refusal
    for other owners. Lock contention is not authority to replay an effect.
- Preserve #38's persisted failure count/error and pre-reminder revalidation.
    Version or refuse incompatible saved session/binding state rather than resetting
    counters when a persisted key changes. Exhausted proposal attempts stop proposal
    generation; they are not evidence that requirements passed semantic review.

Before adopting the combined baseline, run the focused planned-child, dependent-
handoff, review-admission, preview-dedupe, worker-cancellation and submit/harness
tests, including #35/#40's follow-up and pilot planning-admission regressions,
then normal repository gates. #40's flag-forwarding and AST tests prove wiring,
not a complete planning run or production consent authentication. Add native
receipt-backed positive coverage
for a real published planned child leading to its review, not only #37's isolated
test that spies on entry to the planned-child guard. For consent integration,
also prove publication-only refusal, grant-aware review admission/publication,
head/parent/handoff drift, configured correction/re-review success and refusals,
per-head dedupe and
revocation during local/cross-worker contention without changed original budgets.
Pending-branch CI is not a substitute for these combined and consent-aware checks.

#### Advertised Authorization Bundles

Every presentation selects one operator-supported bundle and states its actual
actions, limits, target and stop conditions. One verified human response accepts
that bundle. Do not display a separate prompt for each internal stage.

| Proposed bundle | What one approval covers | What it does not cover |
| --- | --- | --- |
| `plan_publish` | Admit the frozen planning request, create its advertised issues, resolve its advertised dependency links, stage unapproved child previews | Child implementation, PR writes, review/correction execution or merge |
| `plan_deliver` | `plan_publish` plus delivery of explicitly listed child keys, verification, draft publication, head-pinned COMMENT review and any explicitly capped same-PR correction/re-review chain | New issue keys, changed criteria/scope, unrelated tasks or merge |
| `task_deliver` | One exact existing task through the advertised delivery/review stages and optional capped correction chain | Other tasks, scope expansion or merge |
| `amend_items` | Exact before/after edits to named managed issues, declared dependency changes and stale unstarted-preview supersession | Active-run mutation or implementation unless a bounded replacement-task grant is explicitly included |

`plan_publish` is the Discussions default. `plan_deliver` is an explicit optional
profile, never inferred from issue creation or vague enthusiasm. An unsupported
bundle or unavailable required adapter fails before consent, rather than promising
single approval and asking for undisclosed stage permissions later.

#### Immutable Contracts and Evidence

Use strict versioned records and existing canonical JSON/digest conventions:

- `PreparedPlan`: a stable ID, frozen request/target/base and child policy,
  validated `PlanProposal`, exact rendered issue bytes/labels/markers, dependency
  template, read-only inspection evidence, configuration pins and preparation
  limits/deadline. It is not an approved managed run or an issue-write permission.
- `ConsentPresentation`: immutable presentation/source identity, bundle, prepared
  content digest, authorized issue/task keys, stage/effect set, allowed runtime
  substitutions, per-task and aggregate limits, expiry and all relevant bindings.
- `HumanConsent`: exactly one presentation, verified host-qualified human identity,
  source comment ID/version/bytes/digest, strict interpretation evidence, accepted
  scope, acceptance time and trusted service identity recorded separately.
- `ConsentProjection`: consent/presentation IDs, rule/binding revision, exact saved
  preview or pending request, input and operation digests, parent/item lineage,
  concrete target/base/head and the resulting existing approval-decision reference.
- `ConsentUsage`: durable reservations/counts for authorized tasks, calls, changed
  paths and remote effects across descendants; consumed projection IDs; explicit
  revocation/exhaustion evidence. It supplements, never replaces, original ledgers.

Each field that affects authority participates in the canonical digest. Derive
human identity from verified intake, never from record fields supplied by a model
or an untrusted API caller. Keep human evidence and projection provenance separate
from `ApprovalDecision`'s stage/mode/source/actor/operation evidence; link them
durably instead of changing `operator_policy` decisions into fabricated humans.

The bridge may create multiple exact decisions from one human consent only for
actions that the human actually accepted. In particular, human planning admission
and the advertised exact issue creation retain `source="human"`. Unknown future
tool calls, verified snapshots, review bodies and correction pushes use registered
`operator_policy` adapters, even though their outer mandate came from a human.
No model approves its own proposal, chooses an actor or expands the grant.

#### Preparation and Promotion

PR #31's planning-admission refusal remains intact. Prepare the draft in an
explicit bounded read-only conversation/planning operation under operator policy,
not by auto-admitting a managed `pm-planning-*` preview. Save its offer and draft
without implementation tools, remote writes or initialized managed-run ownership.
The preparation budget includes its model work and cannot be reset by redelivery.

Current `ManagedPlanning.propose` renders markers from assignment identity and
includes admission time, ledger path and runtime pins in `PlanRevision.digest`.
Therefore pre-consent drafting needs an explicit promotion contract, not an
assumption that a draft digest already equals the final runtime plan digest:

1. Render opted-in drafts with stable prepared-plan markers before presentation.
    Keep legacy rendering unchanged. Freeze all issue content and dependency rules.
2. Persist the accepted consent and its admission projection before admitting the
    offered planning preview. Admit it as a human decision, not through automatic
    planning admission; preserve the actual commenter and trusted execution operator.
3. Under the normal managed assignment/ledger checks, promote the saved proposal
    without another PM generation. Recheck the exact trusted repository/base and
    request inspection. Allow only documented runtime attachments: assignment/
    preview IDs, scope digest, admission timestamp, original budget path/deadline,
    publication nonce and resulting runtime plan digest. The grant caps the deadline.
4. Save a promotion receipt binding prepared-content and runtime-plan digests.
    Prove unchanged titles/bodies/labels/markers, issue keys, criteria, dependency
    graph, target/base, child policy and configuration/binding pins. Any change
    outside the runtime attachment whitelist needs a new presentation/consent.
5. For the exact saved creation request, persist one human projection whose request
    digest and decision satisfy PR #31's existing human creation check. Admit and
    enqueue only through trusted service capabilities, not a caller actor override.
6. Resolve dependency numbers only from confirmed creation receipts. Save exact
    resolved bytes and a consent-bound link-policy decision before each write.

The creation request must describe the actual selected bundle. Its current
`implementation="Stage unapproved previews only; no assignment or execution"`
and separate-link-approval wording cannot be silently reused as evidence for a
delivery grant. Add a versioned opt-in approval-data contract and update producers,
digest checks and readers together; do not mutate historical requests or receipts.

#### Policies, Descendants and Recovery

Operator-owned authorization profiles bind supported bundles to configured
agents/graphs/routes, stage adapters, verified approver action allowlists, target
repositories, limits and effect-derivation rules. They are a new strict opt-in
configuration contract, not new universal approval modes. Keep `human`, `automatic`
and `disabled`, manual defaults, role ceilings and disabled merge unchanged.

The initial single-consent planning profile uses human planning admission and
`pm_issue_creation="human"`, with consent-bound automatic dependency publication.
Delivery profiles additionally require compatible automatic bindings for unknown
Developer tools, draft/review publication and opted-in correction/continuation.
Descendant task admission must be automatic and grant-aware if advertised as
unattended. Every required disabled/manual-incompatible or missing binding is
reported before presentation. An operator's global automatic setting alone is
not proof of a particular consent, and a consent cannot override a disabled gate.

For a planned child, join the human plan evidence from PR #31 with the saved
delivery grant, exact authorized key and existing publication/route checks. Each
child has its own immutable approval, assignment and original ledger; admission
still counts against `max_automatic_admissions`. Correction/re-review children
also need exact receipt lineage, verified publication/head bindings and shared
correction-round ceilings. Do not infer child authority from naming prefixes.

After acceptance of #37/#39, reuse their shared parent-based eligibility and
validated receipt accessors when applying these extra grant checks. A review's
consent grant comes from the verified delivery lineage, never from a reconstructed
issue author or a task-prefix heuristic. The ordinary #37 callback still refuses
correction re-reviews, but the configured automatic continuation profile must
resolve that eligibility through trusted correction lineage, without another
human approval when the accepted delivery grant already covers it.

#### Configurable Correction and Re-Review Continuation

Manual defaults remain available, but correction and re-review are not permanent
human boundaries. Operator configuration selects a compatible profile using the
existing `human`, `automatic` and `disabled` stages and the advertised consent:

- **Automatic:** with automatic task admission, Developer tools, correction
    publication and review publication, registered lineage/provisioning adapters
    and an explicit correction-round grant, stage and admit the correction, prepare
    its trusted seed, implement, independently verify, advance the same draft with
    its exact non-force publication decision, then stage/admit the head-pinned
    Architect re-review and publish its exact COMMENT. No extra human prompt is
    required solely because these are new child tasks or a corrected head.
- **Human:** the operator can choose explicit human gates for correction or review
    stages. Present those gates honestly; do not advertise that profile as an
    uninterrupted single-consent workflow. Human mode is a policy choice, not a
    hardcoded special case for every correction parent.
- **Disabled:** refuse the disabled correction/re-review action. Do not skip it,
    fabricate successful review or silently switch to a different authorization path.

Automatic eligibility follows the exact root delivery grant through saved review,
correction and publication receipts. Verify approved criteria/path scope, same
repository/PR/public branch, reviewed parent head, verified correction snapshot,
consumed publication approval and current corrected head before re-review. Give
each child its own immutable task and original ledger; preserve existing admission
caps, per-parent/head dedupe, shared round ceilings and aggregate consent limits.
Do not grant authority based only on an admitted ancestor, prefix or changed label.

The user approves the correction policy and maximum rounds with the advertised
delivery bundle, not every mechanically derived iteration. Human action is needed
for changed scope, a new product decision, exhausted limits, revoked/expired
authority or an unresolved/uncertain effect, not ordinary unchanged-scope
correction/re-review. A genuine correction proposal still comes from actual review;
never manufacture defects to keep the loop running. Merge remains unsupported.

Implement trusted seed provisioning alongside this automatic profile, rather than
deferring a mandatory manual fetch into an allegedly automatic correction cycle.
Activation checks must prove all required adapters and stage policies are bound.
No default activation or global permission widening is implied.

Dependent tasks additionally need confirmed human-merged prerequisites and the
trusted handoff adapter. A future operator-owned provisioning worker may refresh
the private seed and attest an exact new base before staging a separately pinned
task under the saved dependency rule. Neither a PM nor task commands select/fetch
an arbitrary base. Until that adapter exists, report seed/readiness work as a
manual boundary; do not promise uninterrupted `plan_deliver` for dependent work.

Bound aggregate fan-out, model calls, changed paths, write effects and lifetime
with durable reservations as well as each task's original limits. Known capacity
contention defers the same authorized work; it cannot replenish grant or task
budgets. Admission/decision ceilings and cancellation remain effective. Scope
changes, unresolved blockers and exhausted correction rounds return to humans.
Unchanged-scope continuation can use its exact registered policy adapter; arbitrary
native `human_input` still requires a separately supported trusted response binding.

Consume a source comment once as consent, with one immutable accepted record.
Consume each request/projection at most once under existing local locks. Persist
projection intents before updating preview/run/queue stores; metadata-only recovery
may finish the same recorded projection at a saved idle boundary. A crash cannot
reinterpret the comment, rerun the PM or authorize a different pending request.
This is not a cross-store/remote transaction: interrupted running or uncertain
effects retain evidence, stop and reconcile read-only, never blindly replay.

Reject stale/unconsumed presentations, edited/deleted source evidence, expired
authority, missing/tampered records or binding drift. For accepted consent, source
integrity loss stops remaining effects for inspection without rewriting history.
Explicit verified stop/revocation records block new effects and drain active work
before releasing ownership; landed writes remain recorded. Revocation or retry
cannot reset original budgets. A failed run is not rescued by presenting the same
uncertain effect as a new amendment or silently making a fresh task.

#### Authority Foundation Progress

[src/aitobuild/approval_consent.py](src/aitobuild/approval_consent.py) implements
strict profiles, presentations, host-qualified human/source records, accepted
consents and projection evidence, with an explicitly initialized `FileConsentStore`.
The immutable journal uses canonical digests, shared atomic/fsynced writes, private
permissions, local locks and symlink refusal. It preserves one consent per source/
presentation and one projection per exact request, checks compatible stage modes,
pins profile/content/source evidence and bounds recorded projection reservations.
Expired evidence remains readable but cannot support new consumption.

[tests/test_approval_consent.py](tests/test_approval_consent.py) covers direct human
creation versus policy-derived link evidence, authority escalation, stale source/
policy, duplicate comments/requests, limits, corruption/loss, concurrent consumers,
expiry and metadata-only recovery after a landed save loses its response.

This library neither authenticates GitHub actors nor interprets comments, proves
semantic equivalence of an effect to a prepared plan, approves managed tasks or
publishes anything. All source/profile inputs must come from trusted adapters.
Producer-side effect checks, trusted human attribution through preview/runner/
worker, explicit revocation, aggregate execution usage and configuration activation
are still pending. Existing approvals, PR #31's admission guard and manual defaults
are unchanged. The opt-in exact-task bridge below begins consent-to-decision
integration; complete runtime binding and single-consent PM publication remain
pending.

Validation on the rebased `f7918b7` baseline (2026-10-12): focused consent/policy
checks passed, then the full suite passed twice with 1,477 tests and two skips,
Ruff passed and mypy passed for 55 source files. The mock copied-fixture report at
`/tmp/aitobuild-consent-foundation-simulation-report.json` records `succeeded=true`,
accepted execution, generated artifact and integer command exit 0; no persistent
session started or remained active. No dependencies, live model/GitHub writes,
runtime activation, merge or approval-model defaults changed.

#### Exact Task Admission Progress

On the merged #40 baseline, `ConsentAdmissionEvidence` binds the exact accepted
human record to a direct-human task-admission projection. `DeveloperPreviewRegistry`
stores it beside the exact pinned approval decision, validates it on reload and
preserves it through dispatch. Existing approvals cannot acquire a new consent
retroactively; legacy approvals without consent evidence retain their behavior.

The explicitly constructed `ConsentTaskAdmission` adapter (`consent_admission.py`)
supports only one frozen `task_deliver` repository-issue preview. Its trusted
readers must supply the current effective profile and authoritative source
snapshot. It checks repository identity, base, bundle and preview-operation
digests, the exact presented item and task budget ceilings. Presentation keys are
aliases, not normalized native task IDs. It reserves the exact projection durably
and rechecks live authority read-only. It never approves a preview itself: it
hands the evaluated preview and a `Consent` evidence variant to the managed
service, which admits it through the single route (§2.4). The consent journal is
the evidence store only; the service's live `ApprovalPolicy` is the authority.
The service refuses review, correction, planning and non-repository tasks for
consent, resolves the decision with `approval_policy.resolve(TASK_ADMISSION,
source="human", ...)` against the live policy and requires it to equal the
recorded projection decision, so a changed policy refuses rather than adopts the
consent. Publication-only/PM consents cannot use this adapter to admit delivery.
Independent local owners share the original one-shot reservation; no new ledger
or execution budget is created by this adapter.

`reconcile` inspects a matching landed preview approval without writing, dispatching
or consuming authority, even after expiry/source loss. A saved projection without
an approved preview is not a successful admission and reconciliation cannot fill
that gap. Missing journals, drift, corrupt evidence and uncertain save responses
retain their fail-closed boundaries.

This is library-level task admission, not an activated Discussions or delivery
profile. Source authentication/intent interpretation, service/runner/worker grant
binding, PM prepared-plan promotion and creation projections, descendant admission,
queue-time revocation and aggregate execution usage remain pending. Do not activate
consent-bound delivery until the required effect and runtime bindings exist. The
adapter does not turn registered ordinary callbacks into consent-aware callbacks.

Validation: 81 consent checks passed, including restart/dispatch, journal-before-
approval ordering, shared-store contention, scope/target/base/budget/policy refusals,
publication-only refusal, post-save drift, corruption, expiry and lost-response
metadata recovery. Ruff and mypy passed. On the rebased `f7918b7` baseline, which includes the merged
#36 cancellation fix, 97 consent tests and the full suite (1,477 passes, two
skips) passed in two consecutive runs. The mock copied-fixture
report at `/tmp/aitobuild-consent-admission-simulation-report.json` records accepted
execution, a generated artifact, integer command exit 0 and `succeeded=true`;
session mode was off and `active_session_id=null`. No dependencies, live external
writes, runtime activation, commits or pushes were added in this slice.

#### Implementation Slices and Exit Criteria

1. **Authority foundation:** PR #31's guard is now the implementation baseline.
    Start with strict consent/profile/projection records and durable immutable
    evidence, then add trusted human attribution through preview, runner and worker.
    Prove missing/duplicate/spoofed evidence refuses and existing manual flows stay
    unchanged. Account for the separately accepted #36/#38 safeguards and combined
    #39/#37 admission foundation described above; do not duplicate their pending
    implementations. This foundation alone does not activate new approvals or delivery.
2. **Single-consent PM publication:** bounded read-only preparation, stable-marker
    rendering, exact promotion, human admission/creation projections and deterministic
    link policy. One user response creates/links the advertised issues; no second
    model planning run or human prompt. Publication-only children stay unapproved
    even with globally automatic admission.
3. **Bounded task/plan delivery:** grant-aware child admission, aggregate usage and
    registered automatic delivery/review/correction adapters. One advertised consent
    reaches a verified, reviewed draft for eligible independent tasks; each internal
    decision has exact evidence and separate task budgets. Reuse accepted parent-
    based review eligibility and implement configurable correction/re-review lineage
    admission plus trusted correction-seed provisioning in this slice. Prove an
    opted-in automatic cycle needs no additional human action within scope/round
    limits, while human/disabled profiles retain their gates. Stop at human merge.
4. **Approved amendments and blockers:** implement section 4.3 item revisions and
    issue-thread intake against this consent model. One change-set approval edits
    existing items; replacement tasks require an explicit included grant. Test
    stale-preview supersession, concurrent human edits and active-scope refusal.
5. **Remove operational handoffs:** trusted dependency seed provisioning,
    merge-readiness scheduling and bounded status notifications. Correction seed
    provisioning belongs to the automatic correction profile in slice 3, not a
    later manual workaround. Only then advertise
    automatic dependent delivery. Repeated blocker detection/meetings, proactive
    maintenance and hosted/distributed operation remain distinct later slices.

Focused acceptance must include PR #31's human-plan positives/negatives plus:
`plan_publish` versus `plan_deliver`; delivery without a grant; direct human versus
policy-derived decisions; changed prepared content/markers; invalid promotion
attachments; duplicate creation projection; fabricated number/head mappings;
unlisted children or escalated paths/stages; aggregate-cap exhaustion; queue-time
revocation; source/policy drift; rejected/expired consent; interrupted promotion;
partial metadata saves; uncertain writes; correction ceilings; and unchanged
historical approvals, remote artifacts and original ledgers after refusal/restart.
Correction/re-review acceptance additionally requires a receipt-backed native
automatic positive across saved waiting/restart boundaries, explicit human-mode
waiting and disabled-mode refusal, missing-grant/seed/adapter rejection before
presentation, and no extra user approval or budget reset for a permitted round.

Use native mocked transports and disposable fixtures for each slice, then the
repository pytest/Ruff/mypy gates and mock copied-fixture simulation. Any live
acceptance needs a fresh bounded disposable-target authorization and must report
actual artifact correctness, verification, effect receipts and cleanup. Neither
green PR CI nor this plan certifies hosted automation or a complete autonomous
issue-to-PR service. Current-state docs change only when a capability is implemented.

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

## 7. Implementation Snapshot (2026-10-09)

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
automatic correction loops were not introduced in that slice (same-PR update follows below). Gates: **684 passed/two optional
Docker skips**, Ruff/mypy (46 sources), diagnostics, role/admission/service constructor
compatibility and successful mock fixture simulation. Twenty new parameterized
cases include actual native Architect mocked transport and request/detached/HTTP
handoff/recovery. No dependency/live model/GitHub/Docker/hosted CI/push/remote write.
This handoff continuation was committed locally at `7d7ea13`; native correction artifact acceptance, managed
PM planning/writes, bounded meetings and live review remain pending.

Consolidated correction lifecycle (2026-10-08, PR #11 head `8bd1015`, locally
imported atop `38c721d` without a commit or remote merge):
each verified correction is its own immutable delivery record holding one round; the original
is never rewritten. Operator-only `stage-publication`/`publish`/`retire` endpoints pin one exact
snapshot (diff, PR, branch/base, parent head, chain, triggering review receipt) behind a one-use,
nonce-bound approval stored as frozen validated records with checked transitions. Publication
fast-forwards the pinned draft head (`force=false`, never a new PR or base change) and stages the
next Architect review. Offers and publishes come only from the chain tip; the chain walk fails
closed; superseded preview IDs are refused with the tip. The adapter checks the original budget before every GitHub write (ordinary publish too),
re-reads target and head right before the ref update and the live ref after it; reconciliation is
read-only (sole parent, message, exact tree diff), so a landed push settles even after expiry. Failures past consume settle by the live
head: ours reconciles to published, the unchanged parent releases the head (`not_applied`), a
moved head fails and keeps holding it, an unreadable head stays resumable. A resume reconciles
from the snapshot's pinned blob SHAs/modes before any budget check; only an untouched parent
needs a valid budget to push again (else `not_applied`). Retire releases a
failed/abandoned correction only while the live head equals its parent; re-offering after a
release creates a new attempt with a new task ID. A landed push on a PR closed or marked ready
before reconciliation stays publishing for operator inspection. Gates: **805 passed/two
optional Docker skips**, Ruff/mypy (47 sources). No live GitHub trial, hosted CI or automatic
correction loop yet.
moved head fails and keeps holding it, an unreadable head stays resumable. Retire releases a
failed/abandoned correction only while the live head equals its parent.
Re-offering a released/retired correction starts a linked fresh attempt with a new task/preview
and fresh approvals, without rewriting predecessor receipts, approvals or budgets. Holding
attempts and partial replacement staging remain idempotent; attempt gaps, wrong predecessors
and target drift fail closed. Attempts do not advance or reset the pinned correction-round limit.
Unparseable delivery files block correction checks; a landed push on a closed/ready PR remains
publishing for operator inspection, without reopening it.

Configured native Developer graphs reuse implementation/independent verification
and end verified, without publication. Operator staging and exact digest approval
are required separately; task/tool/COMMENT approvals cannot substitute. The earlier
direct `delivery_publish_correction` operation and binding callback are removed.
Exact tree/parent/message reconciliation handles uncertain publishing under the
saved consuming approval/operator. Original-budget checks precede every upload/ref
update, including reconciliation object writes; live target/head are rechecked
immediately before PATCH. Private checkout branches remain separate.
An opt-in service follow-up binding stages unapproved correction/re-review tasks,
never actor selection, task/COMMENT approval or execution. Root identity, round and
operator ceiling (0-32, default 1) are pinned in approved review scope/journals;
exhaustion records human escalation and changed bindings cannot reset the cycle.
Saved errors/next-task metadata are visible in status, with ID-only authenticated
staging retries that cannot replay native work/publication or reset ledgers.
Frozen terminal status remains visible after live-head drift; active admission
still refuses stale targets. Every preparation needs an already-provisioned seed.
Combined gates: **798 passed/two optional Docker skips**, Ruff/mypy (47 sources), diagnostics,
caller compatibility and mock simulation. Thirty-one new cases include native SDK
mocked transport with real scoped file edits/disposable-file assertions, request
and detached execution, rejection/recovered errors, verification failure, same-PR
recovery, mocked CLI non-force/expiry/race checks and immutable round ceilings.
Additional tests cover verify-only completion with no approval/write, managed
publication bypass refusal, exact operator publication and visible saved staging
errors, last-moment expiry, pre/post-PATCH races and service recovery without replay.
Six added request/detached cases cover released/retired replacements, interrupted staging,
attempt-chain tampering and immutable predecessor budgets/approvals. Existing hosted baseline
checks passed for upstream `8bd1015`; local-only budget callbacks and pre/post-PATCH guards are
still absent there. Seven isolated upstream probes expose those gaps, reported in a
[PR comment](https://github.com/uhvogala/aitobuild/pull/11#issuecomment-6055261194).
No live model/Docker/contribution-publication trial, new hosted run, dependencies, commit or push;
local evidence is not semantic/live acceptance. PM planning/writes and executed
meetings remain separate milestones.

Historical lifecycle/sync/consolidation gates were 715/733/792 passing tests. The consolidation
retains draft-ref validation, review `commit_id` receipts and removal of the stale
Architect-approve setting. Open [issue #9](https://github.com/uhvogala/aitobuild/issues/9)
requires immediate reconciliation of uncertain PR-create responses; [issue #10](https://github.com/uhvogala/aitobuild/issues/10)
tracks shared atomic-write, symlink and permission hardening. Those fixes remain
pending. Address them before fresh approved live/hosted acceptance; PM planning/writes
and bounded executed meetings remain separate milestones.

### Current Limits

- Configured factories/native admission/assignment/runner libraries have opt-in app integration through operator-owned service/worker factories; the default server still uses legacy handles and metadata dispatch. Service-only activation awaits execution in the request; optional workers durably enqueue with lifespan-owned execution and explicit bounded unowned-task startup discovery. Explicit repository/revision/event and actor/binding registries cannot be selected by HTTP callers. Internal shared-token identity is not multi-user authentication. Journal capacity does not govern unmanaged HTTP runs. Managed operation graphs pin active revisions; direct agent nodes remain denied. Scoped Developer delivery, PM proposals and Architect review operations are explicit libraries, not automatically activated orchestration. Trusted read-only coordinator callbacks are original-deadline bounded, not arbitrary-code isolation. Run/assignment/admission completion is metadata, not verification/publication approval; native input is distinct from service approval. Only saved waiting checkpoints resume; interrupted running effects cannot be blindly replayed. Busy controls return 409; mandatory cleanup/thread draining and original budgets remain enforced. Cross-process cancellation is polling-based; failed worker slots require inspection/restart. Local locks/state are not distributed execution or hostile-tenant certification. Live delivery/hosted CI acceptance remains pending.
- `developer.async.webhook` returns task metadata by default. Detached consumption exists only for explicitly service/worker-factory-activated approved repository routes, not default startup.
- Supported repository issue events produce target-specific bundles and require approval-time base SHA pinning. Checkout preparation validates membership in the explicitly configured local seed/base branch, not live GitHub repository identity or remote freshness. No automatic remote lookup/fetch occurs. Legacy fixture payloads without repository context retain prototype routing; unsupported repository webhook events and missing issue criteria are rejected.
- GitHub branch/commit/draft-PR publication is implemented behind the optional allowlisted `gh_cli` adapter; the reproduced snapshot/recovery gaps are fixed locally with regressions at `88720aa`. Supervised local live publication is accepted, not hosted/distributed delivery. The operator installed checksum-verified `gh` 2.102.0 locally and verified `uhvogala` on github.com; this is not a default image dependency.
- General PM draft/write-approval stores remain in memory; the separate opt-in managed path has durable plans, exact creation/resolved-link approvals and per-effect receipts. Twelve supervised local native/GitHub cases accept its bounded live planning/publication path, not broad semantics or hosted delivery. Native PM coordination stays read-only. Planned Developer handoffs remain unapproved until separate task approval. Incomplete/uncertain batches require inspection/read-only reconciliation, not write replay; GitHub list visibility can temporarily lag. Opt-in review/correction follow-ups retain separate task, exact COMMENT and one-use publication approvals. The correction/re-review sequence has live operator-recovery evidence; bounded executed meetings have supervised local live acceptance, not broader semantic or hosted/distributed certification. Regular UTF-8 review files are supported up to 1 MiB; binary/link/submodule inputs need another approved adapter, not a bypass.
- Explicit managed blocker meetings execute native bounded discussion with durable evidence and exact-approved unchanged-scope continuation. Supervised local live meeting-to-reviewed-draft execution and rejection/scope-change stops are accepted. Legacy bootstrap only constructs workflows; automatic blocker detection/repeated meetings and hosted/distributed acceptance remain pending. Proactive scans interpret supplied metadata rather than inspecting a repository.
- Scheduler ticks require an external caller; legacy meeting/scheduler state and non-repository trigger dedupe remain in memory. Managed meeting receipts, previews, approvals and repository issue task/delivery identities persist locally. `dispatched` records metadata routing, not worker completion; inspect the saved queue after restart. Distributed coordination and full lifecycle auditing remain pending.
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