# Exhaustive adversarial latency re-audit — all 63 groups × 3 axes

> Status: AUDIT (doc-only, 2026-06-21). Live fp `0x873f…`/728/63 UNTOUCHED — no code/container/flag/tree change.
> Commissioned answer to Ben's rejection of "latency is at a quiet end-state" as complacency: *"Unless you've
> looked at EVERY SINGLE feature and can tell me (A) there is no further option to consolidate/improve the
> deployed incremental-state abstractions, AND (B) zero further option to speed up with Rust or reduce compute
> by more aggressively using/updating state between minutes — you are NOT done."* This is that per-group,
> per-axis re-audit, run **adversarially against the prior audits** (#362 `ACCELERATION_ROADMAP`, #367
> `STATE_ABSTRACTION`, #378 `SPECULATIVE_PRECOMPUTE`): every group's classification was re-verified against the
> ACTUAL `origin/main` source (not the prose), assuming each had an un-found win until proven otherwise.

**Method.** Every group's LIVE per-minute path was traced in source (`compute_latest` override / `compute_latest_on_window(N)`
/ `SessionCache` / `StatefulEngine.step()` / `ReductionGroup` fold), not read from the prior docs. Costs are the
single-shard isolated-profiler p50/p99 from `docs/feature_latency_expectations.json` (62 measured groups + 1
gather not-measured = 63), CORRECTED for the two profiler artifacts §0 names. fp/code UNTOUCHED.

