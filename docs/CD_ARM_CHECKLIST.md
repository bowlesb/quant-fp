# CD Arm Checklist — from "built" to LIVE, with exact commands

The CI/CD pipeline (`ops/ci_watcher.py`, `ops/ci_deploy.py`, `ops/ci_scope.py`; design in
`docs/CONTINUOUS_DEPLOY.md`) is built, merged, and unit-tested. This is the **arming runbook**: the exact,
ordered commands to take it live, each phase gated on a dry-run proof. **The Lead clicks the final live arm;
everything before it is dry-run / grade-only and safe.**

Everything runs from `/home/ben/quant-fp` (the live tree). The watchers never edit the live `quantlib` tree,
never touch `fc`/strategies/crypto on the AUTO path, run CI containers env-scrubbed (no `.env`) and CPU-capped
(`--cpus 6`), and fail closed on uncertain scope.

## Pre-arm sanity (read-only, run anytime)

```bash
cd /home/ben/quant-fp
git merge-base --is-ancestor HEAD origin/main && echo "tree on main"   # arming from main, not a stale tree
docker images | grep -q fp-dev && echo "fp-dev image present"           # the test env
pgrep -af "ci_watcher|ci_deploy" | grep -v pgrep || echo "no daemon running yet"
gh auth status >/dev/null && echo "gh authed"                           # status/label/merge calls
```

## Phase 1 — CI gate, GRADE-ONLY (no merge, no deploy)

The safe first step: grade every open PR and post `ci/fp-suite` status + sticky comment + `tier-2-gated`
label. It NEVER merges or deploys in this mode.

**1a. One-shot proof on a single real PR** (pick an open PR number `N`):

```bash
cd /home/ben/quant-fp
python -m ops.ci_watcher --once --pr N --no-auto-merge
# Verify on GitHub: the PR has a ci/fp-suite commit status (success|failure) + a sticky CI comment listing
# the fp/dashboard/store/timing job verdicts, and a tier-2-gated label iff it touches fc/strategy/fingerprint.
```

**1b. One-shot over the whole open queue:**

```bash
python -m ops.ci_watcher --once --no-auto-merge
```

**1c. Install the supervised GRADE-ONLY daemon** (restart-loop; logs to `~/.quant-ops/ci_watcher.log`):

```bash
nohup /home/ben/quant-fp/ops/ci_watcher.sh grade >> ~/.quant-ops/ci_watcher.boot.log 2>&1 &
tail -f ~/.quant-ops/ci_watcher.log     # watch it grade new/updated PRs every 60s
```

The `grade` role runs `ci_watcher --poll 60 --no-auto-merge` — Phase-1 only. Durable form: a `@reboot` cron
line or a systemd unit (`Restart=always`, `WorkingDirectory=/home/ben/quant-fp`).

**Phase-1 done when:** the daemon is running and has correctly graded ≥2 real PRs (one green, one red or
TIER-2) with status+comment+label visible on GitHub.

## Phase 2 — AUTO-MERGE TIER-1 (Lead-gated arm)

Only after watching Phase-1 grade cleanly for a while. Auto-merge fires ONLY for green **TIER-1**
(fp-neutral + safe-surface) PRs without a `no-auto` label; TIER-2 is never auto-merged.

**2a. Dry-run proof (no merge):** confirm a green docs/ops PR classifies TIER-1 and that the ONLY thing
gating its merge is the `--no-auto-merge` flag (the grade-only daemon already shows `tier=tier-1-auto`,
`passed=True`, `not auto-merged ... auto_enabled=False` in the log for such a PR).

**2b. LEAD ARMS:** switch the daemon from `grade` to `ci` (enables auto-merge):

```bash
pkill -f "ci_watcher.sh grade"; pkill -f "ci_watcher.py --poll"     # stop grade-only
nohup /home/ben/quant-fp/ops/ci_watcher.sh ci >> ~/.quant-ops/ci_watcher.boot.log 2>&1 &
```

To hold any individual PR back even under auto-merge: add the `no-auto` label.

## Phase 3 — AUTO-DEPLOY safe container (Lead-gated arm)

On a new merge to main, FF the live tree + restart ONLY the mapped TIER-1 container
(`docker compose up -d --no-deps <svc>`). `fc`/strategy/crypto can never be a deploy target (gated upstream +
`FORBIDDEN_SERVICES`). `fc` is relaunched ONLY via `ops/nightly_relaunch.sh`.

**3a. Dry-run proof (restarts NOTHING):**

```bash
cd /home/ben/quant-fp
# Point the deploy state one merge back so it has a delta to classify, then dry-run:
git rev-parse origin/main~1 > /tmp/ci_deploy_state_demo
CI_LIVE_TREE=/home/ben/quant-fp python -m ops.ci_deploy --once --dry-run --state-file /tmp/ci_deploy_state_demo
# Expect: a dashboard-only merge → "AUTO-DEPLOY ... TIER-1 container 'dashboard'" + the exact
#         `docker compose up -d --no-deps dashboard` it WOULD run; a fc/strategy/feature merge → TIER-2 SKIP.
rm -f /tmp/ci_deploy_state_demo
```

**3b. LEAD ARMS:** install the deploy daemon (uses `~/.quant-ops/ci_deploy_state`, first run sets a baseline
and deploys nothing):

```bash
nohup /home/ben/quant-fp/ops/ci_watcher.sh deploy >> ~/.quant-ops/ci_deploy.boot.log 2>&1 &
tail -f ~/.quant-ops/ci_deploy.log
```

## What stays GATED forever (never auto)

- **fc / fingerprint / strategies / crypto-capture** — TIER-2, the Lead's controlled market-closed relaunch
  window; `fc` only via `ops/nightly_relaunch.sh`.
- **WDPC live hot-swap** into the running `fc` (the per-group applier) — its own rehearsal + wiring, tracked
  in `READINESS.md`, separate from this gate.

## Disarm / stop everything

```bash
pkill -f "ci_watcher.sh"; pkill -f "ci_watcher.py --poll"; pkill -f "ci_deploy.py --poll"
pgrep -af "ci_watcher|ci_deploy" | grep -v pgrep || echo "all CD daemons stopped"
```

## Hard rules (enforced in code, restated for the operator)

- Never `docker restart`/`start` fc — relaunch only via `ops/nightly_relaunch.sh`.
- Never `docker kill --filter ancestor=fp-dev` (kills fc + every sandbox).
- Arm from `origin/main`; never push to a merged PR's branch (commits strand).
- CI containers never mount `.env`; secrets never reach a CI log.
