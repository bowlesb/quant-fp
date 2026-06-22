# Order-flow / tick-tape G0 screen — RUN INSTRUCTIONS

**Status: ONE-CALL-READY.** The trade+quote tape IS landed (PRE_REGISTRATION.md §4); this runs NOW on the
broad-tape window (2026-03-30 .. 2026-06-18, ~55 trading days). Smoke-verified end-to-end through the real
harness (panel build → ARM A/B/C $-curve → G0b effective-cost model) on 2026-06-22.

## The one call (the moment the Lead greenlights the screen)

```bash
# from the worktree root (or any checkout of this branch). Read-only store; --cpus capped; --rm.
docker run --rm --name edgehunt-orderflow-screen --cpus 6 \
  -e N_DATES=42 -e UNIVERSE_TOP=200 -e N_FOLDS=5 -e OUT_DIR=/out \
  -v "$PWD":/app:ro -v fp_store_real:/store:ro -v /tmp/edgehunt-out:/out -w /app \
  fp-dev:latest \
  python -m experiments.2026-06-22-orderflow-tick-cost.run_screen \
  2>&1 | tee /tmp/edgehunt-out/screen.log
```

Outputs: `/tmp/edgehunt-out/g0_orderflow_panel.parquet` (the assembled panel for re-analysis) + the full
G0a/G0b verdict on stdout. Runtime is bounded (the per-symbol tick read is one parquet per (sym,day); 42×200
≈ 8.4k symbol-days).

## Knobs (env)
- `N_DATES` (42): trailing eval dates from the broad-tape window.
- `UNIVERSE_TOP` (200): top-N by dollar-volume per day (the tradeable universe).
- `N_FOLDS` (5): walk-forward folds.
- `GROUPS`: override the baseline group list (default = trusted dense + ALL order-flow/quote groups, so G2 is
  enforced by construction — the new tick proxies must beat the minute-agg trade-flow + static quote features).
- `MIN_TRAIN_ROWS` (600): harness fold floor.
- `OUT_DIR`: writable output dir (mount it; `/app` is read-only).

## What it does (maps to the pre-reg gate)
- **G0a (alpha):** ARM A (full baseline) vs ARM B (+ 7 throwaway tick signed-flow proxies). Prints the
  Δ-total-$ at the 2%/5%/10% cuts. GO only if Δ is POSITIVE and ROBUST across cuts (per-day t; not a
  lone-outlier-tightest-cut win — the standing anti-fooling tell).
- **G0b (effective-cost model):** walk-forward GBM predicting the realized forward EFFECTIVE half-spread
  (size-weighted |price−mid|/mid from the overlapping trade+quote tapes — the TRUE paid cost) from the same
  trailing tick proxies. Reports OOS R²/rank-IC, MAE vs the flat stub, and the realized effective-vs-flat
  distribution. ARM C books the baseline signal under the realized EFFECTIVE cost to show the cost-accuracy
  effect (the analogue of G0b-quoted's 18% optimism haircut).

## Anti-fooling checks built in
- **G-STALE no-look-ahead:** entry proxies read only prints/quotes `ts < T` (strict, µs); each print's
  Lee-Ready sign is anchored to the asof-BACKWARD NBBO (`ts ≤ print_ts`); the cost label reads `ts ≥ T`;
  per-name staleness emitted; `EXCLUDED_CONDITIONS` drops non-eligible prints.
- **Shuffle baseline** (within-timestamp label shuffle) printed per arm.
- **predict-zero** baseline via the harness (`_baseline_curves`).
- **Lone-outlier tell:** the Δ-curve prints all three conservative cuts so a single-cut win while broader cuts
  decline (the fake's signature, per memory `feedback-g0-first-dollar-screen`) is visible.

## What this is NOT
- NOT a production feature group — no `quantlib/` or `groups/` edit; experiments/ only; fingerprint-neutral.
- NOT a re-run of the settled quote-dynamics ALPHA G0a, bar-derived OFI 0/4, or HF03 spread-capture (KILL).
- The G2-incremental effective-vs-quoted cost comparison (the wire-decision gate) needs the quoted label
  column added alongside the effective one before any harness wire — see the note printed by
  `cost_model_screen` and RESULTS.md.

## After running
Fill in RESULTS.md with the G0a Δ-curve verdict + the G0b R²/rank-IC + effective-vs-quoted distribution, then
report to the Lead. If G0a GO → proceed to G1–G6 + G-STALE on two windows (the §6 backfill enables G5). If
G0b shows effective materially better-predicted than quoted → that is the KEEPER cost upgrade.
