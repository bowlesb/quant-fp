# PRE-REGISTRATION — #1: re-grade the highest-GROSS prior nulls under accurate liquid-head cost

**Date:** 2026-06-20  **Author:** Modeller  **Status:** GATE-READ REQUESTED (no numbers produced yet)
**Thesis:** Stage 1 proved the flat 3.0 bps backtest cost stub was ~2.8x too HIGH for the liquid head
(AAPL/NVDA/MSFT realized ~0.7-1.9 bps). Some prior nulls may be a COST-MEASUREMENT artifact: real gross
signal that "died net" was charged ~2-4x its true cost at exactly the liquid names where it lives. This
re-grades a FIXED set of nulls under the now-accurate per-name cost, restricted to a pre-committed liquid-head
universe. **A null staying null is the strongest exhaustion confirmation we can produce — and is the
expected, publishable outcome.**

## 0. ⚠️ THIS IS THE HIGHEST-FOOLING-RISK TEST WE'VE DESIGNED — anti-fooling is locked BEFORE any numbers
Re-grading nulls under more-favorable cost is textbook p-hacking. Every guard below is pre-committed; none
may be relaxed after seeing a result. The honest prior: most of our nulls were null on GROSS signal or had
gross signal in the ILLIQUID tail (the opposite of cost-favorable) — so few are even ELIGIBLE, and most will
stay null. That is fine.

---

## 1. The GROSS-SIGNAL ELIGIBILITY FILTER (pre-committed — most nulls do NOT qualify)
A prior null may be re-graded ONLY if it meets BOTH, documented from its existing VERDICT before re-grade:
- **(E1) it had a POSITIVE gross signal** — its gross AUC and/or rank-IC was ABOVE its control (the signal
  improved ranking), i.e. it died on COST/turnover, not on absent gross signal; AND
- **(E2) that gross signal was NOT illiquidity-concentrated** — it must NOT have been documented as collapsing
  on a liquid universe / dominated by a size/illiquidity feature (those live in the illiquid tail where
  accurate cost is HIGHER than the stub — cheaper liquid-head cost makes them WORSE, not better).

This filter is itself anti-fooling: it forbids fishing across nulls that never had liquid-head gross signal.

## 2. THE FIXED NULL LIST (pre-committed — no adding/dropping after results)
Applying §1 to the settled nulls, exactly these are re-graded. The list is CLOSED.

