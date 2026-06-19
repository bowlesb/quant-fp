# Incremental / Cold-Start Warm-Up Trust Survey

**Date:** 2026-06-19
**Author:** TrustSurvey investigation agent (Lead-routed, Ben-requested)
**Scope:** READ-ONLY survey. No code changes. Propose fixes; do not ship.

## The question (Ben, paraphrased)

> When we start collecting features first thing in the morning, we don't have the
> context yet for incremental features to be valid. Do incremental features have an
> init phase where, on init, they don't rely on prior data? Are ALL incremental
> features "safe" from the cold-start / insufficient-warm-up issue? This is a critical
> part of the trust story between real-time and backfill.

## Headline verdict

**The incremental features are cold-start SAFE for the *trust story* (real-time == backfill).
They are NOT automatically safe for *live trading* (a strategy can consume an under-warmed
partial value off the bus).** These are two different concerns and the survey separates them:

1. **Parity (RT vs backfill).** During a cold start, real-time emits the **same value
   backfill emits at the same `(symbol, minute)`** for every incremental group. This holds
   because backfill itself rolls with **no `min_samples`** (`declarative.py:264-269`) — so a
   60m window at minute 5 produces a *partial* value on BOTH sides, and they agree. The live
   path assembles via the **same `assemble_from_long`** as the batch (`capture.py:360-366`),
   making warm-up null/partial handling "byte-identical to the batch." **No incremental group
   diverges from backfill purely because of cold start.** SAFE.

2. **Trust grading.** The cold-start warm-up window is **provably excluded** from the graded
   set by a hard `rth_mask` (09:30 ET) filter applied to the comparison frame *before* any cell
   verdict (`validate.py:346`), plus a clean-day/coverage gate. A cold-start divergence cannot
   falsely DIVERGE a good feature nor falsely TRUST a bad one. SAFE.

3. **Downstream emission (live-trading risk).** **UNSAFE.** Warm-up vectors are published to
   the bus with **no warmup/ready flag and no minute-count metadata** (`codec.py`, `vector.py`),
   and no strategy gates on warm-up state (`strategies/smoke/strategy.py:251-265`). A strategy
   can trade on a finite-but-under-warmed partial value. This is a signal-quality risk, not a
   parity bug.

The one structurally cold-start-*sensitive* family (RT could differ from backfill even at an
in-RTH minute) is the **session-cumulative state groups** (`swing`, `runner_state`,
`gap_fill_state`, `dumper_state`) whose value depends on the *full minute sequence since the
session/day boundary*. A cold relaunch that starts at 08:11 ET misses the 04:00–08:11 bars that
backfill sees, so a cumulative count (e.g. `n_pivots_today`) can differ. They are
**mitigated, not immune**: the pre-09:30 window is RTH-excluded from grading, and the
warm-up-dependent emissions carry `nan_policy="warmup"`. But an in-RTH cumulative read after a
late cold start is a real residual exposure — see UNSAFE rows below.

---

## A. Incremental / stateful feature families and their longest window

Two execution kinds carry running/windowed state:

- **ReductionGroups** (the `IncrementalEngine`, `incremental.py:125`) — windowed sums folded one
  minute at a time. The vast majority of windowed features.
- **StatefulGroups** (the `StatefulEngine`, `stateful.py`) — trailing extrema, time-lags, and
  session-cumulative pivot/run state.

Source frame determines cold-start exposure:
- **`minute_agg`** = the live intraday minute ring → window fills linearly after capture-start.
- **`trades`** = the live trade ring → same linear fill.
- **`daily`** = the **settled 200-day daily frame** → fully populated at capture-start,
  *independent of cold start* (the group reruns its daily features over settled bars each minute).
- **`reference` / snapshot** = static per-day reference → cold-start-independent.

