# H3 Verdict

## Pre-registered kill condition

> KILL if NO book-state cell beats flat vwap_dev net-of-cost beyond canary.

## Result: **KILL**

No book-state conditioner produced a cell where the vwap_dev L/S net-of-cost gross exceeded:
1. Zero (all cells negative net), and
2. Its own canary ceiling.

### Key numbers

| Comparison | H15 net (bps) |
|------------|--------------|
| Flat vwap_dev | −10.45 |
| Best cell: spread=tight | −2.32 |
| Best cell canary max | −2.36 |
| Best cell spread cost | 2.70 |

The best outcome — tight-spread tercile — shows that conditioning dramatically lowers the cost wall (2.70 bps vs 11.24 bps flat) but the gross signal in that regime (0.38 bps) is too weak to clear even this reduced cost. Worse, the signal net (−2.32 bps) is within 0.04 bps of the canary max (−2.36 bps), meaning it is statistically indistinguishable from noise at our power level.

### Depth and size_imbalance

Neither depth terciles nor size_imbalance terciles show meaningful conditioning: all cells produce gross bps < 2 bps against spread costs of 9–13 bps. No monotonic improvement pattern in the hypothesized direction (thin→deep lifting reversion).

### Caveats (honest)

- **20 days is low power**: ~5,800 cross-sections flat, ~1,900 per tercile. The tight-spread near-miss (−2.32 vs −2.36 canary) could look different over 60+ days, but the effect size is too small to trade on.
- **Spread cost model**: using `rel_spread_mean` as the round-trip cost anchor is approximate (actual fill cost depends on aggressor-side fill quality). The tight-spread bucket is still genuine — stocks in that bucket are genuinely cheaper to trade.
- **Point-in-time quotes**: `size_imbalance` computed from all quotes in the minute, not just the entry tick. This is a fair point-in-time estimate but includes the full minute's book state, not just the entry state.

## Next step

Book depth and spread state do NOT condition vwap_dev reversion into a tradeable cell at this signal strength and horizon. The vwap_dev signal itself is too weak (~0.8 bps gross) for any conditioning to rescue it against market frictions.

**Recommended next step**: close the microstructure conditioning branch (H1–H3 all KILLED). Pivot to either (a) a longer-horizon reversion test where round-trip spread cost is a smaller fraction of gross (H60+ min), or (b) a fundamentally different signal source (earnings surprise, flow imbalance at longer aggregation, or cross-asset momentum).
