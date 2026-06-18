# R5 — Microstructure ACCELERATION → forward returns, PRE-REGISTERED

Ben-flagged lane. Does a violent INCREASE in trade frequency (the rate-of-change / 2nd derivative,
not the level) predict moves at 5m / 30m / multi-day — and does it differ liquid vs speculative?

## Non-redundancy (vs what exists)
- trade_freq_z (F4): the LEVEL z-score of n_trades (is the rate HIGH vs history).
- microstructure_burst: signed run-length / signed volume within a minute.
- NEITHER captures ACCELERATION = the rate of CHANGE of trade frequency. A name can be high-but-flat
  (high z, zero accel) or normal-but-spiking (low z, high accel). Distinct quantity.

## Definition (point-in-time, per symbol-minute)
- trade_accel = (n_trades over last 5 min) / (n_trades over the prior 5 min) - 1  (the 5m-over-5m
  frequency change; >0 = accelerating). Robust ratio form. Also a z-scored variant for the feature.

## Pre-registered study (bars+tick-enriched minute_agg, all available days)
Two tiers, SEPARATELY (the runner/dumper work says microstructure may behave differently by tier):
- LIQUID = top-1500 by adv$. SPECULATIVE = the $2-20 small-cap cohort (the runner/dumper universe).
For each minute with a defined trade_accel, compute forward returns fwd_5m, fwd_30m, fwd_1d (close-to-
close, TRADEABLE entry — book the forward return from the NEXT minute's close, never the signal minute).
Measure:
1. Rank-IC of trade_accel vs fwd_5m / fwd_30m / fwd_1d, per tier, day-clustered t-stat.
2. Decile spread (top-accel minus bottom-accel) forward return, per tier + horizon.
3. SHUFFLE CANARY: permute the accel labels within (day) -> IC ~ 0 band.

## Falsification / feature decision
- STRATEGY KEEP only if a tier shows |IC| t>=2 AND survives the canary AND the decile spread beats a
  realistic cost at that tier's liquidity. (Prior: likely the speculative tier shows the effect and it
  dies to cost/execution — the illiquid-mirage pattern. Honest about that.)
- FEATURE: ship a parity-true trade_accel (rolling/since-open z-score of the trade-frequency change)
  if it's REAL (non-zero IC beyond canary in EITHER tier) + non-redundant (vs trade_freq_z level) +
  not-noise. A feature has the lower bar; even a cost-killed strategy yields the acceleration feature.

## Parity note
trade_accel is a deterministic windowed function of n_trades (already in minute_agg, parity-true) —
a 5m/5m ratio or a rolling z-score of the per-minute change. No new data path, no intraday state
beyond the rolling window -> parity-true by construction (the trade_freq_z pattern).

## Output
KILL/KEEP per tier + a trade_accel FEATURE candidate (batch-1e/1f).
