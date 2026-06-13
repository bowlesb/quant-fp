# Feature Catalog (generated — do not edit by hand; run `make feature-catalog`)

6 features across 2 group(s).

| feature | group | type | dtype | nan_policy | valid_range | description |
|---|---|---|---|---|---|---|
| `ret_1m` | price_returns | price | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 1 minute(s), point-in-time as of the minute open; spans all sessions. |
| `ret_30m` | price_returns | price | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 30 minute(s), point-in-time as of the minute open; spans all sessions. |
| `ret_5m` | price_returns | price | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 5 minute(s), point-in-time as of the minute open; spans all sessions. |
| `signed_volume_1m` | trade_flow | trade_flow | Float64 | none | None | Buy-minus-sell signed share volume over the last minute (tick-rule signed). |
| `trade_freq_1m` | trade_flow | trade_flow | Float64 | none | (0.0, 10000000.0) | Number of trades printed in the last minute (raw trade frequency). |
| `trade_rate_accel_1m` | trade_flow | trade_flow | Float64 | warmup | None | Change in trades-per-second versus the prior minute (trade-rate acceleration). |
