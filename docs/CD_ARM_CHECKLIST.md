# CD Arm Checklist — from "built" to LIVE, with exact commands

The CI/CD pipeline (`ops/ci_watcher.py`, `ops/ci_deploy.py`, `ops/ci_scope.py`, `ops/ci_watcher.sh`,
`ops/ci_daemon_guard.sh`; design in `docs/CONTINUOUS_DEPLOY.md`) is built, merged, and unit-tested. This is
the **arming runbook**: the exact, ordered commands to take it live, each phase gated on a proof. **The Lead
clicks the final live arm; everything before it is grade-only / dry-run and safe.** Companion: `docs/
SYSTEM_GAPS.md` G1 (the gap this closes), `~/.quant-ops/READINESS.md` (the CI/Auto-merge/Auto-deploy rows).

The grade/ci watcher runs from a **dedicated CI checkout** (`/home/ben/.ci-repo`) that the guard keeps reset
`--hard` to `origin/main` every cycle — NEVER the `/home/ben/quant-fp` fc bind-mount tree (which is PINNED at
a controlled SHA; FF-ing it is the gated fc-relaunch deploy step, and it may not even contain the latest ops
scripts). This decoupling is what makes the daemon (a) actually start — the fc tree can lag and lack
`ci_daemon_guard.sh` — and (b) grade with the CURRENT exclude policy. The deploy watcher alone reads the fc
tree (`CI_LIVE_TREE`), because it performs the real `compose up` there. The watchers never edit the live
`quantlib` tree, never touch `fc`/strategies/crypto on the AUTO path, run CI containers env-scrubbed (no
`.env`) and CPU-capped (`--cpus 6`, `-n 6` workers — never `-n auto` on this shared 32-core box), and fail
closed on uncertain scope.

## The safety boundary (why incremental arming is safe)

`ops/ci_scope.py` classifies every PR/merge **fail-closed** into **TIER-1 (auto)** — eligible for auto-merge +
auto-deploy of ONE safe container, requiring BOTH (a) **fingerprint UNCHANGED** vs origin/main and (b) **every**
changed path on the fp-neutral allowlist (`docs/`, `tests/`, `ops/`, `services/dashboard/`, `frontend/`, …)
with **none** on the danger denylist (`services/fc/`, `services/strategies/`, `quantlib/features/groups/`,
`quantlib/bus/schema`, `rust/`, …) — or **TIER-2 (gated)** otherwise. Auto-deploy can only target containers in
`ci_scope.DEPLOY_TARGETS` (`dashboard` today); fc/strategies/crypto are belt-and-suspenders in
`ci_deploy.FORBIDDEN_SERVICES` AND unreachable by construction. The auto-merge decision is the pure
`_should_auto_merge(passed, tier, auto_merge_enabled, labels)` predicate (unit-tested in
`tests/test_fp_ci_arm.py`).

## The daemon lifecycle = `ops/ci_daemon_guard.sh`

This box has no systemd-user, so the watchers are kept alive by a cheap cron that runs the **respawn guard**
every 5 min. The guard is idempotent (no-op if the supervisor is already up; relaunches it — incl. after a
reboot — otherwise), launches the supervisor detached (`setsid`+`nohup`), and exposes `--status` / `--stop`.
Roles: `grade` (Phase-1, `--no-auto-merge`), `ci` (Phase-2, auto-merge), `deploy` (Phase-3, `--dry-run` until
`CI_DEPLOY_DRY_RUN=0`).

## Pre-arm sanity (read-only, run anytime)

```bash
cd /home/ben/quant-fp
git merge-base --is-ancestor HEAD origin/main && echo "tree on main"   # arming from main, not a stale tree
docker images | grep -q fp-dev && echo "fp-dev image present"           # the test env
ops/ci_daemon_guard.sh --status                                         # per-role liveness (all DOWN = unarmed)
gh auth status >/dev/null && echo "gh authed"                           # status/label/merge calls
```

## Phase 1 — CI gate, GRADE-ONLY (no merge, no deploy)

