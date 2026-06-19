# Session warmup — the decisive standard for pre-open data parity

> Status: STANDARD (2026-06-14). Resolves the market-open lookback divergence between real-time and
> backfill. Implemented in `quantlib/features/session.py`; enforced by the validation ledger.

## The problem

Many features depend on the last 10 / 30 / 60+ minutes. At the open, real-time may not have the full
lookback (we only have what we captured since the stream started), while backfill — recomputed from the
settled tape — could have *more* (it can reach arbitrarily far back). If the two paths see different
pre-open data, the same feature computes different values, and the train/serve gap is real but
invisible. Pre-market activity is sometimes large, so the lazy fix — discard pre-market on both sides —
throws away signal and is rejected.

## The decisive standard

**Anchor both paths' lookback to the same fixed pre-open time, and confine bets/validation to RTH.**

1. **Warmup anchor = 90 minutes before the 09:30 ET open = 08:00 ET** (`WARMUP_MINUTES_BEFORE_OPEN`).
   This is a documented, fixed contract — not a heuristic.
2. **Live capture** subscribes and begins buffering at the anchor (08:00 ET), every session. It
   therefore incorporates pre-market activity from 08:00 onward.
3. **Backfill** seeds its rolling windows from the **same** anchor and reaches **no further back**. It
   must not use 07:00 or previous-session minutes the live buffer never had.
4. **Bets — and the validation ledger's grading — are confined to RTH** (`rth_mask`, 09:30–16:00 ET).

### Why this guarantees parity (by construction)

A time-based window `(T−w, T]` evaluated at an RTH minute `T` selects exactly the minutes present since
the anchor. If both paths share the anchor (08:00) and the same tape, the set of minutes inside every
window is identical cell-for-cell — so the feature value is identical. Parity holds across the
pre-market boundary precisely because both sides incorporate pre-market the *same* way, rather than one
side discarding it. This is a guarantee, not a tolerance.

### Warmth vs parity (an honest distinction)

- **Windows ≤ 90m** (the bulk: 5/10/15/30/45/60m) are **fully warm at 09:30** — their entire lookback
  fits inside [08:00, 09:30]. Parity AND completeness both hold from the open.
- **Windows > 90m** (180m, 200m SMA, 240m levels) are **still warming** early in RTH: their window
  reaches before 08:00, so it is truncated to since-08:00 on **both** sides. They remain
  **parity-consistent** (both sides agree) but are **partial** until enough RTH elapses
  (e.g. a 240m feature is fully warm at 12:00 ET). The ledger treats their early-session cells as
  warmup (not graded against the value floor; see `nan_policy="warmup"`).

We accept the long-window warmup because we [prioritize ticker breadth over temporal
depth](VALIDATION_LEDGER.md) — short-horizon, broad-cross-section signal is the target, and those
features are warm at the open. If a longer warmup is ever wanted, raise `WARMUP_MINUTES_BEFORE_OPEN`
to the max feature window (240 → 05:30 ET anchor); the only hard requirement for parity is that live
capture and backfill use the **same** anchor.

## First-pass simplification (explicit)

Bets are taken **only during regular trading hours**. This is a deliberate first-pass simplification:
it means a feature only needs to be warm and comparable *inside* the session, so the pre-open warmup
region never has to be tradeable — it exists solely to fill the lookback. The validation ledger
mirrors this by filtering to `rth_mask` before grading, keeping the warmup out of the trust score.

## What still must be enforced outside this module

`session.py` defines the contract and the RTH scope. Two operational pieces enforce the anchor and live
in `services/` (tracked separately):
- the ingestor must **subscribe before 08:00 ET** so the pre-market warmup is actually captured;
- the backfiller must **bound each session's window at the 08:00 anchor**, not pull an unbounded
  trailing range, so backfill can't see pre-anchor minutes live never had.
Until both are verified, long-window features remain `validating` rather than `certified`.

## Warm-start `populated` self-check (the cold-start-eliminator)

Anchoring the lookback (above) keeps live==backfill *parity* safe across the warmup, but a cold relaunch
still EMITS under-warmed partial values on the bus for the first `window` minutes of streaming. The
warm-start path (`FP_WARM_START=1`, `capture.warm_start_ring`) eliminates that window by rehydrating the
trailing ring from the session's already-settled bars (`backfill_bars` = Alpaca historical RAW = the same
unadjusted SIP tape the stream delivers) BEFORE the first live minute, so every windowed feature is full
from minute one.

