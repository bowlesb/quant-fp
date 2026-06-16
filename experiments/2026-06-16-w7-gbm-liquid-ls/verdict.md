# W7 — GBM non-linear LIQUID L/S: **KILL** (tradeable edge) + valuable byproducts

(Written by the Director from the explorer's report — the run completed but did not persist verdict.md.)

## Verdict: KILL the tradeable L/S edge

- **Walk-forward OOS liquid-500 L/S net@measured-cost** (bounce-immune entry + full-label-horizon embargo):
  H5 = **−14 bps/reb**, 95% CI [−204, +167] (no edge); H10 = +465 bps/reb, CI [+178, +740] — the ONLY positive
  cell, but it's an artifact (below). Net@2× within ~30 bps → cost is not the story.
- **The SHUFFLED-LABEL CANARY is NOT clean** where it matters: liquid500-H10 canary = **−148 bps, CI
  [−280, −27]** (excludes zero; required ≈0). A non-zero canary ⇒ the pooled bootstrap carries small-sample
  bias on 126 days ⇒ the H10 CI is untrustworthy. (megacap-H5 canary +105 bps is also non-zero.)
- **Vanishes in the cleaner megacap-100:** H10 "edge" → +54 bps, CI [−369, +522], its two subperiods flip
  sign (+621 → −512, frac-positive 0.50). A real cross-sectional effect is STRONGEST in megacaps; this isn't
  → broad-universe overfit.
- **Leakage caught (the discipline working):** pre-embargo liquid500-H10 read +884 bps; an early N=4 grid run
  read +1534 bps with a +833 bps megacap canary. The train/predict label-overlap embargo cut it ~50–60%.
  +465 bps/10d ≈ 11%/month is economically implausible = single-regime artifact.
- 126 days is too thin for an ML L/S to clear the bar. Re-run unchanged at ≥250 days (pairs with the
  ≥18-month bar-depth ask).

## Valuable byproducts (kept regardless of the KILL)

**FEATURE IMPORTANCE (gain, stable across walk-forward folds):**
1. **`rvol_10d`** (≈2× the next) — realized-vol state is the dominant cross-sectional predictor.
2. `rvol_21d`
3. `mom_skip1_21d` — medium-horizon skip-1 momentum
4. `ret_21d`
5. `ret_63d`
→ Realized-vol state dominates, then medium-horizon (21–63d) momentum; 1–3d reversal / overnight contribute
little. This points wave-2 ML + linear work at **vol-state-conditioned** signals (and corroborates that the
low-vol / beta-anomaly family — W11, BAB — is where the signal concentrates).

**METHODOLOGY (→ RESEARCH_PITFALLS #9):** (a) a full-LABEL-HORIZON EMBARGO between train and predict is
mandatory for any forward-return ML (without it, overlapping labels leak ~50–60% of the apparent edge); (b)
the label-shuffle canary alone does NOT catch entry-price bid-ask-bounce look-ahead — pair it with a
bounce-immune entry (enter at close[t+1], features as-of close[t]); (c) a non-zero shuffled-label canary is a
small-sample-bias / leakage flag — the bootstrap CI is untrustworthy when it fires.

## Disposition
KILL the L/S as tradeable (overfit on 126d, canary not clean, vanishes in megacaps). RE-RUN at ≥250 days.
The feature-importance + methodology findings are the real deliverables and feed wave 2.
