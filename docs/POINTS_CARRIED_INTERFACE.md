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

## Two ways to source the ring — pick one (the only open decision)

- **(a) New `PointRing` kind** in the taxonomy (the interface above), declared per group, parity-tested once.
  Clean kind boundary; ~1 new state class.
- **(b) Reuse `_matrix_at`'s slice-derive tail / `_CodedBuffer`** as the carried point source — the engine
  already materializes each symbol's last `max_lag+1` positional rows every minute for the value columns;
  the points are the SAME positional reads off that SAME tail. **No new state class, no second ring** — the
  points stop being a separate whole-buffer pass and become extra reads off the tail the engine already
  builds. **Lower surface; recommended** unless the taxonomy wants the explicit kind for Stage-3 reuse.

Both clear the same gate (#435). The choice is purely how much new surface vs. taxonomy explicitness
ArchOverhaul's Stage-3 wants.

## Ship plan once ratified

resolve_points carried path = the first parity-gated PR ON this interface (the proof-of-interface). Then
`matrix_at` is the next consumer of the SAME positional tail (its own slice-derive already is it), and
ArchOverhaul's Stage-3 builds the rest on the ratified primitive.