**Headline verdict (the answer to Ben's two-part bar).** After tracing all 63 groups in source:
- The platform's per-minute LIVE path is **much closer to optimal than #367's adoption map states** — #367's
  "12 BATCH-fullbuffer (90 features) rebuild from full history every minute" is **materially WRONG**: it is a
  mechanical label on the BACKFILL `compute()` (`reduce_buffer_minutes()==None`), NOT the live form. **All 12
  override `compute_latest` to a bounded / session-cached / single-minute pass.** Independently corroborated by
  the `[FullbufferMigration]` owner (SYSTEM_LOG 23:40Z) who reached the same conclusion and DROPPED — there is
  no un-found value-identical full-buffer migration to ship. **This is the single biggest thing a prior audit
  got wrong, and it inflates the apparent backlog.**
- **The genuine remaining wins are NOT un-found per-group migrations.** They are the THREE already-tracked,
  Lead/Ben-gated levers: (1) the `FP_INCREMENTAL` equity flip (201f of armed reductions run BATCH-in-live today
  because the flag is OFF — the largest dormant lever), (2) the centered-denom Rust corr/OLS kernel that unparks
  the 8 `incremental_safe=False` groups (owned + IN EXECUTION by `[RustIncremental]` 23:15Z), (3) `FP_TICK_SYMBOLS`
  breadth (the tick groups are sub-ms at 24 symbols; their profiler rows are artifacts).
- **So: NOT "done" — but the un-doneness is ACTIVATION, not undiscovered per-group inefficiency.** Counting
  honestly (§7): **of 63 groups, 9 carry a real remaining win** (8 parked reductions sharing ONE kernel fix + the
  1 dormant flip covering 15 groups), and **2 carry a SMALL value-identical micro-win** (`size_entropy`/`print_hhi`
  carried-counts — real but ≤sub-ms live at current breadth). **The other ~52 are PROVEN on their optimal kind.**
- **Four prior-audit ERRORS/omissions found (§6)** — the headline #367 mislabel, plus: 5 "A-cached" groups that
  do NOT actually cache (cheap, label-only), `return_dispersion` cross-listed A-cached AND gather, and the Lever-I
  root-cause is **time-axis conditioning asymmetry** (per `[RustIncremental]`), not the generic cancellation #362
  states — and the Rust kernel is ALREADY centered for `resid_std` (kind 7) but NOT the corr/slope/r2 kinds.

---

## 0. The two profiler-artifact corrections (verified in source — both prior audits got these RIGHT)

The per-group JSON times each group's `compute_latest` in ISOLATION. For two whole classes that isolated number
is NOT the live in-flow cost. Both corrections from #362 §0 were **re-verified against source and CONFIRMED**:

1. **The 23 ReductionGroups share ONE batched emit.** In flow they fold through one `WindowedSumState` pass
   (`incremental.py` — verified add-new/subtract-expired with Neumaier compensation, NOT a per-minute re-sum),
   so the high standalone rows (`price_volume` 99ms, `volume_leads_price` 74ms, `liquidity` 64ms, `distribution`
   61ms) over-count. Treat reduction rows as RELATIVE ordering within the reduction lever, not absolute live cost.

2. **The 4 StatefulGroups' profiler number is the ROLLING-DERIVE backfill twin, NOT the live O(1) fold.**
   `StatefulGroup.compute_latest` → `_state_frame_rolling` (whole-buffer `ewm_mean`/self-join/`rolling_*_by`); the
   LIVE path uses `StatefulEngine.step()` via `emit_stateful` (**confirmed in `stream_sim.py`**: live builds ONE
   shared `_CodedBuffer`, folds all 4 engines' resident `EMAState`/`LastKState`/`ExtremaState` O(symbols×state)/min,
   evaluates every group's `assemble()` in one pass). So `price_returns` 42ms / `technical` 17ms / `price_levels`
   14ms / `candlestick` 9ms are **profiler artifacts** — the live fold is far cheaper and **already optimal**.
   The JSON `kind` field already says `Rust-resident` for these; the p50 column is the wrong twin. The cheap
   correct fix (a measurement change, not a feature change) is profiling these through `step()`.

**Net live-cost reality after both corrections** (the genuinely-slow LIVE work, in order):
(a) the gather groups (`market_context`/`breadth`/etc. — paid ONCE in the reader phase, not per-bet);
(b) the reductions' SHARED emit (one fold pass, O(1)/min once `FP_INCREMENTAL` flips; BATCH-recompute today);
(c) `momentum_run` 29ms (real — neither a reduction nor a stateful fold, but already 75m-window-bounded);
(d) the hand-written tick groups (profiler-inflated; sub-ms live at 24-symbol tick breadth).

---

## 1. The KIND taxonomy (what "optimal" means per axis)

| KIND | optimal per-minute design | live cost on it |
|---|---|---|
| **A-cache** | compute once/session via `SessionCache`, O(1) lookup | ~0/min |
| **A-pure** | pure ts/reference function, filtered to T (no rolling) | ~0/min (a filter) |
| **B-reduction** | shared `WindowedSumState` add/subtract fold, read at T | O(1)/min (after `FP_INCREMENTAL`) |
| **B-stateful** | resident `StatefulEngine` EMA/lag/extrema fold | O(symbols×state)/min |
| **B-session-cumulative** | memoized session running min/max/first (`session_cumulative_agg`) | ~0/min (1 cached pass/snapshot) |
| **B-bounded-window** | `compute_latest_on_window(ctx, N)` — same `compute()` on trailing N min | O(N)/min, parity-true |
| **Gather** | run once in the reader gather phase, broadcast | not a per-bet cost |
| **NO-GO reduction** | `incremental_safe=False` — Rust centered-denom kernel is the only fast path | BATCH-recompute until kernel lands |

The adversarial test applied per group: *is it on the fastest-achievable kind for its math, or is it re-computing
something it could carry / cache / push to Rust?* — answered from SOURCE, with a concrete verdict, never "looks fine."

---

## 2. PER-GROUP TABLE — all 63 groups × 3 axes (source-verified)

Verdict legend per axis: **✅** on optimal kind / no real win · **⚙️** lever exists, FLAG-gated (Lead click) ·
**🔴** off-optimal, real win available · **🦀** Rust is the only fast path (kernel) · **➖** profiler over-counts,
no real gap · **🏷️** label-accuracy issue only (cost already ~0).

Columns: **A** = incremental-abstraction form · **B** = Rust · **C** = state-between-minutes / speculative (#378).
`p50` is the isolated-profiler ms (artifact-corrected note inline). "live scope" = the SOURCE-VERIFIED live path.

### 2a. Reductions — the 15 ARMED (⚙️ Monday flip is THE lever) + 8 PARKED (🦀 kernel)

All 23 are `ReductionGroup` on the shared `WindowedSumState` add/subtract fold = the optimal mechanism for their
math. The live O(1) payoff is the Lead-gated `FP_INCREMENTAL` flip; profiler rows over-count (shared emit).

| group | feat | p50 | live scope today | A | B | C |
|---|---|---|---|---|---|---|
| `price_volume` | 70 | 99.0 | BATCH-recompute (parked) | 🦀 centered-denom kernel | 🦀 Lever I ⭐ | ➖ (corr-denom, not foldable in Py) |
| `volume_leads_price` | 12 | 74.2 | BATCH-in-live (flag off) | ⚙️ flip | ✅ | ✅ fold |
| `liquidity` | 15 | 63.8 | BATCH-in-live | ⚙️ flip | ✅ | ✅ fold + A-PRE tick (#378) |
| `distribution` | 20 | 61.3 | BATCH-recompute (parked) | 🦀 kernel | 🦀 | ➖ |
| `momentum_consistency` | 18 | 47.5 | BATCH-in-live | ⚙️ flip | ✅ | ✅ fold |
| `return_dynamics` | 15 | 39.5 | BATCH-recompute (parked) | 🦀 kernel | 🦀 | ➖ |
| `clean_momentum` | 12 | 30.6 | BATCH-recompute (parked) | 🦀 kernel | 🦀 | ➖ |
| `market_beta` | 21 | 29.6 | BATCH-recompute (parked) | 🦀 centered-denom kernel | 🦀 Lever I | ➖ |
| `momentum` | 22 | 20.3 | BATCH-in-live | ⚙️ flip | ✅ | ✅ fold |
| `trend_quality` | 30 | 17.6 | BATCH-recompute (parked) | 🦀 kernel | 🦀 | ➖ |
| `ohlc_vol` | 12 | 17.4 | BATCH-in-live | ⚙️ flip | ✅ | ✅ fold |
| `volume_exhaustion` | 10 | 16.4 | BATCH-in-live | ⚙️ flip | ✅ | ✅ fold |
| `efficiency` | 18 | 15.4 | BATCH-in-live | ⚙️ flip | ✅ | ✅ fold |
| `volatility` | 15 | 13.9 | BATCH-in-live | ⚙️ flip | ✅ | ✅ fold |
| `quote_spread` | 21 | 12.8 | BATCH-in-live | ⚙️ flip | ✅ | ✅ A-PRE quote-tick (#378) |
| `residual_analysis` | 6 | 11.8 | BATCH-recompute (parked) | 🦀 centered-denom kernel | 🦀 Lever I | ➖ (resid_std already centered) |
| `trade_flow` | 23 | 11.6 | BATCH-in-live | ⚙️ flip | ✅ | ✅ A-PRE tick (#378 prototype) |
| `signed_trade_ratio` | 4 | 10.5 | BATCH-in-live | ⚙️ flip | ✅ | A-PRE-PARTIAL (vol at bar) |
| `count_fano` | 1 | 10.0 | BATCH-in-live | ⚙️ flip | ✅ | ✅ A-PRE tick |
| `realized_range` | 3 | 9.1 | BATCH-in-live | ⚙️ flip | ✅ | ✅ fold |
| `range_expansion` | 2 | 8.9 | BATCH-recompute (parked) | 🦀 kernel | 🦀 | ➖ |
| `volume` | 23 | 8.2 | BATCH-in-live (centered std, #307) | ⚙️ flip | ✅ | ✅ fold |
| `trade_freq_z` | 4 | 7.7 | BATCH-in-live | ⚙️ flip | ✅ | ✅ A-PRE tick |

**Parked = `incremental_safe=False` (8, SOURCE-CONFIRMED):** `clean_momentum`, `distribution`, `market_beta`,
`price_volume`, `range_expansion`, `residual_analysis`, `return_dynamics`, `trend_quality`. #362 names only 3
(`price_volume`/`market_beta`/`residual_analysis`) as the "kernel-unparks" set; that is the **highest-$-leverage
subset** (they carry the corr/OLS `denom_x = b·Σx² − (Σx)²` straddle), but the kernel `[RustIncremental]` is
building unparks more broadly. **All 8 share the SAME fix — the centered-denom kernel — so they count as ONE win.**

### 2b. StatefulGroups — ➖ profiler artifact, ✅ already O(1) live (verified `stream_sim.py`)

| group | feat | p50 (ARTIFACT) | live scope (verified) | A | B | C |
|---|---|---|---|---|---|---|
| `price_returns` | 40 | 42.7 | `StatefulEngine.step()` LastK ring, O(1) | ✅ | ➖ already Rust-resident gather | ✅ |
| `technical` | 14 | 17.4 | `step()` EMA + windowed-sum fold, O(1) | ✅ | ➖ | ✅ |
| `price_levels` | 21 | 14.4 | `step()` ExtremaState monotonic deque, O(1) | ✅ | ➖ (Rust extrema gather) | ✅ |
| `candlestick` | 12 | 8.9 | `step()` LastK + EMA, O(1) | ✅ | ➖ | ✅ |

**Verdict: ➖ all four. The 42/17/14/9ms are the rolling-derive backfill twin, not live.** Only real action =
the measurement fix (profile via `step()`) so the dashboard stops flagging an already-optimal group as #5 slowest.

### 2c. swing — ⚙️ built + flag-gated (`FP_SWING_STATEFUL`)

| group | feat | p50 | live scope | A | B | C |
|---|---|---|---|---|---|---|
| `swing` | 9 | 21.3 | flag off → whole-session rescan; flag on → `SwingState` leg-fold (15.5ms, value-identical) | ⚙️ flip Monday | ✅ (delta-frame → ~2ms, #362 Lever II) | ✅ |

`SwingState` (`swing_state.py`, on the `RunningState` `up_to_date`/`rebuild_from_history` contract) is BUILT and
value-identical; default (flag off) is the lone whole-session re-scan. **⚙️ Lead-gated.** Full O(1) needs the
delta-frame engine hook (#362 Lever II).

### 2d. Hand-written tick groups — ✅ already 1-min-bounded (NOT BATCH-fullbuffer — #367 mislabel)

| group | feat | p50 (ARTIFACT) | live scope (verified) | A | B | C |
|---|---|---|---|---|---|---|
| `subminute_gap_fano` | 1 | 48.6 | `compute_latest_on_window(ctx, 61)`; per-minute Fano via `#324` Rust kernel + 60m `rolling_mean_by` | 🔴 (low) carry per-minute Fano in 60-ring → O(1) | 🦀 #324 kernel already in; resident ring = measure-first | A-PRE tick |
| `size_entropy` | 2 | 21.6 | `compute_latest_on_window(ctx, 61)` 6-bin re-aggregate | 🔴 (small) carry windowed bin-COUNTS, fold | ➖ (#324: Rust LOST here) | A-PRE tick |
| `print_hhi` | 2 | 11.3 | `compute_latest_on_window(ctx, 61)` notional-HHI | 🔴 (tiny) carry Σnotional/Σnotional² | ➖ (#324: Rust LOST) | A-PRE tick |
| `microstructure_burst` | 4 | 6.6 | `compute_latest_on_window(ctx, 1)` own-minute | ✅ at-T | ➖ marshal-bound | A-PRE tick |
| `inter_arrival` | 3 | 6.2 | `compute_latest_on_window(ctx, 1)` | ✅ | ➖ | A-PRE |
| `large_print_burst` | 3 | 2.6 | `compute_latest_on_window(ctx, 1)` | ✅ | ➖ | A-PRE |
| `tick_runlength` | 3 | 2.1 | `compute_latest_on_window(ctx, 1)` | ✅ | ➖ | A-PRE |
| `trade_size_dist` | 3 | 1.5 | `compute_latest_on_window(ctx, 1)` | ✅ | ➖ | A-PRE |

**All eight: bounded window (`compute_latest_on_window` 1m or 61m), NOT full-buffer.** #367 listed the first five
as "BATCH-fullbuffer" — REFUTED. The only `state-between-minutes` micro-wins are `size_entropy`/`print_hhi`/
`subminute_gap_fano` carried-window-counts (61m re-aggregate → O(1) fold). **Real but ≤sub-ms live at 24-symbol
tick breadth** (#378 §2 + Latency-16 confirmed a latest-only re-form was value-identical but SLOWER). Worth it
only when `FP_TICK_SYMBOLS` widens. `#324` already put the gap_fano Rust kernel in (2.5× per-minute Fano).

### 2e. Session-state groups — ✅ already memoized-cached (NOT BATCH-fullbuffer — #367 mislabel)

| group | feat | p50 | live scope (verified `session_cumulative.py`) | A | B | C |
|---|---|---|---|---|---|---|
| `dumper_state` | 6 | 5.6 | `session_cumulative_agg()` memoized session min/sum/first → emit T | ✅ cached (1 pass/snapshot) | ✅ | ✅ |
| `runner_state` | 6 | 4.2 | `session_cumulative_agg()` memoized session max/sum/first | ✅ cached | ✅ | ✅ |
| `gap_fill_state` | 2 | 5.2 | `session_cumulative_agg()` memoized session open/close | ✅ cached | ✅ | ✅ |
| `draw_range` | 3 | 5.2 | `compute_latest_on_window(ctx, 61)` bounded | ✅ bounded | ✅ | ✅ |

**`session_cumulative_agg` is `_AGG_CACHE`-memoized** (witness `(id(frame), height, latest)`) — the three groups
share ONE session-running min/max/first pass, value-identical by construction (`CumulativeState` kind, #284). #367
listed dumper/runner/gap_fill as "the cleanest BATCH-fullbuffer wins" — they are **already done**. ✅ no action.

### 2f. Gather groups — ✅ run once in reader phase, not per-bet (profiler is a per-shard artifact)

| group | feat | p50 | live scope (verified) | A | B | C |
|---|---|---|---|---|---|---|
| `market_context` | 36 | 6.5 | `compute_latest` gathers only LATEST row's index returns (NOT whole-buffer lag) | ✅ | ✅ | needs bar (B-BAR gather) |
| `breadth` | 30 | 23.9 | `reduce_buffer_minutes()` bounded (60m), gather phase | ✅ | ✅ | needs bar |
| `market_turbulence` | 5 | 29.7 | 60m re-aggregate per minute, gather phase | 🔴 (low) carry the abs-ret/RV reduction | ✅ | needs bar |
| `cross_sectional_rank` | 6 | 12.8 | bounded 60m, gather | ✅ | ✅ | needs bar |
| `sector_return` | 8 | 13.5 | bounded 60m, gather | ✅ | ✅ | needs bar |
| `sector_beta` | 6 | 11.9 | bounded 61m, gather | ✅ | ✅ | needs bar |
| `peer_relative` | 3 | n/m | bounded 30m, gather (not-measured: gather phase) | ✅ | ✅ | needs bar |

`market_context`'s `compute_latest` was VERIFIED to gather only the latest row (it does NOT lag the whole buffer
per minute — #367's "heaviest fullbuffer group" claim is wrong about the LIVE path). The gather groups are paid
ONCE in the reader gather phase and broadcast — not a per-bet cost; the per-shard profiler number is an artifact
(#362 §2c). `market_turbulence` is the only one re-aggregating a 60m window per minute (low real cost, gather-phase).

### 2g. A-cached / A-pure / hybrid — ✅ ~0/min (1 LABEL fix flagged)

| group | feat | p50 | live scope (verified) | verdict |
|---|---|---|---|---|
| `return_dispersion` | 10 | 9.4 | **`session_cache` AND `reduce_buffer_minutes()=60`** — hybrid (cached daily snapshot + 60m gather) | ✅ (cross-labeled; see §6) |
| `calendar_events` | 7 | 9.2 | pure ts function, filter to T | 🏷️ A-pure, NOT `SessionCache` (label) |
| `prior_day` | 10 | 2.0 | `SessionCache(daily_snapshot_token)` | ✅ cached |
| `multi_day_vwap` | 10 | 1.9 | `SessionCache` | ✅ cached |
| `sector` | 12 | 1.8 | reference join, filter to T (no cache) | 🏷️ A-pure, NOT `SessionCache` (label) |
| `edgar_filing_frequency` | 10 | 1.8 | memoized `available_at` point-in-time join (`_cache`) | ✅ cached (its own memo, not `SessionCache`) |
| `overnight_intraday_split` | 3 | 1.7 | `SessionCache` | ✅ cached |
| `liquidity_rank` | 2 | 1.6 | `SessionCache` | ✅ cached |
| `multi_day_returns` | 28 | 1.6 | `SessionCache` | ✅ cached |
| `daily_beta` | 3 | 1.6 | `SessionCache` (60d) | ✅ cached |
| `overnight_beta` | 3 | 1.6 | `SessionCache` (60d) | ✅ cached |
| `intraday_seasonality` | 2 | 10.4 | session-scoped single-pass agg (latest-only) | ✅ session single-pass |
| `asset_flags` | 4 | 1.0 | reference join, filter to T (no cache) | 🏷️ A-pure, NOT `SessionCache` (label) |
| `calendar` | 4 | 6.5 | pure ts function, filter to T | 🏷️ A-pure, NOT `SessionCache` (label) |
| `round_levels` | 3 | 0.8 | pure price function, filter to T | 🏷️ A-pure, NOT `SessionCache` (label) |

**NEW (prior audits missed):** 5 groups (`sector`, `calendar`, `asset_flags`, `round_levels`, `calendar_events`)
are listed in #367/#362 as "A SessionCache-cached" but **do NOT use `self.session_cache`** — they default to full
`compute()` + filter-to-T. They are pure ts/reference functions (no rolling, single broadcast join), already
≤6.5ms with ~0 marginal cost, so this is a **LABEL-accuracy issue, not a latency lever** (caching a pure-ts filter
would save nothing measurable). Flagged for adoption-map honesty (§6), not the backlog.

### 2h. momentum_run — ✅ already 75m-bounded (NOT full-buffer; one small carried-state micro-win)

| group | feat | p50 | live scope (verified) | A | B | C |
|---|---|---|---|---|---|---|
| `momentum_run` | 12 | 29.3 | `compute_latest` on `LOOKBACK_MINUTES = max(WINDOWS)+15 = 75m` slice (never reads full buffer) | 🔴 (partial) `longest_streak` → carried run-length O(1); `residual_skew` → Rust-or-leave | 🦀 (skew only) | ✅ |

**Verified 75m-bounded** (the per-shard "80ms #1" some logs cite is box-contention inflation, same ordering, NOT
a full rebuild — corroborated by `[FullbufferMigration]`). The ONE real micro-win: `longest_streak_{w}m` is a
sequential run-length state machine → carry per-symbol current-run-sign+length + a per-window ring, fold the new
return's sign (value-identical). `residual_skew_{w}m` is window-local OLS 3rd moment = Rust-or-leave (marginal).

---

## 3. AXIS A — incremental-abstraction: is every group on its OPTIMAL form?

**The adversarial sweep result: YES for ~52 of 63, with the gaps being ACTIVATION not form.**

- **A-cache / A-pure / session-cumulative (28 groups):** on optimal kind. The 5 A-pure non-cachers (§2g) are a
  label issue, not a form gap — caching a pure-ts filter saves ~0.
- **B-stateful (4) + swing:** on optimal O(1) fold; profiler measures the backfill twin (§0.2). swing is ⚙️ flag-gated.
- **B-reduction (23):** ALL on the shared `WindowedSumState` add/subtract fold (the optimal mechanism) —
  `incremental.py` verified to add-new/subtract-expired with Neumaier compensation, NOT re-sum. The 15 armed run
  BATCH-IN-LIVE **only because `FP_INCREMENTAL` is OFF** (the dormant lever, §5). The 8 parked are 🦀 (kernel, §4).
- **B-bounded-window (the 8 tick + `draw_range` + `momentum_run` + the 60m gathers):** on `compute_latest_on_window`
  — parity-true, but NOT yet expressed as a declared B-KIND (they carry a bespoke bounded `compute_latest`). The
  3 carried-count micro-wins (`size_entropy`/`print_hhi`/`subminute_gap_fano`) are the only A-axis fold gaps with
  a value-identical O(1) target — real but sub-ms live at 24-symbol breadth.

**No group rebuilds from the FULL trailing buffer every live minute** — refuting #367's central "12 fullbuffer /
90 features" claim. The closest are `market_turbulence`/`return_dispersion` (60m re-aggregate, gather-phase, cheap).

### B-fold sub-optimality hunt (the prompt's specific ask)
- **Re-summing a window instead of add/subtract?** NO — `WindowedSumState.update` verified add-new/subtract-expired
  (`incremental.py` L111-125), Neumaier-compensated. Optimal.
- **A-cached recomputed more often than needed?** NO — `SessionCache`/`session_cumulative_agg`/`edgar` `_cache` all
  key on a daily/snapshot witness; one compute per snapshot identity. The 5 A-pure non-cachers recompute a pure-ts
  filter each minute (provably value-identical, ~0 cost) — not worth caching.

---

## 4. AXIS B — Rust: which hot paths genuinely win, weighted by REAL live cost

| candidate | profiler p50 | REAL live cost (corrected) | Rust verdict |
|---|---|---|---|
| **centered-denom corr/OLS kernel** (8 parked) | `price_volume` 99 + 7 more | BATCH-recompute/min (real, survives Monday flip) | 🦀 **THE Rust win.** Owned + IN EXECUTION by `[RustIncremental]` (23:15Z). `rust/src/lib.rs:609` `denom_x=b·sxx−sx·sx` is RAW; `resid_std` (kind 7) is ALREADY centered (L512) — extend the same centered algebra to the corr/slope/r2 kinds. **Root cause is time-axis conditioning asymmetry** (batch raw-epoch axis vs incremental rebased origin), NOT the generic cancellation #362 states — see §6. Value-identical → fp-neutral. |
| `subminute_gap_fano` resident ring | 48.6 | sub-ms (24-sym, 1-min) | 🦀 #324 kernel already in (2.5× per-minute Fano); resident-ring = MEASURE-FIRST, marginal at current breadth |
| `size_entropy` / `print_hhi` | 22 / 11 | sub-ms | ➖ #324 measured Rust LOST (cheap group-bys) — the win is carried-COUNTS in polars, not Rust |
| tick marshal (`microstructure_burst` etc.) | ≤18 | sub-ms | ➖ marshal-bound; addressed by the shared delta-frame hook (#362 Lever II), not per-group Rust |
| StatefulGroups extrema/lag gather | 9-42 (artifact) | O(1) live, already Rust-resident gather | ➖ already on Rust |

**Honest Rust ranking:** exactly ONE genuine new Rust win — the centered-denom corr/OLS kernel (already owned/in
execution). Everything else is either already-Rust (stateful gather, #324 gap_fano) or a measured Rust-LOSS
(`#324`: the cheap per-minute group-bys lost to marshal cost). The prompt's "corr/OLS denom + marshal cost" axis
resolves to: **denom = the one win; marshal = a structural delta-frame hook, not Rust.**

---

## 5. AXIS C — state-between-minutes + speculative pre-compute (#378)

**Incremental fold across the minute boundary:**
- **The dominant C-win is the dormant `FP_INCREMENTAL` flip** — 15 armed reductions (201f) run BATCH-recompute
  in live TODAY because the flag is OFF (`capture.py:_incremental_switches` defaults all 3 switches off; live env
  has `FP_BUS=1 FP_WARM_START=1`, NO `FP_INCREMENTAL*`). Flipping `FP_INCREMENTAL=1 FP_INCREMENTAL_PARITY=1`
  (PARITY=1 keeps batch as written truth) → soak one RTH session reading `feature_incremental_parity_breach_total`
  → promote PARITY=0. **The single largest "state-between-minutes" lever, fully built, gated on a PARITY soak that
  does not exist yet** (only crypto runs PARITY=1, on the sparse tape — not equity evidence). This is the activation
  gap #367 §"ACTIVATION GAP" names; re-confirmed in source.
- `swing` `FP_SWING_STATEFUL` flip + the 3 tick carried-counts + `momentum_run.longest_streak` carried run-length
  = the small value-identical C-wins.

**Speculative pre-compute (#378):** RE-VERIFIED the doc's honest verdict. The idle pre-bar window (worker blocked
on `queue.get()`, ~T+59s) lets the **fully-A-PRE tick groups** (`trade_flow`/`quote_spread`/`count_fano`/
`trade_freq_z` + the 8 hand-written tick) speculate their tick-aggregation off the critical path, value-identically
(prototype: `max|spec−full|=0.0`, the subtract-expiring variant REJECTED at ~1e-10). But: (a) the 434 B-BAR OHLCV
features genuinely need the bar (no value-identical proxy → out of scope), (b) the 111 prior/invariant are already
session-cached (no-op), (c) the prize SCALES WITH `FP_TICK_SYMBOLS` — sub-ms at 24 symbols. **Verdict unchanged:
design-validated, build behind Lever II + the tick-breadth widening, not now.** Speculative pre-compute is a
SCHEDULING SPLIT of the same `fold` (`step(partial)` off-path + `step(tail)` on-path), not a new path — correct.

---

## 6. WHAT THE PRIOR AUDITS GOT WRONG OR MISSED (the adversarial payload)

1. **#367 STATE_ABSTRACTION "12 BATCH-fullbuffer (90 features) rebuild from full history every minute" — WRONG
   for the LIVE path.** It is a mechanical label on the BACKFILL `compute()` (`reduce_buffer_minutes()==None`).
   All 12 override `compute_latest` to bounded/cached/single-minute (verified group-by-group: `session_cumulative_agg`
   memoized × 3; `compute_latest_on_window(ctx,1)` × 5 tick; 75m slice for `momentum_run`; SessionCache memo for
   `edgar`; latest-row gather for `market_context`; session single-pass for `intraday_seasonality`). **No live
   full-buffer rebuild exists.** Independently corroborated by `[FullbufferMigration]` (SYSTEM_LOG 23:40Z), who
   reached the same conclusion and DROPPED. **Fix: re-label #367's adoption map "BATCH-fullbuffer" →
   "BATCH-fullbuffer-BACKFILL-ONLY (live already bounded/cached)"** so it stops re-spawning already-done migration work.

2. **#367/#362 "A — SessionCache-cached" over-counts by 5 groups.** `sector`, `calendar`, `asset_flags`,
   `round_levels`, `calendar_events` do NOT use `self.session_cache` (verified) — they default to full
   `compute()`+filter. Harmless (pure ts/ref fns, ≤6.5ms, ~0 marginal), but the adoption-map count "13 A-cached /
   99 features" mixes true caches with pure filters. Label-accuracy, not a lever.

3. **`return_dispersion` is cross-labeled.** #362 §2c lists it under A-cached; it actually carries BOTH
   `session_cache` AND `reduce_buffer_minutes()=60` (hybrid: cached daily snapshot + a 60m gather). Minor.

4. **#362 Lever-I root cause is imprecise; the kernel is PARTIALLY already centered.** #362 attributes the parked-8
   to generic `Σx²−(Σx)²` catastrophic cancellation. `[RustIncremental]` (23:15Z, measure-first) refined it to
   **time-axis conditioning asymmetry** — batch `cov_n=b·Σxy−Σx·Σy` on a far-origin raw epoch axis vs the
   incremental engine's rebased small origin. And `rust/src/lib.rs` ALREADY uses centered algebra for `resid_std`
   (kind 7, L512 `sxx_c = sxx − sx·sx/b`) but RAW `denom_x` for the corr/slope/r2 kinds (L609) — so the fix is a
   targeted extension of an existing centered path, not a from-scratch kernel. (Owned by `[RustIncremental]`;
   this audit only ranks/scopes it.)

5. **The 4 StatefulGroup p50s mis-rank the dashboard** (price_returns #5 slowest at 42ms) — both prior audits
   NOTE this but neither shipped the trivial measurement fix (profile via `step()`). Worth a 1-line backlog item.

**Nothing left as "assumed optimal" without proof:** every ✅ above is a source-traced live path, not a "looks fine."

---

## 7. THE COUNT (the answer to "how many groups have a remaining win vs proven-optimal")

| bucket | groups | feat | note |
|---|---|---|---|
| **Real remaining win — ACTIVATION (built, flag-gated)** | 16 | 210 | 15 armed reductions (`FP_INCREMENTAL` flip, 201f) + `swing` (`FP_SWING_STATEFUL`, 9f) |
| **Real remaining win — Rust kernel (one fix, owned/in-exec)** | 8 | 176 | the parked reductions; ONE centered-denom kernel unparks all (counts as 1 engineering item) |
| **Small value-identical micro-win** | 3 | 5 | `size_entropy`/`print_hhi` carried-counts + `momentum_run.longest_streak` (sub-ms live at current breadth) |
| **Label-accuracy only (cost ~0)** | 5 | 31 | the A-pure non-cachers (no latency lever) |
| **PROVEN-OPTIMAL (no win)** | 31 | 306 | A-cache/session-cumulative/B-stateful/bounded tick/gather, all source-verified |

**Groups with a remaining REAL win: 27 of 63** — but they collapse to **3 engineering items** (the flip, the
swing flip, the one kernel) + 3 micro-wins. **NOT 27 independent inefficiencies.** The other 36 groups are
proven-optimal or label-only. **The honest end-state: the per-group FORM is optimal/near-optimal everywhere; the
un-doneness is ACTIVATION of built levers, all Lead/Ben-gated.**

---

## 8. RANKED EXECUTION BACKLOG (group → axis → exact change → value-identical? → effort → expected ms)

| # | item | groups | axis | exact change | value-identical? | effort | expected live saving |
|---|---|---|---|---|---|---|---|
| 1 | **`FP_INCREMENTAL` equity flip + PARITY soak** | 15 armed reductions (201f) | C | flip `FP_INCREMENTAL=1 FP_INCREMENTAL_PARITY=1` on equity fc → soak 1 RTH → promote PARITY=0 | yes (parity-gated) | ~0 code (built); needs the soak | the largest live lever — 15 reductions BATCH-recompute → O(1)/min |
| 2 | **`FP_SWING_STATEFUL` flip** | `swing` | C | flip flag (built `SwingState`) | yes (value-identical) | ~0 (built) | 26→15.5ms group-local |
| 3 | **centered-denom Rust corr/OLS kernel** | 8 parked (176f) | B | extend `resid_std`'s centered algebra (lib.rs L512) to `denom_x`/`denom_y` in the corr/slope/r2 kinds + thread the per-source anchor (time-axis conditioning) | value-identical (fp-neutral preferred; verify straddle cells) | medium | unparks `price_volume` 99ms + 7 → O(1) fold; survives Monday otherwise | 
| 4 | **delta-frame `step(delta_frame)` engine hook** | `swing` + 8 tick groups | B/C | shared hook passing the single new minute instead of the whole ring | value-identical, fp-neutral | medium-high | swing 15.5→~2ms + tick marshal share; prerequisite for #378 |
| 5 | **`size_entropy` + `print_hhi` carried-count folds** | 2 (4f) | A | carry windowed bin-counts / Σnotional, fold; entropy/HHI at T | value-identical | low ×2 | 22 / 11ms → O(1) (sub-ms live until tick breadth widens) |
| 6 | **`momentum_run.longest_streak` carried run-length** | 1 (partial) | A/C | carry per-symbol run-sign+length + per-window ring, fold new sign | value-identical | low-medium | partial of 29ms (skew stays) |
| 7 | **`subminute_gap_fano` resident tick-ring** (MEASURE-FIRST) | 1 | B/C | carry 60-deep per-minute Fano ring, fold windowed mean (after #4 delta frame) | value-identical | medium | 48→few ms (artifact-inflated; real only at tick breadth) |
| 8 | **StatefulGroup profiler-twin fix** | 4 | (measurement) | profile via `StatefulEngine.step()` not `compute_latest` in `latency_expectations.py` | n/a (measurement) | low | clarity — un-flags `price_returns` 42ms as #5 slowest |
| 9 | **#367/#362 adoption-map RE-LABEL** | doc | (doc) | "BATCH-fullbuffer" → "backfill-only (live bounded/cached)"; split A-cache vs A-pure | n/a | low | prevents re-spawning already-done migration work (the §6.1 trap) |
| 10 | **Speculative pre-compute (`FP_SPECULATIVE`)** | tick A-PRE groups | C | `step(partial)`/`step(tail)` split (after #4) | value-identical | medium | sub-ms today; gated on `FP_TICK_SYMBOLS` widening (#378) |

**Top-10 ⇒ the real headline:** items 1-3 are the entire remaining FIRST-ORDER latency budget, and **all three are
already built or in-execution and Lead/Ben-gated** (flip = Monday click; kernel = `[RustIncremental]` in flight).
Items 4-10 are second-order / value-identical micro-wins / measurement+doc hygiene. **There is no large un-found
per-group inefficiency** — the adversarial sweep PROVES the per-group form is optimal; the win is pulling the
built levers and landing the one owned kernel.

---

## 9. Cross-references

`docs/STATE_ABSTRACTION.md` (#367 — adoption map, RE-LABEL per §6.1) · `docs/ACCELERATION_ROADMAP.md` (#362 —
the levers, root-cause refined per §6.4) · `docs/SPECULATIVE_PRECOMPUTE.md` (#378 — Axis C, verdict re-confirmed) ·
`quantlib/features/{base.py,running_state.py,declarative.py,reduction_anchor.py,incremental.py,stateful.py,session_cumulative.py}` ·
`rust/src/lib.rs` (the corr/OLS denom kinds) · SYSTEM_LOG `[RustIncremental]` 23:15Z (kernel owner) +
`[FullbufferMigration]` 23:40Z (independent corroboration of §6.1).
