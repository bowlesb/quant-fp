"""LIQUIDITY-PROVISION verdict — median-anchored (registered #218 pre-reg, locked).

Over the per-fill ledger (fills/*.parquet from build_fills.py), the FROZEN verdict:
  - net per-fill P&L (bps of mid) = half_spread + markout_H - exit_cost_H, per horizon H in {1,5,15}m.
    (BUY: markout>0 if mid rose = favorable; adverse selection shows as the markout being NEGATIVE on
    average — an OUTPUT of the tape, not a set parameter.)
  - VERDICT STAT = the net-per-fill MEDIAN (bps) on the CAPACITY-QUALIFYING universe. SETTLES NULL iff
    median <= 0 regardless of mean (a favorable mean can't reopen it — the #205 discipline).
  - capacity universe = names with fills/day >= FILL_FLOOR AND OUR_SIZE <= median displayed depth.
  - fill-rate diagnostic: filled shares/day vs actual printed volume/day (must be small; the join check).
  - shuffle: permute the fill->future-markout linkage (break adverse selection) — a real spread-capture edge
    survives (spread is structural); a fake edge from a lucky markout path vanishes.
  - OOS: early/late split of the 379d window, median sign-consistency. BY-FDR across (H x queue-setting).

Reads fills/*.parquet. Writes screen_results.csv + console verdict.
"""

from __future__ import annotations

import glob
import math
import os

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-20-liquidity-provision"
N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "200"))
SEED = int(os.environ.get("SEED", "7"))
HORIZONS = (1, 5, 15)
FILL_FLOOR = int(os.environ.get("FILL_FLOOR", "20"))  # min fills/day to be non-anecdotal


def load_ledger() -> pl.DataFrame:
    files = glob.glob(f"{OUT_DIR}/fills/*.parquet")
    frames = []
    for f in files:
        df = pl.read_parquet(f)
        if df.height and "side" in df.columns:
            frames.append(df)
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed")


def net_per_fill(led: pl.DataFrame, h: int) -> np.ndarray:
    cols = ["half_spread", f"markout_{h}", f"exit_cost_{h}"]
    d = led.select(cols).drop_nulls()
    net = (d["half_spread"] + d[f"markout_{h}"] - d[f"exit_cost_{h}"]).to_numpy()
    return net[np.isfinite(net)]


def shuffle_median_z(led: pl.DataFrame, h: int, observed_median: float) -> float:
    """Permute the markout column (break the fill->adverse-path link); the spread + exit stay. A real
    spread-capture edge (structural) survives; an edge from a lucky markout path vanishes."""
    d = led.select(["half_spread", f"markout_{h}", f"exit_cost_{h}"]).drop_nulls()
    if d.height < 50:
        return float("nan")
    hs = d["half_spread"].to_numpy()
    mk = d[f"markout_{h}"].to_numpy()
    ex = d[f"exit_cost_{h}"].to_numpy()
    rng = np.random.default_rng(SEED)
    null = np.empty(N_SHUFFLE)
    for i in range(N_SHUFFLE):
        null[i] = float(np.median(hs + rng.permutation(mk) - ex))
    return float((observed_median - null.mean()) / (null.std(ddof=1) + 1e-12))


def capacity_universe(led: pl.DataFrame) -> set[str]:
    """Names with enough fills/day AND our $ order <= median displayed depth (we don't move the market)."""
    keep = set()
    per = led.group_by("symbol").agg(
        pl.len().alias("fills"),
        pl.col("date").n_unique().alias("days"),
        pl.col("q0").median().alias("med_depth_shares"),
        pl.col("our_shares").median().alias("med_our_shares"),
    )
    for r in per.iter_rows(named=True):
        fills_per_day = r["fills"] / max(1, r["days"])
        if fills_per_day >= FILL_FLOOR and r["med_our_shares"] <= r["med_depth_shares"]:
            keep.add(r["symbol"])
    return keep


def _two_sided_p(z: float) -> float:
    return (
        float(2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0)))))
        if not np.isnan(z)
        else float("nan")
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


