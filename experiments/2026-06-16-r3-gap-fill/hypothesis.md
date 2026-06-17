# R3 — Gap-fill vs gap-extend dynamic (LIQUID universe), PRE-REGISTERED

A liquid-universe, non-smallcap lane (distinct from R1/R2 which are $2-20 extreme-move detectors).

## Idea
A name opens with an overnight GAP (open/prev_close - 1). Two classic regimes:
- GAP-FILL (mean-reversion): price retraces toward the prior close intraday (the gap "fills").
- GAP-EXTEND (momentum): price continues in the gap direction (the gap "runs").
Which dominates, and does it depend on gap SIZE / direction / liquidity tier? The platform has the
gap LEVEL (gap_open) and the realized split (overnight_intraday_split) but NO running
gap-fill-fraction — the point-in-time "how much of the gap has filled by minute t."

## Pre-registered measurements (bars, liquid tier = top-1500 by $vol, all 379d)
For gapped days (|gap_open| >= 2%), liquid only:
- gap_fill_fraction by EOD = (close - open)/(prev_close - open) clipped — 1.0 = fully filled, 0 = no
  fill, <0 = extended past the open AWAY from prev_close.
- median fill fraction + frac fully-filled, split by gap UP vs DOWN and by gap-size bucket.
- does fill differ by liquidity tier (the standing liquid-vs-illiquid question)?

## Falsification / feature decision
- If fill fraction is ~0.5 with no structure (random) -> no regime, NO feature. Honest null.
- If there's a consistent fill/extend tendency (esp. liquid-tradeable + gap-size-conditional) ->
  a running `gap_fill_fraction` feature (point-in-time: (close_t - open)/(prev_close - open)),
  parity-true, non-redundant with gap_open (level) and overnight_share (realized split). Only ship if
  real + non-redundant + not-noise.

## Output
Dual: a (gated) gap-fill/fade strategy read + a gap_fill_fraction FEATURE candidate (batch-1d).
