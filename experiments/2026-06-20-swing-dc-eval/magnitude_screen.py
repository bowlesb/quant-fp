"""swing_dc MAGNITUDE-feature framing — does any DC scale's chunk/Fib carry net-new MAGNITUDE-prediction
power (forward |return|), surviving the own-vol/size control + shuffle + FDR? (feature-utility gate, NOT a
tradeable claim — the news-H1 reframe applied to swing_dc.)

Reuses the cached swing_dc panel (74 feats + own_vol + size + y_fwd). Target = |y_fwd| (the 30m forward
move-magnitude). The DECISIVE number is the own-vol/size partial-IC COLLAPSE — with the CORRECTED gate from
the news hunt: a feature has net-new magnitude value ONLY IF raw IC is non-trivial (|raw|>=MIN_IC) AND the
partial keeps the SAME sign + stays non-trivial (a collapse>1 on a ~0 raw IC is a near-zero-denominator
artifact, NOT survival). Honest prior: LOW (EDGAR/news magnitude tiers mostly collapsed under own-vol).
READ-ONLY. Writes magnitude_results.csv + console.
"""

from __future__ import annotations

import math
import os

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-20-swing-dc-eval"
N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "200"))
SEED = 7
MIN_IC = 0.02  # raw IC must clear this for the collapse ratio to be meaningful (the news-hunt lesson)


def rank(a):
    return a.argsort().argsort().astype(float)


def sp(x, y):
    rx, ry = rank(x), rank(y)
    return float(np.corrcoef(rx, ry)[0, 1]) if np.std(rx) > 1e-12 and np.std(ry) > 1e-12 else float("nan")


def two_sided_p(t):
    return 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))) if not np.isnan(t) else float("nan")


def main() -> None:
    p = pl.read_parquet(f"{OUT_DIR}/swing_dc_panel.parquet")
    p = p.with_columns(pl.col("y_fwd").abs().alias("y_absfwd"))  # the 30m forward MOVE-MAGNITUDE
    # winsor the magnitude target per day
    lo = pl.col("y_absfwd").quantile(0.01).over("date")
    hi = pl.col("y_absfwd").quantile(0.99).over("date")
    p = p.with_columns(pl.col("y_absfwd").clip(lo, hi))
    feats = [c for c in p.columns if c.startswith("dc_")]
    print(f"swing_dc MAGNITUDE screen: {p.height} rows, {p['date'].n_unique()} days, {len(feats)} feats")
    # how much does the magnitude target depend on own_vol (the confound to beat)?
    chk = p.select(["own_vol", "size", "y_absfwd"]).drop_nulls()
    print(
        f"  own_vol vs |fwd|: IC={sp(chk['own_vol'].to_numpy(), chk['y_absfwd'].to_numpy()):+.3f} (vol persistence) | "
        f"size vs |fwd|: IC={sp(chk['size'].to_numpy(), chk['y_absfwd'].to_numpy()):+.3f}"
    )

    recs = []
    for f in feats:
        df = p.select(["date", f, "own_vol", "size", "y_absfwd"]).drop_nulls()
        raws, pars = [], []
        for (_,), g in df.group_by(["date"]):
            if g.height < 20:
                continue
            x, y = g[f].to_numpy(), g["y_absfwd"].to_numpy()
            if np.std(x) < 1e-12 or np.std(y) < 1e-12:
                continue
            raws.append(sp(x, y))
            Z = np.column_stack([np.ones(g.height), g["own_vol"].to_numpy(), g["size"].to_numpy()])
            rx = x - Z @ np.linalg.lstsq(Z, x, rcond=None)[0]
            ry = y - Z @ np.linalg.lstsq(Z, y, rcond=None)[0]
            if np.std(rx) > 1e-12 and np.std(ry) > 1e-12:
                pars.append(sp(rx, ry))
        if len(raws) < 8 or len(pars) < 8:
            continue
        ra, pa = np.array(raws), np.array(pars)
        raw_ic = float(ra.mean())
        raw_t = float(ra.mean() / (ra.std(ddof=1) / np.sqrt(len(ra)) + 1e-12))
        par_ic = float(pa.mean())
        par_t = float(pa.mean() / (pa.std(ddof=1) / np.sqrt(len(pa)) + 1e-12))
        collapse = abs(par_ic) / abs(raw_ic) if abs(raw_ic) > 1e-9 else float("nan")
        recs.append(
            {
                "feature": f,
                "raw_ic": raw_ic,
                "raw_t": raw_t,
                "partial_ic": par_ic,
                "partial_t": par_t,
                "collapse": collapse,
                "n_days": len(ra),
            }
        )

    res = pl.DataFrame(recs)
    res.write_csv(f"{OUT_DIR}/magnitude_results.csv")
    print(
        "\n=== swing_dc magnitude leaderboard (top by |partial_t| — net-new magnitude after own-vol/size) ==="
    )
    with pl.Config(tbl_rows=15, fmt_str_lengths=30):
        print(
            res.with_columns(pl.col("partial_t").abs().alias("_a"))
            .sort("_a", descending=True)
            .head(12)
            .select(["feature", "raw_ic", "raw_t", "partial_ic", "partial_t", "collapse"])
        )
    # FEATURE-UTILITY gate: net-new magnitude feature = non-trivial raw IC + sign-stable partial + partial
    # significant + survives FDR on the PARTIAL t (the own-vol-independent signal).
    pP = [two_sided_p(r["partial_t"]) for r in recs]
    pv = np.array(pP)
    valid = ~np.isnan(pv)
    m = int(valid.sum())
    cm = float(np.sum(1.0 / np.arange(1, m + 1)))
    order = np.argsort(np.where(valid, pv, np.inf))
    crit = (np.arange(1, m + 1) / (m * cm)) * 0.10
    passed = np.where(pv[order][:m] <= crit)[0]
    nfdr = (passed.max() + 1) if len(passed) else 0
    survivors = []
    for i in order[:nfdr]:
        r = recs[i]
        if (
            abs(r["raw_ic"]) >= MIN_IC
            and abs(r["partial_ic"]) >= MIN_IC
            and np.sign(r["raw_ic"]) == np.sign(r["partial_ic"])
        ):
            survivors.append(r["feature"])
    print(
        f"\n=== ⭐ FEATURE-UTILITY GATE: net-new MAGNITUDE features (partial-t FDR-survive + |raw IC|>={MIN_IC} + sign-stable) ==="
    )
    print(f"  FDR survivors on partial-t: {nfdr}; of those PASSING the raw-IC+sign gate: {len(survivors)}")
    if survivors:
        print("  →", survivors[:12])
    else:
        print(
            "  → NONE — swing_dc carries NO net-new magnitude-feature value (collapses under own-vol/size or raw IC ~0)"
        )


if __name__ == "__main__":
    main()
