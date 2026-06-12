#!/usr/bin/env python3
"""Automated, fail-loud QA invariant suite — the QA_LEDGER invariants as CHECKS, not prose.

The lesson that created this file (2026-06-11): ~21% of the universe was ETFs/leveraged
funds ranked against stocks, and it polluted the central "no edge" verdict. It slipped
because QA's invariants were PROSE. This suite turns each standing invariant into a
concrete assertion that FAILS LOUD with a clear message, so vigilance no longer depends
on any agent "noticing".

Invariants (map to docs/QA_LEDGER.md):
  universe_is_equities_only  — composition: 0 is_etf_like members. NECESSARY-NOT-SUFFICIENT —
                               shares the builder's regex, so it cannot catch a fund the regex
                               misses; it only guards against the builder dropping the filter.
  universe_no_known_funds    — composition (INDEPENDENT gate): 0 members on the FROZEN known-
                               fund denylist (tests/fixtures/known_funds.txt) + a separately-
                               maintained leverage/inverse token heuristic (warn). This is the
                               anti-tautology check that can catch a re-introduced fund.
  universe_sessions_valid    — I1/calendar: no weekend member trade_dates; latest member date
                               doesn't lead the latest ingested ET session (UTC-vs-ET bug).
  calendar_et_correct        — I1: stored minute_of_day/day_of_week equal true ET wall-clock and
                               are RTH (ACTIVE set only). UTC-calendar-leak catch.
  bars_integrity             — OHLC + minute-grid sanity (extended-hours bars reported, not failed).
  no_extreme_backfill_jump   — task #17: no >3× unexplained backfill daily-close day-over-day jump
                               (mixed split-adjustment artifacts; allowlist exempts real splits).
  backfill_realtime_parity   — I2: stream vs backfill bars agree on overlap (replay-equiv).
  trade_agg_parity           — I2b: trade-agg stream vs backfill within tolerance on overlap.
  pit_universe_membership    — I3: ACTIVE-set feature rows exist ONLY for that date's members.
  warmup_coverage            — I4: ACTIVE set has no silently NaN-degraded / dead feature.
  no_inf_no_degenerate       — I5: no Inf in vectors; predictions not score-degenerate;
                               per-ts label cross-section demeaned (avg per-ts median ~0).

Set scoping: calendar/warmup/pit/no_inf scan ONLY the ACTIVE feature set (env QA_ACTIVE_SET,
else the highest set_version present) — so the DEFAULT run is green-on-active and cheap, and the
legacy frozen-dirty fixtures are OPT-IN red: QA_ACTIVE_SET=v1.0.0 reproduces its UTC/dead-feature
FAILs, QA_ACTIVE_SET=v1.1.0 reproduces its PIT-leak FAIL (the suite's own regression fixtures).

Run (from repo root):
    python3 scripts/qa_invariants.py             # all invariants, exit 1 on any FAIL
    python3 scripts/qa_invariants.py --list      # list invariant names
    python3 scripts/qa_invariants.py --only universe_is_equities_only,calendar_et_correct

DB access: by default shells to the documented method
    docker compose exec -T timescaledb psql -U quant -d quant
Override for CI / in-network runs with env QA_PSQL, e.g.
    QA_PSQL='psql postgresql://quant:...@timescaledb:5432/quant'

Exit code: 0 = all selected invariants PASS (skips allowed); 1 = any FAIL (or DB error).
"""

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from quantlib.universe import is_etf_like  # noqa: E402  (path insert required first)

PSQL = os.environ.get(
    "QA_PSQL", "docker compose exec -T timescaledb psql -U quant -d quant"
)

# Tolerances / thresholds — tune here, documented inline.
PARITY_REL_TOL = 0.002  # 0.2% close-price agreement on stream<->backfill overlap
PARITY_MAX_MISMATCH_PCT = 1.0  # >1% of overlapping bars mismatching -> FAIL
TRADE_AGG_WITHIN_PCT = 2.0  # |diff|/ref tolerance band for trade-agg parity
TRADE_AGG_MIN_WITHIN_RATE = 98.0  # >=98% of overlap within band -> PASS (M2 gate)
TRADE_AGG_MIN_SAMPLE = 200  # below this overlap, SKIP (not enough to judge)
WARMUP_EARLY_LATE_GAP_PCT = 20.0  # early_nan - late_nan > this -> ragged warmup FAIL
DEAD_FEATURE_NAN_PCT = 95.0  # NaN-rate above this on a non-warmup feature -> dead/FAIL
PRED_MIN_DISTINCT_SCORES = (
    50  # latest model_version must have at least this many scores
)
LABEL_MEDIAN_TOL = 0.001  # |per-ts median of cross-sectional excess| must be < this
SESSION_FORWARD_SLACK_DAYS = (
    4  # member trade_date may lead the latest bar by at most this
)

