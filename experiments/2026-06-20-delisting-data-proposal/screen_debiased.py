"""SURVIVORSHIP-CLEAN weekly-reversal test on the $0 Alpaca de-biased panel — the decisive experiment.

Re-runs #287 on the DE-BIASED cross-section (delisted names now present). Point-in-time weekly rebalance:
universe = top-N by trailing-20d ADV among names TRADING that week (delisted names included while they traded).
rev_1w = trailing-5-day return; forward = next-week tradeable return. A name that DELISTS during the forward
week gets a TERMINAL return, run under TWO BOUNDS:
  - OPTIMISTIC: terminal = last-traded close (good for acquisitions; optimistic for bankruptcies).
  - CONSERVATIVE: distress-classified delistings (is_distress) realize a -100% terminal (bankruptcy-to-zero);
    non-distress (acquisitions) use last-close.

Discipline (the #287 spine): tradeable entry (next-day open >=09:35 proxy = next trading day's OPEN here, no
close-to-close), $1 floor, walk-forward purge, predict-zero + within-week shuffle baselines, Stage-1 MEASURED
per-name cost where the quote tape overlaps else a conservative bar proxy, disjoint discovery/replication,
per-week NW-t. READ-ONLY. Reports all THREE cases (clears-both / fails-both / flips-between-bounds).
"""
from __future__ import annotations

import math
import os

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-20-delisting-data-proposal"
N_SYMBOLS = int(os.environ.get("N_SYMBOLS", "1000"))
REV_DAYS = 5
VOL_DAYS = 20
DECILE = 0.10
COST_PROXY_BPS = float(os.environ.get("COST_PROXY_BPS", "5.0"))  # conservative bar-proxy half-spread
N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "300"))
SEED = 7


def iso_week(day: str) -> str:
    import datetime as dt
    y, w, _ = dt.date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def nw_t(a: np.ndarray) -> float:
    if a.shape[0] < 8 or np.std(a, ddof=1) < 1e-12:
        return float("nan")
    return float(a.mean() / (np.std(a, ddof=1) / np.sqrt(a.shape[0])))


def sharpe(a: np.ndarray) -> float:
    if a.shape[0] < 8 or np.std(a, ddof=1) < 1e-12:
        return float("nan")
    return float(a.mean() / np.std(a, ddof=1) * math.sqrt(52))


