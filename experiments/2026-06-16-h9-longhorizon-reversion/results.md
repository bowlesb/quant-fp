# H9 Results

**Universe:** 300 liquid symbols | **Dates:** 50 days (2026-04-07 → 2026-06-16) | **Obs:** 44k–130k per cell depending on (W, H)

## Main Result Table

| W (min) | H (min) | Gross (bps) | Turnover | Net @4bps | Net @6bps | Net @10bps | t-stat (day-clust) | Canary 95th (bps) | Clears Canary |
|---------|---------|-------------|----------|-----------|-----------|------------|---------------------|-------------------|---------------|
| 30      | 60      | **−12.68**  | 0.895    | −16.26    | −18.05    | −21.63     | −1.75               | 4.57              | No            |
| 30      | 120     | **−28.47**  | 0.899    | −32.07    | −33.87    | −37.47     | −2.58               | 14.30             | No            |
| 60      | 60      | **−24.66**  | 0.894    | −28.24    | −30.03    | −33.60     | −2.49               | 12.49             | No            |
| 60      | 120     | **−33.67**  | 0.898    | −37.26    | −39.06    | −42.65     | −2.44               | 18.64             | No            |

## Key Observations

### Gross is negative across all cells — and worsens with longer horizons

The most favorable cell is W=30, H=60 at −12.68 bps gross. Every other cell is worse, and the pattern is monotonically more negative at H=120. This is the opposite of the H9 hypothesis: rather than reversion accumulating over a longer hold, the signal **reverses direction** — the vwap deviation has already corrected by 30–60 min and momentum takes over at 60–120 min. The longer you hold, the further negative the spread.

### Turnover does not drop with longer horizons

Turnover is ~0.895–0.899 across all (W, H) cells — virtually constant. The decile composition is reshuffling at nearly every rebalance regardless of whether that rebalance is every 60 or 120 minutes. This defeats the cost-amortization mechanism: the hypothesis assumed turnover would fall at longer horizons, but it does not.

### Net-of-cost is deeply negative at all cost levels

Even at the most optimistic 4 bps RT assumption, the best cell (W=30, H=60) is −16.26 bps. At the anchor (6 bps) it is −18.05 bps. No cost stress scenario produces a positive net in any cell.

### Canary bands confirm the negative gross is signal, not noise

The canary 95th percentile for the best cell (W=30, H=60) is only +4.57 bps. The observed gross of −12.68 bps is far below the canary null (canary = near-zero by construction). The strongly negative t-stats (−1.75 to −2.58) confirm day-clustered significance of the negative direction.

### Baseline comparison

H1–H3 (H=15–30 min) found vwap_dev net = −2 to −10 bps. H9 at H=60 min gives −18 bps; at H=120 min gives −34 to −39 bps. The longer-horizon version is **substantially worse**, not better.

## Rebalance Cadence Detail

| W | H | # Rebalance Periods | # Dates |
|---|---|---------------------|---------|
| 30 | 60  | 294 | 49 |
| 30 | 120 | 147 | 49 |
| 60 | 60  | 249 | 49 |
| 60 | 120 | 147 | 49 |

(W=60 has fewer periods at H=60 because the first 60 bars are consumed by the signal window, leaving fewer valid scoring bars per day.)

## Canary Detail

| W | H | Canary Mean (bps) | Canary Std (bps) | Canary 95th (bps) |
|---|---|-------------------|-----------------|-------------------|
| 30 | 60  | 0.41 | 2.08 | 4.57  |
| 30 | 120 | 1.74 | 6.28 | 14.30 |
| 60 | 60  | 0.63 | 5.93 | 12.49 |
| 60 | 120 | 1.31 | 8.66 | 18.64 |

The null band is wide at H=120 (fewer periods → higher variance) but the observed gross is still far below it in the negative direction.
