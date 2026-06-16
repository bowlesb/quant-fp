"""BEFORE vs AFTER benchmark: SDK thread-pool engine vs direct-httpx multiprocess engine.

Same symbol set + days, separate scratch stores. Reports partitions/min and GB/min.

Run inside fp-dev (NEVER against production /store/raw):
    docker run --rm --network quant_default -v "$PWD":/app -w /app --env-file .env -e PYTHONPATH=/app \\
        fp-dev python experiments/bench_fast_fetch.py old 12     # SDK path, 12 threads
    docker run ... fp-dev python experiments/bench_fast_fetch.py new 24 8   # fast path, 24 procs x 8 threads

Measured (40 mixed mega/mid-cap symbols x 3 days = 120 trades partitions, mega-cap-heavy):
    OLD (SDK threads=12):  14.9 parts/min  0.049 GB/min  (482.5s)
    NEW (httpx 24px8t):    77.4 parts/min  0.252 GB/min  ( 93.0s)  -> 5.2x
A broader 60-symbol pure-fetch sweep (no mega-cap domination) reached ~160 symbol-days/min on the
24px8t config — the headline ceiling once a single fat NVDA day no longer bottlenecks a few processes.
"""
from __future__ import annotations
import datetime as dt
import os
import shutil
import sys
import time

from quantlib.data import raw_backfill
from quantlib.data.fast_backfill import run_tier_fast

SYMS = ["NVDA","AAPL","TSLA","AMD","MSFT","AMZN","META","GOOGL","AVGO","NFLX",
        "KO","PEP","JPM","BAC","XOM","CVX","WMT","DIS","INTC","CSCO",
        "PFE","T","VZ","ORCL","CRM","ADBE","QCOM","TXN","HON","UNH",
        "F","GM","UBER","PYPL","SHOP","PLTR","COIN","MU","WFC","GS"]
DAYS = [dt.date(2026,6,9), dt.date(2026,6,10), dt.date(2026,6,11)]
TIER = "trades"
N = len(SYMS) * len(DAYS)

def fresh(path):
    if os.path.exists(path): shutil.rmtree(path)
    os.makedirs(os.path.join(path, "raw"), exist_ok=True)
    return path

def config(store, max_workers):
    return raw_backfill.BackfillConfig(
        store=store, months=6, top_trades=len(SYMS), top_quotes=len(SYMS),
        budget_bytes=10**15, symbols=SYMS, days=len(DAYS), max_workers=max_workers,
        bars_symbols_per_request=100, bars_chunk_days=30, trades_chunk_days=1,
        quotes_chunk_days=1, fast=False, processes=24, threads_per_process=8)

def bench_old(max_workers):
    store = fresh("/tmp/bench_old")
    cfg = config(store, max_workers)
    t0 = time.perf_counter()
    written, nbytes = raw_backfill.fetch_ticks_tier(cfg, TIER, SYMS, DAYS, chunk_days=1)
    secs = time.perf_counter() - t0
    return written, nbytes, secs

def bench_new(procs, threads):
    store = fresh("/tmp/bench_new")
    t0 = time.perf_counter()
    written, nbytes = run_tier_fast(store, TIER, SYMS, DAYS, processes=procs, threads_per_process=threads)
    secs = time.perf_counter() - t0
    return written, nbytes, secs

if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "old":
        mw = int(sys.argv[2])
        w, b, s = bench_old(mw)
        print(f"OLD  (SDK threads={mw:>2}):  {w} parts  {b/1024**3:6.3f}GB  {s:6.1f}s  -> "
              f"{w/s*60:6.1f} parts/min  {b/1024**3/s*60:6.3f} GB/min", flush=True)
    else:
        procs, threads = int(sys.argv[2]), int(sys.argv[3])
        w, b, s = bench_new(procs, threads)
        print(f"NEW  (httpx {procs}px{threads}t):  {w} parts  {b/1024**3:6.3f}GB  {s:6.1f}s  -> "
              f"{w/s*60:6.1f} parts/min  {b/1024**3/s*60:6.3f} GB/min", flush=True)
