"""Unit tests for the host-side notifier (ops/notify.py): the pure rate-limit / dedup / payload logic and
the no-op-when-unconfigured contract. No real webhook is ever hit — post_webhook is monkeypatched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# notify.py lives in ops/ (host script, stdlib-only) — not an installed package. Load it by path.
_NOTIFY_PATH = Path(__file__).resolve().parents[1] / "ops" / "notify.py"
_spec = importlib.util.spec_from_file_location("ops_notify", _NOTIFY_PATH)
assert _spec is not None and _spec.loader is not None
notify = importlib.util.module_from_spec(_spec)
sys.modules["ops_notify"] = notify
_spec.loader.exec_module(notify)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the dedup-state dir at a temp dir and clear the webhook env for every test."""
    monkeypatch.setenv(notify.STATE_DIR_ENV, str(tmp_path / "alerts"))
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(notify.WEBHOOK_FORMAT_ENV, raising=False)


def test_unconfigured_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    # No QUANT_ALERT_WEBHOOK -> returns False and never attempts a POST.
    calls: list[Any] = []
    monkeypatch.setattr(notify, "post_webhook", lambda url, payload: calls.append((url, payload)) or True)
    sent = notify.send_alert("t", "b", dedup_key="k", now=1000.0)
    assert sent is False
    assert calls == []


def test_first_alert_sends_then_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.test/hook")
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(notify, "post_webhook", lambda url, payload: posted.append(payload) or True)

    first = notify.send_alert("t", "b", dedup_key="k", cooldown_s=3600, now=1000.0)
    second = notify.send_alert("t", "b", dedup_key="k", cooldown_s=3600, now=1000.0 + 60)
    assert first is True
    assert second is False  # within cooldown
    assert len(posted) == 1


def test_fires_again_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.test/hook")
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(notify, "post_webhook", lambda url, payload: posted.append(payload) or True)

    notify.send_alert("t", "b", dedup_key="k", cooldown_s=3600, now=1000.0)
    later = notify.send_alert("t", "b", dedup_key="k", cooldown_s=3600, now=1000.0 + 3601)
    assert later is True
    assert len(posted) == 2


def test_distinct_keys_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.test/hook")
    monkeypatch.setattr(notify, "post_webhook", lambda url, payload: True)
    assert notify.send_alert("t", "b", dedup_key="alpha", now=1000.0) is True
    assert notify.send_alert("t", "b", dedup_key="beta", now=1000.0) is True  # different key, not limited


def test_failed_post_does_not_record_so_it_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transient webhook failure must NOT mark the key fired, so the next tick retries (no silent swallow).
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.test/hook")
    monkeypatch.setattr(notify, "post_webhook", lambda url, payload: False)
    first = notify.send_alert("t", "b", dedup_key="k", now=1000.0)
    assert first is False
    assert notify.last_fired("k") is None  # not recorded

    monkeypatch.setattr(notify, "post_webhook", lambda url, payload: True)
    retried = notify.send_alert("t", "b", dedup_key="k", now=1000.0 + 30)
    assert retried is True  # retried successfully despite being within the cooldown


def test_payload_text_shape_default() -> None:
    payload = notify.build_payload("healthcheck FAIL", "3 checks failing")
    assert payload == {"text": "[quant-fp] healthcheck FAIL — 3 checks failing"}


def test_payload_raw_shape_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(notify.WEBHOOK_FORMAT_ENV, "raw")
    payload = notify.build_payload("title", "body")
    assert payload == {"title": "title", "body": "body", "source": notify.SOURCE}


def test_dedup_path_is_safe_filename() -> None:
    # An arbitrary key (with slashes/colons) must map to a single safe filename in the state dir.
    path = notify.dedup_path("live-restart:feature-computer(was:exited)")
    name = Path(path).name
    assert "/" not in name
    assert name.endswith(".ts")


def test_is_rate_limited_no_history_is_false() -> None:
    assert notify.is_rate_limited("never-fired", cooldown_s=3600, now=1000.0) is False
