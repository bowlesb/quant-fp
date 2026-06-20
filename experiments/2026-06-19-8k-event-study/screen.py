"""8-K EVENT-STUDY — SCREEN (statistics over events.parquet). Pre-registered, see prereg.md.

Per (hypothesis, window, regime) cell, with all the pre-committed discipline:
  - H1 VOLUME, H2 MAGNITUDE (absret/rv/range), H3 DIRECTION (signed ret).
  - The abnormality stat for an own-normalized RATIO (vol/absret/rv/range vs the name's own baseline) is
    the median log-ratio (robust); the event is abnormal if it is shifted POSITIVE.
  - SHUFFLE baseline: the own-baseline IS the within-name null already, so the event-vs-null test is a
    one-sample test of log(ratio) vs 0; we ALSO report a sign-flip permutation z (randomly flip the sign of
    each event's log-ratio -> the |mean| under no-effect) as the event-timestamp shuffle analogue.
  - OWN-VOL CONTROL COLLAPSE (#187 lesson): regress the event log-ratio on the name's baseline vol level;
    the residual median = the part NOT explained by "this is a volatile name". Collapse = residual/raw.
  - OOS: 2018-2021 vs 2022-2025 sign + magnitude consistency.
  - NET-OF-COST (H2 tradeable test): is the realized abnormal |move| larger than the straddle-cost proxy
    (the own-baseline expected |move| = what an ATM straddle would cost) + round-trip bps?
  - BY-FDR (q=0.10) across all cells.

Reads events.parquet only. Writes screen_results.csv + a console verdict table.
"""

from __future__ import annotations

import math
import os

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-19-8k-event-study"
N_PERM = int(os.environ.get("N_PERM", "2000"))
SEED = int(os.environ.get("SEED", "7"))
FDR_Q = 0.10
WINDOWS = (5, 15, 30, 60)
COST_BPS = (5.0, 10.0)  # round-trip cost per side for the H2 net-of-cost leg

# ratio-stat (own-normalized) features by hypothesis
H1 = ["vol"]
H2 = ["absret", "rv", "range"]


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_sided_p(z: float) -> float:
    return float(2.0 * (1.0 - _norm_cdf(abs(z)))) if not np.isnan(z) else float("nan")


def log_ratio(panel: pl.DataFrame, stat: str, window: int) -> np.ndarray:
    """log of the own-normalized abnormality ratio for one (stat, window), finite values only."""
    col = f"{stat}_abn_{window}"
    if col not in panel.columns:
        return np.array([])
    vals = panel[col].drop_nulls().to_numpy()
    vals = vals[(vals > 0) & np.isfinite(vals)]
    return np.log(vals)


def sign_flip_z(values: np.ndarray, observed_mean: float) -> float:
    """Event-shuffle analogue: under no-effect each event's log-ratio is equally likely +/-, so the null
    mean is symmetric about 0. z = observed mean vs the sign-flip permutation null of the mean."""
    if len(values) < 20:
        return float("nan")
    rng = np.random.default_rng(SEED)
    null = np.empty(N_PERM)
    for i in range(N_PERM):
        signs = rng.choice([-1.0, 1.0], size=len(values))
        null[i] = float(np.mean(signs * values))
    return float((observed_mean - null.mean()) / (null.std(ddof=1) + 1e-12))


def own_vol_collapse(panel: pl.DataFrame, stat: str, window: int) -> tuple[float, float]:
    """Collapse ratio: median log-ratio after partialling out the name's baseline vol level (rv_raw at the
    same window) vs the raw median. <~0.3 => the abnormality was 'volatile name', not the event."""
    col = f"{stat}_abn_{window}"
    base_vol = f"rv_raw_{window}"
    if col not in panel.columns or base_vol not in panel.columns:
        return float("nan"), float("nan")
    df = panel.select([col, base_vol]).drop_nulls()
    df = df.filter(
        (pl.col(col) > 0) & pl.col(col).is_finite() & (pl.col(base_vol) > 0) & pl.col(base_vol).is_finite()
    )
    if df.height < 30:
        return float("nan"), float("nan")
    y = np.log(df[col].to_numpy())
    x = np.log(df[base_vol].to_numpy())
    good = np.isfinite(y) & np.isfinite(x)
    y, x = y[good], x[good]
    if len(y) < 30 or np.std(x) < 1e-12:
        return float("nan"), float("nan")
    raw = float(np.median(y))
    Z = np.column_stack([np.ones(len(x)), x])
    beta, *_ = np.linalg.lstsq(Z, y, rcond=None)
    resid = y - Z @ beta
    resid_med = float(np.median(resid))
    collapse = abs(resid_med) / abs(raw) if abs(raw) > 1e-9 else float("nan")
    return resid_med, collapse


def oos_consistent(panel: pl.DataFrame, stat: str, window: int) -> str:
    early = panel.filter(pl.col("year") <= 2021)
    late = panel.filter(pl.col("year") >= 2022)
    le = log_ratio(early, stat, window)
    ll = log_ratio(late, stat, window)
    if len(le) < 20 or len(ll) < 20:
        return "n/a"
    me, ml = np.median(le), np.median(ll)
    return "consistent" if np.sign(me) == np.sign(ml) and abs(ml) > 1e-3 else "FLIP"


