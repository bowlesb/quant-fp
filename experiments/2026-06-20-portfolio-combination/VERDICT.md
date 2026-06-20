# Portfolio-combination — VERDICT: NULL, the streams are CORRELATED (no diversification to harvest)

**Date:** 2026-06-20  **Code SHA:** `1f1f0cf` (branch `modeller/portfolio-combo-prereg`, off origin/main
`7b8a561`).  **Disposition:** NULL → inclusion-liberal (legs retained); routes to deployment-hardening (c).

## Provenance (the Lead's ask — exact data state / features / labels / window)
- **Data state:** read-only `fp_store_real` raw bars (2018-2025) + the merged `quantlib.data.realized_cost`
  Stage-1 per-name half-spread (quote tape, present on the recent overlap; conservative 5bps bar proxy on
  deep dates where the tape is absent). swing_dc kernel `quant_tick.swing_dc_fold` on `origin/main`.
- **Streams / features:**
  - **S-WEEKLY** = decile L/S on `−rev_1w` (trailing 5-day return), the #287 weekly panel: 397 weeks
    2018-2025, point-in-time top-1000 ADV/week, tradeable Monday ≥09:35 ET entry → next-Friday exit, $1 floor.
  - **S-INTRADAY** = decile L/S on the L2+L3 composite, weekly-rebalanced (swing_dc on Fridays): L2 =
    `dc_resp_chunk_slope` (swing_dc magnitude, pure price-path, kernel with trades/spread=0); L3 = PC1 of the
    bar-derived {vol20, range20, ret5d/10d/20d} block (parameter-free reduction). 397 weeks, top-500 ADV/week,
    tradeable next-day ≥09:35 ET entry → +5-day exit, $1 floor. 198,500 obs.
- **Label:** forward 1-week tradeable return per stream (no close-to-close look-ahead; entry ≥09:35 ET).
- **Cost:** Stage-1 measured per-name realized half-spread (round-trip = 2× per leg), NOT the 3bps stub;
  bar-proxy 5bps where the quote tape is absent (deep dates). Charged in EACH stream before combination.
- **Window:** common weekly span = the full 397 weeks (2018-2025). Discovery = first 198 weeks, Replication =
  last 199 weeks (disjoint). Combination methods: M1 equal-risk-weight (vol-scale each stream, mean), M2
  single walk-forward ridge (fit on discovery, applied to replication). Both baselines: predict-zero (a
  no-trade stream = 0) and shuffle (the per-stream NW-t vs its own noise is the implicit shuffle here; the
  combined stream is judged vs each single stream).

## Result: NULL on the full pass bar — and the WHICH is decisive
| (HONEST-cost, haircut ON) | S-WEEKLY | S-INTRADAY | M1 combined | cross-stream corr |
|---|---|---|---|---|
| FULL (397 wk) | Sharpe +0.27, NW-t +0.73 | +0.04 / +0.12 | +0.16 / +0.45 | **+0.771** |
| DISCOVERY (198 wk) | +0.30 / +0.59 | +0.25 / +0.48 | +0.29 / +0.56 | **+0.861** |
| REPLICATION (199 wk) | +0.23 / +0.45 | −0.24 / −0.48 | −0.01 / −0.01 | +0.549 |

M1 PASS-legs (honest-cost): combined-t≥2 = **False** · improves-on-better-single = **False** · low-corr =
**False** · replicates-sign = **False** → **NULL.** (M2 fit-on-disc→repl: Sharpe −0.01, identical conclusion.)

## WHICH null (the strategically important part)
**The streams are HIGHLY CORRELATED (+0.771 full, +0.861 discovery) — the diversification mechanism is
ABSENT.** Cross-stream combination only reduces risk when the streams are weakly correlated; here they move
together, so the M1 combined Sharpe/NW-t is LOWER than the better single stream (S-WEEKLY alone), never
higher. This is outcome-branch §8(i) of the pre-reg: **the real-but-weak signals are not diversifying — they
share structure (the baseline already prices it), so there is no portfolio benefit to harvest.**

This is the decisive answer to the meta-synthesis asymmetry question: combining our real-but-weak signals
does NOT create a tradeable edge, because they are correlated, not independent. **The edge really IS the
existing trusted-baseline portfolio**, and the right next move is **deployment-hardening (move c)** — not
more single-signal hunting and not more combination.

## Both-ways survivorship (the honest-cost gate did its job)
- Haircut ON (the PASS CLAIM): NULL (above).
- Haircut OFF: also NULL (M1 full Sharpe +0.26 NW-t +0.71; replication +0.14 / +0.28). So the null is NOT a
  survivorship artifact — it fails even with the weekly leg's survivorship dollars credited in full. The
  −13bps/week haircut shifts S-WEEKLY's NW-t from +1.21 (off) to +0.73 (on) at the full window — material,
  but the combination is null either way because of the correlation, not the haircut.

## Disposition (Ben's principle) + routing
NULL = the combination doesn't create a tradeable edge yet on these signals — NOT "drop the signals."
`rev_1w`, swing_dc magnitude, and the path/vol features stay INCLUDED/retained (inclusion decoupled from
$-value). The verdict is *what-to-trade*, not *what-to-store*. **Routing: → deployment-hardening (c)** — the
trusted-baseline model is the asset (net-positive under realized cost, PR #271); combining the weak signals
adds nothing because they are correlated. The portfolio pivot is answered: there is no free diversification
here; harden and deploy what we have. (Delisting-inclusive data (a) remains the slow track for making the
weekly-reversal IC tradeable on its own.)
