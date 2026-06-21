# Speculative / anticipatory pre-compute — feature-by-feature evaluation + design

> Status: DESIGN + EVAL + OFFLINE PROTOTYPE (2026-06-21). Doc-only; live fp `0x873f…`/728/63 UNTOUCHED, no
> fc/shard-worker change, no fingerprint change. Evaluates Ben's proposed latency lever: *some features' inputs
> are fully available BEFORE the minute bar arrives at T, so they can be computed in a background pass at ~T−ε;
> when the bar threads at T, emitting is just handing back the pre-computed value (or a tiny final tail-fold).*
> This moves work OFF the critical path (bar-arrival → vector-ready, Ben's latency axis D) rather than merely
> shrinking it — **distinct from, and complementary to, the incremental-state lever** (`docs/STATE_ABSTRACTION.md`,
> `docs/ACCELERATION_ROADMAP.md`). Per Ben: *"it really needs evaluation on a feature-by-feature basis to see if
> it makes sense."* The honest verdict is below: it helps a SPECIFIC, narrow class and is a profiler mirage for
> most of the apparent prize.

Pairs with: `docs/STATE_ABSTRACTION.md` (the seed/fold abstraction this MUST reuse — no disjoint path),
`docs/ACCELERATION_ROADMAP.md` (the per-group optimal-KIND audit + the §3 "delta-frame `step()`" lever this is
adjacent to but distinct from), `quantlib/features/real_capture.py` (the reader: where ticks accumulate live),
`quantlib/features/sharded_capture.py` (`worker_main`: where the bar threads + the idle window lives),
`docs/feature_latency_expectations.json` (the per-group cost source).

---

## 0. The structural opening (why this is even possible — read this first)

The whole idea hinges on **when each input becomes available relative to the minute close T**, and on **the
worker being idle in the pre-bar window.** Both are true in the live path, verified in the source:

1. **Trades & quotes stream in CONTINUOUSLY during the minute.** `real_capture.on_trade`/`on_quote` fire per
   tick and append the raw dict into `trade_buf[minute]`/`quote_buf[minute]` as ticks arrive in real time
   (`real_capture.py` ~L364–396). By ~T+59s the minute's tick set is ~98% accumulated in the reader.
2. **The BAR is the late input.** `on_bar` batches by minute and **dispatches the completed minute only when
   the NEXT minute's first bar arrives** (`real_capture.py` L4–5, L329–345). So the official OHLCV bar for
   minute T lands at ≈ T+60+`feed_delivery` (Alpaca delivery lag), strictly AFTER the minute's ticks are in.
3. **The shard worker is IDLE between minutes**, blocked on `queue.get()` (`sharded_capture.worker_main` L253).
   That idle window — from the last minute's compute finishing until the next bar dispatch — is dead time on
   the critical-path-owning process. **That is the free compute budget speculative pre-compute spends.**

So at ≈T+59s the worker (or the reader, for tick aggregation) *already has ~98% of the inputs for any
tick-derived feature* and is doing nothing. The bar arrival at T+60+δ is the only thing on the critical path
that has to wait. **Speculative pre-compute = do the tick-derived work in that idle window; at the bar, fold in
only the final ~1s of ticks + the official-bar-dependent finish.**

### Why this is NOT the same as the incremental lever (the complementarity Ben asked for)
- **Incremental state** (`STATE_ABSTRACTION.md`) shrinks the *per-minute arithmetic* from O(window) to O(1) by
  carrying running sums and folding one minute. It runs AT the bar, on the critical path. It makes the work
  *smaller*.
- **Speculative pre-compute** moves a slice of that work *earlier in wall-clock time*, off the critical path.
  It does not make the arithmetic smaller; it changes *when* it runs.

They compose: an incremental group's per-minute fold can itself be done speculatively over the partial tick set
at T−ε, with only the tail-fold left at the bar. The two levers multiply (smaller work × done off-path). The
overlap is exactly the **tick-derived reduction groups** (`trade_flow`, `quote_spread`, …): incremental-ready
AND input-ready before the bar. The prototype below is one such group.

---

## 1. Feature-by-feature classification by INPUT-READINESS relative to the minute-close T

Every group classified by *what its inputs are and when they become available*, cross-referenced with the
`STATE_ABSTRACTION.md` adoption map (A-invariant / B-fold / BATCH) and the live-registry latency JSON. Two
top-level classes, with the all-important sub-split inside Class-A.

| Class | meaning re: T | groups | feats |
|---|---|---|---|
| **A-PRE (this-minute ticks)** | inputs = trade/quote ticks of minute T (stream in live during [T,T+60)) — finalizable at T−ε up to the last-1s tail | 14 | 89 |
| **A-PRE (prior / intraday-invariant)** | inputs = prior-minute / prior-day / calendar / sector / daily snapshot — NOT this minute at all; already fully known at T−ε (most already `SessionCache`-cached) | 15 | 111 |
| **B-BAR (current OHLCV)** | needs this minute's official close/high/low/volume — cannot finalize until the bar threads | 27 | 434 |
| **B-BAR (gather: current close/vol)** | universe-gather over this minute's close/volume — needs the bar; runs once in the reader gather phase, not per-bet | 7 | 94 |

(Totals: 14+15+27+7 = 63 groups; 89+111+434+94 = 728 — verified against the live registry.)

**So 29 of 63 groups (200 features) are input-ready before the bar (Class-A-PRE).** But the value is heavily
concentrated and heavily over-stated by the profiler — see §2.

### 1a. The CRITICAL sub-distinction inside the tick groups: pure-tick vs bar-volume-blended
`minute_agg` is **the bar ENRICHED with tick columns** (`quantlib/aggregates.py`): `signed_volume`, `n_trades`,
`mean_spread_bps`, `quote_imbalance`, `mean_bid/ask_size` are **tick-derived (pre-available)**, while `close`
and `volume` come from the **official bar (NOT available until T+60+δ)**. So:

| group | tick inputs (pre-available) | bar inputs (NOT pre-available) | readiness |
|---|---|---|---|
| `trade_flow`, `count_fano`, `trade_freq_z` | n_trades, signed_volume | — | **fully A-PRE** |
| `quote_spread` | mean_spread_bps, quote_imbalance, bid/ask sizes | — | **fully A-PRE** (quote-tick only) |
| `signed_trade_ratio` | signed_volume | **volume** (ratio denominator) | A-PRE-PARTIAL |
| `liquidity` | signed_volume | **close, volume** | A-PRE-PARTIAL |
| `subminute_gap_fano`, `size_entropy`, `print_hhi`, `microstructure_burst`, `inter_arrival`, `tick_runlength`, `trade_size_dist`, `large_print_burst` | raw trade tape (price, size, ts) | — | **fully A-PRE** |

The A-PRE-PARTIAL groups can be speculated up to the bar-volume input, then finalized with the official volume
at the bar — **OR** speculated fully using a **trade-derived volume/close proxy** from the tape (Σtrade size ≈
bar volume; last trade price ≈ close). The proxy is NOT byte-identical to the official SIP bar (Alpaca's bar
volume can differ from summed trade prints — odd lots, corrections, consolidated-vs-SIP), so a proxy-based
speculation would **break parity** unless reconciled to the official bar at T. The safe design (§3) speculates
only the pure-tick portion and finalizes the bar-blended portion at the bar.

### 1b. The prior/intraday-invariant A-PRE groups are mostly ALREADY off the per-minute path
The 15 "prior/invariant" groups (111 feats) are `SessionCache`-cached today (`STATE_ABSTRACTION.md` Class-A):
`sector`, `calendar`, `prior_day`, `daily_beta`, `multi_day_*`, `overnight_*`, `return_dispersion`,
`calendar_events`, `edgar_filing_frequency`, `liquidity_rank`, `asset_flags`, `round_levels`,
`intraday_seasonality`. They are compute-once-per-session, O(1) lookup per minute — their per-minute cost is
already ~0 (≤2ms p50, mostly a broadcast join). **Speculative pre-compute offers them essentially nothing** —
they are already pre-computed, just per-session rather than per-minute. Listing them as A-PRE is correct by the
readiness definition but they are a no-op target. The genuine speculative prize is the **tick groups**.

### 1c. B-BAR — the bulk (434 feats) is genuinely gated on the bar
The 27 OHLCV groups (`price_volume`, `momentum*`, `volatility`, `price_returns`, `swing`, `candlestick`,
`price_levels`, the reductions over close/high/low, the session-state min/max groups, …) all need this minute's
official close/high/low/volume. Their *trailing-window state* is pre-available (and incremental-foldable), but
the *current minute's contribution* is not — it IS the bar. **You cannot finalize a close-return, a realized
range, or a candlestick pattern before the close exists.** A trade-derived OHLC proxy is possible (last
trade=close, max/min trade=high/low, Σsize=volume) but is NOT value-identical to the SIP bar → parity break.
So the B-BAR groups are out of scope for value-identical speculation; their lever is incremental-state +
Rust (the ACCELERATION_ROADMAP), not pre-compute. **This is the honest boundary: ~60% of features (434) the
idea simply does not help.**

---

## 2. Value estimate — weighted by REAL live cost, not profiler artifact (the honest part)

The naive read of the JSON says the A-PRE tick groups are worth ~217ms of p50 (sum). **That number is wrong for
live**, for two independent reasons that gut most of the apparent prize:

1. **The reductions share ONE batched emit + over-count standalone** (`ACCELERATION_ROADMAP` §0.1). `trade_flow`
   11.6ms / `quote_spread` 12.8ms / `liquidity` 63.8ms are timed in isolation (own running-sum build +
   assemble); in flow they fold together through one `WindowedSumState` pass. Their real marginal in-flow share
   is a fraction of the row. `liquidity` 63.8ms is NOT 63.8ms of live critical path.
2. **The trade-window groups see only ~1 MINUTE of trades LIVE, and only ~24 tick-subscribed symbols**
   (`FP_TICK_SYMBOLS` defaults to the liquid canary; widening deferred — SYSTEM_LOG 06-21). The profiler measures
   them on the 245-min trades-on reference frames, so the JSON p50 for `subminute_gap_fano` (48.6ms),
   `size_entropy` (21.6ms), `print_hhi` (11.3ms) is a **profiler artifact** — their live per-minute cost is
   sub-ms (Latency-16 verified this: a latest-only re-form was value-identical but SLOWER, reverted). At the
   current live tick breadth, the microstructure "prize" barely exists.

**Net honest ranking of A-PRE speculative targets (after both corrections):**

| target | JSON p50 | real live cost today | speculative value |
|---|---|---|---|
| `liquidity` / `quote_spread` / `trade_flow` / `signed_trade_ratio` (tick reductions) | 64 / 13 / 12 / 11 ms | small (shared emit; over-counts) | **the real target** — incremental-foldable AND pre-available; the speculative pre-pass overlaps the idle window. Value scales with tick-subscription breadth: small at 24 symbols, MEANINGFUL once `FP_TICK_SYMBOLS` widens to the universe. |
| `subminute_gap_fano` / `size_entropy` / `print_hhi` / `microstructure_burst` / … (hand-written tick) | 2–49 ms | **sub-ms** (1-min trades, ~24 sym) | **artifact — near-zero live value today.** The JSON over-states by 1–2 orders. Do NOT chase on the profiler number (the Latency-16 lesson). Becomes a real target only at full tick breadth. |
| prior/invariant A-PRE (sector/calendar/prior_day/…) | 1–10 ms | ~0 (already `SessionCache`) | **no-op** — already pre-computed per session. |

**Conclusion on value:** the speculative lever's honest payoff is **conditional and forward-looking**: it is
worth building IFF/WHEN `FP_TICK_SYMBOLS` widens toward the universe (the order-flow live-trading gate). At
today's 24-symbol canary the live cost of the tick groups is small and the prize is modest. The right call is
to **design it now, prototype-validate the mechanism + parity (done below), and SEQUENCE the live build behind
the tick-breadth widening** — not to flip it onto a 24-symbol path where it saves sub-ms. This mirrors the
ACCELERATION_ROADMAP discipline: measure real live cost, don't chase profiler rows.

The one place it pays *today*: the tick **reductions** (`trade_flow`/`quote_spread`/`liquidity`/
`signed_trade_ratio`, ~73 feats) run for every subscribed symbol every minute and their per-minute fold + emit
*can* overlap the idle window. Even at 24 symbols that is a real (if small) critical-path reduction, and it is
the natural co-deployment with the Monday `FP_INCREMENTAL` flip (the fold they speculate is the one being
armed).

---

## 3. The mechanism — built ON the existing FeatureState/incremental abstraction (no disjoint path)

Per Ben's state-abstraction mandate (`feedback-incremental-state-abstraction-mandate`): **do NOT build a
parallel speculative path.** Speculative pre-compute is a *scheduling wrapper* around the SAME
`seed`/`fold`/`emit` the incremental abstraction already defines — it changes *when* `fold` runs, not *what* it
computes.

### The two-phase per-minute schedule

```
        T (minute close)        T+59s ≈ T−ε of next dispatch        T+60+δ (bar threads)
        |.......minute ticks streaming into trade_buf/quote_buf......|...bar dispatched...|
                                         |                                   |
                                   PRE-PASS (idle, OFF crit path)      TAIL-FOLD + EMIT (ON crit path)
```

1. **PRE-PASS at ~T−ε (idle window, OFF the critical path).** Over the partial tick set accumulated so far,
   compute the per-minute tick aggregate `minute_agg_partial` (signed_volume/n_trades/spread/… for the ticks in
   `[T, T−ε)`). This is `aggregate_shard_ticks` run on the partial tape — the bulk of the tick-aggregation
   work. Hold it as speculative state. Do NOT advance the window sums speculatively via subtract-expiring (that
   is the parity hazard — see §4); keep the window emit at the bar.
2. **TAIL-FOLD + EMIT at T (bar threads, ON the critical path).** Aggregate ONLY the last-ε ticks (the tail
   delta), add to the speculative partial → the FINAL `minute_agg[T]` (value-identical to aggregating the whole
   minute, see §4). Append `minute_agg[T]` to the buffer and run the group's existing `emit()`/`assemble()`
   over it — for a reduction this is the shared `WindowedSumState` window re-sum/fold, unchanged. For the
   A-PRE-PARTIAL groups (`liquidity`/`signed_trade_ratio`), the official bar `close`/`volume` arrive here and
   are blended in at this step (their bar-dependent term was never speculated).

### Integration points (concrete, reusing existing seams)
- **Where the pre-pass runs:** the natural home is a background thread in the shard worker, triggered by a
  wall-clock timer at ≈ minute_boundary+59s (the worker already knows the minute cadence). It reads the partial
  tick set. *Cleanest variant:* the reader already holds the live-accumulating `trade_buf`/`quote_buf`; a
  pre-pass could aggregate there and ship `minute_agg_partial` with the dispatch. Either way it reuses
  `aggregate_shard_ticks`/`enrich_bars_with_ticks` verbatim — no new aggregation code.
- **The state object:** the speculative partial IS a `FeatureState` in the `seed`/`fold` sense — `seed(history)`
  is the carried window state (already there), `fold(partial_minute)` is the pre-pass, `fold(tail)` is the
  tail-fold, `emit()` is unchanged. The abstraction already expresses this; speculative pre-compute is
  `fold` called in two installments instead of one. **No new KIND, no new emit — a scheduling split of `fold`.**
- **Gating:** an `FP_SPECULATIVE=1` env flag, default OFF, value-identical when on (a timing change only) →
  fingerprint-neutral, same discipline as `FP_INCREMENTAL`/`FP_SWING_STATEFUL`.

### Relationship to ACCELERATION_ROADMAP §3 Lever II (`step(delta_frame)`)
Lever II adds a `step(delta_frame)` hook so a group folds only the *new minute's* frame instead of re-marshaling
the whole ring. Speculative pre-compute is the **temporal complement**: it splits that single `step(minute)`
into `step(partial)` (off-path) + `step(tail)` (on-path). **Build Lever II first** — once a group accepts a
delta frame, splitting that delta at the T−ε boundary is a small scheduling addition, not a new mechanism. They
should land as one workstream (delta-frame hook → speculative split), owned together.

---

## 4. THE HARD CONSTRAINT — parity (value-identical, or it's worthless)

The speculative result MUST be cell-for-cell identical to the at-bar full computation, else live ≠ backfill and
the feature loses trust. The prototype (§5) found the **exact hazard and the exact safe form**:

- **HAZARD (measured, ~1e-10 drift):** advancing the window sums speculatively as `running − expiring +
  partial + tail` is a difference of large near-equal running sums — the SAME catastrophic-cancellation class
  that parks the NO-GO 8 reduction groups (`STATE_ABSTRACTION.md`). The prototype's first variant showed
  `max|spec − full| ≈ 1.5e-10` on `signed_volume_180m`, growing with window depth. **This breaks
  value-identity. Reject this variant.**
- **SAFE FORM (measured, exactly 0.0):** speculate ONLY the per-minute tick aggregation. At the bar, finalize
  `minute_agg[T] = partial + tail`, append it to the buffer, and emit window sums via the SAME batch re-sum /
  fold the reference uses (identical buffer rows, identical summation grouping). Because the only off-path work
  is `aggregate(partial)` and `partial + tail` reconstructs `aggregate(whole-minute)` exactly (the per-minute
  aggregate is an order-stable sum split at a tick boundary), the emitted features are **byte-identical**.
- **Boundary-tick correctness:** ticks are bucketed to `minute_epoch` by exchange ts (`bucket_minute`), so a
  tick that arrives in wall-clock T−ε but carries an exchange ts in the *next* minute is bucketed correctly
  regardless of when the pre-pass ran. The tail-fold re-reads the buffer at T, so any late-arriving in-minute
  tick (the ε-staleness window) is captured. **Documented ε-staleness:** if a tick lands AFTER the tail-fold
  but with an in-minute ts (provider delivery jitter), it would be missed — same risk the existing path already
  carries (the existing path also cuts at dispatch). Speculation does NOT widen this window because the
  tail-fold happens at the SAME instant the non-speculative path would aggregate. **Parity invariant: the
  tail-fold reads the identical tick set the non-speculative aggregation would have read at the bar.**

### Parity testing (the gate, mirroring `test_fp_incremental`)
The single invariant: `speculate(partial); tail_fold(tail) == full_aggregate(partial ∪ tail)` then identical
emit — cell-for-cell, across a full session of folds (no drift accumulation). This is the `fold==reseed`
invariant of `STATE_ABSTRACTION.md` generalized to a sub-minute split. A `test_fp_speculative.py` would assert
`max|spec − full| == 0.0` over a synthetic multi-minute stream at multiple tail cutoffs (T−1s/2s/5s), plus the
live `FP_*PARITY` breach-counter pattern (speculative form computed alongside the batch truth, compared each
minute) for an in-production soak before any PARITY=0 promotion. Same de-risk ladder as the FP_INCREMENTAL flip.

---

## 5. Prototype — measured critical-path win + parity (offline, bounded)

Prototype on **`trade_flow`** (the canonical fully-A-PRE tick reduction: `minute_agg` signed_volume/n_trades,
10 windows + 3 points, 23 features), a faithful standalone replica of the reduction math so the parity claim is
unambiguous. 312 symbols (reference-shard count), ~80 trades/min/symbol, tail = last 1s of the minute.
(`/tmp/spec_precompute_proto.py` + `/tmp/spec_precompute_soak.py`, throwaway per CLAUDE.md.)

### Result — single minute
```
symbols=312  windows=10  feats=23  ~trades/min/sym=80
tail (last 1s) trade fraction: 1.58% of the minute's ticks

FULL at-bar compute (today's critical-path work)   : 0.6668 ms
  speculative pre-pass (idle window, OFF crit path) : 0.3077 ms   <- moved off-path
  tail-fold (the ONLY work AT the bar, ON crit path): 0.4504 ms
  critical-path reduction: 0.6668 -> 0.4504 ms  (32.5% moved off-path)

PARITY  max|spec - full| over all 23 features = 0.000e+00   value-identical: True
```

### Result — 60-minute parity soak (drift accumulation check), multiple tail cutoffs
```
  tail=T-1s : max|spec-full|=0.000e+00  breach-cells=0  value-identical=True
  tail=T-2s : max|spec-full|=0.000e+00  breach-cells=0  value-identical=True
  tail=T-5s : max|spec-full|=0.000e+00  breach-cells=0  value-identical=True
```

### What the prototype establishes (and what it doesn't)
- **Mechanism works + parity is achievable:** the safe form is **exactly value-identical** (0.0, zero breach
  cells, no drift over a session, robust across tail cutoffs). The hard constraint is satisfiable.
- **The hazard is real and was caught:** the first (subtract-expiring) variant drifted ~1e-10 — the
  cancellation class. The doc's safe form is the one to build; the naive one must be rejected. This is the
  single most important engineering finding here.
- **The critical-path win is the tick-aggregation share** (~33% of this group's at-bar work moved off-path).
  But honestly: the absolute number is small (sub-ms here) and, per §2, the live cost of this group at 24-symbol
  tick breadth is already small. The win **scales with tick-subscription breadth** — modest today, meaningful at
  universe-wide tick coverage. The prototype proves the *lever is sound*; §2 proves *when it's worth pulling*.

---

## 6. Verdict + recommendation (the feature-by-feature answer Ben asked for)

**Which features it helps:** the **fully-A-PRE tick groups** — `trade_flow`, `quote_spread`, `count_fano`,
`trade_freq_z` (pure-tick reductions, ~32 feats) and the hand-written tick groups (`subminute_gap_fano`,
`size_entropy`, `print_hhi`, `microstructure_burst`, `inter_arrival`, `tick_runlength`, `trade_size_dist`,
`large_print_burst`, ~21 feats). These are input-ready before the bar and their per-minute fold/aggregation can
overlap the idle window, value-identically.

**Which it helps only partially:** `liquidity`, `signed_trade_ratio` (A-PRE-PARTIAL) — tick part speculable,
`close`/`volume` finalize at the bar (no value-identical proxy for the official SIP bar).

**Which it does NOT help:**
- The **434 B-BAR OHLCV features** (~60% of the platform) — genuinely gated on the official bar; a trade-derived
  OHLC proxy breaks parity. Their lever is incremental-state + Rust, not pre-compute.
- The **111 prior/invariant features** — already `SessionCache`-pre-computed per session; speculation is a no-op.
- The **94 gather features** — need the current minute's universe close/volume (the bar); run once in the reader,
  not a per-bet cost anyway.

**The honesty caveat that dominates the recommendation:** at the current live tick breadth (~24 subscribed
symbols, ~1 min of trades), the tick groups' real live cost is sub-ms — the profiler p50 over-states by 1–2
orders (Latency-16). So the speculative lever saves little *today*. Its payoff is **conditional on
`FP_TICK_SYMBOLS` widening** toward the universe (the order-flow live-trading gate).

**Recommendation:**
1. **DESIGN: validated (this doc).** Mechanism sound, parity achievable + value-identical, hazard identified.
2. **DO NOT build the live path yet** — it would save sub-ms on a 24-symbol path. Sequence it behind two gates:
   (a) the ACCELERATION_ROADMAP §3 Lever II `step(delta_frame)` hook (speculative split is a small addition on
   top of it, not a separate mechanism); (b) the `FP_TICK_SYMBOLS` universe-widening (when an order-flow edge is
   worth trading live). Co-own with the delta-frame workstream.
3. **When built:** ship behind `FP_SPECULATIVE=1` (default OFF, value-identical → fp-neutral), gate the flip on a
   `test_fp_speculative.py` `fold==reseed` parity test + a live PARITY-soak breach-counter session, exactly like
   `FP_INCREMENTAL`. Reject the subtract-expiring window variant; speculate the tick aggregation only.

This is the engineering-honest version of Ben's idea: a real, parity-true lever for a specific narrow class,
whose value is gated on tick breadth — design it now, pull it when the breadth makes it worth more than sub-ms.
