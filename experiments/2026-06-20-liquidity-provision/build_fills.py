"""LIQUIDITY-PROVISION fill simulation — REAL trade-print join (registered #218 pre-reg, locked).

Per (name, day) replays the consolidated SIP NBBO quote stream JOINED to the real trade tape, simulating
continuous two-sided passive quoting under the FROZEN fill rule:
  - Post a passive BUY at the prevailing best bid B (SELL at best ask A mirrors), sampled at a fixed cadence
    (a new resting order each POST_SECS while flat on that side — "continuous" quoting, discretized).
  - Q0 = aggregate displayed bid size at B when we post (back of the FIFO queue).
  - FILL only on REAL TRADE PRINTS at price <= B while the NBBO bid is still >= B: traded_through = cumulative
    print size; we fill our OUR_SIZE once traded_through crosses Q0 (back-of-queue). Fill ts = that print's ts.
  - No fill within R=30min → cancel (record idle).
  - Per fill: half-spread earned (mid-B)/mid; markout over H in {1,5,15} min from the REAL future mid; exit =
    taking half-spread at H. Net per-fill P&L = half_spread + markout - exit_cost (markout is signed; for a
    BUY a falling mid = adverse = negative).
Emits a per-fill ledger parquet. Cancels/trades are NEVER conflated — a cancel shrinks bidsz but prints
nothing, so it can't trigger a fill.

Host-mounted resumable cache (fills/<sym>_<date>.parquet) + chunked-subprocess infra (#205/#212). READ-ONLY
stores. Writes the ledger; screen.py does the median-anchored stats.
"""

from __future__ import annotations

import glob
import os
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

ET = ZoneInfo("America/New_York")
STORE = os.environ.get("STORE_ROOT", "/store")
OUT_DIR = "/app/experiments/2026-06-20-liquidity-provision"
RTH_START = 9 * 60 + 35  # 09:35 ET
RTH_END = 16 * 60  # 16:00 ET
POST_SECS = int(os.environ.get("POST_SECS", "30"))  # re-post cadence while flat on a side ("continuous")
REST_MIN = 30  # max resting horizon → cancel
MARKOUT_MIN = (1, 5, 15)  # adverse-selection markout horizons
OUR_NOTIONAL = float(os.environ.get("OUR_NOTIONAL", "10000"))  # $10k/quote (capacity unit)
MIN_PRICE = 1.0
MAX_SYM_PER_RUN = int(os.environ.get("MAX_SYM_PER_RUN", "0"))  # chunk bound (fresh-process memory cap)

LIQUID_CORE = os.environ.get("LIQUID_CORE", "SPY,QQQ,AAPL,MSFT,NVDA,PLTR,AMD,TSLA,AMZN,META").split(",")


def rth_mask(ts_col: pl.Expr) -> pl.Expr:
    et = ts_col.dt.convert_time_zone("America/New_York")
    m = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    return (m >= RTH_START) & (m < RTH_END)


def load_quotes(sym: str, day: str) -> pl.DataFrame:
    f = glob.glob(f"{STORE}/raw/quotes/symbol={sym}/date={day}/*.parquet")
    if not f:
        return pl.DataFrame()
    q = pl.read_parquet(f[0]).select(["ts", "bid_price", "bid_size", "ask_price", "ask_size"])
    q = q.filter(
        rth_mask(pl.col("ts")) & (pl.col("bid_price") > 0) & (pl.col("ask_price") >= pl.col("bid_price"))
    )
    return q.sort("ts")


def load_trades(sym: str, day: str) -> pl.DataFrame:
    f = glob.glob(f"{STORE}/raw/trades/symbol={sym}/date={day}/*.parquet")
    if not f:
        return pl.DataFrame()
    t = pl.read_parquet(f[0]).select(["ts", "price", "size"])
    return t.filter(rth_mask(pl.col("ts")) & (pl.col("price") > 0)).sort("ts")


