# HF03 — Results (MODELED maker backtest; optimistic-fill caveat is load-bearing)

**Panel:** 853,551 posts (10s grid), 63 dates, 9 megacaps (MSFT/AAPL/TSLA/AVGO/AMD/NVDA/SPY/META/QQQ;
AMZN/GOOGL/NFLX dropped at 20<21 valid days). OOS = last 32 dates (543,551 posts). Fills counted on both
the bid and the ask side, so n_fills ~ up to ~2x n_posts. Half-spread earned is small: per-name 0.14
(SPY) → 2.53 (AMD) bps; fill-weighted ~1.05 bps.

**Per-fill mark-out-net = (mid_at_horizon − fill_price)/post_mid in bps — already nets the earned
half-spread against the post-fill adverse mid drift. The `cross` column additionally pays the half-spread
to flatten at the horizon (the honest exit). All CIs are per-fill bootstraps (2000 resamples; CI of a mean
at n>1e5 is set by the SE and stable at 2000).**

## HEADLINE — OOS, per-fill mark-out-net (bps), baseline vs best qimb overlay

| fill model | overlay | +5s net [95% CI] | +30s net [95% CI] | +60s net [95% CI] | n_fills(+30s) | win@30s |
|---|---|---|---|---|---|---|
| **trade_through** (optimistic UB) | baseline | 0.902 [0.895, 0.908] | **0.785 [0.769, 0.800]** | 0.781 [0.759, 0.805] | 938,098 | 0.548 |
| **trade_through** | qimb thr=0.1 | 0.929 [0.922, 0.936] | **0.801 [0.784, 0.818]** | 0.793 [0.769, 0.817] | 834,986 | 0.548 |
| **queue_proxy** (stricter fill) | baseline | — | **−0.052** (mark-out-net) | — | 935,085 | — |

## The decomposition that tells the truth (OOS baseline, trade_through)

| horizon | earned half-spread | adverse drift given back | mark-out-net | win rate |
|---|---|---|---|---|
| +1s  | 1.052 bps | 0.072 | 0.981 | 0.807 |
| +5s  | 1.052 bps | 0.244 | 0.808 | 0.645 |
| +30s | 1.052 bps | 0.352 | 0.700 | 0.545 |
| +60s | 1.052 bps | 0.356 | 0.697 | 0.531 |

Adverse selection is REAL and monotone in the horizon (drift-lost 0.07 → 0.36 bps; win rate 0.81 → 0.53,
decaying toward a coin flip). The earned spread (~1.05 bps) is larger than the adverse drift **only because
the trade_through fill model assumes we are filled whenever the level merely traded through** (fill rate
0.75–0.93). That is the optimistic upper bound the pre-registration forbids treating as real.

## Why the positive headline is an ARTIFACT (the two pre-committed lies, checked)

1. **Fill model.** Under the stricter **queue_proxy** fill (printed size through the level must exceed the
   resting queue at post — still optimistic, no true queue position), the OOS +30s baseline mark-out-net is
   **NEGATIVE: −0.052 bps**, and the cross-exit is **−1.05 bps**. The entire positive headline lives on the
   most optimistic fill assumption. A real passive order behind a deep queue is filled even less often, and
   preferentially on the adverse side — pushing this further negative.
2. **Exit.** The `cross_mean_bps` column (pay the half-spread to actually flatten) is **NEGATIVE in EVERY
   cell** — trade_through baseline: −0.076 (+1s) → −0.380 (+60s); queue_proxy +30s: **−1.05**. The only way
   the strategy prints positive is the passive-mid-exit assumption (mark at mid, never pay to get out),
   which is itself optimistic (you cannot reliably exit passively at the mid on demand).

## qimb overlay vs no-overlay baseline + CANARY (does qimb actually reduce adverse selection?)

The overlay's mark-out-net is within ~0.01–0.02 bps of baseline — qimb barely moves it. The canary (shuffle
qimb within (symbol,date), 10 seeds) shows the overlay does **NOT** beat random posting at the decisive
horizons:

| horizon | any threshold beats canary? |
|---|---|
| +1s  | only thr=0.1, 0.2 (marginally: 1.122 vs shuf-hi 1.120) |
| +5s  | only thr=0.2 (marginally) |
| **+30s** | **NONE** (real ≤ shuffled 97.5pct at every threshold) |
| **+60s** | **NONE** |

At the horizons that matter for a real hold, qimb-conditioned quoting is statistically indistinguishable
from random posting. The faint +1s "win" is fill-selection noise, not adverse-selection avoidance.

## Adverse-selection magnitude (headline number)
Mean mark-out loss (earned spread minus realized net) grows **0.07 bps (+1s) → 0.36 bps (+60s)** under the
optimistic fill; under queue_proxy it fully consumes the earned spread by +30s (net flips negative).

## Caveat (binding)
Every positive number here is a MODELED upper bound under (a) trade-through fills and (b) passive-mid exits.
This is NOT a live-fill measurement and cannot be validated on Alpaca paper (no queue position). The honest
reading is the queue_proxy + cross-exit cells, which are negative.
