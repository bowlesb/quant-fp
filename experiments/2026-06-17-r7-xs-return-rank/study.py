"""R7 cross-sectional multi-day return-rank study. Pre-reg in hypothesis.md.

Builds a daily panel (per symbol-day: open, close) from /store bars, computes daily_return_w and its
cross-sectional rank xs_rank_w within each day's liquid universe, then measures (1) day-to-day rank
persistence and (2) the forward next-day-return rank-IC (tradeable d+1 open->close entry). Characterizes
the ST-reversal / XS-momentum factor input.
"""

import glob
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import polars as pl
from scipy.stats import spearmanr

BARS = "/store/raw/bars"
OUT = "/app/experiments/2026-06-17-r7-xs-return-rank"
W_DAYS = (1, 5, 20)
RTH_LO, RTH_HI = 570, 960
N_LIQUID = 1500


def daily_bars(symbol: str) -> dict[str, object] | None:
    """Per-day RTH open (first RTH bar open) + close (last RTH bar close) + dollar volume."""
    files = sorted(glob.glob(f"{BARS}/symbol={symbol}/date=*/data.parquet"))
    if len(files) < 40:
        return None
    rows = []
    dollar_total = 0.0
    for path in files:
        date = os.path.basename(os.path.dirname(path)).split("=")[1]
        try:
            df = pl.read_parquet(path, columns=["ts", "open", "close", "volume"])
        except (OSError, pl.exceptions.PolarsError):
            continue
        if df.height == 0:
            continue
        et = df["ts"].dt.convert_time_zone("America/New_York")
        etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
        rth = df.filter((etm >= RTH_LO) & (etm < RTH_HI)).sort("ts")
        if rth.height == 0:
            continue
        rows.append((date, float(rth["open"][0]), float(rth["close"][-1])))
        dollar_total += float((rth["close"] * rth["volume"]).sum())
    if len(rows) < 40:
        return None
    return {"symbol": symbol, "adv": dollar_total / len(rows), "rows": rows}


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    syms = [d.split("=")[1] for d in os.listdir(BARS) if d.startswith("symbol=")]
    print(f"building daily panel for {len(syms)} symbols (parallel)...", flush=True)
    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=8) as ex:
        for res in ex.map(daily_bars, syms, chunksize=20):
            if res is not None:
                results.append(res)
            done += 1
            if done % 1500 == 0:
                print(f"  {done}/{len(syms)} (kept {len(results)})", flush=True)

    results.sort(key=lambda r: -float(r["adv"]))
    liquid = results[:N_LIQUID]
    print(f"\nliquid panel: {len(liquid)} symbols", flush=True)

    rows = []
    for res in liquid:
        for date, open_px, close_px in res["rows"]:
            rows.append({"symbol": res["symbol"], "date": date, "open": open_px, "close": close_px})
    panel = pl.DataFrame(rows).sort(["symbol", "date"])

    # daily_return_w + next-day tradeable return (d+1 open->close)
    exprs = []
    for w in W_DAYS:
        exprs.append((pl.col("close") / pl.col("close").shift(w).over("symbol") - 1.0).alias(f"ret{w}"))
    panel = panel.with_columns(exprs)
    panel = panel.with_columns(
        (pl.col("close").shift(-1).over("symbol") / pl.col("open").shift(-1).over("symbol") - 1.0).alias("fwd1d")
    )
    # cross-sectional rank per date (universe = the day's liquid panel present)
    rank_exprs = [
        (pl.col(f"ret{w}").rank() / pl.col(f"ret{w}").count()).over("date").alias(f"xsrank{w}") for w in W_DAYS
    ]
    panel = panel.with_columns(rank_exprs)

    print("\n=== XS return-rank characterization (liquid) ===")
    # (2) day-to-day rank persistence: corr(xsrank_w[d], xsrank_w[d+1]) per symbol, pooled
    for w in W_DAYS:
        nxt = pl.col(f"xsrank{w}").shift(-1).over("symbol")
        sub = panel.select([pl.col(f"xsrank{w}").alias("a"), nxt.alias("b")]).drop_nulls()
        ac = float(spearmanr(sub["a"].to_numpy(), sub["b"].to_numpy()).correlation) if sub.height > 1000 else float("nan")
        # (3) forward IC: xsrank_w[d] vs fwd1d[d]
        fic = panel.select([pl.col(f"xsrank{w}").alias("r"), pl.col("fwd1d").alias("f")]).drop_nulls()
        ic = float(spearmanr(fic["r"].to_numpy(), fic["f"].to_numpy()).correlation) if fic.height > 1000 else float("nan")
        # decile spread: top-rank minus bottom-rank next-day return
        d = fic.sort("r")
        n = d.height
        k = n // 10
        spread = float(d["f"][-k:].mean() - d["f"][:k].mean()) if k > 0 else float("nan")
        print(f"  w={w:>2}d: day-to-day rank-autocorr {ac:+.3f} | fwd_1d rank-IC {ic:+.4f} | "
              f"top-bottom-decile next-day {spread * 100:+.3f}% (n={fic.height:,})")

    print("\nREAD: persistence (slow factor) + a coherent forward-IC sign (reversal short-w / momentum "
          "long-w) => SHIP xs_return_rank. Verify the signs myself before deciding.")


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
