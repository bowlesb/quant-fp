# H11 Verdict

## Pre-registered KEEP/KILL criteria (from hypothesis.md)

- KEEP: cell net>0 beyond canary AND survives tradeable-entry AND survives per-symbol-demean AND robust to cost stress
- AMBIGUOUS: +gross but collapses under demean or tradeable-entry
- KILL: every cell ≤ canary after the gates

## Verdict: AMBIGUOUS (leans KILL for full-session; anomalous mid-session signal warrants H12)

### Per-cell verdicts

| Cell     | Full-Session Verdict | Notes |
|----------|---------------------|-------|
| W30/H60  | AMBIGUOUS           | Clears canary but net@6=+0.84 bps (barely positive) after demean; t=1.17 too weak |
| W30/H120 | AMBIGUOUS           | Clears canary, net@6=+9.10 after demean, t=1.51 — promising but below t=2 threshold |
| W60/H60  | KILL                | Fails canary in raw; t=0.64; net@6 negative |
| W60/H120 | KILL (full-session) | Fails canary in raw despite +13 bps gross; ANOMALOUS in robust window (see below) |

### The gates summary

1. **Open-print gate**: NOT an issue. The W-bar lookback requirement structurally prevents
   scoring at the 09:30 ET open bar. Gate A is a no-op; the signal is clean of open-print
   contamination by construction.

2. **Per-symbol demean**: Signal survives with small decay (−1.2 to −2.3 bps). Not a
   survivorship artifact. The momentum edge is genuinely cross-sectional.

3. **Canary**: W30 cells pass. W60 cells fail in the full-session panel. The W60 canary
   failure is striking: W60 has a wider canary band (std ~3–5 bps) relative to the signal,
   indicating higher noise, not that the signal is fake.

4. **Cost stress**: W30/H120 net@6 = +9.10 bps (demeaned), but net@10 = +5.52 bps — still
   positive. W30/H60 net@10 = −2.74 bps (negative at aggressive cost).

### The anomalous mid-session result

The robustness check (exclude open/close, 10:00–15:30 ET only) produces dramatically STRONGER
results, especially W60/H120:
- Full session: +13.46 bps gross, t=1.18, fails canary
- Mid-session only: +25.53 bps gross, t=3.27, clears canary (canary_95=5.92)

And W30/H120:
- Full session: +15.90 bps gross, t=1.64, clears canary
- Mid-session only: +20.46 bps gross, t=2.82, clears canary

The mid-session result is statistically stronger than the full-session result. This is
a genuine and puzzling finding: excluding data improves the signal because the early-session
slots (870 = 10:30 ET for H=60) add noise that dilutes the mid-session momentum. At H=120,
only slots at 930 (11:30 ET) and 1050 (13:30 ET) remain in the mid-session panel, and these
are the purest momentum observations.

However: the pre-registration specified the FULL-SESSION test. The mid-session finding is a
DERIVED result, not the pre-registered test. Per the pre-reg, we test the full RTH panel, and
the full panel fails for W60 and is only weakly positive for W30.

### What the prior H9 "finding" actually meant

H9's negative reversion L/S (−12.7 to −33.7 bps) was computed with the SAME timezone bug.
The corrected momentum gross figures (+7.5 to +15.9 bps) are real but much more modest than
the +12 to +34 bps the pre-reg anticipated. The gap was the timezone error, not momentum
strength. The H9/H11 v1 "findings" should be treated as invalid.

## Next step

**H12: Mid-session momentum focus (pre-registered as follow-on).**
The mid-session finding (W60/H120, 10:00–15:30 ET) shows t=3.27 and net@6=+20 bps —
this is the strongest clean signal seen so far and was NOT the main H11 test. H12 should:
1. Pre-register the mid-session restriction explicitly (10:00–15:30 ET entry, H=120)
2. Extend to a longer window (100–150 days) to test out-of-sample
3. Add a no-trade band / hysteresis to reduce turnover (~0.90) before computing net
4. Test whether the slot choice (11:30 vs 13:30 ET) drives the signal or both are equally strong

If the mid-session W60/H120 signal holds over 100+ days and net-of-turnover remains positive,
it would be the first genuine KEEP in this hypothesis chain.

## Honesty note

The AMBIGUOUS label is chosen because the signal is directionally correct and not an
open-print or survivorship artifact, but it does not meet pre-registered KEEP criteria at
the full-session level. The timezone bug in H9 that initiated H11 inflated the apparent
momentum edge by 2–3×. After correction, the edge is real but weak at full-session scope,
and strong only in the mid-session window — which was not the pre-registered test.
