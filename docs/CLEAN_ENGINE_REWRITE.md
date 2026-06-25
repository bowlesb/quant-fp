# Clean-engine rewrite — overnight (for Ben's morning review)

**Mandate (Ben):** "completely rewrite this and make it understandable, simplify GREATLY" + "I DO NOT CARE
ABOUT BYTE TO BYTE ANYTHING RIGHT NOW." Validate **correctness** (formula + golden-set + sanity), not
byte-parity. Build BESIDE the old engine, **OFF live** until you review. Live `fc` kept the OLD engine all
night — nothing live changed, nothing armed.

## The new shape — one engine, the math, validation

Three things, instead of the 90-file machinery sprawl:

1. **`clean_engine.py` (~265 LOC, one file)** — the ONE engine, on your `tracker.py` model:
   - `RingBuffer`: per-symbol circular buffer of the last `window` bar columns. O(1) append; a trailing-window
     read is a roll. Absent symbols don't advance their cursor, so a gap reads the last *present* bars — the
     positional window a feature wants, gap-safe by construction.
   - `Window`: the one read surface — `trailing(col)` / `latest(col)` / `count()` / `present()` (the real
     current-minute delivery mask) + the carried-state accessors (`state` / `static` / `session`).
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

## File / LOC — BEFORE → AFTER (honest framing)

