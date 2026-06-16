"""vwap_dev baseline at depth + H1-recheck. Research script (NOT engine code).

Sharded by DATE to stay within the 16g sandbox budget. For each trading date we:
  1. Load every symbol's bars for that date (one parquet per symbol-date).
  2. Filter RTH (13:30:00-20:00:00 UTC inclusive).
  3. Reindex each symbol to the full 1-min RTH grid (390 bars), forward-fill close
     (and ffill vwap/volume->0 for the cumVWAP running sums). DOCUMENTED choice: a
     symbol with no trade in a minute carries its last close; volume contributes 0 to
     cumVWAP for that minute. This gives a strict wall-clock minute alignment so the
     cross-sections at a given clock-minute are comparable across symbols.
  4. session cumVWAP_t = cumsum(vwap*volume)/cumsum(volume) over RTH bars up to & incl t.
     vwap_dev_t = close_t / cumVWAP_t - 1.
  5. Forward return TRADEABLE: fwd = close(t+H)/close(t+1) - 1 (enter t+1, exit t+H),
     H in {15,30} on the 1-min grid. Only minutes where both t+1 and t+H exist (i.e.
     minute index m s.t. m+1+H <= last grid index) get a fwd value -> no look-ahead.
  6. trailing dollar-volume liq proxy: rolling 30-min sum of (close*volume) per symbol at t.

Then we accumulate, per (date, clock-minute) cross-section:
  - Spearman rank-IC(vwap_dev, demeaned-fwd) pooled  [demean fwd within (date,minute)]
  - per liquidity-tercile IC (split the cross-section by the trailing $vol proxy)
  - shuffle-canary IC (permute fwd within the minute, N_SEEDS seeds)
  - decile L/S book net-of-cost inputs per tier (mean fwd of top vs bottom decile of vwap_dev)

Day-clustered t: per-day mean IC over its minutes, then t = mean_days/(std_days/sqrt(n_days)).
"""

from __future__ import annotations

import glob
import os
import sys
from collections import defaultdict

import numpy as np
import polars as pl

BARS_ROOT = "/store/raw/bars"
OUT_DIR = "/app/experiments/2026-06-16-vwap-baseline-depth"

RTH_START_S = 13 * 3600 + 30 * 60  # 13:30:00 UTC seconds-of-day
RTH_END_S = 20 * 3600  # 20:00:00 UTC
HORIZONS = [15, 30]
N_TIERS = 3
N_SEEDS = 10
LIQ_WINDOW = 30  # minutes trailing for dollar-volume proxy
COST_ONE_WAY_BPS = 2.0
MIN_XS = 20  # need >=20 names in a cross-section to bother ranking
RNG = np.random.default_rng(12345)


def _rankdata(values: np.ndarray) -> np.ndarray:
    """Average-rank (handles ties) — equivalent to scipy.stats.rankdata 'average'."""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=np.float64)
    ranks[order] = np.arange(1, values.size + 1, dtype=np.float64)
    # resolve ties to average rank
    sorted_vals = values[order]
    i = 0
    n = values.size
    while i < n:
        j = i + 1
        while j < n and sorted_vals[j] == sorted_vals[i]:
            j += 1
        if j - i > 1:
            avg = (i + 1 + j) / 2.0  # average of ranks (1-based) i+1..j
            ranks[order[i:j]] = avg
        i = j
    return ranks


def spearman_ic(feat: np.ndarray, fwd: np.ndarray) -> float:
    if feat.size < MIN_XS:
        return np.nan
    if np.all(feat == feat[0]) or np.all(fwd == fwd[0]):
        return np.nan
    rf = _rankdata(feat)
    rr = _rankdata(fwd)
    rf = rf - rf.mean()
    rr = rr - rr.mean()
    denom = np.sqrt((rf * rf).sum() * (rr * rr).sum())
    if denom == 0:
        return np.nan
    return float((rf * rr).sum() / denom)


def list_dates() -> list[str]:
    dates: set[str] = set()
    for d in glob.glob(f"{BARS_ROOT}/symbol=*/date=*"):
        dates.add(os.path.basename(d).split("=", 1)[1])
    return sorted(dates)


def load_date(date: str) -> pl.DataFrame | None:
    files = glob.glob(f"{BARS_ROOT}/symbol=*/date={date}/*.parquet")
    if not files:
        return None
    frames = []
    for f in files:
        df = pl.read_parquet(
            f, columns=["symbol", "ts", "close", "volume", "vwap"]
        )
        frames.append(df)
    return pl.concat(frames)


