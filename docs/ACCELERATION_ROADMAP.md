# Acceleration roadmap — every group/feature critically re-challenged on its optimal KIND

> Ben's escalated efficiency mandate (2026-06-21): *"100ms for a feature group is still way too slow. Look at
> EVERY SINGLE feature and ask: can I make this faster by better taking advantage of incremental state / Rust
> helpers? Think very critically for each feature."*
>
> This is the **audit, not the implementation.** It extends `docs/FEATURE_EFFICIENCY_AUDIT.md` (#334's per-group
> A-cache / B-fold / Rust classification) with a per-feature CRITICAL challenge — for every group it asks whether
> the current realization is on its fastest achievable KIND, names the specific change, and quantifies the
> speedup / effort / parity-fp risk. The Lead sequences implementation PRs from the ranked backlog in §6; nothing
> here is built. Numbers are the single-shard profiler p50/p99 from `docs/feature_latency_expectations.json`
> (312 sym × 245 min, trades on) — RELATIVE ranking, not the e2e bar→vector (the e2e truth is the #315 sim:
> p50 ~387ms / p99 ~436ms on the live 728 set).
>
> Pairs with: `docs/FEATURE_EFFICIENCY_AUDIT.md` (the gap list this sharpens), `docs/INCREMENTAL_READINESS.md`
> (per-group incremental mechanism), `docs/STATE_ABSTRACTION.md` (the seed/fold abstraction),
> `quantlib/features/running_state.py` (the `up_to_date()`/`rebuild_from_history()` contract),
> `quantlib/features/reduction_anchor.py` (the centered anchor), `quantlib/features/declarative.py`
> (`ReductionGroup` + the `assemble_canonical` Rust kernel).

## 0. The two profiler-artifact corrections this audit makes first (CRITICAL — they reframe the whole ranking)

Before ranking anything, two measurement facts re-frame what is actually slow. **The per-group profiler times
each group's `compute_latest` in ISOLATION — and for two whole classes of group that isolated number is NOT the
live in-flow cost.** Reading the raw JSON ms as "this group costs this much live" double-counts.

1. **The 23 ReductionGroups share ONE batched emit.** The profiler times each standalone (its own full
   running-sum build + assemble). In flow they are folded together through one `WindowedSumState` pass, so the
   high rows (`price_volume` 99ms, `volume_leads_price` 74ms, `distribution` 61ms, `liquidity` 64ms) over-count
   — their real marginal in-flow share is a fraction of that. The JSON `measurement.note` already says this; the
   ranking below treats reduction rows as **relative ordering within the reduction lever**, not absolute costs.

2. **The 4 StatefulGroups' profiler number is the ROLLING-DERIVE backfill form, NOT the live O(1) fold.**
   `StatefulGroup.compute_latest` (`stateful.py:673`) calls `_state_frame_rolling` — it derives every EMA/lag/
   extrema over the WHOLE buffer with `ewm_mean`/time-self-join/`rolling_*_by`, then filters to T. The live path
   does NOT use this: it uses `StatefulEngine.step()` (the resident EMAState/LastKState/ExtremaState fold,
   O(symbols × state)/minute). So `price_returns` 42ms / `technical` 17ms / `price_levels` 14ms / `candlestick`
   9ms are the rolling-derive cost, and the live fold is far cheaper. **This means `price_returns` is NOT a real
   42ms live group** — it is already on its optimal kind (LagSpec ring, O(1)), and the profiler just measures the
   wrong twin. The same is conceptually true of swing's old default path. **Action item for the JSON: profile
   StatefulGroups through `StatefulEngine.step()`, not `compute_latest`, so the dashboard stops flagging an
   already-optimal group as the #5 slowest.** (Listed in §6 as a measurement fix — cheap, high clarity value.)

The net effect: after these two corrections, the genuinely-slow LIVE costs are (a) the gather groups (paid once,
not per-bet), (b) the hand-written tick groups (`subminute_gap_fano` 48ms is the real #1 per-symbol cost), (c)
`momentum_run` 29ms (real — it is neither a reduction nor a stateful fold), and (d) the 3 PARKED reduction groups
that stay on the batch fresh-sum path even after Monday's `FP_INCREMENTAL` flip (`price_volume`, `market_beta`,
`residual_analysis`). Those four are where the remaining engineering leverage is.

