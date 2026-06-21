# Costed proposal — survivorship-clean data for the weekly-reversal signal (DECISION-READY, for Ben)

**Date:** 2026-06-20  **Author:** Modeller  **Status:** SUPERSEDED by the Alpaca-first finding — NO SPEND
recommended.

> ## ⚠️ UPDATE (2026-06-20) — Ben was right; do NOT buy. See `ALPACA_FIRST_FINDING.md`.
> The survivorship bias is **self-inflicted** (our `AssetStatus.ACTIVE` filter at raw_backfill.py:198), not an
> Alpaca limit. Measured live: Alpaca has **19,253 INACTIVE US-equity assets** (2,843 exchange-listed delisted
> names), **serves full-life historical daily bars** for them to their last trade (2016+; verified CELG/XLNX/
> MXIM/WORK/WCG), and we infer the delisting date from the last bar. The ONLY gap is the *exact* delisting
> RETURN — handled $0 by a last-traded-close proxy + a two-bound (acquisition vs bankruptcy-haircut) band.
> **Recommendation flips: build the survivorship-clean panel from Alpaca for $0; a paid source (below) is
> justified ONLY if the $0 result lands unresolvably inside the band.** The paid-source comparison below is
> retained as the fallback option if that happens.

## TL;DR (the decision)
- **Problem:** weekly-reversal is our ONE genuinely-real signal (rank-IC +0.025, 11σ over shuffle,
  own-vol-independent, sign-consistent 2018-21/2022-25, PR #287) — but our panel is survivorship-biased (0 of
  600 sampled names delist in-sample), and weekly reversal BUYS recent losers, exactly where the absent
  delisted-losers inflate returns. We charged an *external estimated* −13bps/wk haircut; we cannot honestly
  test whether the signal clears net-of-cost until the delisted names + their delisting returns are IN the
  panel.
- **Recommendation: subscribe to Norgate Data "Platinum US Stocks" — ~US$630/year** (delisted-inclusive
  daily data back to 1990; 25,000+ delisted securities with delisting handling). It is the cheapest,
  cleanest fit for our exact need (we need delisted *names + delisting returns* added to a daily cross-section
  — not a new intraday tape). Sharadar (≈$50-70/mo via Nasdaq Data Link) is the close second.
- **Decision Ben needs to make:** approve **~$630 (one year, Norgate Platinum)** to run the survivorship-clean
  weekly-reversal test. This is a small, bounded, one-year spend that either validates our first real tradeable
  edge or honestly kills it — both decisive.

---

## 1. The exact problem + what delisting-inclusive data fixes
- **Our data:** `/store/raw/bars` = Alpaca minute bars, currently-listed names only, every one carried back to
  2016. Verified survivorship: **0/600 sampled symbols stop printing before 2026-06-17.** `universe_membership`
  (the survivorship-aware table) only starts 2026-06-15. So a historical universe reconstructed from our bars
  is survivors-only by construction.
- **Why it matters for THIS signal specifically:** weekly reversal = long the bottom-return decile (recent
  losers). The losers that fell hardest and then KEPT falling — to delisting/zero — are exactly the names
  absent from a survivors-only panel. So "buy the losers, they bounce" is flattered by construction; the
  bottom decile is censored to losers that survived (and thus disproportionately bounced). This is the bias
  that has flattered loser-buying strategies for decades.
- **What delisting-inclusive data fixes:** it supplies (a) **point-in-time universe membership** (which names
  were actually listed each week, INCLUDING ones that later delisted) and (b) the **delisting return** (the
  realized terminal return when a name leaves — CRSP-style, the Shumway −55%-ish performance-delisting
  correction). With both, the historical bottom decile contains the real losers AND their real outcomes, so we
  can MEASURE (not estimate) whether reversal clears net-of-cost.
- **Narrow requirement (keeps it cheap):** we do NOT need a new intraday tape — our Alpaca minute bars are
  fine for survivors and for the live path. We need ONLY the **delisted names + their daily closes + delisting
  returns** to complete the historical cross-section. That is a daily-EOD, delisting-aware dataset — the
  cheapest category.

