"""HF03 — Spread-capture / passive liquidity provision (the INVERSION of HF01/HF02).

Instead of CROSSING the spread to take a faint qimb edge (HF01/HF02 KILL), POST passively at the
touch and EARN the half-spread — IF the earned spread survives ADVERSE SELECTION.

This is a MODELED backtest with STATED CONSERVATIVE fill assumptions, NOT a live-fill measurement.
The two ways a maker backtest lies, both pre-committed against (see hypothesis.md):

  1. FILL = TRADE-THROUGH, not touch. A passive bid at price P_bid is filled only when a trade prints
     STRICTLY BELOW P_bid (market traded THROUGH the level, clearing the queue ahead of us). We ALSO
     report a stricter queue-depth proxy (cumulative printed size through the level must exceed the
     bid_size resting at post time). Still an OPTIMISTIC upper bound (no true queue visibility) — flagged.

  2. ADVERSE SELECTION via the POST-FILL MARK-OUT (the decisive metric). After EACH fill, mark the
     position at the prevailing MID at +1s/+5s/+30s/+60s. Per-fill net = half_spread_earned −
     adverse_markout_move. HEADLINE = mark-out-net per fill with a per-FILL bootstrap 95% CI; KEEP
     only if that CI excludes zero ABOVE at the conservative trade-through fill, OOS, AND the qimb
     overlay beats the no-overlay baseline.

  3. INVENTORY/EXIT: a filled bid leaves us LONG. We model BOTH a passive-mid exit (mark at the mid)
     and a CROSS exit (pay the half-spread to flatten). Both reported.

qimb OVERLAY: only post on the BID when qimb is NOT strongly negative (book not about to drop); only
post on the ASK when qimb is NOT strongly positive. Sweep the threshold. Canary = shuffle qimb at fill
time (does the overlay beat random posting?).

Read-only data; writes only to the experiment dir.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, "/app/experiments/2026-06-16-hf01-quote-imbalance")
from hf_metrics_fixed import day_clustered_tstat  # noqa: E402

# ── Config ───────────────────────────────────────────────────────────────────
SYMBOLS = [
    "MSFT", "AAPL", "TSLA", "AVGO", "AMD",
    "NVDA", "SPY", "AMZN", "META", "GOOGL",
    "QQQ", "NFLX",
]

BUCKET_SEC = 10                          # posting grid spacing (post a fresh quote every 10s)
MARKOUT_SEC = [1, 5, 30, 60]             # post-fill mark-out horizons (seconds)
QUEUE_PROXY = True                       # also compute stricter queue-depth fill flag

# RTH = UTC minutes [810, 1190) => 13:30–19:50 UTC = 09:30–15:50 ET (genuine UTC, PITFALL #1)
RTH_MIN_START = 810
RTH_MIN_END = 1190

# qimb overlay thresholds to sweep. "post bid only if qimb >= -thr"; "post ask only if qimb <= +thr".
# qimb = (bid_size - ask_size)/(bid_size + ask_size): positive => bid-heavy (book leans up).
QIMB_THRESHOLDS = [0.0, 0.05, 0.10, 0.20, 0.40]
QIMB_WINDOW_SEC = 120                    # trailing window for the qimb signal at post time

QUOTE_ROOT = Path("/store/raw/quotes")
TRADE_ROOT = Path("/store/raw/trades")
EXP_DIR = Path("/app/experiments/2026-06-16-hf03-spread-capture")

TRAIN_FRAC = 0.50
MIN_DAYS = 21
N_BOOT = 2000
N_CANARY_SEEDS = 10


# ── Expressions ──────────────────────────────────────────────────────────────

def utc_min_expr(ts_col: pl.Expr) -> pl.Expr:
    return ts_col.dt.hour().cast(pl.Int32) * 60 + ts_col.dt.minute().cast(pl.Int32)


def floor_bucket_expr(ts_col: pl.Expr) -> pl.Expr:
    bucket_us = BUCKET_SEC * 1_000_000
    return (ts_col.dt.epoch("us") // bucket_us * bucket_us).cast(pl.Datetime("us", "UTC"))


# ── Per-day fill simulation ──────────────────────────────────────────────────

def process_one_day(symbol: str, date_str: str) -> pl.DataFrame | None:
    """Simulate passive maker posting for one (symbol, date).

    At each 10s posting bucket we record the prevailing best bid/ask (the quote we would post AT) and
    the trailing qimb. We then scan the TRADES tape to decide (a) whether a passive bid posted at the
    best-bid is FILLED via TRADE-THROUGH inside the next bucket window, (b) whether a passive ask is
    filled, and (c) the post-fill MARK-OUT mid at +1/5/30/60s. Returns one row PER POSTING EVENT with
    fill flags + mark-out P&L components for BOTH sides, so the caller can apply the qimb overlay and
    aggregate per-fill.
    """
    quote_path = QUOTE_ROOT / f"symbol={symbol}" / f"date={date_str}" / "data.parquet"
    trade_path = TRADE_ROOT / f"symbol={symbol}" / f"date={date_str}" / "data.parquet"
    if not quote_path.exists() or not trade_path.exists():
        return None

    quotes = pl.read_parquet(
        quote_path, columns=["ts", "bid_price", "bid_size", "ask_price", "ask_size"]
    )
    quotes = quotes.with_columns(utc_min_expr(pl.col("ts")).alias("_um"))
    quotes = quotes.filter(
        (pl.col("_um") >= RTH_MIN_START) & (pl.col("_um") < RTH_MIN_END)
        & (pl.col("bid_price") > 0) & (pl.col("ask_price") > 0)
        & (pl.col("bid_size") > 0) & (pl.col("ask_size") > 0)
        & (pl.col("ask_price") >= pl.col("bid_price"))
    ).drop("_um").sort("ts")
    if len(quotes) < 50:
        return None

    quotes = quotes.with_columns([
        ((pl.col("bid_price") + pl.col("ask_price")) / 2.0).alias("mid"),
        ((pl.col("bid_size") - pl.col("ask_size"))
         / (pl.col("bid_size") + pl.col("ask_size"))).alias("qimb_tick"),
    ])

    # Trailing qimb (strict left-closed: only past info at post time).
    quotes = quotes.with_columns(
        pl.col("qimb_tick")
        .rolling_mean_by("ts", window_size=f"{QIMB_WINDOW_SEC}s", closed="left")
        .alias("qimb")
    )

    # Posting grid: one POST per 10s bucket, using the LAST quote seen in that bucket (the quote we
    # would be resting at when the bucket closes / next window opens).
    quotes = quotes.with_columns(floor_bucket_expr(pl.col("ts")).alias("bucket"))
    grid = (
        quotes.group_by("bucket")
        .agg([
            pl.col("bid_price").last().alias("post_bid"),
            pl.col("ask_price").last().alias("post_ask"),
            pl.col("bid_size").last().alias("post_bid_size"),
            pl.col("ask_size").last().alias("post_ask_size"),
            pl.col("mid").last().alias("post_mid"),
            pl.col("qimb").last().alias("qimb"),
        ])
        .sort("bucket")
        .filter(pl.col("post_ask") > pl.col("post_bid"))   # require a real spread to earn
    )
    if len(grid) < 20:
        return None

    # Load trades; restrict to RTH; sort.
    trades = pl.read_parquet(trade_path, columns=["ts", "price", "size"])
    trades = trades.with_columns(utc_min_expr(pl.col("ts")).alias("_um"))
    trades = trades.filter(
        (pl.col("_um") >= RTH_MIN_START) & (pl.col("_um") < RTH_MIN_END) & (pl.col("price") > 0)
    ).drop("_um").sort("ts")
    if len(trades) < 50:
        return None

    trade_ts = trades["ts"].dt.epoch("us").to_numpy().astype(np.int64)
    trade_px = trades["price"].to_numpy()
    trade_sz = trades["size"].to_numpy()

    # Mid path for mark-out: use the per-tick quote mid via searchsorted on the FULL quote stream.
    quote_ts = quotes["ts"].dt.epoch("us").to_numpy().astype(np.int64)
    quote_mid = quotes["mid"].to_numpy()

    post_ts = grid["bucket"].dt.epoch("us").to_numpy().astype(np.int64)
    post_bid = grid["post_bid"].to_numpy()
    post_ask = grid["post_ask"].to_numpy()
    post_bid_size = grid["post_bid_size"].to_numpy()
    post_ask_size = grid["post_ask_size"].to_numpy()
    post_mid = grid["post_mid"].to_numpy()
    qimb = grid["qimb"].fill_null(0.0).to_numpy()

    n_posts = len(post_ts)
    bucket_us = BUCKET_SEC * 1_000_000

    # Fill window = the bucket AFTER posting (we post at bucket close, rest for the next BUCKET_SEC).
    win_start = post_ts + bucket_us
    win_end = post_ts + 2 * bucket_us

    lo = np.searchsorted(trade_ts, win_start, side="left")
    hi = np.searchsorted(trade_ts, win_end, side="left")

    bid_filled = np.zeros(n_posts, dtype=bool)
    ask_filled = np.zeros(n_posts, dtype=bool)
    bid_filled_q = np.zeros(n_posts, dtype=bool)   # queue-depth proxy (stricter)
    ask_filled_q = np.zeros(n_posts, dtype=bool)
    # ACTUAL fill timestamp (the first trade-through), per side, for honest mark-out anchoring. The
    # mark-out MUST start from when the fill really happened, not the window edge — otherwise the +1s
    # mark-out is really an 11s hold and adverse selection over the true hold is undercounted.
    bid_fill_ts = np.full(n_posts, -1, dtype=np.int64)
    ask_fill_ts = np.full(n_posts, -1, dtype=np.int64)
    # Mid AT the fill moment (prevailing quote mid when the fill prints) — the honest reference for the
    # post-fill drift (post_mid is up to 10s stale by the time the fill prints).
    bid_fill_mid = np.full(n_posts, np.nan)
    ask_fill_mid = np.full(n_posts, np.nan)

    for i in range(n_posts):
        a, b = lo[i], hi[i]
        if a >= b:
            continue
        win_px = trade_px[a:b]
        win_sz = trade_sz[a:b]
        win_ts = trade_ts[a:b]
        # TRADE-THROUGH fill: a trade printed STRICTLY THROUGH our resting level.
        through_bid = win_px < post_bid[i]    # seller crossed below our bid => we (buyer) get filled
        through_ask = win_px > post_ask[i]    # buyer crossed above our ask => we (seller) get filled
        if through_bid.any():
            bid_filled[i] = True
            bid_fill_ts[i] = int(win_ts[through_bid][0])   # first trade-through = fill moment
        if through_ask.any():
            ask_filled[i] = True
            ask_fill_ts[i] = int(win_ts[through_ask][0])
        if QUEUE_PROXY:
            # Stricter: printed size THROUGH the level must exceed the queue resting ahead (~bid_size).
            bid_filled_q[i] = bool(win_sz[through_bid].sum() > post_bid_size[i])
            ask_filled_q[i] = bool(win_sz[through_ask].sum() > post_ask_size[i])

    # Mid at the actual fill moment (and post-fill mark-out mids), per side, anchored at the real fill ts.
    bid_anchor = np.where(bid_fill_ts >= 0, bid_fill_ts, win_start)
    ask_anchor = np.where(ask_fill_ts >= 0, ask_fill_ts, win_start)
    bid_mid_idx = np.clip(np.searchsorted(quote_ts, bid_anchor, side="right") - 1, 0, len(quote_mid) - 1)
    ask_mid_idx = np.clip(np.searchsorted(quote_ts, ask_anchor, side="right") - 1, 0, len(quote_mid) - 1)
    bid_fill_mid = quote_mid[bid_mid_idx]
    ask_fill_mid = quote_mid[ask_mid_idx]

    bid_markout_mid: dict[int, np.ndarray] = {}
    ask_markout_mid: dict[int, np.ndarray] = {}
    for sec in MARKOUT_SEC:
        b_target = bid_anchor + sec * 1_000_000
        a_target = ask_anchor + sec * 1_000_000
        b_idx = np.clip(np.searchsorted(quote_ts, b_target, side="right") - 1, 0, len(quote_mid) - 1)
        a_idx = np.clip(np.searchsorted(quote_ts, a_target, side="right") - 1, 0, len(quote_mid) - 1)
        bid_markout_mid[sec] = quote_mid[b_idx]
        ask_markout_mid[sec] = quote_mid[a_idx]

    out = grid.select(["bucket"]).with_columns([
        pl.lit(symbol).alias("symbol"),
        pl.lit(date_str).alias("date"),
        pl.Series("post_bid", post_bid),
        pl.Series("post_ask", post_ask),
        pl.Series("post_mid", post_mid),
        pl.Series("qimb", qimb),
        pl.Series("bid_filled", bid_filled),
        pl.Series("ask_filled", ask_filled),
        pl.Series("bid_filled_q", bid_filled_q),
        pl.Series("ask_filled_q", ask_filled_q),
        pl.Series("bid_fill_mid", bid_fill_mid),
        pl.Series("ask_fill_mid", ask_fill_mid),
    ])
    for sec in MARKOUT_SEC:
        out = out.with_columns([
            pl.Series(f"bid_mo_mid_{sec}", bid_markout_mid[sec]),
            pl.Series(f"ask_mo_mid_{sec}", ask_markout_mid[sec]),
        ])
    return out


# ── Per-fill P&L assembly ────────────────────────────────────────────────────
#
# Everything is anchored at the ACTUAL fill moment (bid_fill_mid/ask_fill_mid = mid prevailing when the
# trade-through prints), NOT the stale post_mid — so the post-fill drift is measured over the TRUE hold.
#
# For a FILLED BID (we BOUGHT at post_bid):
#   earned_spread        = bid_fill_mid - post_bid            (mid at fill − our buy price)
#   markout_pnl(sec)     = bid_mo_mid_sec - post_bid          (passive-mid exit at fill+sec)
#                        = earned_spread + (bid_mo_mid_sec - bid_fill_mid)  [drift]
#   cross_exit_pnl(sec)  = bid_mo_mid_sec - half_spread - post_bid          (pay to flatten)
# For a FILLED ASK (we SOLD at post_ask):
#   earned_spread        = post_ask - ask_fill_mid
#   markout_pnl(sec)     = post_ask - ask_mo_mid_sec
#   cross_exit_pnl(sec)  = post_ask - ask_mo_mid_sec - half_spread
#
# All in fractional units of post_mid (bps = *1e4). The mark-out P&L IS the adverse-selection-adjusted
# realized edge. half_spread for the cross exit uses the post-time half-spread (a fair, slightly
# optimistic proxy for the horizon's spread; cross-exit is the pessimistic bound regardless).


def assemble_fills(panel: pl.DataFrame, qimb_thr: float, side_overlay: bool,
                   use_queue_proxy: bool, shuffle_qimb: bool = False,
                   seed: int = 0) -> dict[int, dict[str, np.ndarray]]:
    """Return per-markout-horizon arrays of per-fill P&L (in fractional units of mid).

    side_overlay=False -> NO-overlay baseline: post BOTH sides always.
    side_overlay=True  -> qimb overlay: post bid only if qimb >= -thr; post ask only if qimb <= +thr.
    use_queue_proxy    -> use the stricter trade-through+queue-depth fill flag.
    shuffle_qimb       -> CANARY: permute qimb within (symbol,date) before applying the overlay.
    Output: {sec: {"markout": arr, "cross": arr, "half_spread": arr, "side": arr(+1 bid / -1 ask)}}
    """
    df = panel
    if shuffle_qimb:
        rng = np.random.default_rng(seed)
        parts = []
        for (_sym, _date), cell in df.group_by(["symbol", "date"]):
            q = cell["qimb"].to_numpy().copy()
            cell = cell.with_columns(pl.Series("qimb", rng.permutation(q)))
            parts.append(cell)
        df = pl.concat(parts)

    bid_col = "bid_filled_q" if use_queue_proxy else "bid_filled"
    ask_col = "ask_filled_q" if use_queue_proxy else "ask_filled"

    qimb = df["qimb"].to_numpy()
    post_bid = df["post_bid"].to_numpy()
    post_ask = df["post_ask"].to_numpy()
    post_mid = df["post_mid"].to_numpy()
    bid_fill_mid = df["bid_fill_mid"].to_numpy()
    ask_fill_mid = df["ask_fill_mid"].to_numpy()
    bid_f = df[bid_col].to_numpy()
    ask_f = df[ask_col].to_numpy()

    half_spread = (post_ask - post_bid) / 2.0

    # Overlay masks: which side are we WILLING to post?
    if side_overlay:
        post_bid_ok = qimb >= -qimb_thr     # skip bid when book strongly bid-light (about to drop)
        post_ask_ok = qimb <= qimb_thr      # skip ask when book strongly bid-heavy (about to rise)
    else:
        post_bid_ok = np.ones(len(df), dtype=bool)
        post_ask_ok = np.ones(len(df), dtype=bool)

    bid_fills = bid_f & post_bid_ok
    ask_fills = ask_f & post_ask_ok

    result: dict[int, dict[str, np.ndarray]] = {}
    for sec in MARKOUT_SEC:
        bid_mo_mid = df[f"bid_mo_mid_{sec}"].to_numpy()
        ask_mo_mid = df[f"ask_mo_mid_{sec}"].to_numpy()

        # BID fills (we are long, bought at post_bid). Anchored at the real fill-moment mid.
        b_markout = (bid_mo_mid[bid_fills] - post_bid[bid_fills]) / post_mid[bid_fills]
        b_earned = (bid_fill_mid[bid_fills] - post_bid[bid_fills]) / post_mid[bid_fills]
        b_drift = (bid_mo_mid[bid_fills] - bid_fill_mid[bid_fills]) / post_mid[bid_fills]
        # cross exit: flatten by selling at (mid - half_spread) at horizon -> pay the half-spread
        b_cross = (bid_mo_mid[bid_fills] - half_spread[bid_fills] - post_bid[bid_fills]) / post_mid[bid_fills]

        # ASK fills (we are short, sold at post_ask)
        a_markout = (post_ask[ask_fills] - ask_mo_mid[ask_fills]) / post_mid[ask_fills]
        a_earned = (post_ask[ask_fills] - ask_fill_mid[ask_fills]) / post_mid[ask_fills]
        a_drift = (ask_fill_mid[ask_fills] - ask_mo_mid[ask_fills]) / post_mid[ask_fills]
        a_cross = (post_ask[ask_fills] - ask_mo_mid[ask_fills] - half_spread[ask_fills]) / post_mid[ask_fills]

        markout = np.concatenate([b_markout, a_markout])
        cross = np.concatenate([b_cross, a_cross])
        earned = np.concatenate([b_earned, a_earned])
        drift = np.concatenate([b_drift, a_drift])
        side = np.concatenate([np.ones(b_markout.size), -np.ones(a_markout.size)])

        finite = np.isfinite(markout) & np.isfinite(cross) & np.isfinite(earned) & np.isfinite(drift)
        result[sec] = {
            "markout": markout[finite],
            "cross": cross[finite],
            "earned": earned[finite],
            "drift": drift[finite],
            "side": side[finite],
        }
    return result


def bootstrap_ci(per_fill: np.ndarray, n_boot: int = N_BOOT, seed: int = 7) -> dict[str, float]:
    """Per-fill bootstrap of the MEAN, in bps. CI must exclude zero ABOVE for a KEEP."""
    arr = per_fill[np.isfinite(per_fill)]
    n = arr.size
    if n < 5:
        return {"n": float(n), "mean_bps": np.nan, "ci_lo_bps": np.nan,
                "ci_hi_bps": np.nan, "win_rate": np.nan, "t": np.nan}
    mean = float(arr.mean())
    se = float(arr.std(ddof=1) / np.sqrt(n)) if n > 1 else np.nan
    t_stat = mean / se if se and se > 0 else np.nan
    rng = np.random.default_rng(seed)
    # Memory-bounded exact bootstrap of the MEAN. Materializing a (n_boot, n) index matrix OOMs for
    # n~1.4M, so resample in row-chunks under a HARD ~160MB index budget and accumulate per-resample
    # means. n_boot=2000 is ample for a stable 95% CI of a mean at n>>1e5 (CI width ~ SE, set by n).
    arr32 = arr.astype(np.float32, copy=False)
    boot = np.empty(n_boot, dtype=np.float64)
    budget_elems = 20_000_000  # ~160MB int64 indices peak per chunk
    chunk = int(max(1, min(n_boot, budget_elems // max(n, 1))))
    filled = 0
    while filled < n_boot:
        rows_now = min(chunk, n_boot - filled)
        idx = rng.integers(0, n, size=(rows_now, n), dtype=np.int64)
        boot[filled:filled + rows_now] = arr32[idx].mean(axis=1)
        del idx
        filled += rows_now
    return {
        "n": float(n),
        "mean_bps": mean * 1e4,
        "ci_lo_bps": float(np.percentile(boot, 2.5)) * 1e4,
        "ci_hi_bps": float(np.percentile(boot, 97.5)) * 1e4,
        "win_rate": float((arr > 0).mean()),
        "t": t_stat,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    EXP_DIR.mkdir(parents=True, exist_ok=True)
    print("=== HF03: passive maker spread-capture (trade-through fill + mark-out) ===", flush=True)

    all_frames: list[pl.DataFrame] = []
    spread_bps_per_sym: dict[str, float] = {}

    for sym in SYMBOLS:
        sym_dir = QUOTE_ROOT / f"symbol={sym}"
        if not sym_dir.exists():
            print(f"  {sym}: no quote dir, skip", flush=True)
            continue
        dates = sorted([d.name.replace("date=", "") for d in sym_dir.iterdir() if d.is_dir()])
        if len(dates) < MIN_DAYS:
            print(f"  {sym}: {len(dates)} days < {MIN_DAYS}, skip", flush=True)
            continue

        sym_frames: list[pl.DataFrame] = []
        spreads: list[float] = []
        for date_str in dates:
            day_df = process_one_day(sym, date_str)
            if day_df is None or day_df.is_empty():
                continue
            hs = float(((day_df["post_ask"] - day_df["post_bid"]) / day_df["post_mid"]).mean() * 1e4 / 2.0)
            spreads.append(hs)
            sym_frames.append(day_df)

        if not sym_frames:
            print(f"  {sym}: no valid days, skip", flush=True)
            continue
        sym_df = pl.concat(sym_frames)
        n_days = sym_df["date"].n_unique()
        if n_days < MIN_DAYS:
            print(f"  {sym}: {n_days} valid days < {MIN_DAYS}, skip", flush=True)
            continue
        spread_bps_per_sym[sym] = float(np.mean(spreads))
        bid_rate = float(sym_df["bid_filled"].mean())
        bid_rate_q = float(sym_df["bid_filled_q"].mean())
        print(f"  {sym}: {n_days} days, {len(sym_df)} posts, half-spread={spread_bps_per_sym[sym]:.3f} bps, "
              f"bid trade-through fill rate={bid_rate:.3f} (queue-proxy={bid_rate_q:.3f})", flush=True)
        all_frames.append(sym_df)

    if not all_frames:
        print("ERROR: no data", flush=True)
        sys.exit(1)

    panel = pl.concat(all_frames).sort(["symbol", "date", "bucket"])
    all_dates = sorted(panel["date"].unique().to_list())
    n_train = int(len(all_dates) * TRAIN_FRAC)
    oos_dates = set(all_dates[n_train:])
    panel_oos = panel.filter(pl.col("date").is_in(list(oos_dates)))
    print(f"\nPanel: {len(panel)} posts, {len(all_dates)} days, {panel['symbol'].n_unique()} symbols. "
          f"OOS={len(oos_dates)} days, {len(panel_oos)} posts.", flush=True)

    pl.DataFrame({
        "symbol": list(spread_bps_per_sym.keys()),
        "half_spread_bps": list(spread_bps_per_sym.values()),
    }).write_csv(EXP_DIR / "spreads.csv")

    # ── Sweep: fill model × overlay × qimb threshold × markout horizon ──────────
    rows: list[dict] = []
    sweep = [("baseline", None, False)] + [("qimb", thr, True) for thr in QIMB_THRESHOLDS]
    for label, dataframe in [("ALL", panel), ("OOS", panel_oos)]:
        for fill_label, use_q in [("trade_through", False), ("queue_proxy", True)]:
            for overlay_name, thr, side_overlay in sweep:
                fills = assemble_fills(dataframe, qimb_thr=(thr or 0.0),
                                       side_overlay=side_overlay, use_queue_proxy=use_q)
                for sec in MARKOUT_SEC:
                    mk = bootstrap_ci(fills[sec]["markout"])
                    cr = bootstrap_ci(fills[sec]["cross"])
                    earned = fills[sec]["earned"]
                    drift = fills[sec]["drift"]
                    rows.append({
                        "split": label, "fill": fill_label, "overlay": overlay_name, "qimb_thr": thr,
                        "markout_sec": sec, "n_fills": int(mk["n"]),
                        "earned_spread_bps": round(float(np.nanmean(earned)) * 1e4, 4) if earned.size else None,
                        "adverse_drift_bps": round(float(np.nanmean(drift)) * 1e4, 4) if drift.size else None,
                        "markout_mean_bps": round(mk["mean_bps"], 4) if np.isfinite(mk["mean_bps"]) else None,
                        "markout_ci_lo": round(mk["ci_lo_bps"], 4) if np.isfinite(mk["ci_lo_bps"]) else None,
                        "markout_ci_hi": round(mk["ci_hi_bps"], 4) if np.isfinite(mk["ci_hi_bps"]) else None,
                        "markout_t": round(mk["t"], 2) if np.isfinite(mk["t"]) else None,
                        "win_rate": round(mk["win_rate"], 4) if np.isfinite(mk["win_rate"]) else None,
                        "cross_mean_bps": round(cr["mean_bps"], 4) if np.isfinite(cr["mean_bps"]) else None,
                        "cross_ci_lo": round(cr["ci_lo_bps"], 4) if np.isfinite(cr["ci_lo_bps"]) else None,
                        "cross_ci_hi": round(cr["ci_hi_bps"], 4) if np.isfinite(cr["ci_hi_bps"]) else None,
                    })

    results = pl.DataFrame(rows)
    results.write_csv(EXP_DIR / "results.csv")
    print("\n=== RESULTS (markout-net per fill, bps) ===", flush=True)
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=240):
        print(results.filter(pl.col("fill") == "trade_through").sort(
            ["split", "overlay", "qimb_thr", "markout_sec"]).__str__(), flush=True)

    # ── CANARY: shuffle qimb at fill time; does the overlay beat random posting? ──
    print("\n=== CANARY: shuffle-qimb overlay (best OOS threshold) ===", flush=True)
    canary_rows: list[dict] = []
    for sec in MARKOUT_SEC:
        for thr in QIMB_THRESHOLDS:
            real = assemble_fills(panel_oos, qimb_thr=thr, side_overlay=True, use_queue_proxy=False)
            real_mean = float(np.mean(real[sec]["markout"])) * 1e4 if real[sec]["markout"].size >= 5 else np.nan
            shuf_means = []
            for s in range(N_CANARY_SEEDS):
                sh = assemble_fills(panel_oos, qimb_thr=thr, side_overlay=True,
                                    use_queue_proxy=False, shuffle_qimb=True, seed=100 + s)
                if sh[sec]["markout"].size >= 5:
                    shuf_means.append(float(np.mean(sh[sec]["markout"])) * 1e4)
            if shuf_means:
                canary_rows.append({
                    "markout_sec": sec, "qimb_thr": thr,
                    "real_mean_bps": round(real_mean, 4) if np.isfinite(real_mean) else None,
                    "shuf_lo_bps": round(float(np.percentile(shuf_means, 2.5)), 4),
                    "shuf_hi_bps": round(float(np.percentile(shuf_means, 97.5)), 4),
                    "beats_canary": bool(np.isfinite(real_mean) and real_mean > np.percentile(shuf_means, 97.5)),
                })
    canary_df = pl.DataFrame(canary_rows)
    canary_df.write_csv(EXP_DIR / "canary.csv")
    with pl.Config(tbl_rows=-1, tbl_width_chars=200):
        print(canary_df.__str__(), flush=True)

    # ── Verdict logic ──────────────────────────────────────────────────────────
    oos_tt = results.filter((pl.col("split") == "OOS") & (pl.col("fill") == "trade_through"))
    base_rows = oos_tt.filter(pl.col("overlay") == "baseline")
    ov_rows = oos_tt.filter(pl.col("overlay") == "qimb")

    def best_keep(frame: pl.DataFrame) -> dict | None:
        kept = frame.filter(
            pl.col("markout_ci_lo").is_not_null() & (pl.col("markout_ci_lo") > 0)
        )
        if kept.is_empty():
            return None
        return kept.sort("markout_ci_lo", descending=True).row(0, named=True)

    base_keep = best_keep(base_rows)
    ov_keep = best_keep(ov_rows)

    # Did the best overlay BEAT the baseline at the same horizon (markout mean)?
    overlay_beats = False
    for sec in MARKOUT_SEC:
        b = base_rows.filter(pl.col("markout_sec") == sec)
        o = ov_rows.filter(pl.col("markout_sec") == sec)
        if b.is_empty() or o.is_empty():
            continue
        b_mean = b["markout_mean_bps"][0]
        o_best = o["markout_mean_bps"].max()
        if b_mean is not None and o_best is not None and o_best > b_mean:
            overlay_beats = True

    print("\n=== VERDICT INPUTS ===", flush=True)
    print(f"  OOS trade-through baseline best CI-lo>0 cell: {base_keep}", flush=True)
    print(f"  OOS trade-through qimb-overlay best CI-lo>0 cell: {ov_keep}", flush=True)
    print(f"  qimb overlay markout-mean beats baseline at some horizon: {overlay_beats}", flush=True)

    if ov_keep is not None and overlay_beats:
        verdict = "KEEP-AS-LEAD"
    elif ov_keep is not None or base_keep is not None:
        verdict = "AMBIGUOUS"
    else:
        verdict = "KILL"
    print(f"\n=== HF03 VERDICT: {verdict} ===", flush=True)
    print("\nDone. CSVs in", EXP_DIR, flush=True)


if __name__ == "__main__":
    main()
