# H10b Results: 8-K Drift Escalation

**Run date:** 2026-06-16  
**Script:** `run_h10b.py` (exit code 0)  
**Universe:** 7,337 symbols, 126 trading dates (2025-12-15 to 2026-06-16)  
**Split:** TRAIN = 2025-12-15 to 2026-03-17 (63 days) | OOS = 2026-03-18 to 2026-06-16 (63 days)  
**Liquid tertile:** top 2,445 / 7,337 symbols by median daily dollar-volume  
**Item-code sample:** 1,200 8-K filings across 1,017 unique CIKs (0 fetch errors), via SEC submissions API  

All returns in percent (%).

---

## Gate 1: Walk-forward OOS — ALL 8-K (close entry)

Per-symbol demean computed WITHIN each split (no cross-split leakage).

| Horizon | TRAIN n | TRAIN dm% | TRAIN t_dm | OOS n | OOS dm% | OOS t_dm | OOS clears t≥2? |
|---------|---------|-----------|-----------|-------|---------|---------|-----------------|
| 1d | 10,380 | +4.75 | 2.04 | 12,500 | +1.79 | **2.71** | YES |
| 3d | 10,380 | +5.43 | 2.51 | 12,131 | +3.55 | **2.56** | YES |
| 5d | 10,380 | +5.69 | 2.58 | 11,761 | +2.69 | 1.93 | NO |

**OOS demeaned t >= 2.0 at 1d AND 3d. KEEP bar met (requires ≥1 of {1d,3d,5d}).**

Magnitude shrinks OOS vs TRAIN (~60% of train alpha at 1d, ~65% at 3d) — classic attenuation, expected.
Alpha does not flip sign or collapse to zero OOS. The effect is real.

Canary OOS: p95 ≈ 0.09%/1.53%/1.94% at 1d/3d/5d — all well below OOS alpha.

---

## Gate 2: PEAD / Item-Code Split (earnings 2.02 vs non-earnings)

**Caution on sample size:** Only 158 earnings filings in TRAIN, 188 OOS — very thin; t-stats are underpowered.

### Earnings 8-K (item 2.02 present)

| Horizon | TRAIN dm% | TRAIN t_dm | OOS dm% | OOS t_dm |
|---------|-----------|-----------|---------|---------|
| 1d | -1.57 | -1.30 | +0.94 | 0.19 |
| 3d | -4.18 | -1.44 | -4.51 | -0.55 |
| 5d | -5.74 | -1.28 | -9.68 | -0.82 |

**Earnings-bearing 8-Ks show NO positive OOS drift at any horizon.** If anything, slightly negative (but t ≈ 0 — noise). The pooled 8-K signal is NOT PEAD.

### Non-earnings 8-K (item 2.02 absent)

| Horizon | TRAIN dm% | TRAIN t_dm | OOS dm% | OOS t_dm |
|---------|-----------|-----------|---------|---------|
| 1d | +47.00 | 1.65 | +3.64 | 0.75 |
| 3d | +33.46 | 1.61 | -1.62 | -0.32 |
| 5d | +36.79 | 1.75 | -5.73 | -0.92 |

**Non-earnings 8-Ks show massive in-sample "alpha" (+33–47%) that completely evaporates OOS.** The in-sample number is a red herring: n=372 events over 60 dates = ~6 events/day, not enough to stabilize the per-date alpha. The in-sample signal is dominated by a few outlier dates (M&A announcements, restructurings) that inflate the mean. OOS: random noise (t ≈ 0 at 3d/5d, slightly positive but underpowered at 1d).

**Interpretation:** The pooled 8-K OOS signal is NOT earnings (not PEAD), and is NOT "other 8-K events" either. It appears to be driven by a DIFFERENT mechanism — one that survives the full-population analysis but is diluted/absent in either sub-split due to:
1. Thin item-code sub-samples (1,200 from 17,000 total) — the signal may be in the unsampled 85%
2. The item-code sample may be non-representative (biased toward CIKs whose submissions page covers our 2025-2026 filings)

**Verdict on PEAD test:** INCONCLUSIVE — the item-code sub-sample is too thin and possibly biased to settle the question definitively. Neither subset shows OOS t≥2. The "not PEAD" reading is supported by the earnings-8K result, but the non-earnings result is too noisy to confirm a distinct signal.

---

## Gate 3: Survivorship Stress — Liquid Tertile OOS

Restricted to top 2,445 symbols by median daily dollar-volume (tradeable names, lowest delisting risk).

| Horizon | TRAIN dm% | TRAIN t_dm | OOS dm% | OOS t_dm |
|---------|-----------|-----------|---------|---------|
| 1d | +0.10 | 0.98 | +0.23 | 0.54 |
| 3d | +0.09 | 0.48 | +0.11 | 0.31 |
| 5d | -0.02 | -0.09 | -0.01 | -0.02 |

**Signal is COMPLETELY ABSENT in liquid names — BOTH train and OOS.** This is the critical finding:

- Pooled OOS alpha vanishes from +1.79%/+3.55% to +0.23%/+0.11% when restricted to tradeable names.
- The full-universe alpha comes exclusively from the bottom 2/3 of names by dollar-volume — illiquid, wide-spread stocks where execution cost would be ~30–200 bps per side, completely consuming the theoretical alpha.
- This is a survivorship/liquidity artifact, not a tradeable anomaly.

---

## Gate 4: Tradeable Entry Realism — D+1 Open, Net 6 bps

Entry at D+1 open price (first bar >= 09:30 ET / 13:30 UTC), exit at close[t+h], round-trip cost 6 bps.

| Horizon | TRAIN dm% | TRAIN t_dm | OOS dm% | OOS t_dm |
|---------|-----------|-----------|---------|---------|
| 1d | +4.07 | 1.89 | +1.48 | **2.26** |
| 3d | +4.93 | 2.41 | +3.09 | **2.38** |
| 5d | +5.14 | 2.45 | +2.20 | 1.67 |

Open entry barely dents the alpha (1d OOS: close=+1.79% vs open=+1.48%; 3d: 3.55% vs 3.09%). The 6 bps cost is trivial. Open-entry OOS holds t≥2 at 1d and 3d.

However, the liquid-tertile result (Gate 3) overrides this: the open-entry analysis also uses the full illiquid universe, so these t-stats are similarly contaminated.

---

## Summary

| Gate | Result |
|------|--------|
| OOS walk-forward: demeaned t≥2.0 at ≥1 horizon | PASS (1d t=2.71, 3d t=2.56) |
| NOT only PEAD (earnings-bearing 8-K has own positive OOS signal) | INCONCLUSIVE (thin sample) |
| Survives in liquid tertile | **FAIL (t≈0.3–0.5 in liquid names)** |
| Open-entry net-of-cost still positive | PASS (t=2.26, 2.38 at 1d/3d OOS) |
| Canary cleared | PASS (all OOS cells) |

**The critical failure is Gate 3: the effect lives almost entirely in the illiquid, untradeable tail of the universe.**
