# Live trial harnesses

Operator-run scripts for supervised live trials against the disposable target repository
`uhvogala/aitobuild_example`. They are not part of the test suite and never run in CI.

## `correction_cycle.py` (trial 3: uninterrupted supervised correction cycle)

One service run drives the whole cycle through the authenticated ASGI app
(`create_app(managed_service_factory=..., managed_worker_factory=...)` served by
`fastapi.testclient.TestClient`, with lifespan so the detached worker runs):

1. Creates a fresh issue on the target whose task invites a seeded defect (`clamp` without
   inverted-bounds validation), and delivers it as a signed `issues/opened` webhook.
2. Operator approves the task (`POST /internal/developer/preview/approve`, pinned base SHA).
3. The detached worker prepares, runs the native Developer, verifies in a fresh constrained
   container and stops at draft publication. Each Developer tool request (`developer_tools`) and
   the exact draft snapshot (`draft_publication`) is approved via `POST /internal/organization/tasks/approve`.
4. `automatic_review_followups=True` stages the head-pinned Architect review; the operator approves the
   task, then the exact COMMENT (`review_publication`).
5. The completed COMMENT with a correction proposal stages the correction task automatically.
6. **Planned manual step:** the operator fetches the reviewed head/published branch into the private
   local seed (the service never fetches; README "Complete Supervised Correction Cycle").
7. Operator approves the correction task and its tool requests; the run implements and verifies.
8. Operator stages and approves the exact same-PR push (`/internal/organization/corrections/stage-publication`
   then `/publish`, `correction` stage). Publication stages the re-review.
9. Operator approves the re-review task and its exact COMMENT.

**Unreviewed approvals:** in human mode the harness approves EVERY pending human-stage request,
including each Developer tool request (file edits, writes and shell commands), automatically and without a
person looking at it. Each decision is recorded with its stage, timestamp and a digest/preview of the exact
data so it can be audited after the run, but nothing is inspected before approval. Use it only on the
disposable target.

All approval stages stay `human` by default, and the harness refuses to start with any other policy. Any failure is recorded,
not retried: re-run from a fresh issue instead of replaying writes or resetting budgets.

### Automatic mode (trial 1)

`--approval-mode automatic` binds the central policy (`AITOBUILD_APPROVAL_*` overrides read by
`load_config()`, passed unchanged to `ManagedOrganizationService`, plus `task_base_revision_for` with an
operator-pinned `base_revision` on the seed source). Only `task_admission` and `developer_tools` may be
automatic (`--automatic-stage` narrows the set). Any other stage is refused before anything is created.
`draft_publication`, `review_publication`, `correction` and the rest stay `human`, and `merge` stays `disabled`.
In this mode the harness never approves admission or tool requests. If a policy-automatic stage waits for a
human, that is recorded as a stall and the run stops. The signed webhook does not dispatch an unapproved
preview, so when no worker picked the root task up, the harness sends `POST /internal/organization/tasks/run`
and records it as an operator trigger (it carries no approval). After the run it reads the saved previews and
runs and records whether each decision came from `operator_policy` or `human`. It also records the policy
revision pin and the count against `max_automatic_decisions`.

### Prerequisites

- `uv sync` in the service checkout; run with `.venv/bin/python`.
- `gh` authenticated with write access to the target repository.
- `az login` (Entra credentials for the Azure OpenAI v1 endpoint). Keep `AITOBUILD_FOUNDRY_API_KEY` unset.
- Docker with the `aitobuild-developer:local` image (`.devcontainer/Dockerfile.developer`).
  If the user cannot reach the Docker socket but has passwordless sudo, pass `--sudo-docker`: the harness
  puts a run-local `docker` wrapper (`exec sudo -n /usr/bin/docker "$@"`) first on its own `PATH` only.

### Usage

```bash
.venv/bin/python scripts/trials/correction_cycle.py --sudo-docker \
	[--approval-mode human|automatic] [--allowed-issue-author <login>] [--model grok-4.6] \
	[--work-root /workspace/trials]
.venv/bin/python scripts/trials/followups.py --sudo-docker --allowed-issue-author <login>
```

