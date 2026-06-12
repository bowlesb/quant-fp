# Opening-range breakout — clean death (explorer-shapes, 2026-06-12)

**Status: REFUTED / KILLED (Lead verdict accepted).** Real micro-effect, uneconomic at any realistic cost.

## 1. Hypothesis (pre-registered)
A name that breaks above its 09:30–10:00 high (or below its low) continues in the breakout direction
for the rest of the session (classic opening-range breakout). A single-name time-series signal — a
different axis than cross-sectional ranking. Pre-registered prior: ~30% (ORB is folklore-popular but
well-arbitraged in liquid names and notoriously cost-sensitive).

## 2. Exploration
`research.common_daily_session_price` helper, 1,213 symbols, 634 dates. Label `ten_to_close` =
close_1600/px_1000 − 1. Signal: break-up if the 10:00 mark ≥ the first-30-min high, break-down if ≤ the
low; position-in-range. Cohort means + t-stats by break direction; long-only up-break book net-of-cost
sweep (1.4 / 2.0 / 2.7 bps).

## 3. Results

| cohort | mean ten_to_close | t | n |
|---|---|---|---|
| break UP | +0.00109 | +11.15 | 50,070 |
| break DOWN | −0.00005 | −0.57 | 45,959 |
| no break | +0.00029 | — | 597,000 |

position-in-range vs ten_to_close correlation: +0.0027 (nil).

Long-only up-break book net Sharpe: −0.064 @1.4bps / −0.175 @2.0bps / −0.303 @2.7bps.

## 4. Verdict + interpretation
**REFUTED.** Break-up continuation is statistically real (t=11) but tiny (+11bps gross) — smaller than
the spread, so the long-only book is net-NEGATIVE at every realistic cost. Break-down carries nothing;
position-in-range is uninformative. Another real-but-uneconomic price effect, consistent with the older
academic ORB literature (Holmberg et al.: basic ORB rules unprofitable once intraday costs are applied).

## 5. Next steps
- Killed; not reopening. **One documented revival path** (declined now): the 2024 Concretum result
  (Sharpe 2.4 net) survives only by restricting to "stocks in play" (abnormal-volume names). My run had
  `or_vol_z` computed but UNUSED in the book. If ORB is ever revisited, gate the up-break book on high
  first-30-min volume. Logged for completeness; not pursued (the unconditional version is a clean kill).
