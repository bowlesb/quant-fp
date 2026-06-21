# Automated CI/CD — event-driven, two-tier, fail-closed

Ben's keystone structural fix: the moment a PR *can* be merged (passes its gates), it merges AND deploys
**automatically** — not on the Lead's 30-minute cadence. This also mechanically kills the recurring
"half-finished / half-tested" class of failure: there was no CI, which is exactly why PR #332 shipped 16 red
tests undetected.

This is built **phased** and **fail-closed**. Phase 1 (the CI gate) is the highest-value piece and ships
first. Phases 2–3 (auto-merge, auto-deploy) layer on top once the gate is trusted.

## The safety boundary (auto vs gated)

The whole design hinges on one classification: **does this change move the feature fingerprint / touch the
live trading hot path?**

- The fingerprint is `BusSchema.from_registry().fingerprint` — a stable 64-bit hash of the registered
  feature set (group:name:version for every feature). It is what `fc` and every strategy compile against.
- A change that leaves the fingerprint **byte-identical** vs `origin/main` and touches only safe surfaces
  (dashboard / docs / tests / ops / non-fingerprint code) is **fp-neutral** → eligible for the AUTO path.
- A change that moves the fingerprint, or touches `fc` / strategy / crypto-capture code, is
  **fingerprint-affecting** → it is GATED to the Lead's controlled, market-closed relaunch window.

