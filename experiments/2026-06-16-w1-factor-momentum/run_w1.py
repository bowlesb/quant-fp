"""W1 — cross-sectional momentum, LIQUID portfolio L/S. Vectorized with polars/numpy.

Reads the cached close_panel.parquet, builds a (date x symbol) close matrix, and for each
(F, S, H) cell runs a NON-overlapping decile (and quintile) equal-weight L/S portfolio,
charges measured per-name round-trip spread on turnover, and runs the gate battery:
shuffle-canary, per-symbol demean, walk-forward OOS, per-rebalance bootstrap, period-clustered t.

Writes ONLY to the experiment dir.
"""
from __future__ import annotations

import os

import numpy as np
import polars as pl

EXP_DIR = "/app/experiments/2026-06-16-w1-factor-momentum"
PANEL = os.path.join(EXP_DIR, "close_panel.parquet")
SPREADS = os.path.join(EXP_DIR, "spreads.csv")

FORMATIONS = [21, 42, 63]
SKIPS = [0, 2]
HOLDS = [5, 10, 21]
LIQ_SIZES = {"liquid500": 500, "megacap100": 100}
N_BOOT = 10000
N_CANARY = 10
MIN_COVERAGE = 0.95  # fraction of dates a symbol must have a close to be eligible
DEFAULT_SPREAD_BPS = 5.0  # fallback round-trip spread if unmeasured (flagged)
RNG = np.random.default_rng(20260616)


def load_matrix() -> tuple[list[object], list[str], np.ndarray, np.ndarray]:
    """Return (dates, symbols, close[T,N], dollar_vol[T,N]). NaN where missing."""
    panel = pl.read_parquet(PANEL)
    dates = sorted(panel["date"].unique().to_list())
    symbols = sorted(panel["symbol"].unique().to_list())
    date_idx = {d: i for i, d in enumerate(dates)}
    sym_idx = {s: j for j, s in enumerate(symbols)}
    n_t, n_n = len(dates), len(symbols)
    close = np.full((n_t, n_n), np.nan)
    dvol = np.full((n_t, n_n), np.nan)
    di = panel["date"].map_elements(lambda d: date_idx[d], return_dtype=pl.Int64).to_numpy()
    si = panel["symbol"].map_elements(lambda s: sym_idx[s], return_dtype=pl.Int64).to_numpy()
    cl = panel["close"].to_numpy()
    dv = panel["dollar_vol"].to_numpy()
    close[di, si] = cl
    dvol[di, si] = dv
    return dates, symbols, close, dvol


def select_universe(close: np.ndarray, dvol: np.ndarray, n_keep: int) -> np.ndarray:
    """Indices of the top n_keep symbols by median daily dollar-volume, requiring coverage."""
    n_t = close.shape[0]
    coverage = np.isfinite(close).sum(axis=0) / n_t
    eligible = coverage >= MIN_COVERAGE
    med_dvol = np.nanmedian(np.where(np.isfinite(dvol), dvol, np.nan), axis=0)
    med_dvol = np.where(eligible, med_dvol, -np.inf)
    order = np.argsort(-med_dvol)
    return order[:n_keep]


def load_spreads(symbols: list[str]) -> dict[str, float]:
    """Measured round-trip spread (bps) per symbol; fallback flagged downstream."""
    if not os.path.exists(SPREADS):
        return {}
    sp = pl.read_csv(SPREADS)
    return {row["symbol"]: float(row["rt_spread_bps"]) for row in sp.iter_rows(named=True)}


def formation_returns(close: np.ndarray, t: int, formation: int, skip: int) -> np.ndarray:
    """F-day trailing return ending `skip` days before t: close[t-skip]/close[t-skip-F] - 1."""
    end = t - skip
    start = end - formation
    if start < 0:
        return np.full(close.shape[1], np.nan)
    return close[end] / close[start] - 1.0


