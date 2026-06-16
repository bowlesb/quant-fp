# H10b Verdict: 8-K Drift Escalation

**Pre-registered KEEP bar:** OOS demeaned t >= 2.0 at >=1 of {1d,3d,5d} AND not wholly an illiquid/earnings-only artifact.

## Verdict: AMBIGUOUS — reframe as ILLIQUID-UNIVERSE signal only

**OOS walk-forward:** PASSES the t >= 2.0 bar at 1d (t=2.71) and 3d (t=2.56). The effect is real in the full universe, survives within-split demean, and holds through open entry (t=2.26/2.38).

**Liquid tertile:** FAILS completely (OOS t = 0.54 / 0.31 / -0.02). The signal lives in the bottom 2/3 of the universe by dollar-volume — stocks where round-trip costs are likely 30–200 bps per side, making theoretical alpha unclaimable in practice.

**Earnings (PEAD) vs non-earnings:** INCONCLUSIVE — item-code subsample (1,200/17,000 = 7% of filings) is too thin. Neither subset shows OOS t≥2. The earnings-8K OOS result (t≈0 at all horizons) does NOT support a PEAD reading. The non-earnings collapse in-sample→OOS is consistent with a noisy small-cap momentum artifact, not a genuine event signal.

## What the results mean

The H10 KEEP finding was real on its own terms: a cross-sectional 8-K event flag does produce positive OOS alpha in the full universe with demeaned t>2. BUT that alpha is concentrated in illiquid, untradeable names. It is consistent with:
1. Small-cap post-filing momentum where the information diffusion is slow (illiquid names mean slow price discovery — the "alpha" is actually a stale-price artifact from thin markets).
2. Selection: 8-K filers in the universe's illiquid tail are volatile, news-sensitive stocks; the event just flags a period of heightened volatility that tilts positive in a broadly-up market period.

The **liquid-tertile extinction (t ≈ 0.3–0.5)** is the definitive result. A signal that vanishes in the top third of names by volume is not tradeable at any meaningful scale.

## Next step (one line)

**REFRAME, do not kill outright:** investigate whether a LIQUID-ONLY 8-K event flag can be rescued by conditioning on filing type (M&A, guidance raise) or filing-day volume shock (a real reaction in a liquid name is a different signal than a stale-price drift in an illiquid one). Dispatch a follow-up pre-registered experiment: H10c, conditional-8K in liquid universe. Low prior (20%) given the liquid-tertile result here.

**Do NOT promote H10 to a feature as-is.** The illiquid-universe alpha cannot be harvested.
