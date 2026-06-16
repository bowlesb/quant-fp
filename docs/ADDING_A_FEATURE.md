# Adding a feature, end to end (the mechanical walkthrough)

This is the step-by-step recipe for taking a feature idea from a spec to a value that **flows live onto
the bus and into a strategy container** — with parity (live == backfill) guaranteed by construction. It
is written so a future agent (or subagent) can follow it mechanically. The companion docs are
`FEATURE_PLATFORM.md` (§3.1 one-function parity, §3.7 extension surface) and `STRATEGY_CONTAINERS.md`
(how the value reaches a container).

## The 30-second mental model

```
  raw ticks/bars            minute_agg frame              FeatureGroup                 bus + store
 (/store/raw, live)  --->  (symbol, minute, <cols>)  --->  declare()/compute()   --->  fv:<SYM> stream
   aggregates.py             loaders / raw_loaders          groups/<name>.py            quantlib/bus
```

Two invariants make the whole thing safe and are NON-NEGOTIABLE:

1. **One compute, two callers.** Live and backfill both call the SAME `compute()` (FEATURE_PLATFORM §3.1).
   You never write a "live version" of the math. A fast latest-minute form (`compute_latest`) is allowed,
   but it is held cell-equal to `compute().filter(last minute)` by `tests/test_fp_latest.py` — so it can
   never silently diverge.
2. **Point-in-time only.** A feature for minute `T` may read data `<= T` ONLY. No `datetime.now()`, no
   future bars, no `shift(-k)`. Look-ahead is caught by `tests/test_fp_lookahead.py`.

There are exactly **two kinds of feature work**, and which one you do decides how many files you touch:

| | A new feature over an EXISTING `minute_agg` column | A feature needing a NEW per-minute input |
|---|---|---|
| Example | another window of `signed_volume`; a ratio of existing columns | per-minute **OFI** from quotes; a new spread statistic |
| Files | **ONE** file in `groups/` | the aggregate primitive + the loaders + a group (3-4 files, parity-critical) |
| Owner | routine MA/Lead feature work | Lead-reviewed (touches the live capture path) |

Most ideas are case A. Do case A unless the raw per-minute quantity you need does not yet exist.

---

## Case A — a new group over existing `minute_agg` columns (the common path)

### Step 1 — pick the shape: declarative reduction, or raw group?

`minute_agg` is the per-`(symbol, minute)` frame the engine feeds every group: bar columns
(`open/high/low/close/volume`) plus the tick-aggregate columns
(`n_trades, signed_volume, mean_spread_bps, quote_imbalance, mean_bid_size, mean_ask_size`). Your group
declares which of these it reads via `inputs = (InputSpec(name="minute_agg", columns=(...)),)`.

- If your feature is a **windowed reduction** (rolling sum/mean/std of a per-minute quantity, or a
  windowed OLS) — subclass **`ReductionGroup`** (`quantlib/features/declarative.py`). You declare the
  reduction ONCE and the engine generates BOTH `compute()` (rolling, source of truth) and
  `compute_latest()` (single-pass Rust kernel) — parity-true by construction. This is the fast lane and
  >90% of groups use it (`trade_flow.py`, `quote_spread.py` are the canonical examples).
- If your feature is **genuinely weird** (non-windowed, custom polars) — subclass `FeatureGroup` directly
  and implement `declare()` + `compute()`. You then either inherit the default `compute_latest` (correct
  but does full rolling work) or override it with a bounded-window form via `compute_latest_on_window()`.

### Step 2 — write the ONE file under `groups/`

A `ReductionGroup` declares three things — `reduced()` (what to roll + which stats + windows), `points()`
(at-T scalar columns), `assemble()` (the features, written with the `mean_/std_/sum_/pt_` accessors):

