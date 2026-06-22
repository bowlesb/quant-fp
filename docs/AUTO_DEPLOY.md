# Continuous Auto-Deploy — keep every docker in sync with merged main

The systemic fix for the **"merged ≠ live"** gap (the live fc was 31 commits behind main). This is CD Phase-3
done properly for **all** services: when code merges to `main`, the affected containers re-deploy — safely,
through a queue, in batches. Companion to `docs/CONTINUOUS_DEPLOY.md` (the merge gate / grade daemon) and
`docs/CD_ARM_CHECKLIST.md` (the arm runbook).

Ben's words: *"dockers pick up new code upon merge in a way that doesn't break anything, have a queue, deploy
in batches."* This doc + the `ops/deploy_scope.py` / `ops/deploy_queue.py` / `ops/auto_deploy.py` machinery
deliver exactly that.

## The two deploy tiers (the safety boundary)

`ops/deploy_scope.py` maps a merge's changed paths to **affected services**, each classified fail-closed:

- **TIER-1 (auto)** — a self-contained container that can be rebuilt + recreated **by name** WITHOUT touching
  the live feature-computer or moving the bus fingerprint: the **dashboard**, the **store-grid worker**, the
  **news/edgar capture** services, the individual **trading strategies**. These deploy **immediately** on
  merge via an isolated image build (`docker compose build --build-arg GIT_SHA=<sha> <svc>`, so the image is
  stamped with its source commit) + `docker compose up -d --no-deps <svc>` (the proven #368/#382 pattern).
  The fc keeps running its pinned bind-mount tree, untouched.
- **TIER-2 (coordinated)** — anything that changes the **feature-compute surface** or the **bus fingerprint**:
  `quantlib/features/**`, `quantlib/bus/**`, `rust/**`, `services/fc|ingestor|executor/**`. These are **never**
  hot-deployed mid-session. They **batch** onto the next coordinated market-closed relaunch
  (`ops/nightly_relaunch.sh`), behind the existing fingerprint-deploy discipline + **Ben's gate**.

An unknown container-bearing path (a `services/<new>/` with no mapped service) **escalates** — it is logged as
needing a service mapping, never silently skipped.

### Path → service → tier (the live map, `deploy_scope.PATH_SERVICE_MAP` + `SERVICE_REGISTRY`)

| Changed path | Service (container) | Tier | Compose file |
|---|---|---|---|
| `services/dashboard/**`, `frontend/**` | `dashboard` | TIER-1 auto | `docker-compose.yml` |
| `services/store_grid_worker/**` | `store-grid-worker` | TIER-1 auto | `docker-compose.yml` |
| `services/news_capture/**` | `news-capture` | TIER-1 auto | `docker-compose.news.yml` |
| `services/edgar/**` | `quant-edgar` | TIER-1 auto | `docker-compose.yml` |
| `services/strategies/{smoke,reversion,overnight_beta}/**` | the matching strategy | TIER-1 auto | `docker-compose.strategies.yml` |
| `services/crypto_strategy/**` | `crypto-momentum-strategy` | TIER-1 auto | `docker-compose.crypto-strategy.yml` |
| `quantlib/features/**`, `quantlib/bus/**`, `rust/**`, `services/fc\|ingestor\|executor/**` | `feature-computer` | **TIER-2 coordinated** | (relaunch only) |
| `ops/ci_*.py`, `ops/deploy_*.py`, `ops/auto_deploy.py` | grade daemon's CI checkout | (self-refresh) | n/a — the guard `reset --hard`s `/home/ben/.ci-repo` each cycle |
| `docs/**`, `tests/**`, other ops | — | ignored | nothing running changed |

## The queue + batching (`ops/deploy_queue.py`)

A **serialized, file-backed** queue (`~/.quant-ops/deploy_queue/pending.jsonl`, an exclusive `fcntl.flock`
serializing every mutation — no third-party dep). It decouples the producer ("a merge happened, these services
are stale") from the single consumer (the applier, "deploy one at a time"):

- **Idempotent** on `(service, sha)` — re-observing a merge never double-enqueues.
- **Coalesce per service to the newest SHA** — a burst of 5 dashboard merges = **one** dashboard rebuild at
  the latest SHA, not 5.
- **Batch window** (`CI_DEPLOY_BATCH_WINDOW_S`, default 120s) — entries younger than the window are held so a
  still-arriving burst coalesces before deploy.
- **TIER-2 entries are kept on the queue** awaiting the relaunch; the relaunch path calls `drain_coordinated()`
  after it FFs + relaunches fc, to clear the fc/fp deploys it just satisfied.
- **Survives a daemon restart** (file-backed) and is human-inspectable.

## The daemon (`ops/auto_deploy.py`) — extends the grade daemon

Per poll: **observe** a new `origin/main` SHA → **map** its changed paths → **enqueue** one entry per affected
service → **apply** the ripe auto-batch (FF the live tree once, then `compose build --build-arg GIT_SHA=<sha>
<svc>` + `compose up -d --no-deps <svc>` each TIER-1 service — the SHA computed from the FF'd HEAD so the
image is stamped with exactly the code shipped) → **report** the deferred TIER-2 batch. It is the Phase-3 sibling of the grade daemon
(Phase-1/2): grade → (Ben-armed) merge → **enqueue deploy → applier deploys per these batching/safety rules**.

### Safety (fail-closed, enforced in `auto_deploy.py`)

- **GOLDEN RULE — fc is NEVER deployed here.** `feature-computer` is TIER-2 by `deploy_scope` AND in a
  belt-and-suspenders `_FORBIDDEN` set; it is relaunched ONLY by `ops/nightly_relaunch.sh` at the coordinated
  window. A fc/fingerprint merge just sits batched until then (Ben-gated).
- **Never `docker kill`/`restart`**; only `compose build --build-arg GIT_SHA` + `compose up -d --no-deps
  <safe-svc>`. **Never** `docker kill --filter ancestor=fp-dev`.
- The live tree is FF'd to `origin/main` ONLY for a TIER-1 deploy (which carries no fc rebuild — fc keeps its
  pinned bind-mount until the relaunch).
- **Box-load pre-check** (`CI_DEPLOY_MAX_LOAD`, default 40): a deploy build is deferred (re-enqueued) if the
  1-min load is already high, so a rebuild can't starve live capture (the crypto-canary contention guard).
- **Rollback per service**: a failed `compose up` leaves the prior container running (compose recreates
  atomically); the failed entry is re-enqueued for the next tick. A bad image is `docker compose up -d
  --no-deps <svc>` against the prior image tag (manual, logged in `_record_deploy`).
- **Dry-run by default** until armed: `--dry-run` classifies + prints the exact plan and deploys nothing.

## Per-service deploy commands (what the applier runs)

`deploy_scope.deploy_commands(service, git_sha)` returns the exact ordered argv steps (build-with-SHA, then
up). The applier FFs the live tree first (`git pull --ff-only origin main` in `/home/ben/quant-fp`), computes
`git_sha` from the FF'd HEAD, then runs the steps with `cwd` = the real repo dir so `.env`/`DB_PASSWORD` load.
The build step bakes `GIT_SHA` into the image (`ARG GIT_SHA` → `ENV GIT_SHA` in each Dockerfile) so the
deployed-sha verification surface never reads `unknown`; the up step has NO `--build` (a second `--build` would
rebuild without the arg and overwrite the image with `GIT_SHA=unknown`). Examples (`<sha>` = the FF'd HEAD):

```bash
# dashboard (+ frontend) — TIER-1, the proven #368/#382 pattern
docker compose build --build-arg GIT_SHA=<sha> dashboard && docker compose up -d --no-deps dashboard

# a strategy — TIER-1, with its compose overlay
docker compose -f docker-compose.yml -f docker-compose.strategies.yml build --build-arg GIT_SHA=<sha> reversion-strategy
docker compose -f docker-compose.yml -f docker-compose.strategies.yml up -d --no-deps reversion-strategy

# news capture — TIER-1
docker compose -f docker-compose.yml -f docker-compose.news.yml build --build-arg GIT_SHA=<sha> news-capture
docker compose -f docker-compose.yml -f docker-compose.news.yml up -d --no-deps news-capture

# feature-computer — TIER-2: NEVER here. Only:
ops/nightly_relaunch.sh    # the coordinated, Ben-gated, market-closed relaunch
```

## Arming (Ben's clicks)

Everything below is **dry-run / off** until armed. The grade daemon (Phase-1) is already live; this is Phase-3.

1. **Watch it (dry-run):** confirm the daemon classifies merges correctly without deploying anything.
   ```bash
   python -m ops.auto_deploy --once --dry-run    # one tick: print the plan for the latest merge
   ```
2. **ARM the auto-deploy daemon** (TIER-1 auto-redeploy on merge; fc still batched + Ben-gated). Run it
   supervised via the existing guard pattern (add a 5-min guard cron, mirroring the grade daemon), or directly:
   ```bash
   # dry-run daemon first (prints plans, deploys nothing):
   CI_AUTO_DEPLOY_DRY_RUN=1 python -m ops.auto_deploy --poll 60 --dry-run
   # ARM real TIER-1 auto-deploy:
   python -m ops.auto_deploy --poll 60
   ```
   Recommended first armed target: a dashboard-only merge — watch it auto-redeploy + verify on :8088.
3. **TIER-2 / fc** stays Ben-gated forever — the batched fc/fingerprint deploys are applied by the coordinated
   `ops/nightly_relaunch.sh` at a market-closed window, which then `drain_coordinated()`s the queue.

## What this is NOT
- Not a fc hot-swap (that's WDPC, separate). fc only moves at the coordinated relaunch.
- Not auto-merge (that's Phase-2, the grade daemon, separately Ben-armed). This deploys what is ALREADY on
  main.