| Group | Source | Longest window | Cold-start exposed? |
|---|---|---|---|
| price_levels | minute_agg | 240m | yes (intraday fill) |
| price_returns / momentum / momentum_run | minute_agg | up to 60m | yes |
| volume / price_volume / volume_exhaustion | minute_agg | 30–60m | yes |
| volatility / realized_range / range_expansion / ohlc_vol | minute_agg | 30–60m | yes |
| technical / efficiency / trend_quality / clean_momentum / residual_analysis | minute_agg | up to 60m | yes |
| momentum_consistency / return_dynamics / return_dispersion | minute_agg | 30–60m | yes |
| distribution / liquidity / signed_trade_ratio / trade_flow / trade_freq_z | minute_agg | window-declared | yes |
| market_beta / market_context / peer_relative / cross_sectional_rank / breadth | minute_agg | intraday | yes (cross-sectional, single-minute or short) |
| inter_arrival / large_print_burst / microstructure_burst / print_hhi / size_entropy / tick_runlength / trade_size_dist | trades | intraday | yes (trade ring fill) |
| **swing** | minute_agg | **session-cumulative (since day boundary)** | **yes — structurally (cumulative)** |
| **runner_state / gap_fill_state / dumper_state** | minute_agg + daily | **session-cumulative** | **yes — structurally (cumulative)** |
| daily_beta (60d) | daily | 60 **days** | no (settled daily frame) |
| overnight_beta (60d) | daily | 60 **days** | no (settled daily frame) |
| liquidity_rank (ADV) | daily | ADV **days** | no (settled daily frame) |
| overnight_intraday_split / multi_day / multi_day_vwap / prior_day / return_dispersion(daily) | daily | multi-day | no (settled daily frame) |

---

## B. The min_periods contract — empty / partial / full

There is **no global `min_periods` gate** in the windowed-sum engine. The running sums
(`WindowedSumState`, `incremental.py:60-122`) hold exactly the minutes folded so far; a partial
window is just "fewer minutes summed." Behavior per state:

- **(a) EMPTY window** → the reduction reads NaN, restored to polars null. Stateful extrema/lag:
  "NaN where the window is empty (warmup / all-absent) restored to Polars null —
  parity-identical to the monotonic-deque fold" (`stateful.py:153`, `:184`). Empty == NaN on
  both sides.
- **(b) PARTIALLY filled** → computes over **whatever data exists** (a real, finite partial
  value), *except* for the three explicit `min_samples`-gated DAILY groups (below) which emit
  NaN until enough DAYS. This is the load-bearing fact: a 60m rolling mean at minute-5 returns
  the 5-minute mean.
- **(c) FULL** → the intended full-window value.

**The only explicit min-count gates** (all on the settled DAILY frame, gating insufficient
*days*, not cold-start minutes):
- `liquidity_rank.py:81` — `rolling_mean(min_samples=MIN_DAYS)`
- `daily_beta.py:105,112,120,127` — `rolling_var/cov/std(min_samples=MIN_PAIRS)`
- `overnight_beta.py:85,92` — `rolling_var/cov(min_samples=MIN_PAIRS)`

**Self-healing vs not:**
- **Self-healing (partial == backfill-partial at same minute):** every `minute_agg`/`trades`
  windowed reduction. Backfill `compute()` uses the identical `rolling_*_by` with no
  `min_samples` (`declarative.py:264-269`), so backfill at minute-5 is *also* a 5-minute
  partial. RT and backfill match; the value just isn't its full-window self yet. The "wrongness"
  is shared and offline-excluded, not a parity break.
- **NOT structurally self-healing (cumulative):** `swing` / `runner_state` / `gap_fill_state` /
  `dumper_state`. Their value is a fold over the *entire* minute sequence since the day boundary.
  A cold start that misses 04:00–08:11 ET means RT's fold started from a different first bar than
  backfill's — a cumulative count/pivot read can differ even at an in-RTH minute. The
  completed-leg emissions are tagged `nan_policy="warmup"` (`swing.py:125,130`).

---

## C. The parity question (the crux): cold RT == backfill?

| Family | Empty emit | Partial emit | Cold RT == backfill? | Warm-up divergence window |
|---|---|---|---|---|
| minute_agg windowed reductions | NaN | partial numeric | **YES — SAFE** (backfill also partial, no min_samples) | none (RT==backfill cell-for-cell at each minute) |
| trades windowed reductions | NaN | partial numeric | **YES — SAFE** (same) | none |
| stateful extrema / time-lag | NaN | partial extremum | **YES — SAFE** (`stateful.py:153,184` parity-identical to deque) | none |
| daily_beta / overnight_beta / liquidity_rank | NaN (min_samples) | NaN until min days | **YES — SAFE** (settled daily frame, source-independent) | none (warm at minute one) |
| other daily-broadcast (multi_day, prior_day, …) | — | — | **YES — SAFE** (settled daily frame) | none |
| **swing / runner_state / gap_fill_state / dumper_state (cumulative)** | null/0 | cumulative-so-far | **CONDITIONAL — UNSAFE on a *late cold start*** | until RT's fold reaches the bar backfill's fold started from (the missing 04:00–08:11 premarket prefix); the pre-09:30 part is RTH-excluded, but an in-RTH cumulative read can still differ |

