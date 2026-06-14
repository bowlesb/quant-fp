# Feature Types & Storage — Author's Guide

Every feature declares its contract once, in its group's `declare()`, as a `FeatureSpec`. The engine
enforces that contract (the conformance gate rejects any undeclared column, wrong dtype, or out-of-range
value), and the store uses it to pick a compact on-disk dtype. This guide explains the type fields and how
to choose them for a NEW feature.

## The two dtypes: compute vs storage

A feature has two distinct dtypes, and they are deliberately different:

| | field | value | why |
|---|---|---|---|
| **Compute** | `dtype` | always `"Float64"` | all the math runs in double precision — uniform, simplest, exact |
| **Storage** | `storage` (or derived) | `Float32` / `UInt8` / `Int16` / … | disk is the scarce resource; nothing needs 8 bytes on disk |

The compute path is always Float64. Only the *persisted* copy is narrowed. This is safe because:

- **Parity is stored-vs-stored.** The T+1 parity check compares the stored stream value against the stored
  backfill value. Both are produced by the *same* code and narrowed by the *same* rule, so they round
  identically — the diff stays ~0 regardless of the storage width.
- **ML trains on Float32 anyway.** No model needs Float64 inputs.
- **Reads widen back.** `store.get_features` casts every feature column back to Float64 on read, so parity,
  training export, and the API all see the uniform compute dtype. The narrowing is invisible above the disk.

Result: ~54% smaller on disk (4152 → 1924 bytes/symbol-minute) before zstd, and the low-cardinality
columns also compress far better.

## How storage dtype is chosen

`storage_dtype(spec)` (in `base.py`) decides, in this order:

1. **Explicit declaration wins.** If you pass `storage="UInt8"` (or any polars dtype name), that is used.
   This is the preferred, self-documenting way for anything non-obvious.
2. **Integer calendar overrides.** The four genuine integer calendar features map to the smallest int.
3. **Flag rule.** A name starting with `is_` / `sector_is_` / `pattern_` / `above_` / `outperforming_`
   **and** a `valid_range ⊆ [-0.01, 1.01]` → `UInt8`.
4. **Default.** Everything else → `Float32`.

The rule covers today's 519 features correctly (survey-verified: 468 Float32, 47 UInt8 flags, 4 small ints,
0 needing Float64, 0 false positives). But a rule is implicit — so when you add a feature whose *type* isn't
obvious from its name and range, **declare `storage` explicitly.**

## Choosing types for a new feature

**Pick the storage dtype by the feature's semantics:**

- **Real-valued** (returns, ratios, z-scores, correlations, volatilities, slopes, percentiles): `Float32`.
  This is the default — you don't need to set `storage`. Float32 gives ~7 significant digits; the tightest
  tolerance in the platform is `1e-6` and that's a stored-vs-stored comparison, so Float32 is always enough.
- **Binary 0/1 flag/indicator/one-hot**: `storage="UInt8"`. Polars `UInt8` is **nullable** (validity
  bitmap), so it correctly holds the `null` that a `warmup`/`sparse` flag emits before it has data — unlike
  a numpy uint8. Give it `valid_range=(0.0, 1.0)` and a flag-style name and the rule will also infer UInt8,
  but **declaring it is clearer**.
- **Small integer / count / category code** (day-of-week, week-of-month, minute-of-day, a sector *index*):
  declare the smallest int that covers the range — `UInt8` (0–255), `UInt16` (0–65535), `Int16` (signed).
  The rule will NOT infer this from range alone (a `[0,1]` continuous ratio looks the same), so **declare
  it.**
- **Never `Float64`.** If you think a feature needs it, reconsider — for a point-in-time ML feature store it
  doesn't.

**The other contract fields:**

- `valid_range=(low, high)` — the closed range the gate enforces. Use `None` on a side that's genuinely
  unbounded (z-scores: `(None, None)`; non-negative unbounded: `(0.0, None)`). Tight, honest ranges also
  document the feature and drive the flag rule.
- `nan_policy` — `"none"` (never null; e.g. calendar, sector one-hots), `"warmup"` (null until the window
  has enough history), `"sparse"` (null when the input is legitimately absent). Be honest: a flag that can
  be null during warmup is `"warmup"`, and its storage MUST be nullable (UInt8/Boolean are — plain ints in
  numpy are not, which is why we store via polars).
- `tolerance` — relative parity tolerance (default `1e-6`). Loosen (e.g. `1e-4`) only for features with
  genuine float-order sensitivity (OLS slopes, autocorrelations).
- `layer` — `"A"` minute bars, `"B"` minute tick-aggregates, `"C"` sub-minute ticks. Drives which parity
  harness covers it.
- `parity_method` — `"tolerance"` (cell-level) for almost everything; `"distributional"` only for features
  where exact cell parity isn't meaningful.

## Example

```python
def declare(self) -> list[FeatureSpec]:
    return [
        # real-valued -> Float32 by default, no storage= needed
        FeatureSpec(name="vwap_deviation_5m", description="...40+ chars...",
                    dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="sparse", layer="A"),
        # binary flag -> declare UInt8 explicitly (nullable: holds warmup null)
        FeatureSpec(name="above_vwap_5m", description="...40+ chars...",
                    dtype="Float64", valid_range=(0.0, 1.0), nan_policy="warmup", layer="A",
                    storage="UInt8"),
        # small integer -> declare the smallest int
        FeatureSpec(name="minutes_since_open", description="...40+ chars...",
                    dtype="Float64", valid_range=(-570.0, 870.0), nan_policy="none", layer="A",
                    storage="Int16"),
    ]
```

## Checklist when adding a feature

1. Compute in Float64 (`dtype="Float64"`), always.
2. Set an honest `valid_range` and `nan_policy`.
3. Set `storage` explicitly unless it's plainly real-valued (then the Float32 default is right).
4. Binary → `UInt8`; small int → smallest int; everything else → leave default Float32.
5. `make qa` + the storage-dtype test (`tests/test_fp_storage_dtype.py`) keeps the mapping honest.
