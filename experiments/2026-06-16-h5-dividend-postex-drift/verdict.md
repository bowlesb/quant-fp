# H5 Verdict: Dividend POST-EX Drift

**Verdict: KILL**

**Reason: Liquid-tertile OOS demeaned t = 0.67 (best across all horizons). Pre-reg threshold is t >= 2.0. Does not even approach the threshold. This is not the H10 "illiquid mirage" — there is NO signal even in the full universe (best OOS t = 0.86). The dividend post-ex drift does not exist in this universe and window.**

---

## Pre-registered Gate (from hypothesis.md)

> KEEP-AS-LEAD: liquid-tertile OOS demeaned t >= 2 at >= 1 horizon, net of 6 bps.
> AMBIGUOUS: full-universe holds but liquid is borderline (1.5 <= t < 2).
> KILL: liquid tertile dead (t < 1.5) — the now-familiar illiquid mirage.

**Result against pre-reg:**
- Liquid-tertile OOS: best |t_dm| = 0.67 → KILL (t < 1.5)
- Full-universe OOS: best t_dm = 0.86 → also KILL (no signal anywhere)

---

## What Was Found

**Nothing tradeable.** Every horizon, every universe slice:

1. **Liquid-tertile OOS: t in [-0.67, +0.51]** — noise. The liquid gate would stop any deployment.
2. **Full-universe OOS: t in [+0.02, +0.86]** — near-zero. No illiquid mirage to explain.
3. **Full-period demeaned alpha is weakly NEGATIVE** (-0.164% to -0.377% depending on horizon) — weak mean reversion, not drift.
4. **Shuffle canary:** liquid-tertile OOS alpha does not clear canary p95 at any horizon.

---

## The High-yield / Liquid-tertile 10d "Signal" — Explicitly Disclaimed

The high-yield / liquid-tertile OOS at 10d shows alpha_dm = +2.311%, t_dm = 3.08. This looks attractive but must be KILLED as a lead:

1. **In-sample 10d TRAIN t_dm = 0.81** — no in-sample support for the OOS number. Per RESEARCH_PITFALLS.md Rule #4: a strong OOS result with no IS backing = spurious noise, not discovery.
2. **Does not clear the canary:** alpha_dm = 2.311% vs canary_p95 = 5.321% — the observed alpha is BELOW the 95th percentile of random permutations.
3. **Post-hoc horizon selection:** the 10d horizon was not pre-committed before observing the yield split results. Selecting it after seeing the data = textbook multiple-comparison / overfit.
4. **Small N:** 173 events on 48 dates, one tercile of one split of one sub-universe. At this N, random OOS fluctuations can easily produce t ~ 3.

This pattern — zero IS signal, strong canary violation, post-hoc selection, small N — is a false discovery. Not to be acted on, not to be pre-registered as a new hypothesis. It would fail an independent replication.

---

## Context: H5 vs H10 (the meta-pattern)

H10 was the "illiquid mirage" prototype: strong full-universe signal (OOS t 2.7) that vanished to t ~ 0.3 in the liquid tertile. H5 is a different failure mode:

- **No signal anywhere** — not even in the full illiquid universe
- Full-universe OOS t < 1 at all horizons
- The academic post-ex dividend drift (Elton-Gruber etc.) does not survive in this universe/window, even in illiquid names

Possible reasons:
- The 126-day window (2025-12-15 to 2026-06-16) is short; dividend events are ~3-4/year per payer → event density is low
- The documented anomaly may be arbitraged away in US large/mid caps (our universe skews toward listed names)
- The ex-date drop is price-adjustment mechanical; "drift" above that is the hypothesis that failed here

---

## Standing Conclusion Update

This is now 0/3 for tradeable edges this cycle:
- H1 (VWAP reversion): ILLIQUID MIRAGE — died liquid
- H10 (8-K drift): ILLIQUID MIRAGE — died liquid
- H5 (dividend post-ex drift): NO SIGNAL ANYWHERE — full universe dead

For H4 (split post-ex drift, same design): expect the same or worse given lower event count (244 splits vs 6,042 dividends). The probability that the edge class "corporate-action post-ex drift" has a tradeable liquid signal is very low based on this evidence.

**The pre-registered 20% prior that H5 would survive the liquid gate was already skeptical. The result is consistent with that skepticism.**

---

## No Action Required

- No feature to implement
- No strategy to develop
- Raw results archived in `raw_results_h5.json`
- The high-yield 10d OOS number does NOT warrant a new experiment without independent pre-registration and a held-out data source
