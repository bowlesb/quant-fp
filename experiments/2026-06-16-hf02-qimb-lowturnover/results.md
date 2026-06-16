# HF02 Results — qimb low-turnover overlays (CORRECTED re-score)

**Run date:** 2026-06-16. Pre-registered follow-up to HF01. **This version uses the DAY-CLUSTERED metric and a per-trade bootstrap (see the correction note below).**

## CORRECTION NOTE (supersedes the first run)

The first HF02 run reported OOS demean t = 9.41. **That was a clustering artifact.** `hf_metrics_fixed.per_symbol_day_ics` originally returned one IC per (symbol, date) CELL, and `day_clustered_tstat` then treated each of the ~204 cells as an independent observation — inflating the t by ~sqrt(n_symbols). The metric is now corrected to day-cluster (average the per-(symbol,date) cell-ICs WITHIN each date → one IC per DAY → t over the ~32 OOS days). **More importantly, the headline `net_bps` (per-bucket overlapping-return accounting) overstated the realized economics; the decisive check is the per-round-trip bootstrap, added in this run.**

## Panel

9 symbols with ≥21 valid quote-days (MSFT, AVGO, AMD, TSLA, AAPL, NVDA, META, SPY, + one more crossing the threshold on the 2026-06-16 data add). 63 unique dates. Train: 31 days, OOS: 32 days. Panel avg round-trip = 2.107 bps.

## Per-symbol measured spreads

| Symbol | Half-spread (bps) | Round-trip (bps) |
|--------|-------------------|------------------|
| SPY    | 0.141             | 0.282            |
| AAPL   | 0.494             | 0.989            |
| NVDA   | 0.564             | 1.129            |
| MSFT   | 0.845             | 1.690            |
| TSLA   | 1.095             | 2.191            |
| META   | 1.731             | 3.462            |
| AVGO   | 1.878             | 3.757            |
| AMD    | 2.525             | 5.050            |
| **Panel avg** | —          | **2.107**        |

## Gate 1: qimb IC + Canary (all-sample)

All 8 cells (2 windows × 4 horizons) canary-pass. IC grows monotonically with horizon and window — qimb is a genuine slow signal (unchanged from HF01).

## Gate 2: Fixed per-symbol demean IC — DAY-CLUSTERED (n_days = 63)

| w (s) | h (min) | mean_ic_dm | t_dm (day-clustered) |
|-------|---------|------------|----------------------|
| 120   | 5       | 0.01639    | 2.69                 |
| 300   | 5       | 0.02580    | 3.59                 |
| 120   | 10      | 0.02603    | 3.31                 |
| 300   | 10      | 0.03574    | 3.96                 |
| 120   | 15      | 0.03496    | 4.59                 |
| 300   | 15      | 0.04879    | 5.82                 |
| 120   | 30      | 0.05118    | 6.44                 |
| **300** | **30** | **0.06712** | **6.80**           |

## Gate 3: Walk-forward OOS IC — DAY-CLUSTERED (n_days = 32)

| w (s) | h (min) | mean_ic_oos | t_oos (day-clustered) | n_days |
|-------|---------|-------------|-----------------------|--------|
| 120   | 5       | 0.02308     | 2.75                  | 32     |
| 300   | 5       | 0.04077     | 4.47                  | 32     |
| 120   | 10      | 0.03928     | 3.49                  | 32     |
| 300   | 10      | 0.05438     | 4.71                  | 32     |
| 120   | 15      | 0.04621     | 4.49                  | 32     |
| 300   | 15      | 0.06488     | 6.45                  | 32     |
| 120   | 30      | 0.06985     | 8.03                  | 32     |
| **300** | **30** | **0.09060** | **8.45**              | 32     |

The day-clustered t's are lower than the buggy run (h=5m: 4.87 → 2.75, the predicted ~sqrt(n_sym) deflation) but the long-horizon cells stay high (8.45 at h=30m) because the per-day IC is consistent. **The IC is real and significant — but the IC is NOT the same thing as a tradeable P&L (see Gate 4).**

⚠️ **YELLOW FLAG:** OOS IC (0.0906) > in-sample demean IC (0.0671) at the best cell. OOS-stronger-than-IS is a small-sample / regime-luck warning, not a strength.

## Gate 4: Turnover-compounded cost gate AND per-trade bootstrap (OOS)

### Per-bucket "net_bps" (the HF01-style headline — OVERLAPPING, MISLEADING)

| w (s) | h (min) | Overlay | net_bps @1x | net_2x_bps |
|-------|---------|---------|-------------|------------|
| 300   | 30      | HOLD    | +1.269      | +1.259     |
| 300   | 15      | HOLD    | +0.740      | +0.721     |
| 120   | 15      | PERSIST(3,0.1) | +0.623 | +0.594  |

These per-bucket numbers look positive **but they double-count overlapping forward returns** (each held position contributes h_buckets terms, each an overlapping h-min forward return). They are NOT realized economics.

### Per-ROUND-TRIP bootstrap (the DECISIVE check)

Collapsing each non-overlapping held block to ONE realized round-trip (signed h-min return at entry minus one round-trip cost), then bootstrapping the mean per-trade net (10,000 resamples):

| w | h | Overlay | n_trades | mean net (bps) | median (bps) | win rate | 95% CI @1x (bps) | 95% CI @2x (bps) |
|---|---|---------|----------|----------------|--------------|----------|------------------|------------------|
| 300 | 30 | HOLD | 1234 | **−0.20** | −1.14 | 44.4% | [−3.16, +2.79] | [−5.34, +0.63] |
| 300 | 15 | HOLD | 2276 | **−2.29** | −1.38 | 45.9% | [−4.04, −0.54] | [−6.29, −2.79] |
| 120 | 15 | PERSIST(3,0.1) | 3440 | **−2.36** | −1.70 | 44.5% | [−3.67, −1.00] | [−5.95, −3.28] |
| 120 | 15 | PERSIST(5,0.05) | 3818 | **−1.32** | −1.46 | 46.3% | [−2.60, −0.02] | [−4.84, −2.25] |
| 120 | 30 | PERSIST(2,0.05) | 1952 | **−1.46** | −1.69 | 44.4% | [−3.94, +1.00] | [−6.17, −1.24] |
| 120 | 30 | HOLD | 1296 | **−1.91** | −1.69 | 44.1% | [−4.80, +0.91] | [−6.95, −1.21] |

**Every top cell has a NEGATIVE mean per-trade net.** Win rates are all below 50% (44–46%). The best cell (300/30 HOLD) has a per-trade net of −0.20 bps with a 95% CI of [−3.16, +2.79] that straddles zero at 1× and is mostly below zero at 2×. No cell's per-trade CI excludes zero on the positive side.

## Reconciliation: why per-bucket net is +1.27 but per-trade is −0.20

The per-bucket gross counts the same 30-min price move ~180 times (once per held bucket) and divides by ALL buckets. Because the signal is most often flat, the average is dominated by the in-position buckets' overlapping returns, which co-move and inflate the apparent edge. When you collapse to ACTUAL non-overlapping round-trips (1,234 of them in OOS) and charge the real spread once per trade, the median trade LOSES ~1.1 bps and the mean is slightly negative. The +1.27 bps was an accounting artifact, not money.
