"""Implied-vol vs trailing-vol PROXY — the load-bearing assumption behind #331's vol-lane shelve.

#331's straddle G0 used premium = 0.8*trailing_rv*sqrt(H) (a TRAILING-VOL PROXY), never real option IV,
then concluded "vol is efficiently priced into the premium". This tests that assumption directly with the
REAL Alpaca option chain (current IV snapshot — no history, so this is a single-snapshot CROSS-SECTIONAL
G0 screen, NOT a forward backtest; see PRE_REGISTRATION.md §4 honesty caveat).

Per liquid underlying (ONE row):
  - atm_iv      = ATM implied vol from the live option chain (strike-interpolated at spot, call/put avg),
                  nearest expiry in [min_dte, max_dte] days, valid latest_quote required.
  - trail_rv_ann= trailing realized vol from recent contiguous 1-min RTH bars, ANNUALIZED (proxy basis).
  - forecast    = the #331 incremental-over-persistence winner: longer-window trailing vol (realized_vol_60m
                  analogue) annualized — the term that beat the 30-min baseline incrementally.

T1: does trail_rv_ann alone explain atm_iv (rank-IC, OLS R2, residual)?
T2 (DECISION): does forecast predict iv_resid = atm_iv - fitted(trail)?  rank-IC + shuffle null + the
    incremental form (residualize BOTH forecast and iv_resid on trail again).
T3: robustness — 2nd expiry bucket sign-stability + call-only/put-only ATM IV.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re

import numpy as np
import polars as pl
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest

STORE = os.environ.get("STORE_ROOT", "/store")
MINUTES_PER_TRADING_YEAR = 252 * 390  # RTH 1-min bars per trading year (annualize 1-min vol)
ANN = float(np.sqrt(MINUTES_PER_TRADING_YEAR))

# OCC symbol: ROOT + YYMMDD + C/P + 8-digit strike(*1000). e.g. SPY260624C00773000
_OCC = re.compile(r"^(?P<root>[A-Z]+)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def _all_store_dates() -> list[str]:
    dates = {os.path.basename(p).replace("date=", "")
             for p in glob.glob(f"{STORE}/raw/bars/symbol=*/date=*")}
    return sorted(dates)


def _adv_universe(dates: list[str], top_n: int) -> list[str]:
    """Top-N symbols by mean RTH dollar-volume over the given dates (from raw bars)."""
    frames = []
    for date_iso in dates:
        pattern = f"{STORE}/raw/bars/symbol=*/date={date_iso}/*.parquet"
        if not glob.glob(pattern):
            continue
        frame = (
            pl.scan_parquet(pattern, hive_partitioning=True)
            .select(["symbol", "close", "volume"])
            .with_columns((pl.col("close") * pl.col("volume")).alias("dv"))
            .group_by("symbol")
            .agg(pl.col("dv").sum().alias("dv"))
            .collect()
        )
        frames.append(frame)
    full = pl.concat(frames).group_by("symbol").agg(pl.col("dv").mean().alias("adv"))
    return full.sort("adv", descending=True).head(top_n)["symbol"].to_list()


def _trailing_vols(symbol: str, latest_date: str, windows: list[int]) -> dict[str, float] | None:
    """Annualized trailing realized vol over the last `w` contiguous 1-min RTH bars, per window."""
    files = sorted(glob.glob(f"{STORE}/raw/bars/symbol={symbol}/date={latest_date}/*.parquet"))
    if not files:
        return None
    bars = pl.concat([pl.read_parquet(p, columns=["ts", "close"]) for p in files]).sort("ts")
    # RTH only (13:30..20:00 UTC = 09:30..16:00 ET) so the annualization minute-count is honest.
    # cast hour/minute to Int32 BEFORE arithmetic — dt.hour() is Int8 and 13*60 overflows (the ET-cast gotcha).
    mod = pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)
    bars = bars.filter((mod >= 13 * 60 + 30) & (mod <= 20 * 60))
    close = bars["close"].to_numpy().astype(float)
    if close.size < max(windows) + 2:
        return None
    logret = np.diff(np.log(close))
    out: dict[str, float] = {"spot": float(close[-1])}
    for w in windows:
        tail = logret[-w:]
        if tail.size < w or not np.all(np.isfinite(tail)):
            return None
        out[f"rv_{w}"] = float(np.std(tail, ddof=1) * ANN)
    return out


def _atm_iv(client: OptionHistoricalDataClient, symbol: str, spot: float,
            today: dt.date, min_dte: int, max_dte: int) -> dict[str, float] | None:
    """Strike-interpolated ATM IV (call/put avg) at the nearest expiry within [min_dte,max_dte] days.

    Returns atm_iv (avg), atm_iv_call, atm_iv_put, dte for the chosen expiry; None if no clean bracket.
    """
    chain = client.get_option_chain(OptionChainRequest(underlying_symbol=symbol))
    rows = []
    for occ, snap in chain.items():
        iv = getattr(snap, "implied_volatility", None)
        if iv is None or not np.isfinite(iv) or iv <= 0:
            continue
        if getattr(snap, "latest_quote", None) is None:  # require a live quote, not stale-only
            continue
        match = _OCC.match(occ)
        if not match:
            continue
        exp = dt.datetime.strptime(match["exp"], "%y%m%d").date()
        dte = (exp - today).days
        if dte < min_dte or dte > max_dte:
            continue
        strike = int(match["strike"]) / 1000.0
        rows.append((exp, dte, match["cp"], strike, float(iv)))
    if not rows:
        return None
    # nearest expiry to the (min+max)/2 target within band
    target_dte = (min_dte + max_dte) / 2.0
    expiries = sorted({(abs(d - target_dte), e, d) for (e, d, _, _, _) in rows})
    _, chosen_exp, chosen_dte = expiries[0]
    legs = [r for r in rows if r[0] == chosen_exp]
    out: dict[str, float] = {"dte": float(chosen_dte)}
    for cp, label in (("C", "call"), ("P", "put")):
        side = sorted([(s, iv) for (_, _, c, s, iv) in legs if c == cp])
        if len(side) < 2:
            continue
        strikes = np.array([s for s, _ in side])
        ivs = np.array([iv for _, iv in side])
        if spot < strikes.min() or spot > strikes.max():
            # spot outside listed strikes — use nearest strike's IV (flagged by wide |moneyness|)
            j = int(np.argmin(np.abs(strikes - spot)))
            out[f"atm_iv_{label}"] = float(ivs[j])
            out[f"moneyness_gap_{label}"] = float(abs(strikes[j] - spot) / spot)
        else:
            out[f"atm_iv_{label}"] = float(np.interp(spot, strikes, ivs))
            out[f"moneyness_gap_{label}"] = 0.0
    have = [out[f"atm_iv_{s}"] for s in ("call", "put") if f"atm_iv_{s}" in out]
    if not have:
        return None
    out["atm_iv"] = float(np.mean(have))
    return out


def _rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=float)
    ranks[order] = np.arange(values.size, dtype=float)
    return ranks / max(values.size - 1, 1)


def _rank_ic(signal: np.ndarray, label: np.ndarray) -> float:
    keep = np.isfinite(signal) & np.isfinite(label)
    if keep.sum() < 10:
        return float("nan")
    rs, rl = _rank(signal[keep]), _rank(label[keep])
    if np.std(rs) < 1e-12 or np.std(rl) < 1e-12:
        return float("nan")
    return float(np.corrcoef(rs, rl)[0, 1])


def _ols_residual(label: np.ndarray, control: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Residual of label on control + R2 + slope (raw-space OLS)."""
    resid = np.full_like(label, np.nan, dtype=float)
    keep = np.isfinite(label) & np.isfinite(control)
    if keep.sum() < 10:
        return resid, float("nan"), float("nan")
    x = control[keep]
    y = label[keep]
    design = np.column_stack([np.ones(x.size), x])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    fitted = design @ beta
    resid[keep] = y - fitted
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-18 else float("nan")
    return resid, r2, float(beta[1])


