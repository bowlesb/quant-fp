# CD ARM CHECKLIST — from "built" to the single live-arm click

This is the operator's checklist for **continuous deployment**: the mechanism by which a subagent's merged fix
reaches production without a human running anything by hand. The machinery (`ops/ci_watcher.py`,
`ops/ci_deploy.py`, `ops/ci_scope.py`, `ops/ci_watcher.sh`, `ops/ci_daemon_guard.sh`) is **built and
unit-tested**. This doc records, per phase: **what it does · the PROOF it works · the EXACT command/flag Ben
flips to arm it live.** Everything is staged so the only remaining action at each gate is a single deliberate
click — nothing arms itself.

Companion docs: `docs/CONTINUOUS_DEPLOY.md` (design), `~/.quant-ops/READINESS.md` (the rows this drives),
`docs/SYSTEM_GAPS.md` G1 (the gap this closes).

## The safety boundary (why this is safe to arm incrementally)

`ops/ci_scope.py` classifies every PR/merge, **fail-closed**, into:
- **TIER-1 (auto)** — eligible for auto-merge + auto-deploy of ONE safe container. Requires BOTH: (a)
  **fingerprint UNCHANGED** vs origin/main (`BusSchema.from_registry().fingerprint` byte-identical), and (b)
  **every** changed path on the fp-neutral safe allowlist (`docs/`, `tests/`, `ops/`, `services/dashboard/`,
  `frontend/`, …) and **none** on the danger denylist (`services/fc/`, `services/strategies/`,
  `quantlib/features/groups/`, `quantlib/bus/schema`, `rust/`, …).
- **TIER-2 (gated)** — anything else, or any ambiguity. Stays for the Lead's controlled relaunch window.

Auto-deploy can ONLY target containers in `ci_scope.DEPLOY_TARGETS` (`dashboard` today). `fc`, `ingestor`,
`executor`, every strategy, `crypto-capture`, `news-capture` are in a belt-and-suspenders
`FORBIDDEN_SERVICES` set in `ci_deploy.py` AND are unreachable by construction (a change touching them is
already TIER-2). The watcher NEVER `docker restart`/`start`s fc and NEVER `docker kill --filter
ancestor=fp-dev`.

---

## Phase 1 — CONTINUOUS GRADING (grade-only, NO merge, NO deploy)

**What it does:** a box-local daemon polls every open PR against `main`; on each new head SHA it runs the full
`tests/` suite in env-scrubbed `fp-dev --rm` containers (bounded `--cpus 6`, `-n 6` workers — never `-n auto`),
classifies scope, and posts a commit **status** (`ci/fp-suite`) + a sticky summary comment + the
`tier-2-gated` label. It **does not merge or deploy** anything (`--no-auto-merge`). This is the piece whose
absence meant "subagents can't self-deploy" — PRs were simply never graded.

