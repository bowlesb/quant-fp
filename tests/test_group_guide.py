"""Unit tests for the group detail-panel info builder (services/dashboard/group_guide).

The registry + the curated guide YAML are exercised against monkeypatched fakes: the per-feature
descriptions come from a fake catalog, the docstrings from a fake registry, and the curated guide from a tmp
YAML — so a seeded group surfaces its value narrative and an un-seeded group is honestly ``guide: null``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import group_guide as gg  # noqa: E402  (path inserted above)


class _FakeGroup:
    def __init__(self, name: str) -> None:
        self.name = name


def _fake_catalog() -> dict[str, list[dict[str, object]]]:
    return {
        "candlestick": [
            {"feature": "body_ratio", "description": "Real-body size…", "type": "candlestick", "layer": "A"},
            {"feature": "is_doji", "description": "Indecision bar.", "type": "candlestick", "layer": "A"},
        ],
        "asset_flags": [
            {"feature": "is_shortable", "description": "Can be shorted.", "type": "reference", "layer": "A"},
        ],
    }


def test_build_group_info_seeded_and_stubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guide_file = tmp_path / "guide.yaml"
    guide_file.write_text(
        "candlestick:\n"
        "  purpose: Per-minute candle shape.\n"
        "  value: Measures intrabar shape; hypothesis only, not an edge.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gg, "_catalog_by_group", _fake_catalog)
    monkeypatch.setattr(gg, "GUIDE_PATH", guide_file)
    monkeypatch.setattr(
        gg.REGISTRY, "groups", lambda: [_FakeGroup("candlestick"), _FakeGroup("asset_flags")]
    )

    info = gg.build_group_info()
    assert set(info) == {"candlestick", "asset_flags"}

    # A SEEDED group: registry per-feature descriptions + the curated guide value.
    candle = info["candlestick"]
    assert candle["n_features"] == 2
    assert candle["features"][0] == {"name": "body_ratio", "description": "Real-body size…"}
    assert candle["guide"]["purpose"] == "Per-minute candle shape."
    assert "hypothesis" in candle["guide"]["value"]

    # An UN-SEEDED group: registry content present, but guide is honestly None (no fabricated rationale).
    flags = info["asset_flags"]
    assert flags["n_features"] == 1
    assert flags["guide"] is None


def test_missing_guide_file_yields_no_narratives(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gg, "_catalog_by_group", _fake_catalog)
    monkeypatch.setattr(gg, "GUIDE_PATH", tmp_path / "does_not_exist.yaml")
    monkeypatch.setattr(
        gg.REGISTRY, "groups", lambda: [_FakeGroup("candlestick"), _FakeGroup("asset_flags")]
    )

    info = gg.build_group_info()
    # Registry half still serves; every group's guide is None (no file).
    assert info["candlestick"]["guide"] is None
    assert info["asset_flags"]["guide"] is None
    assert info["candlestick"]["features"][1]["name"] == "is_doji"