def sim_name_day(sym: str, day: str) -> pl.DataFrame:
    """Simulate two-sided passive quoting for one (sym, day) → per-fill ledger rows."""
    q = load_quotes(sym, day)
    t = load_trades(sym, day)
    if q.height < 100 or t.height < 100:
        return pl.DataFrame()
    qts = q["ts"].to_numpy()
    bid = q["bid_price"].to_numpy()
    bidsz = q["bid_size"].to_numpy()
    ask = q["ask_price"].to_numpy()
    asksz = q["ask_size"].to_numpy()
    mid = (bid + ask) / 2.0
    tts = t["ts"].to_numpy()
    tpx = t["price"].to_numpy()
    tsz = t["size"].to_numpy()
    day_total_vol = float(tsz.sum())

    rows = []
    # Post on a fixed cadence across RTH; at each post time use the prevailing quote (searchsorted).
    start, end = qts[0], qts[-1]
    post_step = np.timedelta64(POST_SECS, "s")
    rest = np.timedelta64(REST_MIN, "m")
    post_times = np.arange(start, end - rest, post_step)
    for side in ("buy", "sell"):
        for pt in post_times:
            qi = np.searchsorted(qts, pt, side="right") - 1
            if qi < 0:
                continue
            if side == "buy":
                price, q0, m0 = bid[qi], bidsz[qi], mid[qi]
            else:
                price, q0, m0 = ask[qi], asksz[qi], mid[qi]
            if price < MIN_PRICE or q0 <= 0 or m0 <= 0:
                continue
            our_shares = OUR_NOTIONAL / price
            # window for the fill search: [pt, pt+REST]
            lo = np.searchsorted(tts, pt, side="left")
            hi = np.searchsorted(tts, pt + rest, side="right")
            if hi <= lo:
                continue
            # trade prints that hit our resting order: buy fills on prints <= our bid; sell on prints >= our ask
            if side == "buy":
                hit = tpx[lo:hi] <= price
            else:
                hit = tpx[lo:hi] >= price
            if not hit.any():
                continue
            csum = np.cumsum(np.where(hit, tsz[lo:hi], 0.0))
            # back-of-queue: we fill once cumulative traded-through exceeds Q0 (the displayed size ahead)
            fill_pos = np.searchsorted(csum, q0, side="right")
            if fill_pos >= (hi - lo):
                continue  # never filled past the queue within the rest horizon
            fill_ts = tts[lo + fill_pos]
            # markout from the REAL future mid at each horizon
            half_spread = (m0 - price) / m0 if side == "buy" else (price - m0) / m0
            # store fill_ts as epoch-microseconds int (avoids polars Object dtype on a numpy datetime64 scalar)
            fill_ts_us = int(fill_ts.astype("datetime64[us]").astype("int64"))
            rec = {
                "symbol": sym,
                "date": day,
                "side": side,
                "fill_ts_us": fill_ts_us,
                "price": float(price),
                "mid0": float(m0),
                "q0": float(q0),
                "our_shares": float(our_shares),
                "day_total_vol": day_total_vol,
                "half_spread": float(half_spread),
            }
            for H in MARKOUT_MIN:
                fi = np.searchsorted(qts, fill_ts + np.timedelta64(H, "m"), side="right") - 1
                if fi < 0 or fi >= len(mid):
                    rec[f"markout_{H}"] = None
                    rec[f"exit_cost_{H}"] = None
                    continue
                mH = mid[fi]
                # BUY profits if mid rises; SELL profits if mid falls
                mk = (mH - m0) / m0 if side == "buy" else (m0 - mH) / m0
                exit_half = (ask[fi] - mH) / mH if side == "buy" else (mH - bid[fi]) / mH
                rec[f"markout_{H}"] = float(mk)
                rec[f"exit_cost_{H}"] = float(exit_half)
            rows.append(rec)
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def list_days() -> list[str]:
    days = sorted(
        p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/quotes/symbol=SPY/date=*")
    )
    return days


def main() -> None:
    universe = LIQUID_CORE if os.environ.get("UNIVERSE", "core") == "core" else _liquid_universe()
    days = list_days()
    fills_dir = f"{OUT_DIR}/fills"
    os.makedirs(fills_dir, exist_ok=True)
    done = set(os.listdir(fills_dir))
    pending = [(s, d) for s in universe for d in days if f"{s}_{d}.parquet" not in done]
    if MAX_SYM_PER_RUN > 0:
        pending = pending[: MAX_SYM_PER_RUN * len(days)]
    print(f"universe={len(universe)} days={len(days)} pending (sym,day)={len(pending)}", flush=True)
    empty_marker = pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Utf8})
    for i, (sym, day) in enumerate(pending):
        led = sim_name_day(sym, day)
        # always write a marker (even empty, typed) so resume skips it
        (led if led.height else empty_marker).write_parquet(f"{fills_dir}/{sym}_{day}.parquet")
        if (i + 1) % 50 == 0 or i == len(pending) - 1:
            print(f"  simmed {i+1}/{len(pending)} (sym,day)", flush=True)
    print(f"LEDGER_STATUS files={len(os.listdir(fills_dir))}", flush=True)


def _liquid_universe() -> list[str]:
    """Names with quotes AND trades on a recent reference day, capped — the full liquid universe."""
    ref = list_days()[-30]
    qs = {p.split("symbol=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/quotes/symbol=*")}
    ts = {p.split("symbol=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/trades/symbol=*")}
    both = sorted(qs & ts)
    # rank by quote-file presence on the ref day as a cheap liquidity proxy; keep top-N
    have = [s for s in both if glob.glob(f"{STORE}/raw/quotes/symbol={s}/date={ref}/*.parquet")]
    return have[: int(os.environ.get("N_SYMBOLS", "150"))]


if __name__ == "__main__":
    main()