def _rank_residualize(values: np.ndarray, control: np.ndarray) -> np.ndarray:
    out = np.full_like(values, np.nan, dtype=float)
    keep = np.isfinite(values) & np.isfinite(control)
    if keep.sum() < 10:
        return out
    y, x = _rank(values[keep]), _rank(control[keep])
    if np.std(x) < 1e-12:
        out[keep] = y - np.mean(y)
        return out
    design = np.column_stack([np.ones(x.size), x])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    out[keep] = y - design @ beta
    return out


def _bootstrap_ic_ci(signal: np.ndarray, label: np.ndarray, n_boot: int, seed: int) -> tuple[float, float]:
    keep = np.isfinite(signal) & np.isfinite(label)
    sig, lab = signal[keep], label[keep]
    rng = np.random.default_rng(seed)
    ics = []
    for _ in range(n_boot):
        idx = rng.integers(0, sig.size, sig.size)
        ics.append(_rank_ic(sig[idx], lab[idx]))
    ics = np.array([v for v in ics if np.isfinite(v)])
    if ics.size == 0:
        return float("nan"), float("nan")
    return float(np.percentile(ics, 2.5)), float(np.percentile(ics, 97.5))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-n", type=int, default=150)
    parser.add_argument("--adv-dates", type=int, default=10, help="trailing store dates for ADV ranking")
    parser.add_argument("--min-dte", type=int, default=5)
    parser.add_argument("--max-dte", type=int, default=45)
    parser.add_argument("--min-dte2", type=int, default=20, help="2nd (longer) expiry bucket for T3")
    parser.add_argument("--max-dte2", type=int, default=75)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    key = os.environ["ALPACA_KEY_ID"]
    sec = os.environ["ALPACA_SECRET_KEY"]
    client = OptionHistoricalDataClient(api_key=key, secret_key=sec)

    store_dates = _all_store_dates()
    latest_date = store_dates[-1]
    adv_dates = store_dates[-args.adv_dates:]
    today = dt.date.today()
    universe = _adv_universe(adv_dates, args.top_n)
    print(f"# universe top-{args.top_n} by ADV over {adv_dates[0]}..{adv_dates[-1]}; "
          f"bars as-of {latest_date}; IV snapshot {today}", flush=True)

    records = []
    for symbol in universe:
        vols = _trailing_vols(symbol, latest_date, windows=[30, 60])
        if vols is None:
            continue
        iv = _atm_iv(client, symbol, vols["spot"], today, args.min_dte, args.max_dte)
        if iv is None or "atm_iv" not in iv:
            continue
        iv2 = _atm_iv(client, symbol, vols["spot"], today, args.min_dte2, args.max_dte2)
        rec = {
            "symbol": symbol,
            "spot": vols["spot"],
            "trail_rv_ann": vols["rv_30"],        # proxy basis (#331's 30-min trailing vol, annualized)
            "forecast": vols["rv_60"],            # the #331 incremental winner (60-min term-structure)
            "atm_iv": iv["atm_iv"],
            "atm_iv_call": iv.get("atm_iv_call", float("nan")),
            "atm_iv_put": iv.get("atm_iv_put", float("nan")),
            "dte": iv["dte"],
            "moneyness_gap": max(iv.get("moneyness_gap_call", 0.0), iv.get("moneyness_gap_put", 0.0)),
            "atm_iv_2": (iv2.get("atm_iv", float("nan")) if iv2 else float("nan")),
        }
        records.append(rec)
    panel = pl.DataFrame(records)
    n = panel.height
    print(f"# panel: {n} names with both clean bars + ATM IV", flush=True)
    if n < 30:
        print("# TOO FEW NAMES — no-decision", flush=True)
        if args.out:
            json.dump({"n": n, "note": "too_few"}, open(args.out, "w"), indent=2)
        return

    # exclude wide-moneyness-gap names from the primary (spot outside listed strikes => noisy ATM IV)
    clean = panel.filter(pl.col("moneyness_gap") < 0.05)
    print(f"# clean (moneyness_gap<5%): {clean.height} names", flush=True)

    atm_iv = clean["atm_iv"].to_numpy().astype(float)
    trail = clean["trail_rv_ann"].to_numpy().astype(float)
    forecast = clean["forecast"].to_numpy().astype(float)

    # T1 — does trailing vol alone explain IV?
    t1_ic = _rank_ic(trail, atm_iv)
    iv_resid, r2, slope = _ols_residual(atm_iv, trail)
    print(f"\n## T1 — proxy adequacy: rank-IC(trail, atm_iv)={t1_ic:+.4f}  "
          f"OLS R2={r2:+.4f} slope={slope:+.3f}", flush=True)
    print(f"#   trail_rv_ann  median={np.nanmedian(trail):.3f} IQR[{np.nanpercentile(trail,25):.3f},"
          f"{np.nanpercentile(trail,75):.3f}]", flush=True)
    print(f"#   atm_iv        median={np.nanmedian(atm_iv):.3f} IQR[{np.nanpercentile(atm_iv,25):.3f},"
          f"{np.nanpercentile(atm_iv,75):.3f}]  (IV/trail median ratio "
          f"{np.nanmedian(atm_iv)/np.nanmedian(trail):.2f})", flush=True)

    # T2 — DECISION: does forecast predict the IV residual?
    t2_raw = _rank_ic(forecast, iv_resid)
    rng = np.random.default_rng(13)
    perm = rng.permutation(forecast.size)
    t2_shuf = _rank_ic(forecast[perm], iv_resid)
    f_resid = _rank_residualize(forecast, trail)
    ivr_resid = _rank_residualize(iv_resid, trail)
    t2_incr = _rank_ic(f_resid, ivr_resid)
    incr_shuf = _rank_ic(f_resid[rng.permutation(f_resid.size)], ivr_resid)
    ci_lo, ci_hi = _bootstrap_ic_ci(f_resid, ivr_resid, n_boot=2000, seed=7)
    print(f"\n## T2 — DECISION: forecast vs iv_resid", flush=True)
    print(f"#   raw rank-IC(forecast, iv_resid)      ={t2_raw:+.4f}  shuffle={t2_shuf:+.4f}  "
          f"edge={t2_raw - t2_shuf:+.4f}", flush=True)
    print(f"#   INCREMENTAL rank-IC (both resid on trail)={t2_incr:+.4f}  shuffle={incr_shuf:+.4f}  "
          f"boot95%CI[{ci_lo:+.4f},{ci_hi:+.4f}]", flush=True)

    # T3 — robustness: 2nd expiry bucket + wings
    iv2 = clean["atm_iv_2"].to_numpy().astype(float)
    iv2_resid, _, _ = _ols_residual(iv2, trail)
    t3_expiry = _rank_ic(_rank_residualize(forecast, trail), _rank_residualize(iv2_resid, trail))
    call_resid, _, _ = _ols_residual(clean["atm_iv_call"].to_numpy().astype(float), trail)
    put_resid, _, _ = _ols_residual(clean["atm_iv_put"].to_numpy().astype(float), trail)
    t3_call = _rank_ic(_rank_residualize(forecast, trail), _rank_residualize(call_resid, trail))
    t3_put = _rank_ic(_rank_residualize(forecast, trail), _rank_residualize(put_resid, trail))
    print(f"\n## T3 — robustness (incremental rank-IC of forecast vs each IV residual)", flush=True)
    print(f"#   2nd expiry bucket (dte~{(args.min_dte2+args.max_dte2)//2}d): {t3_expiry:+.4f}", flush=True)
    print(f"#   call-only ATM IV: {t3_call:+.4f}   put-only ATM IV: {t3_put:+.4f}", flush=True)

    # decision gate
    signs = [np.sign(x) for x in (t2_incr, t3_expiry, t3_call, t3_put) if np.isfinite(x) and abs(x) > 1e-6]
    sign_consistent = len(set(signs)) == 1 if signs else False
    h1 = (np.isfinite(t2_incr) and abs(t2_incr) >= 0.10
          and abs(incr_shuf) < 0.03 and abs(t2_incr - incr_shuf) >= 0.07
          and sign_consistent and clean.height >= 60)
    verdict = "H1 (re-open lane — motivate option-IV backfill)" if h1 else "H0 (shelve — proxy adequate)"
    print(f"\n## VERDICT: {verdict}", flush=True)
    print(f"#   gate: |incr_ic|>=0.10 ({abs(t2_incr):.3f}), shuffle-clean "
          f"({abs(incr_shuf):.3f}<0.03 & edge {abs(t2_incr-incr_shuf):.3f}>=0.07), "
          f"sign-consistent ({sign_consistent}), N>=60 ({clean.height})", flush=True)

    report = {
        "as_of_bars": latest_date, "iv_snapshot": str(today),
        "n_names": n, "n_clean": clean.height, "adv_window": [adv_dates[0], adv_dates[-1]],
        "T1": {"rank_ic_trail_iv": t1_ic, "ols_r2": r2, "ols_slope": slope,
               "iv_median": float(np.nanmedian(atm_iv)), "trail_median": float(np.nanmedian(trail))},
        "T2": {"raw_ic": t2_raw, "raw_shuffle": t2_shuf, "incr_ic": t2_incr,
               "incr_shuffle": incr_shuf, "boot95": [ci_lo, ci_hi]},
        "T3": {"expiry2_incr_ic": t3_expiry, "call_incr_ic": t3_call, "put_incr_ic": t3_put},
        "verdict": verdict, "h1": bool(h1),
    }
    if args.out:
        json.dump(report, open(args.out, "w"), indent=2, default=str)
        panel.write_csv(args.out.replace(".json", "_panel.csv"))


if __name__ == "__main__":
    main()
