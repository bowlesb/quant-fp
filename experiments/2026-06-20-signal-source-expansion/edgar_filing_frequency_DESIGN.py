"""EDGAR filing-FREQUENCY feature group — DESIGN + REFERENCE IMPLEMENTATION (next-cycle stub, NOT WIRED).

STATUS: design artifact, intentionally NOT under quantlib/features/groups/ and NOT @register-ed. Registering
it would add its features to BusSchema.from_registry() and bump the bus fingerprint WITHOUT the group ever
producing values (it would never be `runnable()` until the `filings` input frame is wired), corrupting the
vector layout. So this lives here as the spec the next Modeller cycle lifts into groups/ + wires in ONE pass.

WHY A SEPARATE PR (not bundled with market_turbulence): turbulence reads the `minute_agg` frame that every
materialize/capture path already builds — zero new wiring. EDGAR needs a NEW input frame (`filings`) threaded
through (1) a new DB loader, (2) every materialize_* path in materialize.py, and (3) the live-capture session
snapshots in real_capture.py — a meaningfully larger, capture-touching surface that deserves its own focused
PR with its own backfill==live parity verification. The Lead flagged EDGAR as next-cycle; this readies it.

================================================================================================
DATA (verified live 2026-06-19): the `filings` hypertable (db/init/08_filings.sql, services/edgar) holds
3,175,782 rows / 5,628 symbols / 1994-01-07 -> 2026-06-19, columns (symbol, form_type, available_at, ...).
Form-type counts: 4=1.37M, 8-K=461k, 6-K=163k, 10-Q=121k, 3=106k, 10-K=41k, ... — ample for frequency feats.

PARITY CONTRACT (Lead-confirmed): a DB-join feature is parity-true IFF it reads ONLY filings with
``available_at <= ctx-minute``. ``available_at`` is FIXED at first sight (08_filings.sql: ON CONFLICT DO
NOTHING / coalesce-preserve — never rewritten), so the available_at-gated set at any minute T is IDENTICAL
in live and backfill. This is the SAME compute-time-join pattern as the static reference/behavioral-cluster
joins, EXTENDED with a per-minute point-in-time gate — exactly how the `daily` snapshot is sliced
point-in-time per minute by the multi_day groups (a session snapshot, point-in-time-filtered inside compute).

================================================================================================
WIRING (the next-cycle PR does all four; each mirrors an EXISTING pattern):

1. LOADER — add to quantlib/features/loaders.py (mirrors load_reference / load_universe):

    _FILINGS_SQL = '''
    SELECT symbol, form_type, available_at
    FROM filings
    WHERE symbol IS NOT NULL AND available_at < %(day_end)s
    '''
    FILINGS_SCHEMA = {"symbol": pl.String, "form_type": pl.String,
                      "available_at": pl.Datetime("us", "UTC")}

    def load_filings(day: str) -> pl.DataFrame:
        '''All filings with available_at strictly before the END of `day` (day+1 00:00 UTC). A SESSION
        SNAPSHOT loaded once — the per-minute available_at<=minute gate inside compute() makes it
        point-in-time, so loading the whole day up front is correct (the same shape as the `daily`
        snapshot the multi_day groups slice point-in-time). Source-independent: available_at is fixed at
        first sight, so feeding this identical frame to live and backfill is parity-true by construction.
        Include a LOOKBACK so the trailing 7/30/90-day counts and minutes-since-last are correct at the
        session start — load available_at in [day - MAX_LOOKBACK_DAYS, day_end).'''
        # day_start - 90d <= available_at < day_end  (MAX_LOOKBACK_DAYS = 90, the deepest count window)

   NOTE the lookback: the deepest count window is 90 DAYS, so the snapshot MUST include filings back
   90 days before the session day (not just the day's filings) or count_90d / days_since_last are wrong
   at session start. Load WHERE available_at >= day_start - INTERVAL '90 days' AND available_at < day_end.

2. MATERIALIZE — add ``"filings": load_filings(day)`` to the frames dict in EACH materialize_* in
   materialize.py that should produce EDGAR features (the from-raw bar paths + materialize_minute). It is
   a per-day snapshot like `reference`/`daily`. (load_filings is keyed on the day; it does NOT depend on
   source, so both the stream and backfill sides get the identical frame.)

3. LIVE CAPTURE — add ``"filings": load_filings(day)`` to the session ``snapshots`` dict in real_capture.py
   (BOTH the run() startup at ~line 151 and the sharded path at ~line 197). Loaded ONCE at session start
   like `daily`. The intraday arrival of new 8-Ks is handled by the per-minute available_at<=minute gate:
   a filing that becomes available at 10:32 has available_at=10:32, so it enters the feature only from the
   10:32 minute onward — IF the startup snapshot already covers the whole day (available_at < day_end), it
   is present in the frame but gated out until its minute. (Filings that the edgar ingestor writes to the DB
   AFTER the session snapshot load are the one subtlety — see PARITY TEST below; the snapshot is loaded at
   session start which is premarket, and 08_filings is collecting continuously, so an intraday-arriving 8-K
   filed mid-session would NOT be in a premarket snapshot. RESOLUTION: load the snapshot lazily/refresh, OR
   accept that live uses the available_at<=minute gate over a snapshot refreshed each minute. The cleanest
   parity-true form: the live reduce re-queries filings for the trailing window each minute — but that is a
   per-minute DB hit. ALTERNATIVE that keeps the snapshot static + parity-true: since available_at is the
   point-in-time field, load the snapshot covering [day_start-90d, day_end) at startup AND have the edgar
   ingestor's writes land before the session — for the deep-history frequency features (7/30/90-day counts)
   a same-day 8-K barely moves a 30/90-day count, but minutes_since_last_8k WOULD be wrong intraday. The
   next-cycle PR must decide: (a) per-minute trailing-window re-query (simplest correct, modest cost on the
   reduce path which already runs once/minute), or (b) a minute-refreshed snapshot. RECOMMENDATION: (a) —
   model EDGAR as a small per-minute GATHER-style DB read on the reduce path, NOT a frozen startup snapshot,
   so intraday filings are reflected the minute they become available. Backfill replays the exact
   available_at, so (a) is parity-true.)

4. BACKFILL — load_filings(day) reads the SAME table for the historical day; available_at replay makes it
   identical to what live saw. The parity sweep then validates backfill==live on the EDGAR group.

================================================================================================
PARITY TEST (the gate the next-cycle PR must add, tests/test_fp_edgar_filing_frequency.py):
  * compute_latest == compute().last (the generic latest contract) — trivially true since the feature at T
    is a pure function of the available_at<=T filing set.
  * backfill==live: feed the SAME load_filings frame to both sources -> identical (parity-true by the
    fixed-at-first-sight available_at). Plus a point-in-time test: a filing with available_at=10:32 must NOT
    appear in the 10:31 feature value and MUST appear in the 10:32 value (no look-ahead).
  * count math: count_{7,30,90}d, days_since_last, per-form counts vs a hand-built filing timeline.

================================================================================================
FEATURES (frequency/timing only — NO content; 8-K-item / Form-4 parse deferred per the roadmap):

  edgar_filing_count_{7,30,90}d      : count of THIS symbol's filings with available_at in (T-Nd, T]
  edgar_minutes_since_last_filing    : (T - max available_at <= T), in minutes; null if none ever
  edgar_minutes_since_last_8k        : same, restricted to form_type='8-K' (the event-clock Ben named)
  edgar_count_8k_90d / _10q_90d / _10k_90d / _form4_90d : per-major-form 90d counts
  edgar_filing_burst                 : count_7d vs a trailing-year baseline rate (a filing-frequency spike)

All keyed on available_at<=ctx-minute (look-ahead-safe). All per-SYMBOL (NOT a universe gather) — so this is
a normal per-symbol FeatureGroup (runs per shard, each shard owns its symbols' filings), UNLIKE turbulence.
It is NOT a ReductionGroup (the windows are calendar-DAY counts off an event table, not minute-bar folds),
so the incremental engine does not touch it.

================================================================================================
REFERENCE compute() — the next cycle lifts this into quantlib/features/groups/edgar_filing_frequency.py,
adds @register + the InputSpec(name="filings", ...), wires load_filings per above, versions it, regenerates
the catalog, and adds the parity test. Sketch (per-symbol point-in-time join, minute grid x filing table):

    import polars as pl
    from quantlib.features.base import BatchContext, FeatureGroup, FeatureSpec, FeatureType, InputSpec

    COUNT_WINDOWS_D = (7, 30, 90)
    MAJOR_FORMS = {"8-K": "8k", "10-Q": "10q", "10-K": "10k", "4": "form4"}

    class EdgarFilingFrequencyGroup(FeatureGroup):
        name = "edgar_filing_frequency"
        version = "1.0.0"
        owner = "modeller"
        type = FeatureType.EVENT  # (or the appropriate FeatureType for an event-clock group)
        inputs = (
            InputSpec(name="minute_agg", columns=("symbol", "minute")),
            InputSpec(name="filings", columns=("symbol", "form_type", "available_at")),
        )

        def compute(self, ctx: BatchContext) -> pl.DataFrame:
            keys = ctx.frame("minute_agg").select(["symbol", "minute"])
            filings = ctx.frame("filings").select(["symbol", "form_type", "available_at"])
            # point-in-time join: for each (symbol, minute) cell, the filings with available_at <= minute.
            # A non-equi as-of style join. For the count windows, join the symbol's filings onto its minute
            # grid and aggregate with available_at in (minute - Nd, minute]. minutes_since_last = the max
            # available_at <= minute, differenced. (Vectorize via a join_asof on available_at + range filters
            # per window, grouped by (symbol, minute) — see the per-symbol event-clock pattern.)
            ...  # the next cycle implements + parity-tests this; the math is a standard as-of count.

================================================================================================
The hard part is DONE here: the data is verified present + rich, the parity contract is nailed
(available_at<=minute, fixed-at-first-sight), the wiring touch-points are enumerated against the real loader
/ materialize / capture code, and the one genuine design decision (intraday filing arrival -> per-minute
trailing-window re-query on the reduce path, NOT a frozen startup snapshot) is surfaced with a recommendation.
"""