```python
# quantlib/features/groups/my_flow_ratio.py
"""Buy-pressure ratio from existing trade-flow aggregates (family: TRADE_FLOW, Layer B)."""
from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec, FeatureType, InputSpec
from quantlib.features.declarative import ReductionGroup, sum_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 15, 30)


@register
class MyFlowRatioGroup(ReductionGroup):
    name = "my_flow_ratio"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TRADE_FLOW
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "signed_volume", "volume")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"buy_pressure_{w}m",
                description=(
                    f"Net signed volume as a fraction of total volume over the trailing {w} "
                    f"minutes (buy-minus-sell pressure normalized by activity)."
                ),
                dtype="Float64",
                valid_range=(-1.0, 1.0),
                nan_policy="warmup",
                layer="B",
            )
            for w in WINDOWS
        ]

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        return {
            "sv": (pl.col("signed_volume"), ("sum",), WINDOWS),
            "vol": (pl.col("volume"), ("sum",), WINDOWS),
        }

    def assemble(self) -> dict[str, pl.Expr]:
        # null/zero-volume windows -> null (mathematically undefined), NOT 0 — let the absence show.
        return {
            f"buy_pressure_{w}m": pl.when(sum_("vol", w) > 0)
            .then(sum_("sv", w) / sum_("vol", w))
            .otherwise(None)
            for w in WINDOWS
        }
```

Notes that catch people:
- **`description` must be >= 40 chars** (`base.MIN_DESCRIPTION_CHARS`) and say *what* + *unit* + *window*.
- **`valid_range`** is enforced by the engine — an out-of-range cell raises rather than silently storing
  garbage. A ratio in `[-1, 1]` declares that; an unbounded count declares `(0.0, None)`.
- **`nan_policy="warmup"`** for anything that needs trailing history (so early minutes are legitimately
  null); `"sparse"` for quote-derived features that are null on a quoteless minute; `"none"` only if it
  is ALWAYS finite.
- **Undefined math returns `None`, missing INPUT does not.** Divide-by-zero -> null is correct;
  swallowing a missing required column with `.otherwise(0)` hides a broken pipeline — don't.

### Step 3 — register it

Add the module to `quantlib/features/groups/__init__.py` (the import self-registers via `@register`):

```python
from quantlib.features.groups import (  # noqa: F401
    ...,
    my_flow_ratio,   # <-- add, keep alphabetical
    ...,
)
```

That is the ONLY wiring. `base/registry/engine/store/parity` are stable and untouched (FP §3.7).

### Step 4 — tests (REQUIRED — a feature without tests does not merge)

Two tests, both small:

1. **A math/unit test** with a hand-built `minute_agg` frame asserting exact values on a known case
   (model it on `tests/test_fp_new_families.py`). Cover at least one window and the divide-by-zero -> null
   edge.
2. **Parity is already generic** — `tests/test_fp_latest.py` auto-discovers every registered group and
   asserts `compute_latest == compute().filter(last minute)` cell-for-cell. You get this for free for a
   `ReductionGroup`; if you subclassed `FeatureGroup` and overrode `compute_latest`, this is the test that
   guards your override. Run it: `pytest tests/test_fp_latest.py -k my_flow_ratio` (or the whole file).

Also run the look-ahead guard: `pytest tests/test_fp_lookahead.py`.

### Step 5 — the feature-count / fingerprint bump

Adding features changes the bus **schema fingerprint** (the 64-bit hash over the feature set the
publisher and every consumer validate — `STRATEGY_CONTAINERS.md` "Schema fingerprint safety"). This is
expected. The Lead re-cuts the active feature set version; a strategy container rebuilt from the same
`quantlib` picks up the new fingerprint automatically (its `Dockerfile` `COPY quantlib`). A container on
the OLD fingerprint fails **loudly** on decode rather than misreading offsets — that is the safety, not a
bug. Do NOT hand-edit fingerprints.

### Step 6 — it flows to containers for free

Once registered and live, the producer publishes the new cells in every `fv:<SYMBOL>` frame. A container
reads them by name with no code change beyond *using* the new feature:

```python
vector.value("buy_pressure_15m")     # O(1) name -> float
```

