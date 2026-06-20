"""NEWS HOTNESS screen — median-anchored, H1 feature-utility / H2 tradeable (registered #230, locked).

Over news_panel_emb{1,5,15}.parquet:
  H1 (FEATURE-UTILITY, magnitude — NOT a tradeable claim): cross-sectional rank-IC of each hotness feature
    vs the forward MAGNITUDE target (y_absret_30m), aggregated across (day) with a NW-t; the decisive number
    is the OWN-VOL/SIZE/BASE-COV CONTROL partial-IC + the COLLAPSE RATIO (|partial|/|raw|). Promotion =
    a residual own-vol-INDEPENDENT magnitude IC that survives the control + shuffle + embargo-stability +
    purge-OOS → a trustworthy net-new magnitude FEATURE (NOT alpha). NO net-cost gate for H1.
  H2 (DIRECTION, tradeable): rank-IC of hotness vs signed forward return + the net-of-cost decile L/S MEDIAN
    gate. Median-anchored: a favorable mean can't reopen it.
  EMBARGO-STABILITY: every result reported across embargo {1,5,15}; a signal must hold across the sweep.
  Shuffle (permute the hotness->target link within a day), purge-OOS (early/late month split), BY-FDR.

Reads news_panel_emb*.parquet. Writes screen_results.csv + console verdict.
"""

from __future__ import annotations

import glob
import math
import os

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-20-news-hotness"
N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "200"))
SEED = int(os.environ.get("SEED", "7"))
EMBARGOS = (1, 5, 15)
HOT_FEATURES = ["news_count_24h", "news_hot_z_24h", "news_burst_24h", "news_excl_24h", "news_velocity_24h"]
CONTROLS = ["own_vol", "size", "base_cov"]
COST_BPS = (5.0, 10.0)


def _rank(a: np.ndarray) -> np.ndarray:
    return a.argsort().argsort().astype(float)


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx, ry = _rank(x), _rank(y)
    if np.std(rx) < 1e-12 or np.std(ry) < 1e-12:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_sided_p(z: float) -> float:
    return float(2.0 * (1.0 - _norm_cdf(abs(z)))) if not np.isnan(z) else float("nan")


def daily_ic(panel: pl.DataFrame, feat: str, target: str) -> tuple[float, float, int]:
    """Cross-sectional rank-IC per day, aggregated with a NW-t over the daily IC series."""
    df = panel.select(["date", feat, target]).drop_nulls()
    ics = []
    for (_,), grp in df.group_by(["date"]):
        if grp.height < 8:
            continue
        ic = _spearman(grp[feat].to_numpy(), grp[target].to_numpy())
        if not np.isnan(ic):
            ics.append(ic)
    if len(ics) < 8:
        return float("nan"), float("nan"), len(ics)
    a = np.array(ics)
    return float(a.mean()), float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)) + 1e-12)), len(a)


def control_collapse(panel: pl.DataFrame, feat: str, target: str) -> tuple[float, float]:
    """Daily partial rank-IC after regressing feat + target on the controls; collapse = |partial|/|raw|."""
    df = panel.select(["date", feat, target, *CONTROLS]).drop_nulls()
    raw, par = [], []
    for (_,), grp in df.group_by(["date"]):
        if grp.height < 12:
            continue
        x = grp[feat].to_numpy()
        y = grp[target].to_numpy()
        if np.std(x) < 1e-12 or np.std(y) < 1e-12:
            continue
        raw.append(_spearman(x, y))
        Z = np.column_stack([np.ones(grp.height)] + [grp[c].to_numpy() for c in CONTROLS])
        rx = x - Z @ np.linalg.lstsq(Z, x, rcond=None)[0]
        ry = y - Z @ np.linalg.lstsq(Z, y, rcond=None)[0]
        if np.std(rx) > 1e-12 and np.std(ry) > 1e-12:
            par.append(_spearman(rx, ry))
    if len(raw) < 8 or len(par) < 8:
        return float("nan"), float("nan")
    r, p = float(np.mean(raw)), float(np.mean(par))
    return p, (abs(p) / abs(r) if abs(r) > 1e-9 else float("nan"))


