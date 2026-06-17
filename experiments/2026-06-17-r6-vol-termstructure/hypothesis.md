# R6 — Volatility TERM-STRUCTURE / regime (vol expanding vs contracting), PRE-REGISTERED

## Idea / non-redundancy
The platform has many vol LEVELS (realized_vol_{w}, parkinson, garman_klass, rogers_satchell,
downside/upside_vol) but NO vol TERM-STRUCTURE — the ratio of short-horizon to long-horizon realized
vol, i.e. whether vol is EXPANDING (short > long) or CONTRACTING. This is the canonical vol-regime /
vol-of-vol quantity and a known conditioner of risk premia. A tree model handles ratios poorly (splits
on thresholds, not ratios of two columns), so an explicit term-structure ratio is genuinely additive
even though both levels exist.

## Pre-registered study (bars, liquid + speculative tiers SEPARATELY, all 378d)
For each symbol-day, per minute compute short-vol (realized vol over last 10m) and long-vol (over last
60m); vol_term = short_vol / long_vol. Measure (this is a FEATURE study — characterize the quantity,
not hunt an edge):
1. DISTRIBUTION: is vol_term well-spread (not degenerate ~1)? median, p10/p90, frac >1 (expanding).
2. PERSISTENCE: is vol_term autocorrelated minute-to-minute (a real regime, not noise)? lag-5 autocorr.
3. STABILITY across tiers: does the expansion/contraction balance differ liquid vs speculative?
4. (light) does vol_term at minute t relate to |return| over the NEXT 30m (does an expanding-vol
   regime precede bigger moves)? — a sanity check that the quantity carries information, not a strategy.

## Falsification / feature decision
- If vol_term is degenerate (~1 always) or pure noise (zero persistence) -> NO feature.
- If it's well-spread + persistent -> SHIP a vol_term_structure feature (short/long realized-vol ratio
  over a couple of horizon pairs), parity-true (deterministic windowed function of close, the
  realized_vol pattern). Non-redundant (ratio, not level), not-noise.

## Parity note
vol_term = realized_vol_short / realized_vol_long, both deterministic windowed stds of returns already
computed parity-true in the volatility group -> the ratio is parity-true by construction. NULL when the
long-vol denominator is ~0 (degenerate flat window) — applying the DataIntegrity-4 relative-guard
lesson from the start.

## Output
A vol_term_structure FEATURE candidate (batch-1f) + the characterization. No strategy claim expected.