`--allowed-issue-author` has no default and is required whenever a stage is automatic (the default
`--approval-mode automatic` of `followups.py` and `pilot.py` included): it becomes the repository source's only
`allowed_issue_authors` entry, which automatic task admission requires since #23. It must be exactly one GitHub
login; blanks, wildcards and lists are refused, and the harness refuses to start without it. Use the login that
authors the trial issues (check with `gh issue view <n> --json author`).

The target is locked to `uhvogala/aitobuild_example`. Any other `--repository` needs
`--allow-other-repository`. `--poll-seconds`, `--stage-timeout` and `--invoke-timeout` must be positive.
`--work-root` must be outside the service checkout. Each run creates:

- `<work-root>/runs/trial3-<stamp>/seed`: private clone of the target used as the trusted seed.
- `<work-root>/runs/trial3-<stamp>/service`: throwaway clone of the service checkout used as the
  process working directory (the app treats cwd as the service root it must exclude).
- `<work-root>/runs/trial3-<stamp>/state`: service state (previews, ledgers, deliveries, sessions).
- `<work-root>/artifacts/trial3-<stamp>/report.json` and `events.jsonl`: approvals (stage, request,
  timestamp, digest of the exact approved data), manual steps, stalls, PR/issue links and the result.

Shared pieces (`Recorder`, `Operator`, environment setup, policy checks, evidence collection) live in
`scripts/trials/common.py` for reuse by later trials.

Cleanup: `runs/<trial>/` holds seeds, service clones, private checkouts and agent sessions (hundreds of MB).
Once the report has been checked, remove it with `rm -rf <work-root>/runs/<trial>`. Keep
`artifacts/<trial>/`. Trial issues and draft PRs on the target are left open for inspection.

Webhook secret and internal token are random per run and never written to disk. Nothing here
merges, closes or force-pushes anything.

## `pilot.py` (trial 4: small M5 pilot with human merge points)

A PM planning request produces a few dependent issues. The pilot delivers them one merge point at a time and
never merges: the service merge stage stays disabled, ReviewBot okays the exact head in Common (recorded in
`report.metrics` as a bot standing in for the human merge review) and a person merges. The same locks as the other
harnesses apply (only `task_admission`/`developer_tools` may be automatic, `--repository` locked, approvals of
human stages are given by the script and recorded).

```bash
# 1. plan, then deliver + review every independent issue; stops with result=paused_at_merge_point
uv run python scripts/trials/pilot.py start --sudo-docker --allowed-issue-author <login>
# 2. after the PR in report.merge_points is merged by a person
uv run python scripts/trials/pilot.py resume --sudo-docker --allowed-issue-author <login> --run-dir /workspace/trials/runs/trial4-<stamp> \
    [--brief-edit-what "..." --brief-edit-why "..."] \
    [--policy-override-actor <person> --policy-override-reason "..."]
```

`resume` restarts the service on the saved state, refreshes every merge point from GitHub, provisions the merged
base into the local seed (an operator action), picks the next dependent issue in plan order whose prerequisites are
all merged, offers it through `POST /internal/organization/dependencies/offer`, delivers and reviews it and stops at
the next merge point. It refuses to continue while any merge point is still open. Dependent children are staged by
planning but answer 409 until that handoff exists; `start` skips them. If a person edits a PM-written brief before
resuming, pass `--brief-edit-what/--brief-edit-why`; it lands in `report.operator_actions` as `brief_edit`.
Repeat `resume` until the plan is done. Runs and artifacts live under `--work-root` (outside the checkout,
git-ignored); delete `runs/trial4-*` once the pilot is finished.

