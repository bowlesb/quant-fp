import os, glob
import numpy as np
import polars as pl

BARS = "/store/raw/bars"
RTH_LO, RTH_HI = 810, 1190        # 09:30-15:50 ET in UTC minutes
F30_HI = 840                       # first 30 min ends 10:00 ET
syms = [d.split('=')[1] for d in os.listdir(BARS) if d.startswith('symbol=')]
print(f"scanning {len(syms)} symbols ...", flush=True)

rows = []
for n, s in enumerate(syms):
    if n % 1000 == 0: print(f"  {n}/{len(syms)}", flush=True)
    files = sorted(glob.glob(f"{BARS}/symbol={s}/date=*/data.parquet"))
    if len(files) < 25: continue
    # build a daily summary per date: prev_close, first30 high/vol, RTH close, rth dollar-vol
    day_close = {}     # date -> rth close
    recs = []          # per date: (date, first30_high, first30_vol, rth_close, open_first, rth_dollar)
    for f in files:
        date = os.path.basename(os.path.dirname(f)).split('=')[1]
        try: df = pl.read_parquet(f)
        except Exception: continue
        if df.height == 0: continue
        df = df.with_columns((pl.col("ts").dt.hour().cast(pl.Int32)*60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("m"))
        rth = df.filter((pl.col("m")>=RTH_LO)&(pl.col("m")<RTH_HI))
        if rth.height == 0: continue
        f30 = rth.filter(pl.col("m")<F30_HI)
        if f30.height == 0: continue
        rth_s = rth.sort("m")
        recs.append((date,
                     float(f30["high"].max()),
                     float(f30["volume"].sum()),
                     float(rth_s["close"][-1]),
                     float(rth_s["open"][0]),
                     float((rth["close"]*rth["volume"]).sum())))
    if len(recs) < 25: continue
    rdf = pl.DataFrame(recs, schema=["date","f30_high","f30_vol","rth_close","rth_open","rth_dollar"], orient="row").sort("date")
    rdf = rdf.with_columns([
        pl.col("rth_close").shift(1).alias("prev_close"),
        pl.col("f30_vol").rolling_median(window_size=20, min_periods=10).shift(1).alias("med_f30_vol"),
        pl.col("rth_close").shift(-1).alias("c1"), pl.col("rth_close").shift(-3).alias("c3"), pl.col("rth_close").shift(-5).alias("c5"),
    ])
    rdf = rdf.with_columns([
        (pl.col("f30_high")/pl.col("prev_close")-1.0).alias("early_move"),
        (pl.col("rth_open")/pl.col("prev_close")-1.0).alias("open_gap"),
        (pl.col("f30_vol")/pl.col("med_f30_vol")).alias("vol_surge"),
    ]).filter((pl.col("prev_close")>=2.0)&(pl.col("prev_close")<=20.0)&pl.col("early_move").is_finite()&pl.col("vol_surge").is_finite())
    for r in rdf.iter_rows(named=True):
        if r["early_move"] >= 0.30 and r["vol_surge"] >= 2.0:
            rows.append({"symbol": s, **{k: r[k] for k in ("date","prev_close","early_move","open_gap","vol_surge","f30_high","rth_close","rth_dollar","c1","c3","c5")}})

ev = pl.DataFrame(rows)
print(f"\n=== RUNNER EVENTS (early_move>=0.30, surge>=2): {ev.height} rows, {ev['symbol'].n_unique()} symbols ===")
ev.write_parquet("experiments/2026-06-16-r1-morning-runners/runner_events.parquet")

def cell(emin, smin):
    c = ev.filter((pl.col("early_move")>=emin)&(pl.col("vol_surge")>=smin))
    return c.height, c["symbol"].n_unique()
print("\nCOUNTS (runner-days / unique syms):")
for em in (0.30,0.50,1.00,2.00):
    for sm in (2,3,5):
        h,u = cell(em,sm); print(f"  early>={em:.2f} surge>={sm}: {h:5d} days / {u:4d} syms")

core = ev.filter((pl.col("early_move")>=0.50)&(pl.col("vol_surge")>=3))
print(f"\n=== CORE set (early>=0.50, surge>=3): {core.height} days / {core['symbol'].n_unique()} syms ===")
if core.height:
    print("early_move pctiles:", [round(float(core['early_move'].quantile(q)),2) for q in (.1,.5,.9)])
    print("prev_close pctiles:", [round(float(core['prev_close'].quantile(q)),2) for q in (.1,.5,.9)])
    print("runner-day $vol pctiles:", [f"${float(core['rth_dollar'].quantile(q)):,.0f}" for q in (.1,.5,.9)])
    cvh = (core['rth_close']/core['f30_high']-1.0)
    print(f"INTRADAY: close vs first-30min-high: median {float(cvh.median())*100:.1f}% | frac closing >= the f30 high: {float((cvh>=0).mean())*100:.0f}% | frac fading >10% off high: {float((cvh< -0.10).mean())*100:.0f}%")
    for h,col in (("1d","c1"),("3d","c3"),("5d","c5")):
        r = (core[col]/core['rth_close']-1.0)
        r = r.filter(r.is_finite())
        print(f"MULTI-DAY fwd {h}: median {float(r.median())*100:+.1f}% | frac up {float((r>0).mean())*100:.0f}% (n={r.len()})")
    print("\nSAMPLE runners:")
    for r in core.sort('early_move', descending=True).head(15).iter_rows(named=True):
        print(f"  {r['symbol']:6} {r['date']} prev=${r['prev_close']:.2f} early=+{r['early_move']*100:.0f}% surge={r['vol_surge']:.0f}x $vol=${r['rth_dollar']:,.0f}")
