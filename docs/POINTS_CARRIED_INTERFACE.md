# Carried-state `resolve_points` — interface proposal (for ratification)

Proposed by CriticalProfiler as the first concrete consumer of ArchOverhaul's `RunningState` abstraction
(docs/FEATURE_PREP_OVERHAUL.md). Goal: one ring representation, ratified before code, so resolve_points and
ArchOverhaul's Stage-3 build on the SAME primitive (no competing fork). Grounded in the measured PoC
(byte-identical, 107x) and the closed point vocabulary.

## What it replaces

`resolve_points` (declarative.py) re-runs every minute:

```python
frame.sort(["symbol", "minute"]).select(["symbol", "minute", *point_exprs]).filter(minute == latest)
```

a whole-buffer sort + expression pass to carry each group's `__pt_<name>` columns onto the latest row.
phase_profile: ~6ms of the shared ~41ms incremental step, 100% framework overhead, 0% arithmetic.

## The closed vocabulary (proof the ring is sufficient, not a heuristic)

Every point across all 18 incremental groups is exactly one of:

| shape | example | groups | carried form |
|-------|---------|--------|--------------|
| **at-T value** | `close`, `volume`, `high-low`, `close*volume`, the 1m tape cols | 15 of 18 | the latest ring row — **no lag state** |
| **positive lag** | `close.shift(w).over("symbol")`, `w ∈ {5,10,15,20,30,45,60,90,120}` | efficiency, return_dynamics, momentum_consistency | the **k-th prior present row** |
| **lag-1 delta** | `n_trades - n_trades.shift(1)` (accel) | trade_flow | latest − prior ring row |

There is no other shape. So a per-symbol ring of the recent point-SOURCE rows is provably sufficient.

## The interface (slots under the EXISTING `RunningState` contract — no new contract)

A group declares its points as today (`points()`); the engine carries them. No per-group hand-rolling.

```
PointRing(symbols, depth)               # depth = max declared positive lag (rows), 1 for at-T/delta-only
  .fold(minute_frame)                   # append THIS minute's present symbols' point-source values
                                        #   (advance each present symbol's row cursor; absent symbols do NOT advance)
  .at_t(source)    -> (n_sym,) array    # newest ring row  (NaN where the symbol has no row yet)
  .lag(source, k)  -> (n_sym,) array    # k-th prior ring row, POSITIONAL (NaN if < k+1 rows)
```

Lifecycle is the group-level `RunningState` contract already on `base.FeatureGroup` (base.py:349) — NOT a
new one:

- `up_to_date(buffer)` → False when cold / post-hot-swap / session-boundary / gap (delegates to the ring).
- `rebuild_from_history(buffer)` → `PointRing` re-seeds by folding every buffered minute (== backfill over
  the buffer). After it, `live ring state == backfill` by construction.

This is the SAME `seed`/`fold` shape `StatefulEngine` already uses for `LastKState`/`EMAState`/etc.
(stateful.py:758) — `PointRing` is a sibling kind, not a new mechanism.

## THE LOAD-BEARING INVARIANT (must live in the shared contract, tested)

`shift(w).over("symbol")` is **POSITIONAL** — the w-th prior **ROW**, NOT the bar w minutes ago. On a
**sparse** symbol (gaps) positional ≠ time-based; proven against backfill truth (the gap symbols of a gapped
efficiency fixture differ on `__pt_l5`/`__pt_l10`). Therefore:

> **`PointRing.fold` advances a symbol's cursor ONLY on minutes that symbol is present; `lag(source, k)`
> reads the k-th prior PRESENT row.** An epoch-keyed lag (`LastKState`) is WRONG here and must not be used
> for these points.

This is the SAME positional encoding `_matrix_at`'s `slice_derive` tail (`tail(max_lag+1)` ROWS per symbol)
and the Rust `_CodedBuffer` gather already use — **reuse that exact encoding, do not re-derive it**. That is
why (b) below is the low-surface option.

Gate: `tests/test_fp_points_carried_parity.py` (PR #435) — `CarriedPoints` positional row-ring ==
`resolve_points` byte-identical on a genuinely-sparse fixture (vacuity-guarded) + the seed/fold replay
invariant. This IS the executable contract.

## Two ways to source the ring — RESOLVED to (a) by a viability check

Initially (b) "reuse `_matrix_at`'s slice-derive tail" looked lowest-surface. **A measured check killed it:**
the value-derive tail is only `max_lag+1` rows where `max_lag = 5` (the value columns are short-lag returns),
but the POINT lags reach **`shift(120)`** (efficiency / return_dynamics). Reusing the tail would force it to
deepen from 6 to **121 rows per symbol every minute** — a 20× deeper slice-derive of ALL value columns,
which makes `_matrix_at` (the next phase we want to cut) SLOWER. (b) defeats its own purpose.

- **(a) Dedicated `PointRing` — RECOMMENDED.** A numpy ring of ONLY the point-SOURCE columns
  (`close` / `volume` / `high` / `low`) at depth 121, separate from the value tail. Measured: fold+resolve =
  **0.10ms** vs `resolve_points` 14.4ms (**138× cheaper**), **1.2 MB/shard** (312 × 121 × 4 × 8B). The fold is
  an array shift+write; resolve is indexed reads. It does NOT widen the value tail.
- **(b) Reuse the value tail — REJECTED** (forces a 121-deep all-column slice-derive; net-slower).

So: a dedicated `PointRing` state kind (option a). It still slots under the SAME `RunningState` lifecycle
contract and clears the SAME gate (#435) — the only thing the viability check changed is "don't piggyback on
the value tail; carry the point sources in their own shallow-footprint ring."

## Ship plan once ratified

resolve_points carried path = the first parity-gated PR ON this interface (the proof-of-interface). Then
`matrix_at` is the next consumer of the SAME positional tail (its own slice-derive already is it), and
ArchOverhaul's Stage-3 builds the rest on the ratified primitive.
