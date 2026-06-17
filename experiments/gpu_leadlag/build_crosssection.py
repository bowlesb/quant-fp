"""Build the minute CROSS-SECTION panel for the lead-lag / flow model.

D3 proved single-name autoregression of minute returns is a vol-only null. The unexploited frontier is
CROSS-SYMBOL next-move structure: does the minute-t cross-section (other symbols' returns / signed flow)
predict symbol i's minute-(t+1) move, BEYOND contemporaneous market co-movement?

The honest target is the MARKET-RESIDUALIZED return, so we are not just rediscovering "everything co-moves
with the market this minute" (beta). Per (day, minute):
  - raw return r_{i,t} = log(close_{i,t} / close_{i,t-1})
  - market move m_t = cross-sectional MEAN of r_{.,t} (equal-weight market proxy)
  - residual return resid_{i,t} = r_{i,t} - m_t   (the idiosyncratic part — the lead-lag target)
  - signed flow f_{i,t} = sign(r_{i,t}) * log1p(volume_{i,t}), cross-sectionally z-scored per minute
We emit, per day, the aligned (minute x symbol) matrices RESID and FLOW plus the market series M_t. The model
predicts resid_{i,t+1} from the lagged cross-sectional state {resid_{.,t}, flow_{.,t}, m_t}.

Universe: a fixed liquid set (top-300) present on the day, so the cross-section is densely populated every
minute. Symbols absent for a minute are forward-filled on price (no fabricated move) and get residual 0 /
flow 0 (neutral) for that minute.

Splits: held-out TIME (last 20% of dates). The same liquid universe spans train and test, so this is a
pure temporal OOS test (no symbol leakage concern — the structure must persist forward in time).

Run (inside fp-torch-gpu, -v fp_store_real:/store:ro):
  python experiments/gpu_leadlag/build_crosssection.py --store /store --universe <top300.json> \
      --out experiments/gpu_leadlag/out/crosssection.npz --max-days 379 --max-symbols 300
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl

RTH_START_MIN = 13 * 60 + 30  # 13:30 UTC
RTH_END_MIN = 20 * 60  # 20:00 UTC
SEQ_LEN = 390
MIN_BARS = 300


def load_universe(path: str) -> list[str]:
    data = json.loads(Path(path).read_text())
    return list(data["top300"]) if isinstance(data, dict) else list(data)


def day_grid(parquet: Path) -> pl.DataFrame | None:
    """Return one symbol-day on a regular RTH minute grid (close, volume) or None if too sparse."""
    frame = pl.read_parquet(parquet)
    frame = frame.with_columns(
        minute_of_day=(pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32))
    ).filter((pl.col("minute_of_day") >= RTH_START_MIN) & (pl.col("minute_of_day") < RTH_END_MIN))
    if frame.height < MIN_BARS:
        return None
    grid = pl.DataFrame({"minute_of_day": np.arange(RTH_START_MIN, RTH_END_MIN, dtype=np.int32)})
    merged = (
        grid.join(frame, on="minute_of_day", how="left")
        .sort("minute_of_day")
        .with_columns(close=pl.col("close").forward_fill(), volume=pl.col("volume").fill_null(0))
        .drop_nulls("close")
    )
    if merged.height < SEQ_LEN:
        return None
    return merged.select(["minute_of_day", "close", "volume"]).head(SEQ_LEN)


def build_day_matrices(
    bars_root: Path, symbols: list[str], date: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """For one date, return (resid [T x S], flow [T x S], market [T]) over present symbols, else None."""
    closes: list[np.ndarray] = []
    volumes: list[np.ndarray] = []
    present: list[int] = []
    for si, symbol in enumerate(symbols):
        parquet = bars_root / f"symbol={symbol}" / f"date={date}" / "data.parquet"
        if not parquet.exists():
            continue
        grid = day_grid(parquet)
        if grid is None or grid.height != SEQ_LEN:
            continue
        closes.append(grid["close"].to_numpy().astype(np.float64))
        volumes.append(grid["volume"].to_numpy().astype(np.float64))
        present.append(si)
    if len(present) < 50:  # need a real cross-section
        return None

    close_mat = np.stack(closes, axis=1)  # T x S_present
    vol_mat = np.stack(volumes, axis=1)
    ret = np.zeros_like(close_mat)
    ret[1:] = np.log(close_mat[1:] / close_mat[:-1])
    market = ret.mean(axis=1)  # T (equal-weight market proxy)
    resid = ret - market[:, None]  # T x S_present

    signed_flow = np.sign(ret) * np.log1p(vol_mat)
    flow_mean = signed_flow.mean(axis=1, keepdims=True)
    flow_std = signed_flow.std(axis=1, keepdims=True)
    flow_std = np.where(flow_std < 1e-8, 1.0, flow_std)
    flow = (signed_flow - flow_mean) / flow_std

    # Map back to the FULL universe width S (absent symbols -> 0 = neutral), so all days share columns.
    full_resid = np.zeros((SEQ_LEN, len(symbols)), dtype=np.float32)
    full_flow = np.zeros((SEQ_LEN, len(symbols)), dtype=np.float32)
    for col, si in enumerate(present):
        full_resid[:, si] = resid[:, col].astype(np.float32)
        full_flow[:, si] = flow[:, col].astype(np.float32)
    return full_resid, full_flow, market.astype(np.float32)


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
    sample_dir = bars_root / f"symbol={symbols[0]}"
    all_dates = sorted(path.name.split("=")[1] for path in sample_dir.glob("date=*"))[-args.max_days :]

    resid_days: list[np.ndarray] = []
    flow_days: list[np.ndarray] = []
    market_days: list[np.ndarray] = []
    kept_dates: list[str] = []
    for date in all_dates:
        result = build_day_matrices(bars_root, symbols, date)
        if result is None:
            continue
        resid, flow, market = result
        resid_days.append(resid)
        flow_days.append(flow)
        market_days.append(market)
        kept_dates.append(date)
        if len(kept_dates) % 20 == 0:
            print(f"{len(kept_dates)} days built (latest {date})")

    resid_tensor = np.stack(resid_days, axis=0)  # D x T x S
    flow_tensor = np.stack(flow_days, axis=0)
    market_tensor = np.stack(market_days, axis=0)  # D x T
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_path,
        resid=resid_tensor,
        flow=flow_tensor,
        market=market_tensor,
        symbols=np.array(symbols),
        dates=np.array(kept_dates),
    )
    print(
        f"\n{resid_tensor.shape[0]} days x {resid_tensor.shape[1]} minutes x {resid_tensor.shape[2]} symbols"
    )
    print(f"date range: {kept_dates[0]} -> {kept_dates[-1]}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