| # | Null | Gross signature (from its VERDICT) | Eligible? |
|---|------|-----------------------------------|-----------|
| N1 | **quote-alpha G0a** (quote-dynamics proxies, #268) | Gross IMPROVED: AUC 0.529→0.536, rankIC +0.046→+0.058 on liquid-200; died NET at every cut. | **YES** — E1 met (gross up), E2 met (liquid-200 universe, not illiquidity-concentrated). The cleanest candidate. |
| N2 | **order-flow cross-sectional** (0/4, #orderflow-verdict) | Gross NULL: NW\|t\| all <1.2, IC did not clear shuffle. | **NO** — fails E1 (no gross signal). Re-grade is pointless; recorded as ineligible. |
| N3 | **swing_dc magnitude as $** (#259) | Incremental AUC/rankIC DROPPED (.535→.533 / +.064→+.061) — redundant with baseline. | **NO** — fails E1 (gross fell). |
| N4 | **path-geometry proxies** (G0, #263) | Incremental AUC/rankIC DROPPED (.529→.526 / +.039→+.033). | **NO** — fails E1. |
| N5 | **Lane C overnight** (illiquidity-concentrated) | Full-univ t=3.89 but LIQUID top-1500 COLLAPSES to t=1.20; size-feature-dominated. | **NO** — fails E2 (illiquidity-concentrated; cheaper liquid cost makes it worse). |
| N6 | **Lane D EDGAR/sector direction** (0/13) | Clean direction null; surviving signal predicts VOLUME not return. | **NO** — fails E1. |

**Re-graded set = {N1}.** N2-N6 are recorded as ineligible-by-pre-committed-filter (not silently dropped —
their ineligibility IS a documented anti-fooling result). **N (the multiple-testing count, §5) = 1** for the
re-graded set; if the Lead wants additional borderline nulls added, they are added HERE before any run and N
is incremented accordingly.

> NOTE for the gate-read: if you believe a null I marked ineligible deserves re-grading, name it NOW — the
> list and N lock at gate-read approval, never after a number is seen. I deliberately kept the set minimal and
> honest rather than padding it (a longer list is more fishing surface, not more rigor).

## 3. THE PRINCIPLED LIQUID-HEAD UNIVERSE (pre-committed rule, defined BEFORE results)
Restricting to low-cost names is a LEGITIMATE strategy refinement (trade only where execution is cheap and
realistic) — but ONLY if the rule is fixed in advance, never a post-hoc cut that maximizes the result. The
rule, FIXED here:
- **Universe = names whose Stage-1 REALIZED half-spread at the entry instant is < 2.0 bps**, on the same 42
  well-covered dates / forward-30m / $1-floor substrate. (2.0 bps is chosen as ~the liquid-head boundary the
  Stage-1 distribution showed: mega-caps ~0.7-1.9, the median ~6.5 — so <2bps isolates the genuinely-cheap
  head without being tuned to any result.) A name with no measurable realized cost is EXCLUDED (not stubbed).
- This is the ONLY universe. No sweep over the threshold; no "also tried top-N ADV." If a robustness check on
  the threshold is wanted, it is a SEPARATE pre-registered test, not a within-this-test choice.

## 4. THE FULL ROBUSTNESS GATE (the SAME bar that nulled them — not total-$ at one cut)
N1 "passes" (clears re-grade) ONLY if ALL hold, under Stage-1 realized cost on the liquid-head universe:
- **(G-$) net-$ UP vs its baseline at ALL of {2, 5, 10}% cuts** (not one cut; a single-cut total-$ blip is
  still a null — the swing_dc/path-geom 2%-cut-outlier trap);
- **(G-rank) AUC AND rank-IC UP** vs baseline (gross ranking genuinely better, not just cost arithmetic);
- **(G-t) per-day NW-t of the basket return significant** (|t| ≥ 2.0) and NOT driven by one outlier day
  (report the per-day distribution + the max-day contribution, as the path-geom G0 robustness check did);
- **(G-null) dominates BOTH shuffle and predict-zero baselines** at every cut.
A "pass" missing ANY leg is a NULL.

## 5. MULTIPLE-TESTING honesty
Re-grading the set = N tests (N = |re-graded set|, pre-committed = 1 here). The pass/fail of each is
two-sided on the per-day NW-t; apply **Benjamini-Yekutieli q=0.10** across the N (reuse
`quantlib.battery.family.benjamini_yekutieli`). With N=1 the correction is trivial, but it is APPLIED and N
is fixed in advance — if the Lead expands the set, the BY correction scales with the larger N. One "pass" out
of many under relaxed cost does not survive without the correction.

## 6. DISJOINT-WINDOW REPLICATION (the swing_dc standard — required for any pass)
Anything that clears §4 + §5 on the discovery window must REPLICATE on a DISJOINT window (same sign, net-$ up
across cuts, per-day t still significant). The window split is fixed BEFORE running: discovery = 2026-04-15..
05-14, replication = 2026-05-15..06-12 (the two natural halves of the 42-date substrate; the May-15 trusted-
coverage step makes them a clean break). No pass is reported without replication.

## 7. PRE-COMMITTED NULL BRANCH (the expected, decisive outcome)
If N1 stays null under accurate liquid-head cost (and N2-N6 remain ineligible), that is the STRONGEST
exhaustion confirmation we can produce: the null streak was NOT a cost-measurement artifact even at the names
where edge could most plausibly survive. That is a clean, publishable, decisive result → proceed to **#2: the
#205 weekly-reversal hunt** (the turnover/horizon attack on the actual cause of death — cost amortized over a
multi-day hold), carrying the survivorship-haircut discipline (the deep panel is 0/400 delisted).

## 8. Construction notes (no-look-ahead, reuse)
- Reuse the existing harness A/B machinery + the merged `quantlib.data.realized_cost.realized_half_spread_bps`
  (Stage 1). The cost charged is the measured realized half-spread at entry (truth, ex-post — legitimate for a
  backtest). No new feature code; this is a re-grade of EXISTING signals on a pre-committed universe.
- The entry/label discipline is unchanged from the original null (>=09:35 ET tradeable, forward-30m xs-excess,
  $1 floor) — re-grading must not alter ANYTHING except the cost term and the pre-committed universe cut, so
  the comparison is apples-to-apples with the original null.

## 9. What this is NOT
- NOT a re-grade of nulls that had no gross signal or illiquidity-concentrated signal (§1 forbids it).
- NOT a universe/threshold sweep (§3 fixes one rule).
- NOT a single-cut total-$ pass (§4 requires the full bar).
- NOT run yet — gate-read first. Send me your additions/removals to the fixed list (§2) and the universe rule
  (§3) BEFORE I produce any number.