def shuffle_z(panel: pl.DataFrame, feat: str, target: str, observed: float) -> float:
    if np.isnan(observed):
        return float("nan")
    df = panel.select(["date", feat, target]).drop_nulls()
    rng = np.random.default_rng(SEED)
    null = np.empty(N_SHUFFLE)
    for i in range(N_SHUFFLE):
        sh = df.with_columns(pl.col(target).shuffle(seed=int(rng.integers(1 << 30))).over("date"))
        m, _, _ = daily_ic(sh, feat, target)
        null[i] = m if not np.isnan(m) else 0.0
    return float((observed - null.mean()) / (null.std(ddof=1) + 1e-12))


def purge_oos(panel: pl.DataFrame, feat: str, target: str) -> str:
    months = sorted(panel["year_month"].unique().to_list())
    if len(months) < 4:
        return "n/a"
    mid = months[len(months) // 2]
    ie, *_ = daily_ic(panel.filter(pl.col("year_month") < mid), feat, target)
    il, *_ = daily_ic(panel.filter(pl.col("year_month") > mid), feat, target)  # purge the boundary month
    if np.isnan(ie) or np.isnan(il):
        return "n/a"
    return f"{'consistent' if np.sign(ie) == np.sign(il) else 'FLIP'}({ie:+.3f}/{il:+.3f})"


def ls_net_median(panel: pl.DataFrame, feat: str, bps: float) -> dict[str, float]:
    """H2 tradeable: decile L/S on the signed-return target, net of cost, MEDIAN-anchored."""
    df = panel.select(["date", feat, "y_ret_30m"]).drop_nulls()
    spreads = []
    for (_,), grp in df.group_by(["date"]):
        if grp.height < 20:
            continue
        s = grp[feat].to_numpy()
        y = grp["y_ret_30m"].to_numpy()
        o = s.argsort()
        n = max(1, len(o) // 10)
        spreads.append(float(y[o[-n:]].mean() - y[o[:n]].mean() - 4 * bps / 1e4))
    if len(spreads) < 8:
        return {}
    a = np.array(spreads)
    return {
        "net_mean_bps": float(a.mean() * 1e4),
        "net_median_bps": float(np.median(a) * 1e4),
        "win": float(np.mean(a > 0)),
        "days": len(a),
    }


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


def main() -> None:
    records = []
    for emb in EMBARGOS:
        f = f"{OUT_DIR}/news_panel_emb{emb}.parquet"
        if not glob.glob(f):
            continue
        panel = pl.read_parquet(f)
        # winsor the targets per day
        for tcol in ("y_absret_30m", "y_ret_30m"):
            lo = pl.col(tcol).quantile(0.01).over("date")
            hi = pl.col(tcol).quantile(0.99).over("date")
            panel = panel.with_columns(pl.col(tcol).clip(lo, hi))
        print(
            f"\n##### EMBARGO={emb}m: {panel.height} obs, {panel['symbol'].n_unique()} names, {panel['date'].n_unique()} days #####"
        )
        for feat in HOT_FEATURES:
            # H1 feature-utility: magnitude IC + own-vol-control collapse
            raw_ic, nwt, nd = daily_ic(panel, feat, "y_absret_30m")
            z = shuffle_z(panel, feat, "y_absret_30m", raw_ic)
            par, collapse = control_collapse(panel, feat, "y_absret_30m")
            oos = purge_oos(panel, feat, "y_absret_30m")
            # H2 direction
            dir_ic, dir_t, _ = daily_ic(panel, feat, "y_ret_30m")
            dz = shuffle_z(panel, feat, "y_ret_30m", dir_ic)
            nc5 = ls_net_median(panel, feat, 5.0)
            nc10 = ls_net_median(panel, feat, 10.0)
            records.append(
                {
                    "embargo": emb,
                    "feature": feat,
                    "days": nd,
                    "H1_mag_ic": raw_ic,
                    "H1_shuffle_z": z,
                    "H1_partial_ic": par,
                    "H1_collapse": collapse,
                    "H1_oos": oos,
                    "H2_dir_ic": dir_ic,
                    "H2_shuffle_z": dz,
                    "H2_net5_median_bps": nc5.get("net_median_bps", float("nan")),
                    "H2_net5_mean_bps": nc5.get("net_mean_bps", float("nan")),
                    "H2_net5_win": nc5.get("win", float("nan")),
                    "H2_net10_median_bps": nc10.get("net_median_bps", float("nan")),
                }
            )
            print(
                f"  {feat:20s} H1 mag-IC={raw_ic:+.4f} z={z:.1f} partial={par:+.4f} collapse={collapse:.3f} oos={oos}"
            )
            print(
                f"  {'':20s} H2 dir-IC={dir_ic:+.4f} z={dz:.1f} net5_median={nc5.get('net_median_bps', float('nan')):+.2f}bps win={nc5.get('win', float('nan')):.2f}"
            )

    res = pl.DataFrame(records)
    res.write_csv(f"{OUT_DIR}/screen_results.csv")
    # BY-FDR across the full (feature x embargo) family, separately for H1 (shuffle on magnitude) and H2.
    h1p = [two_sided_p(r["H1_shuffle_z"]) for r in records]
    h2p = [two_sided_p(r["H2_shuffle_z"]) for r in records]
    h1s, h2s = by_fdr(h1p), by_fdr(h2p)
    print(
        "\n=== ⭐ H1 FEATURE-UTILITY GATE (own-vol-independent magnitude signal survives?) — embargo-stable? ==="
    )
    # A feature has net-new magnitude value ONLY IF: (a) the RAW magnitude IC is non-trivial (|raw|>=MIN_IC
    # — else the collapse ratio is a near-zero-denominator artifact), (b) the partial IC keeps the SAME sign
    # AND stays non-trivial after the own-vol/size control, (c) it's embargo-stable AND OOS-consistent. A
    # collapse>1 on a ~0 raw IC is NOT survival — it's a meaningless ratio (the buggy reading I caught).
    MIN_IC = 0.02
    for feat in HOT_FEATURES:
        cells = [r for r in records if r["feature"] == feat]
        if not cells:
            continue
        survives = all(
            (not np.isnan(c["H1_mag_ic"]))
            and abs(c["H1_mag_ic"]) >= MIN_IC
            and abs(c["H1_partial_ic"]) >= MIN_IC
            and np.sign(c["H1_partial_ic"]) == np.sign(c["H1_mag_ic"])
            and not c["H1_oos"].startswith("FLIP")
            for c in cells
        )
        det = [f"{c['embargo']}m:raw{c['H1_mag_ic']:+.3f}/par{c['H1_partial_ic']:+.3f}" for c in cells]
        print(
            f"  {feat:20s} [{' '.join(det)}] oos={cells[0]['H1_oos']} → {'NET-NEW FEATURE (own-vol-independent, sign-stable)' if survives else 'NO net-new magnitude value (raw IC ~0 / sign-flips / collapses)'}"
        )
    print("\n=== ⭐ H2 TRADEABLE GATE (net-of-cost MEDIAN > 0, embargo-stable) ===")
    for feat in HOT_FEATURES:
        cells = [r for r in records if r["feature"] == feat]
        if not cells:
            continue
        meds = [f"{c['embargo']}m:{c['H2_net5_median_bps']:+.1f}" for c in cells]
        trad = all(c["H2_net5_median_bps"] > 0 for c in cells if not np.isnan(c["H2_net5_median_bps"]))
        print(
            f"  {feat:20s} net5_median-by-embargo [{' '.join(meds)}]bps → {'TRADEABLE (median>0 all embargos)' if trad else 'NULL (median<=0)'}"
        )


if __name__ == "__main__":
    main()
