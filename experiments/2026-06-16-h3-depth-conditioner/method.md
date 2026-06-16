# H3 Method

## Data

- **Panel**: H2 panel at `experiments/2026-06-16-h2-retest-ofi-orthogonal/data/panel.parquet`
  - 1,568,418 rows × 250 symbols × 20 trading days (2026-05-18 → 2026-06-15)
  - RTH only (already filtered in H2), cols: `minute`, `symbol`, `date`, `vwap_dev_15`, `fwd_ret_15`, `fwd_ret_30`, `rel_spread_mean`
- **Quotes**: `/store/raw/quotes/symbol=<S>/date=<D>/data.parquet`, cols: `ts`, `bid_price`, `bid_size`, `ask_price`, `ask_size`
  - Loaded for all 250 symbols × 20 dates = 4,970 (sym, date) pairs (0 missing)
  - RTH filter: `ts` in [14:30, 20:00) UTC (09:30–15:00 ET)
  - Positive-depth filter: `bid_size + ask_size > 0`

## Book-state column definitions

Per `(symbol, minute)` after RTH + positive-depth filter:

| Column | Formula |
|--------|---------|
| `book_depth` | `mean(bid_size + ask_size)` over the minute |
| `size_imbalance` | `mean((bid_size − ask_size) / (bid_size + ask_size))` over the minute |

`rel_spread_mean` (already in panel) used as the spread regime.

After join: 1,417,038 clean rows (90.3% join rate — ~10% of panel minutes lack quotes, likely late RTH).

## Conditioning design

**Book-state terciles**: global quantile cuts (q33 / q67) across the full clean panel for each conditioner:
- `spread_tercile`: tight (0) / mid (1) / wide (2) on `rel_spread_mean`
- `depth_tercile`: thin (0) / mid (1) / deep (2) on `book_depth`
- `imbal_tercile`: bid-heavy (0) / neutral (1) / ask-heavy (2) on `size_imbalance`

**Signal**: `vwap_dev_15` (trailing 15-min VWAP deviation, reversion direction: long most-below, short most-above)

**L/S construction** (vectorized with polars `group_by`):
- Within each `(date, minute)` cross-section, rank `vwap_dev_15` → decile 0–9
- Long = decile 0 (most below VWAP), Short = decile 9 (most above VWAP)
- L/S return per cross-section = mean(long fwd_ret) − mean(short fwd_ret)

**Cost anchor**: `rel_spread_mean` within the tercile subset (already fractional → × 10,000 = bps). Round-trip cost = 1 × spread (one-way cost on each leg = spread/2 × 2 sides = 1 spread).

**Net-of-cost gross**: gross bps − spread bps

**Horizons**: H15 (`fwd_ret_15`) and H30 (`fwd_ret_30`)

**Canary**: 10 seeds, within-cross-section shuffle of `vwap_dev_15` signal, applied identically to conditioned subsets. Reports max canary net bps as the noise ceiling.
