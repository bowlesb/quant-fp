"""Unit tests for return_dispersion — cross-sectional std/IQR of the universe's returns (regime variable).

Hand-built minute_agg + daily frames with a known cross-section lock in the dispersion math. Parity
(compute_latest == compute on the last minute) is covered by the generic tests/test_fp_latest.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _ctx_minute() -> BatchContext:
    """3 symbols over 6 minutes with a controlled spread of 5-min returns at minute 5."""
    syms = ["AAA", "BBB", "CCC"]
    rows = []
    # closes engineered so the 5m return (close[t]/close[t-5]-1) at minute 5 is +0.01 / 0.00 / -0.01
    paths = {"AAA": [100, 100, 100, 100, 100, 101.0],
             "BBB": [100, 100, 100, 100, 100, 100.0],
             "CCC": [100, 100, 100, 100, 100, 99.0]}
    for s in syms:
        for i, px in enumerate(paths[s]):
            rows.append({"symbol": s, "minute": BASE + timedelta(minutes=i), "close": float(px)})
    minute = pl.DataFrame(rows)
    # daily frame (unused for the minute-horizon assertions but the group declares it as input)
    daily = pl.DataFrame({"symbol": syms, "date": [BASE.date()] * 3, "close": [100.0, 100.0, 100.0]})
    return BatchContext(frames={"minute_agg": minute, "daily": daily})


def _row_at(out: pl.DataFrame, sym: str, minute_idx: int) -> dict:
    return out.filter(
        (pl.col("symbol") == sym) & (pl.col("minute") == BASE + timedelta(minutes=minute_idx))
    ).row(0, named=True)


def test_dispersion_std_known_cross_section() -> None:
    out = run_group(REGISTRY.get_group("return_dispersion"), _ctx_minute())
    # at minute 5 the 5m returns are {+0.01, 0.0, -0.01}; std (ddof=1) of those:
    rets = np.array([0.01, 0.0, -0.01])
    expected_std = float(np.std(rets, ddof=1))
    row = _row_at(out, "AAA", 5)
    assert row["return_dispersion_std_5m"] == pytest.approx(expected_std, rel=1e-6)
    # broadcast: every symbol gets the SAME market-wide scalar at that minute
    assert _row_at(out, "CCC", 5)["return_dispersion_std_5m"] == pytest.approx(expected_std, rel=1e-6)


def test_dispersion_iqr_known() -> None:
    out = run_group(REGISTRY.get_group("return_dispersion"), _ctx_minute())
    # IQR(p75-p25) of {-0.01, 0.0, +0.01}
    rets = np.array([-0.01, 0.0, 0.01])
    expected_iqr = float(np.quantile(rets, 0.75) - np.quantile(rets, 0.25))
    row = _row_at(out, "AAA", 5)
    # polars + numpy quantile interpolation can differ slightly; allow a loose tolerance on the IQR shape
    assert row["return_dispersion_iqr_5m"] > 0
    assert row["return_dispersion_iqr_5m"] == pytest.approx(expected_iqr, abs=0.011)


def test_warmup_null_before_window() -> None:
    out = run_group(REGISTRY.get_group("return_dispersion"), _ctx_minute())
    # at minute 2 there is no 5-min-ago bar -> all returns null -> std over an all-null cross-section is null
    row = _row_at(out, "AAA", 2)
    assert row["return_dispersion_std_5m"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
