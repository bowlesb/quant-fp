"""Pull split corporate actions for the universe from corporate_actions_pit. Writes data/splits.parquet."""
from __future__ import annotations

import os

import polars as pl
import psycopg

BASE = "experiments/2026-06-16-w12-net-issuance"


def main() -> None:
    syms = pl.read_parquet(f"{BASE}/data/liquid_universe.parquet")["symbol"].to_list()
    conn = psycopg.connect(
        host=os.environ["DB_HOST"], port=os.environ["DB_PORT"], dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT symbol, ex_date, split_ratio FROM corporate_actions_pit "
        "WHERE action_type='split' AND symbol = ANY(%s) ORDER BY symbol, ex_date",
        (syms,),
    )
    rows = cur.fetchall()
    df = pl.DataFrame(rows, schema=["symbol", "ex_date", "split_ratio"], orient="row").with_columns(
        pl.col("split_ratio").cast(pl.Float64)
    )
    df.write_parquet(f"{BASE}/data/splits.parquet")
    print(f"Wrote splits.parquet: {df.shape}")


if __name__ == "__main__":
    main()
