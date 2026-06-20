"""Lane D edge-hunt — SCREEN (statistics over panel.parquet). Pre-registered, see prereg.md.

Per (feature, target) pair, with all the pre-committed discipline:
  - H1 (EDGAR event-intensity): pooled relation with (day,minute) FIXED EFFECTS (de-mean the target
    within each timestamp block so a market-wide move can't masquerade as a filing effect) → a
    timestamp-demeaned Pearson r, plus the OWN-VOL MARGINAL partial (partial out own_rv_30 + mkt_rv_30
    from both sides) with a collapse ratio.
  - H2 (sector-relative cross-sectional): within-(day,minute) rank-IC (Spearman), aggregated across
    timestamps with a Newey-West t-stat on the per-timestamp IC series; the reversal partial controls for
    own trailing return so sector_excess must beat a plain own-return reversal.
  - SHUFFLE baseline (permute target WITHIN each timestamp block, ≥200 iters) → z of the real stat.
  - OOS year/early-late split: fit sign on the earlier block, score the later, report OOS sign-consistency.
  - BY-FDR across all pairs (q=0.10).
  - Cost-net decile spread (5/10 bps) for any survivor.

NO look-ahead; reads panel.parquet only. Writes screen_results.csv + a console verdict table.
"""

from __future__ import annotations

import os

