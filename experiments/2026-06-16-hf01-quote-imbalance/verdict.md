# HF01 Verdict: KILL (on the pre-committed turnover-compounded cost gate)

**An honest near-miss, not a failure.** qimb is a faint REAL slow signal that canary-passes — but turnover
eats it, exactly the adversary pre-registered as the binding constraint for the HF regime.

## Against the pre-registered gates

| gate | result |
|---|---|
| Shuffle canary (FIRST) | **qimb PASSES** all 9 (w×h) cells; OFI mixed; stflow noise/wrong-sign |
| Standalone IC | qimb IC GROWS with horizon — best **qimb/120/5: IC=0.0128** (overall) / mean-daily 0.0109 |
| Standalone t (day-clustered, n=63) | **maxes at 1.62 (<2)** — faint, never strong |
| Per-symbol demean | **BUG — all NaN** (see below); inconclusive |
| Walk-forward OOS | **BUG — all NaN** (n_oos=322,471 but ic_oos=NaN); inconclusive |
| **Turnover-compounded cost gate (DECISIVE)** | **FAIL** — best cell qimb/120/5/band0: gross +0.45 bps, turnover 0.15, **net +0.04 bps @1× cost, −0.37 bps @2×**. Of 162 cells, 9 net-positive @1× (all ≤+0.04 bps, near-zero), only 2 @2× (degenerate zero-turnover cells). NO cell meaningfully nets positive after turnover-compounded cost; none survives the 2× stress. |

## Per-signal read

- **qimb (book imbalance):** the only signal with a real, canary-clearing, sign-correct (positive →
  positive next-mid-return = price-pressure continuation) IC, and it strengthens at longer horizons (5 min >
  1 min) — a genuine SLOW signal. But IC ~0.01 / t ~1.6 is too faint, and at the measured spread (AAPL 0.48,
  MSFT 0.83, TSLA 1.07, AVGO 1.83, AMD 2.48 bps) the turnover-compounded round-trip (~2.7 bps avg) dwarfs the
  ~0.45 bps gross. KILL on cost.
- **ofi (CKS order-flow):** noise at this short horizon (negative mean-daily IC, several canary-fail) — same
  as cycle-1 H2's daily-cross-section kill, now confirmed dead intraday too.
- **stflow (signed trade flow):** noise / wrong-sign.

## The bug to fix (flagged, then fixed in HF02 infra)

`oos_results.csv` and `demean_results.csv` are ALL NaN — the OOS-split and per-symbol-demean stages errored
silently (the row count n_oos=322,471 is populated but every IC is NaN). The COST GATE independently and
decisively kills HF01, so this verdict STANDS regardless. BUT a silent all-NaN gate is exactly the failure
mode that could HIDE a real signal in a future HF hypothesis — so the pipeline bug is fixed before HF02 runs
(see HF02 method). OOS/demean here are **inconclusive-due-to-bug, cost-gate-decisive**.

## Power / scope (honest)

Panel was 5 symbols (MSFT, AVGO, AMD, TSLA, AAPL — the deepest-quote names), 63 days, 551,738 obs. Adequate
for the cost-gate conclusion (the gross signal is simply too small vs the spread), modest for a strong IC
claim. The other ~7 ≥21-day megacaps were not included (memory scope) — but the cost-gate kill is robust to
panel size (it's gross-vs-spread, not power-limited).

## Decision

**KILL.** qimb is a real but faint slow book-pressure signal that does not survive turnover-compounded cost
at the measured megacap spread. OFI/stflow are noise. The pre-registered cost gate did exactly its job:
caught a faint real IC that is not tradeable once you pay the spread on every short-horizon rebalance.

## The one thread worth a follow-up → HF02

qimb's IC GROWS with horizon (0.0033 @1m → 0.0128 @5m) and the signal is SLOW — so the natural question:
does a MUCH LOWER-TURNOVER construction (a longer hold of 15–30+ min, a wider no-trade band, or trading qimb
only on strong PERSISTENCE) ever let the faint-but-real signal clear the cost gate? At 5-min the turnover was
already cut to 0.15 with band0 and still only broke even; pushing the hold to 15–30 min could amortize the
~2.7 bps round-trip over a larger move. Pre-registered as HF02. (And HF02 fixes the OOS/demean bug so the
hold-out is valid this time.)
