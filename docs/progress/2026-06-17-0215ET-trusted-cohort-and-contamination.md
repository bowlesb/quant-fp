# Trusted-features cohort + the 06-15 contamination finding — 2026-06-17 ~02:15 ET

Standing parity-validation agent. New downstream role (Ben's directive): trust grades now TRIGGER the
modeling pipeline — features that EARN trust (parity across ≥2 clean RTH days) become the SELECTION the
backfill agent backfills (378d) and the MA trains lightGBM on, incrementally as the trusted set grows.

## First real lifecycle run exposed: 2026-06-15 is a CAPTURE-RESTART day (cannot validate anyone)

Ran the contamination-aware `sweep_day` on 06-15 (1000-symbol cap, tick-aware). It filed **383 DIVERGENT
+ 383 defects** — ALL spurious. Diagnosis:
- Cleanliness on 06-15: `internal_gap=73, low_coverage=26, clean=49`. The internal-gap check CORRECTLY
  flagged the liquid full-session names as contaminated (the restart hole). The only "clean" survivors
  were thin names that barely traded (avg **47** backfill minutes vs 226 for contaminated).
- Grading off thin names is degenerate: `ret_2m` "mismatches" were 2924/2982 NULL-backfill (thin-tape
  sparsity); `bb_width_20m` "diffs" were `0.000000 vs 0.000000, rel_err=90559` (near-zero-denominator).
- Even liquid mega-caps on 06-15 RTH only hit ret_2m 97.6% / bb_width_20m 57% → the DAY is contaminated.
- `parity_audit` on dense RTH data = **0 divergences** → never real `compute_latest!=compute` bugs.

## Actions taken
1. **Rolled back** the 383 spurious defects + lifecycle grades. Trust state is honest again:
   `feature_trust_summary` = 622 UNGRADED, 0 defects, 0 VALIDATED, 0 falsely-trusted.
2. **Hardened cleanliness** (PR #72, commit 39f24a3): `thin_session` gate (`MIN_BACKFILL_MINUTES=120`).
   A restart day whose only survivors are thin names now falls below `MIN_CLEAN_SYMBOLS` → NO grade,
   instead of false defects. The internal-gap check already excludes the real restart victims.
3. **Built the trusted-features surface** (commit 0de58eb): `trusted_features` + `feature_trust_summary`
   SQL views (applied to live DB) and `quantlib/features/trusted_list.py` CLI
   (`--json|--names|--summary`). This is the SELECTION the backfill + MA gate on; it GROWS as the sweep
   promotes PENDING→VALIDATED. Coherent with the backfill agent's coverage manifest: the agent joins
   `trusted_features` (what's trusted) against its raw-coverage view (what data exists) → "trusted AND
   not-yet-backfilled" is the work queue.

## First trusted cohort: ZERO (honest) — gated on clean days, not on tooling

- VALIDATED requires **≥2 clean RTH days**. We have **zero usable clean days**: 06-15 is a capture-restart
  (contaminated); 06-16 raw tape still being acquired by `quant-backfill`. So no feature can earn trust
  today — correctly. The pipeline is built and waiting.
- The NEXT cohort to cross: the day-1 passers (the ~126 daily/vwap/direction features that hit ≥0.999 even
  on the thin set) will go PENDING→VALIDATED on the FIRST clean full-RTH day + a second one. They are the
  expected first trusted cohort.

## What unblocks the first trusted cohort
- A clean full-RTH capture day (no mid-session restart) + its raw tape. Then: `ops/daily_lifecycle.sh`
  (acquire raw T+1 → tick-aware sweep → lifecycle) lands clean-day grades; two such days → first VALIDATED
  cohort → backfill agent + MA pick it up via `trusted_features`.
- 06-16/06-17: once `quant-backfill` finishes the raw tape AND the capture ran a clean session, sweep them.

## Open / standing
- Daily lifecycle forward; quarantine only REAL RTH divergence (none found — every "divergence" so far is
  contamination/thin-name/coverage artifact).
- Watch for clean days; report the first trusted cohort the moment it crosses.
- PR #72 (tick-aware materialize + dtype fix + thin-session gate + trusted surface) pending review/merge.
