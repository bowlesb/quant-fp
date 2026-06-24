# ZERO-GAP DEPLOY RUNBOOK — "merge → it's live with no capture gap"

Status: **PRACTICED + HARDENED (sandbox), crypto-canary arm next; equity arm Lead/Ben-gated.** Authored
2026-06-24 after driving the live capture-loop seam end-to-end across group KINDS on a throwaway sandbox
capture (>=33 swaps, all green) and hardening the two fail-safe gaps the happy-path tests did not cover.

This is the operational companion to the DESIGN (`docs/WITHIN_DAY_PARITY_CONTINUOUS_DEPLOY.md`) and the FLEET
(`docs/FEATURE_WORKER_FLEET.md`): the exact flow, the arm sequence, how you SEE a swap landed cleanly, the
residual risks, and the precise gate to make it fully unattended.

---

## 0. The one-paragraph flow

A feature-worker (`feature_worker.py`) owns ONE untrusted group, proves a fix in-sandbox
(`compute_latest == compute` on recent live data), and — if the fix is in-scope + fp-neutral — enqueues it on
the FIFO deploy queue (`within_day_deploy_queue`). The LIVE capture loop calls
`poll_and_apply_at_boundary(state, config)` AT EACH MINUTE BOUNDARY (after a minute's dispatch, before the
next minute's compute — the only safe swap point). Armed (`FP_WDPC_LIVE_SWAP=1`), the seam claims one job,
re-checks the scope-guard, fast-forwards the bind-mounted tree to the merged commit, **hot-swaps that one
group's compute in the running `REGISTRY`** (re-import the module → overwrite `REGISTRY._groups[name]`), lets
the `RunningState` contract reseed any carried state, confirms live==backfill on the settled window, and
records `applied` / `rolled_back` / `escalated`. The next minute computes on the fixed code. **No relaunch,
no missed minute, fingerprint unchanged.**

---

## 1. Why it is gap-free (the mechanism, verified)

`capture.py:process_bars` re-fetches the group list FRESH from `REGISTRY` every minute and looks groups up by
NAME. Overwriting `REGISTRY._groups[name]` with a fresh instance between minutes makes the NEXT minute call
the new compute — no engine coordination. The shared `CaptureState` (ring buffer, incremental engines, bus
publisher + schema) is SEPARATE from the per-group compute objects and is untouched; the fingerprint is
unchanged (same `group:name:version`), so publisher/codec/schema are untouched. The swap sits strictly
BETWEEN two `process_bars` calls in the single capture thread → no minute sees a half-old compute, and no
minute is dropped.

**Proven** on a real in-RAM `process_bars` loop (`tests/test_zerogap_deploy_practice.py`,
`tests/test_within_day_live_wiring_seam.py`): a lockstep never-swapped REFERENCE loop, and after each swap the
swapped loop's next-minute output is value-identical to the reference, every OTHER group byte-identical,
fingerprint held, and `live.minutes == reference.minutes` (zero gap).

---

## 2. The three hot-swap KINDS (the reseed surface differs — this is the risk)

The reseed correctness — not the registry overwrite — is the real risk, and it is KIND-dependent. The applier
is KIND-AGNOSTIC by the `RunningState` contract (`if not group.up_to_date(buffer): group.rebuild_from_history(buffer)`);
the kind only determines what that contract does internally. **In the LIVE capture loop today** the three
kinds resolve as:

| KIND | Representative | In the capture loop | Contract behaviour | Reseed? |
|---|---|---|---|---|
| **Stateless** | `calendar` (plain `FeatureGroup`) | recomputes from the ring every minute | `up_to_date()` default **True** | none — DIRECT swap |
| **StatefulGroup** | `technical`, `candlestick`, `price_returns`, `price_levels` | runs `compute_latest(ctx)` FRESH from the ring each minute — `CaptureState` holds **NO** `StatefulEngine` (`emit_stateful` is not wired into `process_bars`) | `up_to_date()` default **True** | none — DIRECT swap |
| **Incremental ReductionGroup** | `momentum` (`incremental_safe`) | carries a live `IncrementalEngine` in `CaptureState.engines` (the `incremental_safe` reductions ALWAYS ride the running sums — the `FP_INCREMENTAL` master switch was removed) | hot-swap binds the engine pending-reseed → `up_to_date()` **False** → `rebuild_from_history` reseeds from `state.buffer` | **YES — the one reseed surface** |

**Practical consequence (the scoping that de-risks the rollout):** the ONLY kind whose live hot-swap reseeds
is the incremental ReductionGroup. The reseed is `IncrementalEngine.seed(buffer, seed_symbols)`, proven
value-identical by `seed(H);fold(m) == seed(H+m)` (the engine's parity invariant) AND by the practice loop's
reference-parity test across 11 repeated swaps. Stateless + StatefulGroup swaps are pure DIRECT registry
overwrites — they cannot get the reseed wrong because there is no carried state to reseed. Pinned by
`test_kind_classification_is_what_the_runbook_claims` so this scoping cannot silently rot if a StatefulEngine
is later wired into capture.

**Bounded-ring note (not a bug):** the reseed seeds from `state.buffer` (the ring, `maxlen=depth`). If the
ring is shallower than a group's longest window (momentum's is 180m), the reseeded state covers only the
ring's depth — but that is the SAME bounded history the per-minute fold already accumulates from, so the
reseed equals the running state by construction (the practice loop runs at `depth=120 < 180` and momentum
still matches its reference cell-for-cell). The tripwire (`live==backfill` on the SETTLED window) is the
backstop if a reseed were ever short.

---

## 3. THE ARM SEQUENCE (crypto-canary first, then equity)

Everything is OFF until env flags are set ON A CAPTURE RELAUNCH. The seam is a pure no-op with the flag unset
(`poll_and_apply_at_boundary` returns `[]` before any DB/git/registry/bus access).

### 3.1 Crypto canary (the first arm — NEVER the equity fc, NEVER a second crypto websocket)

The seam is wired ONLY into `crypto_capture.py` (the canary). Stage it in two steps:

1. **Arm SWAP, WRITE UNSET** (observe zero mutation): relaunch crypto-capture with
   - `FP_WDPC_LIVE_SWAP=1` — the master arm
   - `FP_WDPC_FEATURE_TREE=/app` — the bind-mounted tree the merge/rollback fast-forwards (default `/app`)
   - `FP_WDPC_LIVE_WRITE` **UNSET** → `dry_run=True`: the seam runs, claims nothing destructive, logs intent,
     mutates NOTHING (no git, no DB write, no real swap effect on a real queue). Watch
     `~/.quant-ops/...crypto...log` for `BOUNDARY-APPLY` lines and confirm no dent, no missed minute.
2. **Add WRITE** (`FP_WDPC_LIVE_WRITE=1`): now a real enqueued untrusted-crypto-group fix hot-swaps + the
   tripwire confirms live==backfill. This is the canary proof.

   > Per the WDPC crypto-cert lifecycle work: a LIVE crypto cert is gated on
   > `FP_CRYPTO_PERSIST_INPUTS=1`, and you must NEVER open a second crypto websocket on the same key (single-
   > conn-per-key kicks the live feed). Use the EXISTING crypto-capture container's relaunch, not a parallel one.

### 3.2 Equity fc (the gated second arm — Lead/Ben only)

The seam is NOT wired into the equity `process_bars` loop yet. Wiring it is a separate, deliberately-staged
Lead step. The arm flags are identical (`FP_WDPC_LIVE_SWAP=1`, `FP_WDPC_FEATURE_TREE=/app`,
`FP_WDPC_LIVE_WRITE` staged unset→set). **The equity tree is BIND-MOUNTED + pinned** (the fc bind-mount pin),
so `live_do_merge`'s `--ff-only` IS the deploy mechanism — and a non-ff (diverged) tree RAISES rather than
clobbering the pin (now contained: §5).

---

## 4. OBSERVABILITY — how you SEE a swap happened cleanly

| Signal | Where | What clean looks like |
|---|---|---|
| The boundary outcome | capture log: `WDPC hot-swap: job=<id> group=<g> -> applied (...)` | `applied` (not `rolled_back` / `escalated` / `error`) |
| Zero gap | `metrics` minute counter + the store's stream partitions for the minute | no missing minute around the swap timestamp |
| Fingerprint held | `BusSchema.from_registry().fingerprint` | unchanged across the swap (the seam asserts this; a move auto-reverts + escalates) |
| The cert/trust write | `within_day_parity_cert` + `feature_trust` (`record_outcome`) | a `certified` stamp the day of, trust granted on the nightly sweep |
| Who owns what | `within_day_assignment` (PK `group_name`) | one `active` row per worked group; the Lifecycle dashboard tab rolls it up |
| The reseed note | the `applied` detail string | `swapped + contract-reseeded ...` (incremental) or `swapped; up-to-date ...` (stateless/stateful) |

The tripwire (`live_confirm_tripwire` → `within_day_monitor.compare_is_clean`) is the SAME settled-window read
the nightly sweep uses, so "the swap confirmed clean" means the same thing within-day as it does nightly.

---

## 5. HARDENING shipped in this pass (the rough edges found)

The happy path was solid on the first run (33 swaps across 3 kinds, multi-job drain, surgical isolation — all
green). The genuine gaps were in the FAIL-SAFE envelope — a deploy attempt that ERRORS must never break
capture, because the seam runs INSIDE the capture loop:

1. **A failed merge propagated (would crash capture).** `apply_job` called `do_merge` OUTSIDE the try/except
   that guarded `do_swap`. A diverged-tree `--ff-only` (the documented "raise, don't clobber the pin" case)
   raised `CalledProcessError` straight through the seam into `on_bar`/`process_bars` — a capture-stream
   break. **Fix:** `do_merge` is now wrapped → a merge failure ESCALATES the job with NO swap (the swap must
   not run if the FIXED source never reached the tree). `tests/test_zerogap_deploy_failsafe.py::test_apply_job_merge_failure_escalates_no_swap`.
2. **No defence-in-depth around the per-job apply.** An unexpected error anywhere `apply_job` does not guard
   (`claim_next`, `gather_live_evidence`'s DB read, `record_outcome`) escaped the seam. **Fix:**
   `poll_and_apply_at_boundary` now contains ANY per-job exception → logs + records an `error` outcome + moves
   on; **capture is never interrupted by a deploy attempt.** Same fail-safe pattern as `stream_supervisor.py`
   (the fc-outage mitigation). `test_seam_contains_merge_failure_does_not_crash_capture`,
   `test_seam_contains_unexpected_evidence_error`.

Both fixes are fp-NEUTRAL (control-flow only; fingerprint `0x204f9ee42521b36f` unchanged) and pure hardening
of the deploy machinery — no feature math.

**Verified non-issues (no fix needed):** multi-job drain reseeds both groups correctly with zero gap
(`test_multi_job_drain_at_one_boundary_both_correct`); the per-group rollback target (`prior_commit` captured
once before the drain) is CORRECT even across a multi-job drain because rollback checks out only the group's
own source path and disjoint scope guarantees an earlier job never touched it; repeated swaps of the same
group are idempotent (11x per kind, no drift).

---

## 6. RESIDUAL RISKS + the gate to fully unattended

| Risk | Status | Mitigation / gate |
|---|---|---|
| Live-only minute-boundary race | LOW — mitigated by construction | the seam runs strictly between `process_bars` in the single capture thread; no concurrent compute. Proven on the in-RAM loop; the canary confirms at 24/7. |
| Incremental reseed at 11k-symbol equity scale | UNPROVEN at scale | the canary is small-universe; gate = an equity reseed-parity assert on a STAGED equity relaunch with `FP_WDPC_LIVE_WRITE` UNSET (observe, mutate nothing) before WRITE. |
| The git seam (`live_do_merge` ff-only / `live_rollback_swap` checkout) | UNTESTED against a real tree | all sandbox tests are dry-run; the canary's WRITE step is the first real-tree exercise. A diverged tree now escalates safely (§5) rather than crashing. **Recommended follow-up: a throwaway-git-repo integration test of the ff-only + non-ff-raises + scoped-rollback paths.** |
| `gather_live_evidence` reconstructs conservative evidence | KNOWN stub | the rich agent-attached evidence (changed_files / byte-eq / parity proof) is threaded through the queue row in the production path (a follow-up; the gate is identical either way). |
| Tripwire false-negative (live==backfill but flagged) | LOW | the tripwire is the nightly read; a false-negative rolls back (untrusted → no trade risk) + escalates. Conservative by design. |

### The precise gate to "deploy without thinking about it"

An arbitrary **fp-neutral, single-group, untrusted** PR can deploy to live equity fc with zero gap unattended
once ALL of these hold:

1. **Crypto-canary clean run** — a real untrusted crypto group fix hot-swapped, tripwire-confirmed, no dent
   (the §3.1 WRITE step). *Pending the arm.*
2. **Equity reseed-parity on a staged relaunch** — `FP_WDPC_LIVE_SWAP=1` + `FP_WDPC_LIVE_WRITE` UNSET on a
   real equity fc relaunch, observe the seam at 11k-symbol scale with zero mutation, assert the incremental
   reseed matches. *Pending.*
3. **The seam wired into equity `process_bars`** (it is crypto-only today). *A separate Lead step.*
4. **The fleet auto-enqueuing fp-neutral single-group fixes** (`FLEET_WRITE=1`) so a worker's proven fix
   reaches the queue without a human. *Built, dry-run-default, Lead-gated.*

**Honest confidence today:** the MECHANISM (zero-gap swap + reseed correctness across all kinds + fail-safe
containment) is proven and hardened in sandbox — high confidence it is correct. The remaining gates are
ACTIVATION + SCALE proofs (canary WRITE, equity staged-observe, equity seam-wire), not mechanism redesign.
The single biggest unknown is the incremental reseed at 11k-symbol scale; everything else is staged-arm
discipline already specced here.