The safe first step: grade every open PR and post `ci/fp-suite` status + sticky comment + `tier-2-gated`
label. It NEVER merges or deploys in this mode.

**1a. One-shot proof on a single real PR** (pick an open PR number `N`):

```bash
cd /home/ben/quant-fp
python -m ops.ci_watcher --once --pr N --no-auto-merge
# Verify on GitHub: the PR gets a ci/fp-suite commit status (success|failure) + a sticky CI comment listing
# the fp/dashboard/store/timing job verdicts, and a tier-2-gated label iff it touches fc/strategy/fingerprint.
```

**1b. One-shot over the whole open queue:** `python -m ops.ci_watcher --once --no-auto-merge`

**1c. ARM the supervised GRADE-ONLY daemon** (idempotent; installs the 5-min respawn-guard cron):

```bash
ops/install_crons.sh --dry-run     # review: shows the grade-only guard line it will add
ops/install_crons.sh               # install it — PRs now get graded continuously, grade-only
tail -f ~/.quant-ops/ci_watcher.log     # watch it grade new/updated PRs
```

The cron self-bootstraps the dedicated CI repo (`git clone` if `/home/ben/.ci-repo` is absent) then runs THAT
checkout's `ops/ci_daemon_guard.sh grade`, which resets the CI repo to `origin/main` and keeps
`ci_watcher.sh grade` alive (`ci_watcher --poll 60 --no-auto-merge`) running from it. It never `cd`s into the
pinned fc tree. Grade-only is the ONLY thing `install_crons.sh` auto-installs.

**Phase-1 done when:** the daemon is running and has correctly graded ≥2 real PRs (one green, one red or
TIER-2) with status+comment+label visible on GitHub.

## Phase 2 — AUTO-MERGE TIER-1 (Lead-gated arm)

Only after watching Phase-1 grade cleanly. Auto-merge fires ONLY for green **TIER-1** PRs without a `no-auto`
label (the `_should_auto_merge` predicate); TIER-2 / red / grade-only / held → never.

**2a. Dry-run proof (no merge):** the grade-only daemon already logs `tier=tier-1-auto`, `passed=True`,
`not auto-merged ... auto_enabled=False` for a green docs/ops PR — the ONLY thing gating its merge is
grade-only mode.

**2b. LEAD ARMS:** swap the guard from `grade` to `ci` (enables auto-merge). Edit the cron line
`ops/ci_daemon_guard.sh grade` → `ops/ci_daemon_guard.sh ci`, then:

```bash
ops/ci_daemon_guard.sh --stop grade     # stop grade-only supervisor
ops/ci_daemon_guard.sh ci               # start the auto-merge supervisor
```

To hold any individual PR back even under auto-merge: add the `no-auto` label.

## Phase 3 — AUTO-DEPLOY safe container (Lead-gated arm)

On a new merge to main, FF the live tree + restart ONLY the mapped TIER-1 container
(`docker compose up -d --no-deps <svc>`). `fc`/strategy/crypto can never be a target (gated upstream +
`FORBIDDEN_SERVICES`); `fc` is relaunched ONLY via `ops/nightly_relaunch.sh`.

**3a. Dry-run proof (restarts NOTHING):**

```bash
cd /home/ben/quant-fp
git rev-parse origin/main~1 > /tmp/ci_deploy_state_demo     # one merge back, so there's a delta to classify
CI_LIVE_TREE=/home/ben/quant-fp python -m ops.ci_deploy --once --dry-run --state-file /tmp/ci_deploy_state_demo
# Expect: a dashboard-only merge → "AUTO-DEPLOY ... TIER-1 container 'dashboard'" + the exact
#         `docker compose up -d --no-deps dashboard` it WOULD run; a fc/strategy/feature merge → TIER-2 SKIP.
rm -f /tmp/ci_deploy_state_demo
```

**3b. LEAD ARMS:** start the deploy daemon, dry-run first, then real (first real deploy = a dashboard-only
merge, watched):

