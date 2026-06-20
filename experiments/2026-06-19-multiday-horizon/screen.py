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


def delisting_haircut(
    panel: pl.DataFrame, feat: str, sign: float, terminal: float, bps: float = 5.0
) -> dict[str, float]:
    """Survivorship stress: impute y_fwd_1w = terminal (-0.30 / -1.00) for disappeared names, re-score the
    L/S spread net of cost. If the edge dies, it was survivorship (the centerpiece gate)."""
    hc = panel.with_columns(
        pl.when(pl.col("disappeared") == 1).then(terminal).otherwise(pl.col("y_fwd_1w")).alias("y_fwd_1w")
    )
    return ls_net_of_cost(hc, feat, "y_fwd_1w", sign, bps)


def shuffle_z(panel: pl.DataFrame, feat: str, sign: float, observed_ic: float) -> float:
    """Permute the forward label WITHIN each weekly cross-section (breaks feature<->label, preserves the
    weekly structure); z of the observed mean IC vs the shuffled-null mean IC. Predict-zero is implicit:
    the null mean IC is ~0."""
    if np.isnan(observed_ic):
        return float("nan")
    rng = np.random.default_rng(SEED)
    df = panel.select(["friday", feat, "y_fwd_1w"]).drop_nulls()
    null = np.empty(N_SHUFFLE)
    for i in range(N_SHUFFLE):
        shuffled = df.with_columns(
            pl.col("y_fwd_1w").shuffle(seed=int(rng.integers(1 << 30))).over("friday")
        )
        mean_ic, _, _ = weekly_ic(shuffled, feat, "y_fwd_1w", sign)
        null[i] = mean_ic if not np.isnan(mean_ic) else 0.0
    return float((observed_ic - null.mean()) / (null.std(ddof=1) + 1e-12))


def control_collapse(
    panel: pl.DataFrame, feat: str, sign: float, controls: list[str]
) -> tuple[float, float]:
    """OWN-VOL/SIZE control (the #187/#197 lesson): the partial weekly rank-IC after regressing BOTH the
    (sign*feat) and the forward label on the controls within each week. Collapse = |partial IC| / |raw IC|;
    a reversal that is just vol-mean-reversion or a size tilt collapses to ~0."""
    cols = [feat, "y_fwd_1w", *controls]
    df = panel.select(["friday", *cols]).drop_nulls()
    raw_ics, par_ics = [], []
    for (_,), grp in df.group_by(["friday"]):
        if grp.height < 15:
            continue
        x = sign * grp[feat].to_numpy()
        y = grp["y_fwd_1w"].to_numpy()
        if np.std(x) < 1e-12 or np.std(y) < 1e-12:
            continue
        raw_ics.append(_spearman(x, y))
        Z = np.column_stack([np.ones(grp.height)] + [grp[c].to_numpy() for c in controls])
        rx = x - Z @ np.linalg.lstsq(Z, x, rcond=None)[0]
        ry = y - Z @ np.linalg.lstsq(Z, y, rcond=None)[0]
        if np.std(rx) > 1e-12 and np.std(ry) > 1e-12:
            par_ics.append(_spearman(rx, ry))
    if len(raw_ics) < 8 or len(par_ics) < 8:
        return float("nan"), float("nan")
    raw, par = float(np.mean(raw_ics)), float(np.mean(par_ics))
    return par, (abs(par) / abs(raw) if abs(raw) > 1e-9 else float("nan"))


def oos_split(panel: pl.DataFrame, feat: str, sign: float) -> str:
    early = panel.filter(pl.col("year") <= 2020)
    late = panel.filter(pl.col("year") >= 2021)
    ie, *_ = weekly_ic(early, feat, "y_fwd_1w", sign)
    il, *_ = weekly_ic(late, feat, "y_fwd_1w", sign)
    if np.isnan(ie) or np.isnan(il):
        return "n/a"
    return (
        f"consistent({ie:+.3f}/{il:+.3f})"
        if np.sign(ie) == np.sign(il) and abs(il) > 1e-3
        else f"FLIP({ie:+.3f}/{il:+.3f})"
    )


