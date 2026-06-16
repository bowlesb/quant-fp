# Results — REAL numbers (single live RTH session 2026-06-15, deduped)

Script: `/tmp/probe_h1b.py`, run via `docker exec feature-computer python /tmp/probe_h1b.py`.
Signal = `vwap_deviation_30m`(t); fwd_ret = `ret_Hm`(t+H); liquidity = `dollar_volume_1m`(t).
Median ~2,866–2,898 unique symbols per sampled cross-section.

## H = 15 min forward
```
RTH minutes total=364 sampled=73 horizon=15m
median symbols/cross-section = 2866
POOLED         n_min= 60 mean_IC=-0.02793 std=0.0800 t=-2.70
HIGH-LIQ       n_min= 60 mean_IC=-0.01095 std=0.0943 t=-0.90
LOW-LIQ        n_min= 60 mean_IC=-0.04394 std=0.0775 t=-4.39
CANARY(shuf)   n_min= 60 mean_IC=-0.00744 std=0.0318 t=-1.81

illiquid/liquid |IC| ratio = 4.012   (>2 => illiquid-stronger => AGAINST H1)
high-liq |IC|=0.01095  low-liq |IC|=0.04394
```

## H = 5 min forward
```
RTH minutes total=364 sampled=73 horizon=5m
median symbols/cross-section = 2898
POOLED         n_min= 68 mean_IC=-0.04846 std=0.0780 t=-5.13
HIGH-LIQ       n_min= 68 mean_IC=-0.03152 std=0.1048 t=-2.48
LOW-LIQ        n_min= 68 mean_IC=-0.06490 std=0.0702 t=-7.62
CANARY(shuf)   n_min= 68 mean_IC=-0.00038 std=0.0230 t=-0.14

illiquid/liquid |IC| ratio = 2.059   (>2 => illiquid-stronger => AGAINST H1)
high-liq |IC|=0.03152  low-liq |IC|=0.06490
```

## Leakage canary stability (H=15, 10 shuffle seeds)
```
H=15 canary across 10 seeds: mean=0.00052 std=0.00274 range=[-0.00290,0.00512]
```
The single-seed H=15 canary (−0.0074) was one noisy draw; averaged over 10 seeds the canary is +0.0005
(≈0). H=5 single-seed canary was already −0.0004. **Canary clean — no leakage; the real IC is genuine.**

## Headline numbers
| horizon | pooled IC | pooled t | high-liq |IC| | low-liq |IC| | illiq/liq ratio |
|---------|-----------|----------|---------------|--------------|-----------------|
| 5 min   | −0.0485   | −5.1     | 0.0315        | 0.0649       | **2.06×**       |
| 15 min  | −0.0279   | −2.7     | 0.0110        | 0.0439       | **4.01×**       |

## Caveats / integrity notes
- **Pre-dedup spurious run** (kept for honesty): without `.unique(subset=["symbol"])`, SPY/QQQ/IWM
  replicated across 8 shards created a small cartesian blowup (median ~4,399 rows) and a high-variance,
  sign-flipped HIGH-LIQ IC (mean +0.032, std 0.45). Deduping removed the artifact; all headline numbers
  above are deduped. Lesson noted for any PR: index ETFs are shard-replicated in the live store.
- Single Monday session — NOISY. Per-minute IC std ~0.08 means individual minutes swing widely; the
  pooled t-stats come from n=60–68 minutes of ONE day, not independent days. **Not** a multi-day result.
