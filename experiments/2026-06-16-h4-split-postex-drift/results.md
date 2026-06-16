# H4 Results: Split POST-event Drift

**Run date:** 2026-06-16. Full tables from `raw_results.json`.
Entry = D+1 OPEN after ex_date. Forward return = close[t+h] / open[entry] - 1.
Liquidity tertiles by median daily dollar volume (symbol-level):
  Tier 3 (liquid): median dvol >= $9.3M/day. Tier 1 (illiquid): <= $208K/day.

**Event counts:**
- Reverse splits: 312 total → 297 with open price at h=1d (15 missing EST-period opens discarded)
- Forward splits: 17 total → 16 with open price at h=1d

---

## REVERSE SPLITS (split_ratio < 1; predicted: negative drift)

N = 312 events; 274 unique symbols. Liquid tier N = 4. Illiquid tier N = 179. Mid tier N = 114.

### Full universe

| Horizon | N_events | N_dates | Alpha% | t-stat | Canary_p95% | Clears? | Alpha_dm% | t_dm |
|---------|----------|---------|--------|--------|-------------|---------|-----------|------|
| 1d  | 297 | 105 | -5.040 | -3.55 | 0.182  | YES | -25.258  | -8.66  |
| 3d  | 293 | 104 | -7.571 | -3.54 | 13.257 | YES | -58.786  | -8.87  |
| 5d  | 286 | 102 | -9.167 | -3.46 | -0.344 | YES | -88.420  | -8.53  |
| 10d | 270 |  97 | -10.854| -2.38 | 2.962  | YES | -144.965 | -9.80  |
| 20d | 244 |  89 | -15.922| -2.86 | -0.961 | YES | -228.167 | -10.67 |

### Illiquid tier (T1, N~179 at h=1d)

| Horizon | N_events | N_dates | Alpha% | t-stat | Canary_p95% | Clears? | Alpha_dm% | t_dm |
|---------|----------|---------|--------|--------|-------------|---------|-----------|------|
| 1d  | 179 |  85 | -4.913  | -2.65  | 4.779  | YES | -23.966  | -7.09 |
| 3d  | 175 |  84 | -9.270  | -4.01  | 4.419  | YES | -56.906  | -8.67 |
| 5d  | 173 |  83 | -9.308  | -2.99  | 30.923 | YES | -80.906  | -8.48 |
| 10d | 161 |  78 | -13.820 | -2.61  | 39.880 | NO  | -137.726 | -9.60 |
| 20d | 147 |  71 | -32.410 | -9.44  | 61.535 | YES | -231.526 | -9.69 |

### Mid tier (T2, N~114 at h=1d)

| Horizon | N_events | N_dates | Alpha% | t-stat | Canary_p95% | Clears? | Alpha_dm% | t_dm |
|---------|----------|---------|--------|--------|-------------|---------|-----------|------|
| 1d  | 114 | 72 | -8.646  | -4.37 | 7.323  | YES | -29.875  | -7.35 |
| 3d  | 114 | 72 | -8.863  | -2.13 | 12.068 | YES | -62.301  | -6.71 |
| 5d  | 109 | 70 | -12.897 | -3.00 | 9.703  | YES | -97.446  | -7.09 |
| 10d | 105 | 67 | -15.750 | -2.40 | 7.534  | YES | -161.230 | -8.16 |
| 20d |  93 | 60 | -10.493 | -1.04 | 12.075 | NO  | -260.583 | -6.63 |

### Liquid tier (T3, N=4 — UNDERPOWERED)

| Horizon | N_events | N_dates | Alpha% | t-stat | Canary_p95% | Clears? | Alpha_dm% | t_dm |
|---------|----------|---------|--------|--------|-------------|---------|-----------|------|
| 1d  | 4 ⚠️ | 4 | -13.467 | -2.96 | 3.724 | YES | -19.763 | -2.49 |
| 3d  | 4 ⚠️ | 4 | -17.047 | -2.32 | 5.731 | YES | -32.918 | -2.15 |
| 5d  | 4 ⚠️ | 4 | -15.328 | -2.25 | 6.931 | YES | -39.178 | -2.26 |
| 10d | 4 ⚠️ | 4 | -12.525 | -1.35 | 6.707 | YES | -59.483 | -2.07 |
| 20d | 4 ⚠️ | 4 |  -7.061 | -0.86 | 9.159 | NO  | -108.247| -2.37 |

---

## FORWARD SPLITS (split_ratio >= 1; predicted: positive drift)

N = 17 total (16 with valid open price). ALL CELLS UNDERPOWERED (<20 events in every tier).

### Full universe (N=16, ALL UNDERPOWERED)

