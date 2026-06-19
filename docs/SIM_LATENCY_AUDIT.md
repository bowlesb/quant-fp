# Simulation realism + latency audit (2026-06-15)

> An honest audit of the streaming sim's realism and the measured bar→vector latency, with the slowest
> groups named. Companion to `docs/PROFILE_SIM.md` (the pre-flight tool) and `docs/LATENCY_PLAN.md`.
>
> **Re-measured 2026-06-18 (682 features / 51 groups). See the dated section at the bottom for the current
> per-group baseline — the table just below is the original 519-feature 06-15 reading, kept as history.**

## TL;DR

- **The sim is realistic in SHAPE.** It already emits minute **bars + trades + quotes** through a
  protocol-faithful msgpack Alpaca mock, driven by the *real* `alpaca-py` `StockDataStream` — the exact
  Monday client. Message keys match what `capture.py` expects (`S,o,h,l,c,v,t` for bars; the Alpaca
  trade/quote shapes). The tick path (trade_flow / quote_spread / liquidity / tick_runlength) is actually
  exercised, not bypassed (pinned by `tests/test_fp_stream_sim.py`).
- **Two realism gaps (now addressed / documented):**
  1. The sim's `_report` measured **per-shard compute only** — it never measured the true
     **bar-arrival → universe-vector-ready** wall-clock (LATENCY_PLAN item 6, "end-to-end sanity"). The
     new `profile_sim.py` adds it.
  2. `stream_sim.main` defaulted to **5 trades + 5 quotes/min** — a token rate. The mock's own realistic
     default is 24t + 72q/min/symbol; `profile_sim.py` uses the realistic firehose by default.
- **Latency is well over budget at scale, and the cause is now named.** End-to-end bar→vector p99:

  | universe / shards | per-shard compute p99 | end-to-end bar→vector p99 |
  |---|---|---|
  | 200 / 4   | ~158ms | (small) |
  | 1000 / 16 | ~447ms | **~603ms** |
  | 3000 / 32 | ~1468ms | (proportionally higher) |

  Budget is ~100ms. **FAIL** at every realistic scale on the full 519-feature flow. The **fast path**
  (305 reduction features only) is ~56ms p99 at 200/4 and ~115ms p99 at 1000/16 — close to budget; it is
  the **non-reduction "rest" groups that blow it.**

## The slowest groups (the lever)

A single uncontended shard (93 symbols, 300m buffer) via `profile_sim`'s per-group ranking, independently
cross-checked by `python -m quantlib.features.profile 93 300 250 5 --latest`:

| group | per-shard p50 | note |
|---|---|---|
| **`momentum_run`** | **~95–230ms** | **dominant.** ~23,500 µs/feature — by far the worst per-feature cost |
| `residual_analysis` | ~13–50ms | second |
| `market_context` | ~12–25ms | cross-sectional gather |
| everything else | ≤ ~10ms each | |

### Root cause

`momentum_run` and `residual_analysis` use the **base-class `compute_latest`**, which runs the full
`compute()` over the **entire 300m trailing buffer every minute** and then filters to the last minute.
`momentum_run` only declares a 60m window, so ~80% of that buffer work is recompute it never uses, and its
`compute` does several `with_columns` passes over the whole `300m × N` frame plus a per-window
`rolling().agg()` with a `list.eval` — the expensive part. This violates the standing rule that *"a
feature earns its place only if it is timed and fast"* (`docs/LATENCY_PLAN.md`, `tests/test_fp_latency.py`).

### Recommended fix (NOT done in this PR — parity-sensitive, scoped separately)

> **STATUS (updated 2026-06-18): the buffer-slice half of this is SHIPPED.** Both `MomentumRunGroup`
> (v2.0.0) and `ResidualAnalysisGroup` now override `compute_latest` to run the SAME `compute()` on the
> buffer sliced to `LOOKBACK_MINUTES = max(window)+15 = 75m` (not the full 300m) — guarded cell-for-cell by
> `tests/test_fp_latest.py` + each group's parity test. So the *recompute-the-whole-300m-buffer* tax below
> is GONE; what remains is the intrinsic `rolling().agg().over("symbol")` cost on the 75m slice. Collapsing
> THAT (→ ReductionGroup / Rust kernel) is the still-open, Lead-gated Lever #2 in `docs/LATENCY_PLAN.md` §7.

Override `compute_latest` on `MomentumRunGroup` (and then `ResidualAnalysisGroup`) with an aggregate-at-T
form that:

1. slices the buffer to each feature's **own window** (≤60m for momentum_run) before computing, and
2. emits **one row per symbol** for minute T instead of the whole rolling frame.

This is exactly the pattern the reduction/stateful groups already use, and it is **guarded for free** by
`tests/test_fp_latest.py` (the generic invariant `compute_latest == compute().filter(last minute)`), so a
fast live form can never silently diverge from the backfill rolling form. Expected impact: the dominant
~95–230ms/shard term collapses toward the ~10ms tier, pulling the full-flow p99 toward the fast path's
sub-150ms. It is left out of this audit PR because momentum_run's OLS-residual-skew + run-length math is
parity-sensitive and deserves its own focused change + parity run.

