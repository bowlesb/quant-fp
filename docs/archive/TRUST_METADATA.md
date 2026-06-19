# Trust metadata lifecycle — what "validated" means and when it is written

> **ARCHIVED (superseded 2026-06-17 by the binary-trust redesign).** This describes the retired
> 4-state `lifecycle_state` model (`PENDING/VALIDATED/DIVERGENT/RETIRED`) and the
> `lifecycle_state = 'VALIDATED'` gate. The trust gate is now the binary
> `feature_trust.trust_state = 'TRUSTED'` — see `docs/TRUST_REDESIGN.md` for the design of record and
> `quantlib/features/trusted_list.py` for the live accessor. The `lifecycle_state` column itself is
> still WRITTEN (every parity sweep) and READ as a per-feature DIAGNOSTIC badge on the feature grid,
> but it is no longer the gate. Kept for historical reference only.

This is the operator's reference for the **feature-validation metadata**: the durable markers that say
which feature *values* have been proven to reproduce backfill. It answers two questions precisely:

1. **What does it MEAN when a feature is marked validated?**
2. **WHEN does that marker get written** (the end-to-end path from a raw tape to a trust grade)?

It complements `docs/PARITY_LIFECYCLE.md` (the design rationale) and `docs/VALIDATION_LEDGER.md` (the
durable-record design). Here we trace the metadata itself.

## The one-sentence definition

> A feature is **`VALIDATED`** when, on **≥ 2 CLEAN trading days**, the values our LIVE collector wrote
> (`source=stream`, from `compute_latest` during capture) matched the values BACKFILL computed
> (`source=backfill`, from `compute` over the `/store/raw` tape) **cell-for-cell within tolerance** on
> **every** one of those clean days (match rate ≥ 0.999 over compared cells).

"Clean" means the live capture for that symbol-day had no restart gap and full regular-session coverage
(`quantlib/features/cleanliness.py`), so a divergence can only be a real `compute_latest != compute`
bug, never lost live data. `VALIDATED` is **not** "the feature is predictive" and **not** "deep history
equals live" — it is exactly *"the live compute path reproduces the backfill compute path on recent
overlap, on days where the live data was complete."*

## Where the metadata lives

| Table (Postgres) | Grain | What it records |
|---|---|---|
| `feature_validation_day` | (feature, version, day, tier) | per-day rollup: `n_compared`, `n_match`, `value_rate`, `coverage_rate` |
| `feature_validation_exception` | (feature, symbol, ts) | the rare diverging cells (the evidence), refreshed per day |
| `feature_trust` | (feature, version) | **the trust marker** — `lifecycle_state` (PENDING/VALIDATED/DIVERGENT/RETIRED) + clean-day counts; plus the legacy `status`/`value_grade`/`coverage_grade` |
| `feature_parity_defect` | (feature, version) | the OPEN investigate-queue row for every DIVERGENT feature |
| `stream_symbol_day_cleanliness` | (symbol, day) | the audit trail: why a symbol-day was CLEAN or contaminated |

The parquet `<val_root>` (`/store/_validation/...`) mirrors `feature_validation_day` / `feature_trust`
as the cross-day accumulation source; Postgres is the canonical queryable record store. `feature_trust`
is a **pure recompute** over the day rows, so re-validating a day is idempotent and self-healing.

**Two grades coexist on `feature_trust`, intentionally:**

- `lifecycle_state` (PENDING/VALIDATED/DIVERGENT/RETIRED) — the **contamination-aware** grade, computed
  over CLEAN days only. *This is the one downstream code gates on* (`trusted_feature_names()`).
- legacy `status`/`value_grade`/`coverage_grade` — the older grade over **all** cells (contaminated or
  not). One contaminated day can flip it to `divergent`; it is kept for the overlap-certification gate
  but is NOT the trust signal. (On a contaminated day expect this to look bad — that is the whole reason
  the lifecycle grade exists.)

## When the marker is written — the end-to-end path

