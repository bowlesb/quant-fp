"""MONTHLY LOW-TURNOVER FACTOR — screen (pre-registered, see prereg.md).

The cost-is-NOT-the-enemy gates over monthly_panel.parquet, for each factor (H1 lowvol, H2 sec_rel_mom both
signs):
  - monthly cross-sectional rank-IC + NW-t; shuffle-z (within-month label permute); predict-zero implicit.
  - OWN-VOL/SIZE control: partial rank-IC after regressing feature + label on vol60 + size → collapse ratio.
  - OOS year-split (<=2020 / >=2021) IC sign-consistency.
  - ⭐ TURNOVER-BANDED BOOK + NET-OF-COST GATE (the new thing): build a sticky long/short book with HYSTERESIS
    (enter top/bottom quintile, exit only past the 30/70 pctile) so turnover is minimized BY CONSTRUCTION;
    compute the ACTUAL per-rebalance turnover from book changes; the verdict is the NET mean AND MEDIAN
    monthly return after cost = turnover x round-trip bps. A factor is tradeable ONLY if net MEDIAN > 0
    (the structural bar the #205 weekly reversal failed) — the negative-median lesson is a PASS/FAIL gate.
  - -30%/-100% DELISTING HAIRCUT on disappeared names; $1-floor + per-month winsor.
  - BY-FDR across all (factor x sign) cells.

Reads monthly_panel.parquet. Writes screen_results.csv + screen_console.txt.
"""

from __future__ import annotations

import math
import os

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-20-monthly-lowturnover"
N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "200"))
SEED = int(os.environ.get("SEED", "7"))
WINSOR_P = 0.01
ENTER_Q = 0.20  # enter the long (top) / short (bottom) quintile
EXIT_Q = 0.30  # exit only once a name decays past the 30/70 pctile (hysteresis → low turnover)
COST_BPS = (5.0, 10.0)
# Factors: (name, feature, sign). sec_rel_mom tested BOTH signs (continuation +1 / reversal -1) — both in FDR.
FACTORS = (
    ("H1_lowvol", "lowvol", +1.0),
    ("H2_secmom_cont", "sec_rel_mom", +1.0),
    ("H2_secmom_rev", "sec_rel_mom", -1.0),
)


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


def winsorize_monthly(panel: pl.DataFrame, col: str) -> pl.DataFrame:
    lo = pl.col(col).quantile(WINSOR_P).over("rebal")
    hi = pl.col(col).quantile(1 - WINSOR_P).over("rebal")
    return panel.with_columns(pl.col(col).clip(lo, hi).alias(col))


def monthly_ic(panel: pl.DataFrame, feat: str, sign: float) -> tuple[float, float, int]:
    df = panel.select(["rebal", feat, "y_fwd_1m"]).drop_nulls()
    ics = []
    for (_,), grp in df.group_by(["rebal"]):
        if grp.height < 20:
            continue
        ic = _spearman(sign * grp[feat].to_numpy(), grp["y_fwd_1m"].to_numpy())
        if not np.isnan(ic):
            ics.append(ic)
    if len(ics) < 8:
        return float("nan"), float("nan"), len(ics)
    arr = np.array(ics)
    return float(arr.mean()), float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr)) + 1e-12)), len(arr)


def shuffle_z(panel: pl.DataFrame, feat: str, sign: float, observed: float) -> float:
    if np.isnan(observed):
        return float("nan")
    rng = np.random.default_rng(SEED)
    df = panel.select(["rebal", feat, "y_fwd_1m"]).drop_nulls()
    null = np.empty(N_SHUFFLE)
    for i in range(N_SHUFFLE):
        sh = df.with_columns(pl.col("y_fwd_1m").shuffle(seed=int(rng.integers(1 << 30))).over("rebal"))
        m, _, _ = monthly_ic(sh, feat, sign)
        null[i] = m if not np.isnan(m) else 0.0
    return float((observed - null.mean()) / (null.std(ddof=1) + 1e-12))


