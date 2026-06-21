#!/usr/bin/env python3
"""Single host-side notifier for the live trading apparatus — the first thing that actually PAGES.

Closes G9 in ``docs/SYSTEM_GAPS.md``: until now nothing alerted. The continuous healthcheck only appended
to a jsonl and ``live_monitor`` only restarted containers + logged — a real outage (like the 06-17 fc
37-min outage) paged no one. This module is the one notifier both fire into.

It is deliberately minimal and HOST-runnable (pure stdlib — the cron + live_monitor run on the host, not
in a container, so it must not import polars/psycopg). It posts a short JSON message to a webhook whose URL
comes from the ``QUANT_ALERT_WEBHOOK`` env var (Slack/Discord/generic incoming-webhook shape). The payload
is ``{"text": "<title> — <body>"}`` which Slack and Discord both accept; an optional
``QUANT_ALERT_WEBHOOK_FORMAT=raw`` sends ``{"title","body","source"}`` for a generic endpoint instead.

Two safety properties so it is safe to wire NOW and never spams:
  * **No-op when unconfigured.** If ``QUANT_ALERT_WEBHOOK`` is unset it logs a single line and returns
    ``False`` — wiring it into the crons today is harmless; the moment Ben drops the URL into the env it
    lights up. (This is the deliberate G9 plumbing-now / cred-later split.)
  * **Idempotent + rate-limited.** A ``dedup_key`` identifies a recurring condition (e.g.
    ``healthcheck-fail`` or ``live-restart:feature-computer``). The same key will not re-fire within
    ``cooldown_s`` (default 1h) — so a wedged-all-weekend FAIL pages once, not every 5-minute cron tick.
    State is a tiny per-key timestamp file under ``QUANT_ALERT_STATE_DIR`` (default ``~/.quant-ops/alerts``).

Never prints or logs the webhook URL (it embeds a secret token). CLI for the shell wrappers:

    ops/notify.py --dedup-key healthcheck-fail --title "healthcheck FAIL" --body "3 checks failing"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [notify] %(message)s")
logger = logging.getLogger("notify")

WEBHOOK_ENV = "QUANT_ALERT_WEBHOOK"
WEBHOOK_FORMAT_ENV = "QUANT_ALERT_WEBHOOK_FORMAT"  # "text" (default, Slack/Discord) or "raw"
STATE_DIR_ENV = "QUANT_ALERT_STATE_DIR"
DEFAULT_STATE_DIR = os.path.expanduser("~/.quant-ops/alerts")
DEFAULT_COOLDOWN_S = 3600  # one page per condition per hour — silences a wedged-all-weekend FAIL
SOURCE = "quant-fp"
POST_TIMEOUT_S = 8


def webhook_url() -> str | None:
    """The configured webhook URL, or None when the notifier is unconfigured (the no-op path)."""
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    return url or None


def state_dir() -> str:
    return os.environ.get(STATE_DIR_ENV, DEFAULT_STATE_DIR)


def dedup_path(dedup_key: str) -> str:
    """Per-key timestamp file path. The key is hashed so an arbitrary key is always a safe filename."""
    digest = hashlib.sha1(dedup_key.encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", dedup_key)[:48]
    return os.path.join(state_dir(), f"{safe}.{digest}.ts")


def last_fired(dedup_key: str) -> float | None:
    """Epoch seconds the ``dedup_key`` last fired, or None if never (or the marker is unreadable)."""
    path = dedup_path(dedup_key)
    try:
        with open(path, encoding="utf-8") as handle:
            return float(handle.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def record_fired(dedup_key: str, now: float) -> None:
    os.makedirs(state_dir(), exist_ok=True)
    with open(dedup_path(dedup_key), "w", encoding="utf-8") as handle:
        handle.write(f"{now:.0f}")


def is_rate_limited(dedup_key: str, cooldown_s: float, now: float) -> bool:
    """True if ``dedup_key`` fired within ``cooldown_s`` of ``now`` (so this call should be suppressed)."""
    previous = last_fired(dedup_key)
    if previous is None:
        return False
    return (now - previous) < cooldown_s


def build_payload(title: str, body: str) -> dict[str, object]:
    """Webhook body. Default "text" shape works for Slack + Discord; "raw" for a generic endpoint."""
    if os.environ.get(WEBHOOK_FORMAT_ENV, "text").strip().lower() == "raw":
        return {"title": title, "body": body, "source": SOURCE}
    return {"text": f"[{SOURCE}] {title} — {body}"}


def post_webhook(url: str, payload: dict[str, object]) -> bool:
    """POST the JSON payload. Returns True on a 2xx, False on a transport/HTTP error (logged, never raised
    — a failing pager must never take down the cron that called it). Never logs ``url`` (embeds a token)."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S) as response:
            ok = 200 <= response.status < 300
        if not ok:
            logger.warning("webhook POST returned non-2xx status")
        return ok
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        logger.warning("webhook POST failed: %s", type(error).__name__)
        return False


def send_alert(
    title: str,
    body: str,
    dedup_key: str,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    now: float | None = None,
) -> bool:
    """Send one alert, idempotently. Returns True iff a webhook POST succeeded this call.

    Returns False (a no-op) when the notifier is unconfigured or the ``dedup_key`` is still within its
    cooldown — both expected, non-error outcomes. Marks the key fired only on a successful POST, so a
    transient webhook failure is retried on the next cron tick rather than silently swallowed by the
    cooldown.
    """
    moment = time.time() if now is None else now
    url = webhook_url()
    if url is None:
        # TODO(observability G9): no QUANT_ALERT_WEBHOOK configured. Set it (Slack/Discord incoming
        # webhook, or a generic endpoint with QUANT_ALERT_WEBHOOK_FORMAT=raw) in the cron environment to
        # turn paging ON. Until then this is a logged no-op — the plumbing is wired, the cred is the gap.
        logger.info("alert suppressed (no %s configured): %s — %s", WEBHOOK_ENV, title, body)
        return False
    if is_rate_limited(dedup_key, cooldown_s, moment):
        logger.info("alert rate-limited (key=%s within %.0fs cooldown): %s", dedup_key, cooldown_s, title)
        return False
    sent = post_webhook(url, build_payload(title, body))
    if sent:
        record_fired(dedup_key, moment)
        logger.info("alert sent (key=%s): %s", dedup_key, title)
    return sent


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host-side notifier for the live trading apparatus")
    parser.add_argument("--title", required=True, help="short alert title")
    parser.add_argument("--body", required=True, help="alert body / detail")
    parser.add_argument(
        "--dedup-key",
        required=True,
        help="identifies the recurring condition; suppressed within the cooldown window",
    )
    parser.add_argument(
        "--cooldown-s",
        type=float,
        default=DEFAULT_COOLDOWN_S,
        help=f"min seconds between repeats of the same key (default {DEFAULT_COOLDOWN_S})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    send_alert(args.title, args.body, dedup_key=args.dedup_key, cooldown_s=args.cooldown_s)
    # Always exit 0: an unconfigured/rate-limited/failed-POST notifier is a normal outcome, and the caller
    # (a cron / live_monitor) must never treat "no page" as a failure of its own run.
    return 0


if __name__ == "__main__":
    sys.exit(main())