| Horizon | N_events | N_dates | Alpha% | t-stat | Canary_p95% | Clears? | Alpha_dm% | t_dm |
|---------|----------|---------|--------|--------|-------------|---------|-----------|------|
| 1d  | 16 ⚠️ | 13 | -2.430 | -0.89 | 1.920  | NO | -1.280 | -0.48 |
| 3d  | 16 ⚠️ | 13 | -3.993 | -1.18 | 6.775  | NO | -0.988 | -0.31 |
| 5d  | 16 ⚠️ | 13 | -3.939 | -1.17 | -1.984 | NO | 0.683  |  0.24 |
| 10d | 16 ⚠️ | 13 | -6.292 | -1.23 | 1.143  | NO | 2.060  |  0.49 |
| 20d | 16 ⚠️ | 13 | -9.715 | -1.40 | 81.654 | NO | 5.575  |  0.67 |

### Mid tier (N=6, ALL UNDERPOWERED)

| Horizon | N_events | N_dates | Alpha% | t-stat | Canary_p95% | Clears? | Alpha_dm% | t_dm |
|---------|----------|---------|--------|--------|-------------|---------|-----------|------|
| 1d  | 6 ⚠️ | 6 | -3.133 | -0.53 | 2.868  | YES | -2.375 | -0.42 |
| 3d  | 6 ⚠️ | 6 | -5.068 | -0.77 | 1.310  | YES | -3.147 | -0.52 |
| 5d  | 6 ⚠️ | 6 | -2.415 | -0.36 | 2.525  | NO  |  0.670 |  0.12 |
| 10d | 6 ⚠️ | 6 | -6.088 | -0.54 | -5.930 | NO  | -0.170 | -0.02 |
| 20d | 6 ⚠️ | 6 |-11.260 | -0.77 | -7.098 | NO  |  0.324 |  0.02 |

### Liquid tier (N=9, ALL UNDERPOWERED)

| Horizon | N_events | N_dates | Alpha% | t-stat | Canary_p95% | Clears? | Alpha_dm% | t_dm |
|---------|----------|---------|--------|--------|-------------|---------|-----------|------|
| 1d  | 9 ⚠️ | 8 | 0.692 | 0.42 | 3.049  | NO | 1.286  | 0.87 |
| 3d  | 9 ⚠️ | 8 | 0.998 | 0.33 | 4.388  | NO | 2.726  | 1.03 |
| 5d  | 9 ⚠️ | 8 | 0.632 | 0.20 | 6.256  | NO | 3.330  | 1.26 |
| 10d | 9 ⚠️ | 8 | 1.868 | 0.39 | 15.484 | NO | 6.820  | 1.66 |
| 20d | 9 ⚠️ | 8 | 3.067 | 0.40 | 10.591 | NO | 12.914 | 1.90 |

### Illiquid tier (N=1 — completely untestable)

1 event only. All stats NaN.

---

## Cost check (6 bps threshold, reverse splits)

With alpha_dm=-25.3% at 1d (full universe, demeaned) and full universe alpha=-5.0% at 1d,
cost of 6 bps is negligible vs drift magnitude in the illiquid/mid tiers.
However the LIQUID tier has only N=4 events — net@6bps is not meaningful to compute.

---

## Key observations

1. **Reverse split drift is real and strong in the full universe.** All horizons canary-clear (1-20d),
   t-stats range from -2.38 to -3.55, demeaned alpha t-stats -8.66 to -10.67. Sign-correct (negative).

2. **The drift is concentrated in illiquid + mid tiers.** Illiquid t1 and mid t2 both show strong,
   consistent negative drift that clears canary at most horizons.

3. **Liquid tier (T3) reverse splits: N=4. Directionally consistent (all negative, canary-clearing
   at h=1,3,5d), but the pre-committed threshold is N>=20 for a non-UNDERPOWERED verdict.**
   With 4 events you cannot distinguish a real effect from 4 bad stocks.

4. **Forward splits: fully underpowered across ALL tiers.** N=16 full universe, N=9 liquid — none
   meet the N>=20 threshold. The sign is NEGATIVE (unexpected, predicted positive), but with N<20
   this cannot be trusted. The "negative forward split" could easily be 2-3 specific stocks.

5. **Per-symbol-demean survives strongly for reverse splits** (t_dm up to -10.67) — confirming
   this is cross-sectional relative underperformance, not a composition artifact.

6. **The meta-pattern holds again:** reverse-split drift is illiquid-concentrated. The liquid
   tier has nearly zero events (4/312 = 1.3% of reverse splits are in liquid names). This is
   structural: companies doing reverse splits are distressed, hence inherently illiquid.