EXTREME_JUMP_RATIO = 3.0  # backfill daily-close day-over-day ratio beyond this -> flag
DENYLIST_PATH = REPO / "tests" / "fixtures" / "known_funds.txt"
CORP_ACTIONS_PATH = REPO / "tests" / "fixtures" / "known_corporate_actions.txt"

# Independent leverage/inverse/structure tokens, maintained HERE and deliberately NOT identical
# to quantlib.universe's regex, so this gate can flag a fund the builder's classifier misses
# (anti-tautology: a check that shares the builder's signal can never catch the next slip).
# Used only to WARN (surface for review) — the hard gate is the frozen denylist below, which is
# precise. These tokens may over-fire on operating-company names, hence warn-not-fail.
_INDEP_FUND_TOKENS = (
    " ETF",
    " ETN",
    "ProShares",
    "Direxion",
    "iShares",
    "SPDR",
    "Invesco",
    "VanEck",
    "WisdomTree",
    "GraniteShares",
    "Grayscale",
    "Roundhill",
    "YieldMax",
    "Tradr",
    "Leveraged",
    "Inverse",
    " Ultra",
    "VIX Futures",
    "Index Fund",
    " CLO ",
)


@dataclass
class Result:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    message: str
    details: list[str] = field(default_factory=list)


