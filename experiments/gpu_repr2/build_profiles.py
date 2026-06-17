"""Build multi-channel daily behavioral profiles for cross-asset representation learning.

The #76 baseline (experiments/gpu_repr) learned a LINEAR (SVD) embedding from a SINGLE channel:
close-to-close return correlation. It validated OOS (within-minus-across cluster return corr 0.092
held-out vs 0.0003 random). The INSIGHTS doc states this is "the floor, not the ceiling" — a linear,
single-channel embedding that any deep model must BEAT to justify its complexity.

This lane builds a RICHER substrate: per (symbol, day) we decompose behavior into multiple channels the
single c2c-return channel throws away:
  - overnight return  : log(rth_open_t / rth_close_{t-1})      (gap behavior)
  - intraday return   : log(rth_close_t / rth_open_t)          (session drift behavior)
  - c2c return        : log(rth_close_t / rth_close_{t-1})     (the #76 channel; = overnight + intraday)
  - log dollar volume : log(dollar_vol_t)                      (activity LEVEL, cross-sectionally z-scored)
  - dvol change       : log(dollar_vol_t / dollar_vol_{t-1})   (activity DYNAMICS)

Two stocks can share identical c2c correlation yet differ sharply in HOW they get there (gap-driven vs
intraday-drift) and in their activity regime. That extra behavioral information is what a richer embedding
can exploit beyond the linear c2c floor.

Output: a dict of per-day standardized cross-sectional panels (symbol x channel) the trainer consumes, plus
the symbol list and date list. We CROSS-SECTIONALLY standardize each channel per day (z-score across symbols
on that day) so the embedding captures a symbol's RELATIVE behavior vs the market that day, not the absolute
market move — the market factor is removed by construction, pushing the model toward style/peer structure
(exactly the structure #76's components 2-8 carry, which is where any non-linear edge must live).

Run:
  VENV/bin/python experiments/gpu_repr2/build_profiles.py \
      --bars <certify300_daily.parquet> --out experiments/gpu_repr2/out/profiles.npz
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import polars as pl

MIN_DAYS = 250  # ~1y of history so per-symbol behavior is stable
CHANNELS = ["overnight", "intraday", "c2c", "logdvol", "dvol_chg"]


def build_channels(bars_path: str, min_days: int) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Return (n_symbols x n_days x n_channels) behavioral tensor, symbols, dates.

    Symbols are restricted to those with >= min_days of data. Days are the union of trading dates; a
    symbol missing a day gets NaN for that day (later masked to 0 in the per-day cross-section). All
    channels are RAW here; cross-sectional standardization happens per day in standardize_cross_section.
    """
    frame = pl.read_parquet(bars_path).sort(["symbol", "date"])
    frame = frame.with_columns(
        prev_close=pl.col("rth_close").shift(1).over("symbol"),
        prev_dvol=pl.col("dollar_vol").shift(1).over("symbol"),
    )
    frame = frame.with_columns(
        overnight=(pl.col("rth_open").log() - pl.col("prev_close").log()),
        intraday=(pl.col("rth_close").log() - pl.col("rth_open").log()),
        c2c=(pl.col("rth_close").log() - pl.col("prev_close").log()),
        logdvol=pl.col("dollar_vol").log(),
        dvol_chg=(pl.col("dollar_vol").log() - pl.col("prev_dvol").log()),
    ).drop_nulls(["overnight", "intraday", "c2c", "dvol_chg"])

    counts = frame.group_by("symbol").len().filter(pl.col("len") >= min_days)
    keep_symbols = sorted(counts["symbol"].to_list())
    frame = frame.filter(pl.col("symbol").is_in(keep_symbols))

    all_dates = np.array(sorted(frame["date"].unique().to_list()))
    date_to_idx = {date: idx for idx, date in enumerate(all_dates)}
    sym_to_idx = {sym: idx for idx, sym in enumerate(keep_symbols)}
    n_symbols = len(keep_symbols)
    n_days = len(all_dates)

    tensor = np.full((n_symbols, n_days, len(CHANNELS)), np.nan, dtype=np.float32)
    rows = frame.select(["symbol", "date", *CHANNELS]).to_dicts()
    for row in rows:
        si = sym_to_idx[row["symbol"]]
        di = date_to_idx[row["date"]]
        for ci, channel in enumerate(CHANNELS):
            tensor[si, di, ci] = row[channel]
    return tensor, keep_symbols, all_dates


def standardize_cross_section(tensor: np.ndarray) -> np.ndarray:
    """Per (day, channel) z-score across symbols, then NaN->0.

    Cross-sectional standardization removes the market-wide move each day (the dominant SVD factor in #76),
    so the embedding focuses on a symbol's RELATIVE behavior vs peers — the style/sector structure where any
    non-linear edge beyond the linear market factor must live. NaNs (symbol absent that day) become 0 = "no
    info / at the cross-sectional mean", a neutral fill, not a fabricated move.
    """
    out = np.empty_like(tensor)
    n_days = tensor.shape[1]
    n_channels = tensor.shape[2]
    for di in range(n_days):
        for ci in range(n_channels):
            column = tensor[:, di, ci]
            valid = ~np.isnan(column)
            if valid.sum() < 2:
                out[:, di, ci] = 0.0
                continue
            values = column[valid]
            mean = values.mean()
            std = values.std()
            std = std if std > 1e-8 else 1.0
            standardized = np.zeros_like(column)
            standardized[valid] = (values - mean) / std
            out[:, di, ci] = standardized
    return np.nan_to_num(out, nan=0.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--min-days", type=int, default=MIN_DAYS)
    args = parser.parse_args()

    tensor, symbols, dates = build_channels(args.bars, args.min_days)
    standardized = standardize_cross_section(tensor)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_path,
        panel=standardized,  # n_symbols x n_days x n_channels, cross-sectionally z-scored
        symbols=np.array(symbols),
        dates=np.array([str(date) for date in dates]),
        channels=np.array(CHANNELS),
    )
    print(f"profiles: {standardized.shape[0]} symbols x {standardized.shape[1]} days x "
          f"{standardized.shape[2]} channels")
    print(f"date range: {dates[0]} -> {dates[-1]}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
