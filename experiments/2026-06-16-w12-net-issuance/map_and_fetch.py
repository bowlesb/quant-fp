"""Map the liquid universe to CIKs via SEC company_tickers.json, then fetch XBRL companyfacts.

Step 1: GET sec.gov/files/company_tickers.json (authoritative ticker->CIK), match the 500 liquid symbols
        (dot/dash/strip variants). Writes data/symbol_cik.parquet. ETFs (XL*) have no CIK and drop out.
Step 2: For each CIK, GET data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json (cached to cache/), extract a
        point-in-time shares-outstanding series from preferred tags. Each point carries end/filed/val;
        `filed` is the look-ahead-safe availability date. Writes data/shares_long.parquet.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import polars as pl
import requests

UA = "quant-fp research ben.bowles@gmail.com"
BASE = Path("experiments/2026-06-16-w12-net-issuance")
CACHE = BASE / "cache"
UNIVERSE = BASE / "data" / "liquid_universe.parquet"
SYMBOL_CIK = BASE / "data" / "symbol_cik.parquet"
SHARES_OUT = BASE / "data" / "shares_long.parquet"

SHARE_TAGS = [
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesIssued"),
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
]


def build_symbol_cik() -> pl.DataFrame:
    resp = requests.get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    tmap = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in data.values()}
    syms = pl.read_parquet(UNIVERSE)["symbol"].to_list()
    matched: dict[str, int] = {}
    for symbol in syms:
        for cand in [symbol, symbol.replace(".", "-"), symbol.replace(".", "")]:
            if cand in tmap:
                matched[symbol] = tmap[cand]
                break
    out = pl.DataFrame({"symbol": list(matched.keys()), "cik": [f"{c:010d}" for c in matched.values()]})
    out.write_parquet(SYMBOL_CIK)
    miss = sorted(set(syms) - set(matched))
    print(f"symbol->CIK: matched {out.shape[0]}/{len(syms)}; unmapped (ETFs) {len(miss)}: {miss}")
    return out


def fetch_one(cik: str) -> dict | None:
    cache_path = CACHE / f"CIK{cik}.json"
    if cache_path.exists():
        text = cache_path.read_text()
        return None if text.strip() in ("", "null") else json.loads(text)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    time.sleep(0.12)
    if resp.status_code == 404:
        cache_path.write_text("null")
        return None
    resp.raise_for_status()
    cache_path.write_text(resp.text)
    return resp.json()


def extract_points(facts: dict, symbol: str, cik: str) -> list[dict]:
    rows: list[dict] = []
    all_facts = facts.get("facts", {})
    for taxonomy, tag in SHARE_TAGS:
        node = all_facts.get(taxonomy, {}).get(tag)
        if node is None:
            continue
        for _unit, points in node.get("units", {}).items():
            for point in points:
                end = point.get("end")
                filed = point.get("filed")
                val = point.get("val")
                if end is None or filed is None or val is None:
                    continue
                rows.append(
                    {"symbol": symbol, "cik": cik, "tag": f"{taxonomy}:{tag}", "end": end, "filed": filed, "val": float(val)}
                )
    return rows


def fetch_shares(sym_cik: pl.DataFrame) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    pairs = list(zip(sym_cik["symbol"].to_list(), sym_cik["cik"].to_list()))
    all_rows: list[dict] = []
    n_with = 0
    for i, (symbol, cik) in enumerate(pairs):
        facts = fetch_one(cik)
        if facts is None:
            continue
        rows = extract_points(facts, symbol, cik)
        if rows:
            n_with += 1
            all_rows.extend(rows)
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(pairs)} fetched, {n_with} with shares", flush=True)
    df = pl.DataFrame(all_rows).with_columns(pl.col("end").str.to_date(), pl.col("filed").str.to_date())
    df.write_parquet(SHARES_OUT)
    print(f"Wrote {SHARES_OUT}: {df.shape}; symbols_with_shares={df['symbol'].n_unique()}", flush=True)


def main() -> None:
    sym_cik = build_symbol_cik()
    fetch_shares(sym_cik)


if __name__ == "__main__":
    main()
