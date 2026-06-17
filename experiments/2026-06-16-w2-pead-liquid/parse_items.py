"""W2 step 1 — parse SEC item codes for every 8-K CIK and tag the item-2.02 (earnings) subset.

Pulls all 8-Ks (available_at >= 2025-12-15) from the filings table, then for each distinct CIK calls
the SEC submissions API and maps each DB accession_number -> its `items` string (exact match; the DB
already stores the dashed SEC accession form). Writes item_map.json:
  {accession_number: items_string}  and a summary with n_8k, n_matched, n_earnings(2.02).
Cached so the main analysis never re-hits SEC.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psycopg

OUTPUT_DIR = Path("/app/experiments/2026-06-16-w2-pead-liquid")
SEC_UA = "quant-fp research ben.bowles@gmail.com"
SEC_SLEEP = 0.12


def get_db_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def load_8k_rows() -> list[dict[str, Any]]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT accession_number, cik, symbol
        FROM filings
        WHERE form_type = '8-K' AND available_at >= '2025-12-15'
        """
    )
    rows = [{"accession_number": r[0], "cik": r[1], "symbol": r[2]} for r in cur.fetchall()]
    conn.close()
    return rows


def fetch_cik_items(cik: str) -> dict[str, str]:
    """Return {accession_number: items_string} for all filings of one CIK in the SEC recent block."""
    cik_padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    recent = data["filings"]["recent"]
    accessions = recent["accessionNumber"]
    items = recent["items"]
    return {accessions[i]: items[i] for i in range(len(accessions))}


def main() -> None:
    rows = load_8k_rows()
    ciks = sorted({r["cik"] for r in rows})
    print(f"8-Ks: {len(rows)} rows, {len(ciks)} distinct CIKs")

    item_map: dict[str, str] = {}
    n_fetched = 0
    n_failed = 0
    for cik in ciks:
        try:
            cik_items = fetch_cik_items(cik)
            item_map.update(cik_items)
            n_fetched += 1
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, TimeoutError) as exc:
            n_failed += 1
            print(f"  WARN cik {cik} failed: {exc}")
        time.sleep(SEC_SLEEP)
        if n_fetched % 250 == 0:
            print(f"  fetched {n_fetched}/{len(ciks)} CIKs")

    # Tag the 8-K rows
    n_matched = 0
    n_earnings = 0
    earnings_accessions: list[str] = []
    for row in rows:
        acc = row["accession_number"]
        if acc in item_map:
            n_matched += 1
            item_list = [x.strip() for x in item_map[acc].split(",") if x.strip()]
            if "2.02" in item_list:
                n_earnings += 1
                earnings_accessions.append(acc)

    summary = {
        "n_8k_rows": len(rows),
        "n_distinct_ciks": len(ciks),
        "n_ciks_fetched": n_fetched,
        "n_ciks_failed": n_failed,
        "n_matched_accessions": n_matched,
        "n_earnings_2_02": n_earnings,
        "match_fraction": round(n_matched / len(rows), 4) if rows else 0.0,
    }
    print("SUMMARY:", json.dumps(summary, indent=2))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / "item_map.json", "w") as json_file:
        json.dump({"item_map": item_map, "summary": summary, "earnings_accessions": earnings_accessions}, json_file)
    print(f"Wrote item_map.json ({len(item_map)} accessions mapped)")


if __name__ == "__main__":
    main()
