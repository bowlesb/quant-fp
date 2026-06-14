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
