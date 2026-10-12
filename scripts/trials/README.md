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
    [--brief-edit-what "..." --brief-edit-why "..."]
```

`resume` restarts the service on the saved state, refreshes every merge point from GitHub, provisions the merged
base into the local seed (an operator action), picks the next dependent issue in plan order whose prerequisites are
all merged, offers it through `POST /internal/organization/dependencies/offer`, delivers and reviews it and stops at
the next merge point. It refuses to continue while any merge point is still open. Dependent children are staged by
planning but answer 409 until that handoff exists; `start` skips them. If a person edits a PM-written brief before
resuming, pass `--brief-edit-what/--brief-edit-why`; it lands in `report.operator_actions` as `brief_edit`.
Repeat `resume` until the plan is done. Runs and artifacts live under `--work-root` (outside the checkout,
git-ignored); delete `runs/trial4-*` once the pilot is finished.

`resume` refuses a seed with uncommitted changes, a seed without the base branch checked out and a seed whose base
branch diverged from origin (git's error is included). Any failure, including one before the service starts, is
recorded in `report.json` as `result: failed` with the error and an end time.