## 1. The KIND taxonomy (what "fastest achievable" means per group)

| KIND | fastest-achievable per-minute design | cost when on it |
|---|---|---|
| **A — intraday-invariant** | compute once/session, `SessionCache`, broadcast | ~0/min |
| **B-reduction** | shared `WindowedSumState` running-sum fold, read at T | O(1)/min (after `FP_INCREMENTAL`) |
| **B-stateful** | resident `StatefulEngine` EMA/lag/extrema fold | O(symbols × state)/min |
| **B-latest-only** | one aggregate-at-T, no per-minute scan | ~0/min |
| **Gather** | run once in the reader phase, broadcast | not a per-bet cost |
| **Hand-written window** | carry a per-symbol fold OR a resident Rust tick-ring | O(window) today; O(1) achievable |

The CRITICAL test applied to every group below: *is it on the fastest-achievable kind for its math, or is it
re-computing something it could carry / cache / push to Rust?*

## 2. Per-group critical verdict (all 63 groups; per-feature where features in a group differ in cost)

Verdict legend: ✅ on optimal kind · ⚙️ lever exists, not yet flipped (Lead-gated) · 🔴 OFF optimal, real win
available · ➖ profiler over-counts, no real gap.

### 2a. The genuinely-slow LIVE groups (the real targets)