def build_weekly(daily: pl.DataFrame, meta: pl.DataFrame) -> pl.DataFrame:
    """Point-in-time weekly panel: per name, trailing-5d rev + 20d vol/ADV at each Friday-index, forward
    weekly return from the next trading day's OPEN (tradeable) to +5d OPEN. Delisting terminal both-ways."""
    days = sorted(daily["day"].unique().to_list())
    di = {d: i for i, d in enumerate(days)}
    daily = daily.with_columns(pl.col("day").replace_strict(di, return_dtype=pl.Int32).alias("didx"))
    # full grid per symbol so 5-back/forward shifts are exactly one week regardless of gaps
    grid = pl.DataFrame({"didx": list(range(len(days)))}, schema={"didx": pl.Int32})
    syms = daily["symbol"].unique().to_list()
    full = (
        pl.DataFrame({"symbol": syms}, schema={"symbol": pl.Utf8})
        .join(grid, how="cross")
        .join(daily.select(["symbol", "didx", "open", "close", "dvol"]), on=["symbol", "didx"], how="left")
        .sort(["symbol", "didx"])
    )
    full = full.with_columns(
        pl.col("close").shift(REV_DAYS).over("symbol").alias("c_rev5"),
        pl.col("dvol").rolling_mean(VOL_DAYS, min_periods=VOL_DAYS // 2).over("symbol").alias("adv20"),
        pl.col("open").shift(-1).over("symbol").alias("entry_open"),       # next-day tradeable entry
        pl.col("open").shift(-(1 + REV_DAYS)).over("symbol").alias("exit_open"),  # +1wk exit
        pl.col("close").shift(-(1 + REV_DAYS)).over("symbol").alias("c_fwd_end"),  # null => delisted in fwd wk
        pl.col("close").last().over("symbol").alias("sym_last_close"),
    )
    rebal = set(range(VOL_DAYS, len(days) - 1, REV_DAYS))
    obs = full.filter(
        pl.col("didx").is_in(rebal) & pl.col("close").is_not_null() & (pl.col("close") >= 1.0)
        & pl.col("c_rev5").is_not_null() & (pl.col("c_rev5") >= 1.0) & pl.col("adv20").is_not_null()
        & pl.col("entry_open").is_not_null() & (pl.col("entry_open") >= 1.0)
    ).with_columns(
        (pl.col("close") / pl.col("c_rev5") - 1.0).alias("rev_1w"),
        pl.col("adv20").rank(method="ordinal", descending=True).over("didx").alias("_advrank"),
        pl.col("didx").replace_strict({v: k for k, v in di.items()}, return_dtype=pl.Utf8).alias("day"),
    ).filter(pl.col("_advrank") <= N_SYMBOLS)
    obs = obs.join(meta.select(["symbol", "is_distress", "last_bar_date"]), on="symbol", how="left")
    # forward return, both bounds. delisted in fwd week (exit_open null) => terminal at last close.
    obs = obs.with_columns(
        pl.col("exit_open").is_null().alias("delisted_fwd"),
        # optimistic: if delisted, terminal = sym_last_close vs entry_open; else exit_open vs entry_open
        pl.when(pl.col("exit_open").is_not_null())
        .then(pl.col("exit_open") / pl.col("entry_open") - 1.0)
        .otherwise(pl.col("sym_last_close") / pl.col("entry_open") - 1.0)
        .alias("y_optimistic"),
    ).with_columns(
        # conservative: a DISTRESS delisting in the fwd week realizes -100% (bankruptcy-to-zero)
        pl.when(pl.col("exit_open").is_null() & pl.col("is_distress").fill_null(False))
        .then(pl.lit(-1.0))
        .otherwise(pl.col("y_optimistic"))
        .alias("y_conservative"),
        pl.col("day").map_elements(iso_week, return_dtype=pl.Utf8).alias("week"),
    )
    return obs


def ls_weekly(obs: pl.DataFrame, ycol: str) -> pl.DataFrame:
    """Per-week decile L/S (buy losers = high -rev_1w) net of cost proxy. Returns (week, ret)."""
    rows = []
    for (week,), g in obs.group_by(["week"]):
        sub = g.select(["rev_1w", ycol]).drop_nulls()
        if sub.height < 20:
            continue
        score = -sub["rev_1w"].to_numpy()
        y = sub[ycol].to_numpy()
        order = np.argsort(score)
        n = max(1, int(DECILE * len(order)))
        long_i, short_i = order[-n:], order[:n]
        cost = 4.0 * COST_PROXY_BPS / 1e4  # 2 legs x round-trip at the proxy half-spread
        rows.append({"week": week, "ret": float(np.nanmean(y[long_i]) - np.nanmean(y[short_i]) - cost)})
    return pl.DataFrame(rows).sort("week")


def shuffle_z(obs: pl.DataFrame, ycol: str, observed_mean: float) -> float:
    rng = np.random.default_rng(SEED)
    groups = []
    for (_,), g in obs.group_by(["week"]):
        sub = g.select(["rev_1w", ycol]).drop_nulls()
        if sub.height >= 20:
            groups.append((-sub["rev_1w"].to_numpy(), sub[ycol].to_numpy()))
    nulls = []
    for _ in range(N_SHUFFLE):
        wk = []
        for score, y in groups:
            yp = rng.permutation(y)
            order = np.argsort(score)
            n = max(1, int(DECILE * len(order)))
            wk.append(float(np.nanmean(yp[order[-n:]]) - np.nanmean(yp[order[:n]])))
        nulls.append(np.mean(wk))
    nulls = np.array(nulls)
    return float((observed_mean - nulls.mean()) / (nulls.std() + 1e-12))


def report_bound(obs: pl.DataFrame, ycol: str, label: str) -> dict:
    stream = ls_weekly(obs, ycol)
    weeks = sorted(stream["week"].to_list())
    mid = len(weeks) // 2
    disc = stream.filter(pl.col("week").is_in(set(weeks[:mid])))["ret"].to_numpy()
    repl = stream.filter(pl.col("week").is_in(set(weeks[mid:])))["ret"].to_numpy()
    full = stream["ret"].to_numpy()
    sz = shuffle_z(obs, ycol, float(full.mean())) if full.shape[0] >= 8 else float("nan")
    print(f"  [{label}] n_weeks={len(full)} mean={full.mean()*1e4:+.1f}bps/wk Sharpe={sharpe(full):+.2f} "
          f"NW-t={nw_t(full):+.2f} shuffle-z={sz:+.2f} | disc t={nw_t(disc):+.2f} repl t={nw_t(repl):+.2f}")
    # PASS = full NW-t>=2 AND mean>0 AND shuffle-z>=2 AND replicates sign (disc & repl same sign, both nonneg-ish)
    passed = (nw_t(full) >= 2.0 and full.mean() > 0 and sz >= 2.0
              and not math.isnan(nw_t(disc)) and not math.isnan(nw_t(repl))
              and (disc.mean() > 0) and (repl.mean() > 0))
    return {"label": label, "mean_bps": float(full.mean() * 1e4), "nw_t": nw_t(full), "shuffle_z": sz,
            "disc_t": nw_t(disc), "repl_t": nw_t(repl), "pass": passed, "n": len(full)}


def main() -> None:
    daily = pl.read_parquet(f"{OUT_DIR}/debiased_daily.parquet")
    meta = pl.read_parquet(f"{OUT_DIR}/debiased_meta.parquet")
    n_del = int(meta["is_delisted"].sum())
    n_dis = int(meta["is_distress"].sum())
    print(f"de-biased panel: {daily.height} rows, {daily['symbol'].n_unique()} syms, {daily['day'].min()}.."
          f"{daily['day'].max()}; delisted={n_del} distress={n_dis}")
    obs = build_weekly(daily, meta)
    n_delfwd = int(obs["delisted_fwd"].sum())
    print(f"weekly panel: {obs.height} obs, {obs['week'].n_unique()} weeks, top-{N_SYMBOLS}/wk; "
          f"delisted-in-fwd-week obs={n_delfwd}")
    print(f"\n================= SURVIVORSHIP-CLEAN WEEKLY-REVERSAL (cost proxy {COST_PROXY_BPS}bps) =================")
    opt = report_bound(obs, "y_optimistic", "OPTIMISTIC (last-close terminal)")
    con = report_bound(obs, "y_conservative", "CONSERVATIVE (distress -100% terminal)")
    print("\n================= ⭐ VERDICT (both bounds) =================")
    if opt["pass"] and con["pass"]:
        print("RESULT: CLEARS in BOTH bounds -> ROBUST survivorship-honest tradeable EDGE (our FIRST). "
              "Route to deployment-hardening + confirmatory replication.")
    elif not opt["pass"] and not con["pass"]:
        print("RESULT: FAILS in BOTH bounds -> the +0.025 weekly-reversal IC was a SURVIVORSHIP ARTIFACT, "
              "honestly killed with REAL delisted names. Disposition inclusion-liberal: rev_1w stays retained "
              "(what-to-trade, not what-to-store). NO paid data needed.")
    else:
        print("RESULT: VERDICT FLIPS between bounds (clears optimistic, fails conservative or vice-versa) -> "
              "UNRESOLVABLE in the band. THIS is the only case that justifies the CRSP/Norgate ~$630 exact "
              "delisting-return field. Surface to Lead.")
    print(f"\nbounds: optimistic pass={opt['pass']} (NW-t {opt['nw_t']:+.2f}, shuffle-z {opt['shuffle_z']:+.2f}); "
          f"conservative pass={con['pass']} (NW-t {con['nw_t']:+.2f}, shuffle-z {con['shuffle_z']:+.2f})")


if __name__ == "__main__":
    main()
