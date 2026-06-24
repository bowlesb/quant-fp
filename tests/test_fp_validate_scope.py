"""Symbol-scoped validation — prove ``validate(symbols=...)`` compares ONLY the requested symbols.

The full-universe ``validate`` loaded the entire ~11k-symbol stream root to compare a handful of
materialized names (the OOM). The scope pushes a symbol filter into the store reads so only those
partitions load. This test is network-free: it writes tiny ``source=stream`` + ``source=backfill``
partitions for 3 symbols to a tmp store, stubs the DB touch-points (``load_tiers`` / ``write_validation``),
runs scoped validate for 2 of the 3 symbols, and asserts only those 2 appear in the durable ledger
outputs — and that a planted mismatch surfaces as an exception while the matching symbol does not.
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.features import store, validate as validate_mod, validation_store

GROUP = "calendar"  # a tiny tolerance group; every other group has no backfill written -> skipped
VERSION = "1.0.0"
FEATURE = "minute_of_day_et"  # parity_method=tolerance, nan_policy=none — the column we plant a mismatch in
# All declared calendar features must exist on disk (the scan selects every group column); the OTHER
# columns are written identically on both sides so only FEATURE can diverge.
OTHER_FEATURES = ("day_of_week", "minutes_since_open", "is_regular_session")
M0 = dt.datetime(2026, 6, 12, 14, 0, tzinfo=dt.timezone.utc)  # 10:00 ET — inside RTH
M1 = dt.datetime(2026, 6, 12, 14, 1, tzinfo=dt.timezone.utc)
DAY = "2026-06-12"
SCOPE = ["AAA", "BBB"]  # the 2 we validate; "CCC" is written but must NOT be compared


def _frame(symbol: str, value0: float, value1: float) -> pl.DataFrame:
    columns = {"symbol": [symbol, symbol], "minute": [M0, M1], FEATURE: [value0, value1]}
    for name in OTHER_FEATURES:
        columns[name] = [1.0, 1.0]  # identical on both sides -> always match
    return pl.DataFrame(columns)


def _write_store(root: str) -> None:
    """Write backfill (truth) for 3 symbols and stream for the same 3 — BBB's stream diverges at M1."""
    backfill = pl.concat([_frame("AAA", 600.0, 601.0), _frame("BBB", 600.0, 601.0), _frame("CCC", 600.0, 601.0)])
    stream = pl.concat([_frame("AAA", 600.0, 601.0), _frame("BBB", 600.0, 999.0), _frame("CCC", 600.0, 601.0)])
    store.write_group(root, GROUP, VERSION, "backfill", DAY, backfill)
    store.write_group(root, GROUP, VERSION, "stream", DAY, stream)


def _tiers() -> pl.DataFrame:
    return pl.DataFrame({"symbol": ["AAA", "BBB", "CCC"], "tier": [1, 1, 2]}, schema={"symbol": pl.String, "tier": pl.Int32})


def test_scope_tiers_filters_to_requested() -> None:
    scoped = validate_mod._scope_tiers(_tiers(), ["AAA", "BBB"])
    assert set(scoped["symbol"].to_list()) == {"AAA", "BBB"}  # CCC dropped


def test_scope_tiers_none_keeps_full_universe() -> None:
    full = validate_mod._scope_tiers(_tiers(), None)
    assert set(full["symbol"].to_list()) == {"AAA", "BBB", "CCC"}


def test_scope_tiers_raises_when_no_overlap() -> None:
    with pytest.raises(ValueError, match="none of the requested symbols"):
        validate_mod._scope_tiers(_tiers(), ["ZZZ"])


