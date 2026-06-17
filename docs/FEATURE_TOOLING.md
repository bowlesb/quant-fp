# Feature tooling — selective backfill + data-coverage discovery

Two CLIs for the findings->features loop: backfill JUST a feature/group over history, and SEE what data
(and trust) each feature has. They reuse the existing parity-by-construction materialize path and the
validation agent's trusted-features surface — neither re-encodes "what's trusted" or how features compute.

## 1. Selective backfill — `quantlib.features.selective_backfill`

Materialize only the requested feature(s)/group(s) over a date range, WITHOUT recomputing all ~610.
Features in a group share compute, so the GROUP is the work unit; (group, date) is an independent,
atomic, resumable unit (skip-existing: a partition already on disk is not recomputed unless `--force`).
Days fan out across a process pool. Tick/order-flow groups are runnable because the worker uses
`materialize_from_raw_groups` (the full-tick from-`/store/raw` path scoped to the requested groups).

```bash
# A new feature's group, over a window:
python -m quantlib.features.selective_backfill --groups trade_flow --start 2024-12-12 --end 2026-06-16

# Specific feature names (resolved to their groups):
python -m quantlib.features.selective_backfill --features signed_volume_1m,trade_freq_5m --months 18

# Specific runner names/days (the small-cap RUNNERS lane — backfill THEIR ticks):
python -m quantlib.features.selective_backfill --groups microstructure_burst,trade_flow \
    --symbols GME,AMC,BBBY --start 2025-03-01 --end 2025-03-31

# The trusted -> lightGBM loop: backfill the groups owning the currently-TRUSTED features.
# The cohort GROWS as the nightly sweep promotes PENDING->VALIDATED; re-run to pick up new ones
# (skip-existing makes already-backfilled groups no-ops).
python -m quantlib.features.selective_backfill --trusted --months 18
```

Key flags: `--features`, `--groups`, `--trusted`, `--symbols` (default = full universe), `--start/--end`
or `--months`, `--processes`, `--force`, `--root`/`--raw-root`.

## 2. Feature-data coverage — `quantlib.features.feature_data`

The canonical "what data does feature X have, is it trusted, what's missing?" surface. Joins the THREE
sources of truth: the STORE partitions (stream + backfill dates, separately, + gaps), the TRUST surface
(the validation agent's `trusted_features` view via `trusted_list`), and the CATALOG (`REGISTRY.catalog()`).
(NB: `quantlib.features.introspect` is a DIFFERENT tool — realized-value distribution/contract sanity;
this one is about DATA COVERAGE.)

```bash
# One feature: its group's stream/backfill coverage, gaps, and per-feature trust state.
python -m quantlib.features.feature_data feature signed_volume_1m

# One group, with gap detection over a window (machine-readable):
python -m quantlib.features.feature_data group trade_flow --start 2024-12-12 --end 2026-06-16 --json

# All features x group x trust state (the catalog left-joined to the trusted cohort):
python -m quantlib.features.feature_data list

# The trusted cohort joined to its backfill coverage — "trusted AND (not-)yet-backfilled":
python -m quantlib.features.feature_data trusted --start 2024-12-12 --end 2026-06-16
```

`--deep` adds per-date row/symbol counts (slower). `--json` emits machine-readable output for agents.

## The loop

```
validation agent  →  trusted_features view (lifecycle_state='VALIDATED')
        │                       │
        │            feature_data trusted   ── "trusted AND not-yet-backfilled?" (gaps)
        ▼                       ▼
selective_backfill --trusted  →  materialize trusted groups over 378d  →  MA trains lightGBM
```

`feature_data` is the read side (what's trusted x what data exists x gaps); `selective_backfill` is the
write side (materialize the missing trusted coverage). Both gate on the SAME trusted surface, so there is
one coherent "trusted x data-exists x gaps" picture. The set grows as trust is earned; re-running the
loop incrementally backfills the newly-trusted features (none yet — needs a clean RTH day).
