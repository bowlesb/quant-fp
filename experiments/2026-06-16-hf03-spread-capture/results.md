# HF03 — Results (MODELED maker backtest; optimistic-fill caveat is load-bearing)

**Panel:** 12 deep-quote megacaps (MSFT/AAPL/TSLA/AVGO/AMD/NVDA/SPY/AMZN/META/GOOGL/QQQ/NFLX), ~62–63
dates each, 10s posting grid. OOS = last 32 dates, **849,000 posts**. Fills counted on both the bid and the
ask side (n_fills up to ~2× n_posts). Per-name half-spread 0.13 (SPY) → 2.51 (AMD) bps.

**Mark-out is anchored at the ACTUAL fill timestamp** (first trade-through), with the earned spread measured
against the mid AT the fill instant (`fill_mid − post_bid`), not the stale post-time mid. Per-fill
mark-out-net = (mid_at_horizon − fill_price)/post_mid in bps = earned_spread + adverse_drift. The `cross`
column additionally pays the half-spread to flatten at the horizon (the honest exit). CIs are analytic
1.96·SE per fill (n>1e6 ⇒ identical conclusion to a per-fill bootstrap). Source: `results.csv`.

## HEADLINE — OOS, per-fill mark-out-net (bps), baseline vs best qimb overlay

| fill model | overlay | +5s net [95% CI] | +30s net [95% CI] | +60s net [95% CI] | n_fills(+30s) | win@30s |
|---|---|---|---|---|---|---|
| **trade_through** (optimistic UB) | baseline | 0.681 [0.676, 0.687] | **0.676 [0.665, 0.688]** | 0.674 [0.658, 0.690] | 1,460,321 | 0.543 |
| **trade_through** | qimb thr=0.1 | 0.696 [0.691, 0.702] | **0.685 [0.673, 0.698]** | 0.678 [0.660, 0.695] | 1,303,762 | 0.543 |
| **queue_proxy** (stricter fill) | baseline | 0.051 [0.045, 0.057] | **−0.067 [−0.080, −0.053]** | −0.079 [−0.098, −0.060] | 1,107,017 | 0.484 |
| **queue_proxy** | qimb thr=0.1 | 0.051 [0.045, 0.057] | **−0.077 [−0.091, −0.062]** | −0.096 [−0.116, −0.075] | 989,374 | 0.484 |

## The decomposition that tells the truth (OOS baseline, trade_through, fill-time-anchored)

| horizon | earned spread | adverse drift given back | mark-out-net | win rate |
|---|---|---|---|---|
| +1s  | 0.872 bps | −0.157 | 0.715 | 0.658 |
| +5s  | 0.872 bps | −0.190 | 0.681 | 0.601 |
| +30s | 0.872 bps | −0.196 | 0.676 | 0.543 |
| +60s | 0.872 bps | −0.198 | 0.674 | 0.530 |

Adverse selection is REAL and monotone (drift-lost 0.16 → 0.20 bps; win rate 0.66 → 0.53, decaying toward a
coin flip). The earned spread (~0.87 bps at the fill instant — lower than the naive ~1.05 bps because the
mid moves during the 10s before the fill) exceeds the adverse drift **only because the trade_through fill
model assumes we are filled whenever the level merely trades through** (fill rate 0.75–0.93). That is the
optimistic upper bound the pre-registration forbids treating as real.

## Why the positive headline is an ARTIFACT (the two pre-committed lies, checked)

1. **Fill model.** Under the stricter **queue_proxy** fill (printed size through the level must exceed the
   resting queue at post — still optimistic, no true queue position), the OOS mark-out-net goes **NEGATIVE**
   by +30s: **−0.067 bps** (baseline), **−0.077 bps** (qimb thr=0.1), and the earned spread itself drops to
   ~0.65 bps while adverse drift grows to −0.71 bps. The entire positive headline lives on the most
   optimistic fill assumption. A real passive order behind a deep queue is filled even less often, and
   preferentially on the adverse side — pushing this further negative.
2. **Exit.** The `cross_mean_bps` column (pay the half-spread to actually flatten) is **NEGATIVE in EVERY
   cell** — trade_through baseline −0.31 (+1s) → −0.35 (+60s); queue_proxy +30s **−1.05 bps**. The only way
   the strategy prints positive is the passive-mid-exit assumption (mark at mid, never pay to get out),
   which is itself optimistic — the same queue fantasy on the way out.

## qimb overlay vs no-overlay baseline + CANARY (does qimb actually reduce adverse selection?)

The overlay's mark-out-net is within ~0.01 bps of baseline (trade_through +30s: 0.685 vs 0.676) — qimb
barely moves it. The within-(symbol,date) shuffle canary from the prior (window-edge) run showed the overlay
does **NOT** beat random posting at the decisive horizons (+30s/+60s: real ≤ shuffled 97.5pct at every
threshold; the faint +1s "win" is fill-selection noise). qimb does not reduce adverse selection; the pre-
registered KEEP requirement ("qimb overlay BEATS the no-overlay baseline" surviving the canary) FAILS.

## Adverse-selection magnitude (headline number)
Mean adverse mid drift after a fill: trade_through **0.16 bps (+1s) → 0.20 bps (+60s)**; queue_proxy
**0.31 → 0.72 bps** — under the realistic fill it fully consumes the ~0.65 bps earned spread by +30s (net
flips negative).

## Caveat (binding)
Every positive number here is a MODELED upper bound under (a) trade-through fills and (b) passive-mid exits.
This is NOT a live-fill measurement and cannot be validated on Alpaca paper (no queue position). The honest
reading is the queue_proxy + cross-exit cells, which are negative.
