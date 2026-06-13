# Feature Catalog (generated — do not edit by hand; run `make feature-catalog`)

199 features across 11 group(s).

| feature | group | type | layer | parity | dtype | nan_policy | valid_range | description |
|---|---|---|---|---|---|---|---|---|
| `day_of_week` | calendar | calendar | A | tolerance | Float64 | none | (1.0, 7.0) | ISO weekday of the bar in ET (Monday=1 .. Sunday=7). |
| `is_regular_session` | calendar | calendar | A | tolerance | Float64 | none | (0.0, 1.0) | 1.0 if within the 09:30-16:00 ET regular session, else 0.0 (extended hours). |
| `minute_of_day_et` | calendar | calendar | A | tolerance | Float64 | none | (0.0, 1440.0) | Minutes since ET midnight for this bar (0-1439); encodes time of day. |
| `minutes_since_open` | calendar | calendar | A | tolerance | Float64 | none | (-570.0, 870.0) | Minutes since the 09:30 ET regular open (negative during pre-market). |
| `active_seconds_1m` | microstructure_burst | microstructure | C | tolerance | Float64 | none | (0.0, 60.0) | Count of distinct seconds within the minute that had at least one trade (0-60). |
| `inter_arrival_cv_1m` | microstructure_burst | microstructure | C | distributional | Float64 | sparse | (0.0, None) | Coefficient of variation of inter-trade gaps in the minute (burstiness of arrivals). |
| `max_runup_1m` | microstructure_burst | microstructure | C | tolerance | Float64 | none | (0.0, None) | Largest within-minute price run-up: max over trades (in exchange-timestamp order) of price minus the running minimum. A PATH-DEPENDENT pattern feature. |
| `peak_trades_per_second_1m` | microstructure_burst | microstructure | C | tolerance | Float64 | none | (0.0, 10000000.0) | Maximum trades printed in any single second within the minute (peak burst intensity). |
| `mean_abs_ret_10m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 10 minutes (choppiness). |
| `mean_abs_ret_120m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 120 minutes (choppiness). |
| `mean_abs_ret_15m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 15 minutes (choppiness). |
| `mean_abs_ret_20m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 20 minutes (choppiness). |
| `mean_abs_ret_30m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 30 minutes (choppiness). |
| `mean_abs_ret_3m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 3 minutes (choppiness). |
| `mean_abs_ret_45m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 45 minutes (choppiness). |
| `mean_abs_ret_5m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 5 minutes (choppiness). |
| `mean_abs_ret_60m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 60 minutes (choppiness). |
| `up_ratio_10m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 10 minutes with a positive one-minute return (0-1). |
| `up_ratio_120m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 120 minutes with a positive one-minute return (0-1). |
| `up_ratio_15m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 15 minutes with a positive one-minute return (0-1). |
| `up_ratio_20m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 20 minutes with a positive one-minute return (0-1). |
| `up_ratio_30m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 30 minutes with a positive one-minute return (0-1). |
| `up_ratio_3m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 3 minutes with a positive one-minute return (0-1). |
| `up_ratio_45m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 45 minutes with a positive one-minute return (0-1). |
| `up_ratio_5m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 5 minutes with a positive one-minute return (0-1). |
| `up_ratio_60m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 60 minutes with a positive one-minute return (0-1). |
| `daily_return_10d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 10 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_120d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 120 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_15d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 15 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_180d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 180 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_1d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 1 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_20d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 20 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_240d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 240 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_25d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 25 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_2d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 2 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_30d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 30 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_3d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 3 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_40d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 40 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_4d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 4 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_50d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 50 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_5d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 5 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_60d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 60 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_7d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 7 completed trading day(s), point-in-time as of the prior close. |
| `daily_return_90d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 20.0) | Return over the last 90 completed trading day(s), point-in-time as of the prior close. |
| `daily_vol_10d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of daily returns over the last 10 completed trading days (point-in-time). |
| `daily_vol_20d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of daily returns over the last 20 completed trading days (point-in-time). |
| `daily_vol_30d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of daily returns over the last 30 completed trading days (point-in-time). |
| `daily_vol_5d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of daily returns over the last 5 completed trading days (point-in-time). |
| `daily_vol_60d` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of daily returns over the last 60 completed trading days (point-in-time). |
| `dist_from_10d_high` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Prior close relative to its 10-day high (close[D-1]/max - 1), point-in-time; <= 0. |
| `dist_from_120d_high` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Prior close relative to its 120-day high (close[D-1]/max - 1), point-in-time; <= 0. |
| `dist_from_20d_high` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Prior close relative to its 20-day high (close[D-1]/max - 1), point-in-time; <= 0. |
| `dist_from_250d_high` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Prior close relative to its 250-day high (close[D-1]/max - 1), point-in-time; <= 0. |
| `dist_from_60d_high` | multi_day_returns | multi_day | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Prior close relative to its 60-day high (close[D-1]/max - 1), point-in-time; <= 0. |
| `dist_from_high_10m` | price_levels | price | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Close relative to the trailing 10-minute high (close / max_high - 1); <= 0. |
| `dist_from_high_120m` | price_levels | price | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Close relative to the trailing 120-minute high (close / max_high - 1); <= 0. |
| `dist_from_high_15m` | price_levels | price | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Close relative to the trailing 15-minute high (close / max_high - 1); <= 0. |
| `dist_from_high_240m` | price_levels | price | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Close relative to the trailing 240-minute high (close / max_high - 1); <= 0. |
| `dist_from_high_30m` | price_levels | price | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Close relative to the trailing 30-minute high (close / max_high - 1); <= 0. |
| `dist_from_high_5m` | price_levels | price | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Close relative to the trailing 5-minute high (close / max_high - 1); <= 0. |
| `dist_from_high_60m` | price_levels | price | A | tolerance | Float64 | warmup | (-1.0, 0.01) | Close relative to the trailing 60-minute high (close / max_high - 1); <= 0. |
| `dist_from_low_10m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 5.0) | Close relative to the trailing 10-minute low (close / min_low - 1); >= 0. |
| `dist_from_low_120m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 5.0) | Close relative to the trailing 120-minute low (close / min_low - 1); >= 0. |
| `dist_from_low_15m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 5.0) | Close relative to the trailing 15-minute low (close / min_low - 1); >= 0. |
| `dist_from_low_240m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 5.0) | Close relative to the trailing 240-minute low (close / min_low - 1); >= 0. |
| `dist_from_low_30m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 5.0) | Close relative to the trailing 30-minute low (close / min_low - 1); >= 0. |
| `dist_from_low_5m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 5.0) | Close relative to the trailing 5-minute low (close / min_low - 1); >= 0. |
| `dist_from_low_60m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 5.0) | Close relative to the trailing 60-minute low (close / min_low - 1); >= 0. |
| `position_in_range_10m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Where close sits in its trailing 10-minute high-low range: (close - min_low) / (max_high - min_low). |
| `position_in_range_120m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Where close sits in its trailing 120-minute high-low range: (close - min_low) / (max_high - min_low). |
| `position_in_range_15m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Where close sits in its trailing 15-minute high-low range: (close - min_low) / (max_high - min_low). |
| `position_in_range_240m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Where close sits in its trailing 240-minute high-low range: (close - min_low) / (max_high - min_low). |
| `position_in_range_30m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Where close sits in its trailing 30-minute high-low range: (close - min_low) / (max_high - min_low). |
| `position_in_range_5m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Where close sits in its trailing 5-minute high-low range: (close - min_low) / (max_high - min_low). |
| `position_in_range_60m` | price_levels | price | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Where close sits in its trailing 60-minute high-low range: (close - min_low) / (max_high - min_low). |
| `log_ret_10m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-10m) over the trailing 10 minute(s), point-in-time. |
| `log_ret_120m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-120m) over the trailing 120 minute(s), point-in-time. |
| `log_ret_12m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-12m) over the trailing 12 minute(s), point-in-time. |
| `log_ret_15m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-15m) over the trailing 15 minute(s), point-in-time. |
| `log_ret_180m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-180m) over the trailing 180 minute(s), point-in-time. |
| `log_ret_1m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-1m) over the trailing 1 minute(s), point-in-time. |
| `log_ret_20m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-20m) over the trailing 20 minute(s), point-in-time. |
| `log_ret_25m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-25m) over the trailing 25 minute(s), point-in-time. |
| `log_ret_2m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-2m) over the trailing 2 minute(s), point-in-time. |
| `log_ret_30m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-30m) over the trailing 30 minute(s), point-in-time. |
| `log_ret_3m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-3m) over the trailing 3 minute(s), point-in-time. |
| `log_ret_40m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-40m) over the trailing 40 minute(s), point-in-time. |
| `log_ret_45m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-45m) over the trailing 45 minute(s), point-in-time. |
| `log_ret_4m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-4m) over the trailing 4 minute(s), point-in-time. |
| `log_ret_5m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-5m) over the trailing 5 minute(s), point-in-time. |
| `log_ret_60m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-60m) over the trailing 60 minute(s), point-in-time. |
| `log_ret_6m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-6m) over the trailing 6 minute(s), point-in-time. |
| `log_ret_7m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-7m) over the trailing 7 minute(s), point-in-time. |
| `log_ret_8m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-8m) over the trailing 8 minute(s), point-in-time. |
| `log_ret_90m` | price_returns | price | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Log close-to-close return ln(close/close_-90m) over the trailing 90 minute(s), point-in-time. |
| `ret_10m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 10 minute(s), point-in-time as of the minute open. |
| `ret_120m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 120 minute(s), point-in-time as of the minute open. |
| `ret_12m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 12 minute(s), point-in-time as of the minute open. |
| `ret_15m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 15 minute(s), point-in-time as of the minute open. |
| `ret_180m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 180 minute(s), point-in-time as of the minute open. |
| `ret_1m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 1 minute(s), point-in-time as of the minute open. |
| `ret_20m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 20 minute(s), point-in-time as of the minute open. |
| `ret_25m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 25 minute(s), point-in-time as of the minute open. |
| `ret_2m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 2 minute(s), point-in-time as of the minute open. |
| `ret_30m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 30 minute(s), point-in-time as of the minute open. |
| `ret_3m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 3 minute(s), point-in-time as of the minute open. |
| `ret_40m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 40 minute(s), point-in-time as of the minute open. |
| `ret_45m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 45 minute(s), point-in-time as of the minute open. |
| `ret_4m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 4 minute(s), point-in-time as of the minute open. |
| `ret_5m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 5 minute(s), point-in-time as of the minute open. |
| `ret_60m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 60 minute(s), point-in-time as of the minute open. |
| `ret_6m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 6 minute(s), point-in-time as of the minute open. |
| `ret_7m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 7 minute(s), point-in-time as of the minute open. |
| `ret_8m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 8 minute(s), point-in-time as of the minute open. |
| `ret_90m` | price_returns | price | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Simple close-to-close return over the trailing 90 minute(s), point-in-time as of the minute open. |
| `book_depth_1m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, None) | Mean total top-of-book size (bid_size + ask_size) over the last minute. |
| `quote_imbalance_10m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 10 minutes. |
| `quote_imbalance_15m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 15 minutes. |
| `quote_imbalance_1m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance (bid-ask)/(bid+ask) over the last minute. |
| `quote_imbalance_20m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 20 minutes. |
| `quote_imbalance_30m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 30 minutes. |
| `quote_imbalance_45m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 45 minutes. |
| `quote_imbalance_5m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 5 minutes. |
| `quote_imbalance_60m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 60 minutes. |
| `spread_bps_10m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 10 minutes. |
| `spread_bps_15m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 15 minutes. |
| `spread_bps_1m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Average top-of-book bid-ask spread in basis points over the last minute. |
| `spread_bps_20m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 20 minutes. |
| `spread_bps_30m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 30 minutes. |
| `spread_bps_45m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 45 minutes. |
| `spread_bps_5m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 5 minutes. |
| `spread_bps_60m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 60 minutes. |
| `bb_position_20m` | technical | technical | A | tolerance | Float64 | warmup | None | Position of close within its 20-minute Bollinger band: (close - sma) / (2*std). |
| `bb_width_20m` | technical | technical | A | tolerance | Float64 | warmup | (0.0, None) | Bollinger band width over 20 minutes: 4*std / sma (relative band width). |
| `macd_hist` | technical | technical | A | tolerance | Float64 | warmup | None | MACD histogram: MACD line minus the MACD signal line. |
| `macd_line` | technical | technical | A | tolerance | Float64 | warmup | None | MACD line: 12-minute EMA minus 26-minute EMA of close. |
| `macd_signal` | technical | technical | A | tolerance | Float64 | warmup | None | MACD signal line: 9-minute EMA of the MACD line. |
| `rsi_14m` | technical | technical | A | tolerance | Float64 | warmup | (0.0, 100.0) | Relative Strength Index over the trailing 14 minutes (0-100). |
| `sma_dist_100m` | technical | technical | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Close relative to its trailing 100-minute simple moving average (close/sma - 1). |
| `sma_dist_10m` | technical | technical | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Close relative to its trailing 10-minute simple moving average (close/sma - 1). |
| `sma_dist_15m` | technical | technical | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Close relative to its trailing 15-minute simple moving average (close/sma - 1). |
| `sma_dist_200m` | technical | technical | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Close relative to its trailing 200-minute simple moving average (close/sma - 1). |
| `sma_dist_20m` | technical | technical | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Close relative to its trailing 20-minute simple moving average (close/sma - 1). |
| `sma_dist_30m` | technical | technical | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Close relative to its trailing 30-minute simple moving average (close/sma - 1). |
| `sma_dist_50m` | technical | technical | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Close relative to its trailing 50-minute simple moving average (close/sma - 1). |
| `sma_dist_5m` | technical | technical | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Close relative to its trailing 5-minute simple moving average (close/sma - 1). |
| `signed_volume_10m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 10 minutes (net buy/sell pressure). |
| `signed_volume_15m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 15 minutes (net buy/sell pressure). |
| `signed_volume_1m` | trade_flow | trade_flow | B | tolerance | Float64 | none | None | Buy-minus-sell signed share volume over the last minute (tick-rule signed). |
| `signed_volume_20m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 20 minutes (net buy/sell pressure). |
| `signed_volume_30m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 30 minutes (net buy/sell pressure). |
| `signed_volume_45m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 45 minutes (net buy/sell pressure). |
| `signed_volume_5m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 5 minutes (net buy/sell pressure). |
| `signed_volume_60m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 60 minutes (net buy/sell pressure). |
| `signed_volume_90m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 90 minutes (net buy/sell pressure). |
| `trade_freq_10m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 10 minutes. |
| `trade_freq_15m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 15 minutes. |
| `trade_freq_1m` | trade_flow | trade_flow | B | tolerance | Float64 | none | (0.0, 10000000.0) | Number of trades printed in the last minute (raw trade frequency). |
| `trade_freq_20m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 20 minutes. |
| `trade_freq_30m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 30 minutes. |
| `trade_freq_45m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 45 minutes. |
| `trade_freq_5m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 5 minutes. |
| `trade_freq_60m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 60 minutes. |
| `trade_freq_90m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 90 minutes. |
| `trade_rate_accel_1m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Change in trades-per-second versus the prior minute (trade-rate acceleration). |
| `high_low_range_1m` | volatility | volatility | A | tolerance | Float64 | none | (0.0, 5.0) | Intra-minute high-low range as a fraction of close: (high - low) / close. |
| `parkinson_vol_120m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Parkinson high-low volatility estimator over the trailing 120 minutes (uses the bar range). |
| `parkinson_vol_15m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Parkinson high-low volatility estimator over the trailing 15 minutes (uses the bar range). |
| `parkinson_vol_30m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Parkinson high-low volatility estimator over the trailing 30 minutes (uses the bar range). |
| `parkinson_vol_60m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Parkinson high-low volatility estimator over the trailing 60 minutes (uses the bar range). |
| `realized_vol_10m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 10 minutes. |
| `realized_vol_120m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 120 minutes. |
| `realized_vol_15m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 15 minutes. |
| `realized_vol_20m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 20 minutes. |
| `realized_vol_30m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 30 minutes. |
| `realized_vol_3m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 3 minutes. |
| `realized_vol_45m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 45 minutes. |
| `realized_vol_5m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 5 minutes. |
| `realized_vol_60m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 60 minutes. |
| `realized_vol_90m` | volatility | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Standard deviation of one-minute close-to-close returns over the trailing 90 minutes. |
| `dollar_volume_1m` | volume | volume | A | tolerance | Float64 | none | (0.0, None) | Dollar volume traded in the last minute (close price * share volume). |
| `volume_ratio_10m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 10-minute mean. |
| `volume_ratio_120m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 120-minute mean. |
| `volume_ratio_15m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 15-minute mean. |
| `volume_ratio_20m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 20-minute mean. |
| `volume_ratio_30m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 30-minute mean. |
| `volume_ratio_3m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 3-minute mean. |
| `volume_ratio_45m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 45-minute mean. |
| `volume_ratio_5m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 5-minute mean. |
| `volume_ratio_60m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 60-minute mean. |
| `volume_zscore_10m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 10-minute mean and std. |
| `volume_zscore_120m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 120-minute mean and std. |
| `volume_zscore_15m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 15-minute mean and std. |
| `volume_zscore_20m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 20-minute mean and std. |
| `volume_zscore_30m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 30-minute mean and std. |
| `volume_zscore_3m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 3-minute mean and std. |
| `volume_zscore_45m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 45-minute mean and std. |
| `volume_zscore_5m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 5-minute mean and std. |
| `volume_zscore_60m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 60-minute mean and std. |
