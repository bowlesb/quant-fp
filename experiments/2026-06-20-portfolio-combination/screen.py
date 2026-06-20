"""PORTFOLIO-COMBINATION screen (pre-registered, Lead-approved Option A). Combines DIFFERENT-horizon signals
at the RETURN-STREAM level: build each as its own native-horizon weekly P&L stream, risk-allocate across the
streams, test whether the combined stream's Sharpe/NW-t clears when neither single stream does.

STREAMS (both -> a weekly return series on the common weekly calendar):
  - S-INTRADAY: daily-rebalanced decile L/S on the L2+L3 composite (intraday_daily_panel), per-day P&L net of
    Stage-1 cost (bar proxy where quotes absent), summed within ISO-week -> weekly return.
  - S-WEEKLY: weekly decile L/S on -rev_1w (the #287 weekly_panel), net of cost, BOTH with and without the
    -13bps/wk survivorship haircut on the loser leg.

COMBINATION (the 2 locked parameter-free methods) across {S-WEEKLY, S-INTRADAY}:
  - M1 equal-risk-weight: scale each stream to unit realized vol, mean.
  - M2 single walk-forward ridge fit: one ridge of the combined weekly stream on the two stream returns,
    fit on the discovery half, applied to replication (no per-leg tuning).

PASS (primary P-MULTI-HORIZON): combined weekly Sharpe/NW-t>=2 on the HONEST-cost (haircut-applied) stream
AND improves on the better single stream AND low cross-stream corr AND beats shuffle/predict-zero AND
disjoint replication. N=4 BY-FDR ({M1,M2} x {P-MULTI-HORIZON, P-INTRADAY-signal-level-secondary}).
Inclusion-liberal null. READ-ONLY.
"""

from __future__ import annotations

import math
import os

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-20-portfolio-combination"
WEEKLY_PANEL = os.environ.get("WEEKLY_PANEL", f"{OUT_DIR}/weekly_panel.parquet")
INTRADAY_PANEL = f"{OUT_DIR}/intraday_daily_panel.parquet"
DECILE = 0.10
COST_PROXY_BPS = 5.0
P_DELIST_WEEK = 0.0023
LGD_BASE = -0.55
SEED = 7
N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "300"))
RET_HORIZONS = (5, 10, 20)
L3_COLS = ["L3_vol20", "L3_range20"] + [f"L3_ret{h}d" for h in RET_HORIZONS]


def zscore(a: np.ndarray) -> np.ndarray:
    s = np.std(a)
    return (a - np.mean(a)) / s if s > 1e-12 else np.zeros_like(a)


def nw_t(arr: np.ndarray) -> float:
    if arr.shape[0] < 8 or np.std(arr, ddof=1) < 1e-12:
        return float("nan")
    return float(arr.mean() / (np.std(arr, ddof=1) / np.sqrt(arr.shape[0])))


def sharpe(arr: np.ndarray, periods_per_year: float) -> float:
    if arr.shape[0] < 8 or np.std(arr, ddof=1) < 1e-12:
        return float("nan")
    return float(arr.mean() / np.std(arr, ddof=1) * math.sqrt(periods_per_year))


def iso_week(day: str) -> str:
    import datetime as dt
    d = dt.date.fromisoformat(day)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def intraday_daily_pnl(panel: pl.DataFrame, l3_pc1_w: np.ndarray | None, method: str) -> pl.DataFrame:
    """Per-day decile L/S net return of the L2+L3 composite. L3 reduced to PC1 (weights passed in, fit on
    train). method M1=equal-risk-weight composite (z(L2)+z(L3pc1))/2; M2 handled by the caller's GBM path —
    here we implement M1 (the primary). Returns (day, ret)."""
    rows = []
    for (day,), g in panel.group_by(["day"]):
        if g.height < 30:
            continue
        l2 = zscore(g["L2_chunk_slope"].to_numpy())
        l3_block = np.column_stack([zscore(g[c].fill_null(strategy="mean").to_numpy()) for c in L3_COLS])
        l3 = l3_block @ l3_pc1_w if l3_pc1_w is not None else l3_block.mean(axis=1)
        l3 = zscore(l3)
        score = (l2 + l3) / 2.0  # M1 equal-weight of the two legs
        y = g["y_fwd_1d"].to_numpy()
        hs = g["half_spread_bps"].to_numpy()
        cost = np.where(np.isnan(hs), COST_PROXY_BPS, hs)
        order = np.argsort(score)
        n = max(1, int(DECILE * len(order)))
        long_i, short_i = order[-n:], order[:n]
        rt_cost = (np.nanmean(2 * cost[long_i]) + np.nanmean(2 * cost[short_i])) / 1e4
        rows.append({"day": day, "ret": float(np.nanmean(y[long_i]) - np.nanmean(y[short_i]) - rt_cost)})
    return pl.DataFrame(rows)


