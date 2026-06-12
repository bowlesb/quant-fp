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
                               (split-adjustment artifacts). SELF-GATES against the #18
                               corporate_actions table (real splits auto-exempt, ±1d); manual
                               allowlist is the fallback for non-split events (IPOs/restructurings).
  backfill_realtime_parity   — I2: stream vs backfill bars agree on overlap (replay-equiv).
  trade_agg_parity           — I2b: trade-agg stream vs backfill within tolerance on overlap.
  pit_universe_membership    — I3: ACTIVE-set feature rows exist ONLY for that date's members.
  warmup_coverage            — I4: ACTIVE set has no silently NaN-degraded / dead feature.
  no_inf_no_degenerate       — I5: no Inf in vectors; predictions not score-degenerate;
                               per-ts label cross-section demeaned (avg per-ts median ~0).
  fill_reconciliation        — the live basket EXECUTED as intended (post-flatten, terminal-aware
                               orders_log): no stuck order, realized L/S net exposure within tol, fill
                               rate above floor. The hard gate the per-cycle reconcile `ok` skips
                               (exec-recon-one-directional). FAST tier.
  live_feature_coverage      — same-day LIVE serving-path coverage (Ben's 2026-06-12 ask): for
                               TODAY's source='live' rows, each feature FAMILY is valued at its
                               DERIVED expectation (price/vol ~warmup-adequate fraction; trade/
                               quote ~captured-name fraction; calendar exact) with the symbol
                               deficit EXPLAINED by warmup; fails on a family DROP vs the trailing
                               baseline (tests/fixtures/live_feature_coverage_baseline.json,
                               rolled via --update-baseline) or an unexplained symbol-count loss.

Set scoping: calendar/warmup/pit/no_inf scan ONLY the ACTIVE feature set (env QA_ACTIVE_SET,
else the highest set_version present) — so the DEFAULT run is green-on-active and cheap, and the
legacy frozen-dirty fixtures are OPT-IN red: QA_ACTIVE_SET=v1.0.0 reproduces its UTC/dead-feature
FAILs, QA_ACTIVE_SET=v1.1.0 reproduces its PIT-leak FAIL (the suite's own regression fixtures).

Run (from repo root):
    python3 scripts/qa_invariants.py             # all invariants, exit 1 on any FAIL
    python3 scripts/qa_invariants.py --fast      # FAST tier: cheap composition/calendar/same-day
                                                 #   coverage checks (every wake + post-close gate)
    python3 scripts/qa_invariants.py --full      # FULL tier: fast + heavy panel scans (nightly)
    python3 scripts/qa_invariants.py --list      # list invariant names
    python3 scripts/qa_invariants.py --only universe_is_equities_only,calendar_et_correct
    python3 scripts/qa_invariants.py --update-baseline  # roll live_feature_coverage baseline fwd

Tiering: a monolithic run of ALL invariants exceeds 500s on the 5.5M-row panel. The FAST tier
(universe composition trio + live_feature_coverage) runs in seconds as a standalone every-wake /
post-close gate; the FULL tier adds the heavy research-panel scans (parity/warmup/pit/inf/jump/
bars) for the nightly run.

DB access: by default shells to the documented method
    docker compose exec -T timescaledb psql -U quant -d quant
Override for CI / in-network runs with env QA_PSQL, e.g.
    QA_PSQL='psql postgresql://quant:...@timescaledb:5432/quant'

Exit code: 0 = all selected invariants PASS (skips allowed); 1 = any FAIL (or DB error).
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
# Steady-state mid-session NaN above this FAILS (the gap explorer-data caught 2026-06-13:
# the ragged/dead pair is blind to a feature sitting at a constant moderate NaN — neither a
# warmup gap nor 95%-dead). Measured EXCLUDING the first warmup hour of each session, where
# intraday-return NaN is correct-by-construction (no N-min-lagged bar yet at the open).
STEADY_STATE_NAN_PCT = 10.0
STEADY_STATE_WARMUP_EXCLUDE_MIN = (
    60  # skip the first 60 min/session (ret_60m by-construction NaN)
)
RTH_OPEN_MIN_OF_DAY = 570  # 09:30 ET
PRED_MIN_DISTINCT_SCORES = (
    50  # latest model_version must have at least this many scores
)
LABEL_MEDIAN_TOL = 0.001  # |per-ts median of cross-sectional excess| must be < this
SESSION_FORWARD_SLACK_DAYS = (
    4  # member trade_date may lead the latest bar by at most this
)

# fill_reconciliation: the live basket must EXECUTE as intended, asserted post-flatten when every
# submitted order is terminal. The hard gate that the per-cycle reconcile `ok` deliberately does NOT
# enforce (it stays unexpected+rejected-only to avoid flap; see services/executor #19-Q3).
# Mirrors services/executor/main.py TERMINAL_ORDER_STATES (kept in sync — Alpaca's terminal set).
TERMINAL_ORDER_STATES = {
    "filled",
    "canceled",
    "expired",
    "rejected",
    "done_for_day",
    "replaced",
}
# Skew is RELATIVE to gross, not absolute dollars — at tiny paper sizes a fully one-sided basket is
# only a few hundred $ absolute but still ~100% net exposure. |net|/gross is the size-independent
# lopsidedness measure (0 = perfectly neutral, 1 = fully one-sided). A market-neutral intent should
# realize near 0; 0.40 means 40% of gross is unhedged directional exposure.
FILL_RECON_NET_EXPOSURE_TOL = (
    0.40  # |long-short|/(long+short) filled notional beyond this -> FAIL
)
FILL_RECON_MIN_FILL_RATE_PCT = (
    60.0  # < this share of submitted legs filling -> FAIL (basket gutted)
)

EXTREME_JUMP_RATIO = 3.0  # backfill daily-close day-over-day ratio beyond this -> flag
DENYLIST_PATH = REPO / "tests" / "fixtures" / "known_funds.txt"
CORP_ACTIONS_PATH = REPO / "tests" / "fixtures" / "known_corporate_actions.txt"
LIVE_COVERAGE_BASELINE_PATH = (
    REPO / "tests" / "fixtures" / "live_feature_coverage_baseline.json"
)

# live_feature_coverage: per-family valued% must not DROP below its derived expectation, nor
# regress vs the trailing baseline. Floors are DERIVED (not hardcoded): price/vol expects ~the
# fraction of universe names with adequate intraday history; trade/quote expects ~the captured-
# name fraction; calendar must be exact. A DROP vs trailing baseline beyond tolerance fails loud.
LIVE_PRICEVOL_IDX = list(
    range(1, 12)
)  # ret_*/vol_*/vwap_dev/range_pct/gap_from_open/rel_ret_30m
LIVE_CALENDAR_IDX = [12, 13]  # minute_of_day, day_of_week
LIVE_TRADEQUOTE_IDX = [14, 15, 16, 17, 18]  # trade/quote microstructure family
LIVE_WARMUP_MIN_BARS = (
    60  # bars needed today to warm the longest price-feature lookback (60m)
)
LIVE_COVERAGE_DROP_TOL_PCT = (
    5.0  # family valued% may fall at most this vs trailing baseline
)
LIVE_PRICEVOL_FLOOR_SLACK_PCT = (
    5.0  # price/vol valued% may trail its warmup-adequacy ceiling by this
)
LIVE_SYMBOL_DROP_TOL = (
    25  # live symbol-count may fall at most this vs trailing baseline median
)

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


# Tiers (see --fast/--full): the FAST tier is cheap composition/calendar/same-day checks meant
# to run EVERY wake + post-close as a standalone gate; the FULL tier adds the heavy 5.5M-row
# research-panel scans (parity/warmup/pit/inf/jump/bars), which are nightly. A monolithic run of
# all checks exceeds 500s on the current panel, so the fast tier is what makes the daily live-
# coverage gate actually runnable.
FAST = "fast"
FULL = "full"


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


def _split_ex_dates() -> set[tuple[str, str]]:
    """(symbol, date) for split-like corporate actions (forward/reverse/stock splits), each
    expanded to ex_date ±1 day to absorb ex-date vs first-adjusted-bar timing. Reads the live
    #18 corporate_actions table to ANNOTATE the jump check (a >3× jump ON a split ex_date means
    the Adjustment.ALL fetch FAILED — splits are NOT auto-exempted, the opposite: they're the
    bug). Returns empty if the table is absent. Cash dividends are excluded — they don't move
    price >3×."""
    if scalar("SELECT to_regclass('public.corporate_actions')") in ("", "NULL"):
        return set()
    rows = sql(
        "SELECT symbol, to_char(d,'YYYY-MM-DD') FROM corporate_actions ca, "
        "LATERAL (VALUES (ca.ex_date-1),(ca.ex_date),(ca.ex_date+1)) AS x(d) "
        "WHERE ca.action_type IN ('forward_splits','reverse_splits','stock_dividends')"
    )
    return {(sym, d) for sym, d in rows}


def _split_cutoffs() -> dict[str, str]:
    """{symbol: latest split ex_date 'YYYY-MM-DD'} from the #18 corporate_actions table. Used by
    the parity check to exclude a split name's RAW-stream-vs-ADJUSTED-backfill PRE-ex overlap,
    which is incomparable by design (see check_backfill_realtime_parity). Empty if table absent.
    """
    if scalar("SELECT to_regclass('public.corporate_actions')") in ("", "NULL"):
        return {}
    rows = sql(
        "SELECT symbol, to_char(max(ex_date),'YYYY-MM-DD') FROM corporate_actions "
        "WHERE action_type IN ('forward_splits','reverse_splits') GROUP BY symbol"
    )
    return {sym: dt for sym, dt in rows}


def check_no_extreme_backfill_jump() -> Result:
    """Price integrity (task #17): no >3× UNEXPLAINED day-over-day jump in a backfill daily close.

    A split landing mid-backfill leaves pre-split months on the old adjustment basis, deflating
    prices (the KLAC 10× class). This reaches the PANEL (momentum/return features), not just the
    parity overlap, so it must be caught at the bars level. A flagged jump is exempted ONLY by the
    curated manual allowlist (genuine non-split price events — IPOs, restructurings, real moves).

    Splits are NOT auto-exempt — the opposite: under Adjustment.ALL a CORRECTLY-adjusted split is
    SMOOTH (no >3× jump), so a jump landing ON a split ex_date means the adjustment FAILED. Auto-
    exempting it would HIDE exactly the bug this check exists to catch. The #18 corporate_actions
    table is therefore used only to ANNOTATE a flagged jump ("near split ex_date — adjustment may
    have failed") for triage, never to suppress it.

    HEAVY: aggregates bars_1m (~253M rows) to daily RTH last-close. Part of the post-rebuild /
    nightly --full run, not the --fast wake. Scope to recent history with env QA_JUMP_SINCE.
    """
    symbol_wide, dated = _load_corporate_actions()
    split_dates = (
        _split_ex_dates()
    )  # for ANNOTATION only (a jump on a split = adjustment failed)
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
    flagged = []
    manual_exempt = 0  # matched the curated allowlist (genuine non-split price events)
    split_flagged = (
        0  # a flagged jump that lands ON a split ex_date = adjustment FAILED (worse)
    )
    for sym, dt, prev, close, ratio in rows:
        if sym in symbol_wide or (sym, dt) in dated:
            manual_exempt += 1
            continue
        on_split = (sym, dt) in split_dates
        if on_split:
            split_flagged += 1
        flagged.append((sym, dt, prev, close, ratio, on_split))
    if flagged:
        details = [
            f"{sym:8s} {dt}: {prev} -> {close} ({ratio}×)"
            + (
                "  <-- ON split ex_date: ADJUSTMENT FAILED (Adjustment.ALL should be smooth)"
                if on_split
                else ""
            )
            for sym, dt, prev, close, ratio, on_split in flagged[:40]
        ]
        if len(flagged) > 40:
            details.append(f"... and {len(flagged) - 40} more")
        details.append(
            f"({manual_exempt} via manual allowlist; {split_flagged} of the flagged land on a "
            f"split ex_date = failed adjustment, the rest are mixed-basis / unexplained)"
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
        f"({manual_exempt} genuine non-split event(s) exempted via manual allowlist; "
        f"splits are NOT auto-exempt — a jump on a split ex_date would FAIL as adjustment-failed)",
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
    """I2: stream and backfill bars must agree on (symbol, ts) overlap (replay-equivalence).

    SPLIT-AWARE EXCLUSION — and note the semantics are the OPPOSITE of the jump check's, so do
    NOT "fix" one to match the other (that reintroduces an inversion):
      - This check compares RAW stream close vs RETROACTIVELY split-ADJUSTED backfill close. On a
        name's PRE-ex-date overlap, those two are incomparable BY DESIGN — after a split, backfill
        retro-adjusts the old days while the stream recorded the actual as-traded price (KLAC: 833
        pre-ex bars at exactly 10×). That is NOT a replay-equivalence failure, so excluding the
        (symbol, on/before last split ex_date) overlap is CORRECT — it removes a known non-bug.
      - The JUMP check tests backfill's INTERNAL consistency, where a jump AT a split ex_date means
        the Adjustment.ALL fetch FAILED — there, a split is the BUG and must NOT be exempted.
    ACCEPTED RESIDUAL RISK: a REAL pre-ex corruption on a split name (e.g. genuine tick loss in
    those same days) would now hide from parity. What covers it: (a) `no_extreme_backfill_jump`
    catches an internal-consistency break on that name regardless of parity; (b) the POST-ex
    overlap for the same name is still fully parity-checked (KLAC 6/12-on = 0% mismatch). The
    excluded window is narrow (only on/before the last split, only for split names)."""
    split_cutoffs = _split_cutoffs()  # {symbol: latest split ex_date 'YYYY-MM-DD'}
    # VALUES list of (symbol, cutoff_date) so SQL can exclude stream bars on/before a split ex_date.
    if split_cutoffs:
        values = ",".join(
            f"('{sym}','{dt}'::date)" for sym, dt in split_cutoffs.items()
        )
        excl_cte = f", splitcut(symbol, cutoff) AS (VALUES {values}) "
        excl_pred = (
            "AND NOT EXISTS (SELECT 1 FROM splitcut sc WHERE sc.symbol=s.symbol "
            "AND (s.ts AT TIME ZONE 'America/New_York')::date <= sc.cutoff) "
        )
    else:
        excl_cte = ""
        excl_pred = ""
    # Drive the join from the (small) stream side and prune the 253M-row backfill partition to
    # the stream's ts range so Timescale can exclude chunks. Overlap + mismatch in ONE pass.
    row = sql(
        "WITH s AS (SELECT symbol, ts, close FROM bars_1m WHERE source='stream'), "
        "bnds AS (SELECT min(ts) AS mn, max(ts) AS mx FROM s) "
        f"{excl_cte}"
        "SELECT count(*) AS overlap, "
        "  count(*) FILTER (WHERE b.close <> 0 "
        f"     AND abs(s.close - b.close)/abs(b.close) > {PARITY_REL_TOL}) AS mismatch "
        "FROM s JOIN bars_1m b ON b.symbol=s.symbol AND b.ts=s.ts AND b.source='backfill' "
        "WHERE b.ts >= (SELECT mn FROM bnds) AND b.ts <= (SELECT mx FROM bnds) "
        f"{excl_pred}"
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
        f"split-excluded names (raw-vs-adjusted pre-ex overlap removed): {len(split_cutoffs)}",
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

    # Steady-state mid-session NaN (the explorer-data 2026-06-13 gap): a feature stuck at a
    # constant moderate NaN escapes BOTH the ragged (early≈late) and dead (<95%) nets. Scan the
    # WHOLE panel EXCLUDING the first warmup hour of each session (where intraday-return NaN is
    # correct-by-construction), so what's left is genuine mid-session degrade.
    open_cut = RTH_OPEN_MIN_OF_DAY + STEADY_STATE_WARMUP_EXCLUDE_MIN
    steady_rows = sql(
        "WITH d AS ("
        "  SELECT f.vector, "
        "    (EXTRACT(hour FROM f.ts AT TIME ZONE 'America/New_York')*60 "
        "     + EXTRACT(minute FROM f.ts AT TIME ZONE 'America/New_York'))::int AS mod "
        f"  FROM feature_vectors f WHERE f.source='historical' AND f.set_version='{active}'"
        "), exploded AS ("
        "  SELECT u.idx, (u.val='NaN'::float8)::int AS isnan "
        f"  FROM d, LATERAL unnest(d.vector) WITH ORDINALITY AS u(val, idx) "
        f"  WHERE d.mod >= {open_cut}"
        ") "
        "SELECT e.idx, fs.names[e.idx] AS feature, round(100.0*avg(isnan),1) AS nan_pct "
        f"FROM exploded e JOIN feature_sets fs ON fs.version='{active}' "
        "GROUP BY e.idx, fs.names[e.idx] "
        f"HAVING round(100.0*avg(isnan),1) > {STEADY_STATE_NAN_PCT} ORDER BY e.idx"
    )
    steady: list[str] = [
        f"[{int(idx)}] {feature}: {float(nan_pct)}% NaN mid-session (post-warmup, steady-state degrade)"
        for idx, feature, nan_pct in steady_rows
    ]

    details = ragged + dead + steady
    if ragged or dead or steady:
        return Result(
            "warmup_coverage",
            "FAIL",
            f"ACTIVE set {active}: {len(ragged)} ragged-warmup, {len(dead)} dead, "
            f"{len(steady)} steady-state-NaN (>{STEADY_STATE_NAN_PCT}% mid-session) feature(s)",
            details,
        )
    return Result(
        "warmup_coverage",
        "PASS",
        f"ACTIVE set {active}: no feature is ragged at warmup, dead, or steady-state NaN-degraded",
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


def _load_coverage_baseline() -> list[dict[str, object]]:
    """Trailing per-day live-coverage rows (oldest..newest). Missing file = no baseline yet."""
    if not LIVE_COVERAGE_BASELINE_PATH.exists():
        return []
    return json.loads(LIVE_COVERAGE_BASELINE_PATH.read_text())


def _family_valued_pct(valued_by_idx: dict[int, float], idxs: list[int]) -> float:
    """Mean valued% across a feature family (each feature already a valued% over today's rows)."""
    vals = [valued_by_idx[i] for i in idxs if i in valued_by_idx]
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def check_live_feature_coverage() -> Result:
    """Same-day LIVE feature coverage (Ben's question, 2026-06-12): for TODAY's source='live'
    rows, is each feature FAMILY valued at its EXPECTED level, with any deficit EXPLAINED?

    The standing suite scopes the research panel (historical, set_version active); NOTHING checked
    the live serving path on the day it is produced. This closes that gap. Floors are DERIVED,
    never hardcoded:
      - price/vol  -> ceiling = fraction of universe names with >=60 intraday bars today (warmup-
                      adequate); valued% must not trail that ceiling by > slack. RISES after backfill.
      - trade/quote-> expected = captured-name fraction (distinct trade_agg_1m symbols today /
                      live symbols). A STALL when M2 scales 50->500 is a capture regression.
      - calendar   -> must be exactly valued (deterministic from ts).
      - symbol cnt -> the universe deficit must be explained by warmup, not silent; and must not
                      drop > tol vs the trailing-baseline median (a silent live-path coverage loss).
    Also fails on any family valued% DROP vs the trailing baseline beyond tolerance. SKIPs cleanly
    before the open (no live rows yet today). Roll the baseline forward with --update-baseline.
    """
    today = scalar("SELECT (now() AT TIME ZONE 'America/New_York')::date")
    live_set = scalar(
        "SELECT set_version FROM feature_vectors WHERE source='live' "
        "GROUP BY set_version ORDER BY set_version DESC LIMIT 1"
    )
    if not live_set:
        return Result(
            "live_feature_coverage", "SKIP", "no source='live' feature_vectors yet"
        )
    dims = sql(
        "SELECT count(*), count(DISTINCT symbol), count(DISTINCT ts) FROM feature_vectors "
        f"WHERE source='live' AND set_version='{live_set}' "
        "AND (ts AT TIME ZONE 'America/New_York')::date = "
        "(now() AT TIME ZONE 'America/New_York')::date"
    )
    n_rows, n_syms, n_cadences = (int(x) for x in dims[0])
    if n_rows == 0:
        return Result(
            "live_feature_coverage",
            "SKIP",
            f"no live rows for {today} yet (pre-open / model-server idle)",
        )
    rows = sql(
        "WITH today AS ("
        "  SELECT vector FROM feature_vectors "
        f"  WHERE source='live' AND set_version='{live_set}' "
        "    AND (ts AT TIME ZONE 'America/New_York')::date = "
        "        (now() AT TIME ZONE 'America/New_York')::date"
        "), exploded AS ("
        "  SELECT u.idx, (u.val='NaN'::float8)::int AS isnan "
        "  FROM today, LATERAL unnest(today.vector) WITH ORDINALITY AS u(val, idx)"
        ") SELECT idx, round(100.0*avg(1 - isnan), 1) AS valued_pct "
        "FROM exploded GROUP BY idx ORDER BY idx"
    )
    valued = {int(idx): float(pct) for idx, pct in rows}
    pricevol = _family_valued_pct(valued, LIVE_PRICEVOL_IDX)
    calendar = _family_valued_pct(valued, LIVE_CALENDAR_IDX)
    tradequote = _family_valued_pct(valued, LIVE_TRADEQUOTE_IDX)

    universe_total = int(
        scalar(
            "SELECT count(*) FROM universe_membership WHERE in_universe AND trade_date="
            "(now() AT TIME ZONE 'America/New_York')::date"
        )
    )
    warm_enough = int(
        scalar(
            "WITH univ AS (SELECT symbol FROM universe_membership WHERE in_universe AND "
            "  trade_date=(now() AT TIME ZONE 'America/New_York')::date), "
            "b AS (SELECT symbol, count(*) n FROM bars_1m WHERE source='stream' AND "
            "  (ts AT TIME ZONE 'America/New_York')::date="
            "  (now() AT TIME ZONE 'America/New_York')::date GROUP BY symbol) "
            f"SELECT count(*) FROM univ u LEFT JOIN b USING (symbol) WHERE COALESCE(b.n,0) >= {LIVE_WARMUP_MIN_BARS}"
        )
    )
    captured = int(
        scalar(
            "SELECT count(DISTINCT symbol) FROM trade_agg_1m WHERE "
            "(ts AT TIME ZONE 'America/New_York')::date="
            "(now() AT TIME ZONE 'America/New_York')::date"
        )
    )
    pricevol_ceiling = (
        round(100.0 * warm_enough / universe_total, 1) if universe_total else 0.0
    )
    tradequote_expected = round(100.0 * captured / n_syms, 1) if n_syms else 0.0

    details = [
        f"live set {live_set} for {today}: {n_rows} rows / {n_syms} symbols / {n_cadences} cadences",
        f"price/vol valued {pricevol}% (warmup ceiling {pricevol_ceiling}% = {warm_enough}/{universe_total} names >= {LIVE_WARMUP_MIN_BARS} bars)",
        f"calendar valued {calendar}% (must be 100)",
        f"trade/quote valued {tradequote}% (expected ~{tradequote_expected}% = {captured} captured / {n_syms} live)",
        f"symbol deficit {universe_total - n_syms} of {universe_total} (explained by warmup: {universe_total - warm_enough})",
    ]
    failures: list[str] = []

    if calendar < 100.0:
        failures.append(
            f"calendar features valued {calendar}% < 100% — deterministic family is NaN-leaking"
        )
    if pricevol < pricevol_ceiling - LIVE_PRICEVOL_FLOOR_SLACK_PCT:
        failures.append(
            f"price/vol valued {pricevol}% trails warmup ceiling {pricevol_ceiling}% by "
            f">{LIVE_PRICEVOL_FLOOR_SLACK_PCT}% — coverage loss beyond warmup"
        )
    # trade/quote: should track captured-name coverage; flag a SHORTFALL vs the captured fraction
    # (the family is valued for fewer names than we actually captured -> pipeline drop), not the
    # expected low cross-sectional % itself (that RISES with M2 scaling, by design).
    if tradequote < tradequote_expected - LIVE_PRICEVOL_FLOOR_SLACK_PCT:
        failures.append(
            f"trade/quote valued {tradequote}% < captured-name expectation {tradequote_expected}% — "
            f"microstructure family NaN for names we DID capture (aggregation drop)"
        )
    # The unexplained-deficit gate: every missing live symbol should be a warmup-lacking name.
    unexplained = (universe_total - n_syms) - (universe_total - warm_enough)
    if unexplained > LIVE_SYMBOL_DROP_TOL:
        failures.append(
            f"{unexplained} live symbols missing beyond the warmup-lacking set — unexplained "
            f"live-path coverage loss (not attributable to short intraday history)"
        )

    baseline = _load_coverage_baseline()
    if baseline:
        prior = [r for r in baseline if r["date"] != today]
        if prior:

            def _med(key: str) -> float:
                vals = sorted(float(r[key]) for r in prior)  # type: ignore[arg-type]
                mid = len(vals) // 2
                return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

            for fam, val in (
                ("pricevol", pricevol),
                ("calendar", calendar),
                ("tradequote", tradequote),
            ):
                base = _med(fam)
                if val < base - LIVE_COVERAGE_DROP_TOL_PCT:
                    failures.append(
                        f"{fam} valued {val}% dropped > {LIVE_COVERAGE_DROP_TOL_PCT}% vs trailing "
                        f"baseline median {round(base,1)}% — coverage regression"
                    )
            base_syms = _med("n_syms")
            if n_syms < base_syms - LIVE_SYMBOL_DROP_TOL:
                failures.append(
                    f"live symbol count {n_syms} dropped > {LIVE_SYMBOL_DROP_TOL} vs baseline median "
                    f"{round(base_syms)} — live-path symbol coverage regression"
                )
    else:
        details.append(
            "no trailing baseline yet — run with --update-baseline to record the first row"
        )

    if failures:
        return Result("live_feature_coverage", "FAIL", "; ".join(failures), details)
    return Result(
        "live_feature_coverage",
        "PASS",
        f"live {live_set} coverage healthy for {today} (price/vol {pricevol}%, calendar {calendar}%, trade/quote {tradequote}%)",
        details,
    )


def check_fill_reconciliation() -> Result:
    """The live basket EXECUTED as intended (the hard gate the per-cycle reconcile `ok` does NOT
    enforce — see services/executor #19-Q3: per-cycle ok stays unexpected+rejected-only to avoid
    flap; THIS is where lopsided-fill / incomplete-fill fails loud). Born from the
    exec-recon-one-directional finding: on 2026-06-12 the book filled 3L/1S of an intended 3L/3S
    and the OLD reconcile reported ok:true all session.

    Scoped to the latest trading day that has submitted orders, asserted against the now-terminal-
    aware orders_log (#19 writes status + filled_qty back). Reads fills_log for realized price →
    per-side filled NOTIONAL. FAILS on: (a) any submitted order still NON-terminal (post-flatten it
    must be filled/canceled/expired/rejected — a stuck 'submitted' = a lost/hung order); (b) realized
    long-vs-short filled notional skew > tol (the lopsided-basket gate — a market-neutral intent that
    executed one-sided); (c) fill rate < floor (basket gutted by non-marketable limits). SKIPs if no
    orders today (non-trading day / pre-open)."""
    day = scalar(
        "SELECT max((intended_at AT TIME ZONE 'America/New_York')::date) FROM orders_log "
        "WHERE alpaca_order_id IS NOT NULL"
    )
    if not day:
        return Result(
            "fill_reconciliation",
            "SKIP",
            "no submitted orders yet (nothing to reconcile)",
        )
    rows = sql(
        "SELECT o.symbol, o.side, o.qty, o.status, COALESCE(o.filled_qty,0), "
        "       COALESCE(f.price, 0) "
        "FROM orders_log o "
        "LEFT JOIN LATERAL (SELECT price FROM fills_log f WHERE f.alpaca_order_id=o.alpaca_order_id "
        "                   ORDER BY fill_ts DESC LIMIT 1) f ON true "
        "WHERE o.alpaca_order_id IS NOT NULL "
        f"  AND (o.intended_at AT TIME ZONE 'America/New_York')::date = '{day}'"
    )
    n = len(rows)
    non_terminal = [r[0] for r in rows if r[3] not in TERMINAL_ORDER_STATES]
    filled = [r for r in rows if float(r[4]) > 0]
    fill_rate = 100.0 * len(filled) / n if n else 0.0
    long_notional = round(
        sum(float(r[4]) * float(r[5]) for r in filled if r[1] == "buy"), 2
    )
    short_notional = round(
        sum(float(r[4]) * float(r[5]) for r in filled if r[1] == "sell"), 2
    )
    net_notional = round(long_notional - short_notional, 2)
    gross_notional = round(long_notional + short_notional, 2)
    net_exposure = abs(net_notional) / gross_notional if gross_notional else 0.0
    n_long = sum(1 for r in filled if r[1] == "buy")
    n_short = sum(1 for r in filled if r[1] == "sell")
    details = [
        f"day {day}: {n} submitted orders, {len(filled)} filled ({fill_rate:.0f}%)",
        f"realized {n_long}L / {n_short}S; long ${long_notional} / short ${short_notional} / "
        f"net ${net_notional} / gross ${gross_notional} / net-exposure {net_exposure:.0%}",
    ]
    if non_terminal:
        details.append(f"NON-TERMINAL (stuck): {sorted(non_terminal)}")
    failures: list[str] = []
    if non_terminal:
        failures.append(
            f"{len(non_terminal)} submitted order(s) still non-terminal on a settled day "
            f"{sorted(non_terminal)} — lost/hung order, orders_log status not synced"
        )
    if net_exposure > FILL_RECON_NET_EXPOSURE_TOL:
        failures.append(
            f"realized net exposure {net_exposure:.0%} (net ${net_notional} / gross ${gross_notional}) "
            f"> {FILL_RECON_NET_EXPOSURE_TOL:.0%} — intended market-neutral, executed {n_long}L/{n_short}S "
            f"one-sided (the exec-recon-one-directional failure mode)"
        )
    if n and fill_rate < FILL_RECON_MIN_FILL_RATE_PCT:
        failures.append(
            f"fill rate {fill_rate:.0f}% < {FILL_RECON_MIN_FILL_RATE_PCT:.0f}% — basket gutted "
            f"(non-marketable limits / unfilled legs)"
        )
    if failures:
        return Result("fill_reconciliation", "FAIL", "; ".join(failures), details)
    return Result(
        "fill_reconciliation",
        "PASS",
        f"day {day}: basket executed as intended ({n_long}L/{n_short}S, net ${net_notional}, fill {fill_rate:.0f}%)",
        details,
    )


def update_coverage_baseline() -> None:
    """Append TODAY's live-coverage row to the trailing baseline (idempotent per date)."""
    today = scalar("SELECT (now() AT TIME ZONE 'America/New_York')::date")
    live_set = scalar(
        "SELECT set_version FROM feature_vectors WHERE source='live' "
        "GROUP BY set_version ORDER BY set_version DESC LIMIT 1"
    )
    if not live_set:
        print("no source='live' feature_vectors yet — nothing to record")
        return
    rows = sql(
        "WITH today AS (SELECT vector FROM feature_vectors WHERE source='live' AND "
        f"set_version='{live_set}' AND (ts AT TIME ZONE 'America/New_York')::date="
        "(now() AT TIME ZONE 'America/New_York')::date), exploded AS ("
        "SELECT u.idx, (u.val='NaN'::float8)::int AS isnan FROM today, "
        "LATERAL unnest(today.vector) WITH ORDINALITY AS u(val, idx)) "
        "SELECT idx, round(100.0*avg(1 - isnan), 1) FROM exploded GROUP BY idx ORDER BY idx"
    )
    valued = {int(idx): float(pct) for idx, pct in rows}
    n_syms = int(
        scalar(
            f"SELECT count(DISTINCT symbol) FROM feature_vectors WHERE source='live' AND set_version='{live_set}' "
            "AND (ts AT TIME ZONE 'America/New_York')::date=(now() AT TIME ZONE 'America/New_York')::date"
        )
    )
    row = {
        "date": today,
        "set_version": live_set,
        "n_syms": n_syms,
        "pricevol": _family_valued_pct(valued, LIVE_PRICEVOL_IDX),
        "calendar": _family_valued_pct(valued, LIVE_CALENDAR_IDX),
        "tradequote": _family_valued_pct(valued, LIVE_TRADEQUOTE_IDX),
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    baseline = [r for r in _load_coverage_baseline() if r["date"] != today]
    baseline.append(row)
    baseline.sort(key=lambda r: str(r["date"]))
    LIVE_COVERAGE_BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n")
    print(f"recorded live_feature_coverage baseline row for {today}: {row}")


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
    "live_feature_coverage": check_live_feature_coverage,
    "fill_reconciliation": check_fill_reconciliation,
}

# FAST-tier invariants: sub-second-to-~few-second composition / calendar / same-day checks that
# run every wake + post-close as a standalone gate. Everything NOT listed here is FULL-only (the
# heavy panel scans: bars_integrity, no_extreme_backfill_jump, backfill_realtime_parity,
# trade_agg_parity, pit_universe_membership, warmup_coverage, no_inf_no_degenerate).
FAST_INVARIANTS: set[str] = {
    "universe_is_equities_only",
    "universe_no_known_funds",
    "universe_sessions_valid",
    "live_feature_coverage",
    "fill_reconciliation",
}


def select_tier(tier: str) -> list[str]:
    """Names for a tier: 'fast' = FAST_INVARIANTS in suite order; 'full' = all invariants."""
    if tier == FAST:
        return [name for name in INVARIANTS if name in FAST_INVARIANTS]
    return list(INVARIANTS)


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
    if "--update-baseline" in argv:
        update_coverage_baseline()
        return 0
    if "--fast" in argv:
        selected = select_tier(FAST)
    elif "--full" in argv:
        selected = select_tier(FULL)
    else:
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
