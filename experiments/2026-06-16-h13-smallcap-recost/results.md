# H13 Results: Small-capital re-cost of illiquid signals

**Run date:** 2026-06-16  
**Script:** `run_h13.py` (exit code 0)  
**Universe:** 7,331 symbols with ADV; 2,504 with measured quotes  
**Split:** TRAIN = 2025-12-15 to 2026-03-17 (63 days) | OOS = 2026-03-18 to 2026-06-16 (63 days)  
**Tertile cutoffs:** top 2,443 = liquid, middle 2,444 = mid, bottom 2,444 = illiquid (by median 20d ADV)

---

## 1. Per-tier round-trip cost at $5K and $10K/position (k=10, baseline)

| Tier | n | Median ADV | Spread source | Median half-spread | Median rt $5K | Median rt $10K | p90 rt $5K |
|------|---|------------|---------------|--------------------|---------------|----------------|------------|
| Liquid | 2,443 | $66.5M | 98% measured | 6.4 bps | **32.6 bps** | 40.5 bps | 68.3 bps |
| Mid | 2,444 | $1.56M | 3% measured | 21.8 bps | **157 bps** | 204 bps | 297 bps |
| Illiquid | 2,444 | $35,822 | <2% measured | 33.3 bps* | **813 bps** | 1,123 bps | 4,737 bps |

*Estimated via spread model (R²=0.25), extrapolated ~1800× beyond the fit range. Almost certainly an under-estimate.

**Institutional vs. small-capital cost comparison (liquid tier only):**  
The institutional model used 30–200 bps/side. At $5K in liquid names, the realistic cost is ~33 bps
round-trip (~16 bps/side) — a genuine improvement. However, this applies only to the **liquid tertile**,
which H10b Gate 3 already confirmed has **zero alpha** (OOS t≈0.5).

**The fatal realization:** The illiquid tertile where the alpha concentrates has median ADV of only $35,822.
A $5K order is already **14% of daily volume**. The square-root impact model produces ~747 bps of impact
alone at k=10 (and ~373 bps even at optimistic k=5). The half-spread on top of that is an additional
33+ bps (estimated, likely understated). This is not a cost regime reduction from institutional levels —
it is worse, because dollar participation as a fraction of ADV is enormous even at $5K.

---

## 2. H10 illiquid-tertile OOS net alpha: $5K and $10K vs. institutional

Open entry (D+1 open price), per-symbol demean within OOS split, canary permutation gate.  
Cost deducted = cohort-median round-trip cost at the given order size.

### Baseline k=10

| Horizon | OOS $5K dm% | OOS $5K t_dm | OOS $10K dm% | OOS $10K t_dm | 2x-spread $5K dm% |
|---------|------------|--------------|--------------|---------------|-------------------|
| 1d | **-3.42%** | -1.64 | -6.52% | -3.11 | -4.09% |
| 3d | **+0.58%** | +0.19 | -2.52% | -0.83 | -0.09% |
| 5d | **-1.89%** | -0.71 | -4.98% | -1.89 | -2.55% |

All cells net-negative or noise-level positive. No horizon clears t≥2.

### Sensitivity k=5 (optimistic impact, frictionless fill)

| Horizon | OOS $5K dm% | OOS $5K t_dm | OOS $10K dm% | OOS $10K t_dm | 2x-spread $5K dm% |
|---------|------------|--------------|--------------|---------------|-------------------|
| 1d | +0.31% | +0.15 | -1.24% | -0.59 | -0.36% |
| 3d | +4.31% | +1.42 | +2.77% | +0.91 | +3.65% |
| 5d | +1.84% | +0.70 | +0.30% | +0.11 | +1.18% |

At k=5 (half the standard impact coefficient), 3d shows a mildly positive mean (+4.3% dm) but t=1.42 —
below the t≥2 bar required by the pre-registration. No horizon clears significance. The 2x-spread stress
on 1d is already negative. All are noise-level at $10K.