A secondary, broader lever (already noted in `capture.py` as backlog P1.0): the 300m buffer over-feeds
**every** windowed group; per-group buffer-depth slicing before `compute_latest` would cut the recompute
tax across the board, not just momentum_run.

## What this PR ships

- **`quantlib/features/profile_sim.py`** — the pre-flight profiler: end-to-end bar→vector p50/p95/p99
  (slowest-shard-per-minute, last-bar anchor, write excluded) + a per-GROUP ranking. `make fp-profile-sim`.
- **`stream_sim.py`** — two **default-off** profiler hooks so the tool can attribute cost without changing
  the measured hot path:
  - `FP_SIM_GROUP_TIMINGS=1` → per-group `compute_latest` ms in the bench log (the per-feature/group
    attribution the phase decomposition could not give).
  - a cross-process `dispatch_wall` (reader) / `ready_wall` (worker) `time.time()` stamp pair → the true
    end-to-end bar→vector latency (the same last-bar anchor as `real_capture.feature_assemble_seconds`).
- **`docs/PROFILE_SIM.md`** — how to read the output; **`tests/test_fp_profile_sim.py`** — unit tests for
  the pure aggregators.

All existing tests (`test_fp_stream_sim`, `test_fp_latest`, …) pass unchanged; with the flags unset the
sim path is byte-for-byte what it was.

## What to monitor at the open

- Grafana `feature_assemble_seconds` (last-bar / pure-compute anchor) and `feature_vector_latency_seconds`
  (first-bar anchor, includes Alpaca delivery spread) per shard — the live analogue of this tool's
  end-to-end number.
- `feature_group_compute_seconds` per group — **watch `momentum_run` and `residual_analysis`**; they are
  the live drivers of the per-minute budget until the `compute_latest` override lands.
- `latency_slow_symbols` (TimescaleDB) — which tickers ran hottest when a shard spiked.

## Caveats on the numbers

- The sim **floods** (no inter-minute sleep), so the end-to-end number is a **stress ceiling** dominated by
  cross-process queue contention when all shards compute at once — it is intentionally pessimistic vs the
  real one-bar-per-minute cadence, and it is the honest worst case for "can we keep up".
- The per-shard *compute* decomposition (`stream_sim._report`) and the per-group ranking are
  contention-light at small shard counts but grow super-linearly with symbols/shard, so always profile at
  the shard size you intend to deploy.

## Re-measure 2026-06-18 (682 features / 51 groups) — regression check + current baseline

Run: `make fp-profile-sim N=1000 SHARDS=16 MIN=20` at the deployed commit (fingerprint
`0x710bed9e980616f3`). Same scale as the 06-15 reading above, so the two are directly comparable.

**End-to-end bar-arrival → universe-vector-ready (slowest shard / minute, write excluded):**

| date | universe / shards | features | end-to-end p50 | p95 | p99 |
|---|---|---|---|---|---|
| 2026-06-15 | 1000 / 16 | 519 | — | — | **~603ms** |
| 2026-06-18 | 1000 / 16 | 682 | 401ms | 616ms | **761ms** |

The +26% p99 (603→761ms) tracks the **+31% feature growth** (519→682, the order-flow batches PRs #115 et
al.) almost exactly — it is expected scaling, **NOT a regression**. The two dominant groups are stable
against their 06-15 readings: `momentum_run` p50 94ms (was ~95ms), `residual_analysis` p50 50ms (was the
top of its ~13–50ms range). Still **FAIL** vs the 100ms budget (7.6×), as designed — latency is on a
team-lead STAND-DOWN (`docs/LATENCY_PLAN.md` §7); this is the production-readiness number, not edge-blocking.

**Per-group `compute_latest` p50/p99 baseline (slowest shard each minute, post-warmup).** Diff the next
sim against this to catch a real per-group regression instead of eyeballing prose. Only the non-trivial
groups are listed; the long tail of reduction groups sits at the floor (~2.5ms p50 / ~4.5ms p99 each).

| group | p50 | p99 | note |
|---|---|---|---|
| `momentum_run` | 94ms | 269ms | dominant — Lever #2 target (`compute_latest` override / kernel migration) |
| `residual_analysis` | 50ms | 110ms | second — Lever #2 first move (→ ReductionGroup) |
| `daily_beta` | 20ms | 133ms | cross-sectional gather |
| `overnight_beta` | 12ms | 30ms | |
| `return_dispersion` | 12ms | 48ms | cross-sectional gather |
| `market_context` | 11ms | 113ms | cross-sectional gather |
| `gap_fill_state` | 11ms | 35ms | |
| `dumper_state` | 11ms | 57ms | |
| `runner_state` | 10ms | 72ms | |
| `liquidity_rank` | 9ms | 33ms | cross-sectional gather |
| reduction tier (33 groups) | ~2.5ms | ~4.5ms | each — already on the proven fast shape |

`SUM of per-group p50 = 333ms` (the per-minute serial compute budget; the end-to-end p99 is higher because
it is the slowest-shard tail, not the sum). The ranking is unchanged in SHAPE from 06-15: `momentum_run` +
`residual_analysis` together (~144ms p50) are ~43% of the per-minute compute — exactly the Lever #2 thesis
in `docs/LATENCY_PLAN.md` §7. No single group regressed; nothing to fix this cycle.

