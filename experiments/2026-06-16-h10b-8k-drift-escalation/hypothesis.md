# H10b — 8-K drift ESCALATION: walk-forward OOS + survivorship + magnitude scrutiny

**Registered:** 2026-06-16 (before the escalation run). H10 produced the FIRST KEEP of the hunt: 8-K event
cohorts drift +2.95%/+5.69%/+5.53% (demeaned, t 1.97/3.05/2.96) at 1/3/5 trading days, clearing canary and
surviving per-symbol-demean; Form-4 KILLED (style bias); 8-K 10d KILLED (demean collapse). A first KEEP must
be scrutinized HARDER, not celebrated — this escalation is the gate before it becomes a tradeable lead or a
feature proposal.

## Why scrutinize the magnitude

A pooled-8-K demeaned 3-day excess of +5.7% is HUGE — among the largest documented anomalies if real. Three
benign explanations must be ruled out before believing it:
1. **It's just PEAD.** 8-K filings cluster on EARNINGS (item 2.02); post-earnings-announcement drift is the
   real, documented effect. If so, the "8-K signal" is PEAD wearing a costume — concentrated in the earnings
   subset, much smaller (or absent) in non-earnings 8-Ks. Need the item-code split to know.
2. **Survivorship + a positive-market regime.** The universe is CURRENT holdings only (winners), over a
   broadly-up Jan–Jun 2026. Event cohorts of survivors in an up market inflate absolute drift. A per-date
   demeaned cross-section partly controls this, but the in-sample demean uses each symbol's WHOLE-period mean
   (which includes its event dates) — a possible over/under-correction.
3. **In-sample, no hold-out.** H10's numbers are in-sample over one 6-month window. Per the standing hold-out
   rule (RESEARCH_PITFALLS #4), an in-sample KEEP is NOT a lead until it replicates out-of-sample.

## Hypothesis (pre-committed)

The 8-K 1–5d positive drift REPLICATES on a walk-forward OUT-OF-SAMPLE split (fit the cohort definition on
the first half, measure on the never-seen second half) with demeaned t ≥ 2.0 at ≥1 horizon, AND is not
wholly explained by the earnings (2.02) subset, AND survives a survivorship stress.

## Test design

1. **Walk-forward OOS:** split the 126-day window into TRAIN (first ~63 days) and OOS (last ~63 days). Report
   the 8-K demeaned alpha + day-clustered t SEPARATELY on each. The demean mean must be computed WITHIN each
   split (no cross-split leakage). KEEP requires the OOS half to hold demeaned t ≥ 2.0 at 1d/3d/5d.
2. **Item-code split (the PEAD test):** parse the 8-K filing-index for item codes (the per-filing fetch the
   backfill agent flagged) on a SAMPLE if a full parse is too slow — split earnings-bearing (2.02) vs
   non-earnings 8-Ks. Is the drift concentrated in 2.02? If the NON-earnings 8-Ks still drift demean-positive
   OOS, that is a signal beyond PEAD (more valuable). If it's all 2.02, it's PEAD (still real, but known).
3. **Survivorship stress:** restrict to the most-liquid tertile (least delisting risk) and re-measure; if the
   drift only lives in the illiquid tail, it inherits the H1 cost/tradeability problem.
4. **Tradeable entry realism:** re-book at D+1 OPEN (not close) — the realistic entry — and net the ~6 bps
   round-trip; the alpha is huge vs cost so this should barely dent it, but confirm.
5. Keep the 10-seed canary + per-symbol-demean on every cell.

## Expected / confidence

- Confidence the 8-K drift REPLICATES OOS with demeaned t ≥ 2.0: **~50%** — higher than any prior lead
  because (a) it's the documented PEAD direction, (b) it already survived demean + canary in-sample, (c) the
  cost wall is non-binding. The main risks are magnitude shrinkage OOS and the "it's only earnings + only an
  up-market" explanations.
- KEEP-AS-LEAD: OOS demeaned t ≥ 2.0 at ≥1 horizon AND (non-earnings subset still positive OR a clear
  earnings-concentrated PEAD signal that's tradeable net-of-cost). THEN propose a parity-safe feature
  (days-since-8-K event flag with available_at, look-ahead-safe) to the Lead.
- AMBIGUOUS: OOS positive but t in [1.5, 2.0), or wholly earnings-driven and the earnings subset is thin.
- KILL: OOS demeaned t < 1.5 (it was an in-sample / regime artifact).

## Ordering

THIS is now the top thread (first KEEP → escalate before believing). Dispatch next. H5 (dividend post-ex
drift) + H4 (split post-ex drift) remain queued — and a 2nd survivor would corroborate that EVENT drift is
the live signal class. H12 (mid-session momentum hold-out) stays low-priority.