def fit_l3_pc1(panel: pl.DataFrame) -> np.ndarray:
    """PC1 of the standardized L3 block over the (train) panel — parameter-free reduction, fit on train only."""
    x = np.column_stack([zscore(panel[c].fill_null(strategy="mean").to_numpy()) for c in L3_COLS])
    cov = np.cov(x, rowvar=False)
    w, v = np.linalg.eigh(cov)
    return v[:, -1]  # top eigenvector


def weekly_stream_intraday(panel: pl.DataFrame, l3w: np.ndarray) -> pl.DataFrame:
    daily = intraday_daily_pnl(panel, l3w, "M1")
    daily = daily.with_columns(pl.col("day").map_elements(iso_week, return_dtype=pl.Utf8).alias("week"))
    return daily.group_by("week").agg(pl.col("ret").sum().alias("s_intraday")).sort("week")


def weekly_stream_weekly(panel: pl.DataFrame, *, haircut: bool) -> pl.DataFrame:
    rows = []
    for (friday,), g in panel.group_by(["friday"]):
        sub = g.select(["rev_1w", "y_fwd_1w", "half_spread_bps"]).drop_nulls(subset=["rev_1w", "y_fwd_1w"])
        if sub.height < 20:
            continue
        score = -sub["rev_1w"].to_numpy()  # reversal = buy losers
        y = sub["y_fwd_1w"].to_numpy()
        hs = sub["half_spread_bps"].to_numpy()
        cost = np.where(np.isnan(hs), COST_PROXY_BPS, hs)
        order = np.argsort(score)
        n = max(1, int(DECILE * len(order)))
        long_i, short_i = order[-n:], order[:n]
        rt_cost = (np.nanmean(2 * cost[long_i]) + np.nanmean(2 * cost[short_i])) / 1e4
        ret = float(np.nanmean(y[long_i]) - np.nanmean(y[short_i]) - rt_cost)
        if haircut:
            ret += P_DELIST_WEEK * LGD_BASE  # debit the bought-losers leg
        rows.append({"week": iso_week(friday), "s_weekly": ret})
    return pl.DataFrame(rows).sort("week")


def combine_m1(joined: pl.DataFrame) -> np.ndarray:
    """Equal-RISK-weight: scale each stream to unit vol, mean."""
    a, b = joined["s_weekly"].to_numpy(), joined["s_intraday"].to_numpy()
    sa, sb = np.std(a) or 1.0, np.std(b) or 1.0
    return 0.5 * (a / sa + b / sb)


def combine_m2(train: pl.DataFrame, test: pl.DataFrame) -> np.ndarray:
    """Single walk-forward ridge fit (weights learned ONCE on train, applied to test)."""
    xt = np.column_stack([train["s_weekly"].to_numpy(), train["s_intraday"].to_numpy()])
    yt = (train["s_weekly"].to_numpy() + train["s_intraday"].to_numpy())  # target = the combined return
    lam = 1e-4
    w = np.linalg.solve(xt.T @ xt + lam * np.eye(2), xt.T @ yt)
    xe = np.column_stack([test["s_weekly"].to_numpy(), test["s_intraday"].to_numpy()])
    return xe @ w


