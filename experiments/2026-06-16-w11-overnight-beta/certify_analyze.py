"""W11 CERTIFY — Overnight-BETA premium on the 378d (18mo) deep history.

Reuses the EXACT W11 construction (analyze.py) on the deeper bars + wider universe, and ADDS the
certification controls the 126d run could not do:
  1. ~16-18 NON-overlapping monthly rebalances (vs 3) → real bootstrap power.
  2. THE CONFOUND CONTROL: re-run EXCLUDING the crypto/quantum/AI-speculation open-gapper cohort.
     If the overnight>intraday beta split VANISHES without the gappers, W11 was the regime confound (KILL).
  3. SUB-PERIOD split (2025-H1 / 2025-H2 / 2026-H1): is the split STABLE across regimes?
  4. AUCTION-SLIPPAGE stress beyond the half-spread (overnight bets fill at MOO/MOC auctions).
  5. Walk-forward OOS (second half of rebalances), shuffle-canary.

Market = SPY daily 24h return (spec: rolling 60d OLS on SPY). Beta re-estimated every 21d.
Universe = top liquid single stocks (ETFs/index products EXCLUDED — a sector ETF has beta~1 by
construction and would pollute a single-stock beta sort; SPY itself is the market, beta==1).
"""
from __future__ import annotations

import json

import numpy as np
import polars as pl

DAILY = "/app/experiments/2026-06-16-w11-overnight-beta/certify_daily.parquet"
OUT_JSON = "/app/experiments/2026-06-16-w11-overnight-beta/certify_results.json"

N_LIQUID = 200       # top-N liquid single stocks → ~40/quintile with 5 quintiles
MIN_DAYS = 350       # symbol must have nearly-full depth to be eligible
BETA_WINDOW = 60     # trailing trading days for beta
REBAL_EVERY = 21     # re-estimate beta / rebalance monthly
N_QUINTILES = 5
SPREAD_BPS = 3.0     # per-side quote-spread proxy for a liquid name
AUCTION_SLIP_BPS = 5.0  # extra MOO/MOC auction slippage per side (beyond the half-spread), conservative
RNG_SEED = 11

MARKET_SYM = "SPY"

# Index / sector ETFs to EXCLUDE from the single-stock tradeable beta universe.
ETF_EXCLUDE = {
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO",
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    "SMH", "SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "UDOW", "SDOW",
    "UVXY", "VXX", "VIXY", "SVXY", "GLD", "SLV", "TLT", "HYG", "LQD", "EEM", "EFA",
    "ARKK", "KWEB", "FXI", "USO", "UNG", "GDX", "GDXJ", "TNA", "TZA", "LABU", "LABD",
    "BITO", "IBIT", "FBTC", "NUGT", "JNUG", "BOIL", "KOLD", "ERX", "FAS", "FAZ",
    "YINN", "YANG", "DRIP", "GUSH", "ARKW", "ARKG", "ARKF",
}

# The crypto / quantum / AI-speculation open-gapper cohort (the named 126d confound). Anything in the
# liquid universe that mechanically gaps at the U.S. open because of 24h crypto / overnight-speculation flow.
SPECULATION_COHORT = {
    # crypto-miners / crypto-proxies
    "MSTR", "MARA", "RIOT", "WULF", "CLSK", "BITF", "HUT", "CIFR", "BTBT", "HIVE",
    "APLD", "CORZ", "COIN", "HOOD", "BMNR", "SBET", "CRCL", "GLXY",
    # quantum
    "RGTI", "QBTS", "IONQ", "QUBT", "ARQQ", "LAES",
    # AI / speculative-AI / data-center-AI
    "BBAI", "SOUN", "TEMPUS", "CRWV", "APP", "PLTR", "AI", "SMCI",
    # space / new-economy gappers
    "RKLB", "ASTS", "LUNR", "RDW",
    # nuclear / SMR / AI-power speculation
    "SMR", "OKLO", "NNE", "LEU", "UEC", "UROY", "DNN", "NXE", "CCJ", "VST", "CEG", "GEV", "NRG",
    # fintech-speculation
    "AFRM", "UPST", "SOFI",
}

SUBPERIODS = {
    "2025-H1": ("2025-01-01", "2025-06-30"),
    "2025-H2": ("2025-07-01", "2025-12-31"),
    "2026-H1": ("2026-01-01", "2026-12-31"),
}


