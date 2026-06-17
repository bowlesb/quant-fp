"""Day-ahead behavioral profiles + next-day targets (parity-true nightly-static-lookup substrate).

repr-2 embedded a symbol's WHOLE-history behavioral profile (static peer structure). This lane asks a
PREDICTIVE question on a SLOWER target than minute returns (which D3 + lead-lag proved is a null): does a
per-symbol TRAILING behavioral profile as of day T carry held-out-time structure for the day-(T+1) target?

Parity by construction: the profile at day T uses ONLY data through T (a trailing window). In production this
is a FROZEN NIGHTLY STATIC LOOKUP — each night recompute each symbol's coords from its trailing window,
freeze, next day reads the frozen value (identical in stream and backfill, no intraday state). So we build,
per (symbol, day T) with >= MIN_TRAIL trailing days:

PROFILE (features as of T, trailing window W, all backward-looking):
  - mean / std of trailing c2c, overnight, intraday returns
  - trailing realized vol (std of c2c) and downside semidev
  - trailing mean log dollar-vol and its trend (recent vs older half)
  - trailing return autocorr(1), and trailing momentum (sum c2c over W)
  - overnight/intraday share of variance
TARGETS (day T+1, forward — the thing we predict):
  - resid_ret_next  : next-day c2c return minus that day's cross-sectional mean (market-residualized)
  - realized_vol_next : |next-day c2c return| (a 1-day realized-vol proxy; daily vol clusters -> plausibly
                        predictable, unlike minute returns)
  - overnight_gap_next : |next-day overnight return| (gap magnitude)

We emit a tidy (symbol, date, profile..., target...) frame. Held-out-time split is by date downstream.

Run (CPU; venv or fp-ml):
  python experiments/gpu_dayahead/build_dayahead.py --bars <certify300_daily.parquet> \
      --out experiments/gpu_dayahead/out/dayahead.npz
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl

MIN_TRAIL = 60  # need >= 60 trailing days so the profile is stable
WINDOW = 60  # trailing window length for profile stats
PROFILE_NAMES = [
    "c2c_mean",
    "c2c_std",
    "overnight_mean",
    "overnight_std",
    "intraday_mean",
    "intraday_std",
    "downside_semidev",
    "logdvol_mean",
    "logdvol_trend",
    "ret_autocorr1",
    "momentum",
    "overnight_var_share",
]
TARGET_NAMES = ["resid_ret_next", "realized_vol_next", "overnight_gap_next"]


def compute_daily_returns(bars_path: str) -> pl.DataFrame:
    """Per (symbol, date): c2c, overnight, intraday log returns + log dollar-vol, sorted, nulls dropped."""
    frame = pl.read_parquet(bars_path).sort(["symbol", "date"])
    frame = frame.with_columns(
        prev_close=pl.col("rth_close").shift(1).over("symbol"),
    )
    frame = frame.with_columns(
        c2c=(pl.col("rth_close").log() - pl.col("prev_close").log()),
        overnight=(pl.col("rth_open").log() - pl.col("prev_close").log()),
        intraday=(pl.col("rth_close").log() - pl.col("rth_open").log()),
        logdvol=pl.col("dollar_vol").log(),
    ).drop_nulls(["c2c", "overnight", "intraday"])
    return frame.select(["symbol", "date", "c2c", "overnight", "intraday", "logdvol"])


def market_residual(frame: pl.DataFrame) -> pl.DataFrame:
    """Add the cross-sectional mean c2c per date and the residual (c2c - market) for the target."""
    market = frame.group_by("date").agg(market_c2c=pl.col("c2c").mean())
    return frame.join(market, on="date").with_columns(resid=pl.col("c2c") - pl.col("market_c2c"))


def build_profile_for_symbol(
    c2c: np.ndarray, overnight: np.ndarray, intraday: np.ndarray, logdvol: np.ndarray, t: int
) -> np.ndarray:
    """Trailing profile as of index t (inclusive), window WINDOW. All backward-looking (no look-ahead)."""
    lo = t - WINDOW + 1
    c2c_w = c2c[lo : t + 1]
    over_w = overnight[lo : t + 1]
    intra_w = intraday[lo : t + 1]
    dvol_w = logdvol[lo : t + 1]
    half = len(dvol_w) // 2
    downside = c2c_w[c2c_w < 0]
    centered = c2c_w - c2c_w.mean()
    autocorr_denom = (centered[:-1] ** 2).sum()
    autocorr = float((centered[1:] * centered[:-1]).sum() / autocorr_denom) if autocorr_denom > 1e-12 else 0.0
    over_var = over_w.var()
    intra_var = intra_w.var()
    var_share = float(over_var / (over_var + intra_var)) if (over_var + intra_var) > 1e-12 else 0.5
    return np.array(
        [
            c2c_w.mean(),
            c2c_w.std(),
            over_w.mean(),
            over_w.std(),
            intra_w.mean(),
            intra_w.std(),
            float(np.sqrt((downside**2).mean())) if len(downside) else 0.0,
            dvol_w.mean(),
            float(dvol_w[half:].mean() - dvol_w[:half].mean()),
            autocorr,
            float(c2c_w.sum()),
            var_share,
        ],
        dtype=np.float32,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    frame = market_residual(compute_daily_returns(args.bars))
    rows_profile: list[np.ndarray] = []
    rows_target: list[np.ndarray] = []
    rows_symbol: list[str] = []
    rows_date: list[str] = []

    for symbol, sub in frame.sort(["symbol", "date"]).group_by("symbol", maintain_order=True):
        sym = symbol[0] if isinstance(symbol, tuple) else symbol
        if sub.height < MIN_TRAIL + 1:
            continue
        c2c = sub["c2c"].to_numpy().astype(np.float64)
        overnight = sub["overnight"].to_numpy().astype(np.float64)
        intraday = sub["intraday"].to_numpy().astype(np.float64)
        logdvol = sub["logdvol"].to_numpy().astype(np.float64)
        resid = sub["resid"].to_numpy().astype(np.float64)
        dates = sub["date"].to_numpy()
        # profile at T (uses <=T), target at T+1 (forward). T ranges over [WINDOW-1, n-2].
        for t in range(WINDOW - 1, len(c2c) - 1):
            profile = build_profile_for_symbol(c2c, overnight, intraday, logdvol, t)
            target = np.array([resid[t + 1], abs(c2c[t + 1]), abs(overnight[t + 1])], dtype=np.float32)
            rows_profile.append(profile)
            rows_target.append(target)
            rows_symbol.append(str(sym))
            rows_date.append(str(dates[t + 1]))

    profile_mat = np.stack(rows_profile, axis=0)
    target_mat = np.stack(rows_target, axis=0)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_path,
        profile=profile_mat,
        target=target_mat,
        symbols=np.array(rows_symbol),
        dates=np.array(rows_date),
        profile_names=np.array(PROFILE_NAMES),
        target_names=np.array(TARGET_NAMES),
    )
    summary = {
        "n_rows": int(profile_mat.shape[0]),
        "n_profile_features": int(profile_mat.shape[1]),
        "n_symbols": int(len(set(rows_symbol))),
        "date_range": [min(rows_date), max(rows_date)],
        "targets": TARGET_NAMES,
    }
    print(json.dumps(summary, indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
