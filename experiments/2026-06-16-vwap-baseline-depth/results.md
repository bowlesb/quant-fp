# Results — vwap_dev baseline at depth + H1-recheck

REAL pooled numbers from the full 126-day panel. Pasted script output below.

## Panel actually processed
- n_symbols_seen = **629**, n_dates = **126**, grid rows (symbol-minute) = **26,679,510**.
- Valid cross-sections: **46,873** (H=15), **44,998** (H=30). Day-clustered over **125** days.

## Headline numbers

| Metric | H=15 | H=30 |
|---|---|---|
| pooled mean IC | **−0.05810** | **−0.06566** |
| day-clustered mean IC | −0.05810 | −0.06566 |
| **day-clustered t** | **−32.29** | **−27.58** |
| canary IC mean (std) | +0.00011 (0.04846) | +0.00007 (0.04871) |
| tier 0 illiquid IC (t) | −0.11136 (−84.3) | −0.13373 (−69.5) |
| tier 1 mid IC (t) | −0.05207 (−23.4) | −0.05023 (−16.6) |
| tier 2 liquid IC (t) | −0.01749 (−5.8) | −0.01379 (−3.5) |
| **illiquid/liquid \|IC\| ratio** | **6.37×** | **9.70×** |

## Net-of-cost (decile L/S book, 8bps/period round-trip)

| Tier | H=15 gross / net | H=30 gross / net | clears? |
|---|---|---|---|
| 0 illiquid | +85.2 / **+77.2** bps | +132.3 / **+124.3** bps | "clears" — but ARTIFACT (stale-close, untradeable) |
| 1 mid | +18.2 / +10.2 bps | +20.5 / +12.5 bps | "clears" — partially artifact-contaminated |
| 2 **liquid** (real prices) | +2.6 / **−5.4** bps | +1.0 / **−7.0** bps | **FAILS** (the honest, tradeable read) |

The illiquid/mid "clearing" is driven by the forward-filled-stale-close mechanical reversion
(see method.md COST CAVEAT) — its t+1 entry close is fictional. The **liquid tier is the only one
priced on real trades, and it does NOT clear the 8bps/period cost** at either horizon.

## Raw script output (full panel)

```
Processing 126 dates...
========== RESULTS ==========
Panel: n_symbols_seen=629  n_dates=126
Grid rows processed (symbol-minute): 26,679,510

--- H=15 ---
  n_cross_sections (valid minutes): 46873
  pooled mean IC: -0.05810
  day-clustered: mean_IC=-0.05810  t=-32.29  n_days=125
  canary IC: mean=+0.00011 std=0.04846 (n=468730)
  tier 0 (illiquid): mean_IC=-0.11136 t=-84.28 n_days=125
  tier 1 (mid): mean_IC=-0.05207 t=-23.35 n_days=125
  tier 2 (liquid): mean_IC=-0.01749 t=-5.84 n_days=125
  illiquid/liquid |IC| ratio: 6.368
  L/S net tier 0 (illiquid): gross/period=+85.23bps cost=8.0bps net=+77.23bps CLEARS
  L/S net tier 1 (mid): gross/period=+18.17bps cost=8.0bps net=+10.17bps CLEARS
  L/S net tier 2 (liquid): gross/period=+2.64bps cost=8.0bps net=-5.36bps fails

--- H=30 ---
  n_cross_sections (valid minutes): 44998
  pooled mean IC: -0.06566
  day-clustered: mean_IC=-0.06566  t=-27.58  n_days=125
  canary IC: mean=+0.00007 std=0.04871 (n=449980)
  tier 0 (illiquid): mean_IC=-0.13373 t=-69.46 n_days=125
  tier 1 (mid): mean_IC=-0.05023 t=-16.62 n_days=125
  tier 2 (liquid): mean_IC=-0.01379 t=-3.48 n_days=125
  illiquid/liquid |IC| ratio: 9.701
  L/S net tier 0 (illiquid): gross/period=+132.27bps cost=8.0bps net=+124.27bps CLEARS
  L/S net tier 1 (mid): gross/period=+20.53bps cost=8.0bps net=+12.53bps CLEARS
  L/S net tier 2 (liquid): gross/period=+1.04bps cost=8.0bps net=-6.96bps fails
========== END ==========
```