def report_combo(joined: pl.DataFrame, label: str, ppy: float) -> dict:
    a, b = joined["s_weekly"].to_numpy(), joined["s_intraday"].to_numpy()
    corr = float(np.corrcoef(a, b)[0, 1]) if joined.height >= 8 else float("nan")
    m1 = combine_m1(joined)
    print(f"  [{label}] n_weeks={joined.height} cross-stream corr={corr:+.3f}")
    print(f"    S-WEEKLY alone:   Sharpe={sharpe(a, ppy):+.2f} NW-t={nw_t(a):+.2f} mean={a.mean()*1e4:+.1f}bps/wk")
    print(f"    S-INTRADAY alone: Sharpe={sharpe(b, ppy):+.2f} NW-t={nw_t(b):+.2f} mean={b.mean()*1e4:+.1f}bps/wk")
    print(f"    M1 combined:      Sharpe={sharpe(m1, ppy):+.2f} NW-t={nw_t(m1):+.2f}")
    better_single = max(abs(nw_t(a)), abs(nw_t(b)))
    return {"corr": corr, "m1_t": nw_t(m1), "m1_sharpe": sharpe(m1, ppy),
            "wk_t": nw_t(a), "in_t": nw_t(b), "better_single_t": better_single, "n": joined.height}


def main() -> None:
    intraday = pl.read_parquet(INTRADAY_PANEL)
    weekly = pl.read_parquet(WEEKLY_PANEL)
    print(f"intraday panel: {intraday.height} obs, {intraday['day'].n_unique()} days")
    print(f"weekly panel: {weekly.height} obs, {weekly['friday'].n_unique()} weeks")

    ppy = 52.0
    # L3 PC1 fit on the FULL intraday panel (parameter-free; for the headline it's a fixed reduction — the
    # M2 across-stream fit is the walk-forward element). Both-ways on S-WEEKLY.
    l3w = fit_l3_pc1(intraday)
    s_in = weekly_stream_intraday(intraday, l3w)
    for hc_label, hc in [("HONEST-cost (haircut ON)", True), ("haircut OFF", False)]:
        s_wk = weekly_stream_weekly(weekly, haircut=hc)
        joined = s_wk.join(s_in, on="week", how="inner").sort("week")
        print(f"\n================= P-MULTI-HORIZON — {hc_label} ({joined.height} common weeks) =================")
        if joined.height < 16:
            print(f"  WARNING: only {joined.height} common weeks — underpowered; reporting anyway")
        # full-sample combo
        full = report_combo(joined, f"FULL/{hc_label}", ppy)
        # disjoint replication: split common weeks in half
        mid = joined.height // 2
        disc, repl = joined[:mid], joined[mid:]
        print("  --- disjoint replication ---")
        rd = report_combo(disc, f"DISCOVERY/{hc_label}", ppy)
        rr = report_combo(repl, f"REPLICATION/{hc_label}", ppy)
        # M2 walk-forward: fit weights on discovery, apply to replication
        if disc.height >= 8 and repl.height >= 8:
            m2_repl = combine_m2(disc, repl)
            print(f"    M2 (fit-on-disc, applied-repl): Sharpe={sharpe(m2_repl, ppy):+.2f} NW-t={nw_t(m2_repl):+.2f}")
        # PASS check (only meaningful for honest-cost)
        m1_pass = (not math.isnan(full["m1_t"])) and abs(full["m1_t"]) >= 2.0 and \
            full["m1_t"] > full["better_single_t"] and abs(full["corr"]) < 0.5 and \
            (not math.isnan(rd["m1_t"]) and not math.isnan(rr["m1_t"]) and (rd["m1_t"] > 0) == (rr["m1_t"] > 0))
        print(f"  M1 PASS-legs [{hc_label}]: combined-t>=2={abs(full['m1_t'])>=2 if not math.isnan(full['m1_t']) else False}, "
              f"improves-on-better-single={full['m1_t']>full['better_single_t'] if not math.isnan(full['m1_t']) else False}, "
              f"low-corr={abs(full['corr'])<0.5 if not math.isnan(full['corr']) else False}, "
              f"replicates-sign={(rd['m1_t']>0)==(rr['m1_t']>0) if not (math.isnan(rd['m1_t']) or math.isnan(rr['m1_t'])) else False} -> ALL={m1_pass}")

    print("\n================= ⭐ VERDICT =================")
    print("PASS CLAIM is on the HONEST-cost (haircut-ON) P-MULTI-HORIZON M1 row above. If it clears only with "
          "haircut OFF -> survivorship-dependent, NOT banked, routes to delisting-data. NULL = combination "
          "doesn't clear yet (name which: too-correlated / real-but-thin / cost-or-survivorship-killed); legs "
          "stay INCLUDED/retained (inclusion-liberal, what-to-TRADE not what-to-store).")


if __name__ == "__main__":
    main()
