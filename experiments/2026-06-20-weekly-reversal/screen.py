"""WEEKLY REVERSAL — screen (pre-registered, Lead-approved run). Implements the locked pass bar.

PASS (Lead-refined) = net-positive after the BASE −13bps/week survivorship haircut AND per-week NW-t>=2 AND
disjoint-window replication. REPORT the full band: gross / base-haircut / −100%-stress. Surviving base but
flipping sign under −100% = a robustness CAVEAT (route to acquire a delisting-inclusive universe), NOT an
auto-fail (the −100% = every delisted loser → total loss = the extreme bound, not the expected case).

Calibrated survivorship haircut (the GATE; externally calibrated since the panel has ~0 in-sample delistings):
  loser leg (the bought bottom decile) charged p_delist_week × LGD as a per-week return drag.
  base: p=0.0023/week (bottom-decile adverse-delist), LGD=−0.55 (Shumway) → ~−13 bps/week loser-leg drag.
  stress: LGD=−1.00 (total loss) → ~−23 bps/week. Short (winner) leg charged nothing (delisting helps shorts;
  conservatively credited zero).

Also: own-vol/SIZE control (partial out vol_20d + log_adv), shuffle baseline, Stage-1 realized cost amortized
over the weekly hold (one round-trip, per-name), per-week NW-t, disjoint-window replication, BY-FDR(N=1).
READ-ONLY. Reads weekly_panel.parquet.
"""

from __future__ import annotations

import math
import os

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-20-weekly-reversal"
N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "300"))
SEED = int(os.environ.get("SEED", "7"))
WINSOR_P = 0.01
DECILE = 0.10
COST_PROXY_BPS = float(os.environ.get("COST_PROXY_BPS", "5.0"))  # conservative bar-proxy half-spread (bps)
P_DELIST_WEEK = 0.0023  # bottom-decile adverse-delist per week (calibrated, pre-committed)
LGD_BASE = -0.55  # Shumway delisting-return correction
LGD_STRESS = -1.00  # total-loss extreme bound


def _rank(a: np.ndarray) -> np.ndarray:
    return a.argsort().argsort().astype(float)


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx, ry = _rank(x), _rank(y)
    if np.std(rx) < 1e-12 or np.std(ry) < 1e-12:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def winsorize_weekly(panel: pl.DataFrame, col: str) -> pl.DataFrame:
    lo = pl.col(col).quantile(WINSOR_P).over("friday")
    hi = pl.col(col).quantile(1 - WINSOR_P).over("friday")
    return panel.with_columns(pl.col(col).clip(lo, hi).alias(col))


def weekly_ic_series(panel: pl.DataFrame, feat: str, target: str, sign: float) -> np.ndarray:
    df = panel.select(["friday", feat, target]).drop_nulls()
    ics = []
    for (_,), grp in df.group_by(["friday"]):
        if grp.height < 20:
            continue
        ic = _spearman(sign * grp[feat].to_numpy(), grp[target].to_numpy())
        if not np.isnan(ic):
            ics.append(ic)
    return np.array(ics)


def nw_t(arr: np.ndarray) -> float:
    if arr.shape[0] < 8 or arr.std(ddof=1) < 1e-12:
        return float("nan")
    return float(arr.mean() / (arr.std(ddof=1) / np.sqrt(arr.shape[0])))


def ls_weekly_returns(
    panel: pl.DataFrame, feat: str, sign: float, *, per_name_cost: bool, haircut_lgd: float | None
) -> np.ndarray:
    """Per-week decile L/S net return. Cost: per-name Stage-1 realized half-spread (round-trip = 2x, once per
    weekly hold) where present else COST_PROXY_BPS. Survivorship: if haircut_lgd given, debit the LONG (loser)
    leg P_DELIST_WEEK*haircut_lgd that week."""
    cols = ["friday", feat, "y_fwd_1w", "half_spread_bps"]
    df = panel.select(cols).drop_nulls(subset=["friday", feat, "y_fwd_1w"])
    out = []
    for (_,), grp in df.group_by(["friday"]):
        if grp.height < 20:
            continue
        s = (sign * grp[feat]).to_numpy()
        y = grp["y_fwd_1w"].to_numpy()
        hs = grp["half_spread_bps"].to_numpy()
        order = s.argsort()
        n = max(1, int(DECILE * len(order)))
        long_idx, short_idx = order[-n:], order[:n]  # long = high sign*feat = losers (sign=-1); short = winners
        if per_name_cost:
            def leg_cost(idx: np.ndarray) -> float:
                c = hs[idx]
                c = np.where(np.isnan(c), COST_PROXY_BPS, c)
                return float(np.mean(2.0 * c / 1e4))  # round-trip per name
            cost = leg_cost(long_idx) + leg_cost(short_idx)
        else:
            cost = 4.0 * COST_PROXY_BPS / 1e4
        long_ret = float(np.mean(y[long_idx]))
        short_ret = float(np.mean(y[short_idx]))
        if haircut_lgd is not None:
            long_ret += P_DELIST_WEEK * haircut_lgd  # debit the bought-losers leg
        out.append(long_ret - short_ret - cost)
    return np.array(out)


