# H9 Verdict: KILL

## Decision

**KILL** — no longer-horizon cell turns vwap_dev net-of-cost positive. Every (W, H) cell is deeply negative gross before cost (−12 to −34 bps), turnover stays near 1.0 regardless of horizon, and the losses compound further at H=120 vs H=60. The pre-registered KEEP bar (any cell net > 0 beyond canary, robust to cost stress) is not met by any margin.

## What the data says

The vwap_dev reversion signal at 30-min cross-sections does not accumulate over longer holds — it **reverses into momentum**. The deviation corrects within the first 30 min (captured in H1–H3), and by H=60–120 min the cross-sectional spread in forward returns has flipped negative: the names that were most below VWAP at T are now the worst performers at T+60 and T+120. This is consistent with mean-reversion decaying and momentum dominating at longer intraday horizons — a well-documented microstructure pattern.

The turnover mechanism also failed. The hypothesis required turnover to fall at longer rebalance cadences, amortizing the fixed round-trip cost. Observed turnover is ~0.895 at H=60 and ~0.899 at H=120 — essentially unchanged. Decile composition reshuffles almost completely every period regardless of horizon.

## Implications (per hypothesis.md kill clause)

vwap_dev reversion is now dead at ALL tradeable horizons (15–120 min) under all conditioners tested (H1–H3, H9):
- H1: raw decile L/S → net negative (−2 to −10 bps)
- H2/H3: conditioning (spread-tier, vol-regime, time-of-day) → still net negative
- H9: longer horizons (60, 120 min) → worse, −18 to −39 bps net

This closes the only canary-clearing price signal from the initial sweep. Per the pre-registration, **H6 (vol-conditioner) is de-prioritized** (same conditioner framing, same binding constraint). The hunt pivots to low-turnover, non-price signal families:

**Next step (one line):** Dispatch H4/H5 (event/filing momentum families) — these are directional, non-reversion, and the event signal is orthogonal to the price-microstructure dead end confirmed by H1–H3 + H9.

## Honest caveats

- Power is moderate at 49 days; but the direction is uniformly negative and the t-stats are −1.75 to −2.58 (day-clustered), so the signal is statistically reliable in the negative direction — there is no ambiguity about whether this could be zero.
- The universe (top 300 by dollar-volume) is favorable territory for mean-reversion; if it does not work here, it will not work in less liquid names.
- Rebalance grid assumption (snap to start-of-day + H multiples) is stylized; real execution would stagger, but the direction of the result is robust to entry-time variation.