```bash
CI_DEPLOY_DRY_RUN=1 ops/ci_daemon_guard.sh deploy     # dry-run daemon: prints plans, restarts nothing
CI_DEPLOY_DRY_RUN=0 ops/ci_daemon_guard.sh --stop deploy && CI_DEPLOY_DRY_RUN=0 ops/ci_daemon_guard.sh deploy
tail -f ~/.quant-ops/ci_deploy.log
```

## Phase 4 — WDPC subagent→live-fc per-group HOT-SWAP (rehearse on crypto, never equity)

The real-time feature-fix flow — a subagent fixes its untrusted group, the fix reaches the LIVE fc between
minutes (not next nightly relaunch) via a SAFE per-group hot-swap, scope-guarded to fp-neutral / untrusted /
single-group. The applier rides the `up_to_date()`/`rebuild_from_history` contract (#353/#357). **STATUS:** the
applier + scope-guard are on the `di/wdpc-applier-contract` branch; NO deploy-queue consumer is wired into a
running `fc` yet. **Rehearsal target = `crypto-capture`** (24/7 same-box canary); the live EQUITY fc is never
touched here. Tracked on the READINESS "WDPC continuous-deploy" row; not part of this arm gate.

## PROOFS (re-run this session, against current main)

- **Phase-1 grade e2e:** `ci_watcher --once --pr 364 --no-auto-merge` posted `ci/fp-suite = success`,
  **gate GREEN (fp/dashboard/store all green)**, TIER-1, sticky comment + status on GitHub, and logged
  `not auto-merged (passed=True tier=tier-1-auto auto_enabled=False)` — green+TIER-1, the grade-only gate held.
- **Scope classification** verified on all 4 then-open PRs (live fp `0x873f2fceb8f00c92`): #364/#363/#362
  TIER-1, #361 TIER-2 **fail-closed** on `quantlib/data/quote_breadth_depth_gap.py`.
- **Phase-3 deploy dry-run** on a real dashboard-only commit → fp unchanged, TIER-1, target=`dashboard`,
  printed exactly `docker compose up -d --no-deps dashboard`, restarted nothing, state not advanced.
- **Auto-merge decision matrix** — all 5 cases (green TIER-1 auto-on → merge; grade-only / `no-auto` / TIER-2
  / red → suppress) permanent in `tests/test_fp_ci_arm.py`.
- **Guard lifecycle** live-verified: `ci_daemon_guard.sh grade` launched the supervisor + `ci_watcher --poll`
  child (grade-only), `--status` reported SUPERVISED, `--stop` returned all roles to DOWN with no stray proc.

## What stays GATED forever (never auto)

- **fc / fingerprint / strategies / crypto-capture** — TIER-2, the Lead's controlled market-closed relaunch
  window; `fc` only via `ops/nightly_relaunch.sh`.
- **WDPC live hot-swap** into the running `fc` — its own rehearsal + wiring (Phase 4), tracked in `READINESS.md`.

## Disarm / stop everything

```bash
ops/ci_daemon_guard.sh --stop grade ci deploy
ops/ci_daemon_guard.sh --status     # all DOWN
# To remove the cron: crontab -e and delete the `ci_daemon_guard.sh grade` line.
```

## Hard rules (enforced in code, restated for the operator)

- Never `docker restart`/`start` fc — relaunch only via `ops/nightly_relaunch.sh`.
- Never `docker kill --filter ancestor=fp-dev` (kills fc + every sandbox).
- Arm from `origin/main`; never push to a merged PR's branch (commits strand).
- CI containers never mount `.env`; secrets never reach a CI log.
- `--stop ROLE` group-kills the supervisor; if it was mid `sleep 10; restart` backoff it may spawn one more
  child before dying — re-run `--stop` (or `--status` a few seconds later) to confirm DOWN.

## Biggest blocker to a fully-live arm

Suite wall-clock: even at `-n 6`, the full `tests/` grade is multi-minute per PR (~5.5 min observed) and the
queue grades serially. Correct + bounded (`--cpus 6`, never `-n auto`, 1200 s/suite timeout) — safe as the
Phase-1 daemon now; it just caps auto-merge cadence. Fine for current PR volume; shard the suite / cache the
base-fp worktree if the queue grows.
