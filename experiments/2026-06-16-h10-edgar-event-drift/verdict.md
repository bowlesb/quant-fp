# H10 Verdict: EDGAR 8-K / Form-4 Event Drift

**Date:** 2026-06-16

## 8-K: KEEP (1–5d horizon)

The 8-K event cohort clears the canary at 1d, 3d, and 5d, AND survives per-symbol-demean
at all three horizons:

| Horizon | Alpha_demean% | t_demean | Canary cleared? |
|---------|--------------|----------|-----------------|
| 1d | +2.95 | 1.97 | YES |
| 3d | +5.69 | 3.05 | YES |
| 5d | +5.53 | 2.96 | YES |

The 10d horizon fails the demean test (collapses to -1.7%) — it is a size/style artifact.

**VERDICT: KEEP — 8-K event drift at 1–5d horizon is a real cross-sectional signal.**

## Form-4: KILL

Form-4 shows statistically significant NEGATIVE raw alpha that fully evaporates under
per-symbol-demean at every horizon (t-stats 0.09, -0.11, -1.36, -0.20). This is a
composition artifact (Form-4 filers skew toward underperforming stocks in the current
universe), not event-driven drift. Without buy/sell direction, Form-4 has no predictive
content.

**VERDICT: KILL — Form-4 "insider-ACTIVITY" signal is a style bias, not event drift.**

## Power note

- 8-K: ~11,500–11,900 observations per horizon, across 120–122 event dates. Adequate
  power for the day-clustered t (n_dates ≥ 118 clusters). Signal is stable and consistent.
- Form-4: ~15,375–15,951 observations, well-powered. The null result is reliable.

## Backfill-timestamp caveat

All `available_at` are `submissions_accepted` quality (SEC acceptance timestamp, not
live-feed time). For daily-horizon studies this is acceptable — intraday timing errors
of minutes to hours cannot cause look-ahead over multi-day holding periods. The D+1
conservative entry rule provides additional protection. This is NOT suitable for
intraday studies without better timestamp data.

## Next steps (if KEEP is confirmed)

1. **Item-code granularity**: parse 8-K XML index to split by item type (2.02 earnings,
   8.01 bankruptcy/restructuring, 5.02 management change). The pooled 8-K signal likely
   concentrates in 2–3 item types.
2. **Direction for Form-4**: parse raw Form-4 XML for transaction type (P=purchase, S=sale).
   Insider-BUY days may revive the signal.
3. **Cost gate**: at D+1 close entry, round-trip cost ~6 bps vs alpha of +295 bps at 1d.
   Cost is a rounding error — the cost wall is non-binding as designed.
4. **Walk-forward validation**: split the 6-month window 50/50 (fit / OOS) for a first
   out-of-sample check before committing capital.
5. **Survivorship**: add delisted names for a more honest out-of-sample estimate.
