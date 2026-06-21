# Results — look-ahead-per-minute battery sweep

- **Code SHA**: `320563b` (origin/main, battery `#319`). No fingerprint/feature/registry change.
- **Panel**: cadence=intraday, 2026-05-29..06-18 (14 dates), universe_top=200, **3,085,192 rows × 15
  feat × 200 sym**. 3,080,089 gradable rows per strategy.
- **Wall-time**: panel_load=**24.1s**, eval=**1219.6s**, **total=1243.7s (~20.7 min)**, **23.5s/strategy**
  over the 52-strategy sweep (cost driver = the 8 RIDGE/GBM walk-forward fits over ~3M rows × 5 folds).
- Full cell table + raw leaderboard: `out/report.md`; machine-readable `out/report.json`.

## Verdict: the DIRECTIONAL look-ahead-per-minute class is a CLEAN NULL (0/32). The 10 "PASS" cells are all FWD_MAX_RUNUP and are a vol-circularity + grading artifact, NOT tradeable edge.

The leaderboard the harness prints is dominated by `fwd_max_runup` strategies — but every one is a
non-result for two compounding reasons (both flagged pre-run in `README.md`):

1. **Grading inflation.** `FWD_MAX_RUNUP` is a positive-only magnitude (forward max-high / entry − 1,
   always ≥ 0). Booking a dollar-neutral long-minus-short basket against a non-negative target
   manufactures a positive "net/period" and an absurd "breakeven" (e.g. 1760bps, 3054bps) — these $
   columns are NOT money. For run-up the only honest signal is IC / edge_vs_shuffle.
2. **Vol circularity.** The run-up "winners" are realized vol + spread + last-minute run-up — features
   mechanically correlated with the SIZE of the forward high-low excursion. High-vol / wide-spread names
   have larger forward run-ups *by construction*; this is "vol predicts vol", not alpha.

The decisive evidence is the **same-feature direction-vs-magnitude split** (point-in-time, identical
panel, identical folds):

| feature | up_move_start (DIRECTION) IC | runup (MAGNITUDE) IC |
|---|---|---|
| realized_vol_30m | **−0.041** (NW t −1.3) | **+0.335** (NW t +7.8) |
| realized_vol_5m  | **−0.025** (NW t −0.9) | **+0.309** (NW t +7.6) |
| spread_bps_15m   | **−0.029** (NW t −1.5) | **+0.236** (NW t +7.2) |
| max_runup_1m     | **−0.005** (NW t −0.3) | **+0.154** (NW t +4.2) |

A feature that scores IC≈0/negative on the directional triple-barrier but IC=+0.2..+0.3 on the forward
magnitude is, definitionally, a **forward-volatility predictor with no directional content**. This
reproduces the edge hunt's standing meta-conclusion — **"intensity/magnitude yes, direction no"** — for
a **10th** time, now in the per-minute look-ahead framing the old archetype grid could not express. (It
is consistent with the shipped vol/burst features + the 2026-06-19 vol-burst finding: burst/run-up is
predictable, but direction-symmetric.)

### The directional class (where the L/S hit-spread IS meaningful) — 0/32 PASS

| top directional cell | edge_vs_shuffle | NW t | IC | verdict |
|---|---|---|---|---|
| probe_ret_15m_up_h15_b50 (continuation) | +0.0312 | 1.38 | +0.0292 | FAIL (t<2) |
| probe_quote_imbalance_15m_up_h15_b50    | +0.0091 | 1.93 | +0.0177 | FAIL (t<2) |
| composite_up_h5_b50                     | +0.0159 | 0.57 | +0.0098 | FAIL |
| gbm_up_h15_b50 (non-linear combiner)    | −0.0229 | −1.59| −0.0186 | FAIL (NEG edge) |
| ridge_up_h15_b50                        | −0.0047 | −0.26| −0.0043 | FAIL |

- The best directional cell, 15-min trailing-return **continuation** anticipating a +50bps-before-−50bps
  up-move, reaches edge_vs_shuffle +0.031 but only NW t=1.38 — below the conservative |t|≥2 bar.
- `quote_imbalance_15m` (more bid-side depth → up-move) is the most t-significant directional probe at
  NW t=1.93 — tantalizingly close but still a FAIL, and it does not survive a barrier/horizon sweep.
- The **RIDGE and GBM combiners produce NEGATIVE edge_vs_shuffle on direction** — they fit noise and do
  not even beat their own label-shuffle. No evidence that a non-linear combination of these
  microstructure features anticipates the directional up-move.

## Honest bottom line

No look-ahead-per-minute strategy clears the conservative bar (net-positive AND beats shuffle AND
|NW t|≥2) as a *directional, tradeable* signal. The per-minute look-ahead **direction** class joins the
settled nulls (cross-sectional direction / order-flow / overnight / weekly-reversal / portfolio-combo).
The only thing that "survives" is forward-**magnitude** prediction by vol/spread features — which is
(a) mechanically circular and (b) graded through an inflated $ path, i.e. not a tradeable edge. This is
itself valuable closure: the new label family is net-new, and a disciplined sweep of it is a clean null,
NOT a candidate. The lone-surviving-family-for-a-mechanical-reason pattern is exactly what the harness's
"a lone surviving cell warrants suspicion" guard is for.

## Battery friction (feedback to improve the harness)

1. **Look-ahead-label grading books $ economics against the LABEL, not a return.** For `FWD_MAX_RUNUP`
   (non-negative) this yields a *positive-by-construction* gross, an inflated `sharpe_net`/`breakeven`,
   and a `PASS` verdict that is purely an artifact. The harness presents these run-up rows on the SAME
   leaderboard as directional rows, so the top-10 looks like a strong result when it is a non-result.
   **Suggested fix**: for non-signed labels, either (a) suppress the $/Sharpe/breakeven columns and grade
   on IC/edge_vs_shuffle + a rank-AUC only, or (b) demean the label per timestamp before the L/S P&L so a
   non-negative target can't manufacture positive gross. At minimum, tag run-up rows in the report so they
   are not ranked head-to-head with directional $-meaningful rows.
2. **A built-in "magnitude-vs-direction" guard would have caught this automatically.** Since vol/spread
   predicting forward magnitude is the recurring false-positive, a standard companion check — does the
   feature's edge survive on the SIGNED/excess-return label too? — would auto-flag the circularity. The
   split table above had to be assembled by hand from `report.json`.
3. **Intraday panel size**: at the native 1-min cadence the panel is ~220k rows/date (3.08M for 14
   dates); the daily-cache fast path that daily cadence enjoys has no intraday analogue, so every run
   re-globs + re-joins the store (24s here, linear in dates). A cached intraday panel parquet (keyed by
   group-set + window + universe) would make re-sweeps near-instant. The `USE_REALIZED_COST=1` tape path
   was prohibitively slow over 14 dates (minutes/date) and had to be disabled — the store quote_spread
   column was the tractable realistic cost.
