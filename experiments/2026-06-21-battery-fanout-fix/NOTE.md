# Battery intraday-panel fan-out fix + #326 re-validation (2026-06-21)

## The bug

`quantlib/battery/build_intraday_panel` joins feature groups on (symbol, minute). A group's data for a
date can be SHARDED across several parquet files, and a (symbol, minute) can recur ACROSS those shard
files (measured in the real store: 2026-06-18 = 7 files/group, ~11,118 cross-file duplicate keys per
group; 06-15 is single-file/clean; 06-17 has 15 files but no key overlap). `_load_features_for_date`
concatenated a group's shards WITHOUT dedup, so the per-group frame carried duplicate keys, and the
inner-join across N groups MULTIPLIED them (k files/group → up to k^N rows per key) — a cartesian
explosion (observed 3.06M panel rows, one symbol_code carrying 1.5M rows over 33 distinct minutes).

## The fix (`quantlib/battery/panel.py`)

Dedup to one row per key at every multi-shard read, BEFORE any join:
- `_load_features_for_date`: `.unique(["symbol","minute"])` per group after the shard concat (the root cause).
- `_load_bars_for_date`: `.unique(["symbol","ts"])` (also fixes `_forward_excess`, which self-joins bars).
- `_load_spread_for_date` + `_attach_realized_half_spread`: same dedup before the left-join.

Test `tests/battery/test_intraday_panel_fanout.py`: a synthetic store with cross-shard duplicate keys in
3 groups × 2 dates. On the BUGGY code the panel explodes to 2,304 rows; on the FIXED code it is exactly
`n_symbols × n_sample_minutes × n_dates` = 12 rows, one per (symbol, minute). The full battery +
magnitude_volume suites pass (33 tests).

## #326 re-validation — directional null SURVIVES quantitatively

Re-ran a representative subset of #326's look-ahead cells on the CORRECTED panel (same window
2026-05-29..06-18, top-200, H=15). Panel: **3,085,192 rows → 26,344 rows** (gradable 3.08M → 21,241) —
the ~117× inflation is gone.

| cell (UP_MOVE_START = directional) | #326 IC | corrected IC | #326 NW t | corrected NW t | verdict |
|---|---|---|---|---|---|
| probe_ret_15m (continuation) | +0.0292 | +0.0292 | 1.38 | 1.38 | FAIL (both) |
| probe_quote_imbalance_15m | +0.0177 | +0.0177 | 1.93 | 1.93 | FAIL (both) |
| probe_realized_vol_30m | −0.0406 | −0.0406 | −1.33 | −1.33 | FAIL (both) |
| probe_spread_bps_15m | −0.0292 | −0.0292 | −1.48 | −1.48 | FAIL (both) |
| composite_up | +0.0064 | +0.0064 | 0.35 | 0.35 | FAIL (both) |
| gbm_up | −0.0186 | −0.0186 | −1.59 | −1.59 | FAIL (both) |

The directional-null verdict holds quantitatively: 0 up_move_start cells clear (net-positive + beats
shuffle + |NW t|≥2). The single-feature rank-IC point estimates are UNCHANGED — because per-timestamp
rank-IC is invariant to row duplication WITHIN a timestamp (duplicating a name's row doesn't change the
cross-sectional rank correlation). So the fan-out distorted **n_rows (3.08M phantom)** and the
**dollar-economics columns** (net/period, breakeven — summed over the phantom rows), NOT the IC/NW-t. The
fwd_max_runup magnitudes DID shrink with the cleaner panel and the inflated net collapsed:

| cell (FWD_MAX_RUNUP = the magnitude artifact) | #326 IC | corrected IC | #326 net | corrected net |
|---|---|---|---|---|
| probe_realized_vol_30m_runup | +0.3346 | +0.2931 | +0.2231 | +0.0354 |
| probe_spread_bps_15m_runup | +0.2358 | +0.1829 | +0.2284 | +0.0302 |

These remain the vol-circularity + positive-only-label grading artifact #326 already flagged (a magnitude
predictor booked through the L/S P&L) — reconfirmed on the clean panel, with the inflated net now
collapsed. The vol-predictability investigation (#331) used a clean raw-bar panel and is unaffected.

CONCLUSION: #326's directional-null verdict was correct and is now QUANTITATIVELY confirmed on the
corrected panel; its distorted artifacts were the row count and the L/S $ columns, not the IC evidence.
The battery intraday panel is now fan-out-safe for the vol-VRP work and any future sweep.
