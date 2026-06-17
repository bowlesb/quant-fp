# W14 — results (REAL numbers)

Liquid tier = 300 names, 63 sessions (2026-03-18 .. 2026-06-16). Median liquid half-spread = 3.30 bps
(p90 7.25) → round-trip ≈ 6.6 bps. Multi-day entry = next-session open after the burst day, exit = +h
session closes. All cohort numbers are per-symbol-demeaned (burst minus same-day non-burst control), in bps.

## Burst-event counts (liquid)
| k  | burst-days | with 8-K (±1d) | NO catalyst |
|----|-----------:|---------------:|------------:|
| k2 | 720        | 226 (31.4%)    | 494         |
| k3 | 447        | 156 (34.9%)    | 291         |
| k4 | 297        | 124 (41.8%)    | 173         |

Base 8-K rate across ALL liquid days = 12.3%. Burst days are strongly enriched for an 8-K, and enrichment
RISES with burst violence (31%→42%) — the most violent bursts are disproportionately news-driven.
(Source: `stage4` console + `cache/merged.parquet`.)

## HORIZON-DECAY CURVE (k2, per-symbol-demeaned diff in bps; t-stat)
| horizon | ALL          | catalyst (8-K) | NO-catalyst   |
|---------|-------------:|---------------:|--------------:|
| 5 min   | −2.2 (−0.85) | +4.6 (+1.64)   | −4.4 (−1.41)  |
| 30 min  | −3.8 (−0.63) | +4.1 (+0.54)   | −2.2 (−0.32)  |
| 1 day   | +2.7 (+0.20) | −5.7 (−0.27)   | +6.2 (+0.36)  |
| **2 day** | **−17.6 (−0.77)** | **−43.2 (−1.14)** | **−5.8 (−0.20)** |
| 5 day   | −21.9 (−0.54)| −76.8 (−1.29)  | +5.0 (+0.10)  |

Stronger-gate echoes (full curve in `horizon_decay.csv`): the multi-day drift is NEGATIVE (a violent burst
tends to mark a local TOP → reversal), and it is **monotonically driven by the CATALYST subset** — at 2-day
the 8-K cohort drifts −43 bps (k2), −31 bps (k3), −27 bps (k4), strengthening to −77/−120/−107 bps at 5-day
(t up to −1.7). The catalyst subset is the post-8-K reaction reversing — i.e. textbook short-horizon
news-overreaction / mean-reversion, NOT a new signal.

## THE DECISIVE SPLIT — 2-day, CATALYST vs NO-CATALYST
- **Catalyst (8-K) 2-day**: −43.2 bps (k2), −30.7 (k3), −27.3 (k4). The only sizeable multi-day drift lives
  here. It is the known news/PEAD-family effect (here a reversal), re-labelled.
- **NO-catalyst 2-day (the HEADLINE)**: −5.8 bps (k2, t=−0.20), −9.6 (k3, t=−0.23), −6.0 (k4, t=−0.10).
  **Essentially flat and statistically indistinguishable from zero.** The no-catalyst burst does NOT carry a
  2-day drift. The novel attention/activity signal is absent.

## PRIMARY GATES — 2-day NO-CATALYST (the pre-registered decisive cell)
(`primary_2d_nocatalyst.csv`; net = signed by train-direction, per-trade bootstrap on non-overlapping 2-day
round-trips, 10k resamples; cost = round-trip 2×half-spread, plus a 2× stress.)

| k  | full diff (bps,t) | canary band [lo,hi] | **canary pass** | OOS diff (bps) | train→OOS dir | n OOS trades | gross OOS (bps) | **net OOS (bps) [95% CI]** | net 2× (bps) [CI] |
|----|------------------:|--------------------:|:---------------:|---------------:|:-------------:|-------------:|----------------:|---------------------------:|------------------:|
| k2 | −5.8 (−0.20)      | [−50.2, +19.3]      | **FAIL**        | +6.2           | −1 (flip)     | 231          | −84.9           | **−92.9 [−159.3, −30.0]**  | −100.9 [−164.6, −40.4] |
| k3 | −9.6 (−0.23)      | [−81.0, +25.8]      | **FAIL**        | +18.6          | −1 (flip)     | 146          | −91.9           | **−99.5 [−185.1, −21.8]**  | −107.0 [−192.3, −27.8] |
| k4 | −6.0 (−0.10)      | [−88.1, +39.2]      | **FAIL**        | +15.3          | −1 (flip)     | 89           | −94.2           | **−101.8 [−209.5, −6.6]**  | −109.5 [−221.1, −13.5] |

Reading:
- **Canary FAIL at every k**: the real no-catalyst 2-day diff (−6 to −10 bps) sits well INSIDE the shuffle
  band (±20–88 bps) — i.e. it is pure noise; date-shuffled burst flags produce diffs of the same magnitude.
- **Sign instability**: the in-sample (train) diff is negative, so the signed portfolio shorts the burst;
  but OOS the diff FLIPS positive (+6 to +19 bps) — the direction does not replicate out-of-sample, the
  hallmark of an overfit/noise cell.
- **Net-of-cost decisively negative**: −93 to −102 bps per trade, with the bootstrap 95% CI entirely BELOW
  zero (e.g. k2 [−159, −30]). The pre-registered DECISIVE condition (LIQUID OOS 2-day no-catalyst net-of-cost
  per-trade bootstrap CI > 0) is the OPPOSITE of satisfied — CI excludes zero on the losing side.

## Power / honesty notes
- 63 trade-days only (the burst window). The 10-day trailing baseline costs the first ~10 days, leaving ~53
  usable days; 494 k2 no-catalyst burst-days is a reasonable n, but the day-clustered information content is
  limited and the per-trade bootstrap CIs are wide.
- The catalyst (8-K) reversal at 2–5 day IS real and sizeable but is the known news/overreaction family — out
  of scope as a "new" signal and, being a fade of news on liquid names, itself faces the usual borrow/timing
  frictions; not pursued here.
- Intraday (5/30-min) decay is small and mixed-sign; no microstructure edge either, consistent with HF01–03.