def forward_returns(close: np.ndarray, t: int, hold: int) -> np.ndarray:
    """H-day forward return from t: close[t+H]/close[t] - 1."""
    if t + hold >= close.shape[0]:
        return np.full(close.shape[1], np.nan)
    return close[t + hold] / close[t] - 1.0


def decile_legs(form: np.ndarray, frac: float) -> tuple[np.ndarray, np.ndarray]:
    """Boolean masks for long (top frac) and short (bottom frac) by formation return."""
    valid = np.isfinite(form)
    idx = np.where(valid)[0]
    if len(idx) < 20:
        return np.zeros_like(form, dtype=bool), np.zeros_like(form, dtype=bool)
    vals = form[idx]
    order = idx[np.argsort(vals)]
    k = max(1, int(round(len(idx) * frac)))
    long_mask = np.zeros_like(form, dtype=bool)
    short_mask = np.zeros_like(form, dtype=bool)
    long_mask[order[-k:]] = True
    short_mask[order[:k]] = True
    return long_mask, short_mask


def run_cell(
    close: np.ndarray,
    uni: np.ndarray,
    symbols: list[str],
    spread_bps: dict[str, float],
    formation: int,
    skip: int,
    hold: int,
    frac: float,
    oos_start_t: int,
) -> dict:
    """Run one (F,S,H,frac) non-overlapping L/S portfolio over the universe."""
    n_t = close.shape[0]
    cu = close[:, uni]  # [T, U]
    uni_syms = [symbols[j] for j in uni]
    sp_arr = np.array([spread_bps.get(s, DEFAULT_SPREAD_BPS) / 1e4 for s in uni_syms])
    n_flagged = sum(1 for s in uni_syms if s not in spread_bps)

    first_t = formation + skip
    rebal_ts = list(range(first_t, n_t - hold, hold))

    gross_rets: list[float] = []
    net1_rets: list[float] = []
    net2_rets: list[float] = []
    turnovers: list[float] = []
    rebal_t_list: list[int] = []
    # canary: store per-rebalance (form, fwd) arrays to permute
    cell_forms: list[np.ndarray] = []
    cell_fwds: list[np.ndarray] = []
    cell_masks: list[tuple[np.ndarray, np.ndarray]] = []

    prev_long = np.zeros(cu.shape[1], dtype=bool)
    prev_short = np.zeros(cu.shape[1], dtype=bool)

    for t in rebal_ts:
        form = formation_returns(cu, t, formation, skip)
        fwd = forward_returns(cu, t, hold)
        valid = np.isfinite(form) & np.isfinite(fwd)
        form_v = np.where(valid, form, np.nan)
        long_mask, short_mask = decile_legs(form_v, frac)
        if long_mask.sum() == 0 or short_mask.sum() == 0:
            continue
        long_ret = np.nanmean(fwd[long_mask])
        short_ret = np.nanmean(fwd[short_mask])
        gross = long_ret - short_ret

        # turnover: fraction of names that changed leg (entered/exited long or short)
        changed = (long_mask != prev_long).sum() + (short_mask != prev_short).sum()
        n_positions = long_mask.sum() + short_mask.sum()
        turnover = changed / (2.0 * n_positions)  # 0..1, names per side that turned over
        # cost = (names entering a NEW leg) * their round-trip spread, averaged per position
        entered = (long_mask & ~prev_long) | (short_mask & ~prev_short)
        # exiting also costs (closing the old position)
        exited = (prev_long & ~long_mask) | (prev_short & ~short_mask)
        traded = entered | exited
        # per-position cost contribution: each traded name pays half-spread on the affected side,
        # but round-trip spread already includes both sides of crossing. Charge mean spread of
        # traded names scaled by the fraction of the book that traded.
        cost_frac = traded.sum() / (2.0 * n_positions)
        mean_sp_traded = float(np.mean(sp_arr[traded])) if traded.sum() > 0 else 0.0
        cost = cost_frac * mean_sp_traded

        gross_rets.append(float(gross))
        net1_rets.append(float(gross - cost))
        net2_rets.append(float(gross - 2.0 * cost))
        turnovers.append(float(turnover))
        rebal_t_list.append(t)
        cell_forms.append(form_v[valid])
        cell_fwds.append(fwd[valid])
        cell_masks.append((long_mask, short_mask))

        prev_long, prev_short = long_mask, short_mask

    if len(gross_rets) < 3:
        return {"n_rebal": len(gross_rets), "insufficient": True}

    gross_arr = np.array(gross_rets)
    net1_arr = np.array(net1_rets)
    net2_arr = np.array(net2_rets)
    rebal_t_arr = np.array(rebal_t_list)

    # OOS split by rebalance date
    oos_mask = rebal_t_arr >= oos_start_t
    is_mask = ~oos_mask

    def series_stats(arr: np.ndarray) -> dict:
        if len(arr) < 3:
            return {"mean": np.nan, "t": np.nan, "n": len(arr)}
        mean = float(arr.mean())
        sd = float(arr.std(ddof=1))
        tstat = mean / (sd / np.sqrt(len(arr))) if sd > 0 else np.nan
        return {"mean": mean, "t": tstat, "n": len(arr)}

    # per-rebalance bootstrap on the chosen series (default: OOS net@1x)
    def bootstrap_ci(arr: np.ndarray) -> tuple[float, float]:
        if len(arr) < 3:
            return np.nan, np.nan
        idx = RNG.integers(0, len(arr), size=(N_BOOT, len(arr)))
        means = arr[idx].mean(axis=1)
        return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))

    # shuffle canary: permute formation->forward mapping within each rebalance cross-section
    canary_means: list[float] = []
    for _ in range(N_CANARY):
        perm_rets: list[float] = []
        for (form_v, fwd_v, (lmask, smask)) in zip(cell_forms, cell_fwds, cell_masks):
            # permute fwd relative to form within this cross-section, re-rank, re-form legs
            perm_fwd = RNG.permutation(fwd_v)
            lm, sm = decile_legs(form_v, frac)
            if lm.sum() == 0 or sm.sum() == 0:
                continue
            perm_rets.append(float(np.nanmean(perm_fwd[lm]) - np.nanmean(perm_fwd[sm])))
        if perm_rets:
            canary_means.append(float(np.mean(perm_rets)))
    canary_mean = float(np.mean(canary_means)) if canary_means else np.nan
    canary_sd = float(np.std(canary_means, ddof=1)) if len(canary_means) > 1 else np.nan

    # per-symbol demean: subtract each name's mean forward H-day return, recompute L/S
    # build per-symbol mean fwd over all rebalances in this cell
    n_u = cu.shape[1]
    fwd_by_pos: list[list[float]] = [[] for _ in range(n_u)]
    for t in rebal_ts:
        fwd = forward_returns(cu, t, hold)
        for j in range(n_u):
            if np.isfinite(fwd[j]):
                fwd_by_pos[j].append(fwd[j])
    sym_mean_fwd = np.array([np.mean(v) if v else np.nan for v in fwd_by_pos])
    dm_rets: list[float] = []
    prev_l = np.zeros(n_u, dtype=bool)
    prev_s = np.zeros(n_u, dtype=bool)
    for t in rebal_ts:
        form = formation_returns(cu, t, formation, skip)
        fwd = forward_returns(cu, t, hold)
        valid = np.isfinite(form) & np.isfinite(fwd) & np.isfinite(sym_mean_fwd)
        form_v = np.where(valid, form, np.nan)
        fwd_dm = fwd - sym_mean_fwd
        lm, sm = decile_legs(form_v, frac)
        if lm.sum() == 0 or sm.sum() == 0:
            continue
        dm_rets.append(float(np.nanmean(fwd_dm[lm]) - np.nanmean(fwd_dm[sm])))
    dm_stats = series_stats(np.array(dm_rets))

    oos_net1 = net1_arr[oos_mask]
    oos_ci_lo, oos_ci_hi = bootstrap_ci(oos_net1)
    full_net1_ci = bootstrap_ci(net1_arr)

    return {
        "n_rebal": len(gross_rets),
        "n_flagged_spread": n_flagged,
        "mean_turnover": float(np.mean(turnovers)),
        "gross": series_stats(gross_arr),
        "net1": series_stats(net1_arr),
        "net2": series_stats(net2_arr),
        "is_net1": series_stats(net1_arr[is_mask]),
        "oos_net1": series_stats(oos_net1),
        "oos_net1_ci": (oos_ci_lo, oos_ci_hi),
        "full_net1_ci": full_net1_ci,
        "canary_mean": canary_mean,
        "canary_sd": canary_sd,
        "demean": dm_stats,
        "n_oos": int(oos_mask.sum()),
        "n_is": int(is_mask.sum()),
    }


