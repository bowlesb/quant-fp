"""The uncached daily-table build must fail with an ACTIONABLE error when the raw-bar glob is
empty (no store mounted / wrong STORE_ROOT / date range outside coverage) — instead of dying deep
in polars with `cannot concat empty list`. Guards the harness/battery cold-start foot-gun."""
from __future__ import annotations

import pytest

from quantlib.battery.panel import build_daily_table


def test_build_daily_table_empty_glob_raises_actionable(tmp_path, monkeypatch) -> None:
    # Point STORE at an empty dir so the raw-bar glob matches nothing.
    monkeypatch.setattr("quantlib.battery.panel.STORE", str(tmp_path))
    cache_path = str(tmp_path / "does_not_exist.parquet")
    with pytest.raises(FileNotFoundError) as excinfo:
        build_daily_table(["2026-01-02", "2026-01-03"], cache_path)
    message = str(excinfo.value)
    assert "no raw bars found" in message
    assert str(tmp_path) in message  # names the STORE that was searched
    assert "2 date(s)" in message  # echoes the requested date count
