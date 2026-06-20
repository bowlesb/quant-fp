# swing_dc $-EVALUATION — VERDICT (the #242 deploy gate)

**Date:** 2026-06-20 · Substrate: swing_dc (74-feature Olsen multi-scale Directional-Change ladder + Fib,
scales {0.5,1,2,4}×σ30) computed from raw bars + the tick-enriched minute_agg, on the 40 backfill dates
(2026-04-23→2026-06-18), top-200 liquid, entry ≥09:40 ET, forward 30m CROSS-SECTIONAL EXCESS-return label.
Panel: ~7,150 (name, day) rows. READ-ONLY. Code on this branch.

## TL;DR — NULL for direction. No scale, no Fib/setup feature carries FDR-surviving net-new signal; the GBM doesn't beat its shuffle. Recommend SHELVE / defer the deploy.

## ⚠️ FIRST — a real defect found: swing_dc's Rust kernel is NOT on main
#242 merged the swing_dc PYTHON group + registry entry + InputSpec, but **`rust/src/lib.rs` on main has
only `swing_fold` (v1), NOT `swing_dc_fold`** — the kernel the group calls. So swing_dc **cannot compute on
main / the standard fp-dev image** (it raises `AttributeError: swing_dc_fold`). The kernel exists only in
FeatureDev's `.worktrees/swing-dc-decomp` + the `fp-dev-swingdc:latest` image. This eval ran via that image
(swing_dc computes there — 74 feats, 99.5% non-null). **A deploy from main as-is would fail; the kernel
commit must land first.** Flagged to the Lead.

## Per-feature leaderboard — nothing survives
Best |NW-t| across all 70 scorable features = **2.85** (`dc_xscale_pivot_coincidence`, IC −0.039); the rest
sit |NW-t| < 2.6 with |IC| ≤ 0.06. **BY-FDR (q=0.10): 0 of 70 survive.** The per-scale breakdown (features
are scale-tagged _s05/_s1/_s2/_s4) shows NO scale's chunk/Fib/leg structure carries FDR-surviving directional
signal — exactly the design's pre-committed prior (it asserted no winning scale; the data agrees).

## The headline Fib/setup surface — null
The `dc_fib_*` / setup features (the "golden-zone hold → likely-up chunk" thesis): best is `dc_fib_dist_s05`
at **NW-t 1.64** — not even nominally significant, far from FDR. `dc_fib_golden`, `dc_fib_setup`-class all
NW-t < 1.7. The Fibonacci predictive surface does NOT carry net-new directional tail signal at 30m.

## Own-vol/size control + the GBM (the multivariate check)
- A few `dc_persistence_*` features show collapse > 2 (partial-IC exceeds raw) — but at raw NW-t ~2.2 over
  40 days that is the near-zero-denominator collapse artifact (same trap caught in the news hunt), not a
  surviving signal; none survive FDR.
- **GBM walk-forward (all 74 feats, purged split):** test IC +0.073 BUT **shuffle-z = 1.03** — the GBM's OOS
  IC is INDISTINGUISHABLE from a model trained on SHUFFLED labels (the +0.073 is overfitting noise, not
  signal). The decile L/S net@5bps shows mean +306 / median +60 bps but over only **6 test days** with a
  **50% win rate** — far too few, tail-driven, and consistent with the z~1 null. The multivariate model does
  not extract a real edge the univariate ICs missed.

## Disposition — NULL, shelve/defer the deploy
No FDR-surviving univariate feature + a GBM that doesn't beat shuffle → swing_dc does NOT carry net-new
tradeable directional signal at the 30m horizon. **Recommend the Lead SHELVE the controlled deploy** (or
defer pending more backfill data / a different label horizon). NO promotion. The fingerprint-affecting 74-
feature group would add cost (fp 728→802, compute) for no demonstrated edge — not worth deploying on this
evidence.

## Caveats (honest scope)
- SHORT panel: only ~40 backfill dates exist in the trusted store → the walk-forward test split is ~6 days,
  too thin for a strong multivariate claim. A deeper swing_dc backfill would let the GBM verdict be
  conclusive rather than indicative; the UNIVARIATE leaderboard + FDR (40 days each) is the firmer result.
- HORIZON: tested 30m intraday DIRECTION (the design's intraday-structure thesis). A different horizon
  (EOD / multi-day) or a MAGNITUDE-feature framing (like the news H1 reframe — swing_dc as a vol/structure
  FEATURE for a model rather than a standalone directional signal) was not tested here and could differ —
  but the deploy gate as posed (does swing_dc carry tradeable directional edge) is answered: not on this
  evidence.
- The Rust-kernel-not-on-main defect (above) is the prerequisite to ANY deploy regardless of this verdict.

## Method / infra notes
- swing_dc computed via `REGISTRY.get_group("swing_dc").compute(ctx)` on the tick-enriched minute_agg
  (`load_raw_tick_enriched_minute_agg` — n_trades/spread the group needs), per date, accumulated.
- Per-feature daily rank-IC + NW-t + own-vol/size partial-IC + collapse; BY-FDR(q=0.10) across the 70.
  GBM (LightGBM) walk-forward purged split + 50-iter label-shuffle null. Median-anchored.
- Ran in one bounded named `--rm` container on the `fp-dev-swingdc` image (the only image with the kernel).
  NOTE: the standard harness `build_intraday_panel` OOM'd at multi-group scope under load — this lean
  per-date pipeline is the bounded substitute; the verdict (univariate FDR + GBM-vs-shuffle) is the same
  question the harness answers.
