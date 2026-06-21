# Alpaca-first investigation — the survivorship bias is SELF-INFLICTED; $0 panel is feasible

**Date:** 2026-06-20  **Verdict (headline):** Ben is right. **Alpaca gives us a survivorship-clean panel for
$0** — the bias is our own `AssetStatus.ACTIVE` filter, NOT an Alpaca data limit. The ONE genuine gap
(precise delisting RETURNS) is handled by a documented proxy, and does NOT justify a paid subscription. **No
spend recommended.**

## What we found (all measured live via the paper Alpaca API; no creds printed)

### 1. The bias is our filter (the smoking gun)
`quantlib/data/raw_backfill.py:198` screens the universe with
`GetAssetsRequest(status=AssetStatus.ACTIVE, ...)` — so delisted names are excluded BY US.
- **ACTIVE US-equity assets: 13,889**
- **INACTIVE (delisted/deregistered) US-equity assets: 19,253** — the delisted universe is right there, free.
- Spot-checks confirm real delisted/acquired names ARE in Alpaca's INACTIVE list: Celgene (CELG), Xilinx
  (XLNX), Maxim (MXIM), Slack (WORK), Wellcare (WCG), Fannie Mae (FNMA), Signature Bank (SBNY).

### 2. Historical BARS exist for delisted symbols (the critical question — YES)
Pulled daily bars across the active life of delisted names:
| ticker | bars | range | last close | event |
|---|---|---|---|---|
| CELG | 979 | 2016-01-04 .. **2019-11-20** | $108.24 | acquired by BMS Nov 2019 |
| XLNX | 1540 | 2016-01-04 .. **2022-02-11** | $194.92 | acquired by AMD Feb 2022 |
| MXIM | 1422 | 2016-01-04 .. **2021-08-25** | $103.14 | acquired by ADI Aug 2021 |
| WORK | 525 | **2019-06-20** .. 2021-07-20 | $45.20 | Slack IPO→Salesforce 2021 |
| WCG | 1021 | 2016-01-04 .. 2020-01-23 | $349.92 | acquired by Centene 2020 |

→ Alpaca serves full-life daily bars for delisted exchange-listed names, right up to the last trade. This is
exactly what completes the survivorship-biased cross-section.

### 3. The tradeable universe is fully covered (OTC gaps don't matter)
INACTIVE by exchange: **NASDAQ 1,302 · NYSE 849 · ARCA 467 · BATS 128 · AMEX 97** (exchange-listed = **2,843**)
· OTC 16,410. The 16k OTC names are penny/pink-sheet — irrelevant to our liquid top-N universe. So a
survivorship-clean **exchange-listed** universe (the only tradeable one) gains ~**2,843 delisted names** at $0.
(FNMA returned no bars precisely because it's OTC — confirming the only gap is OTC, which we don't trade.)

## The ONE genuine gap, and how we handle it (no spend needed)
**Precise delisting RETURNS are not directly served.** Two sub-gaps, both worked around:
- **No delisting-DATE field** on the Asset (fields: status/tradable/exchange — no `delisting_date`). →
  Workaround: the **last bar date** per inactive symbol IS the de-facto delisting date (the security stopped
  trading). We infer it from the bars, not a field. Clean and free.
- **No terminal liquidation/acquisition value** directly. The Alpaca corporate-actions endpoint did NOT
  return the historical merger `cash_rate` for our 2019-2021 test names (its history horizon is short — the
  backfill runs a ±35-day recent window). → So the terminal-merger-value route is NOT reliable for old
  delistings. **The available proxy is the last-traded close as the terminal value** — i.e. the holding-period
  return ends at the last real trade.

### The acquisition-premium vs bankruptcy-to-zero ambiguity (flagged honestly)
The last-traded close is an UNBIASED-ish proxy for ACQUISITIONS (a cash/stock deal trades near terminal value
into the close — e.g. CELG $108, XLNX $195, MXIM $103 are real near-deal prices) but is **OPTIMISTIC for
BANKRUPTCIES** (a name halted on the way to zero stops printing at its last small-but-positive price, not $0).
For a LOSER-BUYING strategy (weekly reversal buys the bottom decile), bankruptcies cluster in that leg — so
last-close is exactly where the bias would hide. **Mitigations, all $0:**
1. **Two-bound reporting (the honest band):** run the test with the loser leg's delisted names' terminal
   return = (a) the last-traded close (optimistic bound) AND (b) a −X% bankruptcy haircut on names that
   delisted at a depressed price / from a financial-distress flag (conservative bound) — the same both-ways
   discipline we used in #287, but now on REAL delisted names rather than absent ones.
3. **Use the exchange + last-price signature to classify:** an acquisition delists at a stable/elevated price
   (often a known deal price); a bankruptcy delists at a collapsing/penny price. A simple rule (last-20d
   return + last price level) separates the two cases well enough to apply the haircut only to the distress
   cases — far better than the current panel, which has NEITHER.

This is strictly better than today (real losers present, terminal returns bounded) and good enough to test
the signal honestly. CRSP's exact delisting-return field would be marginally cleaner, but the band brackets
the truth and costs $0.

## VERDICT: build the $0 Alpaca survivorship-clean panel — NO spend
- ✅ Delisted universe: 2,843 exchange-listed inactive names — FREE.
- ✅ Historical bars to last trade (2016+): FREE.
- ✅ Delisting date: inferred from last bar — FREE.
- ⚠️ Delisting return: last-traded close proxy + a two-bound (acquisition vs bankruptcy-haircut) band — FREE,
  honest, strictly better than the current survivors-only panel.
- ❌ The ONLY thing a paid source (CRSP/Norgate/Sharadar) adds is the *exact* delisting-return field and
  pre-2016 history. Neither is necessary: our weekly-reversal test runs 2018-2025 (post-2016), and the
  two-bound band brackets the delisting-return uncertainty.

**RECOMMENDATION: do NOT purchase. Build the survivorship-clean panel from Alpaca INACTIVE assets + their
historical bars (last-close terminal proxy, two-bound bankruptcy band).** The paid spend is justified ONLY if,
after building this, the result lands inside the acquisition-vs-bankruptcy band in a way that the band can't
resolve (i.e. the verdict flips between the optimistic and conservative bounds) — THEN CRSP's exact delisting
returns would be worth ~$630-15k to disambiguate. We'll know after the $0 build, not before.

## Next step (the actual experiment, $0)
Re-point the panel build at `AssetStatus.INACTIVE ∪ ACTIVE` (exchange-listed), pull historical bars for the
delisted names, infer each delisting date from its last bar, and re-run the #287 weekly-reversal test on the
de-biased cross-section: entry ≥09:35 ET, walk-forward purge, predict-zero + shuffle baselines, Stage-1
realized cost, the loser leg's delisted terminal returns booked at last-close (optimistic) AND with a
distress-bankruptcy haircut (conservative) → report the band. PASS (clears net-of-cost across the band +
replicates) = first survivorship-honest tradeable edge; NULL = the +0.025 IC was survivorship, honestly
killed with REAL delisted names. Decisive, $0.

---
*All counts/bars measured live via the paper Alpaca API on 2026-06-20 (scripts: `alpaca_inactive_probe.py`,
`alpaca_inactive_bars.py`, `alpaca_coverage_breakdown.py`, `alpaca_ca_probe.py` — creds referenced by env
name only, never printed).*
