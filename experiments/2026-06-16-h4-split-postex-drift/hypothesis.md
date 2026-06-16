# H4 — Split POST-event drift (reverse=distress, forward=attention), LIQUID-tradeability gate PRIMARY

**Registered:** 2026-06-16 (before run). Data: `corporate_actions_pit` — 483 reverse_splits + 55
forward_splits (24-mo); **244 splits in the 6-month bars window** (not underpowered, unlike the old ~61).
ex_date look-ahead-safe; NO declaration date → POST-event drift only (realized, not announcement).

## Hypothesis

- **Reverse splits** (distress/delisting-pressure signal): negative post-ex drift (continuation of decline).
- **Forward splits** (retail-attention signal): positive post-ex drift.
And — the load-bearing claim, given the cycle's meta-pattern — any such drift is present in the LIQUID
tertile net of cost, not just the illiquid tail.

## The meta-pattern (same as H5)

Every canary-clearing signal this cycle (vwap_dev H1, 8-K drift H10) was illiquid-concentrated and DEAD in
the liquid tier. The liquid-tradeability gate is the PRIMARY test here, not an afterthought. Caveat specific
to splits: reverse splits are BY NATURE distressed/low-priced names — they may be intrinsically illiquid, so
a "liquid reverse-split" cohort could be near-empty. Report the liquid-tertile event count honestly; if it's
too thin to test, that itself is the finding (reverse-split drift is untradeable-by-construction in liquid
names).

## Test design

1. Event = ex_date of a split (reverse / forward separately). Entry = D+1 OPEN after ex_date (tradeable).
   Forward returns {1,3,5,10,20} trading days (splits may drift longer than dividends). UTC-correct time
   handling (13:30 UTC = 09:30 ET; do not reintroduce the off-by-240 bug). Per-date cross-section vs
   same-date controls; per-symbol-demean; 10-seed canary; day-clustered t.
2. Reverse and forward splits tested SEPARATELY (opposite predicted signs; pooling cancels).
3. **LIQUID-tertile gate PRIMARY** — report the liquid-tertile demeaned drift + the liquid event count. KEEP
   needs liquid-tertile separation beyond canary (sign-correct) with enough events to be non-trivial.
4. Sample-size honesty: 244 in-window splits split into reverse/forward × liquid/illiquid will be THIN per
   cell. This is likely UNDERPOWERED for a strong statistical claim — pre-committed: if a cell has <20 liquid
   events, report it as "directionally suggestive, needs more history (H8 deep-split backfill)", NOT a KEEP
   or a confident KILL.

## Prior

Reverse-split underperformance (Desai-Jain) and forward-split announcement drift / retail attention are
documented. Structurally orthogonal to intraday price. BUT reverse splits are distress-concentrated (likely
illiquid) and forward splits are rare (55 total) — both sample-thin.

## Expected / confidence

- Confidence a LIQUID split cohort shows sign-correct drift beyond canary with adequate N: **~15%** — lower
  than H5, because the liquid×split×direction cells are likely too thin, and reverse splits are intrinsically
  illiquid. The most likely honest outcome is "underpowered — needs the delisted/deep backfill (H8)."
- KEEP: a liquid, sign-correct, canary-clearing cell with N≥20 events. AMBIGUOUS / "needs history": thin N.
  KILL: liquid cells sign-wrong or no separation with adequate N.

## Ordering

Dispatch alongside H5 (dividends). Splits are the higher-risk / lower-power of the two event families; H5 is
the better-powered dividend test. Both under the identical liquid-tradeability gate — the cycle's decisive
question is whether ANY event family produces a LIQUID-tradeable edge.
