# Where does the NEXT genuine edge hypothesis come from? — honest read

**Date:** 2026-06-20  **Author:** Modeller  **For:** the Lead's sequencing decision.

## The graveyard (what is settled-null, so we stop re-chasing)
Bar-direction (intraday/overnight, ~5 nulls) · bar-geometry magnitude (swing_dc-as-$, path-geometry G0, +
#255 "~91% already in baseline" — 3x) · quote-alpha cross-sectional (G0a) · news/EDGAR/sector (Lane D).
The cost MODEL is infra, not alpha. **The common cause of death, every time, is NET-OF-COST at our scale and
turnover.** That is the thread to pull.

## The thing that just changed: cost is now accurate, and it's ASYMMETRIC
Stage 1 proved the flat 3.0 bps stub was **2.8x too HIGH for the liquid head** (AAPL/NVDA/MSFT realized
~0.7–1.9 bps) and too LOW for the illiquid tail (AMD 7, GILT 31 bps). Every prior null was graded with this
wrong-for-everyone flat cost. This is not a footnote — it reshapes the search:
- High-turnover liquid-universe signals that "died on cost" were charged **~2–4x their true cost**. Some may
  clear under correct cost. The nulls were RE-RUN-WORTHY at the liquid head specifically.
- The illiquid tail (where flat-cost UNDER-charged) is where prior "edges" were cost-optimistic mirages — we
  should now DISTRUST any past signal that lived in illiquid names.

## My ranked read of the un-nulled directions

### #1 (most promising) — RE-RUN the best-gross nulls under CORRECT cost, liquid-head only
Not a new hypothesis — a re-grade of old ones with the new truth. The G0a quote-dynamics signal IMPROVED
gross ranking (AUC .529→.536) and only "died" on net cost computed with the flat stub. The swing_dc magnitude
and several intraday signals were real-gross. **The single highest-EV move:** take the 2–3 highest gross-IC
nulls, restrict to the liquid head (where realized cost is ~1 bp not 3), and re-book with Stage-1 measured
cost. Cheap (the panels + harness exist; Stage 1 is merged), and it directly tests the hypothesis that our
nulls were a COST-MEASUREMENT artifact, not an absence of edge. If even ONE clears, that is the edge. If none
do, that is the strongest possible confirmation the bar+quote substrate is exhausted — either way decisive.
**This is what I'd run first.**

### #2 — LONGER HORIZON (multi-day / weekly), cost amortized over the hold
The structural reason cost kills us is TURNOVER. A weekly hold amortizes one cost over ~5 days, so a
per-period-weaker signal can be net-positive — and #205's weekly short-term REVERSAL was real-gross
(smoke IC +0.075, +69 bps net @5 bps). It is pre-registered and turn-key (build_weekly.py + screen.py) but
GATED. **Caveat I will not paper over:** the deep multi-day panel is FULLY survivorship-biased (0/400 sampled
syms delisted-in-store), so any long-horizon result needs the delisting-haircut discipline from the memory.
Worth running because it attacks the actual cause of death (turnover) on a different axis than #1.

### #3 — COST-CONDITIONED signal construction (use the cost model as a FILTER, not a predictor of return)
A genuinely new framing the now-accurate cost unlocks: instead of hunting a signal then checking if it clears
cost, CONSTRUCT the cross-section to live where cost is low. E.g. a signal that is only TRADED on names whose
predicted half-spread is in the bottom decile that day — turning the cost model into a tradeability filter.
This is distinct from #1 (re-run) — it's a new selection axis. Lower-confidence (it narrows the universe, may
just re-discover the liquid head), but it's the one direction that is ONLY possible now.

### Lower-priority / I'd defer
- Cross-asset / regime-conditioning: broad, unfocused, and our regime data is thin — high churn risk, the
  "broad creative" trap. Defer unless #1–#3 all null.
- More feature invention on the SAME bar substrate: 3 magnitude-feature nulls say the bar geometry is mined
  out for $. Stop.

## My honest bottom line
The most promising un-nulled direction is **#1 — re-run the highest-gross nulls under correct (liquid-head)
cost**, because (a) it's cheap, (b) it directly tests whether our entire null streak was a cost-measurement
artifact, and (c) the answer is decisive either way. **#2 (weekly, cost-amortized) is the best NEW hypothesis**
and is already pre-registered. I would sequence: #1 first (a few eval-hours, re-uses everything), then #2 if
#1 is null. Both rank above building Stage 2 (which has no real-capital consumer today). I'm ready to
pre-register #1 as a concrete re-grade protocol on your word.
