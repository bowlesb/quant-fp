"""Unit tests for the SECTOR coverage surface (services/dashboard/sector_coverage).

No live DB: the two reads (``_read_universe_sectors`` / ``_read_sector_map_totals``) are passed in as
fixtures (the pure ``build_sector_coverage`` takes them as args), so the aggregation — per-sector ranking,
the classified-vs-unknown split, the blank-row vs no-row distinction, the classified %, and the sample cap —
is exercised end-to-end against a controlled universe.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import sector_coverage as sc  # noqa: E402  (path inserted above)


def _row(symbol: str, has_row: bool, sector: str | None) -> dict[str, object]:
    """One ``_read_universe_sectors`` row: a universe symbol with its sector-map join flags."""
    classified = sector is not None and sector != ""
    return {
        "symbol": symbol,
        "has_row": has_row,
        "classified": classified,
        "sector": sector,
        "_universe_date": "2026-06-22",
    }


# A controlled universe: 3 Technology, 2 Financial Services, 1 Healthcare = 6 classified;
# 1 blank-sector row + 2 no-row = 3 unknown; 9 total.
_UNIVERSE = [
    _row("AAPL", True, "Technology"),
    _row("MSFT", True, "Technology"),
    _row("NVDA", True, "Technology"),
    _row("JPM", True, "Financial Services"),
    _row("GS", True, "Financial Services"),
    _row("UNH", True, "Healthcare"),
    _row("ABR.PRD", True, ""),  # blank-sector row (FMP couldn't map)
    _row("AACBR", False, None),  # no sector_map row at all
    _row("ACHR.WS", False, None),  # no sector_map row at all
]

_TOTALS = {"n_rows": 4980, "n_classified": 2697, "n_distinct_sectors": 11}


def test_split_and_classified_pct() -> None:
    view = sc.build_sector_coverage(universe_rows=_UNIVERSE, sector_map_totals=_TOTALS)
    assert view["universe_size"] == 9
    assert view["n_classified"] == 6
    assert view["n_unknown"] == 3
    assert view["classified_pct"] == round(100.0 * 6 / 9, 1)  # 66.7
    assert view["universe_date"] == "2026-06-22"


def test_blank_row_vs_no_row_distinction() -> None:
    view = sc.build_sector_coverage(universe_rows=_UNIVERSE, sector_map_totals=_TOTALS)
    # unknown (3) splits into 1 blank-sector row + 2 with no row at all.
    assert view["n_blank_sector"] == 1
    assert view["n_no_row"] == 2
    assert view["n_blank_sector"] + view["n_no_row"] == view["n_unknown"]


def test_sectors_ranked_desc_with_pct() -> None:
    view = sc.build_sector_coverage(universe_rows=_UNIVERSE, sector_map_totals=_TOTALS)
    sectors = view["sectors"]
    assert [s["sector"] for s in sectors] == ["Technology", "Financial Services", "Healthcare"]
    assert [s["n_symbols"] for s in sectors] == [3, 2, 1]
    assert view["n_distinct_sectors"] == 3
    # pct_of_universe is over the whole universe (incl. unknown), not just classified.
    assert sectors[0]["pct_of_universe"] == round(100.0 * 3 / 9, 1)


def test_unclassified_sample_sorted_and_capped() -> None:
    # 50 no-row unknowns -> sample is alphabetical and capped at UNCLASSIFIED_SAMPLE.
    big = [_row("AAPL", True, "Technology")]
    big += [_row(f"WARR{idx:03d}", False, None) for idx in range(50)]
    view = sc.build_sector_coverage(universe_rows=big, sector_map_totals=_TOTALS)
    sample = view["unclassified_sample"]
    assert len(sample) == sc.UNCLASSIFIED_SAMPLE
    assert sample == sorted(sample)
    assert sample[0] == "WARR000"


def test_sector_map_totals_passed_through() -> None:
    view = sc.build_sector_coverage(universe_rows=_UNIVERSE, sector_map_totals=_TOTALS)
    assert view["sector_map"] == {"n_rows": 4980, "n_classified": 2697, "n_distinct_sectors": 11}


def test_empty_universe_is_safe() -> None:
    view = sc.build_sector_coverage(universe_rows=[], sector_map_totals=_TOTALS)
    assert view["universe_size"] == 0
    assert view["classified_pct"] == 0.0
    assert view["sectors"] == []
    assert view["unclassified_sample"] == []
    assert view["universe_date"] is None
