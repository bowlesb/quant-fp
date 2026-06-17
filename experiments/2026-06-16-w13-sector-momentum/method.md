# W13 — Sector momentum via the 11 SPDR sector ETFs — METHOD

**Run:** 2026-06-16. Read-only over `/store` (RO sandbox mount). Code: `run_w13.py`. No production code touched;
no live container exec. All compute via `MEM=8g CPUS=4 ops/sandbox.sh`.

## Data
- 11 SPDR sector ETFs (XLK XLF XLE XLV XLI XLY XLP XLU XLB XLRE XLC) + SPY, daily bars at
  `/store/raw/bars/symbol=<S>/date=<D>/data.parquet`, 378 trading days (2024-12-11 .. 2026-06-16).
- **Daily close panel**: for each (symbol, day) the close is the close of the **last RTH bar**, RTH =
  09:30–16:00 ET. RTH is selected **DST-safe** by converting `ts` (genuine UTC) to `America/New_York` and
  filtering on minute-of-day in [570, 960].
  - **Time pitfall fixed:** `dt.hour()` returns `i8`; `hour*60` overflows silently in polars (15*60=900
    wraps), which silently emptied the RTH filter. Fixed by casting hour/minute to `Int32` before the
    minute-of-day arithmetic. Verified RTH non-empty and the volume profile peaks in the ET 14:30–16:00
    window across both winter (EST) and summer (EDT) sample days.
  - Panel aligned on the intersection of dates where all 12 symbols are present → 378 aligned days.

## Signal & portfolio
- Formation F ∈ {21, 63, 126} trading days (≈1/3/6 months). Trailing simple return `close[t]/close[t-F]-1`.
- Rebalance every HOLD=21 trading days, **non-overlapping** (each rebalance's 21-day forward window does not
  overlap the next), starting once F days of history exist. → n_rebalances = 16 / 14 / 11 for F=21/63/126.
- **Cross-sectional L/S:** rank the 11 sectors by trailing return; long the top-3, short the bottom-3,
  equal-weight each leg; portfolio return = mean(long fwd) − mean(short fwd) over the next 21 days.
- **Time-series / absolute:** long every sector with positive trailing return, short every negative;
  equal-weight within each side; an empty side contributes 0.

## Cost
- ETFs are **not** in `/store/raw/quotes`, so per the pre-registration we use a fixed **0.4 bps round-trip
  spread** (conservative for top-liquid SPDR sector ETFs, true spread ~0.1–0.4 bps) × turnover.
- Turnover = fraction of legs (across both sides) that change vs the prior rebalance, normalized by book
  size. Realized average cost is ≤0.63 bps per rebalance — trivial by design.

## Significance gates
1. **Per-rebalance IID bootstrap** (10k resamples) → 95% CI of the mean net return.
2. **Moving-block bootstrap** (block=3, 10k) of the rebalance series → 95% CI robust to serial dependence
   (the better significance test given the coarse 11-instrument cross-section).
3. **Walk-forward OOS:** split the chronological rebalance series first-half (IS) / second-half (OOS);
   report OOS net + both CIs. Decisive gate = OOS net > 0 with CI > 0.
4. **Shuffle canary:** permute the sector→forward-return mapping each rebalance (2000 perms) and recompute
   the L/S mean; p = P(|perm mean| ≥ |real mean|). Coarse with 11 sectors, hence gate #2 is co-primary.

## Honest power note
Only 11 instruments and 11–16 non-overlapping monthly rebalances on 18 months → wide CIs by construction.
The friction is trivial, so the question is purely whether the momentum signal is real and statistically
distinguishable from zero at this n.
