# H12 — Mid-session longer-horizon momentum, with a HOLD-OUT (testing a POST-HOC finding honestly)

**Registered:** 2026-06-16 (before any new run). This tests a finding that emerged POST-HOC from H11's
robustness check — so it carries an EXPLICIT overfitting risk and is designed around a HOLD-OUT to defeat
hindsight. A result without an out-of-sample confirmation does NOT count.

## The finding that motivates it (and why to distrust it)

H11 (timezone-corrected) found the full-session momentum L/S marginal (W30/H120 demeaned net@6 +9.1 bps,
t=1.51 < 2; W60 fails canary). But its robustness check — restrict entries to **mid-session 10:00–15:30 ET**,
excluding the first/last 30 min — produced a MUCH stronger W60/H120 cell: gross +25.5 bps, net@6 +20 bps,
**t=3.27**, clears canary. W30/H120 mid-session also rose to t=2.82.

**Why distrust it:** this was NOT the pre-registered test. Excluding data and finding the signal IMPROVES is
a classic multiple-testing / overfit smell, and at H=120 mid-session only **2–3 entry slots remain** (11:30
+ 13:30 ET), so the t-stat rests on very few independent observations per day. It could be a real
"mid-session momentum is cleaner than the noisy open/close" effect (plausible — open/close are
microstructure-dominated), OR a small-N mirage. The ONLY honest way to tell is out-of-sample.

## Hypothesis (pre-committed, specific)

Mid-session (10:00–15:30 ET entry) longer-horizon momentum — long top-vwap_dev decile, short bottom, W60 /
H120 — has a TRUE day-clustered t ≥ 2.0 and net@6bps > 0, AND this holds on a HOLD-OUT set of days NOT used
to surface the mid-session restriction.

## Test design (the hold-out is the whole point)

1. **Split days into TRAIN and HOLDOUT** by time: the H11 run used 2026-04-07 → 2026-06-16 (49 days). Acquire
   a LONGER history (target 100–150 trading days; bars go back to 2025-12-15 = ~126 days available) and
   designate the EARLIER ~half as HOLDOUT (never seen when the mid-session idea was formed) and the H11 window
   as the discovery/TRAIN set. The mid-session restriction was chosen on the TRAIN set; it must REPLICATE on
   the HOLDOUT with t ≥ 2.0 and positive net to count.
2. On BOTH sets, compute the W60/H120 (and W30/H120) mid-session momentum L/S: gross, turnover, net@4/6/10,
   day-clustered t, 10-seed within-CS shuffle canary, per-symbol-demean. Use the timezone-CORRECT constants
   (09:30 ET=810, 10:00 ET=840, 15:30 ET=1170 in UTC minutes) — and VERIFY one entry bar's timestamp by hand
   (RESEARCH_PITFALLS.md #1).
3. **Turnover control:** mid-session at H=120 rebalances ~2–3×/day; add a no-trade band (only flip a name's
   leg on a material vwap_dev change) and report net AFTER the band — the real economic number.
4. **Slot decomposition:** is the signal in the 11:30 slot, the 13:30 slot, or both? A signal that lives in
   ONE slot is more likely overfit; one present in both is more robust.

## Expected / confidence

- Confidence the mid-session signal REPLICATES on the hold-out with t ≥ 2.0 AND positive net after the
  no-trade band: **~25%.** It is directionally plausible (open/close are noisy; mid-session momentum is a real
  literature effect) but the discovery was post-hoc on 49 days with few slots, so the base rate for such
  findings replicating OOS is low. I am pre-committing to that low prior to avoid talking myself into it.
- KEEP: hold-out t ≥ 2.0 AND net@6 > 0 after the no-trade band, canary-clear, demean-survived, on BOTH slots
  (or robustly on one with a clear reason). THEN escalate (more days, more seeds, a paper container).
- AMBIGUOUS: hold-out 1.5 ≤ t < 2.0 or net positive only pre-band — needs more days, not yet a lead.
- KILL: hold-out t < 1.5 or net ≤ 0 after the band — it was a discovery-set overfit; momentum is marginal
  and not tradeable; close the price branch for good and stay on the event families.

## Ordering

Dispatch AFTER H10 (event drift) returns — one heavy job at a time. H12 reuses the H11 v2 infra (corrected
constants) on a LONGER day range. NOT a high-priority lead (25% prior); it is the honest closure of the one
loose thread H11 left, run properly rather than hand-waved. The event families (H10 now, then H5 dividends /
H4 splits — data landed) remain the primary pivot.
