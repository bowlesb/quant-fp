"""Throughput benchmark for the decoupled bus decode (docs/BUS_FEATURE_ACCESS.md §3).

Proves no throughput regression vs the current exact-fingerprint dense-decode at bus scale, across three
paths the audit asked for:

  - WARM: the fingerprint's schema is already cached (the steady state) — must be within ~5% of `decode`.
  - COLD: first frame of a fingerprint, schema fetched from the (dict) backend once then cached.
  - FP-FLIP mid-stream: frames alternate between two fingerprints, so the cache is hit for both but the
    decode must dispatch per-fingerprint — the deploy-window shape (old + new fp coexisting briefly).

Run as a test (asserts the warm bound) or standalone (prints all three numbers):
  docker run --rm -v "$PWD":/app -w /app -e PYTHONPATH=/app fp-dev python -m pytest tests/bench_bus_decode.py -s -q
  docker run --rm -v "$PWD":/app -w /app -e PYTHONPATH=/app fp-dev python tests/bench_bus_decode.py
"""

from __future__ import annotations

import datetime as dt
import time

import numpy as np

from quantlib.bus.codec import decode, decode_view, encode
from quantlib.bus.registry import DictSchemaBackend, SchemaRegistry
from quantlib.bus.schema import BusField, BusSchema, default_schema

MINUTE = dt.datetime(2026, 6, 19, 14, 31, tzinfo=dt.timezone.utc)
N_FRAMES = 20_000  # ~2x the 10k/min target, per iteration


def _make_frames(schema: BusSchema, n: int) -> list[bytes]:
    rng = np.random.default_rng(0)
    return [
        encode(f"S{i % 500}", MINUTE, rng.standard_normal(schema.n_features).astype("<f8"), schema)
        for i in range(n)
    ]


def _read_names(schema: BusSchema) -> list[str]:
    names = schema.names()
    return [names[0], names[len(names) // 2], names[-1]]  # 3 representative per-name reads per frame


def _time_decode(frames: list[bytes], schema: BusSchema, names: list[str]) -> float:
    start = time.perf_counter()
    for frame in frames:
        vec = decode(frame, schema)
        for name in names:
            vec.value(name)
    return time.perf_counter() - start


def _time_decode_view(
    frames: list[bytes], registry: SchemaRegistry, names: list[str], *, blocking: bool
) -> float:
    start = time.perf_counter()
    for frame in frames:
        view = decode_view(frame, registry, blocking=blocking)
        for name in names:
            view.value(name)
    return time.perf_counter() - start


def _registry_for(schema: BusSchema, *, publish: bool) -> SchemaRegistry:
    backend = DictSchemaBackend()
    registry = SchemaRegistry(backend, compiled_schema=schema)
    if publish:
        registry.publish(schema)
    return registry


def run_benchmark() -> dict[str, float]:
    schema = default_schema()
    names = _read_names(schema)
    frames = _make_frames(schema, N_FRAMES)

    baseline_s = _time_decode(frames, schema, names)

    warm = _registry_for(schema, publish=True)
    _time_decode_view(frames[:100], warm, names, blocking=False)  # prime the cache
    warm_s = _time_decode_view(frames, warm, names, blocking=False)

    # COLD: a fresh registry per run so the first frame pays the one fetch (then cached).
    cold = _registry_for(schema, publish=True)
    cold_s = _time_decode_view(frames, cold, names, blocking=True)

    # FP-FLIP: a second fingerprint (a one-field superset) alternating with the first, both cached.
    extra_field = BusField(group="bench", name="bench_extra", offset=schema.n_features, version="v1")
    schema_b = BusSchema(schema.fields + [extra_field])
    flip = SchemaRegistry(DictSchemaBackend(), compiled_schema=schema)
    flip.publish(schema)
    flip.publish(schema_b)
    frames_b = _make_frames(schema_b, N_FRAMES)
    mixed = [frames[i] if i % 2 == 0 else frames_b[i] for i in range(N_FRAMES)]
    _time_decode_view(mixed[:100], flip, names, blocking=False)
    flip_s = _time_decode_view(mixed, flip, names, blocking=False)

    return {
        "baseline_decode_s": baseline_s,
        "warm_view_s": warm_s,
        "cold_view_s": cold_s,
        "fpflip_view_s": flip_s,
        "warm_ratio": warm_s / baseline_s,
        "cold_ratio": cold_s / baseline_s,
        "fpflip_ratio": flip_s / baseline_s,
        "n_frames": float(N_FRAMES),
    }


def test_warm_decode_within_5pct_of_baseline() -> None:
    results = run_benchmark()
    print(  # -s to see
        f"\n[bench] n={int(results['n_frames'])}  "
        f"baseline={results['baseline_decode_s']*1e3:.1f}ms  "
        f"warm={results['warm_view_s']*1e3:.1f}ms ({results['warm_ratio']:.3f}x)  "
        f"cold={results['cold_view_s']*1e3:.1f}ms ({results['cold_ratio']:.3f}x)  "
        f"fpflip={results['fpflip_view_s']*1e3:.1f}ms ({results['fpflip_ratio']:.3f}x)"
    )
    # The warm path is the steady state — must be within ~5% of the exact-fingerprint decode. Allow a
    # small absolute floor for timing noise on a shared CI box.
    assert results["warm_ratio"] <= 1.05 or (results["warm_view_s"] - results["baseline_decode_s"]) < 0.02


if __name__ == "__main__":
    print(run_benchmark())