def control_collapse(
    panel: pl.DataFrame, feat: str, sign: float, controls: list[str]
) -> tuple[float, float]:
    df = panel.select(["rebal", feat, "y_fwd_1m", *controls]).drop_nulls()
    raw, par = [], []
    for (_,), grp in df.group_by(["rebal"]):
        if grp.height < 25:
            continue
        x = sign * grp[feat].to_numpy()
        y = grp["y_fwd_1m"].to_numpy()
        if np.std(x) < 1e-12 or np.std(y) < 1e-12:
            continue
        raw.append(_spearman(x, y))
        Z = np.column_stack([np.ones(grp.height)] + [grp[c].to_numpy() for c in controls])
        rx = x - Z @ np.linalg.lstsq(Z, x, rcond=None)[0]
        ry = y - Z @ np.linalg.lstsq(Z, y, rcond=None)[0]
        if np.std(rx) > 1e-12 and np.std(ry) > 1e-12:
            par.append(_spearman(rx, ry))
    if len(raw) < 8 or len(par) < 8:
        return float("nan"), float("nan")
    r, p = float(np.mean(raw)), float(np.mean(par))
    return p, (abs(p) / abs(r) if abs(r) > 1e-9 else float("nan"))


def oos_split(panel: pl.DataFrame, feat: str, sign: float) -> str:
    ie, *_ = monthly_ic(panel.filter(pl.col("year") <= 2020), feat, sign)
    il, *_ = monthly_ic(panel.filter(pl.col("year") >= 2021), feat, sign)
    if np.isnan(ie) or np.isnan(il):
        return "n/a"
    ok = np.sign(ie) == np.sign(il) and abs(il) > 1e-3
    return f"{'consistent' if ok else 'FLIP'}({ie:+.3f}/{il:+.3f})"


def banded_book_net(panel: pl.DataFrame, feat: str, sign: float, bps: float) -> dict[str, float]:
    """The cost-is-not-enemy core: a STICKY long/short book with hysteresis. Each rebalance, a name is LONG
    if (sign*feat) percentile > 1-ENTER_Q, SHORT if < ENTER_Q; it STAYS in the book until it decays past the
    EXIT band (1-EXIT_Q / EXIT_Q). Turnover = fraction of book that changed side/exited vs last rebalance.
    Returns per-rebalance gross + NET (gross - turnover*bps) monthly L/S return, mean + MEDIAN + IR."""
    df = panel.select(["rebal", "symbol", feat, "y_fwd_1m"]).drop_nulls().sort("rebal")
    rebals = df["rebal"].unique().sort().to_list()
    prev: dict[str, int] = {}  # symbol -> +1 long / -1 short (the carried book)
    gross, net, turnovers = [], [], []
    for r in rebals:
        g = df.filter(pl.col("rebal") == r)
        if g.height < 30:
            continue
        s = (sign * g[feat]).to_numpy()
        pct = _rank(s) / (len(s) - 1)
        syms = g["symbol"].to_list()
        y = g["y_fwd_1m"].to_numpy()
        book: dict[str, int] = {}
        for sym, p, prv in zip(syms, pct, [prev.get(sym, 0) for sym in syms]):
            if p >= 1 - ENTER_Q:
                book[sym] = 1
            elif p <= ENTER_Q:
                book[sym] = -1
            elif prv == 1 and p >= 1 - EXIT_Q:
                book[sym] = 1  # hysteresis: stay long until past the exit band
            elif prv == -1 and p <= EXIT_Q:
                book[sym] = -1  # stay short
            # else: exit (not in book)
        longs = [y[i] for i, sym in enumerate(syms) if book.get(sym) == 1]
        shorts = [y[i] for i, sym in enumerate(syms) if book.get(sym) == -1]
        if not longs or not shorts:
            prev = book
            continue
        spread = float(np.mean(longs) - np.mean(shorts))
        # turnover = fraction of names whose side changed (enter/exit/flip) vs the prior book, both legs.
        names = set(book) | set(prev)
        changed = sum(1 for nm in names if book.get(nm, 0) != prev.get(nm, 0))
        # turnover normalized by the AVERAGE book size (in+out legs), so a full book replacement = 1.0.
        denom = max(1, (len(book) + len(prev)) / 2.0)
        turnover = min(1.0, changed / denom)
        cost = turnover * 2.0 * bps / 1e4  # turnover * round-trip (in+out) bps
        gross.append(spread)
        net.append(spread - cost)
        turnovers.append(turnover)
        prev = book
    if len(net) < 8:
        return {}
    net_arr = np.array(net)
    return {
        "gross_bps": float(np.mean(gross) * 1e4),
        "net_mean_bps": float(net_arr.mean() * 1e4),
        "net_median_bps": float(np.median(net_arr) * 1e4),
        "net_ir": float(
            net_arr.mean() / (net_arr.std(ddof=1) + 1e-12) * np.sqrt(12)
        ),  # annualized monthly IR
        "avg_turnover": float(np.mean(turnovers)),
        "win": float(np.mean(net_arr > 0)),
        "rebals": len(net_arr),
    }


