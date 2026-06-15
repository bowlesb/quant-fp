# 2026-06-15 ~15:00 ET — swing `fib_retracement` degenerate-micro-leg guard (LOCAL, FIXED-CODE)

## FOR THE OWNER (top)
- **No live action needed; nothing deployed.** FIXED-CODE feature-value change, merged to
  `integration/converged`. The running `feature-computer` only reloads code on restart — per the
  SEQUENCING RULE this ships on the ONE batched clean restart (with CRITICAL-1/-2/-3), not for this alone.
- **After that restart:** swing's `source=stream` data is recollected anyway (CRITICAL-2 buffer<session);
  the recompute now carries the fib guard.
- **swing still BLOCKED-ON-SYSTEMIC** on CRITICAL-2 (warm-start / buffer depth). This cycle closed the only
  open LOCAL item on the group.

## What I advanced (my lane = feature-group data quality)
Closed the last queued LOCAL swing fix: `fib_retracement` exploded on confirmed **micro-legs** (near-zero
prior-leg range → tiny denominator). Real evidence from today's store (39,053 rows, deduped):
- `fib_retracement` max **450** (BBN/PVL/CIGI — thin names); **1261 rows (~3.9% of finite) beyond the
  declared ±10 valid_range**, 31 rows > 100. Bulk distribution is sane (median 1.03, p75 2.06), so ±10 is a
  fair boundary and everything past it is denominator degeneracy, not real structure.

**Fix — parity-safe output guard (no rust rebuild):** `swing_fold_frame` is the *single* fold code path
(live `compute` over the session buffer == backfill `compute`), so a guard there is live==backfill
cell-for-cell by construction. Added `FIB_MAX_ABS=10.0`; any `|fib_retracement| > FIB_MAX_ABS` becomes
**null** (undefined basis — same precedent as `bb_position`→null on a flat window). Pre-existing warmup
nulls are preserved. `valid_range` now references the same constant so the guard and the declared range
can't drift.

This **supersedes** the previously-queued `rust/src/lib.rs:860` denominator-floor: the output guard is
parity-true against the *existing* baked kernel and fully testable now, so no dev-image rebuild is required
to land it (the rust floor is now optional, not blocking).

**Parity (sacred):** mirrored the identical guard in the pure-Python parity reference (`_python_swing`,
`tests/test_fp_swing.py`) so the cell-for-cell reference-pin stays exact. Added
`test_swing_fib_degenerate_microleg_guarded` — a hand-crafted close path that drives the raw kernel fib to
~13.5 and asserts the guarded frame nulls it and keeps every surviving value within ±10.

**Verification:** `tests/test_fp_swing.py` 6 pass (reference-pin, no-look-ahead, fold==reseed,
compute_latest==backfill, valid, new guard); `tests/test_fp_latest.py` + `tests/test_fp_stateful_emit.py`
48 pass / 2 skip. ruff not installed in fp-dev/host this env → manual style review (mirrors existing idioms).

## STOP-CHECK status
All active groups OK / FIXED-CODE / BLOCKED-ON-SYSTEMIC / PENDING-STREAM — no `UNAUDITED` / `ISSUES-LOCAL`
remain. With this swing fib guard landed, **every group with an open LOCAL fix is now resolved**; the
remaining non-OK groups are all blocked on main-loop SYSTEMIC items (CRITICAL-2 warm-start, HIGH-SECTOR,
HIGH-DAILYLOAD, P0-UNIVERSE, MED-BETA) or PENDING-STREAM. Next cycles: STOP-CHECK regression re-verify of
previously-OK groups until the clean restart unblocks the FIXED-CODE recollects.