def load_returns(exclude_speculation: bool) -> tuple[pl.DataFrame, pl.DataFrame, list]:
    daily = pl.read_parquet(DAILY)
    # market = SPY (compute its 24h return separately, used for beta + as the market)
    daily = daily.sort("symbol", "date")
    daily = daily.with_columns(
        pl.col("rth_close").shift(1).over("symbol").alias("prev_close"),
    )
    daily = daily.with_columns(
        (pl.col("rth_open") / pl.col("prev_close") - 1.0).alias("overnight"),
        (pl.col("rth_close") / pl.col("rth_open") - 1.0).alias("intraday"),
        (pl.col("rth_close") / pl.col("prev_close") - 1.0).alias("ret24"),
    )
    daily = daily.filter(
        pl.col("overnight").is_finite() & pl.col("intraday").is_finite() & pl.col("ret24").is_finite()
    )

    mkt = (
        daily.filter(pl.col("symbol") == MARKET_SYM)
        .select(pl.col("date"), pl.col("ret24").alias("mkt_ret24"))
        .sort("date")
    )

    # tradeable single-stock universe: drop ETFs, optionally drop speculation cohort
    excl = set(ETF_EXCLUDE)
    if exclude_speculation:
        excl |= SPECULATION_COHORT
    tradeable = daily.filter(~pl.col("symbol").is_in(list(excl)))

    liq = (
        tradeable.group_by("symbol")
        .agg(pl.col("dollar_vol").median().alias("mdv"), pl.len().alias("n"))
        .filter(pl.col("n") >= MIN_DAYS)
        .sort("mdv", descending=True)
    )
    liquid_syms = liq.head(N_LIQUID)["symbol"].to_list()
    tradeable = tradeable.filter(pl.col("symbol").is_in(liquid_syms)).sort("symbol", "date")
    dates = sorted(tradeable["date"].unique().to_list())
    return tradeable, mkt, dates


def estimate_betas(daily: pl.DataFrame, mkt: pl.DataFrame, est_date_idx: int, dates: list) -> dict[str, float]:
    lo_idx = max(0, est_date_idx - BETA_WINDOW)
    window_dates = dates[lo_idx:est_date_idx]
    if len(window_dates) < BETA_WINDOW // 2:
        return {}
    sub = daily.filter(pl.col("date").is_in(window_dates)).join(mkt, on="date", how="left")
    betas: dict[str, float] = {}
    mkt_by_sym = sub.select(["symbol", "ret24", "mkt_ret24"]).drop_nulls()
    for sym, cell in mkt_by_sym.group_by("symbol"):
        symbol = sym[0]
        x = cell["mkt_ret24"].to_numpy()
        y = cell["ret24"].to_numpy()
        if len(x) < 20:
            continue
        var_x = float(np.var(x))
        if var_x == 0.0:
            continue
        beta = float(np.cov(x, y, ddof=0)[0, 1] / var_x)
        betas[symbol] = beta
    return betas


def ls_returns_over_hold(daily: pl.DataFrame, hold_dates: list, high_syms: set, low_syms: set) -> dict[str, float]:
    hold = daily.filter(pl.col("date").is_in(hold_dates))
    out: dict[str, float] = {}
    for col in ("overnight", "intraday", "ret24"):
        high = hold.filter(pl.col("symbol").is_in(list(high_syms))).group_by("date").agg(pl.col(col).mean().alias("h"))
        low = hold.filter(pl.col("symbol").is_in(list(low_syms))).group_by("date").agg(pl.col(col).mean().alias("l"))
        merged = high.join(low, on="date", how="inner")
        ls = (merged["h"] - merged["l"]).to_numpy()
        out[col] = float(np.mean(ls)) if len(ls) else np.nan
        out[col + "_n"] = len(ls)
    return out


def run(exclude_speculation: bool = False, permute_beta: bool = False) -> tuple[list[dict], int]:
    daily, mkt, dates = load_returns(exclude_speculation)
    n_universe = daily["symbol"].n_unique()
    rng = np.random.default_rng(RNG_SEED)
    results: list[dict] = []
    start_idx = BETA_WINDOW
    rebal_idxs = list(range(start_idx, len(dates) - 1, REBAL_EVERY))
    for r, est_idx in enumerate(rebal_idxs):
        betas = estimate_betas(daily, mkt, est_idx, dates)
        if len(betas) < N_QUINTILES * 5:
            continue
        syms = list(betas.keys())
        beta_vals = np.array([betas[symbol] for symbol in syms])
        if permute_beta:
            beta_vals = rng.permutation(beta_vals)
        order = np.argsort(beta_vals)
        q_size = len(syms) // N_QUINTILES
        low_syms = {syms[i] for i in order[:q_size]}
        high_syms = {syms[i] for i in order[-q_size:]}
        hold_dates = dates[est_idx : est_idx + REBAL_EVERY]
        if len(hold_dates) < 5:
            continue
        ls = ls_returns_over_hold(daily, hold_dates, high_syms, low_syms)
        ls.update({
            "rebal": r,
            "est_date": str(dates[est_idx]),
            "n_syms": len(syms),
            "q_size": q_size,
            "high_syms": sorted(high_syms),
            "low_syms": sorted(low_syms),
            "mean_high_beta": float(np.mean([betas[s] for s in high_syms])) if not permute_beta else None,
            "mean_low_beta": float(np.mean([betas[s] for s in low_syms])) if not permute_beta else None,
        })
        results.append(ls)
    return results, n_universe


