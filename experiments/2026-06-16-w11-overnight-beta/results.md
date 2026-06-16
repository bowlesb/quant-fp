# W11 — Overnight-BETA premium: results

Universe 500 liquid names; beta = trailing-60d OLS on equal-weight liquid-universe market, re-est every 21d;
HIGH/LOW = top/bottom beta quintile (~100 names each), equal-weight; held 21d forward of the beta window.
All numbers in **bps/day** = average daily L/S (high−low) return over the hold. **n = 3 rebalances** (after
the 60d beta warmup on 126d — thin, see power caveat).

## Headline: the overnight/intraday SPLIT (the test)

| Realization        | mean (bps/day) | bootstrap 95% CI (bps/day) |
|--------------------|---------------:|----------------------------|
| **OVERNIGHT gross**| **+75.1**      | [+45.9, +114.3]            |
| **INTRADAY gross** | **−22.8**      | [−38.1, +6.7]             |
| 24h gross          | +52.3          | [+9.0, +78.2]             |

**The split is in the predicted direction: overnight ≫ intraday, with overnight strongly POSITIVE and
intraday NEGATIVE.** This is the sharp Hendershott–Livdan–Rösch prediction, and it holds in every one of the
3 rebalances (overnight always positive, intraday positive only once and small).

## Net of cost (overnight leg)

| | mean (bps/day) | bootstrap 95% CI |
|---|---:|---|
| overnight gross        | +75.1 | [+45.9, +114.3] |
| overnight **net** (3bps/side, turnover-weighted) | **+72.6** | [+45.3, +113.4] |
| overnight net, **2× stress** | +70.1 | [+44.6, +112.5] |

Cost is tiny (≈2.5 bps/day) because turnover is low (avg **12.8%** of leg membership per rebalance) — beta
quintile membership is slow-moving, exactly the friction-favorable profile the hypothesis claimed.

## Per-rebalance detail

| est_date   | overnight | intraday | 24h   | mean high-β | mean low-β |
|------------|----------:|---------:|------:|------------:|-----------:|
| 2026-03-16 | +65.3     | +6.7     | +69.6 | 2.49        | −0.09      |
| 2026-04-15 | +114.3    | −36.8    | +78.2 | 2.37        | −0.18      |
| 2026-05-14 | +45.9     | −38.1    | +9.0  | 2.37        | −0.24      |

Overnight positive in 3/3; intraday negative in 2/3 (and small-positive in the third). 24h positive in 3/3.

## Gates

- **Shuffle-canary** (permute beta→name): overnight L/S collapses to **+9.4 bps/day, CI [−2.2, +17.0]** —
  i.e. the canary CI **includes/straddles zero** and is ~8× smaller than the real +75 bps. The signal is in
  the beta sort, not the leg-construction machinery. **Canary PASSES.**
- **Walk-forward OOS**: held returns are already forward of the beta window (no look-ahead by construction).
  The explicit first-half/second-half split is **degenerate here** (1 IS + 2 OOS rebalances): "OOS overnight
  net" = +79.3 bps [+45.3, +113.4] — directionally consistent but **not a real OOS** with n=2.
- **Per-rebalance bootstrap** (10k, non-overlapping): overnight net **CI = [+45.3, +113.4] bps/day, excludes
  zero**. BUT this resamples only **3 points** → the CI is mechanically narrow-looking yet statistically
  fragile (3 obs cannot establish a robust interval). Treat as suggestive, not decisive.

## Robustness (is it a few crypto-gappers?)

The HIGH-beta leg is dominated by 2026 speculative-overnight-sensitive names: bitcoin miners (HUT, IREN, WULF,
CIFR, BMNR), quantum (QBTS), nuclear/uranium (OKLO, SMR, LEU, UUUU), space (RDW). Crypto trades 24h, so these
mechanically gap at the U.S. open — a plausible **confound** with a generic overnight-gap factor. Two checks:

| rebal | mean-leg overnight | **median-leg** overnight | winsorized ±15% (mean-leg) |
|-------|-------------------:|-------------------------:|---------------------------:|
| 0 | +65.3 | **+44.2** | +63.8 |
| 1 | +114.3 | **+85.3** | +113.2 |
| 2 | +45.9 | **+22.7** | +46.5 |

The effect **survives winsorization and the median-of-leg** (robust to a handful of extreme gappers) — still
strongly positive in all 3 rebalances. So it is a **broad** positive overnight tilt across the high-beta leg,
not a 2-name artifact. It remains **partially confounded with "who is high-beta in this 126d window"** (a
crypto/AI-speculation cohort); whether the premium is the durable beta-risk premium or a regime-specific
overnight-gap factor cannot be separated on 126 days.

## Power (honest)

n = 3 non-overlapping rebalances after the 60d beta warmup. The bootstrap and IS/OOS split are underpowered;
the **SPLIT direction (overnight ≫ intraday, 3/3)** is the robust evidence, the net-of-cost CI is not.
