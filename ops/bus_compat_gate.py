#!/usr/bin/env python3
"""Pre-deploy compatibility gate — run BEFORE relaunching ``fc`` on a new feature set.

For every EXPECTED-live strategy, assert its published ``(name, version)`` contract resolves in the
CANDIDATE schema (the new feature set's). GREEN (exit 0) clears an ``fc``-only deploy: feature additions are
non-breaking and the strategies stay untouched. RED (exit 2) blocks the deploy and names the exact fault
(a missing/renamed or version-changed consumed feature). A strategy that hasn't published its contract
fails the gate CLOSED (exit 3) — never green-by-omission (B3). See docs/BUS_FEATURE_ACCESS.md §2.6.

The candidate schema is the one the build's registry produces (``default_schema()`` inside the candidate
image). Contracts are read LIVE from ``strategy:features:<name>`` (what is ACTUALLY running). Usage::

    python -m ops.bus_compat_gate --expect smoke,reversion,overnight_beta
    python -m ops.bus_compat_gate --expect smoke,reversion --redis redis://redis:6379/0
"""
from __future__ import annotations

import argparse
import os
import sys

import redis

from quantlib.bus.compat import (
    FeatureReq,
    IncompatibleSchema,
    MissingContract,
    contract_from_json,
    contract_key,
    run_gate,
)
from quantlib.bus.publisher import DEFAULT_REDIS_URL
from quantlib.bus.schema import BusSchema, default_schema


def load_contracts(redis_client: redis.Redis, strategies: list[str]) -> dict[str, list[FeatureReq]]:
    """Read each strategy's published contract from ``strategy:features:<name>`` (absent -> not in the
    returned map, so the gate fails closed on it)."""
    contracts: dict[str, list[FeatureReq]] = {}
    for strategy in strategies:
        raw = redis_client.get(contract_key(strategy))
        if raw is not None:
            contracts[strategy] = list(contract_from_json(bytes(raw).decode("utf-8")))
    return contracts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-deploy bus feature-compatibility gate")
    parser.add_argument("--expect", required=True, help="comma-separated expected-live strategy names")
    parser.add_argument("--redis", default=os.environ.get("BUS_REDIS_URL", DEFAULT_REDIS_URL))
    args = parser.parse_args(argv)

    expected = [name.strip() for name in args.expect.split(",") if name.strip()]
    candidate: BusSchema = default_schema()
    redis_client = redis.Redis.from_url(args.redis)
    contracts = load_contracts(redis_client, expected)

    try:
        run_gate(candidate, contracts, expected)
    except MissingContract as exc:
        print(f"RED (fail-closed): no published contract for {exc.strategies}", file=sys.stderr)
        return 3
    except IncompatibleSchema as exc:
        print(
            f"RED: strategy '{exc.strategy}' incompatible with candidate {candidate.fingerprint:#018x}: "
            f"missing={exc.missing} version_changed={exc.version_changed}",
            file=sys.stderr,
        )
        return 2
    print(
        f"GREEN: all {len(expected)} strategies compatible with candidate {candidate.fingerprint:#018x} "
        f"({candidate.n_features} features) — fc may deploy"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