Verify end to end with the bus inspector (no market hours needed):

```bash
docker run --rm --network quant_default -v "$PWD":/app -w /app --env-file .env -e PYTHONPATH=/app \
  fp-dev python -m strategies.tools.inspect_bus --symbols AAPL --once --full | grep buy_pressure
```

---

## Case B — a feature needing a NEW per-minute input (e.g. true OFI from quotes)

When the per-minute quantity you need is not yet in `minute_agg`, you add it at the **parity cornerstone**
— `quantlib/aggregates.py` — so the live ingestor and the historical backfiller produce it identically,
then thread it through the loaders, then write the group as in Case A. This touches the live capture path,
so it is **Lead-reviewed**. The worked example is **OFI** (Order-Flow Imbalance, Cont–Kukanov–Stoikov),
proposed in `experiments/.../` and specced in this repo.

1. **`quantlib/aggregates.py`** — add the field to the aggregate dataclass and compute it in the pure
   per-minute function. OFI needs the PREVIOUS quote, so (like trades' `TickState`) it threads a small
   state across minutes — keep the live==batch property by mutating that state in the function:
   ```python
   @dataclass
   class QuoteState:          # threaded across minutes, mirrors TickState for trades
       last_bid: float | None = None
       last_ask: float | None = None
       last_bid_size: float = 0.0
       last_ask_size: float = 0.0

   # in aggregate_quotes(...): for each consecutive quote, accumulate the CKS increment
   #   bid side: +bid_size if bid_price up; +(bid_size-prev) if equal; -prev_bid_size if down
   #   ask side: +ask_size if ask_price down; +(ask_size-prev) if equal; -prev_ask_size if up
   #   ofi += bid_e - ask_e
   ```
   Add a test in `tests/test_aggregates.py` modeled on `test_live_batch_parity` proving the threaded
   minute-by-minute OFI equals a single batch pass (the Layer-C parity cornerstone).
2. **`quantlib/features/tick_capture.py`** — add `"ofi"` to `TICK_COLUMNS` and to the dict returned by
   `aggregate_symbol_minute` (the live side).
3. **The backfill/DB side** — the per-minute `ofi` must land in `quote_agg_1m` (the table
   `loaders._MINUTE_AGG_SQL` left-joins) for BOTH `source='stream'` (live) and `source='backfill'`
   (settled REST). Add the column to the schema + the writer, and add `q.ofi` to `_MINUTE_AGG_SQL`. This
   is the Lead-owned pipeline change; coordinate it.
4. **The group** — a `ReductionGroup` over the new `ofi` column (windowed sums + a volume-normalized
   variant), exactly as Case A.
5. **The gate** — settled-day trade/quote-agg parity (live vs REST) MUST pass for the new column before
   the OFI feature is *trusted* in a model (`docs/ORDER_FLOW.md` invariant I2b). The feature can exist and
   publish before that; it just isn't trusted-for-trading until the T+1 parity test is green on it.

---

## Checklist (paste into the PR)

- [ ] Group file under `groups/`, `@register`, `owner`/`version`/`type` set, every spec description >= 40 chars.
- [ ] `valid_range` + `nan_policy` correct; undefined math -> `None`, missing input is NOT swallowed.
- [ ] Imported in `groups/__init__.py` (alphabetical).
- [ ] Unit test with a known-value case incl. the divide-by-zero/null edge.
- [ ] `pytest tests/test_fp_latest.py` (parity) and `tests/test_fp_lookahead.py` (no look-ahead) green.
- [ ] (Case B only) `aggregates.py` field + threaded-state parity test; `tick_capture.TICK_COLUMNS`;
      loaders SQL + `quote_agg_1m`/`trade_agg_1m` schema; settled-day parity gate noted.
- [ ] `make qa` (or the repo's lint/type gate) clean.
- [ ] Verified on the live bus with `inspect_bus --full | grep <feature>`.
- [ ] Lead re-cut the feature-set version (fingerprint bumped intentionally).
