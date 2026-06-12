"""The experimenter must NOT persist transient DB failures as done.

The poisoning bug (4 W11 ids): a transient psycopg.OperationalError — lock
exhaustion / out-of-shared-memory during a concurrent rebuild — was caught and
written to results.jsonl as a done {"error": ...} record. done_ids() never retries
a persisted id, so those experiments were permanently dead. Fix: transient ->
run_one_experiment returns None (skip, no persist, retry next cycle); deterministic
errors (ValueError / KeyError / non-operational psycopg.Error) persist so a real bug
isn't retried forever.

The experimenter module imports lightgbm/numpy (the research harness), so this test
skips in the bare test container and runs wherever those are installed (e.g. the
experimenter image). It monkeypatches the panel-load + run_experiment seams so no
real DB is needed.
"""
import contextlib
import sys
from pathlib import Path

import pytest

pytest.importorskip("lightgbm", reason="experimenter research harness needs lightgbm")
pytest.importorskip("numpy")

import psycopg  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "experimenter"))

import main  # noqa: E402


class _FakeX:
    """Stand-in for the numpy feature matrix; only ever indexed for the vol_30m col."""

    def __getitem__(self, key: object) -> list[float]:
        return [0.0]


@contextlib.contextmanager
def _fake_conn():  # type: ignore[no-untyped-def]
    yield object()  # load_panel is mocked, so the conn is never used


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub psycopg.connect + zero the backoff so the retry path is exercised without
    a real DB or real sleeps. Individual tests set main.load_panel / run_experiment."""
    monkeypatch.setattr(psycopg, "connect", lambda **kw: _fake_conn())
    monkeypatch.setattr(main, "TRANSIENT_DB_BACKOFF_S", 0.0)


def test_transient_operational_error_not_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(conn: object, horizon: str, sv: str) -> object:
        raise psycopg.OperationalError("out of shared memory")

    monkeypatch.setattr(main, "load_panel", boom)
    assert main.run_one_experiment({"id": "x", "hypothesis": "h"}) is None


def test_transient_recovers_within_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky(conn: object, horizon: str, sv: str) -> tuple:
        calls["n"] += 1
        if calls["n"] < 2:
            raise psycopg.OperationalError("lock timeout")
        return (["vol_30m"], [1, 2], ["A", "B"], _FakeX(), [0.0] * 2000)

    monkeypatch.setattr(main, "load_panel", flaky)
    monkeypatch.setattr(main, "run_experiment", lambda *a, **k: {"mean_ic": 0.01})
    result = main.run_one_experiment({"id": "x", "hypothesis": "h", "label": "raw"})
    assert result == {"mean_ic": 0.01}
    assert calls["n"] == 2


def test_panel_too_small_not_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    def small(conn: object, horizon: str, sv: str) -> tuple:
        return (["vol_30m"], [1], ["A"], _FakeX(), [0.0] * 10)

    monkeypatch.setattr(main, "load_panel", small)
    assert main.run_one_experiment({"id": "x", "hypothesis": "h"}) is None


def test_deterministic_error_is_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok(conn: object, horizon: str, sv: str) -> tuple:
        return (["vol_30m"], list(range(2000)), ["A"] * 2000, _FakeX(), [0.0] * 2000)

    def raise_value(*a: object, **k: object) -> object:
        raise ValueError("bad label transform")

    monkeypatch.setattr(main, "load_panel", ok)
    monkeypatch.setattr(main, "run_experiment", raise_value)
    result = main.run_one_experiment({"id": "x", "hypothesis": "h", "label": "raw"})
    assert result is not None and "ValueError" in result["error"]