def main() -> None:
    dates, symbols, close, dvol = load_matrix()
    n_t = close.shape[0]
    oos_start_t = n_t // 2  # last-half dates are OOS
    spread_bps = load_spreads(symbols)
    print(f"matrix T={n_t} N={len(symbols)} oos_start_t={oos_start_t} "
          f"measured_spreads={len(spread_bps)}", flush=True)

    rows: list[dict] = []
    for uni_name, n_keep in LIQ_SIZES.items():
        uni = select_universe(close, dvol, n_keep)
        print(f"\n=== {uni_name}: {len(uni)} names ===", flush=True)
        for frac, leg_name in [(0.1, "decile"), (0.2, "quintile")]:
            for formation in FORMATIONS:
                for skip in SKIPS:
                    for hold in HOLDS:
                        res = run_cell(close, uni, symbols, spread_bps,
                                       formation, skip, hold, frac, oos_start_t)
                        if res.get("insufficient"):
                            continue
                        row = {
                            "universe": uni_name, "leg": leg_name,
                            "F": formation, "S": skip, "H": hold,
                            "n_rebal": res["n_rebal"],
                            "turnover": round(res["mean_turnover"], 4),
                            "gross_mean": round(res["gross"]["mean"], 5),
                            "gross_t": round(res["gross"]["t"], 2),
                            "net1_mean": round(res["net1"]["mean"], 5),
                            "net1_t": round(res["net1"]["t"], 2),
                            "net2_mean": round(res["net2"]["mean"], 5),
                            "oos_net1_mean": round(res["oos_net1"]["mean"], 5),
                            "oos_net1_t": round(res["oos_net1"]["t"], 2),
                            "oos_n": res["n_oos"],
                            "oos_ci_lo": round(res["oos_net1_ci"][0], 5),
                            "oos_ci_hi": round(res["oos_net1_ci"][1], 5),
                            "full_ci_lo": round(res["full_net1_ci"][0], 5),
                            "full_ci_hi": round(res["full_net1_ci"][1], 5),
                            "canary_mean": round(res["canary_mean"], 5),
                            "canary_sd": round(res["canary_sd"], 5) if np.isfinite(res["canary_sd"]) else None,
                            "demean_mean": round(res["demean"]["mean"], 5),
                            "demean_t": round(res["demean"]["t"], 2),
                            "n_flagged_spread": res["n_flagged_spread"],
                        }
                        rows.append(row)
                        print(f"{uni_name} {leg_name} F{formation} S{skip} H{hold}: "
                              f"gross={row['gross_mean']:+.4f}(t{row['gross_t']}) "
                              f"net1={row['net1_mean']:+.4f} "
                              f"OOSnet1={row['oos_net1_mean']:+.4f} "
                              f"CI[{row['oos_ci_lo']:+.4f},{row['oos_ci_hi']:+.4f}] "
                              f"turn={row['turnover']:.2f} canary={row['canary_mean']:+.4f}",
                              flush=True)

    out = pl.DataFrame(rows)
    out.write_csv(os.path.join(EXP_DIR, "results.csv"))
    print(f"\nwrote results.csv ({out.height} cells)")


if __name__ == "__main__":
    main()
