# H9 — Longer-horizon vwap_dev reversion (H60/H120): attack the COST FRACTION by horizon

**Registered:** 2026-06-16 (before any data run). Re-prioritized AHEAD of H6 (vol-conditioning) per the H3
verdict's own recommendation: H1–H3 proved conditioning attacks the WRONG side of the constraint. The
binding wall is gross-signal-per-trade vs a fixed ~6–11 bps round-trip cost. Conditioning can lower cost a
little but cannot manufacture signal. The OTHER lever is HORIZON: amortize the same round-trip cost over a
LARGER move.

## Hypothesis

`vwap_dev` reversion realized over a LONGER holding horizon (H60, H120 min — and a same-direction
multi-bar exit) produces a larger GROSS per-trade move, so the FIXED round-trip cost is a smaller fraction
of it. Concretely: a decile L/S on `vwap_dev_30` (or a longer-window vwap_dev) held 60–120 min has
net-of-cost gross > 0 where the 15–30 min version (H1–H3: net −2 to −10 bps) does not — because turnover
(and thus cost frequency) drops and the reversion has more time to play out.

## Test design

On the bars panel (full 126-day depth available; can extend beyond the 20-day quote panel since this needs
NO quotes): decile L/S on `vwap_dev_{30,60}` with forward returns at H ∈ {60, 120} min. Compute:
- GROSS decile L/S bps at each (signal window × horizon),
- TURNOVER (fraction of names changing leg per rebalance — longer horizon + a no-trade band should cut it),
- NET = gross − (turnover × round-trip cost), cost anchor = measured liquid-tier spread (~6 bps RT) PLUS a
  realistic slippage add-on,
- 10-seed within-CS shuffle canary; day-clustered t.
Baseline = the H15/H30 result (already net-negative). The KEEP bar is a (window×horizon) cell with net > 0
clearing canary AND robust to a ±50% cost-anchor stress.

## Prior

Mean-reversion is horizon-dependent; the per-period signal can be weak while the cumulative reversion over a
longer hold is economically meaningful, and lower turnover directly cuts the cost frequency that killed the
30 min version (the platform's own finding: vwap_dev breakeven ~1.4–1.66 bps < cost at turnover ~3.2/period
— so CUTTING turnover is the lever the platform already identified). Grinold-Kahn: net IR scales with
signal × sqrt(breadth) / cost, and cost ∝ turnover.

## Expected / confidence

- Confidence a longer-horizon cell clears net-of-cost beyond canary: ~30% — higher than the conditioners
  (which were ~25% and all failed) because it attacks the binding constraint (cost fraction) directly.
  Honest risk: reversion may DECAY past 30 min (the deviation already corrected), so the longer-horizon
  gross may not grow enough to outrun cost; and longer holds carry more overnight/regime risk.
- KEEP: a (window×horizon) cell with net > 0, clears canary, survives the cost stress. AMBIGUOUS: net ≈ 0
  within canary. KILL: every cell net < 0 beyond canary.

## Kill

KILL if NO longer-horizon cell turns vwap_dev net-of-cost positive beyond canary. Then the vwap_dev
reversion is dead at ALL tradeable horizons (15→120 min) AND under all conditioners — a definitive close on
the only canary-clearing price signal, and the hunt pivots to a LOW-TURNOVER, NON-price signal class
(event families H4/H5; or fundamentals/earnings drift) rather than any further price-reversion variant.

## Ordering

Dispatch next (after H3). Bars-only, so unblocked and cheap. If H9 KILLs, H6 (vol-conditioner) is
DE-PRIORITIZED to near-dead (same conditioner framing) and the hunt moves to event/fundamental families.
