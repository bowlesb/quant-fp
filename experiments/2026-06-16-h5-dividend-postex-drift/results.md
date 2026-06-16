# H5 Results: Dividend POST-EX Drift

**Run date:** 2026-06-16  
**Script:** `run_h5.py` (exit code 0)  
**Universe:** 7,337 symbols, 126 trading dates (2025-12-15 to 2026-06-16)  
**Event universe:** 6,042 dividend events (2,805 unique symbols) in bars universe  
**Split:** TRAIN = 2025-12-15 to 2026-03-17 (63 days) | OOS = 2026-03-18 to 2026-06-16 (63 days)  
**Liquid tertile:** 2,445 / 7,337 symbols by median daily dollar-volume  
**Cost:** 6 bps round-trip (net of cost on event side)

All returns in percent (%).

---

## PRIMARY: Liquid-tertile Walk-forward OOS — HEADLINE

The pre-registered primary gate. KEEP requires liquid-tertile OOS demeaned t >= 2.0 at >= 1 horizon net of 6 bps.

| Horizon | TRAIN n | TRAIN dm% | TRAIN t_dm | OOS n | OOS dm% | OOS t_dm | Clears t≥2? |
|---------|---------|-----------|-----------|-------|---------|---------|-------------|
| 1d | 1,391 | -0.226 | -1.64 | 1,045 | +0.085 | +0.51 | NO |
| 3d | 1,391 | -0.201 | -1.09 | 1,043 | -0.142 | -0.67 | NO |
| 5d | 1,391 | -0.159 | -0.70 | 1,038 | -0.094 | -0.38 | NO |
| 10d | 1,391 | -0.144 | -0.44 | 969 | +0.096 | +0.31 | NO |

**LIQUID-TERTILE OOS: best |t_dm| = 0.67. DOES NOT CLEAR t >= 2.0 AT ANY HORIZON.**

The effect is dead in liquid names across all horizons, both in-sample and out-of-sample. In-sample the demeaned alpha is even slightly NEGATIVE at all horizons (small magnitude, t < -2 only at 1d train borderline). This is not a signal suppressed by noise — it is genuinely absent.

---

## Full-Universe Walk-forward OOS (Secondary Context)

Full universe shows near-zero OOS alpha as well — this is not even a "dies in liquid" illiquid-mirage case. The full-universe signal is absent everywhere.

| Horizon | OOS n | OOS dm% | OOS t_dm |
|---------|-------|---------|---------|
| 1d | 2,541 | +0.005 | +0.02 |
| 3d | 2,539 | +0.029 | +0.09 |
| 5d | 2,519 | +0.182 | +0.46 |
| 10d | 2,360 | +0.472 | +0.86 |

Full-universe OOS demeaned alpha is economically trivial (+0.005% to +0.472%) and statistically zero (best t = 0.86). There is no full-universe dividend post-ex drift to explain away by illiquidity.

## Full-Universe Full-Period (No-Split Baseline)

Context: demeaned alpha over the full period without splitting.

| Horizon | N events | dm% | t_dm |
|---------|---------|-----|------|
| 1d | 5,968 | -0.164 | -0.88 |
| 3d | 5,966 | -0.309 | -0.94 |
| 5d | 5,946 | -0.340 | -0.79 |
| 10d | 5,787 | -0.377 | -0.63 |

The full-period demeaned alpha is slightly NEGATIVE across all horizons — a weak reversal pattern, not drift. No horizon clears t = 1.5.

---

## Dividend Yield Split

Tercile thresholds computed from `cash_amount / close_on_ex_date` across 5,968 in-panel events.
Approximately 1,988 events per tercile.

### High-yield tercile — Liquid-tertile OOS

