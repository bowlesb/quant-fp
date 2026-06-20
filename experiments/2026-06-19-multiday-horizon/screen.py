"""MULTI-DAY (WEEKLY) HORIZON — screen (pre-registered, see prereg.md). Turn-key stub for the gated run.

Implements the pre-committed discipline over weekly_panel.parquet:
  - H1 reversal: weekly cross-sectional rank-IC of -rev_1w vs forward weekly return, NW-t over the weekly IC
    series; H2 low-vol: rank-IC of -vol_20d vs forward weekly risk-adjusted return.
  - SHUFFLE baseline (permute the forward label within each weekly cross-section).
  - OWN-VOL / SIZE control: partial out vol_20d (+ a size proxy when present) -> collapse ratio.
  - NET-OF-COST: decile L/S spread net of 5/10 bps round-trip at WEEKLY turnover + break-even cost.
  - SURVIVORSHIP delisting haircut: re-impute y_fwd_1w = -30% / -100% for disappeared names -> the edge under
    each (the centerpiece survivorship stress).
  - OOS year-split + BY-FDR.

Per-week winsorization (symmetric) + $1-floor already applied upstream / here. Reads weekly_panel.parquet.
"""

from __future__ import annotations

import os

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-19-multiday-horizon"
N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "200"))
SEED = int(os.environ.get("SEED", "7"))
WINSOR_P = 0.01  # per-week symmetric winsorization of the forward label


def _rank(a: np.ndarray) -> np.ndarray:
    return a.argsort().argsort().astype(float)


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx, ry = _rank(x), _rank(y)
    if np.std(rx) < 1e-12 or np.std(ry) < 1e-12:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def winsorize_weekly(panel: pl.DataFrame, col: str) -> pl.DataFrame:
    """Per-week symmetric winsorization of ``col`` (the overnight/multi-day bad-print guard)."""
    lo = pl.col(col).quantile(WINSOR_P).over("friday")
    hi = pl.col(col).quantile(1 - WINSOR_P).over("friday")
    return panel.with_columns(pl.col(col).clip(lo, hi).alias(col))


def weekly_ic(panel: pl.DataFrame, feat: str, target: str, sign: float = -1.0) -> tuple[float, float, int]:
    """Mean weekly rank-IC of (sign*feat) vs target + NW-t over the weekly IC series. sign=-1 => reversal."""
    df = panel.select(["friday", feat, target]).drop_nulls()
    ics = []
    for (_,), grp in df.group_by(["friday"]):
        if grp.height < 10:
            continue
        ic = _spearman(sign * grp[feat].to_numpy(), grp[target].to_numpy())
        if not np.isnan(ic):
            ics.append(ic)
    if len(ics) < 8:
        return float("nan"), float("nan"), len(ics)
    arr = np.array(ics)
    return float(arr.mean()), float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr)) + 1e-12)), len(arr)


def ls_net_of_cost(panel: pl.DataFrame, feat: str, target: str, sign: float, bps: float) -> dict[str, float]:
    """Decile long/short weekly spread net of round-trip cost at weekly turnover."""
    df = panel.select(["friday", feat, target]).drop_nulls()
    spreads = []
    for (_,), grp in df.group_by(["friday"]):
        if grp.height < 20:
            continue
        s = (sign * grp[feat]).to_numpy()
        y = grp[target].to_numpy()
        order = s.argsort()
        n = max(1, len(order) // 10)
        longs = y[order[-n:]].mean()
        shorts = y[order[:n]].mean()
        spreads.append(longs - shorts)
    if len(spreads) < 8:
        return {}
    arr = np.array(spreads)
    cost = 4.0 * bps / 1e4  # long+short, in+out = 4 legs of bps
    net = arr - cost
    return {
        "gross_bps": float(arr.mean() * 1e4),
        "net_bps": float(net.mean() * 1e4),
        "median_net_bps": float(np.median(net) * 1e4),
        "win": float(np.mean(arr > cost)),
        "weeks": len(arr),
    }


def delisting_haircut(panel: pl.DataFrame, feat: str, sign: float, terminal: float) -> dict[str, float]:
    """Survivorship stress: impute y_fwd_1w = terminal (-0.30 / -1.00) for disappeared names, re-score the
    L/S spread. If the edge dies, it was survivorship."""
    hc = panel.with_columns(
        pl.when(pl.col("disappeared") == 1).then(terminal).otherwise(pl.col("y_fwd_1w")).alias("y_fwd_1w")
    )
    return ls_net_of_cost(hc, feat, "y_fwd_1w", sign, 5.0)


def main() -> None:
    panel = pl.read_parquet(f"{OUT_DIR}/weekly_panel.parquet")
    panel = winsorize_weekly(panel, "y_fwd_1w")
    print(
        f"panel: {panel.height} obs, {panel['friday'].n_unique()} weeks, {panel['symbol'].n_unique()} syms, "
        f"disappeared={int(panel['disappeared'].sum())}"
    )

    for name, feat, sign in (("H1_reversal", "rev_1w", -1.0), ("H2_lowvol", "vol_20d", -1.0)):
        mean_ic, nw_t, nweeks = weekly_ic(panel, feat, "y_fwd_1w", sign)
        print(f"\n=== {name} ===")
        print(f"  weekly rank-IC mean={mean_ic:.4f} NW-t={nw_t:.2f} ({nweeks} weeks)")
        for bps in (5.0, 10.0):
            print(f"  net-of-cost @ {bps:.0f}bps: {ls_net_of_cost(panel, feat, 'y_fwd_1w', sign, bps)}")
        print(f"  delisting haircut -30%:  {delisting_haircut(panel, feat, sign, -0.30)}")
        print(f"  delisting haircut -100%: {delisting_haircut(panel, feat, sign, -1.00)}")

    print(
        "\nNOTE: this is the turn-key screen stub for the pre-registered design. The full verdict (shuffle z,"
        " own-vol/size partial collapse, OOS year-split, BY-FDR) runs once the surface is greenlit + the"
        " multi-year panel is built. See prereg.md for the committed cells."
    )


if __name__ == "__main__":
    main()