To make that warm start **robust and self-checking** (rather than silently under-warming if the seed
fails), the increment abstraction has a first-class **`populated`** concept:

- **`WindowedSumState.populated(window)`** — a window is `populated` when the absorbed history reaches a
  full `window` minutes behind the latest minute (`observed_span_minutes() >= window`). Tracked from the
  first/last folded epoch, so it survives the memory `trim` that evicts buffered minutes past the longest
  window.
- **`IncrementalEngine.assert_populated(buffer)` / `WindowedSumState.assert_populated(available_span)`** —
  called right after the warm-start seed. It enforces a **three-way distinction**:
  1. **FULL** (`observed_span >= window`) → the state absorbed its full depth → assert passes.
  2. **LEGITIMATELY not-yet-full** — the seed buffer ITSELF carried fewer than `window` minutes of history
     (newly-listed ticker, first day of data, a genuine gap: `available_span < window`) → the window is
     correctly not populated → **no raise** (it emits partial/NaN as today).
  3. **warm-start FAILED** — the seed buffer HAD `>= window` minutes but the state only absorbed a short
     span (data present, not absorbed: a schema/shape mismatch, a dropped slot) → **raises
     `WarmStartUnderfilled`** (fail-fast, per CLAUDE.md "let errors raise").

  The assert fires ONLY on arm 3 (`available_span >= window` but `observed_span < window`). Because the
  engine seeds from exactly the buffer it then asserts against, a successful seed has
  `observed_span == available_span`, so any short-fall is unambiguously a failed absorb. The assert runs
  once, on the warm-start seed, then clears (later resyncs — a genuinely-new ticker, a daily resync — are
  normal operation, not re-asserted). The `populated`/assert additions are **state metadata + a check
  only**: they touch no running sum, so they are **not a fingerprint change** (proved byte-identical by
  `test_fp_warm_start.py::test_populated_assert_is_value_neutral` and the full incremental/twin/parity
  suite).

### The 7-col / 13-col warm-start ShapeError (fixed)

`warm_start_ring` seeds a 7-column BAR-ONLY ring (`backfill_bars`), but live capture pushes 13-column
TICK-ENRICHED frames (the 6 `TICK_COLUMNS` on top of OHLCV). `MinuteRing.materialize()` concatenated the
per-minute slots; mixing 7-col seed slots with 13-col live slots raised a polars **ShapeError** that
crashed shard workers — which is why `FP_WARM_START` defaulted OFF and every relaunch (incl. Monday's)
started COLD. **Fix:** `materialize()` / `last_minutes()` concat with `how="diagonal"`, which null-fills
the seed minutes' tick columns. This is **parity-correct**: a settled premarket bar carries no tick
enrichment, so its tick columns are null in backfill's `minute_agg` too — the warm ring now holds exactly
the rows the live path would have accumulated (honest "not collected", not a fabricated zero). For a
homogeneous ring the diagonal concat is byte-identical to the plain concat (proved by
`test_warm_start_tick_enriched_no_shape_error` + the existing warm-start parity gate).

### Residual: session-cumulative groups and the day boundary

The `populated` assert covers the **windowed reduction** groups (the `IncrementalEngine`'s declared
windows). The **session-cumulative** groups (`swing`, `runner_state`, `gap_fill_state`, `dumper_state`)
fold the WHOLE session since the UTC day boundary, so their warm-start completeness depends on the seed
reaching the first collected bar of the day, NOT just the longest window. The warm-start SOURCE already
fetches the full UTC day (`backfill_bars` pulls `00:00–23:59`), so the data IS available; the only cap is
the ring `depth = window` (`DEFAULT_BUFFER_MINUTES`). If the deployed `window` is shorter than the
premarket-inclusive session (~720m premarket-open-to-close), these groups stay partial until enough RTH
elapses and keep `nan_policy="warmup"` (RTH-excluded from grading). Deepening the map ring to the day
boundary so they become `populated` since the open is a **separate, memory-/latency-sensitive change**
left out of this cycle to land the warm-start fix safely; tracked as a follow-up. (The `populated`
concept is generalizable to those groups' state once the ring reaches the boundary.)
