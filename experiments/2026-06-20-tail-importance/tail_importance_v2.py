"""HARNESS TAIL-FEATURE-IMPORTANCE (v2, direct store read) — where does the model lean AT THE PROFITABLE TAIL?

The harness demo showed the edge is TAIL-CONCENTRATED (precision/$/trade rise at tight percentile cuts). This
asks: among the model's TOP-PERCENTILE predictions (the profitable long tail), WHICH feature CLASSES does it
lean on? That data-grounds the next engineered-feature direction (and swing_dc's roughness/path-structure WIN
already says structure-of-the-path is a validated vein — this checks what else the tail leans on).

v2 reads the trusted store partitions DIRECTLY per-date (group=*/v=*/source=backfill/date=*), bypassing
build_intraday_panel (which OOMs/hangs at multi-group scope). Per date: read each group's features at the
sampled entry minute (>=09:40 ET), join, attach forward-30m cross-sectional EXCESS label from raw bars.
Walk-forward GBM → global gain-importance by class + ⭐ tail-vs-rest mean|SHAP| by class.

READ-ONLY. Writes tail_importance_v2.csv + console. Research-only.
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

OUT_DIR = "/app/experiments/2026-06-20-tail-importance"
STORE = "/store"
ENTRY_ET = 9 * 60 + 40
FWD_MIN = 30
N_DATES = int(os.environ.get("N_DATES", "40"))
UNIVERSE_TOP = int(os.environ.get("UNIVERSE_TOP", "250"))
TAIL_FRAC = 0.10
GROUPS = os.environ.get(
    "GROUPS",
    "volatility,realized_range,ohlc_vol,momentum,momentum_run,momentum_consistency,runner_state,efficiency,"
    "trend_quality,swing,candlestick,price_returns,return_dynamics,return_dispersion,distribution,"
    "microstructure_burst,trade_flow,signed_trade_ratio,trade_freq_z,liquidity,liquidity_rank,market_beta,"
    "overnight_intraday_split,gap_fill_state,price_volume,clean_momentum,prior_day,range_expansion,draw_range",
).split(",")


def group_vdir(g: str) -> str | None:
    cand = sorted(glob.glob(f"{STORE}/group={g}/v=*"))
    return cand[-1] if cand else None


def entry_ts(day: str) -> dt.datetime:
    d = dt.date.fromisoformat(day)
    return dt.datetime(
        d.year, d.month, d.day, ENTRY_ET // 60, ENTRY_ET % 60, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(dt.timezone.utc)


def liquid(day: str, n: int) -> set[str]:
    lazy = pl.scan_parquet(f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet", hive_partitioning=True).select(
        ["symbol", "ts", "close", "volume"]
    )
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    m = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    dv = (
        lazy.filter((m >= 9 * 60 + 30) & (m < 16 * 60))
        .group_by("symbol")
        .agg((pl.col("close") * pl.col("volume")).sum().alias("dv"))
        .sort("dv", descending=True)
        .head(n)
        .collect()
    )
    return set(dv["symbol"].to_list())


def build_date(day: str, vdirs: dict[str, str], f2class: dict, allcols: list[str]) -> pl.DataFrame:
    et = entry_ts(day)
    uni = liquid(day, UNIVERSE_TOP)
    feat = None
    for g, vd in vdirs.items():
        p = glob.glob(f"{vd}/source=backfill/date={day}/*.parquet")
        if not p:
            continue
        df = pl.read_parquet(p[0])
        if "minute" not in df.columns:
            continue
        df = (
            df.filter(pl.col("symbol").is_in(uni) & (pl.col("minute") >= et))
            .sort(["symbol", "minute"])
            .group_by("symbol")
            .first()
        )
        keep = ["symbol"] + [c for c in df.columns if c not in ("symbol", "minute")]
        df = df.select(keep)
        feat = df if feat is None else feat.join(df, on="symbol", how="outer_coalesce")
    if feat is None or feat.height == 0:
        return pl.DataFrame()
    # forward 30m excess label from raw bars
    px = (
        pl.scan_parquet(f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet", hive_partitioning=True)
        .select(["symbol", "ts", "close"])
        .collect()
        .sort(["symbol", "ts"])
    )
    e0 = px.filter(pl.col("ts") >= et).group_by("symbol").first().rename({"close": "c0"})
    eF = (
        px.filter(pl.col("ts") >= et + dt.timedelta(minutes=FWD_MIN))
        .group_by("symbol")
        .first()
        .rename({"close": "cF"})
    )
    lab = (
        e0.select(["symbol", "c0"])
        .join(eF.select(["symbol", "cF"]), on="symbol")
        .with_columns((pl.col("cF") / pl.col("c0") - 1.0).alias("_r"))
    )
    lab = lab.with_columns((pl.col("_r") - pl.col("_r").median()).alias("y")).select(["symbol", "y"])
    out = feat.join(lab, on="symbol", how="inner")
    return out.with_columns(pl.lit(day).alias("date"))


def main() -> None:
    import lightgbm as lgb

    vdirs = {g: group_vdir(g) for g in GROUPS if group_vdir(g)}
    days = sorted(
        os.path.basename(p).replace("date=", "")
        for p in glob.glob(f"{vdirs['volatility']}/source=backfill/date=*")
    )[-N_DATES:]
    print(f"tail-importance v2: {len(days)} dates {days[0]}..{days[-1]}, {len(vdirs)} groups", flush=True)
    # map feature -> class from one date's columns
    f2class: dict[str, str] = {}
    for g, vd in vdirs.items():
        p = glob.glob(f"{vd}/source=backfill/date={days[-1]}/*.parquet")
        if p:
            for c in pl.read_parquet(p[0]).columns:
                if c not in ("symbol", "minute"):
                    f2class[c] = g
    frames = []
    for i, day in enumerate(days):
        d = build_date(day, vdirs, f2class, list(f2class))
        if d.height:
            frames.append(d)
        if (i + 1) % 5 == 0:
            print(f"  built {i+1}/{len(days)} ({sum(f.height for f in frames)} rows)", flush=True)
    panel = pl.concat(frames, how="diagonal_relaxed")
    feat_cols = [c for c in panel.columns if c in f2class]
    panel = panel.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in feat_cols])
    print(f"panel: {panel.height} rows, {panel['date'].n_unique()} days, {len(feat_cols)} feats", flush=True)

    panel = panel.sort("date")
    days_p = sorted(panel["date"].unique().to_list())
    bounds = np.linspace(0, len(days_p), 5).astype(int)
    gain_c, tail_c, rest_c = defaultdict(float), defaultdict(float), defaultdict(float)
    tail_ex, rest_ex = [], []
    for k in range(3):
        trd = set(days_p[: bounds[k + 1]])
        ted = set(days_p[bounds[k + 1] : bounds[k + 2]])
        # keep feature NaNs (LightGBM handles missing natively) — only require the LABEL non-null, so a name
        # missing a few groups' features isn't dropped (the inner-join+drop_nulls collapsed the panel).
        tr = (
            panel.filter(pl.col("date").is_in(trd))
            .select([*feat_cols, "y"])
            .filter(pl.col("y").is_not_null())
        )
        te = (
            panel.filter(pl.col("date").is_in(ted))
            .select([*feat_cols, "y"])
            .filter(pl.col("y").is_not_null())
        )
        if tr.height < 600 or te.height < 300:
            continue
        Xtr, ytr = tr.select(feat_cols).to_numpy(), tr["y"].to_numpy()
        Xte, yte = te.select(feat_cols).to_numpy(), te["y"].to_numpy()
        bst = lgb.train(
            {
                "objective": "regression",
                "num_leaves": 31,
                "learning_rate": 0.05,
                "verbose": -1,
                "min_data_in_leaf": 80,
            },
            lgb.Dataset(Xtr, ytr, feature_name=feat_cols),
            num_boost_round=200,
        )
        for f, gn in zip(feat_cols, bst.feature_importance("gain")):
            gain_c[f2class[f]] += float(gn)
        contrib = bst.predict(Xte, pred_contrib=True)[:, : len(feat_cols)]
        pred = bst.predict(Xte)
        nt = max(1, int(len(pred) * TAIL_FRAC))
        ti, ri = np.argsort(pred)[-nt:], np.argsort(pred)[:-nt]
        for j, f in enumerate(feat_cols):
            tail_c[f2class[f]] += float(np.abs(contrib[ti, j]).mean())
            rest_c[f2class[f]] += float(np.abs(contrib[ri, j]).mean())
        tail_ex.append(float(yte[ti].mean()))
        rest_ex.append(float(yte[ri].mean()))
        print(
            f"  fold {k}: tr={tr.height} te={te.height} tail-excess={np.mean(yte[ti])*1e4:+.1f}bps",
            flush=True,
        )

    tg = sum(gain_c.values()) or 1.0
    rows = [
        {
            "feature_class": c,
            "global_gain_pct": 100 * gain_c[c] / tg,
            "tail_abs_shap": tail_c[c],
            "rest_abs_shap": rest_c[c],
            "tail_lean_ratio": (tail_c[c] / rest_c[c]) if rest_c[c] > 1e-12 else float("nan"),
        }
        for c in gain_c
    ]
    res = pl.DataFrame(rows)
    res.write_csv(f"{OUT_DIR}/tail_importance_v2.csv")
    print(
        f"\n=== TAIL profitability: tail excess {np.mean(tail_ex)*1e4:+.1f}bps vs rest {np.mean(rest_ex)*1e4:+.1f}bps ==="
    )
    print(
        "\n=== ⭐ FEATURE-CLASS LEAN AT THE PROFITABLE TAIL (tail_lean_ratio>1 = leans harder at the tail) ==="
    )
    with pl.Config(tbl_rows=40, fmt_str_lengths=24):
        print(
            res.sort("tail_lean_ratio", descending=True, nulls_last=True).select(
                ["feature_class", "global_gain_pct", "tail_lean_ratio", "tail_abs_shap"]
            )
        )


if __name__ == "__main__":
    main()