def turnover(results: list[dict]) -> list[float]:
    tos: list[float] = []
    for i in range(1, len(results)):
        prev_h, cur_h = set(results[i - 1]["high_syms"]), set(results[i]["high_syms"])
        prev_l, cur_l = set(results[i - 1]["low_syms"]), set(results[i]["low_syms"])
        ch_h = len(cur_h - prev_h) / max(len(cur_h), 1)
        ch_l = len(cur_l - prev_l) / max(len(cur_l), 1)
        tos.append(0.5 * (ch_h + ch_l))
    return tos


def bootstrap_ci(values: np.ndarray, n_boot: int = 10000, seed: int = 11) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    if len(values) < 2:
        return (float(np.mean(values)) if len(values) else float("nan")), float("nan"), float("nan")
    means = np.array([rng.choice(values, size=len(values), replace=True).mean() for _ in range(n_boot)])
    return float(np.mean(values)), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def costs(results: list[dict], tos: list[float], spread_bps: float, auction_bps: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (on_gross, on_net_spread, on_net_spread_plus_auction) per rebalance for overnight."""
    on_gross = np.array([r["overnight"] for r in results])
    cost_spread = []
    cost_full = []
    for i in range(len(results)):
        frac = 1.0 if i == 0 else tos[i - 1]
        # round-trip both legs at per-side spread, charged on changed fraction
        rt_spread = 2.0 * (spread_bps / 1e4) * frac
        # auction slippage applies to ENTRY+EXIT of the overnight leg each holding period (both auctions),
        # on the WHOLE position (you pay the MOO/MOC auction every day you hold the overnight bet, but as a
        # per-rebalance avg-daily charge we apply it once per rebalance round-trip, on full notional).
        rt_auction = 2.0 * (auction_bps / 1e4)
        cost_spread.append(rt_spread)
        cost_full.append(rt_spread + rt_auction)
    on_net_spread = on_gross - np.array(cost_spread)
    on_net_full = on_gross - np.array(cost_full)
    return on_gross, on_net_spread, on_net_full


def subperiod_split(results: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, (lo, hi) in SUBPERIODS.items():
        sub = [r for r in results if lo <= r["est_date"] <= hi]
        if not sub:
            out[name] = {"n": 0}
            continue
        on = np.array([r["overnight"] for r in sub])
        intr = np.array([r["intraday"] for r in sub])
        out[name] = {
            "n": len(sub),
            "overnight_bps": float(np.mean(on) * 1e4),
            "intraday_bps": float(np.mean(intr) * 1e4),
            "split_ok": bool(np.mean(on) > np.mean(intr) and np.mean(on) > 0),
            "overnight_pos_frac": float(np.mean(on > 0)),
        }
    return out


def analyze(label: str, exclude_speculation: bool) -> dict:
    real, n_universe = run(exclude_speculation=exclude_speculation, permute_beta=False)
    canary, _ = run(exclude_speculation=exclude_speculation, permute_beta=True)
    tos = turnover(real)
    avg_turnover = float(np.mean(tos)) if tos else float("nan")

    on_gross, on_net_spread, on_net_full = costs(real, tos, SPREAD_BPS, AUCTION_SLIP_BPS)
    intr_gross = np.array([r["intraday"] for r in real])
    d24_gross = np.array([r["ret24"] for r in real])

    n = len(real)
    half = n // 2
    oos_idx = list(range(half, n))
    on_oos_net_full = on_net_full[oos_idx] if oos_idx else np.array([])

    canary_on = np.array([r["overnight"] for r in canary]) if canary else np.array([])

    return {
        "label": label,
        "exclude_speculation": exclude_speculation,
        "n_universe": n_universe,
        "n_rebalances": n,
        "q_size": real[0]["q_size"] if real else None,
        "avg_turnover": avg_turnover,
        "mean_overnight_gross_bps": float(np.mean(on_gross) * 1e4),
        "mean_intraday_gross_bps": float(np.mean(intr_gross) * 1e4),
        "mean_24h_gross_bps": float(np.mean(d24_gross) * 1e4),
        "split_predicted_direction": bool(np.mean(on_gross) > np.mean(intr_gross) and np.mean(on_gross) > 0),
        "overnight_pos_frac": float(np.mean(on_gross > 0)),
        "mean_overnight_net_spread_bps": float(np.mean(on_net_spread) * 1e4),
        "mean_overnight_net_full_bps": float(np.mean(on_net_full) * 1e4),
        "bootstrap": {
            "overnight_gross": bootstrap_ci(on_gross),
            "overnight_net_spread": bootstrap_ci(on_net_spread),
            "overnight_net_full_auction": bootstrap_ci(on_net_full),
            "overnight_oos_net_full": bootstrap_ci(on_oos_net_full) if len(on_oos_net_full) >= 2 else None,
            "intraday_gross": bootstrap_ci(intr_gross),
            "canary_overnight_gross": bootstrap_ci(canary_on) if len(canary_on) >= 2 else None,
        },
        "subperiods": subperiod_split(real),
        "per_rebal": [
            {
                "est_date": r["est_date"],
                "overnight_bps": round(r["overnight"] * 1e4, 2),
                "intraday_bps": round(r["intraday"] * 1e4, 2),
                "ret24_bps": round(r["ret24"] * 1e4, 2),
                "mean_high_beta": round(r["mean_high_beta"], 3) if r["mean_high_beta"] is not None else None,
                "mean_low_beta": round(r["mean_low_beta"], 3) if r["mean_low_beta"] is not None else None,
            }
            for r in real
        ],
    }


def main() -> None:
    full = analyze("FULL_UNIVERSE", exclude_speculation=False)
    no_spec = analyze("SPECULATION_EXCLUDED", exclude_speculation=True)

    out = {
        "config": {
            "n_liquid": N_LIQUID,
            "min_days": MIN_DAYS,
            "beta_window": BETA_WINDOW,
            "rebal_every": REBAL_EVERY,
            "n_quintiles": N_QUINTILES,
            "spread_bps_per_side": SPREAD_BPS,
            "auction_slip_bps_per_side": AUCTION_SLIP_BPS,
            "market": MARKET_SYM,
            "n_speculation_in_universe": None,
        },
        "full_universe": full,
        "speculation_excluded": no_spec,
    }
    with open(OUT_JSON, "w") as f:
        json.dump(out, f, indent=2, default=str)

    for blk in (full, no_spec):
        print(f"\n=== {blk['label']} ===")
        print(f"n_universe={blk['n_universe']}  n_rebalances={blk['n_rebalances']}  q_size={blk['q_size']}  avg_turnover={blk['avg_turnover']:.3f}")
        print(f"  OVERNIGHT gross = {blk['mean_overnight_gross_bps']:+.2f} bps/day  (pos {blk['overnight_pos_frac']*100:.0f}%)")
        print(f"  INTRADAY  gross = {blk['mean_intraday_gross_bps']:+.2f} bps/day")
        print(f"  24H       gross = {blk['mean_24h_gross_bps']:+.2f} bps/day")
        print(f"  SPLIT predicted (on>intr & on>0): {blk['split_predicted_direction']}")
        print(f"  OVERNIGHT net (spread) = {blk['mean_overnight_net_spread_bps']:+.2f}  net (spread+auction) = {blk['mean_overnight_net_full_bps']:+.2f}")
        b = blk["bootstrap"]
        for key in ("overnight_gross", "overnight_net_full_auction", "overnight_oos_net_full", "canary_overnight_gross"):
            v = b[key]
            if v is None:
                print(f"  boot {key}: n/a")
            else:
                print(f"  boot {key}: {v[0]*1e4:+.2f}  [{v[1]*1e4:+.2f}, {v[2]*1e4:+.2f}]")
        print("  sub-periods:")
        for name, sp in blk["subperiods"].items():
            if sp["n"] == 0:
                print(f"    {name}: no rebalances")
            else:
                print(f"    {name}: n={sp['n']}  on={sp['overnight_bps']:+.1f}  intr={sp['intraday_bps']:+.1f}  split_ok={sp['split_ok']}  on_pos={sp['overnight_pos_frac']*100:.0f}%")


if __name__ == "__main__":
    main()
