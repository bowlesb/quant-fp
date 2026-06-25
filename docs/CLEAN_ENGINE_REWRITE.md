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
   the carried window, framework-free. *(9 ported + CP-validated so far — one+ per kind; see the generalization
   section. The remaining ~59 wait on the morning greenlight.)*

3. **Validation = correctness** — formula tests (known synthetic inputs → assert the closed form / intuition),
   sanity (valid ranges, no all-NaN/all-zero, monotonicity), and the golden-set quality checks. **Not**
   byte-identity to the old floats.

## File / LOC — BEFORE → AFTER (honest framing)

**Do NOT read this as "32k → 265 LOC."** The 32,160-LOC codebase is two layers, and only the MACHINERY
layer collapses — the feature MATH stays (it's the actual features, just ported to the new interface).
CodeAudit's locked baseline:

| layer | BEFORE | what happens |
|---|---|---|
| **machinery** (the sprawl) | **89 files / 22,275 LOC** — two engines + `step*` twins + per-kind wrappers + the ring/building-block constellation | **collapses** toward: the **265-LOC engine** + ~894 LOC of absorbed building-blocks + the trust/capture that STAYS. The ~3,182-LOC engine-collapse lands group-by-group as the port proceeds. |
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
  which is exactly what makes the legacy second-form + the entire parity machinery unnecessary. (11 keystone
  tests + 44 group tests pass; CP validated independently — see the status table below.)
- **Formula/unit tests** per group: synthetic inputs with a known answer → assert the math (trend OLS r²=1 on a
  line; breadth K/N cross-sectional; macd EMA presence-decay — an absent symbol HOLDS its EMA; swing pivot;
  cumulative reset; ring gap-safe window).
- **Sanity**: per-feature `valid_range`, no degenerate all-NaN/all-zero, expected monotonicity.
- **Golden-set quality**: the existing `app.features.quality` validation re-pointed at the new engine — every
  feature VALID (2+ unique values; binary both 0/1; events real).
### CP independent validation — all 6 kinds GREEN

CP (separate agent, its own adversarial sparse/gap shapes, NOT my tests) validated the engine at `1198b82`:
**44 pass / 0 xfail**, every kind green, no regression. Per kind:

| kind | group | what CP checked | result |
|---|---|---|---|
| windowed | `trend_quality` / `vwap` / `realized_range` / `candlestick` | formula on known inputs (OLS r²=1, range mean, engulfing) | **GREEN** |
| cross-sectional | `breadth` | sparse minute: 2 present-up / 2 absent-carried-down → `breadth_up=1.0` not `0.5` (absent excluded from denominator) | **GREEN** |
| recursive-EMA | `macd` | EMA holds across a per-symbol gap (presence-decay), no decay-to-zero on an absent minute | **GREEN** |
| cumulative | `intraday_seasonality` | since-open count does NOT increment on an absent minute; resets at session boundary | **GREEN** |
| swing | `swing` | ZigZag pivot from carried leg-state; idempotent on a re-delivered minute (watermark, not present) | **GREEN** |
| daily-snapshot | `prior_day` | compute-once / broadcast from `window.session` | **GREEN** |

Plus CP independently re-derived the **keystone** (`seed(H)+step(m) == step(H+m)`, carried state bit-identical) and
both engine-level concerns (presence + idempotency) with no regression to the per-symbol kinds.

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

No kind needed a second engine or a forked step. The 9 ported groups (one+ per kind) are CP-validated green;
the remaining ~59 groups are instances of these same six shapes and will each be ported + correctness-checked
individually (not assumed) once the approach is approved — that bulk port is the work the morning gate unblocks.

**Two engine-level concerns owned ONCE (not per-group):** *presence* (`window.present()` — did a bar arrive
this minute) and *idempotency* (a minute-epoch watermark — a re-delivered/stale minute is a no-op, so carried
state never double-advances). These are orthogonal; the engine owns each once for every kind.

**Why `present()` is the one correct presence source.** A presence-gated read must NOT infer presence from
`isfinite(latest(col))`: `latest()` returns the *carried* value for a symbol that delivered no bar this minute,
so `isfinite()` reads True even on an absent symbol. That mis-inference is wrong for *any* gated kind — it would
advance an absent symbol's EMA, increment its cumulative count, or (most visibly) inflate the cross-sectional
denominator. **Cross-sectional `breadth` was the case that surfaced the bug** (a stale name visibly shifts a
market-wide fraction), but the fix is general: every group that builds a count / denominator / mask gates on
`window.present()`, the engine's real delivery mask, not on `isfinite()` of a carried read.

**Commit history (accurate):** `f3ff4bc` added `window.present()` to the engine and switched the four gated
groups that were then in play — `macd` / `intraday_seasonality` / `swing` / `prior_day`. `1198b82` caught the
**missed** cross-sectional case (`breadth`, in `clean_groups_example.py`, not under `groups/`) by reproducing
its adversarial sparse rep rather than assuming "present() exists everywhere" — and switched it too. The watermark
(idempotency) landed separately at `5d5f564`; it fixes the *duplicate* axis and is independent of presence.

## Taking it live (when you approve)

The new engine runs BESIDE the old; nothing is armed. To go live: wire `CleanEngine` into the capture path
behind a flag (default-off), seed from the live buffer, validate the golden-set on a session off-line, then
arm on a canary shard under the existing deploy seam — same staged, reversible rollout as every prior step.
The trust system is unchanged; it grades the new engine's output exactly as it grades today's.

## Honest status (no inflation)

- **DONE + validated:** the clean engine core (CleanEngine + RingBuffer + Window + EngineGroup), the one
  `compute(window)` interface, **9 groups across all 6 structurally-distinct kinds ported + independently
  CP-validated green** (windowed / cross-sectional / recursive-EMA / cumulative / swing / daily-snapshot), the
  keystone invariant proven, both engine-level concerns (presence + idempotency) owned once and tested, the
  deletion guardrail (trust/cert system carved out as HARD KEEP, all deletions = branch proposals).
- **NOT done (waits on your greenlight):** porting the remaining ~59 groups; CodeAudit's deletion PRs landing;
  the go-live wiring. None of this is started — it's the work your morning decision unblocks.
- The headline is the SHAPE: the ~22k-LOC / 89-file machinery layer collapses toward one **265-LOC engine** +
  absorbed building-blocks; the 9,885-LOC feature math STAYS (ported to the one interface, math unchanged);
  trust/capture STAYS. Tonight's *actual* delete = 602 LOC (`parity_audit.py`); the ~3,182-LOC engine-collapse
  is the migration headline that accrues group-by-group once you approve the approach — not claimed as tonight.

## What you (Ben) decide in the morning

This is a **design-approval gate**, not a "ship it" gate. Nothing is live; nothing is merged. Three decisions:

1. **Approve the approach?** The shape is: one `compute(window)` interface + five accessors (trailing/latest,
   state, static, session, present()) + one `seed/step` engine, with backfill==replay as the structural reason
   the entire byte-parity machinery is obsolete. 6 kinds prove it generalizes (no fork). **If yes →** it
   becomes the target architecture and we port the rest to it. **If no / changes →** the engine is ~265 LOC +
   9 example groups on a branch, cheap to revise before any bulk port.
2. **Full-port scope + order?** ~59 groups remain, each an instance of one of the 6 proven shapes. Recommended:
   port in kind-batches (windowed first — the bulk and simplest; cross-sectional next — present()-gated;
   recursive/cumulative/swing last — carried-state), each group correctness-checked individually (not assumed),
   re-pointing the golden-set at the new engine as the rolling oracle. Decide whether you want all ~59 or a
   representative slice first.
3. **Go-live path?** When ported + golden-set-green, wire `CleanEngine` into capture behind a default-off flag,
   seed from the live buffer, soak on a canary shard under the existing deploy seam, then widen — the same
   staged, reversible rollout as every prior step. The trust/cert system is unchanged and grades the new
   engine's output exactly as it grades today's. **Cert retirement (if ever) is a separate, later decision
   AFTER the new engine is live and proven against settled reality — never bundled into this.**

Until you decide: the new engine stays **OFF-live**, `fc` runs the OLD engine, nothing is self-merged.
