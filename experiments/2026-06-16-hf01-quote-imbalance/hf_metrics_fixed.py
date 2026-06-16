"""Fixed HF metric helpers — the corrected OOS + per-symbol-demean stages.

HF01's `oos_results.csv` and `demean_results.csv` came out all-NaN (n populated, IC NaN). Root cause:
the demean/OOS stages computed `ret_dm = ret - ret.mean().over("symbol")` and then took
`spearman_ic(x, y_dm)` over the WHOLE pooled frame, but the per-symbol demean is only meaningful WITHIN a
(symbol, date) cross-section if there ARE multiple symbols per timestamp — and HF01 pooled all symbols'
SECOND-bucket observations across different clocks, so a single symbol can dominate a "day" and the demean
math + the `argsort` ranking interacted with residual nulls/ties to yield non-finite results that the
overall-pool IC could not recover from. The robust fix: (1) compute the demean as an explicit group-mean
JOIN (null-safe), (2) drop any non-finite AFTER demean, (3) compute the IC PER (symbol, day) and average —
never one giant cross-symbol pool — so a single symbol's clock can't corrupt the rank-IC, and (4) guard
every stage with the same finite-mask `spearman_ic` HF01 already uses (which is correct). This mirrors the
WORKING ic_results stage, just adding a null-safe within-symbol demean.

Drop-in for HF02. `pooled` must have columns: the signal `col`, the forward-return `ret_col`, `date`,
`symbol`.
"""
from __future__ import annotations

import numpy as np
import polars as pl


def spearman_ic(x: np.ndarray, y: np.ndarray) -> float:
    """Rank-IC with a finite-mask guard (identical to HF01's working version)."""
    mask = np.isfinite(x) & np.isfinite(y)
    if int(mask.sum()) < 10:
        return np.nan
    xm, ym = x[mask], y[mask]
    rx = np.argsort(np.argsort(xm)).astype(float)
    ry = np.argsort(np.argsort(ym)).astype(float)
    rx -= rx.mean()
    ry -= ry.mean()
    denom = float(np.sqrt((rx**2).sum() * (ry**2).sum()))
    if denom == 0.0:
        return np.nan
    return float((rx * ry).sum() / denom)


def day_clustered_tstat(daily_ics: list[float]) -> tuple[float, float]:
    arr = np.array([v for v in daily_ics if np.isfinite(v)])
    if len(arr) < 3:
        return np.nan, np.nan
    mean_ic = float(arr.mean())
    sd = float(arr.std(ddof=1))
    if sd == 0.0:
        return mean_ic, np.nan
    return mean_ic, float(mean_ic / (sd / np.sqrt(len(arr))))


def demean_within_symbol(frame: pl.DataFrame, ret_col: str) -> pl.DataFrame:
    """Null-safe per-symbol demean via an explicit group-mean join (not `.over`), then drop non-finite.

    Returns the frame with a `ret_dm` column, rows with non-finite signal/ret_dm removed."""
    means = frame.group_by("symbol").agg(pl.col(ret_col).mean().alias("__sym_mean"))
    out = frame.join(means, on="symbol", how="left").with_columns(
        (pl.col(ret_col) - pl.col("__sym_mean")).alias("ret_dm")
    )
    return out.filter(pl.col("ret_dm").is_finite())


def per_symbol_day_ics(frame: pl.DataFrame, col: str, target: str) -> list[float]:
    """IC per (symbol, date) cell, never one giant cross-symbol pool — so one symbol's clock can't corrupt
    the rank-IC. A cell needs >=10 finite obs (spearman_ic guards it). Returns the list of cell ICs."""
    ics: list[float] = []
    for (sym, date), cell in frame.group_by(["symbol", "date"]):
        x = cell[col].to_numpy()
        y = cell[target].to_numpy()
        ic = spearman_ic(x, y)
        if np.isfinite(ic):
            ics.append(ic)
    return ics


def compute_demean_ic(pooled: pl.DataFrame, signals: list[str], windows: list[int], horizons: list[int]) -> list[dict]:
    """Corrected per-symbol-demean IC: demean within symbol (null-safe), then per-(symbol,date) IC averaged."""
    rows: list[dict] = []
    for sig in signals:
        for w in windows:
            col = f"{sig}_{w}"
            if col not in pooled.columns:
                continue
            for h_min in horizons:
                ret_col = f"fwd_{h_min}m"
                sub = pooled.select([col, ret_col, "date", "symbol"]).drop_nulls()
                if sub.height < 100:
                    continue
                sub = demean_within_symbol(sub, ret_col)
                ics = per_symbol_day_ics(sub, col, "ret_dm")
                mean_dm, t_dm = day_clustered_tstat(ics)
                rows.append({
                    "signal": sig, "w": w, "h_min": h_min,
                    "mean_ic_dm": round(mean_dm, 5) if np.isfinite(mean_dm) else None,
                    "t_dm": round(t_dm, 2) if np.isfinite(t_dm) else None,
                    "n_cells": len(ics),
                })
    return rows


def compute_oos_ic(pooled: pl.DataFrame, oos_dates: set, signals: list[str], windows: list[int], horizons: list[int]) -> list[dict]:
    """Corrected walk-forward OOS IC: restrict to OOS dates, demean within symbol on OOS data only (no
    cross-split leakage), per-(symbol,date) IC averaged."""
    rows: list[dict] = []
    oos = pooled.filter(pl.col("date").is_in(list(oos_dates)))
    for sig in signals:
        for w in windows:
            col = f"{sig}_{w}"
            if col not in oos.columns:
                continue
            for h_min in horizons:
                ret_col = f"fwd_{h_min}m"
                sub = oos.select([col, ret_col, "date", "symbol"]).drop_nulls()
                if sub.height < 50:
                    continue
                sub = demean_within_symbol(sub, ret_col)
                ics = per_symbol_day_ics(sub, col, "ret_dm")
                mean_oos, t_oos = day_clustered_tstat(ics)
                rows.append({
                    "signal": sig, "w": w, "h_min": h_min,
                    "mean_ic_oos": round(mean_oos, 5) if np.isfinite(mean_oos) else None,
                    "t_oos": round(t_oos, 2) if np.isfinite(t_oos) else None,
                    "n_cells": len(ics), "n_obs": sub.height,
                })
    return rows
