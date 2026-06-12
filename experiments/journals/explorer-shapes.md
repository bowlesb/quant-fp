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
