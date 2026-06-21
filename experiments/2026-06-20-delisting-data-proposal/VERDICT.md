# Survivorship-clean weekly-reversal — VERDICT: NULL in BOTH bounds, $0, NO paid data needed

**Date:** 2026-06-20  **Code SHA:** `dbf3415` (branch `modeller/delisting-data-proposal`, off origin/main).
**Cost: $0** (Alpaca paper API; the bias was our own `AssetStatus.ACTIVE` filter). **Spend recommendation:
do NOT purchase — the $0 panel fully resolves the question; the verdict does NOT flip between bounds.**

## The bottom line
On the **survivorship-CLEAN** cross-section (delisted losers now present — 1,679 delisted names, 493
distress/bankruptcy), the weekly-reversal L/S basket **does NOT clear the tradeable bar (per-week NW-t < 2)
under BOTH the optimistic and the conservative delisting-return bounds**, which agree almost exactly. So:
- The +0.025 weekly-reversal IC is REAL (shuffle-z +4.8, it dominates the null) but the **dollar basket is
  too noisy to trade (NW-t +1.38 < 2)** — the #287 finding, now CONFIRMED on real delisted data.
- The two bounds barely differ (NW-t +1.38 vs +1.35) → **the result is NOT sensitive to the delisting-return
  ambiguity** → the exact CRSP/Norgate delisting-return field would change nothing → **no spend.**

## Provenance (the Lead's ask — exact construction)
- **Data state:** $0 Alpaca paper API. Universe = (`AssetStatus.ACTIVE` ∪ `AssetStatus.INACTIVE`) US-equity,
  exchange-listed (NYSE/NASDAQ/ARCA/AMEX/BATS), clean A-Z tickers only (CUSIP/CVR/escrow delisting-residue
  artifacts excluded), ETF-screened → **7,209 symbols with bars**. DAILY split-adjusted bars 2018-01-02 ..
  2025-12-30 (**9.16M rows**), $1 floor. **1,679 delisted** (last bar before span-end), **493 distress**.
- **Point-in-time membership:** a name is in the weekly cross-section only while it was trading (its bars span
  only its trading life; the last bar = de-facto delisting date) → fixes the survivors-only universe.
- **Universe per week:** top-1000 by trailing-20d ADV among names trading that week (delisted names included
  while they traded). **398 weekly rebalances**, 398,000 obs; **1,179 obs where the name delisted during the
  forward week** (the real losers that left — absent from the old panel).
- **Signal/label:** `−rev_1w` (trailing-5d return, buy losers) → forward 1-week return from the **next trading
  day's OPEN** (tradeable entry, no close-to-close look-ahead) to the +5d OPEN. $1 floor both legs.
- **Delisting terminal — TWO BOUNDS (the honest band):**
  - OPTIMISTIC: a name delisting in the forward week realizes its last-traded close vs entry (good for
    acquisitions; optimistic for bankruptcies).
  - CONSERVATIVE: distress-classified delistings (delisted-before-span-end AND last-20d-ret < −50% OR last
    close < $5) realize **−100%** (bankruptcy-to-zero); acquisitions keep last-close.
- **Cost:** a conservative 5bps bar-proxy half-spread (round-trip per leg). [Stage-1 measured quote-tape cost
  applies only to the recent overlap; on the deep span the proxy is the documented stand-in — and since the
  basket is insignificant even at this low cost, a higher measured cost only makes it MORE null.]
- **Baselines:** within-week label shuffle (300 iters → shuffle-z) + predict-zero (a no-trade basket = 0).
- **Discipline:** walk-forward (disjoint discovery 1st-half / replication 2nd-half), per-week NW-t.

## Result — all three cases reported (the Lead's decisive output)
| bound | n_weeks | mean bps/wk | Sharpe | NW-t | shuffle-z | disc t | repl t | PASS |
|---|---|---|---|---|---|---|---|---|
| OPTIMISTIC (last-close) | 398 | +40.7 | +0.50 | **+1.38** | +4.83 | +0.72 | +1.28 | **No** |
| CONSERVATIVE (distress −100%) | 398 | +40.0 | +0.49 | **+1.35** | +4.34 | +0.71 | +1.25 | **No** |

- **CLEARS in both bounds?** No. **FAILS in both bounds?** YES. **Flips between bounds?** No (the bounds are
  nearly identical).
- Pass bar = NW-t ≥ 2 AND mean > 0 AND shuffle-z ≥ 2 AND replicates (disc & repl both positive). Both bounds
  satisfy mean>0, shuffle-z≥2, and positive disc/repl — but **fail the per-week NW-t ≥ 2** (the basket is
  positive-but-not-significant). NULL.

## Interpretation (and why it's CLEANER than #287)
On the survivorship-clean panel the basket is still **net-POSITIVE (+40bps/wk)** — adding the real delisted
losers (incl. 493 bankruptcies at −100%) did NOT flip it negative. This is *less* bad than #287's verdict,
where the *estimated* −13bps/wk haircut was too harsh and flipped replication negative. The REAL delisting
returns are gentler than that estimate, so the de-biased basket stays positive — it just isn't statistically
significant week-to-week (NW-t +1.38). **The signal is real (IC, shuffle-z) but not a tradeable basket** — the
honest, final answer, reached for $0 with real delisted names.

## Disposition + decision
- **Inclusion-liberal:** NULL = the model doesn't TRADE weekly-reversal as a standalone basket — NOT "drop
  rev_1w." The feature stays included/retained (the IC is real and now survivorship-confirmed); future use
  (a combined model, a different construction) may employ it. What-to-trade, not what-to-store.
- **Spend decision for Ben: NO PURCHASE.** The $0 Alpaca panel resolved the question decisively and the
  verdict is insensitive to the delisting-return ambiguity (bounds agree). CRSP/Norgate/Sharadar would add the
  exact delisting-return field, but it would not change a NW-t of +1.38 vs +1.35 into a pass. The ~$630
  Norgate fallback stays documented but is NOT triggered.
- **Routing:** the standalone weekly-reversal edge is settled (real IC, untradeable basket, survivorship-
  confirmed). The platform's edge remains the net-positive trusted-baseline portfolio → deployment-hardening.

---
*All data measured live via the paper Alpaca API on 2026-06-20 (build: `build_debiased_panel.py`; screen:
`screen_debiased.py`; probes: `alpaca_*`. Creds referenced by env name only, never printed). $0 spent.*
