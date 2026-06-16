"""Dry-run universe-count helper for the nightly clean-relaunch (``ops/nightly_relaunch.sh``).

``reseed-count`` pulls Alpaca's active US-equity assets and applies the SAME screen ``seed_universe`` uses
(primary exchanges + ``quantlib.universe.is_etf_like`` ETF/fund filter, capped at ``UNIVERSE_MAX_SYMBOLS``),
then prints the in-universe count WITHOUT touching the DB. It is the dry-run proof of the clean filtered set;
the real write is ``python -m quantlib.features.seed_universe <date>``, which this mirrors EXACTLY by reusing
that module's own ``fetch_assets`` / ``select_universe`` (so the dry count == what the seed would write).

The ``docker inspect`` -> ``docker run`` reproduction lives in the shell wrapper, on the host where the docker
socket and CLI are; this Python piece only needs alpaca-py + quantlib, so it runs inside the fp-dev image::

    docker run --rm --env-file .env --network quant_default -v "$PWD":/app -w /app fp-dev \\
        python -m ops.nightly_relaunch reseed-count 2026-06-16
"""
from __future__ import annotations

import argparse

from quantlib.features.seed_universe import KEEP_EXCHANGES, MAX_SYMBOLS, fetch_assets, select_universe


def reseed_count(trade_date: str) -> int:
    """The number of in-universe symbols ``seed_universe`` WOULD write for ``trade_date`` — computed
    in-memory from Alpaca's live asset list via the SAME ``select_universe`` screen, with NO DB write."""
    assets = fetch_assets()
    total = len(assets)
    symbols = select_universe(assets)
    print(f"[reseed-count] pulled {total} active US-equity assets from Alpaca", flush=True)
    print(
        f"[reseed-count] in-universe after screen "
        f"(exchanges={sorted(KEEP_EXCHANGES)}, is_etf_like filter, cap={MAX_SYMBOLS}): "
        f"{len(symbols)} common-stock symbols",
        flush=True,
    )
    print(f"[reseed-count] sample: {symbols[:8]}", flush=True)
    return len(symbols)


def main() -> None:
    parser = argparse.ArgumentParser(description="Nightly clean-relaunch dry-run universe count")
    sub = parser.add_subparsers(dest="cmd", required=True)
    count = sub.add_parser("reseed-count", help="dry-run filtered universe count (no DB write)")
    count.add_argument("trade_date")
    args = parser.parse_args()
    reseed_count(args.trade_date)


if __name__ == "__main__":
    main()
