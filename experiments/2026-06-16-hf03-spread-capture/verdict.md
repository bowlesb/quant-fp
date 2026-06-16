# HF03 — Verdict: KILL (adverse selection + queue position eat the spread)

**The script's mechanical auto-verdict printed "KEEP-AS-LEAD" — that is WRONG and is exactly the
optimistic-fill artifact the pre-registration committed against. The honest verdict is KILL.**

## Why the auto-KEEP is an artifact
The mechanical rule fired because the per-fill mark-out-net under the **trade_through** fill model is
positive with a razor-thin CI (fill-time-anchored OOS +30s baseline = +0.676 [0.665, 0.688] bps; qimb
thr=0.1 = +0.685 [0.673, 0.698]). But the headline BUNDLES the earned half-spread (~0.87 bps at the fill
instant) with the adverse drift, and the +CI lives ENTIRELY on the most optimistic fill assumption. The
pre-registration is explicit that "touched/traded-through => filled" is an upper bound and a positive result
there alone is AMBIGUOUS at best, not a KEEP.

## The three things that kill it
1. **Stricter fill model flips it negative.** Under the queue_proxy fill (printed size through the level must
   exceed the resting queue — still optimistic, no real queue position), OOS +30s mark-out-net is
   **−0.067 bps** (baseline) and **−0.077 bps** (qimb thr=0.1); the earned spread itself drops to ~0.65 bps
   while adverse drift grows to −0.71 bps. Real queue position is worse than this proxy, so the true number
   is more negative. The edge does not survive any honest fill model.
2. **Real exit is always negative.** Paying the half-spread to flatten (`cross`) is negative in EVERY cell:
   −0.31 → −0.35 bps (trade_through) and **−1.05 bps** (queue_proxy +30s). The strategy is positive only
   under the passive-mid-exit fantasy (always exit at mid, never cross), which is itself optimistic.
3. **qimb fails the canary at the decisive horizons.** At +30s and +60s, the qimb overlay does NOT beat a
   within-day shuffle of qimb at ANY threshold — random posting is statistically as good, and the overlay's
   mark-out-net is within ~0.01 bps of baseline. qimb does not reduce adverse selection; its micro-edge over
   baseline is fill-selection noise. The pre-registered KEEP requirement ("qimb overlay BEATS the no-overlay
   baseline" surviving the canary) FAILS.

## Adverse-selection signature (textbook)
Win rate decays 0.66 (+1s) → 0.53 (+60s); adverse drift given back grows 0.16 → 0.20 bps (trade_through) and
0.31 → 0.72 bps (queue_proxy) monotonically with the hold. We are filled preferentially right before the mid
moves against us — exactly the mechanism that dominates naive retail market-making.

## Conclusion
Naive passive liquidity provision on megacaps is dominated by adverse selection and (modeled-away) queue
position — the standard result. The earned half-spread is real but is given back to informed flow and, once
you require an honest fill model OR an honest exit, the per-fill net is ≤ 0 OOS. qimb does not rescue it
(fails the canary at the horizons that matter). **KILL.**

This closes the qimb/microstructure family for cycle 2: HF01 (taker-directional) KILL, HF02 (low-turnover
taker) KILL, HF03 (maker) KILL. The honest cycle-2 conclusion stands: the qimb microstructure signal is
real but neither **takeable** (too faint vs the spread) nor **makeable** (adverse selection + no queue
edge) at our latency/queue position. An HF-liquid edge needs either genuine queue-position/latency infra
(which we do not have) or a different liquid signal class (menu #2 fundamentals content / #3 ETF flow —
Ben's data-acquisition call).

## Honesty note
This was a MODELED backtest. The positive trade_through numbers are an optimistic upper bound and must not
be read as an edge. The decisive cells are queue_proxy + cross-exit (negative) and the canary (overlay does
not beat random). Verdict set by judgment over the mechanical rule, per the pre-registration's caveat.
