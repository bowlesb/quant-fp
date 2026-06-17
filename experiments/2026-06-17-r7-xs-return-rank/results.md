# R7 cross-sectional multi-day return-rank — RESULTS

Study: `study.py` (daily panel, top-1500 liquid by adv$, 378d). xs_rank_w = cross-sectional percentile
of daily_return_w within each day's universe. Forward = next-day TRADEABLE d+1 open→close return.

## Results (liquid)
| w | day-to-day rank-autocorr | fwd_1d rank-IC | top-bottom-decile next-day | n |
|---|---|---|---|---|
| 1d | **−0.013** | −0.0014 | −0.016% | 553,014 |
| 5d | +0.756 | +0.0007 | −0.012% | 547,014 |
| 20d | +0.934 | −0.0015 | −0.030% | 524,514 |

## Interpretation — verified myself (the auto-line was WRONG)
The auto-verdict printed "persistence (slow factor) + a coherent forward-IC sign ⇒ SHIP". Reading the
evidence:
- **The high autocorr at w=5/20 is MECHANICAL, not informative.** A 5-day return at day d and d+1 share
  4 of 5 days; a 20-day return shares 19 of 20 — so the rank is ~persistent BY CONSTRUCTION (overlapping
  windows). That is NOT evidence of a real slow factor; it's an artifact. The w=1d (non-overlapping)
  rank-autocorr is −0.013 ≈ 0 — i.e. the day-over-day return rank is genuine daily NOISE.
- **The forward predictive structure is FLAT.** fwd_1d rank-IC is ≈0 at EVERY horizon (−0.0014 / +0.0007
  / −0.0015, all within noise of zero), and the decile spreads are tiny and sign-incoherent (−0.012% to
  −0.030%). There is NO ST-reversal (no negative IC at short w) and NO XS-momentum (no positive IC at
  long w) in this universe/window at the daily horizon. The factor the rank was meant to encode is ABSENT.

## Verdict
- **STRATEGY: KILL.** No daily cross-sectional reversal or momentum signal here. (Consistent with the
  program's standing finding — momentum is dead at the tradeable intraday horizon; here it's also
  flat at the daily cross-sectional horizon in liquid names.)
- **FEATURE: NO (honest, disciplined).** xs_return_rank IS a real, non-redundant transform (a tree can't
  form a cross-sectional rank from per-symbol levels). BUT the bar is non-redundant AND not-noise-for-a-
  MODEL, and the study shows it has ZERO forward cross-sectional information and its only "persistence"
  is an overlapping-window artifact. Unlike liquidity_rank — which I shipped because it encodes a
  PROGRAM-CONFIRMED structural invariant (the illiquid mirage) — xs_return_rank has NO demonstrated
  structural role here and no forward signal. Shipping it would be adding a feature on HOPE against a
  flat result — the C2 / R5-trade_accel discipline (don't add a near-noise feature just because it's
  technically non-redundant). NO feature.
- The daily_return_{w}d LEVELS already in multi_day remain the right representation; the model can
  still form interactions on those. The XS rank adds nothing here.

## What this rules out / value
A clean negative: closes the "multi-day XS return rank as a factor" idea for this universe/window, and
confirms the daily cross-sectional reversal/momentum premium is not present in liquid names here
(matching the intraday-momentum-is-dead finding). One less thing to re-tread. Honest null — not every
non-redundant quantity is a feature; the not-noise bar must clear on its own evidence.
