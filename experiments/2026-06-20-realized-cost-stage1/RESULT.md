# Stage 1 — REALIZED per-name half-spread wired into the harness backtest cost (truth, no model)

**Date:** 2026-06-20  **Scope:** Stage 1 of cost-accuracy (the G0b finding). Replace the harness's flat
`DEFAULT_HALF_SPREAD_BPS = 3.0` backtest cost stub with the per-name half-spread MEASURED directly from the
quote tape at the entry instant. Measured = unimpeachable truth (no prediction risk); the PREDICTED model
for live/forward use is Stage 2 (pre-registered separately).

## What shipped
- **`quantlib/data/realized_cost.py`** — `realized_half_spread_bps(store, day, symbols, at_ts, window_min=5)`:
  reads the raw NBBO quote tape over `[at_ts - window, at_ts)` (strict `ts < T` — no look-ahead), filters to
  valid NBBO (bid<ask, sizes>0), and returns the TIME-WEIGHTED mean relative half-spread per name (a quote
  that stood 5s counts 5x a 1ms flicker). Spread formula matches `raw_loaders._tick_minute_columns` exactly.
  A name with < `MIN_QUOTES` valid quotes is omitted (caller falls back to the stub).
- **`quantlib/battery/panel.py`** — `_build_intraday_date` now attaches the realized tape half-spread FIRST,
  in precedence: (1) realized tape measurement → (2) store `quote_spread` column → (3) flat stub last
  (`pl.coalesce`). Gated by `USE_REALIZED_COST` (default ON; `=0` reverts to the legacy path).
- **`tests/test_realized_cost.py`** — 6 hermetic unit tests (synthetic quote partitions): constant spread,
  time-weighting favors long-dwell quotes, strict `ts<T` no-look-ahead, invalid-NBBO/too-few-quotes drop,
  missing-symbol omitted, MIN_QUOTES enforced. All pass. Harness (8) + battery (18) suites still green.

## Before/after on a prior verdict (the proof: accurate cost makes verdicts MORE-null)
Re-booked the SAME trusted-baseline harness $-curve (42 dates, top-200 liquid, walk-forward GBM, 3,621 OOS
rows) under FLAT stub vs REALIZED tape cost:

| | FLAT (3.0bps) | REALIZED (tape) |
|---|---:|---:|
| median half-spread | 3.00 bps | **8.39 bps** |
| headline-10% $ | +158,130 | **+123,579** (−22%) |
| headline-10% Sharpe | +31.45 | **+18.86** |

| cut | FLAT $ | REALIZED $ | Δ |
|----:|-------:|-----------:|--:|
| 2% | +282,560 | +207,299 | **−75,260** |
| 5% | +276,680 | +212,899 | **−63,782** |
| 10% | +148,400 | +90,080 | **−58,320** |

The realized median half-spread is **2.8x** the flat stub; every conservative cut's $ shrinks (−$58k to
−$75k); the headline takes a **−22% haircut**. The effect is one-directional: measured cost is higher than
the stub for the liquid universe at this 09:40 entry, so every $-verdict was OPTIMISTIC and accurate cost
makes results MORE-null — exactly as predicted. This RETROACTIVELY sharpens every past verdict (the 3
path-structure incremental nulls were judged on a baseline that now pays true cost on BOTH arms, so they
remain null, more so) and every future $-gate.

## Notes
- The strong trusted baseline is still net-positive under realized cost — it is the reference model, not a
  marginal signal. The value here is that INCREMENTAL candidates are now judged against a cost-accurate
  baseline, and the headline $ numbers are honest.
- Window = 5 min trailing, time-weighted; entry-instant cost (what a market order crossing the spread pays).
  A future refinement could add a size/impact term, but the half-spread is the dominant, measurable piece.
- Portability: this is a cost-INPUT change (data feeding `long_short_per_name_cost`), NOT a decide-core
  change — zero impact on the strategy logic or the live decision path. Fingerprint-neutral.

## Stage 2 (separate, pre-register first)
For LIVE/forward use, realized cost is unknown at decision time → the PREDICTED cost model (the G0b GBM on
quote proxies, OOS R²=0.575 / IC=0.902 / 59% MAE cut) is needed. That is a feature+model pipeline with
point-in-time / parity / versioning concerns → pre-register and gate-read before building.
