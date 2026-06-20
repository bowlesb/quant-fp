"""Reusable LP (liquidity-provision) cost/fill PRIMITIVES from the raw NBBO quote tape.

This is SHARED RESEARCH INFRASTRUCTURE, not an edge test. It extracts — from the now-queryable
379d quote tape (/store/raw/quotes, real bid/ask price + size) — the honest microstructure
primitives a liquidity-PROVISION surface needs but that the #205 quote-spread re-test did NOT build
(retest.py only computes the liquidity-TAKING effective half-spread). The deferred-then-active LP
surface, and every future quote-dependent experiment, consumes these instead of re-deriving fragile
spread/fill math inline.

The three primitives, all pre-registered (see HYPOTHESIS.md) BEFORE any LP return is computed:

  1. quoted_half_spread        — (ask-bid)/2/mid, the spread a passive resting order EARNS at the touch.
  2. top_of_book_depth         — bid_size/ask_size at the touch (in ROUND LOTS x100 sh; documented), the
                                 capacity / fill-rationing primitive.
  3. passive_fill_then_adverse — the HONEST FILL MODEL: post a passive order AT THE TOUCH, measure
                                 P(fill within a window) AND the post-fill signed mid move (realized
                                 adverse selection). Net per-fill = earned-half-spread + adverse-move.
                                 This is the make-or-break number: can you actually EARN the spread net
                                 of adverse selection, or does the mid run you over?

CONVENTIONS (documented so consumers don't re-guess):
- Sizes are ROUND LOTS (x100 shares). Verified: AAPL top-of-book median size 2.0 => ~$39k notional at
  ~$196 mid (2 lots x 100 x $196). Raw-share interpretation would give ~$393 (absurd). Module exposes
  depth in lots; multiply by 100 for shares.
- RTH only, tradeable entry >= 09:35 ET (matches the trusted-substrate harness discipline; never the
  09:30 print).
- NBBO is sub-ms dense; we sample onto a 1-second grid (last quote in each second) to make a forward-mid
  look-ahead tractable and robust to firehose jitter. Grid step + horizons are parameters.
- Fill proxy WITHOUT trades: a resting passive BID at price b is "filled" over [t, t+window] the first
  second the grid mid falls to <= b (price came down to the posted bid); symmetric for the ask. This is
  a deliberately OPTIMISTIC fill rule (queue position ignored => upper bound on fill rate); the
  consumer must treat fill_rate as a CEILING. Flagged, not hidden.
- Adverse selection is measured CONDITIONAL ON FILL and SIGNED from the provider's post-fill inventory
  (a filled bid leaves the provider LONG at b; a subsequent mid fall is a LOSS). Unconditional |move|
  would overstate the earnable edge by ignoring that you only fill on the adverse side.

This module computes per-(symbol, day) primitive rows. It does NOT form portfolios, does NOT compute
forward cross-sectional returns, and does NOT make any edge claim. Reads READ-ONLY.
"""

from __future__ import annotations

import argparse
import glob
import os

import numpy as np
import polars as pl

STORE = os.environ.get("STORE_ROOT", "/store")
LOT_SHARES = 100  # NBBO sizes are round lots
RTH_OPEN_MIN = 9 * 60 + 35  # tradeable entry >= 09:35 ET
RTH_CLOSE_MIN = 16 * 60
MAX_HALF_SPREAD = 0.05  # drop crossed/garbage prints with >5% half-spread