def build_grid_frame(df: pl.DataFrame, date: str) -> pl.DataFrame:
    """Reindex each symbol to full 1-min RTH grid; ffill close; compute cumVWAP, vwap_dev,
    fwd returns (per H), trailing $vol. Returns a long frame with one row per (symbol,minute)."""
    sod = (
        df["ts"].dt.hour().cast(pl.Int64) * 3600
        + df["ts"].dt.minute().cast(pl.Int64) * 60
        + df["ts"].dt.second().cast(pl.Int64)
    )
    df = df.with_columns(sod.alias("sod"))
    df = df.filter((pl.col("sod") >= RTH_START_S) & (pl.col("sod") < RTH_END_S))
    if df.height == 0:
        return df
    # minute index 0..389
    df = df.with_columns(((pl.col("sod") - RTH_START_S) // 60).alias("mi"))
    # collapse any dup minutes (shouldn't happen) keeping last
    df = df.sort(["symbol", "mi"]).unique(subset=["symbol", "mi"], keep="last")

    grid = pl.DataFrame({"mi": list(range(390))})
    out_frames = []
    for symbol, sub in df.group_by("symbol", maintain_order=True):
        sym = symbol[0] if isinstance(symbol, tuple) else symbol
        sub = sub.sort("mi")
        g = grid.join(sub, on="mi", how="left").with_columns(
            pl.lit(sym).alias("symbol")
        )
        # ffill close; volume/vw*vol missing -> 0 contribution
        g = g.with_columns(
            pl.col("close").forward_fill().backward_fill().alias("close"),
            pl.col("volume").fill_null(0).alias("volume"),
            pl.col("vwap").alias("vwap"),
        )
        g = g.with_columns(
            (pl.col("vwap").fill_null(pl.col("close")) * pl.col("volume")).alias("pv")
        )
        g = g.with_columns(
            (pl.col("pv").cum_sum() / pl.col("volume").cum_sum().clip(lower_bound=1)).alias(
                "cumvwap"
            ),
            (pl.col("close") * pl.col("volume")).alias("dollar"),
        )
        g = g.with_columns(
            (pl.col("close") / pl.col("cumvwap") - 1.0).alias("vwap_dev"),
            pl.col("dollar")
            .rolling_sum(window_size=LIQ_WINDOW, min_samples=1)
            .alias("trail_dollar"),
        )
        close = g["close"].to_numpy()
        n = close.shape[0]
        for horizon in HORIZONS:
            fwd = np.full(n, np.nan)
            # enter t+1, exit t+H : valid where m+1+horizon <= n-1
            for m in range(n):
                entry = m + 1
                exit_i = m + horizon
                if exit_i <= n - 1 and close[entry] > 0:
                    fwd[m] = close[exit_i] / close[entry] - 1.0
            g = g.with_columns(pl.Series(f"fwd{horizon}", fwd))
        out_frames.append(
            g.select(
                ["symbol", "mi", "vwap_dev", "trail_dollar"]
                + [f"fwd{h}" for h in HORIZONS]
            )
        )
    return pl.concat(out_frames)


# accumulators: per (horizon) -> per date -> list of minute ICs
day_ic: dict[int, dict[str, list[float]]] = {h: defaultdict(list) for h in HORIZONS}
day_ic_tier: dict[int, dict[int, dict[str, list[float]]]] = {
    h: {t: defaultdict(list) for t in range(N_TIERS)} for h in HORIZONS
}
canary_ic: dict[int, list[float]] = {h: [] for h in HORIZONS}
# net-of-cost: per (horizon, tier) accumulate decile spread fwd & count
ls_spread: dict[int, dict[int, list[float]]] = {
    h: {t: [] for t in range(N_TIERS)} for h in HORIZONS
}
pooled_ic: dict[int, list[float]] = {h: [] for h in HORIZONS}

panel_rows = 0
panel_xs = 0


def process_minute(
    horizon: int, date: str, feat: np.ndarray, fwd: np.ndarray, liq: np.ndarray
) -> None:
    global panel_xs
    mask = np.isfinite(feat) & np.isfinite(fwd) & np.isfinite(liq)
    feat = feat[mask]
    fwd = fwd[mask]
    liq = liq[mask]
    if feat.size < MIN_XS:
        return
    fwd = fwd - fwd.mean()  # cross-sectional demean within (date,minute)
    ic = spearman_ic(feat, fwd)
    if np.isfinite(ic):
        day_ic[horizon][date].append(ic)
        pooled_ic[horizon].append(ic)
        panel_xs += 1
    # canary
    for _ in range(N_SEEDS):
        perm = RNG.permutation(fwd)
        cic = spearman_ic(feat, perm)
        if np.isfinite(cic):
            canary_ic[horizon].append(cic)
    # liquidity terciles
    order = np.argsort(liq)
    tier_bounds = np.array_split(order, N_TIERS)
    for tier_idx, idxs in enumerate(tier_bounds):  # tier 0 = illiquid, N-1 = liquid
        if idxs.size < MIN_XS:
            continue
        tf = feat[idxs]
        tr = fwd[idxs]
        tic = spearman_ic(tf, tr)
        if np.isfinite(tic):
            day_ic_tier[horizon][tier_idx][date].append(tic)
        # decile L/S book within tier: top vs bottom decile of vwap_dev
        if idxs.size >= 20:
            dec = max(1, idxs.size // 10)
            sf = np.argsort(tf)
            bottom = tr[sf[:dec]].mean()
            top = tr[sf[-dec:]].mean()
            # vwap_dev reversion: short high vwap_dev (rich), long low (cheap) -> long bottom - top
            ls_spread[horizon][tier_idx].append(bottom - top)


def day_clustered_t(date_to_ics: dict[str, list[float]]) -> tuple[float, float, int]:
    day_means = [np.mean(v) for v in date_to_ics.values() if len(v) > 0]
    if len(day_means) < 2:
        return (float("nan"), float("nan"), len(day_means))
    arr = np.array(day_means)
    mean = arr.mean()
    se = arr.std(ddof=1) / np.sqrt(arr.size)
    return (float(mean), float(mean / se if se > 0 else float("nan")), arr.size)


def main() -> None:
    global panel_rows
    dates = list_dates()
    max_dates = int(os.environ.get("MAX_DATES", "0"))
    if max_dates > 0:
        dates = dates[:max_dates]
    n_sym_seen: set[str] = set()
    print(f"Processing {len(dates)} dates...", flush=True)
    for di, date in enumerate(dates):
        raw = load_date(date)
        if raw is None or raw.height == 0:
            continue
        frame = build_grid_frame(raw, date)
        if frame.height == 0:
            continue
        n_sym_seen.update(frame["symbol"].unique().to_list())
        panel_rows += frame.height
        # iterate minutes
        for mi, msub in frame.group_by("mi"):
            feat = msub["vwap_dev"].to_numpy()
            liq = msub["trail_dollar"].to_numpy()
            for horizon in HORIZONS:
                fwd = msub[f"fwd{horizon}"].to_numpy()
                process_minute(horizon, date, feat, fwd, liq)
        if (di + 1) % 10 == 0:
            print(f"  ...{di+1}/{len(dates)} dates done", flush=True)

    print("\n========== RESULTS ==========", flush=True)
    print(f"Panel: n_symbols_seen={len(n_sym_seen)}  n_dates={len(dates)}", flush=True)
    print(f"Grid rows processed (symbol-minute): {panel_rows:,}", flush=True)
    for horizon in HORIZONS:
        n_xs = len(pooled_ic[horizon])
        pooled_mean = float(np.mean(pooled_ic[horizon])) if n_xs else float("nan")
        mean_d, t_d, n_days = day_clustered_t(day_ic[horizon])
        print(f"\n--- H={horizon} ---", flush=True)
        print(f"  n_cross_sections (valid minutes): {n_xs}", flush=True)
        print(f"  pooled mean IC: {pooled_mean:+.5f}", flush=True)
        print(
            f"  day-clustered: mean_IC={mean_d:+.5f}  t={t_d:+.2f}  n_days={n_days}",
            flush=True,
        )
        # canary
        can = np.array(canary_ic[horizon])
        print(
            f"  canary IC: mean={can.mean():+.5f} std={can.std():.5f} (n={can.size})",
            flush=True,
        )
        # tier ICs
        tier_means = []
        for tier_idx in range(N_TIERS):
            tmean, tt, tnd = day_clustered_t(day_ic_tier[horizon][tier_idx])
            tier_means.append(tmean)
            label = (
                "illiquid"
                if tier_idx == 0
                else ("liquid" if tier_idx == N_TIERS - 1 else "mid")
            )
            print(
                f"  tier {tier_idx} ({label}): mean_IC={tmean:+.5f} t={tt:+.2f} n_days={tnd}",
                flush=True,
            )
        illiq = abs(tier_means[0])
        liqv = abs(tier_means[-1])
        ratio = illiq / liqv if liqv > 0 else float("nan")
        print(f"  illiquid/liquid |IC| ratio: {ratio:.3f}", flush=True)
        # net of cost: decile L/S book per tier. spread is per-period mean return of
        # (long cheap decile - short rich decile). gross per period. Cost: full rebalance
        # each period -> turnover ~ 2 legs * 2 sides * COST one-way; conservative: assume
        # 100% turnover/period each side => round-trip cost = 4 * one-way? Use crude:
        # book holds H minutes; cost per period (entry+exit, 2 sides) = 4*one-way bps.
        for tier_idx in range(N_TIERS):
            spreads = np.array(ls_spread[horizon][tier_idx])
            if spreads.size == 0:
                continue
            gross = spreads.mean()  # per-period gross return of L/S book
            # crude cost: rebuilding the L/S book every period. one-way 2bps, 2 sides,
            # entry+exit = 4 * one-way = 8 bps/period round-trip on notional.
            cost = 4 * COST_ONE_WAY_BPS / 1e4
            net = gross - cost
            label = (
                "illiquid"
                if tier_idx == 0
                else ("liquid" if tier_idx == N_TIERS - 1 else "mid")
            )
            print(
                f"  L/S net tier {tier_idx} ({label}): gross/period={gross*1e4:+.2f}bps "
                f"cost={cost*1e4:.1f}bps net={net*1e4:+.2f}bps "
                f"{'CLEARS' if net > 0 else 'fails'}",
                flush=True,
            )
    print("\n========== END ==========", flush=True)


if __name__ == "__main__":
    sys.exit(main())  # type: ignore[func-returns-value]