This is the same classification the WDPC scope-guard already encodes
(`quantlib/features/within_day_scope_guard.py`, PR #329): fp-neutral vs fingerprint-affecting. The CI gate
**reuses** it as the auto-vs-gated boundary rather than inventing a parallel notion.

**If scope is uncertain → treat as TIER 2 (fail closed).** The cost of wrongly auto-deploying a
fingerprint change to the live trading loop is far higher than the cost of asking the Lead to click once.

## TIER 1 — AUTO (fp-neutral + scope-safe)

Dashboard, docs, tests, ops scripts, and other non-fingerprint code.

```
PR opened/updated
  → CI runs the whole tests/ dir (fp job + dashboard job) + coverage audit in fp-dev --rm containers
  → posts a commit status (success/failure)
  → IF green AND fp-neutral AND touches only a known-safe container surface:
       AUTO-MERGE  (Phase 2)
       → on the new main merge:
           AUTO-DEPLOY (Phase 3): FF the live tree + restart ONLY the affected safe container
           (docker compose up -d --no-deps <svc>) → verify health → update READINESS.md / SYSTEM_LOG.md
```

No Lead involvement. Event-driven.

## TIER 2 — GATED (fingerprint-changing / fc-affecting / live-trading)

Any change that moves the fingerprint, or touches `fc` / strategies / crypto-capture / execution.

```
PR opened/updated
  → CI runs the SAME full suite + posts the SAME commit status
  → the PR is LABELED `tier-2-gated` (and NEVER auto-merged)
  → the Lead merges + deploys it in the controlled, market-closed relaunch window
    (fc only ever via ops/nightly_relaunch.sh — NEVER docker restart/start fc)
```

CI still runs and still gates correctness — only the *merge* and *deploy* are manual for TIER 2.

## Why a self-hosted runner (not cloud GitHub Actions)

The fp test environment is the `fp-dev` Docker image, which carries the compiled `quantlib` + Rust kernels.
The whole `tests/` dir (1271 tests) collects in `fp-dev` with no DB / store / Redis for the unit subset. A
cloud GHA runner would have to rebuild that image (with Rust) on every run and still couldn't reach the
`/store` volume or Timescale. A **box-local watcher** using the already-built `fp-dev` image is faster,
reproducible, and has the real environment. So CI is a **watcher daemon on the box**, not a
`.github/workflows/*.yml`.

## Coverage honesty — the gate runs EVERYTHING, and is loud about what it can't

A gate that runs only `test_fp_*.py` would silently skip **90 of 156** test files — exactly the half-tested
pattern. So the gate runs the **whole `tests/` dir** as two jobs, and audits for blind spots:

- **`fp` job** — the entire `tests/` dir in base `fp-dev`, `--ignore`-ing only the dashboard-dep files below.
- **`dashboard` job** — `test_group_guide.py`, `test_store_grid.py`, `test_store_grid_cache.py`,
  `test_latency_expectations_route.py` run in `fp-dev` after `pip install -r
  services/dashboard/requirements.txt`. These import `fastapi` / `pyyaml` / `pymongo`, which are NOT in the
  base `fp-dev` image, so they ERROR at collection there (a real, pre-existing CI blind spot). Installing the
  dashboard's own (authoritative, drift-proof) requirements lets them run. They are **excluded explicitly and
  visibly**, never silently skipped.
- **coverage audit** — after the jobs, the gate `--collect-only`s `tests/` and reports any `test_*.py` that
  errors at collection yet is NOT a known dashboard-dep file. A NEW test that pulls an uninstalled dep (the
  same failure mode) is flagged **loudly and turns the gate RED**, so a whole untested class can never hide
  behind a green badge.

The PR is **green only if every job passes AND the audit is empty**. The sticky comment lists each job's
verdict and any uncovered files. When a new dashboard-dep test appears, add it to `DASHBOARD_DEP_TESTS` in
`ops/ci_watcher.py` (the audit will have already turned the gate red to force the decision).

## Phase 1 — the CI gate (SHIPPED FIRST)

`ops/ci_watcher.py` — a polling daemon (default 60s) that:

1. `gh pr list` for open PRs against `main`.
2. For each PR whose head SHA it hasn't yet graded, checks out that SHA into a throwaway worktree.
3. Runs the two jobs (`fp` + `dashboard`) + the coverage audit described above, all in `fp-dev --rm`
   containers (env scrubbed — **never** mounts `.env`, so the paper Alpaca creds cannot leak into CI logs).
4. Classifies scope: computes the fingerprint in the PR worktree vs `origin/main`; inspects changed paths.
   fp-neutral + safe-surface → TIER 1; else TIER 2.
5. Posts a **commit status** via `gh api` (context `ci/fp-suite`) so the PR's mergeability reflects real
   test state, and a sticky summary comment with per-job pass/fail + any uncovered files + tier.
6. Applies the `tier-2-gated` label when the change is gated.

Run it: `python -m ops.ci_watcher --once` (grade all open PRs once) or `ops/ci_watcher.sh` (the persistent
daemon, installed as a systemd/cron-supervised loop). See `ops/ci_watcher.sh` for the supervised form.

The gate is **proven by breaking it on purpose**: a PR with a deliberately-failing test must go red
(status = failure, not mergeable); a good PR must go green; a test importing an uninstalled dep must be
flagged uncovered (red). Proofs are recorded in the PR thread and the SYSTEM_LOG.

## Phase 2 — auto-merge (TIER 1 only)

When CI is green AND the PR is TIER 1, the watcher merges it (`gh pr merge --squash --delete-branch`). TIER 2
is never auto-merged — it only gets the `tier-2-gated` label for the Lead. Auto-merge is on by default for
green TIER-1 PRs; a human can add the `no-auto` label to hold any individual PR, and the whole daemon can run
with `--no-auto-merge` (gate + status only, no merging) during conservative rollout.

## Phase 3 — auto-deploy (TIER 1 safe containers only)

On a new merge to `main`, the deploy watcher:

1. Maps changed paths → affected safe container (dashboard ← `services/dashboard/**` / frontend; etc.) via
   an explicit allowlist. Anything not in the allowlist → **no auto-deploy** (escalate).
2. Fast-forwards the live tree and restarts ONLY that container: `docker compose up -d --no-deps <svc>`.
3. Verifies health (panel reachable / container healthy), then appends to `READINESS.md` + `SYSTEM_LOG.md`.

`fc` / fingerprint / strategy / crypto deploys are **never** auto — `fc` only via `ops/nightly_relaunch.sh`
at the controlled window. The deploy allowlist physically cannot name `fc`.

## Installing the daemons

After PR #348 merges, run the two watchers as supervised loops. The restart-loop wrapper is
`ops/ci_watcher.sh` (it restarts the python daemon after a 10s backoff if it dies, so a transient
`gh`/`docker` hiccup can't take CI offline):

```bash
# The CI gate + auto-merge watcher (Phase 1-2)
nohup ops/ci_watcher.sh ci    >> ~/.quant-ops/ci_watcher.log 2>&1 &
# The auto-deploy watcher (Phase 3)
nohup ops/ci_watcher.sh deploy >> ~/.quant-ops/ci_deploy.log  2>&1 &
```

A systemd unit (`Restart=always`, `WorkingDirectory=/home/ben/quant-fp`) is the durable form; until then the
`nohup` loop or a `@reboot` cron line suffices. Phase-2 auto-merge is enabled by running the gate WITHOUT
`--no-auto-merge` (the daemon default). To roll out conservatively, start with `--no-auto-merge` (gate +
status only), watch it grade a few PRs correctly, then drop the flag.

Env knobs: `CI_REPO_DIR` (default `/home/ben/quant-fp`), `CI_FP_IMAGE` (default `fp-dev`),
`CI_POLL` (seconds), `CI_SUITE_GLOB` (default the full `tests/test_fp_*.py`), `CI_SUITE_TIMEOUT_S`,
`CI_LIVE_TREE` (deploy watcher's live checkout).

## Hard boundaries (encoded in the code, not just here)

- worktree → PR off `origin/main`; the watcher never edits the live `quantlib` tree.
- The AUTO path is ONLY ever TIER 1 (fp-neutral + safe container). It can never touch
  fc / fingerprint / strategies / crypto-capture.
- GOLDEN RULE: never `docker restart`/`start` fc — relaunch only via `ops/nightly_relaunch.sh`.
- Never `docker kill --filter ancestor=fp-dev` (that would kill fc + every sandbox).
- CI containers never mount `.env` for the unit suite; env is scrubbed from logs. The paper Alpaca creds
  never reach a CI log.
