"""D3 — build RTH minute-sequence dataset for the intraday SEQUENCE world-model.

The repr-2 lane embedded STATIC cross-sectional behavior (a symbol's daily-return profile). D3 is the
orthogonal frontier: intraday DYNAMICS — given a symbol's minute-by-minute path so far, what happens next,
and where is the path predictable vs SURPRISING? The hidden state of a next-state predictor is a learned
dynamics embedding; the per-minute prediction surprise (||predicted - actual||) is the candidate feature
("an unusual-dynamics scalar").

Substrate (WELL-POWERED, unlike the n=137 tick model the queue rejected): the `fp_store_real` bars volume,
7,682 symbols x ~379 days of minute OHLCV+vwap+trade_count. We restrict to the curated top-300 LIQUID
universe (clean, densely-traded minute bars) x all available days = tens of thousands of symbol-day
sequences -> plenty for a sequence model.

Per RTH minute (13:30-20:00 UTC = 09:30-16:00 ET) we build a per-minute behavioral feature vector that is a
deterministic function of settled bars (parity-relevant):
  - log return         : log(close_t / close_{t-1})
  - high-low range     : (high - low) / close            (intrabar volatility)
  - close location     : (close - low) / (high - low)     (where in the bar it closed; 0.5 if flat)
  - signed log volume  : sign(ret) * log1p(volume)        (directional activity)
  - log trade_count    : log1p(trade_count)               (activity intensity)
  - minute-of-session  : t / n_minutes                    (intraday clock; U-shaped vol is real)
All return/volume features are per-(symbol,day) z-scored across the session's minutes so the model learns
the SHAPE of the path, not the day's absolute scale (a deterministic within-sequence transform).

Splits (pre-registered rigor): held-out SYMBOLS (20% of symbols never seen in train) AND held-out TIME
(the last 20% of dates). The OOS metric is computed on the intersection-safe held-out sets.

Run (inside fp-torch-gpu, with -v fp_store_real:/store:ro):
  python experiments/gpu_repr2_d3/build_sequences.py --store /store --universe <top300.json> \
      --out experiments/gpu_repr2_d3/out/sequences.npz --max-days 379
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl

RTH_START_MIN = 13 * 60 + 30  # 13:30 UTC
RTH_END_MIN = 20 * 60  # 20:00 UTC
SEQ_LEN = 390  # RTH minutes
MIN_BARS = 300  # require a well-covered session (>=300 of 390 RTH minutes traded)
FEATURE_NAMES = ["logret", "range", "close_loc", "signed_logvol", "log_tradecount", "minute_frac"]


def load_universe(path: str) -> list[str]:
    data = json.loads(Path(path).read_text())
    return list(data["top300"]) if isinstance(data, dict) else list(data)


def session_minutes(frame: pl.DataFrame) -> pl.DataFrame:
    """Filter to RTH minutes and compute the minute-of-day index, sorted by time."""
    frame = frame.with_columns(
        minute_of_day=(pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32))
    )
    return frame.filter(
        (pl.col("minute_of_day") >= RTH_START_MIN) & (pl.col("minute_of_day") < RTH_END_MIN)
    ).sort("ts")


def build_day_features(frame: pl.DataFrame) -> np.ndarray | None:
    """Return a (SEQ_LEN x n_features) array for one symbol-day, or None if too sparse.

    Sparse minutes (no trade) are forward-filled on price and zero-filled on volume so the sequence is a
    regular grid; the model sees the real path with gaps marked by zero activity, not fabricated moves.
    """
    rth = session_minutes(frame)
    if rth.height < MIN_BARS:
        return None
    grid = pl.DataFrame({"minute_of_day": np.arange(RTH_START_MIN, RTH_END_MIN, dtype=np.int32)})
    merged = grid.join(rth, on="minute_of_day", how="left").sort("minute_of_day")
    merged = merged.with_columns(
        close=pl.col("close").forward_fill(),
        high=pl.col("high").forward_fill(),
        low=pl.col("low").forward_fill(),
        volume=pl.col("volume").fill_null(0),
        trade_count=pl.col("trade_count").fill_null(0),
    ).drop_nulls("close")
    if merged.height < MIN_BARS:
        return None

    close = merged["close"].to_numpy().astype(np.float64)
    high = merged["high"].to_numpy().astype(np.float64)
    low = merged["low"].to_numpy().astype(np.float64)
    volume = merged["volume"].to_numpy().astype(np.float64)
    trade_count = merged["trade_count"].to_numpy().astype(np.float64)
    n = len(close)

    logret = np.zeros(n)
    logret[1:] = np.log(close[1:] / close[:-1])
    span = high - low
    rng = np.where(close > 0, span / close, 0.0)
    close_loc = np.where(span > 1e-9, (close - low) / span, 0.5)
    signed_logvol = np.sign(logret) * np.log1p(volume)
    log_tradecount = np.log1p(trade_count)
    minute_frac = np.linspace(0.0, 1.0, n)

    features = np.stack([logret, rng, close_loc, signed_logvol, log_tradecount, minute_frac], axis=1).astype(
        np.float32
    )

    # z-score the scale-dependent channels WITHIN the session (learn shape, not absolute scale)
    for ci in [0, 1, 3, 4]:
        column = features[:, ci]
        std = column.std()
        if std > 1e-8:
            features[:, ci] = (column - column.mean()) / std

    if n < SEQ_LEN:
        pad = np.zeros((SEQ_LEN - n, features.shape[1]), dtype=np.float32)
        features = np.concatenate([features, pad], axis=0)
    return features[:SEQ_LEN]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default="/store")
    parser.add_argument("--universe", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-days", type=int, default=379)
    parser.add_argument("--max-symbols", type=int, default=300)
    args = parser.parse_args()

    symbols = load_universe(args.universe)[: args.max_symbols]
    bars_root = Path(args.store) / "raw" / "bars"

    sequences: list[np.ndarray] = []
    seq_symbols: list[str] = []
    seq_dates: list[str] = []
    for symbol in symbols:
        sym_dir = bars_root / f"symbol={symbol}"
        if not sym_dir.is_dir():
            continue
        date_dirs = sorted(sym_dir.glob("date=*"))[-args.max_days :]
        for date_dir in date_dirs:
            parquet = date_dir / "data.parquet"
            if not parquet.exists():
                continue
            frame = pl.read_parquet(parquet)
            day_features = build_day_features(frame)
            if day_features is None:
                continue
            sequences.append(day_features)
            seq_symbols.append(symbol)
            seq_dates.append(date_dir.name.split("=")[1])
        print(f"{symbol}: {seq_symbols.count(symbol)} sessions")

    tensor = np.stack(sequences, axis=0)  # n_sessions x SEQ_LEN x n_features
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_path,
        sequences=tensor,
        symbols=np.array(seq_symbols),
        dates=np.array(seq_dates),
        feature_names=np.array(FEATURE_NAMES),
    )
    print(f"\n{tensor.shape[0]} sessions x {tensor.shape[1]} minutes x {tensor.shape[2]} features")
    print(f"unique symbols: {len(set(seq_symbols))} | date range: {min(seq_dates)} -> {max(seq_dates)}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
