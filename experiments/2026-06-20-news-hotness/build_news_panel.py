"""NEWS HOTNESS panel builder — compute-time join to the news_articles store (registered #230, locked).

Builds a per-(name, entry-day) panel over the 7-month news window (2025-11-12 → 2026-06-19), joining the
deep `fp_store_real` bars to the news store at compute time (available_at + EMBARGO <= T — the filings/
sector_map parity contract; symbols[] exploded at read time). For each entry (one per name per day at the
sampled tradeable minute >= 09:35 ET), emits:
  - RAW hotness: news_count_{1h,4h,24h,7d}, recency_min, velocity_24h (over articles with
    available_at + EMBARGO <= entry).
  - PER-NAME z (point-in-time): news_hot_z_24h = (count_24h - mu_name)/sigma_name vs the name's trailing
    BASELINE_DAYS=60 daily-count baseline computed strictly from articles with available_at < T - 24h
    (no overlap → no look-ahead); news_burst_24h = count_24h / (trailing-60d mean daily count + 1).
  - RELEVANCE (keyword-free subset that needs only symbols[]): excl_24h (# trailing-24h articles where this
    is the ONLY symbol), dilution_24h (mean # other symbols/article).
  - CONTROLS: own_vol (trailing-20d daily-return realized vol), size (log trailing-20d ADV), base_cov
    (the name's trailing-60d mean daily article count — the baseline-coverage control).
  - TARGETS: y_absret / y_rv / y_logvol (magnitude, H1) and y_ret (signed, H2) forward over {30m, EOD,
    next-day}, entered at the tradeable open.

EMBARGO is swept {1,5,15} by re-running with EMBARGO_MIN; the panel writes one EMBARGO's columns per run
(the orchestrator runs all three). Reuses the #205/#212 host-mounted resumable daily-cache + chunked infra.
READ-ONLY stores.
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

STORE = os.environ.get("STORE_ROOT", "/store")
OUT_DIR = "/app/experiments/2026-06-20-news-hotness"
NEWS_START = "2025-11-12"
NEWS_END = "2026-06-19"
ENTRY_ET_MIN = 9 * 60 + 35  # 09:35 ET tradeable entry
EMBARGO_MIN = int(os.environ.get("EMBARGO_MIN", "5"))  # the feed-delay buffer (swept {1,5,15})
BASELINE_DAYS = 60  # trailing per-name baseline window
MIN_BASELINE_OBS = 20  # need this many baseline days or the z is NULL
MIN_ARTICLES = 20  # a name needs this many total articles to enter the screen
HOT_WINDOWS_MIN = {"1h": 60, "4h": 240, "24h": 1440, "7d": 10080}
FWD = {"30m": 30}  # intraday forward horizon (EOD/next-day handled via daily series)
VOL_DAYS = 20
MAX_DAYS_PER_RUN = int(os.environ.get("MAX_DAYS_PER_RUN", "0"))
CACHE_ONLY = os.environ.get("CACHE_ONLY", "0") == "1"
MIN_PRICE = 1.0


def rth_etm(ts_col: pl.Expr) -> pl.Expr:
    et = ts_col.dt.convert_time_zone("America/New_York")
    return et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)


def list_days() -> list[str]:
    days = sorted(
        p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=SPY/date=*")
    )
    return [d for d in days if NEWS_START <= d <= NEWS_END]


def day_daily(day: str) -> pl.DataFrame:
    pattern = f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet"
    if not glob.glob(pattern):
        return pl.DataFrame()
    lazy = pl.scan_parquet(pattern, hive_partitioning=True).select(["symbol", "ts", "close", "volume"])
    etm = rth_etm(pl.col("ts"))
    rth = lazy.with_columns(etm.alias("_m")).filter((pl.col("_m") >= 9 * 60 + 30) & (pl.col("_m") < 16 * 60))
    out = (
        rth.sort("ts")
        .group_by("symbol")
        .agg(
            pl.col("close").last().alias("close"),
            (pl.col("close") * pl.col("volume")).sum().alias("dvol"),
            pl.col("close").filter(pl.col("_m") >= ENTRY_ET_MIN).first().alias("entry"),
            pl.col("close").filter(pl.col("_m") >= ENTRY_ET_MIN).head(35).alias("_first35"),
        )
        .collect()
    )
    # forward 30m intraday move from the entry: |ret| over the first ~30 bars after entry
    out = out.with_columns(
        pl.col("_first35")
        .map_elements(
            lambda c: (
                float(c[min(30, len(c) - 1)] / c[0] - 1.0) if c is not None and len(c) > 2 and c[0] else None
            ),
            return_dtype=pl.Float64,
        )
        .alias("fwd30_ret")
    ).drop("_first35")
    return out.with_columns(pl.lit(day).alias("date"))


def build_daily_cache(days: list[str]) -> pl.DataFrame:
    cache_dir = f"{OUT_DIR}/daily_cache"
    os.makedirs(cache_dir, exist_ok=True)
    done = {p[:-8] for p in os.listdir(cache_dir) if p.endswith(".parquet")}
    pending = [d for d in days if d not in done]
    if MAX_DAYS_PER_RUN > 0:
        pending = pending[:MAX_DAYS_PER_RUN]
    if done:
        print(f"resuming: {len(done)} cached, {len(pending)} this chunk", flush=True)
    for i, day in enumerate(pending):
        dd = day_daily(day)
        (dd if dd.height else pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Utf8})).write_parquet(
            f"{cache_dir}/{day}.parquet"
        )
        if (i + 1) % 30 == 0 or i == len(pending) - 1:
            print(f"  cached {len(done) + i + 1} (chunk {i + 1}/{len(pending)})", flush=True)
    n = len([p for p in os.listdir(cache_dir) if p.endswith(".parquet")])
    print(f"CACHE_STATUS cached={n} total={len(days)}", flush=True)
    if CACHE_ONLY:
        return pl.DataFrame()
    return pl.scan_parquet(f"{cache_dir}/*.parquet").filter(pl.col("date").is_in(set(days))).collect()


def load_news() -> pl.DataFrame:
    files = glob.glob(f"{STORE}/news/published_date=*/*.parquet")
    news = pl.concat(
        [pl.read_parquet(f).select(["symbols", "available_at"]) for f in files], how="vertical_relaxed"
    )
    # per-article: n_symbols (for dilution/exclusive) + the known-at instant (created_at + embargo)
    news = news.with_columns(
        pl.col("symbols").list.len().alias("n_sym"),
        (pl.col("available_at") + pl.duration(minutes=EMBARGO_MIN)).alias("known_at"),
    )
    return news.explode("symbols").rename({"symbols": "symbol"}).select(["symbol", "known_at", "n_sym"])


def build() -> None:
    days = list_days()
    print(f"trading days {len(days)} {days[0]}..{days[-1]} (EMBARGO={EMBARGO_MIN}m)", flush=True)
    daily = build_daily_cache(days)
    if CACHE_ONLY:
        return
    news = load_news()
    # screen universe: names with >= MIN_ARTICLES total AND present in the bar cache
    art_counts = news.group_by("symbol").len()
    news_syms = set(art_counts.filter(pl.col("len") >= MIN_ARTICLES)["symbol"].to_list())
    bar_syms = set(daily["symbol"].unique().to_list())
    universe = sorted(news_syms & bar_syms)
    print(f"universe={len(universe)} (>= {MIN_ARTICLES} articles AND in bars)", flush=True)

    # per-name daily close series + per-name article known_at timestamps (epoch sec for fast windowing)
    day_dt = {d: dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc) for d in days}
    closes: dict[str, dict[str, float]] = {}
    dvols: dict[str, dict[str, float]] = {}
    entries: dict[str, dict[str, float]] = {}
    fwd30: dict[str, dict[str, float]] = {}
    for sym, grp in daily.filter(pl.col("symbol").is_in(set(universe))).group_by("symbol"):
        s = sym[0] if isinstance(sym, tuple) else sym
        closes[s] = dict(zip(grp["date"].to_list(), grp["close"].to_list()))
        dvols[s] = dict(zip(grp["date"].to_list(), grp["dvol"].to_list()))
        entries[s] = dict(zip(grp["date"].to_list(), grp["entry"].to_list()))
        fwd30[s] = dict(zip(grp["date"].to_list(), grp["fwd30_ret"].to_list()))
    art: dict[str, np.ndarray] = {}
    art_nsym: dict[str, np.ndarray] = {}
    for sym, grp in news.filter(pl.col("symbol").is_in(set(universe))).group_by("symbol"):
        s = sym[0] if isinstance(sym, tuple) else sym
        g = grp.sort("known_at")
        art[s] = g["known_at"].to_numpy().astype("datetime64[s]").astype("int64")
        art_nsym[s] = g["n_sym"].to_numpy()

    rows = []
    for sym in universe:
        kn = art.get(sym)
        if kn is None:
            continue
        nsym = art_nsym[sym]
        cser = closes[sym]
        sorted_days = [d for d in days if d in cser]
        for di, day in enumerate(sorted_days):
            if di < VOL_DAYS:
                continue
            entry = entries[sym].get(day)
            if entry is None or entry < MIN_PRICE:
                continue
            # entry instant in epoch sec: 09:35 ET on `day`, converted to UTC (DST-correct via ET tz).
            entry_et = dt.datetime.fromisoformat(day).replace(
                hour=ENTRY_ET_MIN // 60, minute=ENTRY_ET_MIN % 60, tzinfo=ZoneInfo("America/New_York")
            )
            T = int(entry_et.timestamp())
            # hotness counts over windows ending at T (known_at <= T)
            feats = {}
            for tag, wmin in HOT_WINDOWS_MIN.items():
                lo = T - wmin * 60
                feats[f"news_count_{tag}"] = int(np.sum((kn <= T) & (kn > lo)))
            known_before = kn[kn <= T]
            feats["news_recency_min"] = float((T - known_before[-1]) / 60.0) if len(known_before) else 1e5
            feats["news_velocity_24h"] = feats["news_count_24h"] / 24.0
            # relevance: trailing-24h exclusive (n_sym==1) + dilution
            w24_mask = (kn <= T) & (kn > T - 1440 * 60)
            feats["news_excl_24h"] = int(np.sum(w24_mask & (nsym == 1)))
            feats["news_dilution_24h"] = float(np.mean(nsym[w24_mask])) if w24_mask.any() else 0.0
            # per-name point-in-time z + burst vs trailing-60d daily-count baseline (known_at < T-24h)
            base_lo = T - (BASELINE_DAYS + 1) * 86400
            base_hi = T - 1440 * 60  # strictly before the current 24h window
            base_known = kn[(kn >= base_lo) & (kn < base_hi)]
            if len(base_known) >= MIN_BASELINE_OBS:
                day_bins = ((base_hi - base_known) // 86400).astype(int)
                daily_counts = np.bincount(day_bins, minlength=BASELINE_DAYS)[:BASELINE_DAYS]
                mu, sd = float(daily_counts.mean()), float(daily_counts.std())
                feats["news_hot_z_24h"] = (feats["news_count_24h"] - mu) / sd if sd > 1e-9 else None
                feats["news_burst_24h"] = feats["news_count_24h"] / (mu + 1.0)
                feats["base_cov"] = mu
            else:
                feats["news_hot_z_24h"] = None
                feats["news_burst_24h"] = None
                feats["base_cov"] = None
            # controls: own_vol (trailing-20d), size (log trailing-20d ADV)
            prior_days = sorted_days[di - VOL_DAYS : di + 1]
            pc = [cser[d] for d in prior_days if d in cser]
            own_vol = float(np.std(np.diff(np.log(pc)))) if len(pc) >= VOL_DAYS // 2 else None
            adv = np.mean([dvols[sym][d] for d in prior_days if d in dvols[sym]])
            size = float(np.log(adv)) if adv and adv > 0 else None
            # targets
            y_abs30 = abs(fwd30[sym].get(day)) if fwd30[sym].get(day) is not None else None
            y_ret30 = fwd30[sym].get(day)
            rows.append(
                {
                    "symbol": sym,
                    "date": day,
                    "year_month": day[:7],
                    **feats,
                    "own_vol": own_vol,
                    "size": size,
                    "y_absret_30m": y_abs30,
                    "y_ret_30m": y_ret30,
                }
            )
    panel = pl.DataFrame(rows, infer_schema_length=None)
    out = f"{OUT_DIR}/news_panel_emb{EMBARGO_MIN}.parquet"
    panel.write_parquet(out)
    print(
        f"WROTE {out}: {panel.height} obs, {panel['symbol'].n_unique() if panel.height else 0} names, "
        f"{panel['date'].n_unique() if panel.height else 0} days",
        flush=True,
    )


if __name__ == "__main__":
    build()
