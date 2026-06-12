# Data-lens report — v1.1.1 intraday features are 12-20% NaN (a verified invariant blind spot)

**Author:** explorer-data | **Date:** 2026-06-12 | **Status:** QA-VERIFIED + fix in progress; M1 verdict UNAFFECTED.

## One-line
The "0.000% NaN on all 21 features" headline was wrong: the intraday return/vol features run 12-20% NaN
panel-wide. QA reproduced it digit-for-digit, confirmed it's a real blind spot in warmup_coverage, and is
fixing both the invariant and the ledger. The M1 "no edge" verdict still stands (honest missing data).

## Evidence (full 5,525,040-row v1.1.1 panel; metric = count(vector[i]='NaN'::float8))
| feature | pct NaN (all) | pct NaN (excl 9:30 open) |
|---|---:|---:|
| ret_5m | 13.44 | 5.76 |
| ret_15m | 13.52 | 5.84 |
| ret_30m | 12.38 | 4.61 |
| ret_60m | **20.06** | **12.96** |
| vol_30m / vol_60m / vol_z_30 | 16.87 | 9.50 |
| rel_ret_30m | 12.38 | 4.61 |
| vwap_dev, range_pct, gap_from_open, calendar, mom_1d..5d | 0.00 | 0.00 |

## Two mechanisms (both honest missing data, not a broken pipeline)
1. **9:30 ET open = 100% NaN for every return feature** — no lookback at the open (450,208 rows, the first
   cadence of every day). Correct by construction.
2. **Mid-session 5-13% NaN = missing N-min-lagged bars in THIN names** — concentrated in high-nominal-price
   thin-trade S&P members (NVR 60.7%, LFUS 50.6%, GWW 48%, CW 47%, TPL/TDY/MUSA ~45%). ret is NaN when the
   lagged minute had zero trades (an undefined return = the correct np.nan case).

## Consumption path
quantlib.research.load_panel (line 48) maps None → math.nan into X; LightGBM handles NaN natively (learned
default split direction). Rows are NEITHER dropped NOR imputed — the M1 battery IC was computed including
these rows. "No edge" is not a verdict a degraded feature subset fabricates, so the verdict holds.

## QA resolution (verified by qa, 2026-06-12)
- warmup_coverage DOES scan the 'NaN'::float8 sentinel — no sentinel mismatch. Its blind spot is the
  FAILURE CONDITIONS: it only fails on ragged (early−late gap >20pp) or dead (≥95% NaN). A STEADY 13-20%
  NaN is neither, and it only scanned boundary dates, not mid-panel. Both gaps real → I4 silent-NaN-degrade
  fell through. QA fix: a steady-state per-feature NaN-rate check that excludes by-construction warmup
  cadences (so it won't false-positive the open) but flags residual mid-session NaN above a bound.
- The "0.000% NaN" headline counted NULL/whole-vector-missing, not the in-vector sentinel → QA correcting it.

## A SECOND, distinct NaN class found (handed to QA for the new check's threshold)
The v1.2.0 OFI panel (task #9) has mom_3d/5d/10d + their _rel = 100% NaN AND mom_1d/mom_1d_rel = 60% NaN —
a daily-bar JOIN FAILURE in the OFI panel builder, NOT warmup or thin-name. This is the first "not
open/thin-name explained" NaN and a genuine build bug; it's on the panel the M2 OFI pilot will run on, so
it changes the pilot's price baseline (price momentum dead). Reported to the Lead (#9) and QA.

## So what
The data-integrity habit caught a real invariant blind spot before it could silently mis-trust a future
verdict — the system catching itself. The verdict survives because the NaN is honest missing data taken
natively. The durable win is QA's steady-state NaN check, which will fail-loud on the NEXT pipeline degrade
(the v1.2.0 momentum bug is its first live target).
