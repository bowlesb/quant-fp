# Clean-engine rewrite — overnight (for Ben's morning review)

**Mandate (Ben):** "completely rewrite this and make it understandable, simplify GREATLY" + "I DO NOT CARE
ABOUT BYTE TO BYTE ANYTHING RIGHT NOW." Validate **correctness** (formula + golden-set + sanity), not
byte-parity. Build BESIDE the old engine, **OFF live** until you review. Live `fc` kept the OLD engine all
night — nothing live changed, nothing armed.

## The new shape — one engine, the math, validation

Three things, instead of the 90-file machinery sprawl:

1. **`clean_engine.py` (~190 LOC, one file)** — the ONE engine, on your `tracker.py` model:
   - `RingBuffer`: per-symbol circular buffer of the last `window` bar columns. O(1) append; a trailing-window
     read is a roll. Absent symbols don't advance their cursor, so a gap reads the last *present* bars — the
     positional window a feature wants, gap-safe by construction.
   - `Window`: the one read surface — `trailing(col)` / `latest(col)` / `count()`.
   - `CleanEngine`: `seed(history)` + `step(minute_bars)`. **One path.** The live step and the backfill are the
     **same replay** (`seed(H); fold(m) == seed(H+m)` by construction) — so there is no second "fast vs
     backfill" form and **no parity gate between them**. (This is the backfill=replay endpoint from the design
     doc, now the *whole* design.)
   - `EngineGroup`: the ONE interface — `compute(window) -> {feature: (n_symbols,) array}`. Numpy in, numpy
     out, no per-minute polars.

2. **The ~68 feature-math groups (KEPT)** — each becomes one small numpy `compute(window)`. Their legacy
   `reduced()/regressions()/assemble()` (or bespoke polars `compute()`) collapses to the same arithmetic over
   the carried window, framework-free. *(Ported so far: see "Ported groups" below.)*

3. **Validation = correctness** — formula tests (known synthetic inputs → assert the closed form / intuition),
   sanity (valid ranges, no all-NaN/all-zero, monotonicity), and the golden-set quality checks. **Not**
   byte-identity to the old floats.

## File / LOC — BEFORE → AFTER

| | BEFORE (main) | AFTER (this branch) |
|---|---|---|
| machinery `.py` files in `quantlib/features/` | **90** | _<filled as deletions land>_ |
| machinery LOC (non-group) | ~32,000 | _<filled>_ |
| the engine | 2 engines + 4 `step*` twins + per-kind state wrappers (`declarative.py` + `incremental.py` + `stateful.py` + the ring constellation, ~3,500 LOC) | **`clean_engine.py`, ~190 LOC, one file** |
| group-math files | 68 (kept) | 68 (kept, ported to one interface) |

## What's DELETED (compute-engine sprawl — proposals on the branch, for your approval)

Deleted by CodeAudit as branch proposals (not applied live):
- the second engine + the four `step*` twins
- the per-kind state wrappers (`stateful.py`: EMA/Cumulative/LastK/Extrema/ReductionFold) + the
  `WindowedSumState`/`PointRing`/`ValueInputRing` constellation
- the duplicate capture paths (`capture` / `real_capture` / `sharded_capture` redundancy)
- the **byte-parity machinery** Ben dropped: the #451 demolition gate, `validate.py` / `validation_sweep` /
  `parity_audit` / `compare`, the `FP_*_PARITY` shadow plumbing
- dead code, `__pycache__`, stale compiled artifacts

_<final delete list + LOC from CodeAudit>_

## What REMAINS (kept — production, not sprawl)

- the ~68 group-math files (the actual features)
- **the live TRUST / CERT system** — `within_day_*` certifier, `trust_lifecycle`, the trust-grading the
  strategies trade on. It grades *via* parity but it is a production capability the live trading depends on —
  explicitly NOT deleted. (Anything CodeAudit was unsure about is flagged here for your call, not cut.)
- the registry, `base.py` contracts, the store/bus boundary

## How correctness is checked (replacing byte-parity)

- **Formula/unit tests** per group: synthetic inputs with a known answer → assert the math.
- **Sanity**: per-feature `valid_range`, no degenerate all-NaN/all-zero, expected monotonicity.
- **Golden-set quality**: the existing `app.features.quality` validation re-pointed at the new engine — every
  feature VALID (2+ unique values; binary both 0/1; events real).
- _<CP's validation results>_

## Ported groups (so far)

- `trend_quality` (rolling OLS slope / r² / strength) — verified: trend→+slope, perfect line→r²=1, flat→~0.
- `vwap_deviation` (windowed volume-weighted ratio) — verified: close above trailing vwap → positive.
- _<batch as the port proceeds>_

## Taking it live (when you approve)

The new engine runs BESIDE the old; nothing is armed. To go live: wire `CleanEngine` into the capture path
behind a flag (default-off), seed from the live buffer, validate the golden-set on a session off-line, then
arm on a canary shard under the existing deploy seam — same staged, reversible rollout as every prior step.
The trust system is unchanged; it grades the new engine's output exactly as it grades today's.

## Honest status (no inflation)

- DONE: the clean engine core (verified), the interface, 2 representative groups ported + verified end-to-end,
  the deletion guardrail (trust system carved out, all deletions = branch proposals).
- IN PROGRESS: porting the remaining groups; CP's correctness validation; CodeAudit's deletion PRs + the final
  BEFORE→AFTER numbers.
- The headline is the SHAPE: 90-file machinery → one ~190-LOC engine + the kept math + correctness validation.
  The exact LOC delta fills in as the deletions land; the engine + the proven interface are the substance.
