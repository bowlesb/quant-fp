# H11 Method (v2 — timezone-corrected)

## Critical Correction from v1

The original H9/H11-v1 scripts contained an off-by-240-minute timezone bug.
The bars `ts` column is genuine UTC (verified: 08:00 UTC = 04:00 ET pre-market,
13:30:00 UTC = 09:30 ET = NYSE open, confirmed by volume spike at UTC minute 810).
H9/H11-v1 used ET-hour constants (e.g. `RTH_START = 9*60+30 = 570`) as UTC minute values,
placing scoring at 05:30–11:50 UTC (pre-dawn) and anchoring slot-0 at the 09:30 ET open print.
The Gate A tradeable-entry filter (`utc_minute >= 575 = 09:35 ET in ET-minutes`) never fired
because all real RTH bars are at UTC minute >=810.

All v2 constants are TRUE UTC:
- NYSE open (09:30 ET): 13:30 UTC = minute 810
- Tradeable entry (09:35 ET): 13:35 UTC = minute 815
- Last score bar (15:50 ET): 19:50 UTC = minute 1190
- Load end: 22:00 UTC = minute 1320 (captures T+120 for T≤1190)
- Open exclusion end (10:00 ET): 14:00 UTC = minute 840
- Close exclusion start (15:30 ET): 19:30 UTC = minute 1170

## Panel Reuse

Rebuilt from scratch (sandbox /tmp does not persist). Same 300 liquid symbols × 50 trading
days (2026-04-07 to 2026-06-16), same universe selection (top 300 by median daily dollar-volume,
≥90% date coverage). Final: 300 symbols, 6,102,419 RTH rows (larger than v1 because the
corrected load window captures the full 09:30–22:00 UTC range, not the pre-dawn range).

## Signal: vwap_dev_W

`vwap_dev_W = close / trailing_W_min_VWAP − 1`

Trailing VWAP: rolling_sum(close×vol, W) / rolling_sum(vol, W) via polars .over(["symbol","date"]).
min_samples=W means the signal is null for the first W-1 bars of each (symbol, date).
Since RTH starts at minute 810 and most symbols have no RTH pre-period data, the first valid
vwap_dev_W is at minute 810 + (W-1). For W=30: minute 839. For W=60: minute 869.

## Rebalance Grid

Entry at: `810 + slot × H` for slot = 0, 1, 2, ...
First slot at minute 810 has null vwap_dev for all symbols (bar 0, no lookback) → excluded
by the `is_not_null()` filter. First contributing slot is at 870 (H=60) or 930 (H=120).

Gate A (>=815) removes 0 rows from the panel because no observations exist at minute 810.
The 09:30 ET open-print trap does not apply here by construction — the W-bar lookback
requirement prevents any signal from being scored at the open bar itself.

## Momentum L/S Definition

OPPOSITE of H9's reversion leg:
- LONG: top vwap_dev decile (decile 9 = most ABOVE trailing VWAP) — momentum/continuation
- SHORT: bottom vwap_dev decile (decile 0 = most BELOW trailing VWAP)

Gross L/S = mean(long_leg_ret) − mean(short_leg_ret), averaged over (date, slot).

## Gates

**Gate A: Tradeable Entry** — filter base panel to utc_minute >= 815 (09:35 ET).
Structurally a no-op for this signal (no valid vwap_dev at minute 810), but rigorously applied.

**Gate B: Per-Symbol Demean** — subtract each symbol's mean forward return over all its
rebalance-slot observations. Removes survivorship and idiosyncratic drift bias.

**Combined Gate** — apply both Gate A and Gate B simultaneously (the "real test").

**Canary** — 10 seeds, shuffle forward returns within each (date, slot) cross-section.
Canary 95th = canary_mean + 2×canary_std. Signal must beat this band.

**Robustness: Exclude Open+Close** — restrict to utc_minute ∈ [840, 1170) = 10:00–15:30 ET.
Tests whether the edge is a microstructure artifact of open/close.

## Software

polars only (no pyarrow); vectorized group_by/.over() for all cross-sectional ops.
No O(n²) per-minute python loops. Run via sandbox with MEM=12g CPUS=8.