**Why most groups are SAFE even cold:** the design choice is *symmetric partiality*. Backfill is
the truth, and backfill rolls with no `min_samples`, so "cold real-time" and "cold backfill"
produce the identical partial value at the same minute. Parity holds by construction. The live
assemble is literally the same `assemble_from_long` as the batch (`capture.py:360-366`), and the
faster Rust emit variants are deliberately **NOT used live** because "their warmup representation
is not yet gated against the batch" (`capture.py:363-364`) — a conscious parity guard.

**The one real exposure:** session-cumulative groups depend on the *first bar of the day*. The
known finding "live long-window covers a different bar SET (capture-start ~08:11 ET vs raw 04:00
ET)" bites hardest here. For a pure windowed reduction it's harmless (the window slides off the
missing prefix within `window` minutes). For a *cumulative* fold the missing prefix can leave a
permanent offset for the session. This is the family to scrutinize / add a re-seed for.

---

## D. Trust-grading safety — is the warm-up window excluded? **YES, provably.**

Two independent, both-active mechanisms:

1. **Hard RTH filter (the decisive one).** Every graded cell flows through the joined
   stream-vs-backfill frame that is `.filter(rth_mask(pl.col("minute")))` *before* any verdict
   (`validate.py:342-360`, the filter at **`validate.py:346`**, comment: "bet/validate only
   during RTH — warmup stays out of the grade"). `rth_mask` drops `minute_of_day < 570` (09:30
   ET) (`session.py:61-64`); warm-up runs 08:00–09:29 ET (`session.py:31-32`). So the **entire
   pre-open warm-up is dropped from grading on every day, clean or not**, symmetrically on both
   sides. No code path in `compare_groups` produces a cell/exception/feature_day row from an
   unfiltered frame.

2. **Clean-day / coverage gate (handles the late-capture different-bar-SET case).**
   `symbol_day_cleanliness` (`cleanliness.py:105-191`, re-applies `rth_mask` itself at
   `cleanliness.py:126`) fails a symbol-day on `coverage_frac < 0.90` (`low_coverage`) or
   `max_gap_minutes > 5` (`internal_gap`); contaminated symbol-days are excluded
   (`trust_lifecycle.py:69` grades only over clean symbols; `MIN_CLEAN_SYMBOLS=20` day gate at
   `validation_sweep.py:427`). `gather_coherence` (`cleanliness.py:75-102`, gate at
   `validation_sweep.py:450-469`) skips the fragmented-restart day. `assert_tail_settled` /
   `assert_raw_present` (`validation_sweep.py:134,208`) prevent grading an unsettled backfill.

3. **In-RTH long-window carve-out.** For windows still warming *inside* RTH (e.g. 180/240m), both
   sides truncate identically to since-08:00 (so they agree), and the early-session cells are
   tagged `nan_policy="warmup"` so they're not graded against the value floor
   (`validate.py:243`; `docs/SESSION_WARMUP.md:40-43`).

**Conclusion:** the warm-up window is **provably outside the graded set**. A cold-start partial
value emitted pre-09:30 can never become a mismatch cell, never enter `feature_parity_defect`,
and never push a feature to DIVERGENT or falsely to TRUSTED. The `missing_live` verdict is even
explicitly documented as "incl. legitimate warmup" (`compare.py:21`).

---

## E. Downstream-emission safety (live-trading risk) — **UNSAFE.**

Separate from grading. During warm-up, the emitted feature **vectors** strategies consume off the
bus are **NOT** marked invalid / withheld:

- **No emission gate.** After each minute's compute, vectors are published unconditionally when
  `FP_BUS=1` (`capture.py:389-390,408-412`; `bus_hook.py:63-85`). No minute-count / warmup /
  ready check. `BusPublisher.publish_many` just XADDs (`quantlib/bus/publisher.py:57-68`).
- **No warmup metadata on the wire.** The codec carries only magic, fingerprint, minute, n,
  symbol, floats (`codec.py` `_HEADER_FMT = "<4sQqIH"`); the decoded `FeatureVector` has only
  symbol/minute/array/fingerprint (`vector.py:54-69`). A consumer cannot tell a minute-5 partial
  vector from a warmed one.
- **No consumer gate.** Smoke strategy bets on whatever it polls
  (`strategies/smoke/strategy.py:251-265`). Reversion only skips *NaN* features
  (`strategies/reversion/strategy.py:153`) — a finite-but-partial value passes.
