# W3 — Results: 13D activist drift on LIQUID targets

Panel: 7356 symbols, 378 trading days (2024-12-11 .. 2026-06-16). Liquidity tertiles from median
20d dollar-volume: liquid=2447, mid=2447, illiquid=2448. Measured LIQUID median half-spread = 6.70
bps -> round-trip cost 13.4 bps (1x), 26.8 bps (2x stress). OOS split by date: TRAIN
2024-12-11..2025-09-15 (189d) | OOS 2025-09-16..2026-06-16 (189d) — 13D events populate BOTH halves
(a genuine walk-forward, unlike W2 whose 8-Ks were all OOS).

All numbers are **directional LONG the 13D cohort** (the documented activist-premium direction):
cohort open-entry forward return minus same-date non-event control, **per-symbol demeaned**,
**day-clustered** (one obs per event date). OOS "long net%" is the realized per-event D+1->D+H
round-trip, control-neutralized, **net of the 13.4 bps liquid round-trip cost**, with a 10k per-trade
bootstrap 95% CI. `excl0>` = lower CI bound > 0 (the KEEP condition).

## Event counts (with bars, entry-dateable)

| set | total | LIQUID |
|---|---|---|
| 13D initial (SCHEDULE 13D / SC 13D) | 1186 | **221** |
| 13D/A amendment | 4843 | 1581 |
| 13D all | 6029 | 1802 |

Composition note: the table is amendment-dominated (4.8k 13D/A vs 1.2k initial). The clean
"new >5% activist stake" info shock is the **initial** set; the headline below is initial-on-LIQUID.

## HEADLINE — 13D INITIAL, LIQUID tertile (PRIMARY), N=221 liquid events

| H | full demean% | t_dm | canary p95% | OOS long net% | net bootstrap CI | n | excl0>0 |
|---|---|---|---|---|---|---|---|
| 1d  | -1.57 | -1.69 | 0.52 | -0.16 | [-2.31, +1.96] | 88 | no |
| 3d  | -2.43 | -2.27 | 1.06 | -0.49 | [-3.46, +3.15] | 87 | no |
| 5d  | -2.75 | -1.55 | 1.11 | +0.90 | [-4.99, +10.02] | 86 | no |
| 10d | -3.68 | -1.75 | 2.48 | +2.09 | [-5.52, +12.10] | 86 | no |
| 20d | -6.28 | -3.58 | 4.34 | -4.24 | [-9.46, +1.26] | 81 | no |
| 40d | -6.94 | -2.16 | 9.15 | -5.09 | [-13.69, +5.13] | 74 | no |
| 60d | -8.78 | -2.28 | 3.21 | -9.98 | [-21.81, +3.72] | 63 | no |

The full-sample per-symbol-demeaned cohort drift is **NEGATIVE at every horizon** (−1.6% at 1d to
−8.8% at 60d), the OPPOSITE sign of the documented activist-announcement premium, and several
horizons are individually significant (t = −2.2 to −3.6 at 3/20/40/60d). The OOS per-trade bootstrap
CIs all **straddle zero** — no horizon excludes zero above. Not a tradeable long edge.

## 13D initial — Top-300 sub-cut (the most-liquid megacaps), N small

| H | full demean% | t_dm | OOS long net% | CI | n |
|---|---|---|---|---|---|
| 1d  | -2.37 | -1.37 | -0.60 | n<20 (no boot) | 9 |
| 3d  | -6.59 | -2.08 | -4.49 | n<20 | 9 |
| 5d  | -8.51 | -1.62 | -2.72 | n<20 | 9 |
| 10d | -12.95 | -1.54 | -2.92 | n<20 | 9 |
| 20d | -11.80 | -2.34 | -6.24 | n<20 | 8 |
| 40d | -23.56 | -2.43 | -15.92 | n<20 | 6 |
| 60d | -20.08 | -2.79 | -23.79 | n<20 | 5 |

