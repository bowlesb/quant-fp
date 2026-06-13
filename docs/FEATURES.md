# Feature Catalog (generated — do not edit by hand; run `make feature-catalog`)

18 features across 6 group(s).

| feature | group | type | layer | parity | dtype | nan_policy | valid_range | description |
|---|---|---|---|---|---|---|---|---|
| `day_of_week` | calendar | calendar | A | tolerance | Float64 | none | (1.0, 7.0) | ISO weekday of the bar in ET (Monday=1 .. Sunday=7). |
| `is_regular_session` | calendar | calendar | A | tolerance | Float64 | none | (0.0, 1.0) | 1.0 if within the 09:30-16:00 ET regular session, else 0.0 (extended hours). |
| `minute_of_day_et` | calendar | calendar | A | tolerance | Float64 | none | (0.0, 1440.0) | Minutes since ET midnight for this bar (0-1439); encodes time of day. |
| `minutes_since_open` | calendar | calendar | A | tolerance | Float64 | none | (-570.0, 870.0) | Minutes since the 09:30 ET regular open (negative during pre-market). |
| `active_seconds_1m` | microstructure_burst | microstructure | C | tolerance | Float64 | none | (0.0, 60.0) | Count of distinct seconds within the minute that had at least one trade (0-60). |
| `inter_arrival_cv_1m` | microstructure_burst | microstructure | C | distributional | Float64 | sparse | (0.0, None) | Coefficient of variation of inter-trade gaps in the minute (burstiness of arrivals). |
| `peak_trades_per_second_1m` | microstructure_burst | microstructure | C | tolerance | Float64 | none | (0.0, 10000000.0) | Maximum trades printed in any single second within the minute (peak burst intensity). |
| `ret_1m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 1 minute(s), point-in-time as of the minute open; spans all sessions. |
| `ret_30m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 30 minute(s), point-in-time as of the minute open; spans all sessions. |
| `ret_5m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 5 minute(s), point-in-time as of the minute open; spans all sessions. |
| `book_depth_1m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, None) | Mean total top-of-book size (bid_size + ask_size) over the last minute. |
| `quote_imbalance_1m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance (bid-ask)/(bid+ask) over the last minute. |
| `spread_bps_1m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Average top-of-book bid-ask spread in basis points over the last minute. |
| `signed_volume_1m` | trade_flow | trade_flow | B | tolerance | Float64 | none | None | Buy-minus-sell signed share volume over the last minute (tick-rule signed). |
| `trade_freq_1m` | trade_flow | trade_flow | B | tolerance | Float64 | none | (0.0, 10000000.0) | Number of trades printed in the last minute (raw trade frequency). |
| `trade_rate_accel_1m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Change in trades-per-second versus the prior minute (trade-rate acceleration). |
| `high_low_range_1m` | volatility | volatility | A | tolerance | Float64 | none | (0.0, 5.0) | Intra-minute high-low range as a fraction of close: (high - low) / close. |
| `realized_vol_5m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of the last 5 one-minute close-to-close returns (realized vol). |
