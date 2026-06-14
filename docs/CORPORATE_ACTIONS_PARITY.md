# Corporate actions (splits) — the decisive parity standard

> Status: STANDARD (2026-06-14). Scrutiny of divergence class **D** (splits), the failure that
> corrupted the prior system (the "KLAC class"). Grounded in `quantlib/corporate_actions.py`,
> `quantlib/features/backfill_bars.py`, and the multi-day feature groups.

## The exact hazard

Two adjustment bases coexist by design:
- **Intraday bars are `Adjustment.RAW`** (`backfill_bars.py:76`) — to match the raw, unadjusted live
  tape cell-for-cell. Live and backfill intraday agree.
- **Daily bars are `Adjustment.SPLIT`** (`backfill_bars.py:101`) — so the long daily windows
  (`daily_return_240d`, `dist_from_250d_high`, …) are computable across splits.

The multi-day groups (`multi_day`, `multi_day_vwap`, `prior_day`) combine a **raw intraday close** with
**split-adjusted daily history**. That is fine on a normal day. It breaks in exactly one place: a
**split ex-date inside the daily lookback window**. Two distinct failures there:

1. **Parity break (production).** `Adjustment.SPLIT` re-bases the *entire* returned daily history when a
   split occurs. The live `daily` snapshot, built that morning before the vendor applied the split, is
   in the OLD basis; the T+1 backfill `daily` is in the NEW basis. Same group code, different daily
   inputs → every multi-day feature for that symbol diverges live-vs-backfill until the live snapshot
   refreshes. (The parity *test* feeds one `daily` to both sides, so it does NOT surface this — only
   the stored-vs-stored ledger on real data does.)
2. **Correctness break (both paths).** On/around the ex-date, the raw intraday close and the
   split-adjusted prior-day levels are in different bases, so ratios like `close / prior_day_high` are
   nonsense for that symbol-window regardless of parity.

## The decisive standard: null-on-split, parity-safe

We do **not** try to reconcile the two bases intraday (fragile, and low value under
[breadth-over-depth](VALIDATION_LEDGER.md)). Instead:

**For any symbol with a split ex-date inside a multi-day feature's daily lookback window, that
feature's value is NULL (sparse policy) for the affected window — on BOTH paths.**

Why this is the right call:
- **Parity preserved by construction:** null == null. Both live and backfill null the same
  symbol-windows (the null is driven by the corporate-actions table, a settled artifact fed to both),
  so a split can never *manufacture* a divergence.
- **No garbage emitted:** a known mixed-basis value is replaced by an honest "no signal here", not a
  wrong number a model could learn from.
- **Cheap:** splits are rare per symbol; a handful of symbol-days lose multi-day signal around their
  split. Short-horizon breadth (the priority) is untouched.

### Integration point (the wiring)

The detector already exists: `corporate_actions.names_with_recent_ex_date(..., SPLIT_TYPES)` returns the
symbols with a recent split ex-date. The clean, no-new-required-input wiring:

1. **Enrich the `daily` snapshot** (the single source both paths consume) with a per-`(symbol, date)`
   `split_recent` boolean, computed from `names_with_recent_ex_date` over the max daily window. This is
   the ONE place to add it — `backfill_daily` and the live daily-snapshot builder produce the same
   enriched frame, so both paths inherit the same mask.
2. **Null on consume:** `multi_day`, `multi_day_vwap`, `prior_day` set their outputs to null where the
   broadcast `split_recent` is true. No new `InputSpec` (so `runnable()` group-selection is unchanged);
   the flag rides the existing required `daily` input.

This is a contained, parity-critical change to three groups + the daily builder, with a split-day
parity test (a synthetic split that re-bases daily on one side → the masked features stay null on both,
the unmasked features still agree). It is scoped as its own focused pass so each group's nulling is
verified individually.

## Why we are not exposed in the meantime

The **validation ledger is the live detector** for this class. A production split divergence (stale
live daily vs re-based backfill daily) shows up as concentrated `mismatch` cells on that symbol's
multi-day features → the feature-day value-rate drops → the feature flips `divergent` and is pulled
from the training-eligible set by the certification gate. So until the null-on-split wiring lands, a
split-corrupted feature is **caught and quarantined, not silently trusted** — which is the guarantee
that actually matters. The wiring upgrades us from "detect and quarantine" to "never emit the garbage
in the first place".