## Regression re-check 2026-06-18 (later cycle) — NO regression, with a host-load caveat

Re-ran `FP_SIM_GROUP_TIMINGS=1 make fp-profile-sim N=1000 SHARDS=16` at the SAME deployed fingerprint
(`0x710bed9e980616f3` / 682 / 51, commit 47e3187) plus single-shard `profile 93 300 250 5 --latest` reps,
to diff against the per-group baseline above. **Verdict: NO per-group regression.** The per-group *shape* is
identical — `momentum_run` then `residual_analysis` then `daily_beta` dominate; the 33-group reduction tier
sits at its ~2.5ms floor; nothing changed rank.

**Read the per-group p50, NOT the end-to-end p99, for the regression signal.** This re-check ran while the
box was under heavy concurrent load (1-min load avg ~15 on 32 cores; two sibling loops at 540% + 400% CPU =
the order-flow backfill + a research harness). Under that BLAS oversubscription the contention-sensitive
numbers inflated uniformly (end-to-end p99 1256ms vs the 761ms baseline; `momentum_run` per-group p50
108ms vs 94ms, +15%), while the contention-LIGHT numbers stayed flat (`residual_analysis` 49ms vs 50ms;
the reduction tier unchanged). Confirmed it is load, not code: re-measuring `momentum_run` uncontended via
three single-shard min-of-5 reps gave 130 / 131 / 180ms — a 38% spread across identical reps that tracks
host load, not a stable shift (the same parallelism-profile effect documented in the #123 latency-ceiling
fix). The robust regression discipline holds: **diff per-group p50 on a quiet box, and re-confirm any
suspected offender uncontended before declaring a regression** (mirrors the re-confirm-on-offense logic in
`tests/test_fp_latency.py`).

Net: the 682/51 fingerprint is latency-stable vs the #125 baseline. The dominant cost is still Lever #2
(`momentum_run` + `residual_analysis`, ~43% of compute) — and note (see the "Recommended fix" status box
above) the *buffer-slice* sub-lever already shipped (both groups slice to 75m); what remains is the
intrinsic `over("symbol")` rolling cost on the slice, which is the still-open, Lead-gated §7 migration.

## Single-shard `--latest` per-group baseline 2026-06-19 (the cheap regression harness)

Every cycle's regression check actually runs the *single-shard* `--latest` profiler, not the full
N=1000/16 sim — it is ~5× cheaper and runs fine under host load, and it is the one whose top offender
(`momentum_run`) the prior re-checks re-confirmed. But until now only `momentum_run` had a recorded
single-shard reference; everything else had to be eyeballed from prose. This table is the diff-able
baseline for that harness so the *whole* ranking can be diffed, not just the top group.

Run (deployed fingerprint `0x710bed9e980616f3` / 682 / 51, commit ec2b5ef):
`docker run --rm -v $PWD:/app -w /app fp-dev python -m quantlib.features.profile 93 300 250 5 --latest`
— 666 features / 46 groups (the single-shard live path excludes the cross-shard gather groups the sim
adds, so absolute ms are NOT comparable to the sim table above; this table is internally consistent and
diffs only against ITSELF). Three min-of-5 reps under host load avg ~12 (the order-flow backfill + sibling
loops at 546% + 402% CPU). Columns: the three reps, then the diff rule.

| group | rep1 | rep2 | rep3 | classification |
|---|---|---|---|---|
| `momentum_run` | 137 | 132 | 146 | **STABLE-dominant** — the Lever #2 target; min-of-5 band ~130–180ms |
| `price_volume` (70 feat) | 50 | 50 | 52 | **STABLE** — high total is feature-count (707 µs/feat), not per-feat cost |
| `distribution` | 60 | 38 | 35 | CONTENTION-SENSITIVE — 36% swing across identical reps = host load |
| `liquidity` | 34 | 46 | 32 | CONTENTION-SENSITIVE |
| `volume_leads_price` | 33 | — | 36 | CONTENTION-SENSITIVE |
| `residual_analysis` | 17 | 16 | 16 | **STABLE** — second Lever #2 move (→ ReductionGroup) |
| `clean_momentum` | 23 | — | 18 | mid-tier, mild swing |
| reduction / reference tail (~30 groups) | ≤ ~10 each | | | at the floor — `round_levels`/`asset_flags`/`multi_day_*` ~1–2ms |

**The diff rule (codify the re-confirm-on-offense discipline):** a group is a real regression ONLY if it
is in the STABLE column AND rises across ALL three reps. A single-rep spike in a CONTENTION-SENSITIVE group
(`distribution`, `liquidity`, `volume_leads_price`, the cross-sectional gathers) is host load — re-confirm
it uncontended (min-of-5, quiet box) before believing it, exactly as the #123 latency-ceiling test does on
its offenders. On this run no STABLE group rose across all three reps → **NO regression** (consistent with
the sim re-check above; the same fingerprint, a cheaper independent harness).