- **Partial lands on the bus.** With only 5 min buffered, the 60m rolling reduction emits the
  finite 5-minute value (no `min_samples`) and it is published. (The `min_samples`-gated daily
  groups and empty windows emit NaN instead.)

**Bottom line for live trading:** during the first ~`window` minutes after each morning
capture-start (or any relaunch), under-warmed finite partials are indistinguishable from warmed
values on the bus, and a strategy will trade on them. The values *match backfill* at that minute
(not a parity bug), but a long-window feature is semantically under-warmed and nothing flags it.
The real mitigation — `FP_WARM_START=1` — is **OFF by default** and currently **crashes** shard
workers (below).

---

## The warm-start mechanism and why it's disabled

- `warm_start_ring` (`capture.py:427-450`) rehydrates the ring from settled trailing bars so a
  restart begins warm. Gated by `FP_WARM_START=1` (`capture.py:417-424`, default OFF), checked at
  `real_capture.py:157-161,234-243`, `sharded_capture.py:234`.
- The engine `seed()` (`incremental.py:424-444`, `stateful.py:672-696`) folds every buffered
  minute through the **same** slice-derive + stateful path used live — parity-safe by construction.
- **THE CRASH (confirmed):** `warm_start_ring` seeds a **BAR-ONLY 7-col** ring
  (`backfill_bars.py:84` → `["symbol","minute","open","close","high","low","volume"]`,
  `BARS_SCHEMA` at `capture.py:31-39`), while live capture pushes **TICK-ENRICHED 13-col** frames
  (`{**BARS_SCHEMA, **TICK_SCHEMA}`, 7 + 6 tick cols, `capture.py:259`, `tick_capture.py:32-34`).
  `MinuteRing.materialize()` does `pl.concat` over slots (`capture.py:180-182`); mixed 7-col/13-col
  slots raise a polars **ShapeError**, crashing shard workers and silently dropping per-symbol
  capture. Documented at `ops/nightly_relaunch.sh:90-94`.
- **The cron relaunches COLD:** `ops/nightly_relaunch.sh:97` — `[ "${WARM_START:-0}" = "1" ] &&
  cmd+=(-e FP_WARM_START=1)`, default `0`. The Monday relaunch does not set it → cold ring. The
  early-morning no-context scenario is LIVE, not hypothetical.
- **No persisted `capture_start`** anywhere (only stderr prints). Store partitions are keyed
  `(group, version, source, date)` + per-row `(symbol, minute)` (`store.py:50`). Trust grading
  cannot read an explicit warm-up boundary — but it doesn't need to, because the RTH cutoff is a
  fixed time-of-day, not a capture-relative one.

---

## Prioritized recommendations (propose; do not ship this cycle)

1. **Fix the 7-col/13-col warm-start crash so the cron can re-enable warm-start.** Seed
   `warm_start_ring` with the enriched schema (`{**BARS_SCHEMA, **TICK_SCHEMA}`, null tick
   columns) or have `MinuteRing.materialize()` null-fill missing columns before `pl.concat`. Then
   flip `WARM_START=1` in `nightly_relaunch.sh`. This collapses the entire cold-start warm-up
   window in one move — every windowed feature is full from minute one, and the cumulative groups
   get their premarket prefix back. **Highest ROI; directly answers Ben's concern.**

2. **Mark warm-up vectors invalid / withheld on the bus (live-trading safety).** Until (1) lands,
   add a minutes-folded count (or a `ready` flag once the deepest window is full) to the emitted
   vector, and have strategies skip un-warmed vectors. Cheapest interim: a consumer-side
   "elapsed minutes since first-seen this session < max_window ⇒ don't bet" gate. This closes the
   one genuine *live* exposure without touching parity.

3. **Re-seed the session-cumulative groups from settled premarket bars on relaunch.** `swing` /
   `runner_state` / `gap_fill_state` / `dumper_state` are the only family that can diverge from
   backfill at an in-RTH minute after a *late* cold start (missing day-prefix). Even with (1),
   confirm the warm-start seed reaches back to the session/day boundary, not just `window`
   minutes — otherwise their cumulative state stays offset. (Pure windowed reductions don't need
   this; they self-heal within `window` minutes.)

## Boundaries honored

Read-only. No store/DB writes, no code changes, no container touch, no fingerprint change. This
doc committed on the current experiments branch (not main).
