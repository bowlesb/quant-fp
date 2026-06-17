# R6 vol term-structure — RESULTS (bars, liquid vs speculative)

Study: `study.py` (~3,841 syms sampled → 1,000 liquid / 838 speculative, 378d). vol_term =
std(1m log-returns over last 10m) / std(over last 60m). Characterization (feature study, not an edge
hunt). Pre-reg decision rule: SHIP only if WELL-SPREAD and PERSISTENT.

## Results
| metric | LIQUID (1000) | SPECULATIVE (838) |
|---|---|---|
| vol_term median | 0.844 | 0.816 |
| p10 / p90 | 0.528 / 1.253 | 0.423 / 1.336 |
| frac expanding (>1) | 30% | 31% |
| **lag-5 autocorr (persistence)** | **+0.554** | **+0.561** |
| fwd \|ret\| 30m rank-IC | −0.0115 (n=300k) | +0.0386 (n=251k) |

## Interpretation
- **WELL-SPREAD.** p90−p10 ≈ 0.73 (liquid) / 0.91 (spec); median ~0.83 (typically short-vol < long-vol,
  i.e. vol mean-reverts down within the session), 30% of minutes expanding. NOT degenerate ~1 — a genuine
  spread of vol-regime states.
- **STRONGLY PERSISTENT.** lag-5 autocorr **+0.55** in BOTH tiers — vol_term is a real, slow-moving
  regime, not minute-noise (far above the 0.05 bar). This is the load-bearing result: a persistent
  regime variable is exactly what a model conditions on.
- **Forward-|ret| sanity — carries information, with a TIER ASYMMETRY.** In the speculative tier an
  expanding-vol regime (high vol_term) PRECEDES bigger 30m moves (IC +0.039); in the liquid tier the
  sign FLIPS slightly negative (−0.012, vol mean-reverts so high short-vol tends to subside). The
  quantity carries information and its sign is tier-dependent — itself a signal a model can exploit
  (vol_term × liquidity_rank). This is a SANITY check, NOT a strategy claim, and is not gated on.

## Verdict (verified myself, not trusting the auto-line)
- **FEATURE: SHIP — `vol_term_structure`.** Clears the bar decisively: REAL (well-spread, n=550k+),
  PARITY-TRUE (a deterministic windowed ratio of realized vols — the existing realized_vol pattern),
  NON-REDUNDANT (the platform has many vol LEVELS but NO term-structure ratio; a tree splits on
  thresholds, not ratios-of-two-columns, so the explicit ratio is additive), NOT-NOISE (autocorr 0.55
  — emphatically a regime, not noise). The tier-asymmetric forward sign confirms it carries information.
- **No standalone STRATEGY claim** (the forward IC is modest and a feature study, not an edge hunt).

## Feature design (parity-true; STATIC windowed, NO FeatureState)
A ReductionGroup-style windowed feature, NO FeatureState needed (a pure deterministic function of the
close buffer, like realized_vol):
  - `vol_term_{SHORT}_{LONG}` = realized_vol_short / realized_vol_long over a couple of horizon pairs
    (e.g. 10/60m and 5/30m), NULL when the long-vol denominator is numerically ~0 (apply the
    DataIntegrity-4 RELATIVE guard from the start: long_vol > eps*… → NULL otherwise, so stream==backfill).
Ratio is scale-free and continuous → no dead-band; the reduce runs over the full buffer, output keys
filter to latest → compute_latest == compute().last by construction.

## Output
batch-1f candidate: `vol_term_structure` (2 horizon-pair features). Worktree → PR off origin/main.