## 2. Candidate sources (cost / coverage / fit)
| Source | Cost (US$) | Delisted + delisting returns? | History | Fit to our pipeline |
|---|---|---|---|---|
| **Norgate Data — Platinum US Stocks** | **~$630/yr** ($346.50/6mo) | YES — 25,000+ delisted securities, delisting handled; survivorship-bias-free by design | daily back to 1990 (Diamond → 1950, $787.50/yr) | Daily EOD OHLCV + delist flags; clean Python API (`norgatedata` on PyPI). We use it to ADD delisted names + delisting returns to the daily cross-section; our Alpaca bars stay the survivor/live source. **Best fit.** |
| **Sharadar SEP (Nasdaq Data Link)** | ~$50-70/mo (~$600-840/yr; standalone SEP cheaper than the bundle) | YES — 21,000+ active+delisted tickers, "nearly completely free from survivorship bias" | daily back to 1998 | Daily EOD via Nasdaq Data Link API (Python). Same integration shape. Strong second; monthly billing = flexible cancel. |
| **CRSP (via WRDS)** | institutional only — typically **$5k-15k+/yr**, signed contract, academic-gated | YES — the academic gold standard; explicit delisting-return fields (Shumway correction is literally CRSP) | daily/monthly back to 1925/1962 | The "correct" answer academically, but priced for institutions + WRDS access; **not realistic for a solo quant's bounded spend.** (Note: Morningstar acquired CRSP Feb 2026.) |
| **Polygon.io / Tiingo (delisted add-ons)** | ~$30-200/mo | PARTIAL — some delisted coverage, delisting-RETURN completeness is weaker/less documented than Norgate/Sharadar/CRSP | varies | Possible but the delisting-return quality (the thing that matters) is the least certain. Not recommended as primary. |

## 3. The exact experiment this enables (the survivorship-clean weekly-reversal test)
On a delisting-inclusive daily panel (the new source for the historical cross-section; Alpaca for the live
path), re-run the #287 weekly-reversal test with the bias REMOVED BY DATA rather than charged by assumption:
- **Universe:** point-in-time top-N-by-ADV per week from the delisting-aware membership (now includes names
  that later delisted) — fixes the survivors-only universe.
- **Signal/label:** `−rev_1w` (trailing 5-day return) → forward 1-week tradeable return, **entry ≥09:35 ET**
  (next-Monday tradeable open, never the Friday close — no close-to-close look-ahead), $1 floor.
- **Delisting handling (the whole point):** a name that delists during the forward week realizes its
  **actual delisting return** from the data (not a −13bps/wk estimated haircut). The bought-losers leg now
  eats the real losses of the losers that didn't bounce.
- **Cost:** Stage-1 measured per-name realized half-spread where our quote tape overlaps; a documented
  conservative bps proxy on the deep dates the tape doesn't cover (the cost model is already built, PR #271).
- **Baselines:** predict-zero (no-trade = $0) AND within-week label shuffle (≥300 iters) — the signal must
  dominate both.
- **Discipline:** walk-forward purged by the 1-week horizon; per-week NW-t; disjoint-window replication
  (two non-overlapping multi-year halves); BY-FDR if multiple cells.
- **THE PASS BAR:** the L/S reversal net edge must clear **net-of-measured-cost AND net of the REAL
  (in-sample, data-driven) delisting returns**, with per-week NW-t ≥ 2 (not one-outlier-driven) and disjoint
  replication. If it clears → the FIRST validated, survivorship-honest tradeable edge → confirmatory
  replication, then deployment-hardening. If it dies → the +0.025 IC was a survivorship artifact, honestly
  killed with real data (a clean, decisive result either way).

## 4. Recommendation + the decision
**Recommend Norgate Data Platinum (~$630/year).** Why over the alternatives:
- It is delisted-inclusive WITH delisting handling, daily to 1990 — exactly the historical cross-section
  completion we need, and nothing more (we don't need CRSP's institutional depth or a second intraday tape).
- Cheapest credible option with documented delisting coverage; clean Python API that plugs into our daily
  panel build as an additive source (delisted names + delisting returns) alongside our Alpaca bars.
- Sharadar is an equally-valid second choice (monthly billing = easy cancel after one test); pick it instead
  if Ben prefers month-to-month flexibility over the annual discount.
- CRSP is the academic gold standard but institution-priced ($5k-15k+) — not justified for a single bounded
  test by a solo quant.

**THE DECISION FOR BEN:** approve **~$630 (one year, Norgate Platinum)** — OR ~$50-70/mo Sharadar month-to-
month — to run the survivorship-clean weekly-reversal test. This is the ONLY data spend that lets us honestly
resolve whether our one real signal is a tradeable edge or a survivorship mirage. Small, bounded, decisive.
On approval I'll build the delisting-aware panel + run the §3 test (no further spend needed).

---
*Sources: [Norgate Data prices](https://norgatedata.com/prices.php) · [Norgate stock packages](https://norgatedata.com/stockmarketpackages.php) · [Sharadar SEP (Nasdaq Data Link)](https://data.nasdaq.com/databases/SEP) · [Sharadar pricing (QuantRocket)](https://www.quantrocket.com/pricing/data/sharadar/) · [CRSP via WRDS](https://wrds-www.wharton.upenn.edu/pages/about/data-vendors/center-for-research-in-security-prices-crsp/). Prices are vendor-listed as of 2026-06; confirm at purchase. No purchase made.*