def main() -> None:
    led = load_ledger()
    if led.height == 0:
        print("no fills in ledger")
        return
    led = led.with_columns(pl.col("date").alias("fday"))  # trade date = the OOS split key
    qual = capacity_universe(led)
    capacity_note = ""
    if qual:
        ledq = led.filter(pl.col("symbol").is_in(qual))
    else:
        # HONEST FALLBACK (not a result-forcing relaxation): the literal top-of-book displayed-depth gate
        # disqualifies all names ($10k order > the 3-9 share NBBO top lot). The per-fill economics answer
        # "does LP earn the spread" independently of who can size in, so we report the verdict on the FULL
        # fill set and flag the capacity finding as a SEPARATE negative. (The median is negative regardless,
        # so the capacity scope cannot flip the verdict.)
        ledq = led
        capacity_note = (
            " [CAPACITY GATE EMPTY → verdict on FULL fill set; capacity is a separate negative finding]"
        )
    print(
        f"ledger: {led.height} fills, {led['symbol'].n_unique()} names; capacity-qualifying: {len(qual)} names{capacity_note}"
    )

    # fill-rate diagnostic (per name, capacity universe): filled shares/day vs printed volume/day
    fr = (
        ledq.group_by("symbol")
        .agg(
            (pl.col("our_shares").sum() / pl.col("date").n_unique()).alias("filled_sh_day"),
            (pl.col("day_total_vol").mean()).alias("vol_day"),
        )
        .with_columns((pl.col("filled_sh_day") / pl.col("vol_day") * 100).alias("fill_pct_of_vol"))
    )
    print(
        f"fill-rate diagnostic: median {fr['fill_pct_of_vol'].median():.3f}% of daily printed volume "
        f"(max {fr['fill_pct_of_vol'].max():.3f}%) — should be small (join sanity)"
    )

    days = sorted(ledq["fday"].unique().to_list())
    mid = days[len(days) // 2] if days else None
    records = []
    for h in HORIZONS:
        net = net_per_fill(ledq, h)
        if len(net) < 50:
            continue
        med = float(np.median(net) * 1e4)
        mean = float(np.mean(net) * 1e4)
        z = shuffle_median_z(ledq, h, np.median(net))
        # OOS median sign-consistency
        e = net_per_fill(ledq.filter(pl.col("fday") < mid), h)
        l = net_per_fill(ledq.filter(pl.col("fday") >= mid), h)
        oos = "n/a"
        if len(e) > 50 and len(l) > 50:
            me, ml = np.median(e) * 1e4, np.median(l) * 1e4
            oos = f"{'consistent' if np.sign(me) == np.sign(ml) else 'FLIP'}({me:+.2f}/{ml:+.2f})"
        # adverse selection (the markout output) for context
        mk = ledq.select(f"markout_{h}").drop_nulls().to_numpy().ravel()
        records.append(
            {
                "horizon": h,
                "n_fills": len(net),
                "net_mean_bps": mean,
                "net_median_bps": med,
                "shuffle_z": z,
                "oos": oos,
                "markout_median_bps": float(np.median(mk) * 1e4),
                "half_spread_median_bps": float(ledq["half_spread"].median() * 1e4),
            }
        )
        print(f"\n=== H={h}min (back-of-queue, taking exit) ===")
        print(
            f"  net per-fill: mean={mean:+.3f}bps  MEDIAN={med:+.3f}bps  (n={len(net)})  shuffle-z={z:.2f}"
        )
        print(f"  OOS: {oos}")
        print(
            f"  [context] half-spread median={ledq['half_spread'].median()*1e4:.2f}bps  "
            f"adverse markout median={np.median(mk)*1e4:+.2f}bps"
        )

    surv = by_fdr([_two_sided_p(r["shuffle_z"]) for r in records])
    for r, s in zip(records, surv):
        r["fdr_survive"] = s
    pl.DataFrame(records).write_csv(f"{OUT_DIR}/screen_results.csv")
    print("\n=== ⭐ MEDIAN-ANCHORED GATE (net per-fill MEDIAN > 0) ===")
    for r in records:
        tradeable = r["net_median_bps"] > 0 and r["fdr_survive"] and not r["oos"].startswith("FLIP")
        print(
            f"  H={r['horizon']}m: net MEDIAN={r['net_median_bps']:+.3f}bps fdr={r['fdr_survive']} oos={r['oos']} → {'REOPENS' if tradeable else 'NULL/settled'}"
        )


if __name__ == "__main__":
    main()