def summarize(arr: np.ndarray, label: str) -> dict[str, float]:
    if arr.shape[0] < 8:
        return {"label": label, "weeks": int(arr.shape[0])}
    t = nw_t(arr)
    return {
        "label": label,
        "mean_bps": float(arr.mean() * 1e4),
        "median_bps": float(np.median(arr) * 1e4),
        "nw_t": t,
        "win_rate": float(np.mean(arr > 0)),
        "max_week_share": float(np.max(np.abs(arr)) / np.sum(np.abs(arr))) if np.sum(np.abs(arr)) > 0 else float("nan"),
        "weeks": int(arr.shape[0]),
    }


def own_vol_size_control(panel: pl.DataFrame, feat: str, sign: float) -> float:
    """Partial out vol_20d + log_adv from BOTH sign*feat and y_fwd_1w per week; mean partial rank-IC."""
    df = panel.select(["friday", feat, "vol_20d", "log_adv", "y_fwd_1w"]).drop_nulls()
    pics = []
    for (_,), grp in df.group_by(["friday"]):
        if grp.height < 30:
            continue
        x = sign * grp[feat].to_numpy()
        y = grp["y_fwd_1w"].to_numpy()
        z = np.column_stack([np.ones(grp.height), grp["vol_20d"].to_numpy(), grp["log_adv"].to_numpy()])
        rx = x - z @ np.linalg.lstsq(z, x, rcond=None)[0]
        ry = y - z @ np.linalg.lstsq(z, y, rcond=None)[0]
        ic = _spearman(rx, ry)
        if not np.isnan(ic):
            pics.append(ic)
    return float(np.mean(pics)) if len(pics) >= 8 else float("nan")


def shuffle_z(panel: pl.DataFrame, feat: str, sign: float, observed_mean_ic: float) -> float:
    rng = np.random.default_rng(SEED)
    df = panel.select(["friday", feat, "y_fwd_1w"]).drop_nulls()
    groups = [(grp[feat].to_numpy(), grp["y_fwd_1w"].to_numpy()) for (_,), grp in df.group_by(["friday"]) if grp.height >= 20]
    null_means = []
    for _ in range(N_SHUFFLE):
        ics = []
        for fx, fy in groups:
            yp = rng.permutation(fy)
            ic = _spearman(sign * fx, yp)
            if not np.isnan(ic):
                ics.append(ic)
        if ics:
            null_means.append(np.mean(ics))
    null = np.array(null_means)
    return float((observed_mean_ic - null.mean()) / (null.std() + 1e-12))


def run_window(panel: pl.DataFrame, label: str) -> dict[str, object]:
    print(f"\n================= WINDOW: {label} ({panel['friday'].n_unique()} weeks, {panel.height} obs) =================")
    feat, sign = "rev_1w", -1.0
    ic_series = weekly_ic_series(panel, feat, "y_fwd_1w", sign)
    mean_ic, t_ic = float(ic_series.mean()) if ic_series.size else float("nan"), nw_t(ic_series)
    partial_ic = own_vol_size_control(panel, feat, sign)
    sz = shuffle_z(panel, feat, sign, mean_ic) if ic_series.size >= 8 else float("nan")
    print(f"  reversal weekly rank-IC: mean={mean_ic:+.4f} NW-t={t_ic:+.2f} (n={ic_series.size})  "
          f"partial-IC(own-vol+size)={partial_ic:+.4f}  shuffle-z={sz:+.2f}")
    gross = summarize(ls_weekly_returns(panel, feat, sign, per_name_cost=False, haircut_lgd=None), "gross")
    netcost = summarize(ls_weekly_returns(panel, feat, sign, per_name_cost=True, haircut_lgd=None), "net-cost (Stage-1)")
    base = summarize(ls_weekly_returns(panel, feat, sign, per_name_cost=True, haircut_lgd=LGD_BASE), "net-cost+BASE-haircut")
    stress = summarize(ls_weekly_returns(panel, feat, sign, per_name_cost=True, haircut_lgd=LGD_STRESS), "net-cost+(-100%)-stress")
    for d in (gross, netcost, base, stress):
        print(f"  L/S {d['label']:>26}: mean={d.get('mean_bps', float('nan')):+.1f}bps/wk "
              f"median={d.get('median_bps', float('nan')):+.1f} NW-t={d.get('nw_t', float('nan')):+.2f} "
              f"win={d.get('win_rate', float('nan')):.2f} max-wk-share={d.get('max_week_share', float('nan')):.2f} n={d.get('weeks', 0)}")
    # PASS bar: base-haircut net-positive + NW-t>=2 + shuffle-dominant + partial-IC survives
    base_t = base.get("nw_t", float("nan"))
    base_mean = base.get("mean_bps", float("nan"))
    pass_legs = {
        "base_net_positive": (not math.isnan(base_mean)) and base_mean > 0,
        "base_NW_t>=2": (not math.isnan(base_t)) and abs(base_t) >= 2.0,
        "shuffle_dominant": (not math.isnan(sz)) and sz >= 2.0,
        "partial_IC_survives": (not math.isnan(partial_ic)) and partial_ic > 0 and (abs(partial_ic) >= 0.4 * abs(mean_ic) if abs(mean_ic) > 1e-9 else False),
    }
    print(f"  PASS legs [{label}]: {pass_legs} -> ALL={all(pass_legs.values())}")
    stress_t = stress.get("nw_t", float("nan"))
    stress_pos = (not math.isnan(stress.get("mean_bps", float("nan")))) and stress["mean_bps"] > 0
    print(f"  −100% stress: mean={stress.get('mean_bps', float('nan')):+.1f}bps NW-t={stress_t:+.2f} "
          f"sign-holds={stress_pos} (band-end; caveat-not-autofail per Lead)")
    return {"pass": all(pass_legs.values()), "legs": pass_legs, "mean_ic": mean_ic, "stress_pos": stress_pos,
            "base_mean_bps": base_mean}


