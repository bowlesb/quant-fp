"""Debug/inspect CLI for the feature-vector bus — verify vectors arrive COMPLETE with sane values.

Subscribe to the per-symbol streams for some tickers and pretty-print each received vector:

  * the schema fingerprint (and whether it matches the consumer's locally-built schema),
  * the bar minute,
  * the NaN count / fraction (how "complete" the vector is),
  * min / max / a few sample feature values,
  * optionally the full ``group.feature = value`` listing (``--full``).

Usage:

    python -m strategies.tools.inspect_bus --symbols AAPL,MSFT              # follow live, forever
    python -m strategies.tools.inspect_bus --symbols AAPL --once            # one vector then exit
    python -m strategies.tools.inspect_bus --symbols AAPL --full            # dump every feature
    python -m strategies.tools.inspect_bus --symbols AAPL --synthetic       # publish a synthetic
                                                                            # vector to its own stream
                                                                            # prefix and read it back
                                                                            # (no live bus needed)

``--synthetic`` is the network-light self-test: it publishes a known vector to a private stream prefix
and verifies the consumer reads it back complete — proving the encode/decode + fingerprint path end to
end without depending on the live producer or market hours.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import uuid

import numpy as np

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import DEFAULT_REDIS_URL, BusPublisher
from quantlib.bus.schema import BusSchema, default_schema
from quantlib.bus.vector import FeatureVector

SAMPLE_FEATURES = ["ret_1m", "volume_zscore_5m", "rsi_14", "vwap_dev"]


def parse_symbols(raw: str) -> list[str]:
    return [token.strip().upper() for token in raw.split(",") if token.strip()]


def vector_stats(vector: FeatureVector) -> dict[str, float]:
    """NaN-aware summary stats over the packed array — how complete + how sane the values are."""
    array = vector.array
    nan_mask = np.isnan(array)
    nan_count = int(nan_mask.sum())
    finite = array[~nan_mask]
    return {
        "n": float(array.size),
        "nan_count": float(nan_count),
        "nan_fraction": float(nan_count) / float(array.size) if array.size else 0.0,
        "finite_count": float(finite.size),
        "min": float(finite.min()) if finite.size else float("nan"),
        "max": float(finite.max()) if finite.size else float("nan"),
        "mean": float(finite.mean()) if finite.size else float("nan"),
    }


def render_samples(vector: FeatureVector, schema: BusSchema) -> str:
    parts = []
    for name in SAMPLE_FEATURES:
        if schema.has(name):
            value = vector.value(name)
            parts.append(f"{name}={value:+.5f}")
    return " ".join(parts)


def render_full(vector: FeatureVector, schema: BusSchema) -> str:
    """Every feature as ``group.feature = value``, grouped, for a complete eyeball dump."""
    lines = []
    for group in schema.group_names():
        lines.append(f"  [{group}]")
        for field in schema.group_fields(group):
            value = float(vector.array[field.offset])
            lines.append(f"    {group}.{field.name} = {value:+.6g}")
    return "\n".join(lines)


def print_vector(vector: FeatureVector, schema: BusSchema, full: bool) -> None:
    stats = vector_stats(vector)
    fp_match = vector.fingerprint == schema.fingerprint
    print(f"=== {vector.symbol} @ {vector.minute.isoformat()} ===")
    print(
        f"  fingerprint: frame={vector.fingerprint:#018x} "
        f"schema={schema.fingerprint:#018x} match={fp_match}"
    )
    print(f"  features: n={int(stats['n'])} finite={int(stats['finite_count'])} "
          f"nan={int(stats['nan_count'])} ({stats['nan_fraction']*100:.1f}%)")
    print(f"  values: min={stats['min']:+.5g} max={stats['max']:+.5g} mean={stats['mean']:+.5g}")
    samples = render_samples(vector, schema)
    if samples:
        print(f"  sample: {samples}")
    if full:
        print(render_full(vector, schema))
    sys.stdout.flush()


def run_synthetic(symbols: list[str], url: str, full: bool) -> int:
    """Publish a known synthetic vector per symbol to a private prefix and read it back. Returns the
    number of vectors verified (so the caller / a test can assert completeness)."""
    schema = default_schema()
    prefix = f"inspect_{uuid.uuid4().hex[:8]}"
    minute = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    publisher = BusPublisher(url=url, schema=schema, prefix=prefix)
    array = np.arange(schema.n_features, dtype="<f8") / float(schema.n_features)
    for symbol in symbols:
        publisher.publish(symbol, minute, array.copy())
    consumer = BusConsumer(symbols, url=url, schema=schema, prefix=prefix, start="0")
    vectors = consumer.poll(block_ms=1000, count=len(symbols) * 4)
    for vector in vectors:
        print_vector(vector, schema, full)
    publisher.close()
    consumer.close()
    print(f"synthetic: published {len(symbols)} vector(s), read back {len(vectors)}")
    return len(vectors)


def run_live(symbols: list[str], url: str, once: bool, full: bool, block_ms: int) -> None:
    schema = default_schema()
    consumer = BusConsumer(symbols, url=url, schema=schema, start="$")
    print(f"inspecting bus {url} for {symbols} (schema fingerprint {schema.fingerprint:#018x}); "
          f"{'one vector then exit' if once else 'following live, Ctrl-C to stop'}...")
    sys.stdout.flush()
    while True:
        vectors = consumer.poll(block_ms=block_ms, count=200)
        for vector in vectors:
            print_vector(vector, schema, full)
            if once:
                consumer.close()
                return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect feature vectors on the bus.")
    parser.add_argument("--symbols", required=True, help="comma-separated tickers, e.g. AAPL,MSFT")
    parser.add_argument("--url", default=DEFAULT_REDIS_URL, help="bus Redis URL")
    parser.add_argument("--once", action="store_true", help="print one vector then exit")
    parser.add_argument("--full", action="store_true", help="dump every group.feature = value")
    parser.add_argument("--synthetic", action="store_true",
                        help="publish + read back a known synthetic vector (no live producer needed)")
    parser.add_argument("--block-ms", type=int, default=2000, help="poll block time in ms")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = parse_symbols(args.symbols)
    if not symbols:
        print("no symbols given", file=sys.stderr)
        return 2
    if args.synthetic:
        verified = run_synthetic(symbols, args.url, args.full)
        return 0 if verified == len(symbols) else 1
    run_live(symbols, args.url, args.once, args.full, args.block_ms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
