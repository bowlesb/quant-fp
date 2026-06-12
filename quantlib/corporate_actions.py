"""Authoritative corporate-action feed (Alpaca CorporateActionsClient — task #18).

Pulls forward/reverse/unit splits, cash dividends, and mergers/name-changes for a set of symbols
and upserts them into the `corporate_actions` table. Consumers:
  - QA jump invariant: self-gates a >Nx day-over-day backfill close jump against a REAL split.
  - backfill-manager (#17): a NEWLY-seen action on a symbol triggers a full-history single-pass
    re-fetch so month-windows never mix adjustment states (the KLAC failure class).
  - executor: names with an ex-date inside the feature-lookback window are excluded from the basket
    until their series is verified consistent (replaces the manual KLAC denylist).

Alpaca items are pydantic objects (attribute access, NOT .get()); field sets differ per action
type, so parsing guards every attribute. The split forward-factor is new_rate/old_rate.
"""
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import psycopg
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest
from psycopg.types.json import Jsonb

CHUNK = 50
SPLIT_TYPES = ("forward_splits", "reverse_splits", "unit_splits")
DIVIDEND_TYPES = ("cash_dividends",)


@dataclass
class CorporateAction:
    symbol: str
    action_type: str
    ex_date: date
    old_rate: float | None
    new_rate: float | None
    cash_rate: float | None
    record_date: date | None
    payable_date: date | None
    raw: dict[str, Any]


def _coerce_date(value: Any) -> date | None:
    """Alpaca dates arrive as date or datetime; normalize to date (None if absent)."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return value.date()


def _primary_ex_date(item: Any) -> date | None:
    """The date that anchors this action. ex_date for splits/dividends; for mergers/name-changes
    (no ex_date) fall back to the effective/process date so the row still has a PK date."""
    for attr in ("ex_date", "effective_date", "process_date", "payable_date"):
        value = _coerce_date(getattr(item, attr, None))
        if value is not None:
            return value
    return None


def _to_raw(item: Any) -> dict[str, Any]:
    """Full API payload for fields we don't promote to columns (pydantic v2 -> json-safe dict)."""
    if hasattr(item, "model_dump"):
        dumped: dict[str, Any] = item.model_dump(mode="json")
        return dumped
    return {"repr": repr(item)}


def _parse_item(action_type: str, item: Any) -> CorporateAction | None:
    """Parse one Alpaca corporate-action object into a CorporateAction, or None if it has no
    usable symbol/date (we never silently coerce a missing PK component)."""
    symbol = getattr(item, "symbol", None)
    ex_date = _primary_ex_date(item)
    if symbol is None or ex_date is None:
        return None
    old_rate = getattr(item, "old_rate", None)
    new_rate = getattr(item, "new_rate", None)
    cash_rate = getattr(item, "rate", None) if action_type in DIVIDEND_TYPES else None
    if cash_rate is None and "merger" in action_type:
        cash_rate = getattr(item, "rate", None)
    return CorporateAction(
        symbol=symbol,
        action_type=action_type,
        ex_date=ex_date,
        old_rate=old_rate,
        new_rate=new_rate,
        cash_rate=cash_rate,
        record_date=_coerce_date(getattr(item, "record_date", None)),
        payable_date=_coerce_date(getattr(item, "payable_date", None)),
        raw=_to_raw(item),
    )


def fetch_corporate_actions(
    client: CorporateActionsClient,
    symbols: list[str],
    start: date,
    end: date,
    pause_seconds: float = 0.3,
) -> list[CorporateAction]:
    """Fetch all corporate actions for symbols over [start, end]. res.data is keyed by action
    type (forward_splits, reverse_splits, cash_dividends, stock_mergers, ...); we parse every
    type generically."""
    actions: list[CorporateAction] = []
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        response = client.get_corporate_actions(
            CorporateActionsRequest(symbols=chunk, start=start, end=end)
        )
        for action_type, items in response.data.items():
            for item in items:
                parsed = _parse_item(action_type, item)
                if parsed is not None:
                    actions.append(parsed)
        time.sleep(pause_seconds)
    return actions


_UPSERT = """
INSERT INTO corporate_actions
    (symbol, action_type, ex_date, old_rate, new_rate, cash_rate,
     record_date, payable_date, raw)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (symbol, action_type, ex_date) DO UPDATE SET
    old_rate=EXCLUDED.old_rate, new_rate=EXCLUDED.new_rate, cash_rate=EXCLUDED.cash_rate,
    record_date=EXCLUDED.record_date, payable_date=EXCLUDED.payable_date,
    raw=EXCLUDED.raw, ingested_at=now()
RETURNING symbol, (xmax = 0) AS inserted
"""


def upsert_corporate_actions(
    conn: psycopg.Connection, actions: list[CorporateAction]
) -> set[str]:
    """Upsert actions. Returns the set of symbols that had a NEWLY-inserted action (not a
    re-seen one) — this is the #17 re-fetch trigger: a never-before-seen action on a symbol
    means its bar history may straddle the adjustment boundary and must be re-fetched whole.
    (xmax = 0 distinguishes a fresh INSERT from an ON CONFLICT UPDATE.)"""
    newly_inserted: set[str] = set()
    with conn.cursor() as cur:
        for action in actions:
            cur.execute(
                _UPSERT,
                (
                    action.symbol,
                    action.action_type,
                    action.ex_date,
                    action.old_rate,
                    action.new_rate,
                    action.cash_rate,
                    action.record_date,
                    action.payable_date,
                    Jsonb(action.raw),
                ),
            )
            row = cur.fetchone()
            if row is not None and row[1]:
                newly_inserted.add(row[0])
    return newly_inserted


def names_with_recent_ex_date(
    conn: psycopg.Connection,
    as_of: date,
    lookback_days: int,
    action_types: tuple[str, ...] = SPLIT_TYPES,
) -> set[str]:
    """Executor ex-date guard: symbols with an ex_date in [as_of - lookback_days, as_of] for the
    given action types (splits by default — the adjustment-consistency hazard). These names are
    excluded from the basket until their series is verified consistent."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT symbol FROM corporate_actions
            WHERE action_type = ANY(%s)
              AND ex_date BETWEEN %s AND %s
            """,
            (list(action_types), as_of - timedelta(days=lookback_days), as_of),
        )
        return {row[0] for row in cur.fetchall()}
