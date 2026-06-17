# R3 gap-fill vs gap-extend — RESULTS (liquid universe, bars)

Run: `characterize.py` (parallel, 7,682 syms × ~379d). **313,473 gapped-days (|gap|>=2%), 5,908 syms** —
a large, well-powered sample. Output: `gap_events.parquet`. fill_fraction = (close-open)/(prev_close-open):
1.0 = fully filled to prev_close, 0 = no fill, <0 = EXTENDED past the open (momentum).

## Fill fraction by gap direction × liquidity tier (median | % fully-filled | % extended<0 | n)
| | liquid | mid | illiquid |
|---|---|---|---|
| gap UP | **+0.13** (20% filled, 44% extend) | +0.30 (30%) | **+0.48** (34%) |
| gap DOWN | **+0.10** (18%, 45% extend) | +0.06 (23%) | **+0.20** (27%) |

## Fill fraction by gap SIZE (LIQUID only)
| \|gap\| | median fill | n |
|---|---|---|
| 2-5% | +0.13 | 84,767 |
| 5-10% | +0.09 | 14,624 |
| 10-25% | +0.04 | 3,487 |
| 25%+ | +0.01 | 552 |

## Interpretation
- **Gaps PARTIALLY fill, and the fill is strongly LIQUIDITY-MONOTONIC.** Gap-up fill is **3.7×**
  stronger illiquid (+0.48) than liquid (+0.13); gap-down 2× (+0.20 vs +0.10). The mean-reversion
  "edge" lives in the illiquid tier — **the H1/H10/H4 illiquid-mirage pattern AGAIN** (stale-price
  diffusion fills the gap slowly in thin names; liquid names price the gap correctly at the open).
- **In the LIQUID (tradeable) tier the gap barely fills** (median +0.10-0.13) and **44-45% EXTEND**
  (momentum) — the liquid gap is nearly a coin-flip with a slight fill tilt. ~20-26 bps gross on a
  2% gap, concentrated in the untradeable tier and below cost in the liquid tier.
- **Bigger gaps fill LESS** (liquid: 2-5% → +0.13, 25%+ → +0.01) — large gaps are real information
  that does not revert, consistent with R1/R2 (the extreme moves carry news).

## Verdict
- **STRATEGY: KILL.** Same illiquid trap as H1/H10/H4. No liquid-tradeable gap-fill edge: the liquid
  fill is +0.10-0.13 (~20 bps gross) < cost, and the real fill is illiquid-concentrated (unharvestable,
  per the H13 capacity result). 44-45% of liquid gaps extend, so it isn't even a clean reversion signal.
  Adds to the standing meta-finding: every reversion edge (price, event, now gap-fill) is illiquid-
  concentrated and dies in the liquid tier; W11 overnight-beta remains the lone liquid survivor.
- **FEATURE: SHIP — `gap_fill_state`** (batch-1d candidate). A running, point-in-time
  `gap_fill_fraction` = (close_t - session_open)/(prev_close - session_open) is REAL (consistent
  structure across 313k days), parity-true, and NON-redundant: gap_open is the LEVEL, overnight_share
  is the realized split, but nothing encodes the *running fraction of the gap that has filled by minute
  t*. The strong liquidity-monotonicity is itself a signal a model exploits (fill × tier interaction).
  Clears the feature bar (real + parity-true + non-redundant + not-noise) even though the standalone
  strategy dies — exactly the mandate's "a killed strategy still yields a valuable feature." UNIVERSE-
  WIDE (not $2-20), so distinct from F9/dumper_state.

## Next
Build `gap_fill_state` (worktree+PR) as a batch-1d candidate (AFTER batch-1c deploys — do not
co-mingle). Running point-in-time, RTH-only, partitioned by (symbol, ET-session-date); gap_fill_fraction
+ gap_extended flag + gap_size bucket. Honest strategy KILL committed alongside.
