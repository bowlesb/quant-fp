"""W12 — Net share issuance / buyback L/S on the LIQUID top-500 universe.

Pipeline:
  1. PIT shares: for each (symbol, rebalance_date) take the most-recent XBRL value whose `filed` <=
     rebalance_date (look-ahead-safe). For one symbol use ONE consistent tag (TAG_PRIORITY) for both the
     current and the ~1y-prior endpoint, so the ratio is not corrupted by mixing measures.
  2. Net issuance = log(shares_now_adj / shares_1y_ago).  Split-adjust the OLD count to the NEW basis with
     corporate_actions_pit splits between the two period-end dates. Drop |issuance|>ISSUANCE_CLIP (data/
     split artifacts: >~4x growth or >75% shrink in 1y).  Lower (buyback)=long, higher (issue)=short.
  3. Each quarterly rebalance: equal-weight quintile L/S (long bottom 20%, short top 20%). Hold forward H
     trading days, NON-overlapping.
  4. Forward return per name = close_{t+H}/close_t - 1 (daily panel). Portfolio gross = mean(long)-mean(short).
  5. Gates: shuffle-canary (permute issuance vs forward ret), per-symbol demean, walk-forward OOS split,
     per-rebalance bootstrap (10k) on net-of-cost, cost @ measured liquid spread proxy + 2x.

POWER: ~5-6 non-overlapping quarterly rebalances on 18mo bars. Cross-sectional breadth (~460 names) is the
power, not the time dimension. Reported honestly.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import polars as pl

BASE = "experiments/2026-06-16-w12-net-issuance"
SHARES = f"{BASE}/data/shares_long.parquet"
PANEL = f"{BASE}/data/daily_panel.parquet"
SPLITS = f"{BASE}/data/splits.parquet"
OUT_JSON = f"{BASE}/data/raw_results.json"

HOLD_DAYS = 63
QUINTILE = 0.20
ISSUANCE_CLIP = 1.5
N_BOOT = 10000
CANARY_SEEDS = 20
ROUND_TRIP_COST_BPS = 5.0
SEED = 12

TAG_PRIORITY = [
    "dei:EntityCommonStockSharesOutstanding",
    "us-gaap:CommonStockSharesOutstanding",
    "us-gaap:CommonStockSharesIssued",
    "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic",
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
]

rng = np.random.default_rng(SEED)


def load_trading_days() -> list[date]:
    return sorted(pl.read_parquet(PANEL)["date"].unique().to_list())


def pit_shares_at(shares: pl.DataFrame, as_of: date, tag: str) -> dict[str, dict]:
    """Most-recent share value per symbol for ONE tag whose filed <= as_of (median over same-period members)."""
    avail = shares.filter((pl.col("filed") <= as_of) & (pl.col("tag") == tag))
    if avail.height == 0:
        return {}
    latest_end = avail.group_by("symbol").agg(pl.col("end").max().alias("end"))
    at_end = avail.join(latest_end, on=["symbol", "end"], how="inner")
    out = at_end.group_by(["symbol", "end"]).agg(pl.col("val").median().alias("val")).select("symbol", "end", "val")
    return {r["symbol"]: r for r in out.iter_rows(named=True)}


def split_factor_between(splits_map: dict[str, list[tuple[date, float]]], symbol: str, start: date, end: date) -> float:
    events = splits_map.get(symbol)
    if not events:
        return 1.0
    factor = 1.0
    for ex_date, ratio in events:
        if start < ex_date <= end:
            factor *= ratio
    return factor


def compute_issuance(shares: pl.DataFrame, splits_map: dict, as_of: date) -> pl.DataFrame:
    by_tag_now = {tag: pit_shares_at(shares, as_of, tag) for tag in TAG_PRIORITY}
    by_tag_prior = {tag: pit_shares_at(shares, as_of - timedelta(days=365), tag) for tag in TAG_PRIORITY}
    all_syms = shares["symbol"].unique().to_list()
    rows = []
    for symbol in all_syms:
        for tag in TAG_PRIORITY:
            now = by_tag_now[tag].get(symbol)
            prior = by_tag_prior[tag].get(symbol)
            if now is None or prior is None:
                continue
            val_now, end_now = now["val"], now["end"]
            val_prior, end_prior = prior["val"], prior["end"]
            if val_now <= 0 or val_prior <= 0 or end_now is None or end_prior is None:
                continue
            gap_days = (end_now - end_prior).days
            if gap_days < 270 or gap_days > 460:
                continue
            factor = split_factor_between(splits_map, symbol, end_prior, end_now)
            issuance = float(np.log(val_now / (val_prior * factor)))
            if np.isfinite(issuance) and abs(issuance) <= ISSUANCE_CLIP:
                rows.append({"symbol": symbol, "issuance": issuance, "tag": tag})
            break
    out = pl.DataFrame(rows, schema={"symbol": pl.String, "issuance": pl.Float64, "tag": pl.String})
    return out.filter(pl.col("issuance").is_finite())


def build_panel_pivot(panel: pl.DataFrame) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    for key, sub in panel.group_by("symbol"):
        name = key[0] if isinstance(key, tuple) else key
        out[name] = dict(zip(sub["date"].to_list(), sub["close"].to_list()))
    return out


def forward_return(pivot: dict, symbol: str, t0: date, t1: date) -> float:
    sym = pivot.get(symbol)
    if sym is None:
        return np.nan
    c0, c1 = sym.get(t0), sym.get(t1)
    if c0 is None or c1 is None or c0 <= 0:
        return np.nan
    return c1 / c0 - 1.0


def boot_ci(series: np.ndarray) -> dict:
    if len(series) < 2:
        return {"mean": float(series.mean()) if len(series) else float("nan"), "lo": float("nan"), "hi": float("nan"), "p_gt0": float("nan")}
    boots = np.array([rng.choice(series, size=len(series), replace=True).mean() for _ in range(N_BOOT)])
    return {"mean": float(series.mean()), "lo": float(np.percentile(boots, 2.5)), "hi": float(np.percentile(boots, 97.5)), "p_gt0": float((boots > 0).mean())}


def main() -> None:
    shares = pl.read_parquet(SHARES)
    panel = pl.read_parquet(PANEL)
    splits_df = pl.read_parquet(SPLITS)
    splits_map: dict[str, list[tuple[date, float]]] = {}
    for r in splits_df.iter_rows(named=True):
        splits_map.setdefault(r["symbol"], []).append((r["ex_date"], float(r["split_ratio"])))
    days = load_trading_days()
    pivot = build_panel_pivot(panel)

    rebal_idxs = list(range(0, len(days) - HOLD_DAYS, HOLD_DAYS))
    rebal_days = [days[i] for i in rebal_idxs]
    print(f"Trading days: {len(days)} ({days[0]}..{days[-1]}); rebalances: {len(rebal_days)} -> {rebal_days}")

    per_rebal: list[dict] = []
    pairs_rows: list[dict] = []
    for ri, t0 in zip(rebal_idxs, rebal_days):
        t1 = days[ri + HOLD_DAYS]
        iss = compute_issuance(shares, splits_map, t0)
        if iss.height < 20:
            print(f"  {t0}: {iss.height} names, skip")
            continue
        fwd = [forward_return(pivot, r["symbol"], t0, t1) for r in iss.iter_rows(named=True)]
        iss = iss.with_columns(pl.Series("fwd_ret", fwd)).filter(pl.col("fwd_ret").is_finite()).sort("issuance")
        n = iss.height
        if n < 20:
            print(f"  {t0}: {n} names w/ fwd, skip")
            continue
        for r in iss.iter_rows(named=True):
            pairs_rows.append({"rebal": str(t0), "symbol": r["symbol"], "issuance": r["issuance"], "fwd_ret": r["fwd_ret"]})
        k = max(1, int(round(n * QUINTILE)))
        long_leg, short_leg = iss.head(k), iss.tail(k)
        long_ret, short_ret = float(long_leg["fwd_ret"].mean()), float(short_leg["fwd_ret"].mean())
        gross = long_ret - short_ret
        cost = (ROUND_TRIP_COST_BPS / 1e4) * 1.0 * 2.0
        net = gross - cost
        per_rebal.append({"rebal": str(t0), "exit": str(t1), "n": n, "k": k, "long_ret": long_ret, "short_ret": short_ret,
                          "gross": gross, "cost": cost, "net": net,
                          "long_iss_mean": float(long_leg["issuance"].mean()), "short_iss_mean": float(short_leg["issuance"].mean())})
        print(f"  {t0}->{t1}: n={n} k={k} long={long_ret:+.4f} short={short_ret:+.4f} gross={gross:+.4f} net={net:+.4f}")

    gross_s = np.array([r["gross"] for r in per_rebal])
    net_s = np.array([r["net"] for r in per_rebal])
    cost2x_s = np.array([r["gross"] - 2 * r["cost"] for r in per_rebal])

    pairs = pl.DataFrame(pairs_rows)
    canary_means = []
    for seed in range(CANARY_SEEDS):
        crng = np.random.default_rng(1000 + seed)
        perm = []
        for _rebal, sub in pairs.group_by("rebal"):
            issv = sub["issuance"].to_numpy().copy()
            fwd = sub["fwd_ret"].to_numpy()
            crng.shuffle(issv)
            order = np.argsort(issv)
            nn = len(issv)
            kk = max(1, int(round(nn * QUINTILE)))
            perm.append(fwd[order[:kk]].mean() - fwd[order[-kk:]].mean())
        canary_means.append(float(np.mean(perm)))
    canary = {"mean": float(np.mean(canary_means)), "std": float(np.std(canary_means)), "abs_max": float(np.max(np.abs(canary_means)))}

    sym_mean = pairs.group_by("symbol").agg(pl.col("fwd_ret").mean().alias("sym_mean"))
    dm = pairs.join(sym_mean, on="symbol", how="left").with_columns((pl.col("fwd_ret") - pl.col("sym_mean")).alias("fwd_dm"))
    dm_rebal = []
    for _rebal, sub in dm.group_by("rebal"):
        sub2 = sub.sort("issuance")
        nn = sub2.height
        kk = max(1, int(round(nn * QUINTILE)))
        dm_rebal.append(float(sub2.head(kk)["fwd_dm"].mean()) - float(sub2.tail(kk)["fwd_dm"].mean()))
    dm_s = np.array(dm_rebal)

    n_reb = len(per_rebal)
    half = n_reb // 2
    is_net = net_s[:half] if n_reb >= 2 else net_s
    oos_net = net_s[half:] if n_reb >= 2 else net_s

    results = {
        "config": {"hold_days": HOLD_DAYS, "quintile": QUINTILE, "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
                   "issuance_clip": ISSUANCE_CLIP, "n_rebalances": n_reb, "universe_n": int(panel["symbol"].n_unique())},
        "per_rebal": per_rebal,
        "gross_ci": boot_ci(gross_s), "net_ci": boot_ci(net_s), "cost2x_ci": boot_ci(cost2x_s),
        "canary": canary, "demean_ci": boot_ci(dm_s),
        "oos": {"is_net_mean": float(is_net.mean()) if len(is_net) else float("nan"),
                "oos_net_mean": float(oos_net.mean()) if len(oos_net) else float("nan"),
                "oos_ci": boot_ci(oos_net), "n_oos": int(len(oos_net))},
        "names_with_shares": int(shares["symbol"].n_unique()),
        "avg_names_per_rebal": float(np.mean([r["n"] for r in per_rebal])) if per_rebal else float("nan"),
    }
    with open(OUT_JSON, "w") as f:
        json.dump(results, f, indent=2, default=str)
    nc, gc = results["net_ci"], results["gross_ci"]
    oc = results["oos"]["oos_ci"]
    print("\n===== SUMMARY =====")
    print(f"n_rebalances={n_reb}  names_with_shares={results['names_with_shares']}  avg_names/rebal={results['avg_names_per_rebal']:.0f}")
    print(f"GROSS mean={gc['mean']:+.4f} CI[{gc['lo']:+.4f},{gc['hi']:+.4f}]")
    print(f"NET   mean={nc['mean']:+.4f} CI[{nc['lo']:+.4f},{nc['hi']:+.4f}] p_gt0={nc['p_gt0']:.3f}")
    print(f"NET2x mean={results['cost2x_ci']['mean']:+.4f} CI[{results['cost2x_ci']['lo']:+.4f},{results['cost2x_ci']['hi']:+.4f}]")
    print(f"CANARY mean={canary['mean']:+.4f} abs_max={canary['abs_max']:.4f}")
    print(f"DEMEAN mean={results['demean_ci']['mean']:+.4f} CI[{results['demean_ci']['lo']:+.4f},{results['demean_ci']['hi']:+.4f}]")
    print(f"OOS net mean={results['oos']['oos_net_mean']:+.4f} CI[{oc['lo']:+.4f},{oc['hi']:+.4f}] n_oos={results['oos']['n_oos']}")
    print(f"Wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
