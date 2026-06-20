from __future__ import annotations
import datetime as dt, glob, os
from zoneinfo import ZoneInfo
import numpy as np, polars as pl
from quantlib.data.realized_cost import realized_half_spread_bps
STORE="/store"; ENTRY_ET=9*60+40
def gv(g):
    c=sorted(glob.glob(f"{STORE}/group={g}/v=*")); return c[-1] if c else None
def ets(day):
    d=dt.date.fromisoformat(day); return dt.datetime(d.year,d.month,d.day,ENTRY_ET//60,ENTRY_ET%60,tzinfo=ZoneInfo("America/New_York")).astimezone(dt.timezone.utc)
def cov(vd,m=500):
    days=sorted(os.path.basename(p).replace("date=","") for p in glob.glob(f"{vd}/source=backfill/date=*"))
    return [d for d in days if (g:=glob.glob(f"{vd}/source=backfill/date={d}/*.parquet")) and pl.read_parquet(g[0],columns=["symbol"])["symbol"].n_unique()>=m]
def liq(day,n):
    lz=pl.scan_parquet(f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet",hive_partitioning=True).select(["symbol","ts","close","volume"])
    e=pl.col("ts").dt.convert_time_zone("America/New_York"); mm=e.dt.hour().cast(pl.Int32)*60+e.dt.minute().cast(pl.Int32)
    return lz.filter((mm>=570)&(mm<960)).group_by("symbol").agg((pl.col("close")*pl.col("volume")).sum().alias("dv")).sort("dv",descending=True).head(n).collect()["symbol"].to_list()
days=cov(gv("volatility"))[-42:]
allhs=[]
for day in days[:10]:
    rc=realized_half_spread_bps(STORE,day,liq(day,200),ets(day))
    if rc.height: allhs.extend(rc["realized_half_spread_bps"].to_list())
a=np.array(allhs)
print(f"liquid-200 realized half-spread (bps) over 10 dates, n={len(a)}:")
print(f"  p10={np.percentile(a,10):.2f} p25={np.percentile(a,25):.2f} median={np.median(a):.2f} p75={np.percentile(a,75):.2f} p90={np.percentile(a,90):.2f}")
print(f"  frac < flat-3bps stub: {np.mean(a<3.0):.0%}  (these were OVERCHARGED by the stub)")
print(f"  frac > flat-3bps stub: {np.mean(a>3.0):.0%}  (UNDERCHARGED by the stub)")