**PROOF (this pass, 2026-06-21):**
- `python -m ops.ci_watcher --once --pr 364 --no-auto-merge` posted `ci/fp-suite = pending "fp suite running"`,
  ran the full suite (~5.5 min), and posted the final verdict to PR #364's head SHA `f102ab4`:
  **`ci/fp-suite = success` "gate GREEN (fp=ok, dashboard=ok, store=ok, timing=ok\*) — TIER-1 (auto)"`** plus a
  sticky per-job comment. The watcher logged `#364 not auto-merged (passed=True tier=tier-1-auto
  auto_enabled=False)` — green + TIER-1 but the `--no-auto-merge` Phase-1 gate held, exactly as designed.
- Scope classification verified on all 4 open PRs (live registry fingerprint `0x873f2fceb8f00c92`):
  - #364 / #363 / #362 → **TIER-1 auto** (docs/ops only, fp-neutral).
  - #361 → **TIER-2 gated** — fail-closed on `quantlib/data/quote_breadth_depth_gap.py` (not on the safe
    allowlist). This is the boundary working: a library change cannot auto-merge.
- `ops/ci_daemon_guard.sh --status` reports per-role liveness; `bash -n` clean.

**The daemon (supervised, reboot-surviving):** this box has no systemd-user, so liveness is kept by a cheap
cron that runs the respawn guard every 5 min. `ops/install_crons.sh` installs (idempotently) the **grade-only**
guard line:

```
*/5 * * * * cd /home/ben/quant-fp && ops/ci_daemon_guard.sh ci >> /home/ben/.quant-ops/ci_daemon_guard.log 2>&1
```

The guard launches `ops/ci_watcher.sh ci` with `CI_NO_AUTO_MERGE=1` (its default) → the watcher runs
`--no-auto-merge`. Grade-only is SAFE (only posts statuses), so this is the one piece auto-installed by
`install_crons.sh`.

**ARM COMMAND (Ben):**
```bash
ops/install_crons.sh --dry-run     # review: shows the grade-only guard line it will add
ops/install_crons.sh               # install it; PRs now get graded continuously, grade-only
```
After install, confirm: `tail -f ~/.quant-ops/ci_watcher.log` fills, and any open PR gets an `ci/fp-suite`
status within ~5 min.

---

## Phase 2 — AUTO-MERGE (TIER-1 only)

**What it does:** when the gate is GREEN **and** scope is TIER-1 **and** the PR has no `no-auto` label, the
watcher squash-merges it (`gh pr merge --squash --delete-branch`). TIER-2 PRs are never touched.

**PROOF (this pass):** classification proven (see Phase 1 — #364/#363/#362 would qualify, #361 would not).
The merge call itself is held behind `--no-auto-merge` until armed — proven-eligible, not yet performed.

**ARM COMMAND (Ben):** flip the guard's gate from grade-only to auto-merge by setting the env on the cron line
(or exporting it for a manual supervisor run):
```bash
# Edit the cron line OR run the supervisor with the flag flipped:
CI_NO_AUTO_MERGE=0 ops/ci_daemon_guard.sh --stop ci   # stop grade-only supervisor
CI_NO_AUTO_MERGE=0 ops/ci_daemon_guard.sh ci          # relaunch with auto-merge enabled
```
To arm permanently, prefix the cron line with `CI_NO_AUTO_MERGE=0`. **Recommended first target:** let it
auto-merge ONE docs-only TIER-1 PR, watch it land, then leave it on.

**Hold switch:** put the `no-auto` label on any PR to exempt it from auto-merge even when green+TIER-1.

---

## Phase 3 — AUTO-DEPLOY (one safe container; first target = dashboard)

**What it does:** a second daemon (`ci_deploy.py`) watches `origin/main` for new merges; for a TIER-1 merge
with a single safe `deploy_target` it FFs the live tree and `docker compose up -d --no-deps --build <svc>` for
that one container, verifies it healthy, and appends an audit line to `SYSTEM_LOG.md`. Non-TIER-1 / multi-svc /
no-target merges are SKIPPED (the Lead's window).

**PROOF (this pass):** on a REAL dashboard-frontend-only main commit (`423e3f3`, `App.tsx` + `styles.css`):
fingerprint unchanged (`0x873f2fceb8f00c92`), TIER-1, `deploy_target = 'dashboard'`, and
`deploy_safe_container(dry_run=True)` printed exactly:
```
[dry-run] would: docker compose up -d --no-deps dashboard (in /home/ben/quant-fp)
```
…restarting **nothing**. `python -m ops.ci_deploy --dry-run --once` on first run correctly sets the baseline
SHA and deploys nothing (no history replay).

**ARM COMMAND (Ben):** add the deploy guard cron, kept in DRY-RUN until you trust it, then flip:
```bash
# Dry-run daemon (prints plans, restarts nothing):
CI_DEPLOY_DRY_RUN=1 ops/ci_daemon_guard.sh deploy
# Arm real dashboard auto-deploy (first real deploy = a dashboard-only merge):
CI_DEPLOY_DRY_RUN=0 ops/ci_daemon_guard.sh --stop deploy && CI_DEPLOY_DRY_RUN=0 ops/ci_daemon_guard.sh deploy
```
To arm permanently, add a `*/5` cron line `cd /home/ben/quant-fp && CI_DEPLOY_DRY_RUN=0 ops/ci_daemon_guard.sh
deploy >> ~/.quant-ops/ci_daemon_guard.log 2>&1`. **First real deploy target = a dashboard-only merge**, watched.

---

## Phase 4 — WDPC subagent→live-fc per-group HOT-SWAP (rehearse on crypto, never equity)

**What it does:** the real-time feature-fix flow — a subagent fixes its untrusted feature group, the fix
reaches the LIVE feature-computer between minutes (not next nightly relaunch) via a SAFE per-group hot-swap,
scope-guarded to fp-neutral / untrusted / single-group swaps. The applier rides the `up_to_date()` /
`rebuild_from_history` contract (#353) refactored onto #357.

**STATUS:** the applier + scope-guard + `up_to_date()` contract are on main; the live wiring into a running fc
is NOT done. **Rehearsal target = `crypto-capture`** (24/7 same-box canary, low blast radius) — prove a group
hot-swaps into a running crypto fc between minutes. **The live EQUITY fc is never touched here** (its relaunch
is the Lead's gated `ops/nightly_relaunch.sh` click). See the Phase-4 rehearsal plan section below.

**ARM COMMAND (Ben):** N/A yet — this phase is at REHEARSAL stage; the arm is "wire the proven crypto
rehearsal into the equity fc," a later Lead-gated step. Tracked on the READINESS "WDPC continuous-deploy" row.

---

## Watchdog (so a dead daemon can't hide)

`ops/ci_daemon_guard.sh --status` reports each role's liveness. The 5-min guard cron self-heals a dead
supervisor. Follow-up (small): wire a stale-`ci_watcher.log` check into `ops/healthcheck.sh` so a daemon that
dies AND fails to respawn FAILs the healthcheck loudly during business hours (coordinate with the G9/G10
market-aware-healthcheck + notifier work so it's owned once).

**Known behavior:** `--stop ROLE` group-kills the supervisor; if the supervisor was mid `sleep 10; restart`
backoff it can spawn one more child before dying. Re-run `--stop` (or just `--status` a few seconds later) to
confirm DOWN — the cron guard is the intended lifecycle manager, `--stop` is for manual intervention.

**Live-verified (this pass):** the guard launched `ci_watcher.sh ci` → supervisor pid + `ci_watcher --poll 60`
child with `--no-auto-merge` (grade-only), `--status` reported `SUPERVISED`, and `--stop ci` returned both
roles to DOWN with no stray process. Dogfood: PR #366 (this PR) self-graded through the gate → `ci/fp-suite =
success`, gate GREEN, TIER-1, NOT merged.

## The single biggest blocker to a fully-live arm

**Suite wall-clock.** Even at `-n 6`, the full `tests/` grade is multi-minute per PR; the queue grades
serially. It is correct and bounded (`--cpus 6`, never `-n auto`, 1200 s/suite timeout) — safe to run as the
Phase-1 daemon now — but throughput is the practical ceiling on auto-merge cadence. Acceptable for the current
low PR volume; revisit (shard the suite / cache the base-fp worktree) if the queue grows.
