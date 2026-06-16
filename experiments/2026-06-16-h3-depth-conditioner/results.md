# H3 Results

**Panel**: 1,417,038 clean rows (250 syms × 20 days, RTH, with book features joined)
**Cost model**: round-trip = 1 × rel_spread_mean (bps = × 10,000)

## Flat vwap_dev baseline (no conditioning)

| Horizon | Gross (bps) | Spread/cost (bps) | Net (bps) | N cross-sections |
|---------|-------------|-------------------|-----------|------------------|
| H15     | 0.79        | 11.24             | **−10.45** | 5,800 |
| H30     | −0.56       | 11.24             | **−11.80** | 5,800 |

**Flat canary (10 seeds)**: H15 mean=−8.20, max=−7.98 bps | H30 mean=−8.21, max=−7.72 bps

Note: flat signal gross is already inside the canary band at H30. H15 signal sits marginally above canary gross (0.79 vs ≈0) but far below cost recovery.

---

## Conditioning on SPREAD regime

| Tercile | N rows | H15 gross (bps) | H15 spread/cost (bps) | H15 net (bps) | H15 canary max (bps) | H30 net (bps) |
|---------|--------|-----------------|----------------------|---------------|----------------------|---------------|
| tight   | 471,874 | 0.38 | 2.70 | **−2.32** | **−2.36** | −3.76 |
| mid     | 473,291 | 0.73 | 6.32 | −5.59 | −6.13 | −6.49 |
| wide    | 471,873 | 1.21 | 18.75 | −17.55 | −15.22 | −19.50 |

---

## Conditioning on DEPTH regime

| Tercile | N rows | H15 gross (bps) | H15 spread/cost (bps) | H15 net (bps) | H15 canary max (bps) | H30 net (bps) |
|---------|--------|-----------------|----------------------|---------------|----------------------|---------------|
| thin    | 471,890 | 1.36 | 12.26 | −10.90 | −9.38 | −11.88 |
| mid     | 473,314 | 1.22 | 12.61 | −11.39 | −8.94 | −12.83 |
| deep    | 471,834 | 0.13 | 8.90  | **−8.77** | −5.49 | −10.18 |

---

## Conditioning on SIZE_IMBALANCE regime

| Tercile   | N rows  | H15 gross (bps) | H15 spread/cost (bps) | H15 net (bps) | H15 canary max (bps) | H30 net (bps) |
|-----------|---------|-----------------|----------------------|---------------|----------------------|---------------|
| bid-heavy | 471,874 | 0.57 | 11.82 | −11.26 | −8.45 | −12.32 |
| neutral   | 473,291 | 0.77 | 9.87  | −9.10  | −7.26 | −10.33 |
| ask-heavy | 471,873 | 1.02 | 12.01 | −10.99 | −8.23 | −12.57 |

---

## Best cell summary

**Best conditioned cell at H15**: `spread=tight` (net=−2.32 bps vs spread cost=2.70 bps vs canary max=−2.36 bps)

The tight-spread tercile reduces the cost wall dramatically (2.70 bps vs 11.24 bps flat) and raises the net from −10.45 to −2.32 bps — but the gross (0.38 bps) is BELOW the round-trip cost (2.70 bps), so net is still negative. The canary max in this same cell is −2.36 bps, meaning the real signal net (−2.32 bps) does NOT clear canary — it is statistically indistinguishable from noise.