### Sensitivity k=20 (adverse selection / thin book)

| Horizon | OOS $5K dm% | OOS $5K t_dm |
|---------|------------|--------------|
| 1d | -10.9% | -5.21 |
| 3d | -6.89% | -2.26 |
| 5d | -9.36% | -3.54 |

Deeply negative at k=20.

### Summary vs. institutional baseline

| Cost model | 1d OOS dm% | 3d OOS dm% | 5d OOS dm% |
|------------|-----------|------------|------------|
| Gross (no cost deducted) | +1.79%* | +3.55%* | +2.69%* |
| Institutional 30 bps/side | ≈ -4.4% | ≈ -2.5% | ≈ -3.4% |
| Small-capital k=10, $5K | **-3.42%** | **+0.58%** | **-1.89%** |
| Small-capital k=5, $5K | +0.31% | +4.31% | +1.84% |
| Small-capital k=5, $5K, 2x-spread | -0.36% | +3.65% | +1.18% |

*These gross numbers are for the full universe (all tertiles), not the illiquid-only cohort.  
The illiquid-tertile train alpha (uncosted) is +2.6%, +4.7%, +4.1% at 1d/3d/5d — so small-capital
costs at k=10 completely consume and invert the OOS signal.

---

## 3. Capacity ceiling

Gross alpha used: OOS 1d illiquid-tertile dm% at $5K (k=10) = -3.42% (i.e., **negative**).  
This means the capacity analysis is moot — there is no capital level at which net alpha is positive.

To illustrate nonetheless (using the 1% ADV participation cap):

| k | Order size | n names eligible | Total capital | Median rt cost | Net alpha |
|---|-----------|-----------------|---------------|----------------|-----------|
| k=5 | $1,000 | 594 | $594,000 | 139 bps | < 0 (gross alpha negative) |
| k=10 | $1,000 | 594 | $594,000 | 220 bps | < 0 |
| k=20 | $1,000 | 594 | $594,000 | 383 bps | < 0 |

At $1,000/name (the smallest tested), only 594 of 2,444 illiquid names even qualify (those with ADV ≥ $100K, i.e., 1% × ADV ≥ $1K). Total deployable capital = $594K. Round-trip cost at k=10 is already 220 bps at this tiny size — far exceeding any signal.

**At $5K/name with 1% ADV cap:** 0 illiquid names qualify (median ADV = $35,822 → 1% = $358 max per name, far below $5K).

**Capacity ceiling = $0 at any practical order size.**

---

## 4. Spread-estimation honesty table

| Source of cost uncertainty | Direction of error | Magnitude |
|----------------------------|--------------------|-----------|
| Spread model R²=0.25 (poor fit even in-sample) | Likely under-estimate | Could be 2–5× off |
| Extrapolation 1800× beyond fit range | Under-estimate (spreads widen nonlinearly) | Unknown; potentially 10× |
| 32/2,444 measured illiquid names | Unknown selection bias | Unknown |
| Impact model (sqrt, k=10) | Standard assumption | k=5 to k=20 reasonable range |
| 1% ADV participation cap for $5K | Not achievable for median illiquid stock | Capacity ceiling = $0 |

The 2× spread stress test applied in the re-score adds only ~67 bps to the round-trip cost (the estimated
half-spread contribution). But if true illiquid spreads are 10× the model prediction (i.e., 330 bps instead
of 33 bps), the round-trip cost would be ~7,600 bps — approximately the ADV of the underlying stock itself.
The signal is not robust to realistic illiquid-cost uncertainty.

---

## 5. Canary results

All OOS cells: canary p95 is well below the (negative) OOS alpha, confirming the negative result is not
a statistical artifact — costs genuinely destroy the signal. The pre-cost train-phase signal shows
n=2,124 event observations, canary p95 ~8.5 bps — the underlying drift is real pre-cost.
