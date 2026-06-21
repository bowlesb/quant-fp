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
  → CI runs FULL tests/test_fp_*.py (+ opt-in latency e2e) in an fp-dev --rm container
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
**64 of 66 `tests/test_fp_*.py` files run purely inside `fp-dev` with no DB / store / Redis** — verified by
grep. The full suite is `docker run --rm -v $PWD:/app -w /app fp-dev python -m pytest tests/test_fp_*.py`.
A cloud GHA runner would have to rebuild that image (with Rust) on every run and still couldn't reach the
`/store` volume or Timescale for the 2 infra-touching tests. A **box-local watcher** using the already-built
`fp-dev` image is faster, reproducible, and has the real environment. So CI is a **watcher daemon on the
box**, not a `.github/workflows/*.yml`.

## Phase 1 — the CI gate (SHIPPED FIRST)

`ops/ci_watcher.py` — a polling daemon (default 60s) that:

1. `gh pr list` for open PRs against `main`.
2. For each PR whose head SHA it hasn't yet graded, checks out that SHA into a throwaway worktree.
3. Runs the FULL `tests/test_fp_*.py` suite in an `fp-dev --rm` container (env scrubbed — **never** mounts
   `.env`, so the paper Alpaca creds cannot leak into CI logs). The opt-in latency e2e
   (`FP_LATENCY_E2E=1`) is run only when `CI_RUN_LATENCY=1`, since it needs `.env`.
4. Classifies scope: computes the fingerprint in the PR worktree vs `origin/main`; inspects changed paths.
   fp-neutral + safe-surface → TIER 1; else TIER 2.
5. Posts a **commit status** via `gh api` (context `ci/fp-suite`) so the PR's mergeability reflects real
   test state, and a sticky summary comment with pass/fail + tier.
6. Applies the `tier-2-gated` label when the change is gated.

Run it: `python -m ops.ci_watcher --once` (grade all open PRs once) or `ops/ci_watcher.sh` (the persistent
daemon, installed as a systemd/cron-supervised loop). See `ops/ci_watcher.sh` for the supervised form.

The gate is **proven by breaking it on purpose**: a PR with a deliberately-failing `test_fp_*` must go red
(status = failure, not mergeable); a good PR must go green. Both proofs are recorded in the PR thread and the
SYSTEM_LOG.

## Phase 2 — auto-merge (TIER 1 only)

When CI is green AND the PR is TIER 1, the watcher merges it (`gh pr merge --squash`). TIER 2 is never
auto-merged — it only gets the `tier-2-gated` label for the Lead. Auto-merge requires the PR to carry the
`auto-ok` posture (default-on for TIER-1; a human can add `no-auto` to hold any PR).

## Phase 3 — auto-deploy (TIER 1 safe containers only)

On a new merge to `main`, the deploy watcher:

1. Maps changed paths → affected safe container (dashboard ← `services/dashboard/**` / frontend; etc.) via
   an explicit allowlist. Anything not in the allowlist → **no auto-deploy** (escalate).
2. Fast-forwards the live tree and restarts ONLY that container: `docker compose up -d --no-deps <svc>`.
3. Verifies health (panel reachable / container healthy), then appends to `READINESS.md` + `SYSTEM_LOG.md`.

`fc` / fingerprint / strategy / crypto deploys are **never** auto — `fc` only via `ops/nightly_relaunch.sh`
at the controlled window. The deploy allowlist physically cannot name `fc`.

## Hard boundaries (encoded in the code, not just here)

- worktree → PR off `origin/main`; the watcher never edits the live `quantlib` tree.
- The AUTO path is ONLY ever TIER 1 (fp-neutral + safe container). It can never touch
  fc / fingerprint / strategies / crypto-capture.
- GOLDEN RULE: never `docker restart`/`start` fc — relaunch only via `ops/nightly_relaunch.sh`.
- Never `docker kill --filter ancestor=fp-dev` (that would kill fc + every sandbox).
- CI containers never mount `.env` for the unit suite; env is scrubbed from logs. The paper Alpaca creds
  never reach a CI log.
