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

All approval stages stay `human`; the harness refuses to start otherwise. Any failure is recorded,
not retried: re-run from a fresh issue instead of replaying writes or resetting budgets.

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
	[--repository uhvogala/aitobuild_example] [--model grok-4.6] [--work-root /workspace/trials]
```

`--work-root` must be outside the service checkout. Each run creates:

- `<work-root>/runs/trial3-<stamp>/seed`: private clone of the target used as the trusted seed.
- `<work-root>/runs/trial3-<stamp>/service`: throwaway clone of the service checkout used as the
  process working directory (the app treats cwd as the service root it must exclude).
- `<work-root>/runs/trial3-<stamp>/state`: service state (previews, ledgers, deliveries, sessions).
- `<work-root>/artifacts/trial3-<stamp>/report.json` and `events.jsonl`: approvals (stage, request,
  timestamp, digest of the exact approved data), manual steps, stalls, PR/issue links and the result.

Webhook secret and internal token are random per run and never written to disk. Nothing here
merges, closes or force-pushes anything.
