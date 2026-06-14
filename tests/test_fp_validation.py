"""The validation ledger's shared cell-match predicate and per-cell verdict.

These are the foundation of the real-time-vs-backfill validation ledger (docs/VALIDATION_LEDGER.md):
``match_predicate`` is the ONE agreement test used by both the parity report and the ledger, and
``cell_verdict`` classifies every stored cell (match / mismatch / extra_live / missing_live). The
ledger's per-feature trust grade is only as sound as these, so they are pinned here.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec
from quantlib.features.compare import (
    VERDICT_EXTRA,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_MISSING,
    VERDICT_UNCOMPARED,
    cell_verdict,
    match_predicate,
)

DESC = "x" * 40  # descriptions aren't validated by the dataclass; keep them realistic
FLOAT_SPEC = FeatureSpec(name="ret_5m", description=DESC, dtype="Float64", tolerance=1e-6)
FLAG_SPEC = FeatureSpec(name="is_up", description=DESC, dtype="Float64", valid_range=(0.0, 1.0), storage="UInt8")


def test_match_predicate_float_is_relative_tolerance() -> None:
    """Real-valued feature: agree within 1e-12 + tol*|b|; a 1e-3 gap fails; exact zeros agree."""
    frame = pl.DataFrame({"ret_5m": [1.0, 1.0, 0.0], "ret_5m_bk": [1.0 + 5e-7, 1.0 + 1e-3, 0.0]})
    got = frame.select(match_predicate(FLOAT_SPEC, pl.col("ret_5m"), pl.col("ret_5m_bk")).alias("m"))["m"].to_list()
    assert got == [True, False, True]


def test_match_predicate_flag_is_exact() -> None:
    """Flag/UInt8-stored feature: a 0/1 indicator is right or wrong — exact equality, never tolerance."""
    frame = pl.DataFrame({"is_up": [1.0, 1.0, 0.0], "is_up_bk": [1.0, 0.0, 0.0]})
    got = frame.select(match_predicate(FLAG_SPEC, pl.col("is_up"), pl.col("is_up_bk")).alias("m"))["m"].to_list()
    assert got == [True, False, True]


def test_cell_verdict_classifies_every_case() -> None:
    """match / mismatch / extra_live / missing_live / uncompared — one expression, all five branches."""
    frame = pl.DataFrame(
        {
            "ret_5m": [1.0, 1.0, 2.0, None, None],
            "ret_5m_bk": [1.0, 1.5, None, 9.0, None],
        }
    )
    got = frame.select(cell_verdict(FLOAT_SPEC, "ret_5m", frame.schema).alias("v"))["v"].to_list()
    assert got == [VERDICT_MATCH, VERDICT_MISMATCH, VERDICT_EXTRA, VERDICT_MISSING, VERDICT_UNCOMPARED]


def test_cell_verdict_treats_nan_as_absent() -> None:
    """A float NaN is not a value — live-NaN vs real backfill is missing_live, not a mismatch."""
    frame = pl.DataFrame({"ret_5m": [float("nan"), 1.0], "ret_5m_bk": [1.0, float("nan")]})
    got = frame.select(cell_verdict(FLOAT_SPEC, "ret_5m", frame.schema).alias("v"))["v"].to_list()
    assert got == [VERDICT_MISSING, VERDICT_EXTRA]