def by_fdr(pvals: list[float], q: float = 0.10) -> list[bool]:
    p = np.array(pvals)
    valid = ~np.isnan(p)
    m = int(valid.sum())
    if m == 0:
        return [False] * len(p)
    c_m = float(np.sum(1.0 / np.arange(1, m + 1)))
    order = np.argsort(np.where(valid, p, np.inf))
    crit = (np.arange(1, m + 1) / (m * c_m)) * q
    passed = np.where(p[order][:m] <= crit)[0]
    keep = np.zeros(len(p), dtype=bool)
    if len(passed):
        keep[order[: passed.max() + 1]] = True
    return keep.tolist()


def _two_sided_p(z: float) -> float:
    import math

    return (
        float(2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0)))))
        if not np.isnan(z)
        else float("nan")
    )


HYPOTHESES = (("H1_reversal", "rev_1w", -1.0), ("H2_lowvol", "vol_20d", -1.0))


def main() -> None:
    panel = pl.read_parquet(f"{OUT_DIR}/weekly_panel.parquet")
    panel = winsorize_weekly(panel, "y_fwd_1w")
    has_size = "size" in panel.columns
    controls = ["vol_20d"] + (["size"] if has_size else [])
    print(
        f"panel: {panel.height} obs, {panel['friday'].n_unique()} weeks, {panel['symbol'].n_unique()} syms, "
        f"disappeared={int(panel['disappeared'].sum())}, controls={controls}"
    )

    records = []
    for name, feat, sign in HYPOTHESES:
        mean_ic, nw_t, nweeks = weekly_ic(panel, feat, "y_fwd_1w", sign)
        z = shuffle_z(panel, feat, sign, mean_ic)
        # for the reversal control, do NOT partial out vol_20d against itself (the H2 feature IS vol)
        ctrl = [c for c in controls if c != feat]
        par_ic, collapse = control_collapse(panel, feat, sign, ctrl) if ctrl else (mean_ic, 1.0)
        oos = oos_split(panel, feat, sign)
        nc5 = ls_net_of_cost(panel, feat, "y_fwd_1w", sign, 5.0)
        nc10 = ls_net_of_cost(panel, feat, "y_fwd_1w", sign, 10.0)
        hc30 = delisting_haircut(panel, feat, sign, -0.30, 5.0)
        hc100 = delisting_haircut(panel, feat, sign, -1.00, 5.0)
        rec = {
            "hypothesis": name,
            "feature": feat,
            "sign": sign,
            "weeks": nweeks,
            "ic_mean": mean_ic,
            "ic_nw_t": nw_t,
            "shuffle_z": z,
            "partial_ic": par_ic,
            "collapse": collapse,
            "oos": oos,
            "net5_bps": nc5.get("net_bps", float("nan")),
            "net5_median_bps": nc5.get("median_net_bps", float("nan")),
            "net5_win": nc5.get("win", float("nan")),
            "net10_bps": nc10.get("net_bps", float("nan")),
            "net10_median_bps": nc10.get("median_net_bps", float("nan")),
            "hc30_net5_bps": hc30.get("net_bps", float("nan")),
            "hc30_median_bps": hc30.get("median_net_bps", float("nan")),
            "hc100_net5_bps": hc100.get("net_bps", float("nan")),
            "hc100_median_bps": hc100.get("median_net_bps", float("nan")),
            "hc100_win": hc100.get("win", float("nan")),
        }
        records.append(rec)
        print(f"\n=== {name} ({feat}, sign={sign:+.0f}) ===")
        print(f"  IC: mean={mean_ic:.4f} NW-t={nw_t:.2f} shuffle-z={z:.2f} ({nweeks} weeks)")
        print(f"  control {ctrl}: partial-IC={par_ic:.4f} collapse={collapse:.3f}")
        print(f"  OOS year-split (<=2020 / >=2021): {oos}")
        print(f"  net-of-cost  5bps: {nc5}")
        print(f"  net-of-cost 10bps: {nc10}")
        print(f"  delisting -30%  (net@5bps): {hc30}")
        print(f"  delisting -100% (net@5bps): {hc100}")

    survive = by_fdr([_two_sided_p(r["shuffle_z"]) for r in records])
    for rec, s in zip(records, survive):
        rec["fdr_survive"] = s
    pl.DataFrame(records).write_csv(f"{OUT_DIR}/screen_results.csv")
    print(f"\nWROTE {OUT_DIR}/screen_results.csv")
    print("\n=== BY-FDR (shuffle-z) ===")
    for rec in records:
        print(f"  {rec['hypothesis']}: shuffle-z={rec['shuffle_z']:.2f} fdr_survive={rec['fdr_survive']}")


if __name__ == "__main__":
    main()