| group | feat | p50/p99 ms | kind today | verdict | fastest-achievable change |
|---|---|---|---|---|---|
| `subminute_gap_fano` | 1 | 48.6 / 65.3 | hand-written tick group-by, `compute_latest_on_window(60+1)` | 🔴 | **Rust tick-ring resident kernel** carrying the windowed gap-moment sums (Σgap, Σgap², per-minute counts) folded O(1)/min. The #324 `per_minute_gap_fano` kernel already cuts the per-minute Fano 2.5× (169→68ms compute); the residual is re-running the 60-window mean each minute. Resident-state version = carry the per-minute Fano series in a ring, fold the new minute, O(1). MEASURE first (the #324 lesson: marshaling can erase a win) but this is the slowest per-symbol group and its win is the largest single per-symbol lever. |
| `momentum_run` | 12 | 29.3 / 44.3 | hand-written windowed-slice `compute_latest`; NOT a reduction, NOT stateful | 🔴 (partial) | Two heterogeneous features: **`residual_skew_{w}m`** (window-local OLS 3rd moment — irreducible per-window fit, but a Rust windowed-OLS-moment kernel could fold the centered power sums cancellation-free, see §3) and **`longest_streak_{w}m`** (a sequential run-length state machine — a TRUE O(1) carried-state candidate: carry per-symbol current-run-sign + length + a per-window longest-in-window ring, fold the new return's sign). longest_streak is the cleaner win; residual_skew is Rust-or-leave. |
| `size_entropy` | 2 | 21.6 / 26.8 | hand-written 30/60m bin-count re-aggregate | 🔴 | carry the windowed 6-bin notional-bin COUNTS (Σ per bin over the window), fold the new minute's counts, entropy at T. O(window)→O(1). #324 found the Rust marshal LOST here (per-minute group-by already cheap), so the win is the CARRIED-COUNT fold in polars/numpy, not Rust. |
| `print_hhi` | 2 | 11.3 / 12.8 | hand-written 30/60m notional-HHI re-aggregate | 🔴 (low) | carry windowed Σnotional / Σnotional² per symbol, HHI at T = O(1). Same as size_entropy — carried-sum, not Rust (#324: Rust lost). Low absolute saving. |
| `microstructure_burst` / `inter_arrival` / `tick_runlength` / `trade_size_dist` / `large_print_burst` | 4/3/3/3/3 | ≤18 / ≤31 | hand-written `compute_latest_on_window(1)` — own-minute only | ✅ | already at-T (one minute, no re-scan). The residual cost is the per-minute tick marshal, inherent. No carried-state win — these are already optimal-ish. (`microstructure_burst` 18ms is the only mild outlier; the cost is the tick→frame marshal, addressed by the shared delta-frame engine change in §3, not a per-group change.) |

### 2b. The reduction groups — ⚙️ flip-gated, EXCEPT the 3 parked which need Rust (§3)

All 23 `ReductionGroup`s are on the shared `WindowedSumState` running-sum fold = the optimal mechanism. The live
O(1) payoff is the **Lead-gated `FP_INCREMENTAL` flip** (15 armed `incremental_safe=True`, Monday). The profiler
rows over-count (shared emit). Per-group verdict:

- **15 armed (⚙️ flip Monday):** `volume_leads_price`, `distribution`, `liquidity`, `momentum_consistency`,
  `clean_momentum`, `momentum`, `volatility`, `volume_exhaustion`, `efficiency`, `ohlc_vol`, `quote_spread`,
  `trade_flow`, `signed_trade_ratio`, `count_fano`, `volume`, `trend_quality`, `realized_range`, `trade_freq_z`,
  `range_expansion`, `return_dynamics` — running-sum fold, parity-soak-cleared (per the #327 soak: 15 GO). On
  optimal kind; nothing to do but flip.
- **3 PARKED (🔴 — the real reduction-lever gap):** `price_volume` (99ms, 70 feat), `market_beta` (29ms, 21
  feat), `residual_analysis` (12ms, 6 feat). These carry corr/OLS denominators `b·Σx²−(Σx)²` that the incremental
  running-Σx² rounds differently from the batch fresh-sum at the defined-guard threshold (the corr-denom
  straddle) → they stay on the **batch fresh-sum recompute even after Monday's flip.** This is the single largest
  reduction-path latency left (`price_volume` is the #1 standalone row at 99ms). **§3's centered-denom Rust
  kernel is what unparks them** — it is the highest-leverage NEW engineering item in this roadmap.

### 2c. Class-A cached / gather / latest-only / stateful — ✅ already optimal (recorded for completeness)

- **A cached/static (~24):** `sector`, `calendar`, `asset_flags`, `round_levels`, `prior_day`,
  `multi_day_returns`/`_vwap`, `daily_beta`, `liquidity_rank`, `return_dispersion`, `overnight_*`,
  `calendar_events`, `edgar_filing_frequency`, `intraday_seasonality`-cache → all `SessionCache` once/session.
  All ≤9ms p50 and ~0 marginal/min. **✅ no change** — #334 measured several NET-NEGATIVE to consolidate
  further; do not.
- **Gather (8):** `breadth`, `market_context`, `market_turbulence`, `cross_sectional_rank`, `peer_relative`,
  `sector_beta`, `sector_return` — run ONCE in the reader gather phase, not per-bet. **✅ N/A** (the per-shard
  profiling number for `sector_beta` ~96ms in single-shard `fp-profile-latest` is a per-shard ARTIFACT — the e2e
  sim confirms gather-phase-cheap; do NOT chase it).
- **B-latest-only (6):** `dumper_state`, `runner_state`, `gap_fill_state`, `draw_range`, `intraday_seasonality`,
  `momentum_run`-streak-portion — one aggregate-at-T. **✅** (could be PROMOTED to a declared `CumulativeState`
  kind for uniformity, but cost is already ~0).
- **B-stateful (4):** `price_returns` (42ms profiler ARTIFACT — §0), `technical`, `price_levels`, `candlestick`
  — resident `StatefulEngine` fold, O(1)/min live. **✅ already optimal; the JSON over-states their cost** (§0
  measurement fix).

### 2d. swing — ⚙️ already fixed, gated on `FP_SWING_STATEFUL`

`swing` (9 feat, 21ms) was the lone whole-session re-scan; the carried leg-state (`SwingState` on the
`RunningState` contract, `FP_SWING_STATEFUL`) folds only the new minute (26.1→15.5ms group-local, value-identical,
fp-unchanged). **⚙️ Lead-gated flip Monday.** The full O(1) (~2ms) needs the delta-frame engine change (§3).

## 3. The three durable engineering levers (the bigger changes — sequence these, don't bespoke-patch)

Per Ben's incremental-state mandate (`feedback-incremental-state-abstraction-mandate`): these are SHARED
abstractions with many beneficiaries, not per-group hacks.

### Lever I — the centered-denom Rust OLS/corr kernel (unparks the 3 reduction groups) ⭐ highest new leverage

**Problem:** `price_volume` / `market_beta` / `residual_analysis` are parked because the OLS/corr denominator
`denom_x = b·Σx² − (Σx)²` is a catastrophic-cancellation difference of two large near-equal running sums. Batch
fresh-sum and incremental running-Σx² round it onto opposite sides of the `>0` defined-guard at ~1e-16 → a
parity break, so they stay on the batch path (the 99ms `price_volume` cost survives Monday).

**The fix (already half-built, scoped here):** `reduction_anchor.py` proved the centered form
`Σ(x−a)²` is value-identical (shift-invariant) AND well-conditioned for the std case (rel-err 3e-6→1e-16,
`volume` unparked, #307). The same per-symbol anchor `a` applied to the OLS/corr `x` regressor makes
`denom_x = b·Σ(x−a)² − (Σ(x−a))²` a difference of SMALL conditioned sums — both paths round it the SAME side of
zero. The `assemble_canonical` Rust kernel (`rust/src/lib.rs:539`, the `defined = denom_x > 1e-12·(sx·sx)` guard
at lib.rs:616) already computes the RAW-power-sum denom; **change it to the centered denom and thread the
per-source anchor through `attach_reduction_anchors`** (the wiring point already shared by capture + materialize).
Value-identical (centered variance is shift-invariant → fp unchanged), unparks all 3 → they ride the
`FP_INCREMENTAL` O(1) fold instead of the batch recompute.

- **Speedup:** `price_volume` 99ms batch → reduction-fold share (~few ms in-flow). The single biggest reduction
  win. `market_beta` 29→fold, `residual_analysis` 12→fold.
- **Effort:** medium (Rust kernel denom change + anchor wiring for the 2 new sources + the `RunningState`/parity
  test walking the corr-denom-straddle cells cold/boundary/rewind). The std-anchor template (#307) is the proof
  of concept; this is the OLS extension flagged in `price_volume.py:39` as "the queued follow-up to widen this".
- **fp risk:** value-identical → **fp-neutral preferred** (verify cell-for-cell on the straddle names; if any
  b==2 corner shifts, it's a per-group version bump + re-trust on those 3 only). Parity-gated, not a Monday-blind
  flip.

### Lever II — the delta-frame engine hook (`step(delta_frame)`) for stateful + tick-window groups

**Problem:** swing's residual ~15ms and the Tier-1 tick groups' cost is per-minute polars MARSHALING of the whole
trailing ring (`unique`, `filter(minute==max)`, slice scan, output build), NOT fold arithmetic. The group receives
the full ring each minute even though it only needs the new minute's delta.

**The fix:** the capture path already builds `new_frame` (the single new minute) at `process_bars` before pushing
to the ring. Add a shared group hook `step(delta_frame)` alongside `compute_latest(full_buffer)`, with a
`seed(buffer)` on the first minute / restart (the `RunningState` contract already gives the cold-start guard).
ONE engine change; beneficiaries: `swing` (15.5→~2ms), `subminute_gap_fano`, `size_entropy`, `print_hhi`,
`microstructure_burst` (the marshal cost), and it generalizes the StatefulEngine's existing per-minute fold to the
tick-window kinds.

- **Speedup:** swing ~13ms, the 4 tick groups' marshal share each. Value-identical, fp-neutral.
- **Effort:** medium-high (shared engine hook + per-group `step` + the `RunningState` parity test per group — the
  swing template is the proof). The biggest STRUCTURAL win after the centered-denom kernel.

### Lever III — the `subminute_gap_fano` resident tick-ring (the slowest per-symbol group)

Covered in §2a. After Lever II gives it the delta frame, carry the per-minute gap-Fano series in a 60-deep ring
and fold the windowed mean O(1). MEASURE the Rust-vs-polars marshal first (#324: gap_fano WON Rust, the cheap
group-bys LOST). This is the largest single per-symbol latency item once the reduction lever is realized.

## 4. The standing gate (so a new feature can't silently regress) — from #334, re-affirmed

#334 proposed the gate that would have caught swing's whole-buffer re-scan. Re-affirmed as the durable mechanism
that makes "fast and cheap by construction" CHECKABLE: (1) a required `state_kind` declaration per group enforced
at registry load; (2) the "no un-overridden `compute_latest` on an unbounded buffer" assertion in
`test_fp_latency_budget.py`; (3) `optimal_design` + `on_optimal` fields in the latency JSON so the dashboard
surfaces drift; (4) the `RunningState` contract + parity test required for any held-state group. The Lead
operationalizes; this roadmap's per-group `state_kind` column (§2) is the seed data.

## 5. Effort / fp-risk summary

| item | speedup | effort | fp risk |
|---|---|---|---|
| `FP_INCREMENTAL` flip (15 armed) | 15 reduction groups → O(1) live | ~0 (built, soak-cleared) | parity-gated, Lead/Monday |
| `FP_SWING_STATEFUL` flip | swing 26→15.5ms | ~0 (built) | value-identical, fp-unchanged |
| **Lever I — centered-denom kernel** | price_volume 99→fold; +market_beta, +residual_analysis | medium | value-identical (fp-neutral preferred); parity-gated |
| **Lever II — delta-frame hook** | swing 15.5→2ms + 4 tick groups' marshal | medium-high | value-identical, fp-neutral |
| **Lever III — gap_fano tick-ring** | subminute_gap_fano 48→~few ms | medium (measure-first) | value-identical, fp-neutral |
| longest_streak carried-state | momentum_run partial | low-medium | value-identical |
| size_entropy / print_hhi carried-counts | 22ms / 11ms → O(1) | low (×2) | value-identical |
| StatefulGroup profiler fix (JSON) | clarity (un-flags price_returns) | low | none (measurement) |

## 6. RANKED implementation backlog (Lead sequences from here — biggest live latency win first)

1. **`FP_INCREMENTAL` flip (15 armed) + `FP_SWING_STATEFUL` flip** — already built/soak-cleared, the single
   largest live lever, Monday. (Not new work; the gate is the Lead's flip.)
2. **Lever I — centered-denom Rust OLS/corr kernel** ⭐ — unparks `price_volume` (99ms, the #1 standalone) +
   `market_beta` + `residual_analysis` onto the O(1) fold. Highest NEW engineering leverage; value-identical,
   parity-gated. Owner: RustIncremental (extends #307's std-anchor + the `assemble_canonical` kernel).
3. **Lever II — shared `step(delta_frame)` engine hook** — swing → ~2ms + the 4 tick-window groups' marshal; one
   change, many beneficiaries; the structural realization of the incremental-state mandate.
4. **Lever III — `subminute_gap_fano` resident tick-ring** (measure-first per #324) — the slowest per-symbol
   group, 48→~few ms. Depends on Lever II's delta frame.
5. **`size_entropy` + `print_hhi` carried-count folds** (low effort ×2) + **`momentum_run.longest_streak`
   carried run-length state** — the cleanest small value-identical wins; can be done independently of the levers.
   `residual_skew` is Rust-or-leave (window-local OLS 3rd moment, assessed marginal).

**Quick win ready-to-implement (noted, NOT built here):** the StatefulGroup profiler-twin fix in
`latency_expectations.py` — profile through `StatefulEngine.step()` not `compute_latest` — is a measurement-only
change that stops the dashboard mis-flagging `price_returns` (42ms) as the #5 slowest when its live path is O(1).
Value-identical, no fp impact; a clarity win the Lead can sequence cheaply.

---

**Top-5 highest-leverage items (the answer to Ben's directive):**
1. Centered-denom Rust OLS/corr kernel → unparks `price_volume` 99ms (+ market_beta, residual_analysis).
2. Delta-frame `step()` engine hook → swing 15.5→2ms + the 4 tick-window groups.
3. `subminute_gap_fano` resident tick-ring → 48→~few ms (the slowest per-symbol group).
4. `FP_INCREMENTAL` (15 armed) + `FP_SWING_STATEFUL` flips → the built Monday lever.
5. `size_entropy`/`print_hhi` carried-counts + `longest_streak` carried run-length → the cheap value-identical wins.
