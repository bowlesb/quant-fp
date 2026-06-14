# Feature Catalog (generated — do not edit by hand; run `make feature-catalog`)

516 features across 28 group(s).

| feature | group | type | layer | parity | dtype | nan_policy | valid_range | description |
|---|---|---|---|---|---|---|---|---|
| `is_easy_to_borrow` | asset_flags | reference | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when the symbol is on the easy-to-borrow list (cheap, available short locate), else 0.0. |
| `is_fractionable` | asset_flags | reference | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when the broker supports fractional-share trading of the symbol, else 0.0. |
| `is_marginable` | asset_flags | reference | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when the symbol is marginable (can be held on margin), else 0.0. |
| `is_shortable` | asset_flags | reference | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when the symbol can be sold short at the broker, else 0.0 (broadcast across the day). |
| `day_of_week` | calendar | calendar | A | tolerance | Float64 | none | (1.0, 7.0) | ISO weekday of the bar in ET (Monday=1 .. Sunday=7). |
| `is_regular_session` | calendar | calendar | A | tolerance | Float64 | none | (0.0, 1.0) | 1.0 if within the 09:30-16:00 ET regular session, else 0.0 (extended hours). |
| `minute_of_day_et` | calendar | calendar | A | tolerance | Float64 | none | (0.0, 1440.0) | Minutes since ET midnight for this bar (0-1439); encodes time of day. |
| `minutes_since_open` | calendar | calendar | A | tolerance | Float64 | none | (-570.0, 870.0) | Minutes since the 09:30 ET regular open (negative during pre-market). |
| `day_of_month_norm` | calendar_events | calendar | A | tolerance | Float64 | none | (0.0, 1.04) | Calendar day of month in ET divided by 31 (position through the month, 0-1). |
| `is_first_week` | calendar_events | calendar | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the ET calendar day of month is 7 or earlier (first week), else 0.0. |
| `is_last_week` | calendar_events | calendar | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the ET calendar day of month is 22 or later (last week, month-end window), else 0.0. |
| `is_opex_day` | calendar_events | calendar | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the bar is on monthly options-expiration Friday (the third Friday of the month) in ET, else 0.0. |
| `is_quarter_end_month` | calendar_events | calendar | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the bar falls in a quarter-end month (Mar/Jun/Sep/Dec) in ET, else 0.0. |
| `is_triple_witching` | calendar_events | calendar | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 on quarterly triple-witching (third Friday of March/June/September/December) in ET, else 0.0. |
| `week_of_month` | calendar_events | calendar | A | tolerance | Float64 | none | (1.0, 5.0) | Week of the month in ET (1-5), as ceil(day_of_month / 7). |
| `body_ratio` | candlestick | candlestick | A | tolerance | Float64 | none | (-0.01, 1.01) | Real-body size as a fraction of the bar's high-low range: |close-open| / (high-low); 0 when the range is zero. |
| `is_bullish` | candlestick | candlestick | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the minute closed above its open (a green/up bar), else 0.0. |
| `is_doji` | candlestick | candlestick | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the real body is under 10% of the bar range (indecision/doji), else 0.0. |
| `is_hammer` | candlestick | candlestick | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 for a hammer: long lower wick (>60% of range), tiny upper wick, small body. |
| `is_marubozu` | candlestick | candlestick | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 for a marubozu: real body fills over 90% of the bar range (almost no wicks). |
| `is_shooting_star` | candlestick | candlestick | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 for a shooting star: long upper wick (>60% of range), tiny lower wick, small body. |
| `lower_shadow_ratio` | candlestick | candlestick | A | tolerance | Float64 | none | (-0.01, 1.01) | Lower wick as a fraction of the high-low range: (min(open,close) - low) / (high-low). |
| `pattern_engulfing_bearish` | candlestick | candlestick | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when a bearish bar's body fully engulfs the prior bullish bar's body (two-candle reversal). |
| `pattern_engulfing_bullish` | candlestick | candlestick | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when a bullish bar's body fully engulfs the prior bearish bar's body (two-candle reversal). |
| `pattern_harami_bearish` | candlestick | candlestick | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when a small bearish bar's body sits inside the prior larger bullish bar's body (harami). |
| `pattern_harami_bullish` | candlestick | candlestick | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when a small bullish bar's body sits inside the prior larger bearish bar's body (harami). |
| `upper_shadow_ratio` | candlestick | candlestick | A | tolerance | Float64 | none | (-0.01, 1.01) | Upper wick as a fraction of the high-low range: (high - max(open,close)) / (high-low). |
| `dollar_volume_rank_1m` | cross_sectional_rank | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Cross-sectional percentile (0-1) of this ticker's last-minute dollar volume (close*volume) across all symbols present that minute. |
| `return_rank_15m` | cross_sectional_rank | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Cross-sectional percentile (0-1) of this ticker's trailing 15-minute return across all symbols present that minute. |
| `return_rank_30m` | cross_sectional_rank | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Cross-sectional percentile (0-1) of this ticker's trailing 30-minute return across all symbols present that minute. |
| `return_rank_5m` | cross_sectional_rank | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Cross-sectional percentile (0-1) of this ticker's trailing 5-minute return across all symbols present that minute. |
| `return_rank_60m` | cross_sectional_rank | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Cross-sectional percentile (0-1) of this ticker's trailing 60-minute return across all symbols present that minute. |
| `volume_rank_1m` | cross_sectional_rank | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Cross-sectional percentile (0-1) of this ticker's last-minute share volume across all symbols present that minute. |
| `downside_vol_10m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Downside semi-deviation of one-minute returns over 10 minutes: root-mean-square of the negative returns only. |
| `downside_vol_120m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Downside semi-deviation of one-minute returns over 120 minutes: root-mean-square of the negative returns only. |
| `downside_vol_15m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Downside semi-deviation of one-minute returns over 15 minutes: root-mean-square of the negative returns only. |
| `downside_vol_30m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Downside semi-deviation of one-minute returns over 30 minutes: root-mean-square of the negative returns only. |
| `downside_vol_60m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Downside semi-deviation of one-minute returns over 60 minutes: root-mean-square of the negative returns only. |
| `ret_kurt_10m` | distribution | volatility | A | tolerance | Float64 | warmup | (-3.0, 1000.0) | Excess kurtosis of one-minute returns over the trailing 10 minutes (fat tails / jumpiness; 0 is Gaussian). |
| `ret_kurt_120m` | distribution | volatility | A | tolerance | Float64 | warmup | (-3.0, 1000.0) | Excess kurtosis of one-minute returns over the trailing 120 minutes (fat tails / jumpiness; 0 is Gaussian). |
| `ret_kurt_15m` | distribution | volatility | A | tolerance | Float64 | warmup | (-3.0, 1000.0) | Excess kurtosis of one-minute returns over the trailing 15 minutes (fat tails / jumpiness; 0 is Gaussian). |
| `ret_kurt_30m` | distribution | volatility | A | tolerance | Float64 | warmup | (-3.0, 1000.0) | Excess kurtosis of one-minute returns over the trailing 30 minutes (fat tails / jumpiness; 0 is Gaussian). |
| `ret_kurt_60m` | distribution | volatility | A | tolerance | Float64 | warmup | (-3.0, 1000.0) | Excess kurtosis of one-minute returns over the trailing 60 minutes (fat tails / jumpiness; 0 is Gaussian). |
| `ret_skew_10m` | distribution | volatility | A | tolerance | Float64 | warmup | (-50.0, 50.0) | Skewness of one-minute returns over the trailing 10 minutes (negative = downside-heavy, positive = upside-heavy). |
| `ret_skew_120m` | distribution | volatility | A | tolerance | Float64 | warmup | (-50.0, 50.0) | Skewness of one-minute returns over the trailing 120 minutes (negative = downside-heavy, positive = upside-heavy). |
| `ret_skew_15m` | distribution | volatility | A | tolerance | Float64 | warmup | (-50.0, 50.0) | Skewness of one-minute returns over the trailing 15 minutes (negative = downside-heavy, positive = upside-heavy). |
| `ret_skew_30m` | distribution | volatility | A | tolerance | Float64 | warmup | (-50.0, 50.0) | Skewness of one-minute returns over the trailing 30 minutes (negative = downside-heavy, positive = upside-heavy). |
| `ret_skew_60m` | distribution | volatility | A | tolerance | Float64 | warmup | (-50.0, 50.0) | Skewness of one-minute returns over the trailing 60 minutes (negative = downside-heavy, positive = upside-heavy). |
| `upside_vol_10m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Upside semi-deviation of one-minute returns over 10 minutes: root-mean-square of the positive returns only. |
| `upside_vol_120m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Upside semi-deviation of one-minute returns over 120 minutes: root-mean-square of the positive returns only. |
| `upside_vol_15m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Upside semi-deviation of one-minute returns over 15 minutes: root-mean-square of the positive returns only. |
| `upside_vol_30m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Upside semi-deviation of one-minute returns over 30 minutes: root-mean-square of the positive returns only. |
| `upside_vol_60m` | distribution | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Upside semi-deviation of one-minute returns over 60 minutes: root-mean-square of the positive returns only. |
| `directional_efficiency_10m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Signed Kaufman efficiency over 10 minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction). |
| `directional_efficiency_120m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Signed Kaufman efficiency over 120 minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction). |
| `directional_efficiency_15m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Signed Kaufman efficiency over 15 minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction). |
| `directional_efficiency_20m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Signed Kaufman efficiency over 20 minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction). |
| `directional_efficiency_30m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Signed Kaufman efficiency over 30 minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction). |
| `directional_efficiency_45m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Signed Kaufman efficiency over 45 minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction). |
| `directional_efficiency_5m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Signed Kaufman efficiency over 5 minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction). |
| `directional_efficiency_60m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Signed Kaufman efficiency over 60 minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction). |
| `directional_efficiency_90m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Signed Kaufman efficiency over 90 minutes: net price change / total absolute travel, in [-1, 1] (sign = net direction). |
| `efficiency_ratio_10m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Kaufman efficiency over 10 minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop. |
| `efficiency_ratio_120m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Kaufman efficiency over 120 minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop. |
| `efficiency_ratio_15m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Kaufman efficiency over 15 minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop. |
| `efficiency_ratio_20m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Kaufman efficiency over 20 minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop. |
| `efficiency_ratio_30m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Kaufman efficiency over 30 minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop. |
| `efficiency_ratio_45m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Kaufman efficiency over 45 minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop. |
| `efficiency_ratio_5m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Kaufman efficiency over 5 minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop. |
| `efficiency_ratio_60m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Kaufman efficiency over 60 minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop. |
| `efficiency_ratio_90m` | efficiency | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Kaufman efficiency over 90 minutes: |net price change| / total absolute minute-to-minute travel; 1 is a clean move, 0 is chop. |
| `amihud_illiq_10m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, None) | Amihud illiquidity over 10 minutes: mean of |one-minute return| / dollar volume (price impact per dollar traded). |
| `amihud_illiq_120m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, None) | Amihud illiquidity over 120 minutes: mean of |one-minute return| / dollar volume (price impact per dollar traded). |
| `amihud_illiq_15m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, None) | Amihud illiquidity over 15 minutes: mean of |one-minute return| / dollar volume (price impact per dollar traded). |
| `amihud_illiq_30m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, None) | Amihud illiquidity over 30 minutes: mean of |one-minute return| / dollar volume (price impact per dollar traded). |
| `amihud_illiq_60m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, None) | Amihud illiquidity over 60 minutes: mean of |one-minute return| / dollar volume (price impact per dollar traded). |
| `kyle_lambda_10m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | None | Kyle's lambda over 10 minutes: price-change-per-share-of-signed-flow (OLS slope of close change on signed volume); higher = less liquid. |
| `kyle_lambda_120m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | None | Kyle's lambda over 120 minutes: price-change-per-share-of-signed-flow (OLS slope of close change on signed volume); higher = less liquid. |
| `kyle_lambda_15m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | None | Kyle's lambda over 15 minutes: price-change-per-share-of-signed-flow (OLS slope of close change on signed volume); higher = less liquid. |
| `kyle_lambda_30m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | None | Kyle's lambda over 30 minutes: price-change-per-share-of-signed-flow (OLS slope of close change on signed volume); higher = less liquid. |
| `kyle_lambda_60m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | None | Kyle's lambda over 60 minutes: price-change-per-share-of-signed-flow (OLS slope of close change on signed volume); higher = less liquid. |
| `roll_spread_10m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1.0) | Roll implied effective spread over 10 minutes: 2*sqrt(-cov of consecutive price changes)/close, 0 when autocovariance is non-negative. |
| `roll_spread_120m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1.0) | Roll implied effective spread over 120 minutes: 2*sqrt(-cov of consecutive price changes)/close, 0 when autocovariance is non-negative. |
| `roll_spread_15m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1.0) | Roll implied effective spread over 15 minutes: 2*sqrt(-cov of consecutive price changes)/close, 0 when autocovariance is non-negative. |
| `roll_spread_30m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1.0) | Roll implied effective spread over 30 minutes: 2*sqrt(-cov of consecutive price changes)/close, 0 when autocovariance is non-negative. |
| `roll_spread_60m` | liquidity | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1.0) | Roll implied effective spread over 60 minutes: 2*sqrt(-cov of consecutive price changes)/close, 0 when autocovariance is non-negative. |
| `idio_vol_10m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (0.0, 5.0) | Idiosyncratic volatility over 10 minutes: this ticker's return std times sqrt(1 - market R^2) (movement SPY does not explain). |
| `idio_vol_120m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (0.0, 5.0) | Idiosyncratic volatility over 120 minutes: this ticker's return std times sqrt(1 - market R^2) (movement SPY does not explain). |
| `idio_vol_15m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (0.0, 5.0) | Idiosyncratic volatility over 15 minutes: this ticker's return std times sqrt(1 - market R^2) (movement SPY does not explain). |
| `idio_vol_30m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (0.0, 5.0) | Idiosyncratic volatility over 30 minutes: this ticker's return std times sqrt(1 - market R^2) (movement SPY does not explain). |
| `idio_vol_45m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (0.0, 5.0) | Idiosyncratic volatility over 45 minutes: this ticker's return std times sqrt(1 - market R^2) (movement SPY does not explain). |
| `idio_vol_60m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (0.0, 5.0) | Idiosyncratic volatility over 60 minutes: this ticker's return std times sqrt(1 - market R^2) (movement SPY does not explain). |
| `idio_vol_90m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (0.0, 5.0) | Idiosyncratic volatility over 90 minutes: this ticker's return std times sqrt(1 - market R^2) (movement SPY does not explain). |
| `market_beta_10m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-15.0, 15.0) | Rolling beta to SPY over 10 minutes: slope of this ticker's one-minute return regressed on SPY's. |
| `market_beta_120m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-15.0, 15.0) | Rolling beta to SPY over 120 minutes: slope of this ticker's one-minute return regressed on SPY's. |
| `market_beta_15m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-15.0, 15.0) | Rolling beta to SPY over 15 minutes: slope of this ticker's one-minute return regressed on SPY's. |
| `market_beta_30m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-15.0, 15.0) | Rolling beta to SPY over 30 minutes: slope of this ticker's one-minute return regressed on SPY's. |
| `market_beta_45m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-15.0, 15.0) | Rolling beta to SPY over 45 minutes: slope of this ticker's one-minute return regressed on SPY's. |
| `market_beta_60m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-15.0, 15.0) | Rolling beta to SPY over 60 minutes: slope of this ticker's one-minute return regressed on SPY's. |
| `market_beta_90m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-15.0, 15.0) | Rolling beta to SPY over 90 minutes: slope of this ticker's one-minute return regressed on SPY's. |
| `market_corr_10m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Rolling correlation of this ticker's one-minute return with SPY's over 10 minutes, in [-1, 1]. |
| `market_corr_120m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Rolling correlation of this ticker's one-minute return with SPY's over 120 minutes, in [-1, 1]. |
| `market_corr_15m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Rolling correlation of this ticker's one-minute return with SPY's over 15 minutes, in [-1, 1]. |
| `market_corr_30m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Rolling correlation of this ticker's one-minute return with SPY's over 30 minutes, in [-1, 1]. |
| `market_corr_45m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Rolling correlation of this ticker's one-minute return with SPY's over 45 minutes, in [-1, 1]. |
| `market_corr_60m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Rolling correlation of this ticker's one-minute return with SPY's over 60 minutes, in [-1, 1]. |
| `market_corr_90m` | market_beta | cross_sectional | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Rolling correlation of this ticker's one-minute return with SPY's over 90 minutes, in [-1, 1]. |
| `market_return_10m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 10-minute close-to-close return of the SPY index, broadcast to every ticker as of the minute open. |
| `market_return_120m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 120-minute close-to-close return of the SPY index, broadcast to every ticker as of the minute open. |
| `market_return_15m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 15-minute close-to-close return of the SPY index, broadcast to every ticker as of the minute open. |
| `market_return_20m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 20-minute close-to-close return of the SPY index, broadcast to every ticker as of the minute open. |
| `market_return_30m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 30-minute close-to-close return of the SPY index, broadcast to every ticker as of the minute open. |
| `market_return_45m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 45-minute close-to-close return of the SPY index, broadcast to every ticker as of the minute open. |
| `market_return_5m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 5-minute close-to-close return of the SPY index, broadcast to every ticker as of the minute open. |
| `market_return_60m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 60-minute close-to-close return of the SPY index, broadcast to every ticker as of the minute open. |
| `market_return_90m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 90-minute close-to-close return of the SPY index, broadcast to every ticker as of the minute open. |
| `nasdaq_return_10m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 10-minute close-to-close return of the QQQ index, broadcast to every ticker as of the minute open. |
| `nasdaq_return_120m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 120-minute close-to-close return of the QQQ index, broadcast to every ticker as of the minute open. |
| `nasdaq_return_15m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 15-minute close-to-close return of the QQQ index, broadcast to every ticker as of the minute open. |
| `nasdaq_return_20m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 20-minute close-to-close return of the QQQ index, broadcast to every ticker as of the minute open. |
| `nasdaq_return_30m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 30-minute close-to-close return of the QQQ index, broadcast to every ticker as of the minute open. |
| `nasdaq_return_45m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 45-minute close-to-close return of the QQQ index, broadcast to every ticker as of the minute open. |
| `nasdaq_return_5m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 5-minute close-to-close return of the QQQ index, broadcast to every ticker as of the minute open. |
| `nasdaq_return_60m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 60-minute close-to-close return of the QQQ index, broadcast to every ticker as of the minute open. |
| `nasdaq_return_90m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Trailing 90-minute close-to-close return of the QQQ index, broadcast to every ticker as of the minute open. |
| `outperforming_10m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when this ticker's trailing 10-minute return exceeds SPY's over the same window, else 0.0. |
| `outperforming_120m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when this ticker's trailing 120-minute return exceeds SPY's over the same window, else 0.0. |
| `outperforming_15m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when this ticker's trailing 15-minute return exceeds SPY's over the same window, else 0.0. |
| `outperforming_20m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when this ticker's trailing 20-minute return exceeds SPY's over the same window, else 0.0. |
| `outperforming_30m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when this ticker's trailing 30-minute return exceeds SPY's over the same window, else 0.0. |
| `outperforming_45m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when this ticker's trailing 45-minute return exceeds SPY's over the same window, else 0.0. |
| `outperforming_5m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when this ticker's trailing 5-minute return exceeds SPY's over the same window, else 0.0. |
| `outperforming_60m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when this ticker's trailing 60-minute return exceeds SPY's over the same window, else 0.0. |
| `outperforming_90m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-0.01, 1.01) | 1.0 when this ticker's trailing 90-minute return exceeds SPY's over the same window, else 0.0. |
| `relative_return_10m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-6.0, 6.0) | This ticker's trailing 10-minute return minus SPY's over the same window (market-relative excess return). |
| `relative_return_120m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-6.0, 6.0) | This ticker's trailing 120-minute return minus SPY's over the same window (market-relative excess return). |
| `relative_return_15m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-6.0, 6.0) | This ticker's trailing 15-minute return minus SPY's over the same window (market-relative excess return). |
| `relative_return_20m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-6.0, 6.0) | This ticker's trailing 20-minute return minus SPY's over the same window (market-relative excess return). |
| `relative_return_30m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-6.0, 6.0) | This ticker's trailing 30-minute return minus SPY's over the same window (market-relative excess return). |
| `relative_return_45m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-6.0, 6.0) | This ticker's trailing 45-minute return minus SPY's over the same window (market-relative excess return). |
| `relative_return_5m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-6.0, 6.0) | This ticker's trailing 5-minute return minus SPY's over the same window (market-relative excess return). |
| `relative_return_60m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-6.0, 6.0) | This ticker's trailing 60-minute return minus SPY's over the same window (market-relative excess return). |
| `relative_return_90m` | market_context | cross_sectional | A | tolerance | Float64 | sparse | (-6.0, 6.0) | This ticker's trailing 90-minute return minus SPY's over the same window (market-relative excess return). |
| `active_seconds_1m` | microstructure_burst | microstructure | C | tolerance | Float64 | none | (0.0, 60.0) | Count of distinct seconds within the minute that had at least one trade (0-60). |
| `inter_arrival_cv_1m` | microstructure_burst | microstructure | C | distributional | Float64 | sparse | (0.0, None) | Coefficient of variation of inter-trade gaps in the minute (burstiness of arrivals). |
| `max_runup_1m` | microstructure_burst | microstructure | C | tolerance | Float64 | none | (0.0, None) | Largest within-minute price run-up: max over trades (in exchange-timestamp order) of price minus the running minimum. A PATH-DEPENDENT pattern feature. |
| `peak_trades_per_second_1m` | microstructure_burst | microstructure | C | tolerance | Float64 | none | (0.0, 10000000.0) | Maximum trades printed in any single second within the minute (peak burst intensity). |
| `mean_abs_ret_10m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 10 minutes (choppiness). |
| `mean_abs_ret_120m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 120 minutes (choppiness). |
| `mean_abs_ret_15m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 15 minutes (choppiness). |
| `mean_abs_ret_180m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 180 minutes (choppiness). |
| `mean_abs_ret_20m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 20 minutes (choppiness). |
| `mean_abs_ret_30m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 30 minutes (choppiness). |
| `mean_abs_ret_3m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 3 minutes (choppiness). |
| `mean_abs_ret_45m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 45 minutes (choppiness). |
| `mean_abs_ret_5m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 5 minutes (choppiness). |
| `mean_abs_ret_60m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 60 minutes (choppiness). |
| `mean_abs_ret_90m` | momentum | momentum | A | tolerance | Float64 | warmup | (0.0, 5.0) | Mean absolute one-minute return over the trailing 90 minutes (choppiness). |
| `up_ratio_10m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 10 minutes with a positive one-minute return (0-1). |
| `up_ratio_120m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 120 minutes with a positive one-minute return (0-1). |
| `up_ratio_15m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 15 minutes with a positive one-minute return (0-1). |
| `up_ratio_180m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 180 minutes with a positive one-minute return (0-1). |
| `up_ratio_20m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 20 minutes with a positive one-minute return (0-1). |
| `up_ratio_30m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 30 minutes with a positive one-minute return (0-1). |
| `up_ratio_3m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 3 minutes with a positive one-minute return (0-1). |
| `up_ratio_45m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 45 minutes with a positive one-minute return (0-1). |
| `up_ratio_5m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 5 minutes with a positive one-minute return (0-1). |
| `up_ratio_60m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 60 minutes with a positive one-minute return (0-1). |
| `up_ratio_90m` | momentum | momentum | A | tolerance | Float64 | warmup | (-0.01, 1.01) | Fraction of the trailing 90 minutes with a positive one-minute return (0-1). |
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
| `above_vwap_10d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when the prior close is above the 10-day volume-weighted average price, else 0.0. |
| `above_vwap_120d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when the prior close is above the 120-day volume-weighted average price, else 0.0. |
| `above_vwap_20d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when the prior close is above the 20-day volume-weighted average price, else 0.0. |
| `above_vwap_5d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when the prior close is above the 5-day volume-weighted average price, else 0.0. |
| `above_vwap_60d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when the prior close is above the 60-day volume-weighted average price, else 0.0. |
| `dist_from_vwap_10d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Prior close relative to the 10-day volume-weighted average price (close/vwap_10d - 1), point-in-time. |
| `dist_from_vwap_120d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Prior close relative to the 120-day volume-weighted average price (close/vwap_120d - 1), point-in-time. |
| `dist_from_vwap_20d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Prior close relative to the 20-day volume-weighted average price (close/vwap_20d - 1), point-in-time. |
| `dist_from_vwap_5d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Prior close relative to the 5-day volume-weighted average price (close/vwap_5d - 1), point-in-time. |
| `dist_from_vwap_60d` | multi_day_vwap | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Prior close relative to the 60-day volume-weighted average price (close/vwap_60d - 1), point-in-time. |
| `garman_klass_vol_10m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Garman-Klass volatility over 10 minutes: OHLC-efficient per-bar variance (0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2) averaged then rooted. |
| `garman_klass_vol_120m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Garman-Klass volatility over 120 minutes: OHLC-efficient per-bar variance (0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2) averaged then rooted. |
| `garman_klass_vol_15m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Garman-Klass volatility over 15 minutes: OHLC-efficient per-bar variance (0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2) averaged then rooted. |
| `garman_klass_vol_30m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Garman-Klass volatility over 30 minutes: OHLC-efficient per-bar variance (0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2) averaged then rooted. |
| `garman_klass_vol_5m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Garman-Klass volatility over 5 minutes: OHLC-efficient per-bar variance (0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2) averaged then rooted. |
| `garman_klass_vol_60m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Garman-Klass volatility over 60 minutes: OHLC-efficient per-bar variance (0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2) averaged then rooted. |
| `rogers_satchell_vol_10m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Rogers-Satchell volatility over 10 minutes: drift-independent OHLC variance (ln(H/C)ln(H/O)+ln(L/C)ln(L/O)) averaged then rooted. |
| `rogers_satchell_vol_120m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Rogers-Satchell volatility over 120 minutes: drift-independent OHLC variance (ln(H/C)ln(H/O)+ln(L/C)ln(L/O)) averaged then rooted. |
| `rogers_satchell_vol_15m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Rogers-Satchell volatility over 15 minutes: drift-independent OHLC variance (ln(H/C)ln(H/O)+ln(L/C)ln(L/O)) averaged then rooted. |
| `rogers_satchell_vol_30m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Rogers-Satchell volatility over 30 minutes: drift-independent OHLC variance (ln(H/C)ln(H/O)+ln(L/C)ln(L/O)) averaged then rooted. |
| `rogers_satchell_vol_5m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Rogers-Satchell volatility over 5 minutes: drift-independent OHLC variance (ln(H/C)ln(H/O)+ln(L/C)ln(L/O)) averaged then rooted. |
| `rogers_satchell_vol_60m` | ohlc_vol | volatility | A | tolerance | Float64 | warmup | (0.0, 5.0) | Rogers-Satchell volatility over 60 minutes: drift-independent OHLC variance (ln(H/C)ln(H/O)+ln(L/C)ln(L/O)) averaged then rooted. |
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
| `buying_pressure_10m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 10 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `buying_pressure_120m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 120 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `buying_pressure_15m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 15 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `buying_pressure_20m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 20 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `buying_pressure_30m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 30 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `buying_pressure_3m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 3 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `buying_pressure_45m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 45 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `buying_pressure_5m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 5 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `buying_pressure_60m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 60 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `buying_pressure_90m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Volume-weighted money-flow position over 90 minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1]. |
| `down_volume_ratio_10m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 10-minute share volume that printed on down-bars (negative one-minute return). |
| `down_volume_ratio_120m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 120-minute share volume that printed on down-bars (negative one-minute return). |
| `down_volume_ratio_15m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 15-minute share volume that printed on down-bars (negative one-minute return). |
| `down_volume_ratio_20m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 20-minute share volume that printed on down-bars (negative one-minute return). |
| `down_volume_ratio_30m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 30-minute share volume that printed on down-bars (negative one-minute return). |
| `down_volume_ratio_3m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 3-minute share volume that printed on down-bars (negative one-minute return). |
| `down_volume_ratio_45m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 45-minute share volume that printed on down-bars (negative one-minute return). |
| `down_volume_ratio_5m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 5-minute share volume that printed on down-bars (negative one-minute return). |
| `down_volume_ratio_60m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 60-minute share volume that printed on down-bars (negative one-minute return). |
| `down_volume_ratio_90m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 90-minute share volume that printed on down-bars (negative one-minute return). |
| `obv_slope_10m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 10 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `obv_slope_120m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 120 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `obv_slope_15m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 15 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `obv_slope_20m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 20 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `obv_slope_30m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 30 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `obv_slope_3m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 3 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `obv_slope_45m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 45 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `obv_slope_5m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 5 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `obv_slope_60m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 60 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `obv_slope_90m` | price_volume | price_volume | A | tolerance | Float64 | warmup | None | Slope of on-balance volume regressed on time over 90 minutes, normalized by mean window volume (accumulation/distribution drift). |
| `pv_correlation_10m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 10 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `pv_correlation_120m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 120 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `pv_correlation_15m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 15 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `pv_correlation_20m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 20 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `pv_correlation_30m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 30 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `pv_correlation_3m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 3 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `pv_correlation_45m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 45 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `pv_correlation_5m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 5 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `pv_correlation_60m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 60 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `pv_correlation_90m` | price_volume | price_volume | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Rolling correlation of one-minute return and share volume over 90 minutes (does volume accompany up or down moves), in [-1, 1]. |
| `up_volume_ratio_10m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 10-minute share volume that printed on up-bars (positive one-minute return). |
| `up_volume_ratio_120m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 120-minute share volume that printed on up-bars (positive one-minute return). |
| `up_volume_ratio_15m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 15-minute share volume that printed on up-bars (positive one-minute return). |
| `up_volume_ratio_20m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 20-minute share volume that printed on up-bars (positive one-minute return). |
| `up_volume_ratio_30m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 30-minute share volume that printed on up-bars (positive one-minute return). |
| `up_volume_ratio_3m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 3-minute share volume that printed on up-bars (positive one-minute return). |
| `up_volume_ratio_45m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 45-minute share volume that printed on up-bars (positive one-minute return). |
| `up_volume_ratio_5m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 5-minute share volume that printed on up-bars (positive one-minute return). |
| `up_volume_ratio_60m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 60-minute share volume that printed on up-bars (positive one-minute return). |
| `up_volume_ratio_90m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-0.01, 1.01) | Fraction of the trailing 90-minute share volume that printed on up-bars (positive one-minute return). |
| `volume_delta_10m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 10 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `volume_delta_120m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 120 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `volume_delta_15m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 15 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `volume_delta_20m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 20 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `volume_delta_30m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 30 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `volume_delta_3m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 3 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `volume_delta_45m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 45 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `volume_delta_5m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 5 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `volume_delta_60m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 60 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `volume_delta_90m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.01, 1.01) | Net directional volume over 90 minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1]. |
| `vwap_deviation_10m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 10-minute volume-weighted average price (close/vwap - 1). |
| `vwap_deviation_120m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 120-minute volume-weighted average price (close/vwap - 1). |
| `vwap_deviation_15m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 15-minute volume-weighted average price (close/vwap - 1). |
| `vwap_deviation_20m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 20-minute volume-weighted average price (close/vwap - 1). |
| `vwap_deviation_30m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 30-minute volume-weighted average price (close/vwap - 1). |
| `vwap_deviation_3m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 3-minute volume-weighted average price (close/vwap - 1). |
| `vwap_deviation_45m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 45-minute volume-weighted average price (close/vwap - 1). |
| `vwap_deviation_5m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 5-minute volume-weighted average price (close/vwap - 1). |
| `vwap_deviation_60m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 60-minute volume-weighted average price (close/vwap - 1). |
| `vwap_deviation_90m` | price_volume | price_volume | A | tolerance | Float64 | sparse | (-1.0, 5.0) | Close relative to its trailing 90-minute volume-weighted average price (close/vwap - 1). |
| `above_pivot` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-0.01, 1.01) | 1.0 when the current close is above the prior-day floor pivot P, else 0.0. |
| `dist_from_pivot_p` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Current close relative to the pivot P from the prior day's OHLC (close/level - 1). |
| `dist_from_pivot_r1` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Current close relative to resistance R1 from the prior day's OHLC (close/level - 1). |
| `dist_from_pivot_r2` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Current close relative to resistance R2 from the prior day's OHLC (close/level - 1). |
| `dist_from_pivot_s1` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Current close relative to support S1 from the prior day's OHLC (close/level - 1). |
| `dist_from_pivot_s2` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Current close relative to support S2 from the prior day's OHLC (close/level - 1). |
| `dist_from_prior_close` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Current close relative to the prior day's close (close/prev_close - 1). |
| `dist_from_prior_high` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Current close relative to the prior day's high (close/prev_high - 1). |
| `dist_from_prior_low` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Current close relative to the prior day's low (close/prev_low - 1). |
| `gap_open` | prior_day | multi_day | A | tolerance | Float64 | warmup | (-1.0, 5.0) | Overnight gap: today's daily open relative to the prior day's close (open/prev_close - 1). |
| `book_depth_1m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, None) | Mean total top-of-book size (bid_size + ask_size) over the last minute. |
| `quote_imbalance_10m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 10 minutes. |
| `quote_imbalance_120m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 120 minutes. |
| `quote_imbalance_15m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 15 minutes. |
| `quote_imbalance_1m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance (bid-ask)/(bid+ask) over the last minute. |
| `quote_imbalance_20m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 20 minutes. |
| `quote_imbalance_30m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 30 minutes. |
| `quote_imbalance_45m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 45 minutes. |
| `quote_imbalance_5m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 5 minutes. |
| `quote_imbalance_60m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 60 minutes. |
| `quote_imbalance_90m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (-1.0, 1.0) | Mean top-of-book size imbalance over the trailing 90 minutes. |
| `spread_bps_10m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 10 minutes. |
| `spread_bps_120m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 120 minutes. |
| `spread_bps_15m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 15 minutes. |
| `spread_bps_1m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Average top-of-book bid-ask spread in basis points over the last minute. |
| `spread_bps_20m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 20 minutes. |
| `spread_bps_30m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 30 minutes. |
| `spread_bps_45m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 45 minutes. |
| `spread_bps_5m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 5 minutes. |
| `spread_bps_60m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 60 minutes. |
| `spread_bps_90m` | quote_spread | quote_spread | B | tolerance | Float64 | sparse | (0.0, 100000.0) | Mean top-of-book spread in basis points over the trailing 90 minutes. |
| `autocorr_1_10m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-1 autocorrelation of one-minute returns over 10 minutes (negative = mean-reverting, positive = trending), in [-1, 1]. |
| `autocorr_1_120m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-1 autocorrelation of one-minute returns over 120 minutes (negative = mean-reverting, positive = trending), in [-1, 1]. |
| `autocorr_1_15m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-1 autocorrelation of one-minute returns over 15 minutes (negative = mean-reverting, positive = trending), in [-1, 1]. |
| `autocorr_1_30m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-1 autocorrelation of one-minute returns over 30 minutes (negative = mean-reverting, positive = trending), in [-1, 1]. |
| `autocorr_1_60m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-1 autocorrelation of one-minute returns over 60 minutes (negative = mean-reverting, positive = trending), in [-1, 1]. |
| `autocorr_2_10m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-2 autocorrelation of one-minute returns over 10 minutes (two-step return persistence), in [-1, 1]. |
| `autocorr_2_120m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-2 autocorrelation of one-minute returns over 120 minutes (two-step return persistence), in [-1, 1]. |
| `autocorr_2_15m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-2 autocorrelation of one-minute returns over 15 minutes (two-step return persistence), in [-1, 1]. |
| `autocorr_2_30m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-2 autocorrelation of one-minute returns over 30 minutes (two-step return persistence), in [-1, 1]. |
| `autocorr_2_60m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-1.01, 1.01) | Lag-2 autocorrelation of one-minute returns over 60 minutes (two-step return persistence), in [-1, 1]. |
| `ret_accel_10m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Return acceleration: the trailing 10-minute return minus the prior 10-minute return (is the move speeding up or fading). |
| `ret_accel_15m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Return acceleration: the trailing 15-minute return minus the prior 15-minute return (is the move speeding up or fading). |
| `ret_accel_30m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Return acceleration: the trailing 30-minute return minus the prior 30-minute return (is the move speeding up or fading). |
| `ret_accel_5m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Return acceleration: the trailing 5-minute return minus the prior 5-minute return (is the move speeding up or fading). |
| `ret_accel_60m` | return_dynamics | momentum | A | tolerance | Float64 | warmup | (-5.0, 5.0) | Return acceleration: the trailing 60-minute return minus the prior 60-minute return (is the move speeding up or fading). |
| `dist_to_half_dollar` | round_levels | price | A | tolerance | Float64 | none | (0.0, 0.26) | Absolute distance from the close to the nearest half dollar (x.00 or x.50), in dollars (0 to 0.25). |
| `dist_to_round_dollar` | round_levels | price | A | tolerance | Float64 | none | (0.0, 0.51) | Absolute distance from the close to the nearest whole dollar, in dollars (0 to 0.5). |
| `is_at_round_dollar` | round_levels | price | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the close is within 2 cents of a whole dollar, else 0.0 (round-number cluster). |
| `sector_is_basic_materials` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is basic materials, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_communication_services` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is communication services, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_consumer_cyclical` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is consumer cyclical, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_consumer_defensive` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is consumer defensive, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_energy` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is energy, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_financial_services` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is financial services, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_healthcare` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is healthcare, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_industrials` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is industrials, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_real_estate` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is real estate, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_technology` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is technology, else 0.0 (one-hot, broadcast across the day). |
| `sector_is_unknown` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol has no mapped sector (unlisted in the sector map or FMP could not classify it), else 0.0. |
| `sector_is_utilities` | sector | reference | A | tolerance | Float64 | none | (-0.01, 1.01) | 1.0 when the symbol's GICS-aligned sector is utilities, else 0.0 (one-hot, broadcast across the day). |
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
| `signed_volume_120m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 120 minutes (net buy/sell pressure). |
| `signed_volume_15m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 15 minutes (net buy/sell pressure). |
| `signed_volume_180m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 180 minutes (net buy/sell pressure). |
| `signed_volume_1m` | trade_flow | trade_flow | B | tolerance | Float64 | none | None | Buy-minus-sell signed share volume over the last minute (tick-rule signed). |
| `signed_volume_20m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 20 minutes (net buy/sell pressure). |
| `signed_volume_30m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 30 minutes (net buy/sell pressure). |
| `signed_volume_45m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 45 minutes (net buy/sell pressure). |
| `signed_volume_5m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 5 minutes (net buy/sell pressure). |
| `signed_volume_60m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 60 minutes (net buy/sell pressure). |
| `signed_volume_90m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Sum of signed share volume over the trailing 90 minutes (net buy/sell pressure). |
| `trade_freq_10m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 10 minutes. |
| `trade_freq_120m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 120 minutes. |
| `trade_freq_15m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 15 minutes. |
| `trade_freq_180m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 180 minutes. |
| `trade_freq_1m` | trade_flow | trade_flow | B | tolerance | Float64 | none | (0.0, 10000000.0) | Number of trades printed in the last minute (raw trade frequency). |
| `trade_freq_20m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 20 minutes. |
| `trade_freq_30m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 30 minutes. |
| `trade_freq_45m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 45 minutes. |
| `trade_freq_5m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 5 minutes. |
| `trade_freq_60m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 60 minutes. |
| `trade_freq_90m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | (0.0, 1000000000.0) | Total number of trades over the trailing 90 minutes. |
| `trade_rate_accel_1m` | trade_flow | trade_flow | B | tolerance | Float64 | warmup | None | Change in trades-per-second versus the prior minute (trade-rate acceleration). |
| `price_r2_10m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 10-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_r2_120m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 120-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_r2_15m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 15-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_r2_180m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 180-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_r2_20m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 20-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_r2_30m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 30-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_r2_45m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 45-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_r2_5m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 5-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_r2_60m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 60-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_r2_90m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-0.01, 1.01) | R-squared of the trailing 90-minute close-vs-time OLS fit: 1.0 is a perfectly straight move, 0.0 is choppy. |
| `price_slope_10m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 10 minutes, normalized as a fractional price move per minute. |
| `price_slope_120m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 120 minutes, normalized as a fractional price move per minute. |
| `price_slope_15m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 15 minutes, normalized as a fractional price move per minute. |
| `price_slope_180m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 180 minutes, normalized as a fractional price move per minute. |
| `price_slope_20m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 20 minutes, normalized as a fractional price move per minute. |
| `price_slope_30m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 30 minutes, normalized as a fractional price move per minute. |
| `price_slope_45m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 45 minutes, normalized as a fractional price move per minute. |
| `price_slope_5m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 5 minutes, normalized as a fractional price move per minute. |
| `price_slope_60m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 60 minutes, normalized as a fractional price move per minute. |
| `price_slope_90m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | OLS slope of close on time over the trailing 90 minutes, normalized as a fractional price move per minute. |
| `trend_strength_10m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 10 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
| `trend_strength_120m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 120 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
| `trend_strength_15m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 15 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
| `trend_strength_180m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 180 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
| `trend_strength_20m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 20 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
| `trend_strength_30m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 30 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
| `trend_strength_45m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 45 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
| `trend_strength_5m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 5 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
| `trend_strength_60m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 60 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
| `trend_strength_90m` | trend_quality | trend_quality | A | tolerance | Float64 | warmup | (-1.0, 1.0) | Signed quality-weighted trend over 90 minutes: normalized slope times R-squared (steep AND clean moves score highest). |
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
| `volume_ratio_180m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 180-minute mean. |
| `volume_ratio_20m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 20-minute mean. |
| `volume_ratio_30m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 30-minute mean. |
| `volume_ratio_3m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 3-minute mean. |
| `volume_ratio_45m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 45-minute mean. |
| `volume_ratio_5m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 5-minute mean. |
| `volume_ratio_60m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 60-minute mean. |
| `volume_ratio_90m` | volume | volume | A | tolerance | Float64 | warmup | (0.0, None) | Ratio of the last minute's share volume to its trailing 90-minute mean. |
| `volume_zscore_10m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 10-minute mean and std. |
| `volume_zscore_120m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 120-minute mean and std. |
| `volume_zscore_15m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 15-minute mean and std. |
| `volume_zscore_180m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 180-minute mean and std. |
| `volume_zscore_20m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 20-minute mean and std. |
| `volume_zscore_30m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 30-minute mean and std. |
| `volume_zscore_3m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 3-minute mean and std. |
| `volume_zscore_45m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 45-minute mean and std. |
| `volume_zscore_5m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 5-minute mean and std. |
| `volume_zscore_60m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 60-minute mean and std. |
| `volume_zscore_90m` | volume | volume | A | tolerance | Float64 | warmup | None | Z-score of the last minute's share volume vs the trailing 90-minute mean and std. |