Same negative sign, even larger in magnitude, but the OOS top-300 cell is **too thin (n=5–9)** to
bootstrap — "needs more history", not a verdict. (Full-sample demean t's are negative throughout.)

## 13D AMENDMENT (13D/A), LIQUID tertile, N=1581 liquid events

| H | full demean% | t_dm | OOS long net% | net bootstrap CI | n | excl0>0 |
|---|---|---|---|---|---|---|
| 1d  | +0.36 | +0.81 | +1.36 | [-0.53, +4.75] | 648 | no |
| 3d  | +0.27 | +0.63 | +0.79 | [-0.89, +3.68] | 640 | no |
| 5d  | -0.05 | -0.12 | +0.57 | [-1.05, +3.24] | 634 | no |
| 10d | -0.12 | -0.19 | +0.94 | [-1.53, +4.90] | 611 | no |
| 20d | +0.13 | +0.14 | +1.01 | [-1.94, +5.80] | 565 | no |
| 40d | +0.33 | +0.25 | -0.40 | [-3.75, +3.89] | 474 | no |

13D/A amendments on liquid names show essentially **no drift** (demean ~0, |t|<1 every horizon) and
every OOS CI straddles zero. The amendment is a weaker/noisier signal than the initial filing, as
expected (stake top-ups are not the same as a fresh activist arrival).

## 13D ALL (initial + amendment), LIQUID tertile, N=1802 liquid events

| H | full demean% | t_dm | OOS long net% | net bootstrap CI | n | excl0>0 |
|---|---|---|---|---|---|---|
| 1d  | +0.01 | +0.02 | +1.19 | [-0.54, +4.29] | 730 | no |
| 3d  | -0.19 | -0.44 | +0.64 | [-0.93, +3.16] | 721 | no |
| 5d  | -0.37 | -0.71 | +0.64 | [-1.14, +3.14] | 714 | no |
| 10d | -0.59 | -0.89 | +1.14 | [-1.41, +4.87] | 691 | no |
| 20d | -0.83 | -0.93 | +0.37 | [-2.39, +4.62] | 641 | no |
| 40d | -0.95 | -0.70 | -1.08 | [-4.23, +2.73] | 545 | no |
| 60d | -0.48 | -0.21 | -4.19 | [-7.34, -0.86] | 463 | no (excludes BELOW) |

Pooling washes the initial's negative drift into the amendment's flatness -> near-zero. The H=60d OOS
CI excludes zero on the WRONG side (−4.19%, CI [−7.34, −0.86]) — a net LOSS, not an edge.

## Context cohorts (full-universe / mid / illiquid)

13D/A full-universe and mid/illiquid: demean t's small or negative; the illiquid 13D/A short-horizon
demean is negative and significant (e.g. 5d t=−4.18) but net-of-cost straddles or loses. 13D_initial
mid/illiquid show the same negative-or-noisy pattern.

**The only OOS net-of-cost CIs that exclude zero above** (10 of 210 legs, scanning all 105 cells × 1x
and 2x cost) are ALL at the **H=60d horizon ONLY** and ALL in **non-PRIMARY context cohorts**:
13D_initial full-universe 60d (+36%, CI[+0.3,+80.8], demean t=−0.45), mid 60d (+95%, demean t=+0.79);
13D_all full-universe 60d (+20%, demean t=+0.04), mid 60d (+55%, demean t=+0.68); 13D/A Top-300 60d
(+11%, CI[+0.24,+22.6], n=40, demean t=+1.12). **Every one FAILS the per-symbol demean gate**
(demean t ≈ 0 or negative) — the positive raw net is un-demeaned market/beta drift over a 60-trading-day
hold in a rising OOS window (control-neutralization is weak at a quarter), NOT an activist-specific
effect. None is in the LIQUID PRIMARY tier. So: **ZERO demean-surviving LIQUID-tier cells clear the
net-of-cost OOS bootstrap above zero at any horizon.**

## Reading

The documented Brav-Jiang-Partnoy-Thomas activist premium is a 1990s–2000s phenomenon measured around
the announcement and over long horizons. In this 2024-12..2026-06 minute-bar universe, entering at the
**D+1 open** (after the announcement pop), the LIQUID initial-13D cohort drifts **negative**, not
positive — consistent with (a) the announcement jump already being captured before a D+1-open entry,
and (b) the strategy being crowded/arbitraged in liquid names, leaving post-pop mean-reversion. The
amendment and pooled sets are flat. No cell clears the net-of-cost OOS bootstrap.