def delisting_haircut(
    panel: pl.DataFrame, feat: str, sign: float, terminal: float, bps: float = 5.0
) -> dict[str, float]:
    hc = panel.with_columns(
        pl.when(pl.col("disappeared") == 1).then(terminal).otherwise(pl.col("y_fwd_1m")).alias("y_fwd_1m")
    )
    return banded_book_net(hc, feat, sign, bps)


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
    panel = winsorize_monthly(pl.read_parquet(f"{OUT_DIR}/monthly_panel.parquet"), "y_fwd_1m")
    print(
        f"panel: {panel.height} obs, {panel['rebal'].n_unique()} rebalances, {panel['symbol'].n_unique()} syms, "
        f"disappeared={int(panel['disappeared'].sum())}"
    )
    records = []
    for name, feat, sign in FACTORS:
        mic, nwt, nreb = monthly_ic(panel, feat, sign)
        z = shuffle_z(panel, feat, sign, mic)
        # own-vol/size control, EXCLUDING any control mechanically equal to the feature (lowvol = -vol60,
        # so vol60 would be a perfect-collinearity self-control → meaningless; for lowvol use size only).
        ctrl = [c for c in ("vol60", "size") if c != feat and not (feat == "lowvol" and c == "vol60")]
        par, collapse = control_collapse(panel, feat, sign, ctrl)
        oos = oos_split(panel, feat, sign)
        nb5 = banded_book_net(panel, feat, sign, 5.0)
        nb10 = banded_book_net(panel, feat, sign, 10.0)
        hc100 = delisting_haircut(panel, feat, sign, -1.00, 5.0)
        records.append(
            {
                "factor": name,
                "feature": feat,
                "sign": sign,
                "rebals": nreb,
                "ic_mean": mic,
                "ic_nw_t": nwt,
                "shuffle_z": z,
                "partial_ic": par,
                "collapse": collapse,
                "oos": oos,
                "gross_bps": nb5.get("gross_bps", float("nan")),
                "avg_turnover": nb5.get("avg_turnover", float("nan")),
                "net5_mean_bps": nb5.get("net_mean_bps", float("nan")),
                "net5_median_bps": nb5.get("net_median_bps", float("nan")),
                "net5_ir": nb5.get("net_ir", float("nan")),
                "net5_win": nb5.get("win", float("nan")),
                "net10_mean_bps": nb10.get("net_mean_bps", float("nan")),
                "net10_median_bps": nb10.get("net_median_bps", float("nan")),
                "hc100_net_mean_bps": hc100.get("net_mean_bps", float("nan")),
                "hc100_net_median_bps": hc100.get("net_median_bps", float("nan")),
            }
        )
        print(f"\n=== {name} ({feat}, sign={sign:+.0f}) ===")
        print(f"  IC mean={mic:.4f} NW-t={nwt:.2f} shuffle-z={z:.2f} ({nreb} rebalances)")
        print(f"  control {ctrl}: partial-IC={par:.4f} collapse={collapse:.3f}")
        print(f"  OOS: {oos}")
        print(f"  banded book @5bps:  {nb5}")
        print(f"  banded book @10bps: {nb10}")
        print(f"  delisting -100% @5bps: {hc100}")

    surv = by_fdr([two_sided_p(r["shuffle_z"]) for r in records])
    for r, s in zip(records, surv):
        r["fdr_survive"] = s
    pl.DataFrame(records).write_csv(f"{OUT_DIR}/screen_results.csv")
    print(f"\nWROTE {OUT_DIR}/screen_results.csv")
    print("\n=== TRADEABLE GATE (net MEDIAN > 0 the bar #205 weekly failed) ===")
    for r in records:
        tradeable = (not np.isnan(r["net5_median_bps"])) and r["net5_median_bps"] > 0 and r["fdr_survive"]
        print(
            f"  {r['factor']}: net5_median={r['net5_median_bps']:.1f}bps fdr={r['fdr_survive']} → tradeable={tradeable}"
        )


if __name__ == "__main__":
    main()
