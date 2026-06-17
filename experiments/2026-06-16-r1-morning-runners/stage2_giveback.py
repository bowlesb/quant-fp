"""R1 Stage 2a — runner give-back prediction from the first-30-min bar path (pre-registered).

For each CORE runner-day, build first-30-min (09:30-10:00 ET) path features and predict the EOD
give-back + fwd-5d. Walk-forward by date, shuffle canary, lightGBM. See stage2_giveback_prereg.md.
"""

import glob
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import polars as pl
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.metrics import r2_score, roc_auc_score

BARS = "/store/raw/bars"
OUT = "/app/experiments/2026-06-16-r1-morning-runners"
OPEN_M, F30_M = 570, 600  # 09:30, 10:00 ET in ET-minutes-of-day


def f30_path_features(symbol: str, date: str, prev_close: float) -> dict[str, float] | None:
    path = f"{BARS}/symbol={symbol}/date={date}/data.parquet"
    if not os.path.exists(path):
        return None
    df = pl.read_parquet(path, columns=["ts", "open", "high", "low", "close", "volume"])
    if df.height == 0:
        return None
    et = df["ts"].dt.convert_time_zone("America/New_York")
    etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    df = df.with_columns(etm.alias("etm")).filter((pl.col("etm") >= OPEN_M) & (pl.col("etm") < F30_M)).sort("etm")
    if df.height < 5:
        return None
    high = df["high"].to_numpy()
    close = df["close"].to_numpy()
    vol = df["volume"].to_numpy()
    opens = df["open"].to_numpy()
    rets = close[1:] / close[:-1] - 1.0
    run_high = np.maximum.accumulate(high)
    peak_idx = int(np.argmax(high))
    f30_high = float(high.max())
    dollar = float((close * vol).sum())
    n = len(close)
    first5_vol = float(vol[: min(5, n)].sum())
    last5_vol = float(vol[max(0, n - 5) :].sum())
    return {
        "gap_open": float(opens[0] / prev_close - 1.0),
        "early_move": float(f30_high / prev_close - 1.0),
        "log_prev_close": float(np.log(prev_close)),
        "log_f30_dollar": float(np.log1p(dollar)),
        "peak_minute": float(peak_idx),
        "peak_frac": float(peak_idx / max(1, n - 1)),
        "n_green": float((rets > 0).sum()),
        "n_red": float((rets < 0).sum()),
        "max_1m_ret": float(rets.max()) if rets.size else 0.0,
        "ret_vol": float(rets.std()) if rets.size else 0.0,
        "intra_pullback": float(close[-1] / f30_high - 1.0),
        "runup_slope": float((run_high[-1] / run_high[0] - 1.0) / max(1, n)),
        "vol_accel": float(last5_vol / first5_vol) if first5_vol > 0 else 0.0,
    }


def _build_one(args: tuple[str, str, float, float, float]) -> dict[str, float] | None:
    symbol, date, prev_close, f30_high, label_giveback = args
    feats = f30_path_features(symbol, date, prev_close)
    if feats is None:
        return None
    feats["__giveback"] = label_giveback
    return feats


def main() -> None:
    ev = pl.read_parquet(f"{OUT}/runner_events.parquet")
    core = ev.filter((pl.col("early_move") >= 0.5) & (pl.col("vol_surge") >= 3)).sort("date")
    core = core.with_columns((pl.col("rth_close") / pl.col("f30_high") - 1.0).alias("giveback"))
    core = core.filter(pl.col("giveback").is_finite())
    print(f"CORE runner-days: {core.height}", flush=True)

    args = [
        (row["symbol"], row["date"], row["prev_close"], row["f30_high"], row["giveback"])
        for row in core.iter_rows(named=True)
    ]
    rows: list[dict[str, float]] = []
    dates: list[str] = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for arg, result in zip(args, ex.map(_build_one, args, chunksize=16)):
            if result is not None:
                rows.append(result)
                dates.append(arg[1])
    print(f"built features for {len(rows)} runner-days", flush=True)

    feat_df = pl.DataFrame(rows)
    feat_cols = [column for column in feat_df.columns if not column.startswith("__")]
    X = feat_df.select(feat_cols).to_numpy()
    y = feat_df["__giveback"].to_numpy()
    date_arr = np.array(dates)

    order = np.argsort(date_arr)
    X, y, date_arr = X[order], y[order], date_arr[order]
    split = int(len(X) * 0.70)
    cut_date = date_arr[split]
    train = date_arr < cut_date
    test = ~train
    print(f"walk-forward split at {cut_date}: train {train.sum()} / test {test.sum()}", flush=True)

    reg = LGBMRegressor(n_estimators=200, max_depth=3, learning_rate=0.03, num_leaves=7, verbose=-1)
    reg.fit(X[train], y[train])
    pred = reg.predict(X[test])
    oos_r2 = r2_score(y[test], pred)

    rng = np.random.default_rng(0)
    y_shuf = y[train].copy()
    rng.shuffle(y_shuf)
    reg_c = LGBMRegressor(n_estimators=200, max_depth=3, learning_rate=0.03, num_leaves=7, verbose=-1)
    reg_c.fit(X[train], y_shuf)
    canary_r2 = r2_score(y[test], reg_c.predict(X[test]))

    hard = (y <= -0.10).astype(int)
    clf = LGBMClassifier(n_estimators=200, max_depth=3, learning_rate=0.03, num_leaves=7, verbose=-1)
    clf.fit(X[train], hard[train])
    proba = clf.predict_proba(X[test])[:, 1]
    oos_auc = roc_auc_score(hard[test], proba) if len(set(hard[test])) > 1 else float("nan")
    base_rate = float(hard[test].mean())

    print("\n=== R1 Stage 2a give-back model (walk-forward OOS) ===")
    print(f"giveback OOS R2: {oos_r2:+.4f}  | shuffle-canary R2: {canary_r2:+.4f}  | "
          f"skill-over-canary: {oos_r2 - canary_r2:+.4f}")
    print(f"hard-fade (<=-10%) OOS AUC: {oos_auc:.3f}  | base rate: {base_rate:.2f}")
    print("\nfeature importances (gain):")
    imp = sorted(zip(feat_cols, reg.feature_importances_), key=lambda kv: -kv[1])
    for name, gain in imp:
        print(f"  {name:18} {gain}")

    verdict = "FORECASTABLE (beats canary)" if oos_r2 - canary_r2 > 0.01 else "NOT forecastable beyond canary"
    print(f"\nVERDICT: {verdict}")


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