Approval policy pinning (#65, related to #41): the first session pins its approval policy in the report, and every
phase records the policy revision it ran under. A `resume` whose `--automatic-stage` flags give a different policy is
refused (both revisions are named) unless `--policy-override-actor <person> --policy-override-reason "..."` records a
human override in `report.policy_overrides`; the harness then records the service's own override
(`POST /internal/organization/tasks/override_policy`) on each still-open run before anything resumes it.
`report.evidence` judges each phase, and each run decision, by the policy recorded for it, never by the latest
session's policy (pilot trial 4 reported 29 start-phase decisions against the resume policy).

Correction and re-review admission (harness only; Vertti's Oct 12 rule and #39): a correction is admitted
automatically only when the `correction` stage is automatic, and a correction's re-review inherits its eligibility
from the correction, so it is automatic under the same condition and no other. The trial locks keep `correction`
human, so after automatic task admission the service refuses both by design (the worker logs them in
`refused_follow_ups`). `pilot.py` and `followups.py` then admit each one as a **scripted human step**
(`HandsOff.correction_child`): the harness waits the `--pickup-seconds` gate, records that nothing admitted the
task, and admits it through `POST /internal/developer/preview/approve` (`admit_task(Human)`). It never sends the run
trigger or any automatic request for them, so there is no 409 path (pilot trial4-20261019-021602 stopped on that
409 at the re-review). If something admits the task during the gate, the run fails. Each step lands in
`report.scripted_human_admissions` (`kind: scripted_human_admission`, `reason: correction|correction_rereview`,
`auto_admitted: false`, `evidence_kind: human`) and as a `scripted_human_admission` operator action, separate from
`worker_pickups` and run triggers. `report.evidence` expects that human step for every correction re-review and
reports its `admission_path`. It never counts a scripted human step as an automatic admission or as review
inheritance: `evidence.inherited_reviews` lists only reviews the service admitted automatically as
`inherited_review`, and a scripted step on a task expected to be automatic is a problem. If `correction` were
automatic, the harness waits for the worker instead and the evidence expects the re-review as `inherited_review`.

Completed runs only (#83): `report.evidence` is based on the service's recorded run state
(`service.status(preview_id)`), not on the harness's own lists. Every phase (planning, delivery, review, correction,
re-review) is a problem unless its service-recorded run `completed`, `evidence.phases[].run_state` shows that state,
and `evidence.inherited_reviews` lists only reviews whose run completed. A merge point needs a completed review's
follow-up outcome (`review_outcome` `no_correction` or `staged`), and so does a session-level `report.review_outcome`
(`followups.py`, `correction_cycle.py`). `pilot.py` and `followups.py` stop the session as soon as a phase's run ends
in any other state or a completed review has no such outcome, so a failed review (pilot trial4-20261019-071847: model
content-filter 400) never becomes a merge point or `evidence.ok: true`.

Worker liveness (#69): while it drives a phase, the harness checks `GET /internal/organization/worker` whenever the
job is `queued`. If no managed worker loop is alive (or the status has no health signal), the run fails at once with
the reason and the loop errors recorded in `report.stalls`, instead of waiting out `--stage-timeout`.

`resume` refuses a seed with uncommitted changes, a seed without the base branch checked out and a seed whose base
branch diverged from origin (git's error is included). Any failure, including one before the service starts, is
recorded in `report.json` as `result: fail` with the error and an end time.

GitHub transport failures (#72): the service answers a GitHub transport or availability failure (TLS handshake
timeout, EOF, connection reset, gh timeout, gateway 5xx) with `503` and `"transient": true`; `409` stays reserved
for policy refusals. The harness's one HTTP helper (`Operator._request`) retries a `transient` 503 at most
`len(TRANSIENT_RETRY_DELAYS)` times. A status read retries as is. A write retries only when it carries a state
re-read (`landed=`) that shows nothing landed (today: tool and stage approvals in `Operator.drive`); a write without
one, or one whose re-read shows it landed, stops the run. Every 409 stays fatal. Each retry is recorded in
`report.transient_retries` (method, path, attempt, delay, detail) and as a `transient_retry` event. Wrappers that
patch `Operator.post` must pass its keyword arguments (`landed=`) through.