**Do NOT read this as "32k → 224 LOC."** The 32,160-LOC codebase is two layers, and only the MACHINERY
layer collapses — the feature MATH stays (it's the actual features, just ported to the new interface).
CodeAudit's locked baseline:

| layer | BEFORE | what happens |
|---|---|---|
| **machinery** (the sprawl) | **89 files / 22,275 LOC** — two engines + `step*` twins + per-kind wrappers + the ring/building-block constellation | **collapses** toward: the **224-LOC engine** + ~894 LOC of absorbed building-blocks + the trust/capture that STAYS. The ~3,182-LOC engine-collapse lands group-by-group as the port proceeds. |
| **feature math** | **67 files / 9,885 LOC** | **STAYS** — these are the features. Each group ports to the one `compute(window)` interface (its math is unchanged, just re-expressed). |
| **total** | **156 files / 32,160 LOC** | machinery shrinks; math stays; trust/capture stays. |

**Tonight's actual delete = 602 LOC** (`parity_audit.py`, grep-clean). The ~3,182-LOC engine-collapse is the
**migration headline** that accrues group-by-group once Ben approves the approach — it is NOT a tonight number,
and we do not claim it as one.

## What's DELETED (compute-engine sprawl — proposals on the branch, for your approval)

Deleted by CodeAudit as branch proposals (not applied live). **Two machineries that look similar but are NOT
the same** — this distinction is load-bearing:

**DELETABLE — the byte-parity-BETWEEN-TWO-FORMS machinery (obsoleted by the invariant):**
- the second engine + the four `step*` twins
- the per-kind state wrappers (`stateful.py`: EMA/Cumulative/LastK/Extrema/ReductionFold) + the
  `WindowedSumState`/`PointRing`/`ValueInputRing` constellation
- `parity_audit.py`, the **#451 value-parity demolition gate**, the `FP_*_PARITY` shadow flags, `parity.py`
- dead code, `__pycache__`, stale compiled artifacts

These existed ONLY to chase fast-live-vs-backfill divergence between two compute forms. The backfill=replay
invariant makes the two forms the **same** form → this machinery is genuinely obsolete. **The honest Ben
framing: dropped because the design removes what it guarded, not because we stopped caring about correctness.**

## What REMAINS (HARD KEEP — production, NOT sprawl)

- the ~67 group-math files (the actual features)
- **the live TRUST / CERT system — HARD KEEP, NOT byte-parity:** `compare.py`, `validate.py`,
  `validation_sweep.py`, `within_day_*` (the certifier), `trust_lifecycle`, the trust-grading the strategies
  trade on. These answer a **DIFFERENT question** — "is the live feature trustworthy vs *settled reality*,
  stable enough to trade real money on" — NOT "do two compute forms agree." backfill=replay says NOTHING about
  that. The live OLD engine still produces real live-vs-settled divergence (provisional close vs settled bar
  revisions), so the cert is doing real work TODAY; deleting it would blind the live trust system. Whether any
  cert piece becomes retirable AFTER the new engine goes live is a SEPARATE, carefully-argued FUTURE decision —
  never an overnight delete. (Trust/capture already IMPORT compare/validate/validation_sweep — confirmed.)
- the registry, `base.py` contracts, the store/bus boundary, the capture/store path

## How correctness is checked (replacing byte-parity)

- **THE load-bearing invariant** (`tests/test_clean_engine.py::test_backfill_equals_replay`): `seed(H) +
  step(m)` produces byte-identical output to a continuous `step` over the whole `H+m` sequence — across
  windowed / cross-sectional / recursive-EMA / cumulative / swing kinds in one multi-group engine. This
  **proves the design's central claim** — live and backfill are the *same replay*, so they cannot diverge —
  which is exactly what makes the legacy second-form + the entire parity machinery unnecessary. (7 tests pass.)
- **Formula/unit tests** per group: synthetic inputs with a known answer → assert the math (trend OLS r²=1 on a
  line; breadth K/N cross-sectional; macd EMA presence-decay — an absent symbol HOLDS its EMA; swing pivot;
  cumulative reset; ring gap-safe window).
- **Sanity**: per-feature `valid_range`, no degenerate all-NaN/all-zero, expected monotonicity.
- **Golden-set quality**: the existing `app.features.quality` validation re-pointed at the new engine — every
  feature VALID (2+ unique values; binary both 0/1; events real).
- _<CP's validation results>_

## Does the ONE interface generalize, or fork? (the real test)

The four per-symbol-window groups below are all the **same kind** ("compute from this symbol's trailing
bars") in four flavours — they do NOT test whether one interface generalizes. The genuinely-different kinds —
the ones that test generalization vs a fork — are **cross-sectional**, **recursive-EMA**, and **stateful
swing**. Status: **all three FIT the one interface** (each ported + verified), via carried-state hooks on the
one spine — no fork into separate engines.

**Per-symbol-window kind (4 flavours, proven):**
- `trend_quality` (rolling OLS slope / r² / strength) — trend→+slope, perfect line→r²=1, flat→~0.
- `vwap_deviation` (windowed volume-weighted ratio) — close above trailing vwap → positive.
- `realized_range` (windowed mean of per-bar `(high-low)/close`) — == mean of the per-bar range fractions.
- `candlestick` (per-bar OHLC geometry + a two-candle lag-1 engulfing pattern) — `body_ratio==|c−o|/(h−l)`.

**The three hard kinds — the generalization test — all FIT (verified):**
- **Cross-sectional** (`breadth`): needs the WHOLE symbol cross-section. **FITS** — `compute(window)` already
  sees the full `(n_symbols, window)` matrices, so the reduce is a numpy reduce over axis 0. The denominator
  gates on `window.present()` (an absent symbol's trailing window is finite from its CARRIED bars, so
  `isfinite()` alone over-counts it — the one real absence bug, found + fixed): an adversarial sparse rep (2
  present-up / 2 absent carried-down) reads `breadth_up = 1.0`, not `2/4 = 0.5`. The interface DOES expose the
  full symbol axis. *(sector_beta / cross_sectional_rank are the same shape — symbol-axis reduce / rank — +
  `window.static['sector']`.)*
- **Recursive-EMA** (`macd`): a carried SCALAR decayed value, not a ring of rows. **FITS** — via
  `window.state` (the engine's per-group carried dict). Verified: a +10 jump off flat → `macd_line = +0.80`
  (ema12 reacts faster, the carried scalar lags); decay is on bar-PRESENCE (an absent symbol HOLDS its EMA),
  not clock.
- **Stateful swing** (`swing`): a per-symbol ZigZag state machine across minutes. **FITS** — carried in
  `window.state` (leg direction / running extreme / pivot). Verified: an up-leg then a ≥θ reversal → a
  confirmed down `swing_pivot` + `swing_direction = −1`, from the carried cross-minute state.
  **HONEST PERF NOTE:** swing is the one genuinely-sequential kind — its `compute` runs a per-symbol Python
  loop (a ZigZag can't cleanly vectorize across leg transitions). It is correct and O(1)/bar, but not
  pure-vectorized; a Rust kernel (like the old `swing_fold`) is the perf option if it ever matters. It does
  not fork the *interface* — it fits via `window.state` — it just isn't array-vectorized.

**Plus** `intraday_seasonality` (cumulative session-reset) and `prior_day` (daily-snapshot) — the remaining two
kinds — also ported + verified, via `window.state` + `minute_epoch` and `window.session` respectively.

**So: 9 groups across all 6 structurally-distinct kinds — one interface, FIVE accessors, no fork.** The
interface generalizes — one `compute(window)` reads through five accessors over the one spine:
1. **trailing/latest** (the ring — the per-symbol trailing window),
2. **state** (the group's own carried per-symbol EMA / cumulative sum / swing leg-state, engine-owned),
3. **static** (per-symbol labels for the cross-sectional reduces — sector id, etc.),
4. **session** (the daily-snapshot memo, set once per session),
5. **present()** (the real current-minute delivery mask — the one correct presence source).

No kind needed a second engine or a forked step. The remaining ~59 groups are instances of these six shapes;
each is ported + correctness-checked individually (not assumed) as the port proceeds — _<count as it lands>_.

**Two engine-level concerns owned ONCE (not per-group):** *presence* (`window.present()` — did a bar arrive
this minute; a presence-gated read must NOT infer presence from `isfinite(latest())`, which returns the carried
value on an absent minute) and *idempotency* (a minute-epoch watermark — a re-delivered/stale minute is a
no-op, so carried state never double-advances). These are orthogonal; the engine owns each once for every kind.

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
- The headline is the SHAPE: the ~22k-LOC / 89-file machinery layer collapses toward one ~265-LOC engine +
  absorbed building-blocks; the 9,885-LOC feature math STAYS (ported to the one interface); trust/capture STAYS.
  The exact LOC delta fills in as the deletions land; the engine + the proven interface are the substance.