def _load_rth_nbbo(symbol: str, day: str) -> pl.DataFrame | None:
    """RTH NBBO for one (symbol, day): ts, bid/ask price+size, mid. None if unusable."""
    pattern = f"{STORE}/raw/quotes/symbol={symbol}/date={day}/*.parquet"
    files = glob.glob(pattern)
    if not files:
        return None
    df = (
        pl.scan_parquet(files)
        .select(["ts", "bid_price", "bid_size", "ask_price", "ask_size"])
        .collect()
        .sort("ts")
    )
    if df.height == 0:
        return None
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    df = df.with_columns(etm.alias("_m")).filter(
        (pl.col("_m") >= RTH_OPEN_MIN) & (pl.col("_m") < RTH_CLOSE_MIN)
    )
    df = df.filter((pl.col("bid_price") > 0) & (pl.col("ask_price") >= pl.col("bid_price")))
    if df.height < 100:
        return None
    df = df.with_columns(((pl.col("ask_price") + pl.col("bid_price")) / 2.0).alias("mid"))
    half_sp = (pl.col("ask_price") - pl.col("bid_price")) / 2.0 / pl.col("mid")
    df = df.filter((half_sp >= 0) & (half_sp < MAX_HALF_SPREAD))
    if df.height < 100:
        return None
    return df


def _second_grid(df: pl.DataFrame) -> pl.DataFrame:
    """Collapse the dense NBBO to a 1-second grid (last quote in each second)."""
    return (
        df.with_columns(pl.col("ts").dt.truncate("1s").alias("sec"))
        .group_by("sec")
        .agg(
            pl.col("mid").last(),
            pl.col("bid_price").last(),
            pl.col("ask_price").last(),
            pl.col("bid_size").last(),
            pl.col("ask_size").last(),
        )
        .sort("sec")
    )


def compute_day_primitives(
    symbol: str, day: str, fill_window_s: int = 60, hold_s: int = 60, post_step_s: int = 5
) -> dict[str, float] | None:
    """All LP primitives for one (symbol, day). Returns a flat dict, or None if no usable quotes.

    fill_window_s: seconds a passive post rests waiting for a fill.
    hold_s:        seconds the filled inventory is held before marking the adverse move.
    post_step_s:   subsample stride for posting passive orders along the day (cost control; each post is
                   an independent draw of the conditional fill experiment).
    """
    df = _load_rth_nbbo(symbol, day)
    if df is None:
        return None
    grid = _second_grid(df)
    n = grid.height
    if n < fill_window_s + hold_s + 10:
        return None
    mid = grid["mid"].to_numpy()
    bid = grid["bid_price"].to_numpy()
    ask = grid["ask_price"].to_numpy()
    bid_sz = grid["bid_size"].to_numpy()
    ask_sz = grid["ask_size"].to_numpy()

    quoted_half = ((ask - bid) / 2.0 / mid).astype(float)
    quoted_half_bps = float(np.median(quoted_half) * 1e4)

    bid_depth_lots = float(np.median(bid_sz))
    ask_depth_lots = float(np.median(ask_sz))
    touch_notional = float(np.median((bid_sz + ask_sz) / 2.0 * LOT_SHARES * mid))

    fills: list[int] = []
    net_pnl_bps: list[float] = []
    earn_bps: list[float] = []
    horizon = fill_window_s + hold_s
    for i in range(0, n - horizon, post_step_s):
        posted_bid = bid[i]
        mid0 = mid[i]
        future = mid[i + 1 : i + 1 + fill_window_s]
        hit = np.where(future <= posted_bid)[0]
        if hit.size == 0:
            fills.append(0)
            continue
        fills.append(1)
        fill_idx = i + 1 + int(hit[0])
        earned = (mid0 - posted_bid) / mid0  # half-spread captured vs the pre-fill mid
        if fill_idx + hold_s < n:
            adverse = (mid[fill_idx + hold_s] - posted_bid) / posted_bid  # long-at-b mark
            earn_bps.append(earned * 1e4)
            net_pnl_bps.append((earned + adverse) * 1e4)

    if not net_pnl_bps:
        return None
    fills_arr = np.array(fills)
    net = np.array(net_pnl_bps)
    return {
        "symbol_day": f"{symbol}|{day}",
        "symbol": symbol,
        "day": day,
        "quoted_half_spread_bps": quoted_half_bps,
        "bid_depth_lots": bid_depth_lots,
        "ask_depth_lots": ask_depth_lots,
        "touch_notional_usd": touch_notional,
        "fill_rate_ceiling": float(fills_arr.mean()),
        "n_posts": int(fills_arr.size),
        "n_fills": int(net.size),
        "earn_half_spread_bps_mean": float(np.mean(earn_bps)),
        "net_per_fill_bps_mean": float(net.mean()),
        "net_per_fill_bps_median": float(np.median(net)),
        "realized_adverse_bps_mean": float(np.mean(earn_bps) - net.mean()),
    }


