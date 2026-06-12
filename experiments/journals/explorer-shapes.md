# Journal — explorer-shapes (strategy shapes beyond cross-sectional L/S)

Append-only. Every idea, proposal, result, dead end, learning — dated. My lens: trading
HYPOTHESES (shapes) beyond the one shape ever tested here (cross-sectional L/S ranking at
30m/overnight). Each shape = mechanism story + required label + cost/turnover story.

---

## 2026-06-12 — First wake. Grounding + 5 shapes ranked by cost-structure advantage.

### The binding cost reality (this is the lens that ranks everything)
Read it straight from the team's own evidence, not assumed:
- Every signal tested so far **dies on turnover**: price-only 30m IC 0.027-0.032 but
  net-NEGATIVE, breakeven ~1.4bps < ~2bps assumed cost (M1 verdict, FINAL).
- Cost-by-liquidity (modeller task #5): only **11/50 liquid equities** have measured
  half-spread under the 1.4bps breakeven - a STEEP cost-vs-tradeable-count curve. Edge,
  if any, lives in the liquid head, not the breadth.
- **Fill asymmetry** (exec/risk, 6/12): the SHORT leg under-fills (wide-spread shorts rest
  unfilled) -> realized book is net-LONG-skewed, not the neutral L/S the battery assumes.
  An unfilled short is a missed hedge = its own cost.

THEREFORE the structurally-advantaged shape classes - my ranking axis - are:
  (A) LOW-TURNOVER: hold longer / trade rarely, so the per-rebalance cost amortizes.
  (B) CONDITIONAL / SPARSE-PARTICIPATION: trade only the timestamps where the signal is
      strong AND the name is cheap (liquid head). Turnover down by construction.
  (C) LONG-BIASED or long-only: don't depend on wide-spread shorts filling.
A shape that is (A)+(B)+(C) at once is the prize. Cross-sectional L/S at 30m is none of them.

### Data state (verified by query, not assumed) - 2026-06-12
- `corporate_actions` LIVE: 7,205 actions / 633 symbols, 2020->2026-07-09 (future-dated).
  7,133 cash_dividends, 42 forward_splits, 19 reverse_splits, 11 stock_dividends. -> Shape 3
  (post-event drift) and any ex-date-anchored label are CHEAP and runnable NOW.
- `news` table EMPTY (0 rows). -> Shape 6 (post-news drift) STILL BLOCKED. Logged, not proposed.
- `trade_agg_1m` only 52 symbols, 2026-06-10->12 (M2 not scaled). -> OFI/intensity shapes are
  thin-coverage; bar-only intensity (volume_z) is the runnable version now.
- `labels` has fwd_30m (4.84M), fwd_60m (4.42M), overnight (428K). No open-anchored or
  event-anchored label exists yet -> new shapes need new labels (coordinate via Lead).
- NO daily-OHLC helper table exists. This is THE efficiency blocker the prior session
  flagged: the bar-heavy shapes (open-gap, opening-range) re-scan all 693 bars_1m chunks
  per experiment because `(ts AT TIME ZONE 'ET')::time IN (...)` is non-indexable.

### THE UNBLOCKER I'm proposing first (precedes the shapes): a daily session-price helper.
Prior session DEFERRED Shapes 1+2 purely on bar-scan cost, and named the fix: a small
`(symbol, date) -> {open_0930, p_1000, close_1600}` daily-price table, built ONCE. This is
not itself a shape - it's the cheap derived artifact that makes the open-anchored shape
CLASS (1, 2, and the gap-conditioning in others) cost minutes instead of hours. I spec it
as proposal 000 so the Lead can sequence it (it's a one-time read-only materialization into
the sandbox; coordinate with prod for the write target). Every open-anchored shape below
assumes it.

### The 5 shapes, ranked by cost-structure advantage (best first):

1. **CONDITIONAL PARTICIPATION on the existing ret_5m signal** (proposal 001) - the single
   most cost-advantaged idea. We ALREADY have a 30m signal with real raw IC (~0.03) that
   dies ONLY on turnover. Don't trade every timestamp - trade ONLY the top-conviction,
   liquid-head timestamps (|prediction| in top decile AND name in the <1.4bps tier). This
   is (A)+(B)+(C): turnover collapses, cost-per-trade is the cheap tier, and we can run it
   long-biased. Reuses EXISTING predictions+labels - no new label, no new data. If the
   net-of-cost curve ever crosses positive, it crosses HERE first. Highest EV, cheapest.

2. **SHAPE 1 - OPEN-GAP FADE/FOLLOW, conditional** (proposal 002) - overnight gap continues
   or reverts, conditioned on gap size x overnight volume. Label: open(09:30)->close(16:00),
   cross-sectionally demeaned (reuse cross_sectional_excess). LOW-TURNOVER: one decision per
   name per day at the open, held to close = far less churn than 30m rebalancing. Cheap once
   the daily-price helper lands. Classic, elegant, single new label.

3. **SHAPE 3 - POST-EX-DIVIDEND DRIFT/REVERSAL** (proposal 003) - event-anchored, LIVE data
   (corporate_actions). Label: fwd 1-5 day return anchored on ex_date. SPARSE by construction
   (only fires on ~7,000 ex-dates across the history) -> structurally low-turnover, and the
   trade is event-triggered not continuous. Distinct from the ex-div label-HYGIENE work
   (that REMOVES the artifact; this TRADES the post-event drift). Uses live data, no bar
   re-scan. A genuinely different shape axis (event-reaction).

4. **SHAPE 2 - OPENING-RANGE BREAKOUT, liquid-head only** (proposal 004) - break the
   09:30-10:00 high/low -> continue intraday. Label: 10:00->close. Single-name TIME-SERIES
   signal (not cross-sectional) - a different shape. Restrict to the liquid tier so cost is
   the cheap bucket; breakout is naturally sparse (only fires on names that actually break)
   -> low participation. Cheap once daily-price helper + a first-30-min-range feature land.

5. **SHAPE 4 - VOLUME-SHOCK OVERNIGHT REVERSAL, bar-only** (proposal 005) - a volume shock
   (today vol >> trailing ADV) predicts next-session reversal. Label: EXISTING overnight (no
   new label!). Feature: volume_z from bars (cheap; richer OFI version is M2-gated). LOW-
   TURNOVER (overnight hold) and SPARSE (only fires on shock days). Cheapest to test because
   it reuses the overnight label and only needs a volume_z gate. Ranked last only because the
   overnight label is survivorship-negative across everything tested - but as a CONDITIONAL
   sparse overlay (trade only shock nights, not the whole book) it's a fair, cheap re-test of
   whether sparsity rescues an otherwise-dead label.

DELIBERATELY NOT PROPOSED: Shape 6 (post-news) - news table empty, blocked. Shape 5 (sector-
relative) - sector_map not confirmed landed; will pick up when it does. Shape 7 (horizon
ensemble) - already tested -> DISCARDED (30m signal has zero overnight IC).

Next: wrote proposals 000-005, messaged the Lead, handed off label-computation needs.

## 2026-06-12 (cont.) — Lead dispositions in. Delivered helper-000 SQL + 001/003/005 scripts.

Lead dispositioned all 6 proposals (in the files). Build order he set: 001 (priority 1), 003 (2),
005 (3) runnable NOW; 002+004 behind helper-000; helper-000 becomes a REGISTERED catalog table
(research.common_daily_session_price) since explorers are DB-read-only — I deliver the SQL, he runs it.

DELIVERED (committed c97df44, ruff-clean + black + py_compile-OK):
- experiments/builders/common_daily_session_price.sql — catalog builder copying the
  common_spreads_at_cadence INSERT pattern. Materialized snapshot (NOT a live view — the whole point
  is to avoid the per-experiment 693-chunk bars_1m scan). ET/DST via ::time-in-ET; early-close
  16:00-absent -> NULL (honest, not stale-filled). Lead to EXPLAIN + run quiet + register.
- experiments/shape_conditional_participation.py (001) — the Lead handed me a SHARP tension I baked
  into the pre-registration AND the design: task #5 found the signal is ~0 on the liquid-50 tier
  (IC -0.0035 vs +0.023 full) and explorer-data found ret_5m is a REVERSAL concentrated in ILLIQUID
  names. So the cheap-tier gate may remove the SIGNAL, not just the cost. My script therefore
  SEPARATES the gates: sweeps the conviction gate on BOTH full_panel (signal lives) and liquid50
  (cheap), 6 conviction fracs x 2 modes (L/S + long-only) x 2 tiers, reporting the
  participation-vs-net-Sharpe FRONTIER with shuffle-canary + survivorship-neutral on every cell. The
  CONVICTION-gate axis is the genuinely new knowledge regardless of how the tier tension resolves.
- experiments/shape_post_exdiv_drift.py (003) — event-anchored, in-memory sparse label (no panel
  rebuild), cohort-demeaned forward N-day from ex_date close. Added a PLACEBO-DATE canary (anchor the
  same window on a random trading day) so the effect must be ex-date-SPECIFIC. Reports liquid-tier
  event count (the high-yield tail is the wide-spread tail — honest). Must beat canary AND Family-C
  NO-edge precedent.
- experiments/shape_volume_shock_overnight.py (005) — vol_z_30 shock gate on the EXISTING overnight
  label (cheapest test). Honest ~20% prior; survivorship-demean is the make-or-break gate; a clean
  death CLOSES the overnight label as a shape.

NOTE for the Lead when he runs them: 001/005 import the cost_liquidity_tier.py / battery.py helpers
(collect_oos, load_panel, per_symbol_demean, shuffle_within_groups) — same harness, run from /app via
the experimenter container. 003 is standalone (only needs corporate_actions + bars_1m, read-only).
NEXT (when helper-000 lands): build 002 (gap fade/follow) + 004 (ORB) together on the shared open-anchored machinery.

## 2026-06-12 (cont.) — helper-000 LANDED; built + SMOKE-RAN 002 + 004. Two real results.

Shared research layer went LIVE (read-only lifted for research.); Lead validated/EXPLAIN'd helper-000
and ran it: research.common_daily_session_price = 741,174 rows / 1,213 symbols / 634 dates / 9,055
NULL closes (early-close days honestly NULL'd). Wider than v1.1.1 (it's all backfill bars, not panel
members) — fine for open-anchored shapes. Built 002 + 004 against it (no bars_1m re-scan — the whole
point of the helper). Both ruff+black clean, py_compile-OK. SMOKE-RAN both:

002 GAP FADE/FOLLOW — a REAL CONDITIONAL SIGN-FLIP (the hypothesized structure):
  aggregate gap->open_to_close IC = -0.0273 (NW t -3.54) — but this HIDES a regime flip:
    low_vol gaps: IC -0.0866, FADE Sharpe@2bps +4.12   (light-volume gaps REVERT — noise gaps)
    high_vol gaps: IC +0.0195, FOLLOW Sharpe@2bps +1.24 (heavy-volume gaps CONTINUE — information)
  The two regimes CANCEL in aggregate, which is exactly why nobody saw it as a cross-sectional signal.
  This CLEARS my pre-registered bar (sign-coherent, |IC|>0.01, t>2 in BOTH regimes). CANDIDATE.
  BUT — HONEST CAVEAT, NOT a finding yet: these are GROSS/flat-2bps Sharpes with NO gates. The +4.1
  fade Sharpe is SUSPICIOUSLY strong and almost certainly part survivorship + in-sample. Needs the
  full gate stack (shuffle canary, survivorship per-symbol demean, per-name measured cost, walk-forward
  OOS) before any verdict. Handing to the Lead as the strongest candidate to GATE, not to believe.

004 OPENING-RANGE BREAKOUT — clean PREDICTED DEATH (~30% prior):
  break_UP mean ten_to_close +0.00109 (t +11.15, n 50,070) — a REAL but TINY continuation.
  break_DOWN +(-0.00005) t -0.57 — nothing. position_in_range corr +0.0027 — nothing.
  long-only up-break book net Sharpe: -0.064 @1.4bps / -0.175 @2.0 / -0.303 @2.7 — NET-NEGATIVE at
  every realistic cost. The micro-continuation (11bps) is real but smaller than the spread. DEAD as a
  tradeable shape — a clean falsification (valuable: closes ORB). Matches the cost reality exactly:
  another real-but-uneconomic price effect.

NET: 002 is the first conditional shape with sign-coherent structure that SURVIVES a first look — but
unverified. 004 dies cleanly. Both committed with results jsonl. Reporting to the Lead with the gate
gap on 002 explicit (verdicts his).

## 2026-06-12 (cont.) — 002 GATED: low-vol fade SURVIVES canary + survivorship. Strongest candidate.

Proactively wired the shuffle-canary + per-symbol survivorship demean into shape_gap_fade_follow.py
(don't wait for the Lead to gate the candidate — gate it myself, hand him a gated result). Re-ran:

  regime    | real fade@2bps | CANARY fade | SURV-NEUTRAL fade   (the make-or-break columns)
  low_vol   | +4.12          | -0.76       | +3.82   <- SURVIVES BOTH GATES
  high_vol  | follow +1.24   | follow -1.49| follow +0.92  <- follow side also survives both
  all       | -0.08          | -2.79       | -0.11   (aggregate is noise — the regimes cancel)

  canary IC per regime: low_vol -0.001, high_vol +0.002, all +0.0003 — ALL collapse to ~0 (the gap
  effect is NOT a leak). Survivorship demean barely moves the surviving Sharpes (low-vol fade
  4.12->3.82) — so it's TIMING alpha (when a name gaps), NOT survivor-name selection.

This is the strongest candidate the shapes lens has produced: a low-turnover (1 round-trip/day),
CONDITIONAL (fade light-volume gaps / follow heavy-volume gaps) shape that passes canary + survivorship.
It is exactly the cost-advantaged class I ranked #1-style (low-turnover + conditional).

REMAINING HONEST CAVEATS (handed to the Lead — these are why it's a candidate, not a verdict):
 1. IN-SAMPLE sort, not walk-forward OOS. The Lead's harness should re-run with walk-forward folds —
    a stable effect across folds is the real test (in-sample 3.8 Sharpe could shrink OOS).
 2. FLAT 2bps cost. The gap round-trip executes at the OPEN, where spreads are WIDEST — the real cost
    is harsher than the 30m-cadence common_spreads_at_cadence marks (which exclude the auction). The
    +3.8 Sharpe has headroom but the open-spread haircut is the true economic test.
 3. The universe here is all-1213-backfill-symbols, NOT the liquid tier — and the low-vol-gap names
    skew ILLIQUID (light volume = wide spread). So the fade may live exactly where it's expensive to
    trade — the same tension the Lead flagged on 001. The liquid-tier-only re-run is the honest cut.
NEXT: offered the Lead either I add walk-forward+liquid-tier+open-cost myself, or he takes it into his
harness. Committed the gated script + results.

## 2026-06-12 (cont.) — 002 LIQUID-TIER cut: low-vol fade SURVIVES on the liquid head too.

Added TIER=liquid50 env switch (caveat #3 — my lane, reversible). Ran the liquid-50 cut:

  liquid50, low_vol fade: real +3.10 / canary -0.39 / surv-neutral +3.13   <- SURVIVES both, on liquid
  liquid50, high_vol follow: real +1.10 / canary +0.14 / surv-neutral +0.99  <- survives both
  liquid50 AGGREGATE gap IC = -0.0059 (t -0.55) — WEAK (gaps arbitraged out of liquid names, as folklore
    predicts) BUT the CONDITIONAL low-vol-fade IC is still -0.0911 and its fade Sharpe survives.

KEY UPGRADE: the conditional fade is NOT just an illiquid-microcap artifact — it survives canary +
survivorship on the LIQUID-50 head (the tradeable names), where the cost is the cheap tier. That kills
the strongest "it lives where it's expensive" objection (caveat #3). The low-vol-gap fade on liquid
names is the cleanest cost-advantaged candidate the shapes lens has: low-turnover (1 RT/day),
conditional, liquid, survives both gates.

REMAINING (the Lead's harness, true verdict): (1) walk-forward OOS — in-sample 3.1 Sharpe must hold
across folds; (2) OPEN-spread cost — even liquid names have wide OPENING-auction-adjacent spreads, and
common_spreads_at_cadence excludes the auction, so the open-RT cost needs a dedicated measure. If it
survives walk-forward + real open-cost, this is a genuine M3-track edge candidate.
Both cuts (all + liquid50) committed in shape_gap_results.jsonl.

## 2026-06-12 (cont.) — LITERATURE SEARCH (new binding protocol). My charge: strategy classes + NET-of-cost reality.

Ran 4 targeted searches, one per queued shape, focused on the NET-of-cost / liquid-vs-illiquid angle
(my specific charge — a shape that's gross-profitable but dead net-of-cost is the norm). Findings
INFORM pre-registration, never replace gates. Each cite = link + takeaway, translated to our reality.

[002 GAP REVERSAL] Berkman et al. 2012 "Time-Varying Rationality" + Baltussen/Da/Soebhag "End-of-Day
  Reversal" (https://academicweb.nd.edu/~zda/EOD.pdf) + Della Corte/Kosowski "Overnight-Intraday
  Reversal Everywhere".
  MECHANISM (matches my shape exactly): retail-attention price pressure at the OPEN pushes price up ->
  high overnight return -> INTRADAY REVERSAL. Baltussen: end-of-day/overnight price-pressure reversal
  from late-day forced selling, NOT information.
  ★ CRITICAL COST TAKEAWAY (my charge): BOTH papers find the reversal is STRONGER for ILLIQUID/small-cap
  stocks and "does NOT survive reasonable transaction costs for most investors"; "large-cap liquid names
  show dramatically attenuated or negligible reversals after costs." TRANSLATE: this is EXACTLY the open-
  minute cost wall the Lead just measured (6-12bps half at the open vs 2.7 at 10:00). The literature
  PREDICTS my low-vol-fade should be WEAKER on liquid-50 — yet it survived canary+survivorship there.
  So either (a) my flat-2bps cost flattered it and the measured open cost will kill it (the Lead's ~70%
  prior, lit-consistent), or (b) the volume-CONDITIONING isolates a sub-effect the broad reversal lit
  didn't test. The measured-open-cost gate (task #12) is the lit-predicted decider. Pre-registration
  updated: my prior the low-vol-fade survives MEASURED open cost drops from ~50% to ~30% on this lit.

[004 ORB] Concretum/Cretarola 2024 "A Profitable Day Trading Strategy" (ssrn 4729284) + Holmberg et al.
  "Assessing the profitability of intraday ORB" (sciencedirect S1544612312000438).
  TAKEAWAY: the OLDER lit (Holmberg) = basic ORB rules "would NOT be profitable when applied to intra-
  daily datasets" once costs are in — MATCHES my clean death (+11bps gross, net-negative at every cost).
  BUT the 2024 Concretum result (2.4 Sharpe, beta~0) survives net-of-cost ONLY by restricting to "STOCKS
  IN PLAY" (abnormal-volume names) — the SAME sparsity-on-liquid-volatile-names rescue as everything
  here. TRANSLATE: my 004 tested UNCONDITIONAL break direction and died (correctly). The lit says the
  live version is breakout CONDITIONED ON abnormal volume. My 004 had or_vol_z computed but UNUSED in
  the book — a concrete miss. If 004 is ever revisited: gate the up-break book on high or_vol_z (stocks
  in play). Logged as the one revival path; not reopening now (Lead killed it, accepted).

[003 POST-EX-DIV] Frank-Jagannathan 1998 + Elton-Gruber 1970 + decimalization studies (researchgate
  4992558; the 2001-decimalization / 2003-tax-equalization decline literature).
  TAKEAWAY: Frank-Jagannathan = the ex-day return is a MICROSTRUCTURE artifact (bid-ask bounce + tick
  size make the dividend exceed the price drop), NOT alpha. Ex-day abnormal returns "declined SIGNIFI-
  CANTLY after 2001 decimalization and further after 2003 tax equalization." TRANSLATE: in a post-2024
  decimalized, penny-spread market the effect should be NEAR-ZERO and what remains is bid-ask-bounce
  (untradeable). This is fully CONSISTENT with the Lead's verdict (003 NO-EDGE: mean excess ~0, |t|<0.8).
  The lit RETRODICTS the null — good. My ~30% pre-reg prior was if anything too high given the post-
  decimalization decay; lesson logged.

[005 VOLUME-SHOCK] Quantitativo "Volume Shocks and Overnight Returns" (quantitativo.com) + abnormal-
  trading-volume reversal lit (tandfonline 1351847X.2024.2303092; investor-attention reversal S154461..).
  ★ TAKEAWAY THAT CORRECTS MY PRE-REG: the documented volume-shock overnight effect is CONTINUATION, not
  reversal — "stocks with large volume shocks earn significantly higher CLOSE-TO-OPEN returns" (high vol
  -> positive overnight gap), with NO intraday predictability. I pre-registered REVERSAL. And the
  net-of-cost reality is the headline: on Russell 3000 with commissions + 10% ADV limit the Sharpe
  COLLAPSES from 1.5+ to unviable ("a good chunk of the edge gets eaten"); it survives ONLY in a
  concentrated LIQUID-VOLATILE subset (Nasdaq biotech, Sharpe 1.52). TRANSLATE: my 005 tested reversal on
  the overnight L/S label and the Lead verdicted NO-EDGE (survivorship-neg at every sparsity) — consistent
  with "dies on the broad universe." The lit says if anything to test (a) CONTINUATION not reversal and
  (b) a concentrated liquid-volatile subset (we have no sector map yet -> blocked). Logged as the only
  revival path for the volume-shock class; not reopening (Lead killed the reversal version).

OFI CROSS-REFERENCE (Lead's survey): Markwick / Chordia-Subrahmanyam document the identical failure mode
— "looks profitable gross, costs destroy it." My low-turnover/sparse/conditional designs are the
lit-endorsed RESPONSE to exactly this wall. The gap-fade is the test case: does conditioning + low
turnover beat the cost wall where the broad reversal strategies don't?

NET PRE-REGISTRATION UPDATE going into task #12: lit consensus is that overnight/EOD reversal is an
illiquid, cost-fragile, open-spread-killed effect. My gap-fade survived flat-2bps on liquid-50 AGAINST
that prior — which is exactly why the MEASURED-open-cost gate is decisive, not optional. I now put ~30%
(down from 50%) on it surviving measured open cost after walk-forward. Honest going in.

## 2026-06-12 (cont.) — ★★ 002 PASSES THE VERDICT GATE: walk-forward OOS + measured open cost + canary.

Built shape_gap_walkforward.py — the two gates the Lead set as the decider. Per fold: LEARN volz-split +
fade/follow DIRECTION on TRAIN (via gap-excess covariance sign), APPLY to TEST, accumulate OOS returns
(reuses quantlib walk_forward_folds, horizon=390 purges train labels reaching into test). Charges the
Lead's MEASURED open half-spread by entry minute + close exit half-spread. Added a leakage canary
(shuffle excess within date, re-run the FULL pipeline) because a 2.6 net Sharpe demands it.

RESULT (liquid-50, walk-forward OOS):
  low_vol FADE:  gross OOS Sharpe +3.40 (517 OOS dates)
    net @09:30 (RT 15.3bps) +2.62 | @09:33 (10.2) +2.88 | @09:35 (9.4) +2.92 | @09:40 (8.7) +2.96
    CANARY (shuffled excess): -0.72  <- COLLAPSES. No structural leak.
  high_vol FOLLOW: gross OOS Sharpe -0.95 (DIES out-of-sample — not a universal artifact; the edge is
    specifically the LIGHT-volume gap reversion, which is the cleaner mechanism story anyway).

VERDICT-RULE (the Lead's, verbatim): "low-vol-fade Sharpe POSITIVE at measured open cost AFTER
walk-forward -> legitimate M3 candidate, escalate THAT moment." -> CONDITION MET. Positive (+2.6 to +3.0)
at the MEASURED open cost across EVERY entry minute, walk-forward OOS, canary-clean.

This is the FIRST honest edge candidate the whole effort has produced. It clears all 4 M3-style gates:
  [x] within-timestamp structure (conditional gap->O2C IC, regime sign-coherent)
  [x] clean shuffle canary (OOS canary -0.72)
  [x] positive net-of-cost at MEASURED open spread (not flat 2bps) on the liquid tradeable tier
  [x] survives survivorship (per-symbol demean barely moved it; + the OOS direction-learning is itself
      a survivorship-robustness check — direction is learned per-fold, not assumed)

HONEST REMAINING CAVEATS (must travel WITH the escalation — not blockers, but real):
  1. Open-spread cost measured on ~3 days of quote_agg_1m (small sample). Needs more sessions to firm.
  2. Entry-price DECAY not modeled: the cost-sweep varies COST by entry minute but the entry PRICE is
     fixed at the 09:30 open (helper has 09:30 + 10:00 only). Later entry = tighter spread BUT some fade
     already realized — the true entry-minute optimum needs intra-window bars (helper extension). The
     +2.96 @09:40 is OPTIMISTIC on price (assumes full fade still available at 09:40); the +2.62 @09:30
     is the CONSERVATIVE honest number (full spread, full fade) — and it's STILL positive. Lead to judge.
  3. Paper-stage; mechanism = light-volume overnight gaps overshoot and revert (Berkman retail-attention
     price pressure) — lit says this is usually illiquid/cost-killed, yet it survives here on liquid-50
     at measured cost. That's surprising enough to warrant the Lead's independent re-run before promotion.
Reporting to the Lead to ESCALATE per his verdict rule. Wrote the 002 report. Committed.
