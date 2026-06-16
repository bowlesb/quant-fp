# H10 Results: EDGAR 8-K / Form-4 Event Drift

**Run date:** 2026-06-16  
**Universe:** 7,337 symbols, 126 trading dates (2025-12-15 to 2026-06-16)  
**Entry rule:** next trading day after `available_at` UTC date (conservative D+1, close-to-close)  
**All returns in percent (%)**

## 8-K Event Cohort

| Horizon | N_obs | N_dates | Alpha% | t-stat | Canary_p95% | Clears canary? | Alpha_demean% | t_demean |
|---------|-------|---------|--------|--------|-------------|----------------|---------------|----------|
| 1d | 11,887 | 122 | +3.345 | 2.21 | 1.771 | **YES** | +2.948 | 1.97 |
| 3d | 11,694 | 120 | +6.668 | 3.46 | 3.379 | **YES** | +5.685 | 3.05 |
| 5d | 11,502 | 118 | +7.104 | 3.60 | 2.263 | **YES** | +5.532 | 2.96 |
| 10d | 10,934 | 114 | +10.423 | 4.17 | 5.424 | YES | -1.688 | -0.18 |

**Key finding:** 8-K event cohort shows consistently positive cross-sectional alpha vs same-date
controls. Alpha grows monotonically from 1d to 10d in the raw cross-section. The canary is cleared
at ALL horizons. However, the per-symbol-demean test tells a more nuanced story:

- **1d, 3d, 5d:** Alpha survives demean (2.9%, 5.7%, 5.5%) with t-stats 1.97, 3.05, 2.96 —
  the signal is NOT driven by persistently trending stocks. The cross-sectional separation is
  real relative to each symbol's own baseline.
- **10d:** Alpha collapses under demean (-1.7%, t=-0.18). The raw 10.4% at 10d appears to
  reflect survivorship/selection bias: 8-K filers are concentrated in stocks that have been
  trending upward over the 10-week window, so the excess is absorbed by the per-symbol mean.
  The 10d horizon should be treated as NOISE.

**Clean signal window: 1–5 trading days post-filing.**

Canary 10-seed band (raw permutation, p5/p95 across seeds):
- 1d: canary mean ≈ 0.1%, p95 = 1.77% — real alpha 3.35% >> canary
- 3d: canary mean ≈ 0.3%, p95 = 3.38% — real alpha 6.67% >> canary
- 5d: canary mean ≈ 0.1%, p95 = 2.26% — real alpha 7.10% >> canary
- 10d: canary mean ≈ 0.5%, p95 = 5.42% — real alpha 10.42% > canary but demean kills it

## Form-4 Event Cohort

| Horizon | N_obs | N_dates | Alpha% | t-stat | Canary_p95% | Clears canary? | Alpha_demean% | t_demean |
|---------|-------|---------|--------|--------|-------------|----------------|---------------|----------|
| 1d | 15,951 | 122 | -0.639 | -2.12 | 1.389 | NO | +0.028 | 0.09 |
| 3d | 15,671 | 120 | -1.712 | -4.11 | 2.358 | YES | -0.045 | -0.11 |
| 5d | 15,375 | 118 | -2.909 | -8.30 | 0.726 | YES | -0.475 | -1.36 |
| 10d | 14,425 | 113 | -4.159 | -5.77 | 2.406 | YES | -0.131 | -0.20 |

**Key finding:** Form-4 cohorts show strongly NEGATIVE raw cross-sectional alpha (statistically
significant at 3d, 5d, 10d). This appears to be a spurious composition effect: the Form-4
universe is heavily weighted toward smaller, more volatile stocks that tend to underperform
the broad universe (size/value tilt). Under per-symbol-demean, the negative alpha completely
disappears at ALL horizons (t-stats: 0.09, -0.11, -1.36, -0.20 — all effectively zero).

**Form-4 conclusion:** the "signal" is entirely a style/sector bias, not event-driven drift.
Insider-ACTIVITY day has no directional predictive content once you remove symbol-level means.
This is expected given the lack of buy/sell direction; even directional Form-4 studies are
weak in recent years.

## Interpretation note

The 8-K positive alpha at 1–5d is large in absolute terms (+3–7%). This reflects partly a
real event-reaction effect and partly the composition of 8-K filers in the current universe
(not a purely random sample). The demean test substantially reduces it (+2.9–5.5%) but does
NOT eliminate it, suggesting a meaningful signal beyond just "8-K filers are different kinds
of stocks." The residual demean-adjusted signal is 2.9% at 1d and 5.5% at 3–5d, both with
t > 1.97 — not trivially random.

## Caveats

1. **All available_at from submissions_accepted** (backfill quality, not live-feed). Entry
   timing error is seconds-to-hours but irrelevant for D+1 close-to-close returns.
2. **Survivorship:** current universe only; distressed/delisted companies missing.
3. **No item-code granularity** on 8-K: earnings (2.02), restructuring (8.01), management
   change (5.02) all pooled. The positive drift may concentrate in a few item types.
4. **No direction on Form-4**: insider-BUY vs insider-SELL split would change the picture.
5. **Close-to-close D+1 entry**: a real implementation enters at D+1 open (gap risk).
6. **January–June 2026 was a broadly positive market period**: the absolute return levels
   are not out-of-sample estimates.