def deep_core_symbols() -> list[str]:
    """Symbols with the full deep window (a 2024-12-12 partition) — the LP-testable liquid core."""
    base = f"{STORE}/raw/quotes"
    out = []
    for entry in os.listdir(base):
        if entry.startswith("symbol=") and os.path.isdir(
            os.path.join(base, entry, "date=2024-12-12")
        ):
            out.append(entry.split("=", 1)[1])
    return sorted(out)


def quote_days(reference_symbol: str = "SPY") -> list[str]:
    base = f"{STORE}/raw/quotes/symbol={reference_symbol}"
    return sorted(p.split("date=")[1].split("/")[0] for p in glob.glob(f"{base}/date=*"))


def main() -> None:
    parser = argparse.ArgumentParser(description="LP fill primitives over the deep quote core.")
    parser.add_argument("--symbols", default="", help="comma list; default = deep core")
    parser.add_argument("--n-days", type=int, default=20, help="sample this many quote days evenly")
    parser.add_argument("--fill-window-s", type=int, default=60)
    parser.add_argument("--hold-s", type=int, default=60)
    parser.add_argument("--post-step-s", type=int, default=5)
    parser.add_argument("--out", default="lp_primitives_results.csv")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else deep_core_symbols()
    all_days = quote_days()
    stride = max(1, len(all_days) // args.n_days)
    sample_days = all_days[::stride]
    print(
        f"LP primitives: {len(symbols)} symbols x {len(sample_days)} sampled days "
        f"(of {len(all_days)} total, {all_days[0]}..{all_days[-1]})",
        flush=True,
    )

    rows: list[dict[str, float]] = []
    for symbol in symbols:
        for day in sample_days:
            rec = compute_day_primitives(
                symbol,
                day,
                fill_window_s=args.fill_window_s,
                hold_s=args.hold_s,
                post_step_s=args.post_step_s,
            )
            if rec is not None:
                rows.append(rec)
        print(f"  {symbol}: {sum(1 for r in rows if r['symbol'] == symbol)} days", flush=True)

    if not rows:
        print("NO usable quote data for the requested symbols/days.")
        return
    result = pl.DataFrame(rows)
    result.write_csv(args.out)

    per_symbol = (
        result.group_by("symbol")
        .agg(
            pl.col("quoted_half_spread_bps").median().alias("half_sp_bps"),
            pl.col("fill_rate_ceiling").median(),
            pl.col("earn_half_spread_bps_mean").median().alias("earn_bps"),
            pl.col("realized_adverse_bps_mean").median().alias("adverse_bps"),
            pl.col("net_per_fill_bps_mean").median().alias("net_bps"),
            pl.col("net_per_fill_bps_median").median().alias("net_median_bps"),
            pl.col("touch_notional_usd").median(),
        )
        .sort("half_sp_bps")
    )
    print("\nPER-SYMBOL LP PRIMITIVE SUMMARY (medians across sampled days):")
    with pl.Config(tbl_rows=50, tbl_cols=20, float_precision=2):
        print(per_symbol)
    print(f"\nwrote {result.height} (symbol,day) rows -> {args.out}")
    print(
        "READ: net_per_fill < 0 means the post-fill adverse mid move EXCEEDS the earned half-spread "
        "(providing liquidity loses to adverse selection at that name's tightness). fill_rate is a "
        "CEILING (queue position ignored)."
    )


if __name__ == "__main__":
    main()