def sql(query: str) -> list[list[str]]:
    """Run one SQL statement, return rows as lists of string cells (tab-separated)."""
    cmd = PSQL.split() + ["-At", "-F", "\t", "-c", query]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    if proc.returncode != 0:
        raise RuntimeError(
            f"psql failed (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return [line.split("\t") for line in proc.stdout.splitlines() if line != ""]


def scalar(query: str) -> str:
    rows = sql(query)
    if not rows or not rows[0]:
        return ""
    return rows[0][0]


def active_set_version() -> str:
    """The feature set the system currently builds/serves: env QA_ACTIVE_SET, else the highest
    version present in feature_vectors. Set-scoped invariants gate on THIS version; older
    versions (e.g. the frozen-dirty v1.1.0 fixture) are reported informationally, not failed.
    Target a fixture explicitly with QA_ACTIVE_SET=v1.1.0 to reproduce its historical FAIL.
    """
    env = os.environ.get("QA_ACTIVE_SET")
    if env:
        return env
    rows = sql(
        "SELECT set_version FROM feature_vectors GROUP BY set_version ORDER BY set_version DESC LIMIT 1"
    )
    if not rows:
        raise RuntimeError(
            "no feature_vectors present — cannot determine active set version"
        )
    return rows[0][0]


def _latest_members() -> tuple[str, list[tuple[str, str]]]:
    """(latest trade_date, [(symbol, name), ...]) for the current in_universe members."""
    latest = scalar("SELECT max(trade_date) FROM universe_membership")
    rows = sql(
        "SELECT um.symbol, am.name FROM universe_membership um "
        "JOIN asset_metadata am USING (symbol) "
        f"WHERE um.trade_date = '{latest}' AND um.in_universe ORDER BY um.symbol"
    )
    return latest, [(sym, name) for sym, name in rows]


def _load_denylist() -> dict[str, str]:
    """Frozen known-fund denylist (symbol -> name). Independent of the live is_etf_like regex."""
    if not DENYLIST_PATH.exists():
        raise RuntimeError(f"denylist fixture missing: {DENYLIST_PATH}")
    denylist: dict[str, str] = {}
    for line in DENYLIST_PATH.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        symbol, _, name = line.partition("\t")
        denylist[symbol.strip()] = name.strip()
    return denylist


def _load_corporate_actions() -> tuple[set[str], set[tuple[str, str]]]:
    """Allowlist of CONFIRMED-REAL corporate actions so legit splits don't false-positive.

    Returns (symbol_wide_exemptions, (symbol, date)_exemptions). A bare 'SYMBOL' line exempts all
    that symbol's jumps; 'SYMBOL<TAB>DATE' exempts only the jump on that ET session date.
    """
    symbol_wide: set[str] = set()
    dated: set[tuple[str, str]] = set()
    if not CORP_ACTIONS_PATH.exists():
        return symbol_wide, dated
    for line in CORP_ACTIONS_PATH.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        symbol, _, date = line.partition("\t")
        symbol = symbol.strip()
        if date.strip():
            dated.add((symbol, date.strip()))
        else:
            symbol_wide.add(symbol)
    return symbol_wide, dated


def check_no_extreme_backfill_jump() -> Result:
    """Price integrity (task #17): no >3× UNEXPLAINED day-over-day jump in a backfill daily close.

    A split landing mid-backfill leaves pre-split months on the old adjustment basis, deflating
    prices (the KLAC 10× class). This reaches the PANEL (momentum/return features), not just the
    parity overlap, so it must be caught at the bars level. Real splits/reverse-splits are exempted
    via the curated corporate-action allowlist (we have no corporate-action feed yet).

    HEAVY: aggregates bars_1m (~253M rows) to daily RTH last-close. Part of the post-rebuild
    battery, not the light wake run. Scope to recent history with env QA_JUMP_SINCE=YYYY-MM-DD.
    """
    symbol_wide, dated = _load_corporate_actions()
    since = os.environ.get("QA_JUMP_SINCE")
    et = "(ts AT TIME ZONE 'America/New_York')"
    since_clause = f"AND ts >= '{since}'" if since else ""
    rows = sql(
        "WITH daily AS ("
        "  SELECT symbol, ts::date AS dt, last(close, ts) AS close "
        f"  FROM bars_1m WHERE source='backfill' {since_clause} "
        f"    AND {et}::time >= '09:30' AND {et}::time < '16:00' "
        "  GROUP BY symbol, ts::date"
        "), jumps AS ("
        "  SELECT symbol, dt, close, lag(close) OVER (PARTITION BY symbol ORDER BY dt) AS prev "
        "  FROM daily"
        ") "
        "SELECT symbol, dt, prev, close, round((close/prev)::numeric,2) AS ratio FROM jumps "
        f"WHERE prev > 0 AND (close/prev > {EXTREME_JUMP_RATIO} OR close/prev < {1.0/EXTREME_JUMP_RATIO}) "
        "ORDER BY symbol, dt"
    )
    flagged = [
        (sym, dt, prev, close, ratio)
        for sym, dt, prev, close, ratio in rows
        if sym not in symbol_wide and (sym, dt) not in dated
    ]
    exempted = len(rows) - len(flagged)
    if flagged:
        details = [
            f"{sym:8s} {dt}: {prev} -> {close} ({ratio}×)"
            for sym, dt, prev, close, ratio in flagged[:40]
        ]
        if len(flagged) > 40:
            details.append(f"... and {len(flagged) - 40} more")
        details.append(
            f"({exempted} jump(s) exempted by the corporate-action allowlist)"
        )
        return Result(
            "no_extreme_backfill_jump",
            "FAIL",
            f"{len(flagged)} unexplained >{EXTREME_JUMP_RATIO}× backfill daily-close jump(s) "
            f"(mixed split-adjustment artifacts — the KLAC class — corrupt panel features)",
            details,
        )
    return Result(
        "no_extreme_backfill_jump",
        "PASS",
        f"no unexplained >{EXTREME_JUMP_RATIO}× backfill daily-close jumps "
        f"({exempted} known corporate action(s) exempted)",
    )


def _calendar_indices() -> tuple[int, int]:
    """Locate (minute_of_day, day_of_week) 1-based indices; assert they are stable."""
    rows = sql(
        "SELECT version, i, names[i] FROM feature_sets, generate_subscripts(names,1) i "
        "WHERE names[i] IN ('minute_of_day','day_of_week') ORDER BY version, i"
    )
    mins = {int(i) for _, i, n in rows if n == "minute_of_day"}
    dows = {int(i) for _, i, n in rows if n == "day_of_week"}
    if len(mins) != 1 or len(dows) != 1:
        raise RuntimeError(
            f"calendar feature indices not stable across versions: minute_of_day={mins} "
            f"day_of_week={dows} — calendar invariant cannot assume a fixed index"
        )
    return mins.pop(), dows.pop()


def check_universe_is_equities_only() -> Result:
    """Universe composition: ZERO ETF/leveraged/fund members at the latest trade_date.

    NECESSARY-NOT-SUFFICIENT: this reuses the SAME is_etf_like() the builder uses, so on a
    freshly-built universe it is tautologically green — it CANNOT catch a fund whose name slips
    that regex (and Alpaca classes stocks and ETFs alike as 'us_equity', so there is no broker-
    side separation to fall back on). It guards against regressions where the builder stops
    calling the classifier at all. The INDEPENDENT gate is `universe_no_known_funds` (frozen
    denylist) — that is the one that can catch the next contamination.
    """
    latest, rows = _latest_members()
    offenders = [(sym, name) for sym, name in rows if is_etf_like(name)]
    n_members = len(rows)
    if offenders:
        details = [f"{sym:8s} {name}" for sym, name in offenders[:50]]
        if len(offenders) > 50:
            details.append(f"... and {len(offenders) - 50} more")
        return Result(
            "universe_is_equities_only",
            "FAIL",
            f"{len(offenders)}/{n_members} members at {latest} are ETF/leveraged/fund "
            f"products (must be 0) — these get ranked cross-sectionally against stocks",
            details,
        )
    return Result(
        "universe_is_equities_only",
        "PASS",
        f"0/{n_members} members at {latest} classify as funds via is_etf_like "
        f"(necessary-not-sufficient: see universe_no_known_funds for the independent gate)",
    )


def check_universe_no_known_funds() -> Result:
    """INDEPENDENT universe-composition gate (anti-tautology).

    Hard gate: no current member symbol is in the FROZEN known-fund denylist
    (tests/fixtures/known_funds.txt) — a snapshot of confirmed fund symbols that does NOT
    depend on the live is_etf_like regex, so it catches a fund re-introduced as a member even
    if someone later weakens or stops calling the classifier. WARN (surface, not fail): member
    names matching a separately-maintained leverage/inverse token list that are NOT already on
    the denylist — candidate new funds to review (the tokens may over-fire, so warn-not-fail).
    """
    denylist = _load_denylist()
    latest, rows = _latest_members()
    in_deny = [(sym, denylist[sym]) for sym, _ in rows if sym in denylist]

    heuristic_hits = [
        (sym, name)
        for sym, name in rows
        if sym not in denylist
        and name
        and any(token.lower() in f" {name} ".lower() for token in _INDEP_FUND_TOKENS)
    ]

    details: list[str] = [f"frozen denylist: {len(denylist)} known funds"]
    if heuristic_hits:
        details.append(
            f"WARN — {len(heuristic_hits)} member(s) match independent fund tokens but are not "
            f"on the denylist (review; may be false positives):"
        )
        details.extend(f"  ? {sym:8s} {name}" for sym, name in heuristic_hits[:20])

    if in_deny:
        offender_lines = [f"{sym:8s} {name}" for sym, name in in_deny[:50]]
        if len(in_deny) > 50:
            offender_lines.append(f"... and {len(in_deny) - 50} more")
        return Result(
            "universe_no_known_funds",
            "FAIL",
            f"{len(in_deny)}/{len(rows)} members at {latest} are on the frozen known-fund "
            f"denylist (a confirmed fund was re-introduced into the equity universe)",
            offender_lines + details,
        )
    return Result(
        "universe_no_known_funds",
        "PASS",
        f"0/{len(rows)} members at {latest} are on the frozen known-fund denylist "
        f"(independent of the builder's classifier)",
        details,
    )


def check_universe_sessions_valid() -> Result:
    """Universe rows exist only on valid ET trading sessions (calendar lane; task #13).

    maybe_build_universe used datetime.now(UTC).date(); near midnight UTC (≈ 7-8pm ET) that
    rolls to the WRONG session — a Friday-evening build lands a Saturday trade_date, or a date
    ahead of any real session. This asserts: (1) NO member trade_date falls on a weekend, and
    (2) the latest member trade_date does not lead the latest ingested bar's ET session by more
    than a few days (gross forward-dating guard).
    """
    weekend = sql(
        "SELECT DISTINCT trade_date FROM universe_membership "
        "WHERE in_universe AND extract(isodow from trade_date) IN (6,7) ORDER BY trade_date"
    )
    max_trade = scalar(
        "SELECT max(trade_date) FROM universe_membership WHERE in_universe"
    )
    latest_bar_et = scalar(
        "SELECT (max(ts) AT TIME ZONE 'America/New_York')::date FROM bars_1m"
    )
    lead_days = int(scalar(f"SELECT ('{max_trade}'::date - '{latest_bar_et}'::date)"))
    details = [
        f"latest member trade_date: {max_trade}",
        f"latest bar ET session:    {latest_bar_et} (member date leads by {lead_days}d)",
        f"weekend member trade_dates: {len(weekend)}",
    ]
    failures = []
    if weekend:
        failures.append(
            f"{len(weekend)} weekend trade_date(s): {[w[0] for w in weekend][:10]}"
        )
    if lead_days > SESSION_FORWARD_SLACK_DAYS:
        failures.append(
            f"latest member trade_date {max_trade} leads the latest bar session "
            f"{latest_bar_et} by {lead_days}d (> {SESSION_FORWARD_SLACK_DAYS}) — likely a "
            f"UTC-vs-ET session-date bug"
        )
    if failures:
        return Result("universe_sessions_valid", "FAIL", "; ".join(failures), details)
    return Result(
        "universe_sessions_valid",
        "PASS",
        "all universe trade_dates fall on weekdays and align with ingested ET sessions",
        details,
    )


def check_calendar_et_correct() -> Result:
    """I1: stored minute_of_day/day_of_week equal true America/New_York wall-clock.

    A row computed in UTC instead of ET (the P0 UTC-today / UTC-stream contamination) will
    have a minute_of_day / day_of_week that disagrees with the ET derivation — this finds it.
    Also asserts every feature row is inside RTH (ET 09:30-16:00): features are only sampled
    on the RTH cadence grid, so a non-RTH feature ts is itself a calendar bug.
    """
    min_idx, dow_idx = _calendar_indices()
    et = "(ts AT TIME ZONE 'America/New_York')"
    expected_min = f"(extract(hour from {et})*60 + extract(minute from {et}))"
    expected_dow = (
        f"(extract(isodow from {et}) - 1)"  # isodow Mon=1..Sun=7 -> Mon=0..Sun=6
    )
    rth = f"({et}::time >= '09:30' AND {et}::time < '16:00')"
    active = active_set_version()
    # Scan ONLY the active set: legacy frozen-dirty fixtures are opt-in red via QA_ACTIVE_SET
    # (e.g. QA_ACTIVE_SET=v1.0.0 reproduces the UTC-leakage FAIL), not standing red — and it
    # keeps the default run cheap (one set, not the whole table).
    groups = sql(
        "SELECT source, "
        f"  count(*) FILTER (WHERE vector[{min_idx}] <> {expected_min} "
        f"                     OR vector[{dow_idx}] <> {expected_dow}) AS cal_bad, "
        f"  count(*) FILTER (WHERE NOT {rth}) AS non_rth, "
        "  count(*) AS total "
        f"FROM feature_vectors WHERE set_version='{active}' GROUP BY 1 ORDER BY 1"
    )
    details: list[str] = []
    active_bad = 0
    for source, cal_bad, non_rth, total in groups:
        active_bad += int(cal_bad) + int(non_rth)
        flags = []
        if int(cal_bad):
            flags.append("UTC leakage")
        if int(non_rth):
            flags.append("non-RTH ts")
        flag = ("  <-- " + ", ".join(flags)) if flags else ""
        details.append(
            f"{source:11s} cal_bad={int(cal_bad)} non_rth={int(non_rth)} / {int(total)}{flag}"
        )
    if active_bad:
        return Result(
            "calendar_et_correct",
            "FAIL",
            f"{active_bad} rows in ACTIVE set {active} have minute_of_day/day_of_week that "
            f"disagree with ET wall-clock (UTC-calendar leakage)",
            details,
        )
    return Result(
        "calendar_et_correct",
        "PASS",
        f"ACTIVE set {active}: all feature rows match America/New_York wall-clock and are RTH",
        details,
    )


def check_bars_integrity() -> Result:
    """OHLC sanity + minute-grid (data_probes #1 as a hard gate).

    NOTE: raw bars_1m LEGITIMATELY contains extended-hours bars (~14% of backfill is pre/
    post-market); RTH filtering happens downstream at feature-compute time. So extended-hours
    exposure is reported informationally, NOT failed. One grouped scan (bars_1m is ~253M rows).
    """
    probes = [
        ("high<low", "high < low"),
        ("high<open/close", "high < open OR high < close"),
        ("low>open/close", "low > open OR low > close"),
        ("close<=0", "close <= 0"),
        ("volume<0", "volume < 0"),
        (
            "vwap outside [low,high]",
            "vwap IS NOT NULL AND (vwap < low - 0.01 OR vwap > high + 0.01)",
        ),
        ("ts off minute-grid", "extract(second from ts) <> 0"),
    ]
    filters = ", ".join(f"count(*) FILTER (WHERE {pred})" for _, pred in probes)
    et = "(ts AT TIME ZONE 'America/New_York')"
    row = sql(
        f"SELECT {filters}, "
        f"  count(*) FILTER (WHERE source='backfill' AND NOT ({et}::time >= '09:30' AND {et}::time < '16:00')), "
        "  count(*) FROM bars_1m"
    )[0]
    counts = [int(c) for c in row]
    total_bad = sum(counts[: len(probes)])
    details = [f"{label:28s} {counts[i]}" for i, (label, _) in enumerate(probes)]
    ext_hours, total = counts[len(probes)], counts[len(probes) + 1]
    details.append(
        f"{'extended-hours bars (info only)':28s} {ext_hours} "
        f"({100.0*ext_hours/total:.1f}% of {total} — expected, not a violation)"
    )
    if total_bad:
        return Result(
            "bars_integrity",
            "FAIL",
            f"{total_bad} OHLC/grid integrity violations",
            details,
        )
    return Result(
        "bars_integrity", "PASS", "all OHLC/minute-grid integrity probes are 0", details
    )


def check_backfill_realtime_parity() -> Result:
    """I2: stream and backfill bars must agree on (symbol, ts) overlap (replay-equivalence)."""
    # Drive the join from the (small) stream side and prune the 253M-row backfill partition to
    # the stream's ts range so Timescale can exclude chunks. Overlap + mismatch in ONE pass.
    row = sql(
        "WITH s AS (SELECT symbol, ts, close FROM bars_1m WHERE source='stream'), "
        "bnds AS (SELECT min(ts) AS mn, max(ts) AS mx FROM s) "
        "SELECT count(*) AS overlap, "
        "  count(*) FILTER (WHERE b.close <> 0 "
        f"     AND abs(s.close - b.close)/abs(b.close) > {PARITY_REL_TOL}) AS mismatch "
        "FROM s JOIN bars_1m b ON b.symbol=s.symbol AND b.ts=s.ts AND b.source='backfill' "
        "WHERE b.ts >= (SELECT mn FROM bnds) AND b.ts <= (SELECT mx FROM bnds)"
    )[0]
    overlap, mismatch = int(row[0]), int(row[1])
    if overlap == 0:
        return Result(
            "backfill_realtime_parity",
            "SKIP",
            "no stream<->backfill bar overlap yet (cannot test replay-equivalence)",
        )
    pct = 100.0 * mismatch / overlap
    details = [
        f"overlap bars: {overlap}",
        f"mismatch (>{PARITY_REL_TOL*100:.1f}% close diff): {mismatch} ({pct:.2f}%)",
    ]
    if pct > PARITY_MAX_MISMATCH_PCT:
        return Result(
            "backfill_realtime_parity",
            "FAIL",
            f"{pct:.2f}% of overlapping bars disagree (> {PARITY_MAX_MISMATCH_PCT}%) — "
            f"backfill is not replay-equivalent to the live stream",
            details,
        )
    return Result(
        "backfill_realtime_parity",
        "PASS",
        f"{100.0 - pct:.2f}% of {overlap} overlapping bars agree within {PARITY_REL_TOL*100:.1f}%",
        details,
    )


def check_trade_agg_parity() -> Result:
    """I2b: trade-agg stream vs backfill on overlap — the weakest, most-important parity.

    Order flow is the leading edge candidate; its aggregation from a lossy live feed is the
    least-proven path. This check enforces the M2 gate (>=98% within 2%) WHEN both sources
    exist on overlapping minutes; otherwise it SKIPs loudly (a skip is NOT green — it means
    settled-day trade-agg parity at scale is still unproven, blocked on sharded ingestion).
    """
    has_source = sql("SELECT DISTINCT source FROM trade_agg_1m")
    sources = {row[0] for row in has_source}
    if not ({"stream", "backfill"} <= sources):
        return Result(
            "trade_agg_parity",
            "SKIP",
            f"trade_agg_1m lacks both sources (have: {sorted(sources)}) — settled-day "
            f"trade-agg parity at scale UNPROVEN (blocked on sharded trade/quote ingestion)",
        )
    overlap = int(
        scalar(
            "SELECT count(*) FROM trade_agg_1m s JOIN trade_agg_1m b "
            "ON b.symbol=s.symbol AND b.ts=s.ts AND b.source='backfill' WHERE s.source='stream'"
        )
    )
    if overlap < TRADE_AGG_MIN_SAMPLE:
        return Result(
            "trade_agg_parity",
            "SKIP",
            f"only {overlap} overlapping trade-agg minutes (< {TRADE_AGG_MIN_SAMPLE}) — "
            f"too small to judge; settled-day parity at scale still unproven",
        )
    within = int(
        scalar(
            "SELECT count(*) FROM trade_agg_1m s JOIN trade_agg_1m b "
            "ON b.symbol=s.symbol AND b.ts=s.ts AND b.source='backfill' "
            "WHERE s.source='stream' AND b.n_trades <> 0 "
            f"AND abs(s.n_trades - b.n_trades)/abs(b.n_trades::float) <= {TRADE_AGG_WITHIN_PCT/100.0}"
        )
    )
    rate = 100.0 * within / overlap
    details = [
        f"overlap minutes: {overlap}",
        f"within {TRADE_AGG_WITHIN_PCT}%: {within} ({rate:.2f}%)",
    ]
    if rate < TRADE_AGG_MIN_WITHIN_RATE:
        return Result(
            "trade_agg_parity",
            "FAIL",
            f"only {rate:.2f}% of trade-agg minutes within {TRADE_AGG_WITHIN_PCT}% "
            f"(< {TRADE_AGG_MIN_WITHIN_RATE}% gate) — trade features not yet trustworthy",
            details,
        )
    return Result(
        "trade_agg_parity",
        "PASS",
        f"{rate:.2f}% of {overlap} trade-agg minutes within {TRADE_AGG_WITHIN_PCT}% (>= gate)",
        details,
    )


def check_pit_universe_membership() -> Result:
    """I3: every historical feature row's (symbol, date) is an in_universe member that date.

    A feature row for a non-member (or a derived/leveraged ticker) is point-in-time leakage:
    the model would see names that were not selectable that day.
    """
    active = active_set_version()
    leak = int(
        scalar(
            "SELECT count(*) FROM ("
            "  SELECT DISTINCT fv.symbol, fv.ts::date AS dt FROM feature_vectors fv "
            f"  WHERE fv.source='historical' AND fv.set_version='{active}'"
            ") f "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM universe_membership um "
            "  WHERE um.symbol=f.symbol AND um.trade_date=f.dt AND um.in_universe"
            ")"
        )
    )
    if leak:
        examples = sql(
            "SELECT DISTINCT fv.symbol, fv.ts::date FROM feature_vectors fv "
            f"WHERE fv.source='historical' AND fv.set_version='{active}' AND NOT EXISTS ("
            "  SELECT 1 FROM universe_membership um "
            "  WHERE um.symbol=fv.symbol AND um.trade_date=fv.ts::date AND um.in_universe"
            ") ORDER BY 1,2 LIMIT 25"
        )
        details = [f"{sym} @ {dt}" for sym, dt in examples]
        return Result(
            "pit_universe_membership",
            "FAIL",
            f"ACTIVE set {active}: {leak} (symbol,date) feature rows are NOT in-universe "
            f"members for that date (point-in-time leakage)",
            details,
        )
    return Result(
        "pit_universe_membership",
        "PASS",
        f"ACTIVE set {active}: every historical feature (symbol,date) is an in-universe member",
    )


def check_warmup_coverage() -> Result:
    """I4: no feature silently NaN-degraded; ragged-warmup detector (early vs late NaN-rate).

    Scoped to the ACTIVE set only (legacy frozen-dirty fixtures are opt-in via QA_ACTIVE_SET,
    e.g. QA_ACTIVE_SET=v1.0.0 reproduces its 5 dead micro features).
    """
    active = active_set_version()
    rows = sql(
        "WITH bounds AS ("
        "  SELECT min(ts::date) mn, max(ts::date) mx "
        f"  FROM feature_vectors WHERE source='historical' AND set_version='{active}'"
        "), d AS ("
        # Only unnest boundary dates (first/last 6 sessions), not the whole panel.
        "  SELECT f.ts::date AS dt, f.vector FROM feature_vectors f, bounds b "
        f"  WHERE f.source='historical' AND f.set_version='{active}' "
        "    AND (f.ts::date <= b.mn + 5 OR f.ts::date >= b.mx - 5)"
        "), exploded AS ("
        "  SELECT u.idx, (u.val='NaN'::float8)::int AS isnan, "
        "         (d.dt <= b.mn + 5) AS early, (d.dt >= b.mx - 5) AS late "
        "  FROM d, bounds b, LATERAL unnest(d.vector) WITH ORDINALITY AS u(val, idx)"
        ") "
        "SELECT e.idx, fs.names[e.idx] AS feature, "
        "       round(100.0*avg(isnan) FILTER (WHERE early),1) AS early_nan, "
        "       round(100.0*avg(isnan) FILTER (WHERE late),1)  AS late_nan, "
        "       round(100.0*avg(isnan),1) AS all_nan "
        f"FROM exploded e JOIN feature_sets fs ON fs.version='{active}' "
        "GROUP BY e.idx, fs.names[e.idx] ORDER BY e.idx"
    )
    ragged: list[str] = []
    dead: list[str] = []
    for idx, feature, early, late, all_nan in rows:
        early_f, late_f, all_f = float(early), float(late), float(all_nan)
        is_ragged = early_f - late_f > WARMUP_EARLY_LATE_GAP_PCT
        is_dead = all_f >= DEAD_FEATURE_NAN_PCT
        if not (is_ragged or is_dead):
            continue
        kind = "ragged warmup" if is_ragged else f"{all_f}% NaN (dead)"
        line = f"[{idx}] {feature}: early={early_f}% late={late_f}% — {kind}"
        (ragged if is_ragged else dead).append(line)
    details = ragged + dead
    if ragged or dead:
        return Result(
            "warmup_coverage",
            "FAIL",
            f"ACTIVE set {active}: {len(ragged)} ragged-warmup, {len(dead)} dead feature(s)",
            details,
        )
    return Result(
        "warmup_coverage",
        "PASS",
        f"ACTIVE set {active}: no feature is ragged at warmup or dead",
        details,
    )


def check_no_inf_no_degenerate() -> Result:
    """I5: no Inf in vectors; predictions not score-degenerate; per-ts labels demeaned.

    The Inf scan is scoped to the ACTIVE set (legacy opt-in via QA_ACTIVE_SET).
    """
    active = active_set_version()
    details: list[str] = []
    failures: list[str] = []

    # Per-row array membership (= ANY) instead of unnest (which would explode 6.9M x 25 rows).
    inf_count = int(
        scalar(
            f"SELECT count(*) FROM feature_vectors WHERE set_version='{active}' AND "
            "('Infinity'::float8 = ANY(vector) OR '-Infinity'::float8 = ANY(vector))"
        )
    )
    details.append(
        f"ACTIVE set {active}: feature rows containing Inf/-Inf: {inf_count}"
    )
    if inf_count:
        failures.append(
            f"{inf_count} feature rows in {active} contain Inf/-Inf elements"
        )

    pred_rows = sql("SELECT count(*) FROM predictions")
    if pred_rows and int(pred_rows[0][0]) > 0:
        latest_model = scalar(
            "SELECT model_version FROM predictions ORDER BY ts DESC LIMIT 1"
        )
        latest_ts = scalar(
            f"SELECT max(ts) FROM predictions WHERE model_version='{latest_model}'"
        )
        distinct_scores = int(
            scalar(
                "SELECT count(DISTINCT score) FROM predictions "
                f"WHERE model_version='{latest_model}' AND ts='{latest_ts}'"
            )
        )
        n_preds = int(
            scalar(
                "SELECT count(*) FROM predictions "
                f"WHERE model_version='{latest_model}' AND ts='{latest_ts}'"
            )
        )
        details.append(
            f"latest preds {latest_model}@{latest_ts}: {distinct_scores} distinct scores / {n_preds}"
        )
        if (
            n_preds >= PRED_MIN_DISTINCT_SCORES
            and distinct_scores < PRED_MIN_DISTINCT_SCORES
        ):
            failures.append(
                f"predictions score-degenerate: only {distinct_scores} distinct scores across "
                f"{n_preds} names (basket decided by tie-break, not signal)"
            )
    else:
        details.append("predictions: none yet (skip degeneracy check)")

    label_rows = sql("SELECT count(*) FROM labels")
    if label_rows and int(label_rows[0][0]) > 0:
        # Use the TYPICAL per-ts median (avg of |median| across timestamps), not the max —
        # a single thin/outlier cross-section must not be mistaken for systemic mis-centering.
        worst = sql(
            "SELECT horizon, round(avg(abs(med))::numeric,6) AS avg_abs, "
            "       round(max(abs(med))::numeric,6) AS max_abs FROM ("
            "  SELECT horizon, ts, percentile_cont(0.5) WITHIN GROUP (ORDER BY value) AS med "
            "  FROM labels GROUP BY horizon, ts"
            ") m GROUP BY horizon ORDER BY horizon"
        )
        for horizon, avg_abs_med, max_abs_med in worst:
            details.append(
                f"label {horizon}: avg |per-ts median| = {avg_abs_med} (max {max_abs_med})"
            )
            if float(avg_abs_med) > LABEL_MEDIAN_TOL:
                failures.append(
                    f"label {horizon} not demeaned: avg per-ts cross-section median = "
                    f"{avg_abs_med} (> {LABEL_MEDIAN_TOL}) — cross-sectional excess is off-center"
                )
    else:
        details.append("labels: none yet (skip demean check)")

    if failures:
        return Result("no_inf_no_degenerate", "FAIL", "; ".join(failures), details)
    return Result(
        "no_inf_no_degenerate",
        "PASS",
        "no Inf; predictions non-degenerate; per-ts label cross-sections demeaned",
        details,
    )


INVARIANTS: dict[str, Callable[[], Result]] = {
    "universe_is_equities_only": check_universe_is_equities_only,
    "universe_no_known_funds": check_universe_no_known_funds,
    "universe_sessions_valid": check_universe_sessions_valid,
    "calendar_et_correct": check_calendar_et_correct,
    "bars_integrity": check_bars_integrity,
    "no_extreme_backfill_jump": check_no_extreme_backfill_jump,
    "backfill_realtime_parity": check_backfill_realtime_parity,
    "trade_agg_parity": check_trade_agg_parity,
    "pit_universe_membership": check_pit_universe_membership,
    "warmup_coverage": check_warmup_coverage,
    "no_inf_no_degenerate": check_no_inf_no_degenerate,
}


def run(selected: list[str]) -> int:
    results: list[Result] = []
    for name in selected:
        check = INVARIANTS[name]
        try:
            results.append(check())
        except RuntimeError as exc:
            results.append(Result(name, "FAIL", f"check errored: {exc}"))

    print("\n" + "=" * 78)
    print("QA INVARIANT SUITE")
    print("=" * 78)
    for result in results:
        glyph = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}[result.status]
        print(f"\n[{result.status}] {glyph} {result.name}")
        print(f"      {result.message}")
        for line in result.details:
            print(f"        {line}")

    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_skip = sum(1 for r in results if r.status == "SKIP")
    n_pass = sum(1 for r in results if r.status == "PASS")
    print("\n" + "-" * 78)
    print(f"SUMMARY: {n_pass} PASS, {n_fail} FAIL, {n_skip} SKIP")
    print("-" * 78)
    return 1 if n_fail else 0


def main() -> int:
    argv = sys.argv[1:]
    if "--list" in argv:
        for name in INVARIANTS:
            print(name)
        return 0
    selected = list(INVARIANTS)
    if "--only" in argv:
        idx = argv.index("--only")
        selected = [n.strip() for n in argv[idx + 1].split(",") if n.strip()]
        unknown = [n for n in selected if n not in INVARIANTS]
        if unknown:
            print(
                f"unknown invariant(s): {unknown}; known: {list(INVARIANTS)}",
                file=sys.stderr,
            )
            return 2
    return run(selected)


if __name__ == "__main__":
    raise SystemExit(main())