def test_scoped_validate_compares_only_requested_symbols(tmp_path, monkeypatch) -> None:
    feature_root = str(tmp_path / "store")
    val_root = str(tmp_path / "val")
    _write_store(feature_root)

    monkeypatch.setattr(validate_mod, "load_tiers", lambda day: _tiers())
    written: dict[str, pl.DataFrame] = {}
    monkeypatch.setattr(
        validate_mod.validation_db,
        "write_validation",
        lambda feature_day, trust, exceptions, day: written.update(
            feature_day=feature_day, trust=trust, exceptions=exceptions
        ),
    )

    trust = validate_mod.validate(feature_root, DAY, val_root, allow_today=False, symbols=SCOPE)

    # The trust registration covers the scoped group's feature, and is non-empty.
    assert FEATURE in set(trust["feature"].to_list())

    # Only the 2 scoped symbols are compared — CCC (written but out of scope) never appears.
    cell = validation_store.read_cell(val_root, DAY)
    assert set(cell["symbol"].to_list()) == {"AAA", "BBB"}

    # BBB diverged at M1 (stream 999 vs backfill 601) -> exactly one exception, for BBB; AAA matches.
    exceptions = validation_store.read_exceptions(val_root, DAY)
    assert set(exceptions["symbol"].to_list()) == {"BBB"}
    assert exceptions.height == 1
    bbb = exceptions.row(0, named=True)
    assert bbb["status"] == "mismatch"
    assert bbb["stream_value"] == pytest.approx(999.0)
    assert bbb["backfill_value"] == pytest.approx(601.0)

    # The DB write was handed the same scoped frames (no CCC leaks into the canonical record).
    assert set(written["exceptions"]["symbol"].to_list()) == {"BBB"}


def test_unscoped_validate_includes_all_universe_symbols(tmp_path, monkeypatch) -> None:
    feature_root = str(tmp_path / "store")
    val_root = str(tmp_path / "val")
    _write_store(feature_root)

    monkeypatch.setattr(validate_mod, "load_tiers", lambda day: _tiers())
    monkeypatch.setattr(
        validate_mod.validation_db, "write_validation", lambda feature_day, trust, exceptions, day: None
    )

    validate_mod.validate(feature_root, DAY, val_root, allow_today=False, symbols=None)
    cell = validation_store.read_cell(val_root, DAY)
    assert set(cell["symbol"].to_list()) == {"AAA", "BBB", "CCC"}  # full universe when unscoped


def _sorted(frame: pl.DataFrame) -> pl.DataFrame:
    """Canonical ordering so two results built in different symbol-batch orders compare equal."""
    if frame.height == 0:
        return frame
    keys = [col for col in ("feature", "symbol", "minute", "tier") if col in frame.columns]
    return frame.sort(keys).select(sorted(frame.columns))


def test_symbol_sub_batching_is_value_identical(tmp_path, monkeypatch) -> None:
    """The PASS-2 OOM fix: compare_groups sub-batches the symbol scope to bound the per-minute join peak.
    Prove it changes only memory, not the verdict — comparing all symbols at once (symbol_batch_size=None)
    yields byte-identical cell / feature_day / exceptions to forcing one symbol per batch (size=1)."""
    feature_root = str(tmp_path / "store")
    _write_store(feature_root)
    monkeypatch.setattr(validate_mod, "load_tiers", lambda day: _tiers())

    scope = ["AAA", "BBB", "CCC"]
    _, tiers = validate_mod.scoped_tiers(DAY, scope)

    whole = validate_mod.compare_groups(feature_root, DAY, scope, tiers, symbol_batch_size=None)
    batched = validate_mod.compare_groups(feature_root, DAY, scope, tiers, symbol_batch_size=1)

    # Every symbol is still compared (CCC across a batch boundary is not dropped).
    assert set(batched.cell["symbol"].to_list()) == {"AAA", "BBB", "CCC"}
    # The three durable frames are identical regardless of batching.
    assert _sorted(batched.cell).equals(_sorted(whole.cell))
    assert _sorted(batched.exceptions).equals(_sorted(whole.exceptions))
    assert _sorted(batched.feature_day).equals(_sorted(whole.feature_day))
    # And the planted BBB mismatch survives the batch boundary as exactly one exception.
    assert set(batched.exceptions["symbol"].to_list()) == {"BBB"}
    assert batched.exceptions.height == 1
    # feature_day must hold exactly one row per feature even though 3 batches contributed.
    assert batched.feature_day.height == batched.feature_day["feature"].n_unique()


def test_scope_batches_splits_and_preserves_order() -> None:
    """The batcher: a None/0/oversized limit is one whole-scope batch; a smaller limit splits in order."""
    symbols = ["A", "B", "C", "D", "E"]
    assert validate_mod._scope_batches(symbols, None) == [symbols]
    assert validate_mod._scope_batches(symbols, 0) == [symbols]
    assert validate_mod._scope_batches(symbols, 10) == [symbols]
    assert validate_mod._scope_batches(symbols, 2) == [["A", "B"], ["C", "D"], ["E"]]