| Horizon | TRAIN n | TRAIN dm% | TRAIN t_dm | OOS n | OOS dm% | OOS t_dm |
|---------|---------|-----------|-----------|-------|---------|---------|
| 1d | 190 | +0.071 | +0.26 | 177 | +0.810 | +1.62 |
| 3d | 190 | +0.093 | +0.29 | 177 | +0.843 | +1.64 |
| 5d | 190 | -0.033 | -0.07 | 177 | +0.528 | +0.96 |
| 10d | 190 | +0.545 | +0.81 | 173 | +2.311 | **+3.08** |

**WARNING — do NOT treat this as a KEEP signal.** The 10d OOS t = 3.08 is a post-hoc finding:
- In-sample 10d t_dm = 0.81 (not significant) — OOS "beats" in-sample with no prior signal = pure noise
- The yield split was pre-registered as a conditioning variable, but horizon selection (10d specifically) was not pre-committed independently of the results
- Canary p95 at 10d OOS = 5.321% vs alpha_dm = 2.311% — the alpha does NOT clear the canary (2.311 < 5.321)
- n = 173 events on 48 dates with zero in-sample support = false discovery rate extremely high
- Per RESEARCH_PITFALLS.md Rule #4 (post-hoc hold-out): this cannot be promoted as a lead

### High-yield tercile — Full-universe OOS (context)

| Horizon | OOS n | OOS dm% | OOS t_dm |
|---------|-------|---------|---------|
| 1d | 827 | +0.350 | +0.95 |
| 3d | 827 | +0.289 | +0.63 |
| 5d | 815 | +0.606 | +1.19 |
| 10d | 753 | +1.320 | +1.92 |

Full-universe high-yield OOS also slightly positive at 10d (t=1.92) but sub-threshold and also post-hoc.

### Mid-yield tercile — Liquid-tertile OOS

All horizons: t_dm in [-0.19, +0.10]. Dead.

### Low-yield tercile — Liquid-tertile OOS

| Horizon | OOS n | OOS dm% | OOS t_dm |
|---------|-------|---------|---------|
| 1d | 522 | -0.119 | -0.55 |
| 3d | 522 | -0.532 | -1.79 |
| 5d | 518 | -0.112 | -0.35 |
| 10d | 479 | -0.503 | -1.37 |

Low-yield events show a slightly negative demeaned alpha at 3d (t = -1.79, sub-threshold). No KEEP signal.

---

## Event Count Summary

| Subset | Total events | Symbols | Liquid-tertile events |
|--------|-------------|---------|----------------------|
| All dividends | 6,042 | 2,805 | ~2,436 (TRAIN 1,391 + OOS 1,045) |
| High-yield | ~1,988 | — | ~367 (TRAIN 190 + OOS 177) |
| Mid-yield | ~1,993 | — | ~825 |
| Low-yield | ~1,987 | — | ~1,244 |

---

## Canary Summary (Primary Liquid-tertile OOS)

Canary p95 vs actual demeaned alpha:

| Horizon | Canary p95% | Actual alpha_dm% | Clears canary? |
|---------|------------|-----------------|----------------|
| 1d | 0.195 | +0.085 | NO |
| 3d | 0.863 | -0.142 | NO |
| 5d | 0.746 | -0.094 | NO |
| 10d | 1.058 | +0.096 | NO |

None of the liquid-tertile OOS results clears the shuffle canary. The signal is indistinguishable from noise.

---

## Summary Table

| Gate | Result |
|------|--------|
| Liquid-tertile OOS: demeaned t >= 2.0 at any horizon | **FAIL** (best |t| = 0.67) |
| Full-universe OOS: demeaned t >= 2.0 at any horizon | **FAIL** (best t = 0.86) |
| Full-period baseline clears t = 1.5 | **FAIL** (best |t| = 0.94) |
| Canary cleared in liquid-tertile OOS | **FAIL** (none) |
| High-yield sub-split clears pre-reg gate | **FAIL** (post-hoc, no in-sample support, doesn't clear canary) |

**This is not even the H10 pattern (illiquid signal that dies in liquid) — there is no full-universe signal either.** The dividend post-ex drift simply does not exist in this data window for either liquid or illiquid names.
