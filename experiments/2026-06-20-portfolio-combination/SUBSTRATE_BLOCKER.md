# Pre-run SUBSTRATE BLOCKER — the two streams barely overlap (flagged BEFORE running, per Lead)

**Date:** 2026-06-20  The Lead's return-stream framing is methodologically correct. But before running I
checked the substrate, and there is a material data problem the framing didn't anticipate — flagging it
rather than run an underpowered non-test (the Lead: "if you see a problem with the strategy-return-level
framing, flag it before running").

## The problem: the two streams live on almost-disjoint date spans
- **S-WEEKLY** (L1 weekly-reversal) runs on the deep raw-bar panel → **397 weeks, 2018-2025** (#287).
- **S-INTRADAY** (L2 swing_dc + L3 path/vol + L4 quote-dynamics) runs on the **TRUSTED STORE**, which only
  has the dense feature coverage on **46 dates, 2026-04-15..06-12 ≈ 9 calendar weeks** (verified: the
  >=500-sym trusted-coverage window). L4 quote-dynamics additionally needs the quote tape (broad only
  2026-03+).
- **Common weekly span for a stream-level combination = ~9 weeks.**

A cross-stream diversification test needs a LONG common span: to estimate the cross-stream return correlation
reliably, to get a combined-stream Sharpe/NW-t with N≥~30 weeks, and to DISJOINT-replicate (the pre-reg's
pass bar). 9 overlapping weeks cannot do any of these — it would be an underpowered non-test (a 9-point
correlation and a 9-week Sharpe are meaningless; no room to split disjoint).

## Why each intraday leg is substrate-limited (verified)
| leg | deep-history feasible? | why |
|---|---|---|
| L1 weekly-reversal | YES (2018-2025) | raw bars, deep |
| L2 swing_dc magnitude | YES (raw bars) | the kernel runs on minute bars, deep |
| L3 path/vol composite | YES (raw bars) | returns/vol/range computable from raw bars, deep |
| L4 quote-dynamics | **NO** | needs the quote tape; broad only 2026-03+ |

## The honest options (need a steer — this is a data/scope choice, not a methods choice)

### Option A — DEEP S-INTRADAY from RAW BARS, drop L4 (RECOMMENDED)
Build S-INTRADAY = L2 (swing_dc) + L3 (path/vol composite) computed FROM RAW BARS over 2018-2025, so it
shares the deep span with S-WEEKLY. **Drop L4 (quote-dynamics)** from the confirmatory combination — it is
quote-tape-bound to the recent window and cannot go deep. The cross-stream combination then has the full
~397-week common span: real correlation estimate, powered combined Sharpe/NW-t, disjoint replication.
- Pro: the diversification test is actually POWERED and survivorship-honest (L1 haircut applies); it directly
  tests the headline hypothesis (does a weekly stream diversify an intraday stream over a long history).
- Con: L4 is excluded from the headline (it can be a recent-window-only secondary). Cost: L2+L3 from raw bars
  is a build (swing_dc kernel over 2018-2025 is heavy but bounded; same vectorized/streamed pattern as #287).
- Survivorship note: S-INTRADAY from raw bars over 2018-2025 inherits the SAME survivors-only panel — but
  intraday signals on a same-day cross-section are survivorship-robust (no multi-day delisting censoring of
  the holding period), so S-INTRADAY stays clean; only the S-WEEKLY leg carries the haircut.

### Option B — recent-window combination on the ~9-week overlap (NOT recommended)
Run all 4 legs on the 2026-04..06 overlap. Honest but underpowered: 9 weeks can't estimate cross-stream
correlation or a stable Sharpe, and can't disjoint-replicate → an inconclusive non-test. Only worth it as a
quick directional peek, explicitly labelled non-confirmatory.

### Option C — defer the multi-horizon combination until deeper quote history exists
Pairs with the delisting-data track. Slowest; not preferred given Option A is available now.

## My recommendation
**Option A.** It is the only way to run the headline P-MULTI-HORIZON test POWERED, and it's available now
(raw-bar build, no data acquisition). The cost is dropping L4 from the confirmatory headline (kept as a
recent-window secondary). This keeps the diversification hypothesis testable over a real history while
staying survivorship-honest. If the Lead approves, I update the pre-reg's leg set for S-INTRADAY to {L2, L3}
(L4 → recent-window secondary), keep everything else locked, and run. N for BY-FDR stays 4 (2 methods × 2
arms; the arms are now P-MULTI-HORIZON {S-WEEKLY, S-INTRADAY[L2+L3]} and P-INTRADAY-signal-level[L2+L3]).