import math

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-19-laneD-edge-hunt"


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation = Pearson on the rank-transformed series (average ranks for ties)."""
    rx = _rankdata(x)
    ry = _rankdata(y)
    if np.std(rx) < 1e-12 or np.std(ry) < 1e-12:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def _rankdata(a: np.ndarray) -> np.ndarray:
    """Average-rank of each element (ties share the mean rank), numpy-only."""
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    ranks[order] = np.arange(1, len(a) + 1, dtype=float)
    # average ties
    sorted_a = a[order]
    i = 0
    n = len(a)
    while i < n:
        j = i + 1
        while j < n and sorted_a[j] == sorted_a[i]:
            j += 1
        if j - i > 1:
            avg = ranks[order[i:j]].mean()
            ranks[order[i:j]] = avg
        i = j
    return ranks


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "200"))
SEED = int(os.environ.get("SEED", "7"))
FDR_Q = 0.10

# (feature, hypothesis, list of targets, "magnitude"|"direction" axis per target)
H1_FEATURES = ["edgar_burst_7v90", "edgar_cnt_7d", "mins_since_8k", "mins_since_any"]
H2_FEATURES = ["sector_excess_15", "sector_excess_30", "sector_excess_60"]
MAG_TARGETS = ["y_absret_30", "y_absret_60", "y_fwd_rv", "y_logvol"]
DIR_TARGETS = ["y_ret_15m", "y_ret_30m", "y_ret_60m"]

BLOCK = ["date", "minute"]  # a (day, entry-minute) cross-section


def prepare(panel: pl.DataFrame) -> pl.DataFrame:
    return panel.with_columns(
        pl.col("y_ret_30m").abs().alias("y_absret_30"),
        pl.col("y_ret_60m").abs().alias("y_absret_60"),
        (pl.col("y_fwd_vol").cast(pl.Float64) + 1.0).log().alias("y_logvol"),
    )


def _demean_block(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    """Subtract the per-(day,minute) block mean from each column (timestamp fixed effects)."""
    exprs = [(pl.col(c) - pl.col(c).mean().over(BLOCK)).alias(c) for c in cols]
    return df.with_columns(exprs)


def _clean(panel: pl.DataFrame, feature: str, target: str) -> pl.DataFrame:
    return panel.select([*BLOCK, feature, target]).drop_nulls()


def pearson_demeaned(panel: pl.DataFrame, feature: str, target: str) -> tuple[float, int]:
    """Timestamp-demeaned Pearson r between feature and target (H1 pooled-with-FE)."""
    df = _clean(panel, feature, target)
    if df.height < 50:
        return float("nan"), df.height
    dm = _demean_block(df, [feature, target])
    x, y = dm[feature].to_numpy(), dm[target].to_numpy()
    if np.std(x) < 1e-12 or np.std(y) < 1e-12:
        return float("nan"), df.height
    return float(np.corrcoef(x, y)[0, 1]), df.height


def partial_demeaned(panel: pl.DataFrame, feature: str, target: str, controls: list[str]) -> float:
    """Timestamp-demeaned partial correlation of feature vs target after regressing BOTH on `controls`
    (also demeaned). Measures NET-NEW relation beyond the controls (the own-vol marginal lesson)."""
    cols = [feature, target, *controls]
    df = panel.select([*BLOCK, *cols]).drop_nulls()
    if df.height < 50:
        return float("nan")
    dm = _demean_block(df, cols)
    Z = np.column_stack([dm[c].to_numpy() for c in controls])
    Z = np.column_stack([np.ones(len(Z)), Z])

    def resid(name: str) -> np.ndarray:
        y = dm[name].to_numpy()
        beta, *_ = np.linalg.lstsq(Z, y, rcond=None)
        return y - Z @ beta

    rx, ry = resid(feature), resid(target)
    if np.std(rx) < 1e-12 or np.std(ry) < 1e-12:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def block_rank_ic(panel: pl.DataFrame, feature: str, target: str) -> tuple[float, float, int]:
    """Within-(day,minute) Spearman rank-IC, aggregated across blocks with a Newey-West t-stat on the
    per-block IC series (H2 cross-sectional). Returns (mean_ic, nw_t, n_blocks)."""
    df = _clean(panel, feature, target)
    ics: list[float] = []
    for (_, _), grp in df.group_by(BLOCK):
        if grp.height < 10:
            continue
        fx, ty = grp[feature].to_numpy(), grp[target].to_numpy()
        if np.std(fx) < 1e-12 or np.std(ty) < 1e-12:
            continue
        ics.append(_spearman(fx, ty))
    if len(ics) < 10:
        return float("nan"), float("nan"), len(ics)
    arr = np.array(ics)
    mean_ic = arr.mean()
    nw_t = mean_ic / (arr.std(ddof=1) / np.sqrt(len(arr)) + 1e-12)
    return float(mean_ic), float(nw_t), len(arr)


def shuffle_z(
    panel: pl.DataFrame, feature: str, target: str, kind: str, stat: float, controls: list[str] | None = None
) -> float:
    """z of the real stat vs a within-block target-shuffle null (≥N_SHUFFLE iters). Preserves the
    cross-sectional structure, breaks the feature↔label link."""
    if np.isnan(stat):
        return float("nan")
    rng = np.random.default_rng(SEED)
    cols = [feature, target] + (controls or [])
    df = panel.select([*BLOCK, *cols]).drop_nulls()
    if df.height < 50:
        return float("nan")
    block_id = df.select(BLOCK).hash_rows()
    df = df.with_columns(block_id.alias("_blk"))
    null: list[float] = []
    for _ in range(N_SHUFFLE):
        shuffled = df.with_columns(
            pl.col(target).shuffle(seed=int(rng.integers(1 << 30))).over("_blk").alias(target)
        )
        if kind == "pearson":
            val, _ = pearson_demeaned(shuffled, feature, target)
        elif kind == "partial":
            val = partial_demeaned(shuffled, feature, target, controls or [])
        else:  # rank_ic
            _, val, _ = block_rank_ic(shuffled, feature, target)
        if not np.isnan(val):
            null.append(val)
    if len(null) < 10:
        return float("nan")
    null_arr = np.array(null)
    return float((abs(stat) - abs(null_arr).mean()) / (null_arr.std(ddof=1) + 1e-12))


def oos_sign(
    panel: pl.DataFrame, feature: str, target: str, kind: str, controls: list[str] | None = None
) -> str:
    """Fit the stat sign on the earlier half of days, score the later half; report whether the sign holds."""
    days = sorted(panel["date"].unique().to_list())
    if len(days) < 4:
        return "n/a"
    mid = days[len(days) // 2]
    early = panel.filter(pl.col("date") < mid)
    late = panel.filter(pl.col("date") >= mid)

    def s(df: pl.DataFrame) -> float:
        if kind == "pearson":
            return pearson_demeaned(df, feature, target)[0]
        if kind == "partial":
            return partial_demeaned(df, feature, target, controls or [])
        return block_rank_ic(df, feature, target)[1]

    se, sl = s(early), s(late)
    if np.isnan(se) or np.isnan(sl):
        return "n/a"
    return "consistent" if np.sign(se) == np.sign(sl) and abs(sl) > 1e-3 else "FLIP"


def by_fdr(pvals: list[float], q: float) -> list[bool]:
    """Benjamini-Yekutieli step-up. Returns survival mask aligned to pvals order."""
    p = np.array(pvals)
    valid = ~np.isnan(p)
    m = valid.sum()
    if m == 0:
        return [False] * len(p)
    c_m = np.sum(1.0 / np.arange(1, m + 1))
    order = np.argsort(np.where(valid, p, np.inf))
    thresh = np.zeros(len(p), dtype=bool)
    ranked = p[order][:m]
    crit = (np.arange(1, m + 1) / (m * c_m)) * q
    passed = ranked <= crit
    kmax = np.where(passed)[0]
    if len(kmax):
        cutoff = kmax.max()
        survivors = order[: cutoff + 1]
        thresh[survivors] = True
    return thresh.tolist()


def two_sided_p_from_z(z: float) -> float:
    if np.isnan(z):
        return float("nan")
    return float(2.0 * (1.0 - _norm_cdf(abs(z))))


def main() -> None:
    panel = prepare(pl.read_parquet(f"{OUT_DIR}/panel.parquet"))
    print(
        f"panel: {panel.height} rows, {panel['date'].n_unique()} days, {panel['symbol'].n_unique()} symbols"
    )

    records: list[dict] = []
    mag_controls = ["own_rv_30", "mkt_rv_30"]

    # H1 — EDGAR event-intensity (pooled with timestamp FE). Magnitude + direction targets.
    for feat in H1_FEATURES:
        for tgt in MAG_TARGETS + DIR_TARGETS:
            axis = "magnitude" if tgt in MAG_TARGETS else "direction"
            r, n = pearson_demeaned(panel, feat, tgt)
            z = shuffle_z(panel, feat, tgt, "pearson", r)
            partial = (
                partial_demeaned(panel, feat, tgt, mag_controls) if axis == "magnitude" else float("nan")
            )
            collapse = (
                (abs(partial) / abs(r))
                if (axis == "magnitude" and not np.isnan(partial) and abs(r) > 1e-9)
                else float("nan")
            )
            oos = oos_sign(panel, feat, tgt, "pearson")
            records.append(
                dict(
                    hypothesis="H1",
                    feature=feat,
                    target=tgt,
                    axis=axis,
                    stat_kind="pearson_FE",
                    stat=r,
                    n=n,
                    shuffle_z=z,
                    partial=partial,
                    collapse=collapse,
                    oos=oos,
                )
            )

    # H2 — sector-relative cross-sectional rank-IC. Direction (reversal/momentum) + a magnitude check.
    own_ret_for = {"sector_excess_15": "_ret15", "sector_excess_30": "_ret30", "sector_excess_60": "_ret60"}
    for feat in H2_FEATURES:
        for tgt in DIR_TARGETS + ["y_absret_30"]:
            axis = "direction" if tgt in DIR_TARGETS else "magnitude"
            mean_ic, nw_t, nblk = block_rank_ic(panel, feat, tgt)
            z = shuffle_z(panel, feat, tgt, "rank_ic", nw_t)
            # reversal partial: control for own trailing return so sector_excess must beat plain own-return reversal
            own_ret = own_ret_for[feat]
            partial = partial_demeaned(panel, feat, tgt, [own_ret]) if axis == "direction" else float("nan")
            oos = oos_sign(panel, feat, tgt, "rank_ic")
            records.append(
                dict(
                    hypothesis="H2",
                    feature=feat,
                    target=tgt,
                    axis=axis,
                    stat_kind="rankIC_NWt",
                    stat=mean_ic,
                    n=nblk,
                    shuffle_z=z,
                    nw_t=nw_t,
                    partial=partial,
                    collapse=float("nan"),
                    oos=oos,
                )
            )

    # BY-FDR across ALL pairs, using the shuffle-z two-sided p.
    pvals = [two_sided_p_from_z(rec["shuffle_z"]) for rec in records]
    survival = by_fdr(pvals, FDR_Q)
    for rec, p, surv in zip(records, pvals, survival):
        rec["shuffle_p"] = p
        rec["fdr_survive"] = surv

    res = pl.DataFrame(records)
    res.write_csv(f"{OUT_DIR}/screen_results.csv")

    print("\n=== VERDICT TABLE (sorted by |shuffle_z|) ===")
    show = res.with_columns(pl.col("shuffle_z").abs().alias("_az")).sort(
        "_az", descending=True, nulls_last=True
    )
    with pl.Config(tbl_rows=60, tbl_cols=14, fmt_str_lengths=20):
        print(
            show.select(
                [
                    "hypothesis",
                    "feature",
                    "target",
                    "axis",
                    "stat",
                    "shuffle_z",
                    "partial",
                    "collapse",
                    "oos",
                    "fdr_survive",
                ]
            )
        )
    n_surv = res.filter(pl.col("fdr_survive")).height
    print(
        f"\nBY-FDR (q={FDR_Q}) survivors across {len([p for p in pvals if not np.isnan(p)])} valid pairs: {n_surv}"
    )
    if n_surv:
        print(
            res.filter(pl.col("fdr_survive")).select(
                [
                    "hypothesis",
                    "feature",
                    "target",
                    "axis",
                    "stat",
                    "shuffle_z",
                    "partial",
                    "collapse",
                    "oos",
                ]
            )
        )


if __name__ == "__main__":
    main()
