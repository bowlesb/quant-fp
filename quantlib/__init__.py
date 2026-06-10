"""Shared library used identically by the live ingestor and the historical
backfiller. Keeping aggregation/feature logic here — and calling it from both
paths — is what guarantees real-time/backfill parity (see ARCHITECTURE.md).

Pure stdlib only: no wall-clock reads, no I/O. Deterministic given inputs.
"""
