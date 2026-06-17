# R1 VERDICT — small-cap morning runners

**Stage 1 (bars-only event characterization): DONE. See results.md.**

## One-line
Small-cap morning runners ($2–20 base, +50%+ in the first 30 min on a volume surge) **FADE** —
median close −17.8% off the first-30-min high (65% fade >10%), and forward returns are negative
and deepening (1d −6.3%, 3d −9.8%, 5d −13.9%; only ~30% still up at 5d). The tradeable shape is
**short/fade the runner**, not chase continuation. 643 CORE runner-days / 468 symbols over 379d.

## Dual verdict (per the core mandate)

### Strategy: PROMISING, NOT CERTIFIED — gated on EXECUTION REALITY (→ Stage 2)
The reversal is large and consistent, but a SHORT faces the binding constraints we pre-registered:
borrow availability/cost on hard-to-borrow low-float names, LULD halts breaking the intraday
entry, and the fact that the f30 HIGH is not a fillable short price (Stage 1's close-vs-high
OVERSTATES the shortable edge). Stage 2 must re-measure from a tradeable post-peak entry with a
per-trade non-overlapping bootstrap + borrow gate before any container.

### Feature: SHIP — batch-1b candidate **F9 `runner_state`** (spec below)
A real, parity-true, point-in-time runner-state detector. Large consistent forward sign, fully
non-redundant (no existing group encodes the small-cap-runner regime), not noise. Gives a model a
clean conditioning variable for the small-cap reversal regime. Cost/standalone-tradeability do NOT
gate a feature — this clears the lower feature bar decisively.

## Status
- Stage 1 event set: `runner_events.parquet` (1,571 rows, 963 syms). Committed.
- F9 spec: `F9_RUNNER_STATE_SPEC.md`. To be built in a WORKTREE+PR into batch-1b (never the live
  groups/ — fingerprint hazard), batched for the Lead's coordinated fingerprint deploy.
- Stage 2 (gated short certification + GPU job 1 sequence model): queued; depends on selective tick
  backfill of the 468 runner symbols (PR #75 `--symbols`) + borrow-reality data.
