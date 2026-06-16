# H2-RETEST: OFI Orthogonal to vwap_dev — Pre-Registration
**Registered:** 2026-06-16 (before any data run)
**Author:** Explorer subagent
**Prior experiment:** experiments/2026-06-15-ofi-marginal-lift/

## Hypothesis

**H2 (primary):** True OFI (Cont-Kukanov-Stoikov quote-flow imbalance), rolling 15-min and 30-min,
carries rank-IC with forward returns that is ORTHOGONAL to vwap_dev, in the liquid tier (150-250
high-dollar-volume symbols with both trades and quotes), over the full ~20 completed trade+quote days.

Specifically: after cross-sectionally residualizing forward returns on vwap_dev, the marginal
rank-IC of ofi_15_norm (normalized by total volume) is positive (continuation) with magnitude
≥ 0.008 and day-clustered |t| ≥ 2.0.

## Expected Sign and Magnitude per Arm

| Signal | H15 IC | H30 IC | Expected Sign | Expected |t| |
|--------|--------|--------|---------------|------------|
| ofi_15 raw | 0.012-0.020 | 0.010-0.016 | + (continuation) | ≥ 2.5 |
| ofi_15_norm | 0.010-0.018 | 0.008-0.014 | + | ≥ 2.0 |
| ofi_30_norm | 0.008-0.015 | 0.010-0.018 | + | ≥ 2.0 |
| sv_15 (tick-rule) | 0.010-0.016 | 0.008-0.012 | + | ≥ 2.0 |
| vwap_dev | -0.015 to -0.020 | -0.012 to -0.018 | - (reversion) | ≥ 3.0 |
| ofi_15_norm RESIDUAL | 0.008-0.015 | 0.006-0.012 | + | ≥ 2.0 |

**Rationale:** The prior 3-day test found ofi_15 raw rank-IC +0.0185 (t=3.96, continuation).
Extending to 20 days and 150-250 liquid symbols (vs prior ~100 symbols, 3 days) should
power the test adequately (~7,000+ cross-sections). The marginal lift over vwap_dev is the
key question — vwap_dev captures mean-reversion, OFI should capture informed-order-flow
momentum orthogonal to that deviation.

## Confidence

- Standalone OFI signal (continuation): **65%** — consistent with prior 3-day finding
- Orthogonal marginal lift: **45%** — this is the uncertain claim; vwap_dev and OFI may overlap in
  liquidity-demand signal, so the residual could collapse
- Net-of-cost positive: **25%** — even if marginal IC exists, 2-4 bps one-way in liquid names is
  a high bar at 15-30 min horizons

## Kill Criterion (pre-registered)

KILL (do not promote to feature PR) if ANY of:
1. Standalone ofi_15 rank-IC |t| < 2.0 at H=15 (signal not real)
2. Marginal IC on residual |t| < 2.0 at BOTH H=15 and H=30 (not orthogonal)
3. Canary shuffle band overlaps the marginal IC at the relevant horizon
4. Best OFI decile L-S gross bps < measured median spread (net negative before alpha decay)

KEEP (promote to feature PR) if:
1. Marginal IC |t| ≥ 2.5 at H=15 or H=30 AND
2. Clears canary band AND
3. Gross bps > 1x median spread (positive after typical cost)

AMBIGUOUS if partial: |t| in [2.0, 2.5] or gross barely exceeds cost — needs further study.
