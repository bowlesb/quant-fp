# swing_dc $-curve A/B — VERDICT: NULL (does not move the money curve)

**Date:** 2026-06-20  **Question (the deploy gate):** does ADDING the swing_dc feature group (74 feats) to
the trusted-model inputs IMPROVE the harness $-curve at conservative percentile cuts vs the SAME
trusted-model baseline WITHOUT swing_dc?

**Answer: NO.** Adding swing_dc does not improve the money curve at conservative cuts; it marginally
DEGRADES precision, Sharpe, AUC and rank-IC at the 5% and 10% cuts, with only a noisy 2%-cut total-$ blip
that is contradicted by its own precision (−0.074) and Sharpe (−9.18). swing_dc remains a trustworthy,
replicated MAGNITUDE feature — but on this evidence it does NOT justify a 728→802 fingerprint change +
~74-feature latency cost. **Recommend: do NOT deploy swing_dc to the live fingerprint.**

## Setup (honest, faithful to the harness)
- Substrate: trusted backfill store, 42 well-covered dates 2026-04-15..2026-06-12 (the recent capture-outage
  tail 06-15..06-18 excluded: trusted coverage collapses to <100 syms there). Top-200 liquid/day, entry
  09:40 ET (>=09:35 tradeable, fold warmed), forward-30m cross-sectional EXCESS label, $1 floor.
- Panel: **4,612 rows, 54 entry-timestamps(days), 127 trusted feats vs 201 (127+74 swing_dc)**. IDENTICAL
  row set both arms (only the feature columns differ). swing_dc non-null density at entry = **97–99.9%**
  (`dc_resp_chunk_slope` 99.76%) — NOT a warmup/coverage artifact; swing_dc had full opportunity to help.
- Engine: the EXACT harness path — purged walk-forward GBM (5 folds → 27 OOS test-fold days, 3,433 OOS
  rows), shared `CrossSectionalLS` decide-core scoring, per-name half-spread + slippage + borrow cost,
  threshold curve + shuffle + predict-zero baselines. Both arms used the SAME config/folds.
- This is OOS (walk-forward test folds only), 5 folds. NOTE: the trusted-store universe shifts mid-window
  (April ~1.3k syms → mid-May+ ~6.8k syms), so April dates contribute ~30 rows/day vs ~191 later — the
  dense June dates dominate the later folds. The A/B is unaffected (identical rows both arms).

## The $-curve (OOS, net of cost). Δ = ARM B (+swing_dc) − ARM A (baseline)

| cut | A total $ | B total $ | Δ total $ | Δ $/trade | Δ precision | Δ Sharpe |
|----:|----------:|----------:|----------:|----------:|------------:|---------:|
|  2% | +282,560 | +390,295 | **+107,735** | +998 | **−0.0741** | **−9.18** |
|  5% | +276,680 | +219,076 | **−57,605** | −178 | −0.0216 | −11.22 |
| 10% | +148,400 | +129,605 | **−18,796** | −28 | −0.0104 | −2.96 |

- Headline 10% L/S basket: A = +$158,130 (Sharpe 31.4) vs B = +$139,786 (Sharpe 28.5) → swing_dc DEGRADES.
- AUC: A 0.5347 vs B 0.5329; rank-IC: A +0.0644 vs B +0.0609 — both slightly lower WITH swing_dc.
- Shuffle baselines: all NEGATIVE at every cut both arms (the real curves dominate the shuffle null — the
  baseline model's tail edge is real). predict-zero = $0 both arms.

## Reading
The ONLY positive delta is the **2%-cut total-$** (+$108k on n=108 trades, ~54 name-periods/side) — the
noisiest, lowest-count cut, and it is CONTRADICTED at the same cut by precision (−0.074) and Sharpe (−9.18):
a few large idiosyncratic winners, not a hit-rate improvement. At every broader, better-powered cut (5%,
10%, 20%, 33%, 50%) and on AUC and rank-IC, the baseline-only model is as good or BETTER. The trusted model
already captures the structure-of-the-path edge (the #255 tail-importance result: ~91% of the profitable-
tail gain is already in price-return-shape + volatility groups that ARE in the baseline); swing_dc's 74
columns are largely redundant with that surface for the GBM, and add variance without adding $.

## Caveat (does not change the verdict)
- 30m horizon, single 09:40 entry/day, 5 folds, ~27 OOS test days. A different horizon or multiple
  entries/day could in principle differ, but the consistent degradation across 5 of 6 cuts + AUC + IC is a
  clean null, not a marginal miss. swing_dc's confirmed MAGNITUDE value (the replicated `dc_resp_chunk_slope`
  IC) is real; it just does not translate into incremental tradeable $ on top of the existing trusted model.