def net_of_cost(panel: pl.DataFrame, window: int) -> dict[str, float]:
    """H2 tradeable: realized abnormal |move| vs the straddle-cost proxy. The straddle premium an event-bet
    pays ~ the name's own-baseline expected |move| (absret_raw / absret_abn = baseline); the bet wins when
    the EVENT |move| exceeds (baseline expected move + round-trip cost). Report the mean net edge in bps."""
    need = [f"absret_raw_{window}", f"absret_abn_{window}"]
    if any(c not in panel.columns for c in need):
        return {}
    df = panel.select(need).drop_nulls()
    df = df.filter((pl.col(f"absret_abn_{window}") > 0) & pl.col(f"absret_abn_{window}").is_finite())
    if df.height < 30:
        return {}
    realized = df[f"absret_raw_{window}"].to_numpy()  # event realized |move|
    baseline = (
        realized / df[f"absret_abn_{window}"].to_numpy()
    )  # the implied straddle premium (expected |move|)
    out = {}
    for bps in COST_BPS:
        cost = baseline + 2.0 * bps / 1e4  # premium + round-trip (in + out)
        net = realized - cost
        out[f"net_bps_{int(bps)}"] = float(np.mean(net) * 1e4)
        out[f"win_rate_{int(bps)}"] = float(np.mean(realized > cost))
    return out


def by_fdr(pvals: list[float], q: float) -> list[bool]:
    p = np.array(pvals)
    valid = ~np.isnan(p)
    m = int(valid.sum())
    if m == 0:
        return [False] * len(p)
    c_m = float(np.sum(1.0 / np.arange(1, m + 1)))
    order = np.argsort(np.where(valid, p, np.inf))
    ranked = p[order][:m]
    crit = (np.arange(1, m + 1) / (m * c_m)) * q
    passed = np.where(ranked <= crit)[0]
    keep = np.zeros(len(p), dtype=bool)
    if len(passed):
        keep[order[: passed.max() + 1]] = True
    return keep.tolist()


def main() -> None:
    panel = pl.read_parquet(f"{OUT_DIR}/events.parquet")
    print(
        f"events: {panel.height}, symbols: {panel['symbol'].n_unique()}, years {panel['year'].min()}-{panel['year'].max()}"
    )
    print(panel["regime"].value_counts())

    records: list[dict] = []
    regimes = {
        "all": panel,
        "rth": panel.filter(pl.col("regime") == "rth"),
        "offhours": panel.filter(pl.col("regime") == "offhours"),
    }

    for regime_name, sub in regimes.items():
        if sub.height < 30:
            continue
        # H1 + H2: own-normalized ratio abnormality
        for hyp, stats in (("H1", H1), ("H2", H2)):
            for stat in stats:
                for window in WINDOWS:
                    lr = log_ratio(sub, stat, window)
                    if len(lr) < 20:
                        continue
                    med = float(np.median(lr))
                    mean = float(np.mean(lr))
                    z = sign_flip_z(lr, mean)
                    resid_med, collapse = own_vol_collapse(sub, stat, window)
                    rec = dict(
                        hypothesis=hyp,
                        stat=stat,
                        target=f"{stat}_abn",
                        window=window,
                        regime=regime_name,
                        n=len(lr),
                        median_logratio=med,
                        mean_logratio=mean,
                        shuffle_z=z,
                        resid_median=resid_med,
                        collapse=collapse,
                        oos=oos_consistent(sub, stat, window),
                    )
                    if hyp == "H2" and stat == "absret":
                        rec.update(net_of_cost(sub, window))
                    records.append(rec)
        # H3: signed return drift (one-sample t of ret_abn vs 0)
        for window in WINDOWS:
            col = f"ret_abn_{window}"
            if col not in sub.columns:
                continue
            rets = sub[col].drop_nulls().to_numpy()
            rets = rets[np.isfinite(rets)]
            if len(rets) < 20:
                continue
            mean = float(np.mean(rets))
            t = mean / (np.std(rets, ddof=1) / np.sqrt(len(rets)) + 1e-12)
            records.append(
                dict(
                    hypothesis="H3",
                    stat="ret",
                    target="ret_drift",
                    window=window,
                    regime=regime_name,
                    n=len(rets),
                    median_logratio=float("nan"),
                    mean_logratio=mean,
                    shuffle_z=t,
                    resid_median=float("nan"),
                    collapse=float("nan"),
                    oos="n/a",
                )
            )

    pvals = [two_sided_p(r["shuffle_z"]) for r in records]
    keep = by_fdr(pvals, FDR_Q)
    for r, p, k in zip(records, pvals, keep):
        r["p"] = p
        r["fdr_survive"] = k

    res = pl.DataFrame(records)
    res.write_csv(f"{OUT_DIR}/screen_results.csv")

    print("\n=== VERDICT (regime=all, sorted by |z|) ===")
    show = (
        res.filter(pl.col("regime") == "all")
        .with_columns(pl.col("shuffle_z").abs().alias("_az"))
        .sort("_az", descending=True, nulls_last=True)
    )
    with pl.Config(tbl_rows=40, tbl_cols=12, fmt_str_lengths=16):
        print(
            show.select(
                [
                    "hypothesis",
                    "target",
                    "window",
                    "n",
                    "median_logratio",
                    "shuffle_z",
                    "collapse",
                    "oos",
                    "fdr_survive",
                ]
            )
        )

    print("\n=== H2 net-of-cost (absret, regime=all) ===")
    nc = res.filter((pl.col("stat") == "absret") & (pl.col("regime") == "all"))
    cols = [
        c
        for c in [
            "window",
            "median_logratio",
            "collapse",
            "net_bps_5",
            "win_rate_5",
            "net_bps_10",
            "win_rate_10",
        ]
        if c in nc.columns
    ]
    print(nc.select(cols))

    print(
        f"\nBY-FDR survivors: {res.filter(pl.col('fdr_survive')).height} / {len([p for p in pvals if not np.isnan(p)])} valid cells"
    )


if __name__ == "__main__":
    main()