```
  /store/raw tape          materialize_from_raw         validate(stream vs backfill)        lifecycle grade
  (bars, download-once) ──> source=backfill features ──> feature_validation_day  ──────────> feature_trust
                                                          feature_validation_exception        .lifecycle_state
                                                                                              feature_parity_defect
                                                                                              stream_symbol_day_cleanliness
```

Driven by the nightly **`validation_sweep`** for a settled (T+1) day:

1. **Discover** the live-collected symbols (`source=stream`).
2. **Materialize** each chunk's backfill from `/store/raw` (`materialize_from_raw`) — **with SPY/QQQ
   pinned in** so the cross-sectional features resolve their market reference.
3. **Validate** the chunk: classify every cell match / mismatch / extra_live / missing_live, write
   `feature_validation_day` + exceptions + the legacy trust + the canonical DB record.
4. **Cleanliness**: per (symbol, day) decide CLEAN vs contaminated; write `stream_symbol_day_cleanliness`.
5. **Grade** over CLEAN comparisons only; set `lifecycle_state`; file a `feature_parity_defect` for any
   feature that failed parity on a clean day.

### The clean-breadth floor (why "today" writes 0 grades)

A day must have **≥ 20 CLEAN symbols** to contribute a clean-day grade at all. A capture-contaminated
day (e.g. a live restart, or grading *today* mid-session) yields too few clean names; the sweep then
**records the cleanliness rows but writes NO `lifecycle_state`** and files **no defects** — every feature
stays `PENDING` for that day. This is correct and expected: you will see `feature_validation_day` +
`stream_symbol_day_cleanliness` populated but `feature_trust.lifecycle_state` still NULL until enough
clean days accumulate. Honest scope: on a contaminated day, **0 graded, 0 defects, cleanliness recorded.**

## The state machine

| State | Means | Enters when |
|---|---|---|
| `PENDING`   | not proven yet | < 2 clean days of comparison |
| `VALIDATED` | live reproduces backfill | ≥ 2 clean days AND parity held on **every** clean day |
| `DIVERGENT` | a real compute bug — **quarantined** (kept + collected, untrusted), defect filed | parity failed on **any** clean day |
| `RETIRED`   | terminal, manual last resort | operator only — never auto-recomputed |

`DIVERGENT` comes ONLY from clean-day failures (contamination never condemns a feature), and it wins over
`PENDING` (a single clean-day failure files a defect immediately).

## Features that cannot validate by construction (and how that is handled)

Some features could never reach `VALIDATED` regardless of correctness, because they never produce a
*compared* cell. The sweep must be built so these don't exist; the known classes:

- **Market-relative features** (`market_beta_*`, `idio_vol_*`, `market_return_*`, `nasdaq_return_*`,
  `relative_return_*`, `outperforming_*`, `market_corr_*`). They regress against SPY/QQQ. SPY/QQQ are
  ETF-screened out of the raw universe, so a backfill chunk without them produces null → every cell is
  `extra_live` → 0 compared → never validatable. **Handled** by force-including SPY/QQQ in
  `raw_backfill.universe_symbols` and pinning them into every sweep chunk (`MARKET_TICKERS`). After the
  fix these features show thousands of compared cells and can validate normally.
- **Warmup-only features** in the morning produce `missing_live` until the window fills; this is correct
  by construction and the lifecycle does NOT gate `coverage` for `nan_policy == "warmup"` features. They
  validate on their compared (post-warmup) cells.

If you see a feature whose `feature_validation_day.n_compared` is **always 0** (all `extra_live` or all
`missing_live`), that feature has a structural reference gap like the above — investigate the materialize
inputs, don't wait for it to "eventually" validate.

## How downstream code uses the marker

```python
from quantlib.features.trust_lifecycle import trusted_feature_names

trusted = trusted_feature_names()            # the VALIDATED set
export = [f for f in requested if f in trusted]   # never train/trade on an unproven feature
```

`feature_trust_grades()` returns `{feature: lifecycle_state}` for consumers that prefer to *flag* rather
than *exclude*.