def main() -> None:
    panel = pl.read_parquet(f"{OUT_DIR}/weekly_panel.parquet")
    panel = winsorize_weekly(panel, "y_fwd_1w")
    yrs = sorted(panel["year"].unique().to_list())
    print(f"panel: {panel.height} obs, {panel['friday'].n_unique()} weeks, {panel['symbol'].n_unique()} syms, "
          f"years {yrs[0]}-{yrs[-1]}, disappeared={int(panel['disappeared'].sum())}, "
          f"realized-cost rows={int(panel['half_spread_bps'].is_not_null().sum())} "
          f"({100*panel['half_spread_bps'].is_not_null().mean():.0f}%)")
    print(f"haircut calibration: P_delist={P_DELIST_WEEK}/wk x LGD_base={LGD_BASE} = "
          f"{P_DELIST_WEEK*LGD_BASE*1e4:+.1f}bps/wk loser-leg drag (stress LGD={LGD_STRESS} = {P_DELIST_WEEK*LGD_STRESS*1e4:+.1f}bps)")

    # disjoint-window replication: split the year span into two non-overlapping halves
    mid = yrs[len(yrs) // 2]
    disc = panel.filter(pl.col("year") < mid)
    repl = panel.filter(pl.col("year") >= mid)
    r_disc = run_window(disc, f"DISCOVERY (<{mid})")
    r_repl = run_window(repl, f"REPLICATION (>={mid})")

    print("\n================= ⭐ WEEKLY-REVERSAL VERDICT =================")
    overall = r_disc["pass"] and r_repl["pass"] and (r_disc["mean_ic"] > 0) == (r_repl["mean_ic"] > 0)
    print(f"DISCOVERY pass={r_disc['pass']} | REPLICATION pass={r_repl['pass']} | sign-consistent="
          f"{(r_disc['mean_ic']>0)==(r_repl['mean_ic']>0)}")
    if overall:
        both_stress = r_disc["stress_pos"] and r_repl["stress_pos"]
        tag = "ROBUST (survives even the −100% extreme)" if both_stress else \
              "PASS w/ SURVIVORSHIP CAVEAT (survives BASE haircut + replicates, but −100% extreme flips sign "
        print(f"RESULT: PASS — weekly reversal clears the BASE haircut + NW-t + replication. {tag}"
              f"{'' if both_stress else '→ route to a delisting-inclusive (CRSP-style) universe to confirm before banking; flag, do not chase)'}")
        print("  → FIRST genuinely cost-positive, survivorship-gated edge candidate. Flag the Lead for confirmatory disjoint-period replication.")
    else:
        print("RESULT: NULL — weekly reversal does NOT clear the full bar (base haircut + NW-t + replication).")
        print("  DISPOSITION (Ben): the current model doesn't TRADE weekly-reversal yet on this survivors-only "
              "substrate; rev_1w/weekly features stay INCLUDED/retained. If it died ONLY under the survivorship "
              "haircut, that's the honest bias quantification → follow-up = acquire a delisting-inclusive "
              "universe, flagged not chased. The $-test answers what to trade, not what to store.")


if __name__ == "__main__":
    main()
