"""EDGAR ingestor: real-time, point-in-time-correct SEC filing collection (Phase 1 — COLLECT only).

Design + rationale: docs/EDGAR_INGESTION.md. This is a LIGHTWEIGHT service (psycopg + httpx +
stdlib), mirroring services/scheduler — it does NOT pull in the heavy fp-dev feature stack. NO
features are computed here; we only grow a clean, deduped, look-ahead-safe `filings` store.

Two modes (selected by EDGAR_MODE):
  * "stream"   (default): poll the SEC current-filings Atom feed every ~5s, dedupe by accession,
                map CIK->ticker, and UPSERT each filing capturing available_at = the Atom <updated>
                instant (the moment the filing became publicly visible — the point-in-time field).
  * "backfill": walk the SEC submissions API (data.sec.gov/submissions/CIK{cik}.json) for history
                of a symbol list. available_at for historical filings is the submissions
                acceptanceDateTime, FLAGGED available_at_source='submissions_accepted' (lower
                confidence than the live feed — see EDGAR_INGESTION.md "the parity crux").

THE PARITY FIX (vs the prior project): the old code set filed_at = the Atom <updated> time,
conflating the company filing date with the public-dissemination time. Here they are kept SEPARATE:
filed_at is metadata; available_at is the look-ahead-safe field every future feature keys off.

All SEC HTTP goes through a token bucket (~4 rps) and carries the required User-Agent header (SEC
blocks requests without one).
"""

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

import httpx
import psycopg

from quantlib.sec_rate_limit import TokenBucket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("edgar")

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

EDGAR_ATOM_URL = os.environ.get(
    "EDGAR_ATOM_URL",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&company="
    "&dateb=&owner=include&count=100&output=atom",
)
COMPANY_TICKERS_URL = os.environ.get("EDGAR_TICKERS_URL", "https://www.sec.gov/files/company_tickers.json")
SUBMISSIONS_BASE = os.environ.get("EDGAR_SUBMISSIONS_BASE", "https://data.sec.gov/submissions")

POLL_SECONDS = int(os.environ.get("EDGAR_POLL_SECONDS", "5"))
SEC_MAX_RPS = float(os.environ.get("EDGAR_SEC_MAX_RPS", "4.0"))
USER_AGENT = os.environ.get("SEC_USER_AGENT", "quant-fp research ben.bowles@gmail.com")
CIK_REFRESH_SECONDS = int(os.environ.get("EDGAR_CIK_REFRESH_SECONDS", str(24 * 3600)))
HTTP_TIMEOUT = float(os.environ.get("EDGAR_HTTP_TIMEOUT", "30.0"))
EDGAR_MODE = os.environ.get("EDGAR_MODE", "stream")
BACKFILL_SYMBOLS = [
    s.strip().upper() for s in os.environ.get("EDGAR_BACKFILL_SYMBOLS", "").split(",") if s.strip()
]
# Form types we collect. Empty/"*" = collect ALL forms (Phase 1 is breadth-first — keep everything,
# filter at feature time). A comma list restricts to those forms.
_form_filter_env = os.environ.get("EDGAR_FORMS", "").strip()
FORM_FILTER: set[str] | None = (
    None if _form_filter_env in ("", "*") else {f.strip().upper() for f in _form_filter_env.split(",")}
)


def db_kwargs() -> dict[str, object]:
    """DB connection kwargs from the environment. A function (not a module constant) so the module
    imports cleanly in tests / byte-compile without DB_* present — the real job supplies them.
    """
    return {
        "host": os.environ["DB_HOST"],
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


def normalize_cik(cik: str) -> str:
    """SEC CIK as a zero-padded 10-digit string (the form company_tickers.json and the mapper use)."""
    return cik.strip().lstrip("0").zfill(10) if cik.strip().lstrip("0") else "0".zfill(10)


def parse_company_tickers(payload: dict[str, dict[str, object]]) -> dict[str, str]:
    """Build a CIK(10-digit)->ticker map from SEC company_tickers.json.

    Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}. Ported from the
    prior project's cik_mapper, reduced to the pure transform (no DB, no httpx) so it is unit-testable.
    """
    mapping: dict[str, str] = {}
    for entry in payload.values():
        cik = normalize_cik(str(entry["cik_str"]))
        ticker = str(entry["ticker"]).upper()
        mapping[cik] = ticker
    return mapping


def parse_atom_updated(text: str) -> datetime:
    """Parse an Atom <updated> timestamp (RFC-3339, e.g. '2026-06-14T13:30:05-04:00') to an
    aware UTC datetime. This instant IS available_at — when the filing became publicly visible.
    """
    parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_atom_entry(entry: ET.Element, cik_to_ticker: dict[str, str]) -> dict[str, object] | None:
    """Parse one Atom <entry> into a filing dict with the THREE timestamps.

    Returns None when the entry is unparseable or filtered out by FORM_FILTER. The returned dict is
    exactly what _upsert_filing writes. THE CRITICAL SEPARATION lives here:
      * available_at = the <updated> dissemination instant (look-ahead-safe point-in-time field)
      * filed_at     = NULL from the feed (the feed carries no separate filing date; the live feed
                       moment IS the available time — we do NOT copy it into filed_at, which is the
                       exact conflation the old code made). filed_at is filled only by backfill.
      * accepted_at  = NULL from the feed (only the submissions API exposes acceptanceDateTime).
    """
    title_elem = entry.find("atom:title", ATOM_NS)
    updated_elem = entry.find("atom:updated", ATOM_NS)
    summary_elem = entry.find("atom:summary", ATOM_NS)
    link_elem = entry.find("atom:link", ATOM_NS)
    id_elem = entry.find("atom:id", ATOM_NS)

    if title_elem is None or title_elem.text is None:
        return None
    if updated_elem is None or updated_elem.text is None:
        return None

    title = title_elem.text.strip()

    form_match = re.match(r"^([\w\-/. ]+?)\s+-\s+", title)
    if not form_match:
        return None
    form_type = form_match.group(1).strip().upper()
    if FORM_FILTER is not None and form_type not in FORM_FILTER:
        return None

    summary_text = summary_elem.text if (summary_elem is not None and summary_elem.text) else ""
    id_text = id_elem.text if (id_elem is not None and id_elem.text) else ""
    accession = extract_accession(summary_text, id_text)
    if accession is None:
        return None

    available_at = parse_atom_updated(updated_elem.text)

    link = link_elem.get("href") if link_elem is not None else None
    cik = extract_cik(title, id_text, link)
    if cik is None:
        # cik is NOT-NULL in the filings table; a row with no extractable CIK can't be inserted. The
        # live feed always carries the CIK in the link path (.../edgar/data/<CIK>/...) or the title,
        # so this is rare/malformed — skip it (don't fabricate a null-cik row that would fail the
        # UPSERT and stall the whole batch).
        logger.warning("skipping entry with no extractable CIK: title=%r link=%r", title, link)
        return None
    symbol = cik_to_ticker.get(cik)
    company_name = extract_company_name(title)

    return {
        "accession_number": accession,
        "cik": cik,
        "symbol": symbol,
        "form_type": form_type,
        "company_name": company_name,
        "filed_at": None,  # METADATA — the live feed does NOT carry a separate filing date
        "accepted_at": None,  # only the submissions API exposes acceptanceDateTime
        "available_at": available_at,
        "available_at_source": "atom_feed",
        "link": link,
        "source": "stream",
    }


def extract_accession(summary_text: str, id_text: str) -> str | None:
    """Pull the accession number from an Atom entry: primary source is the <summary> 'AccNo:' field;
    fallback is the <id> urn (urn:tag:sec.gov,2008:accession-number=...)."""
    match = re.search(r"AccNo:</b>\s*(\d{10}-\d{2}-\d{6})", summary_text)
    if match:
        return match.group(1)
    match = re.search(r"accession-number=(\d{10}-\d{2}-\d{6})", id_text)
    if match:
        return match.group(1)
    match = re.search(r"(\d{10}-\d{2}-\d{6})", summary_text)
    if match:
        return match.group(1)
    return None


def extract_cik(title: str, id_text: str, link: str | None = None) -> str | None:
    """CIK from the Atom entry, normalized to 10-digit zero-padded.

    Sources, most-robust first:
      1. the filing URL path '.../edgar/data/<CIK>/...' (the <link href>) — ALWAYS present in the live
         current-filings feed and unambiguous (this is the source the old title-only logic missed,
         which left cik=NULL and broke every insert).
      2. the <title> '(0001234567)' group — note the live feed appends a ROLE suffix after it,
         e.g. '424B2 - BofA Finance LLC (0001682472) (Filer)', so the CIK is NOT at end-of-string.
      3. the <id>/link 'CIK=...' query param.
    """
    if link:
        match = re.search(r"/edgar/data/(\d{1,10})(?:/|$)", link)
        if match:
            return normalize_cik(match.group(1))
    match = re.search(r"\((\d{1,10})\)", title)
    if match:
        return normalize_cik(match.group(1))
    for source_text in (id_text, link or ""):
        match = re.search(r"[?&]CIK=(\d{1,10})", source_text)
        if match:
            return normalize_cik(match.group(1))
    return None


def extract_company_name(title: str) -> str | None:
    """Filer name from the Atom <title>: '<form> - <COMPANY NAME> (CIK) (ROLE)'. The CIK group is
    followed by an optional role suffix ('(Filer)', '(Reporting)', '(Issuer)', '(Subject)'), so the
    name is everything between the ' - ' and the '(CIK)' group. Best-effort metadata."""
    match = re.match(r"^[\w\-/. ]+?\s+-\s+(.*?)\s*\(\d{1,10}\)", title.strip())
    if match:
        return match.group(1).strip()
    return None


def parse_atom_feed(feed_xml: str, cik_to_ticker: dict[str, str]) -> list[dict[str, object]]:
    """Parse a full Atom feed payload into a list of filing dicts (skipping unparseable/filtered)."""
    root = ET.fromstring(feed_xml)
    filings: list[dict[str, object]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        parsed = parse_atom_entry(entry, cik_to_ticker)
        if parsed is not None:
            filings.append(parsed)
    return filings


class CikMapper:
    """In-memory CIK(10-digit)->ticker map, refreshed from SEC company_tickers.json once/day.

    Ported from the prior project's cik_mapper but stripped to the in-memory cache + SEC refresh (no
    Postgres ticker table — this service only needs the lookup). Refresh is rate-limited + carries the
    User-Agent. The map is intentionally simple: an unmapped CIK yields None and the filing is KEPT
    with symbol=NULL (never dropped)."""

    def __init__(self, client: httpx.Client, rate_limiter: TokenBucket) -> None:
        self._client = client
        self._rate_limiter = rate_limiter
        self._cik_to_ticker: dict[str, str] = {}
        self._last_refresh_monotonic: float | None = None

    def get_ticker(self, cik: str | None) -> str | None:
        if cik is None:
            return None
        return self._cik_to_ticker.get(normalize_cik(cik))

    @property
    def mapping(self) -> dict[str, str]:
        return self._cik_to_ticker

    def needs_refresh(self) -> bool:
        if self._last_refresh_monotonic is None:
            return True
        return (time.monotonic() - self._last_refresh_monotonic) > CIK_REFRESH_SECONDS

    def maybe_refresh(self) -> None:
        if not self.needs_refresh():
            return
        self.refresh()

    def refresh(self) -> None:
        logger.info("refreshing CIK->ticker map from SEC company_tickers.json")
        with self._rate_limiter.acquire():
            response = self._client.get(COMPANY_TICKERS_URL)
            response.raise_for_status()
            payload = response.json()
        self._cik_to_ticker = parse_company_tickers(payload)
        self._last_refresh_monotonic = time.monotonic()
        logger.info("CIK map refreshed: %d tickers", len(self._cik_to_ticker))


UPSERT_SQL = """
INSERT INTO filings (
    accession_number, cik, symbol, form_type, company_name,
    filed_at, accepted_at, available_at, available_at_source, link, source
) VALUES (
    %(accession_number)s, %(cik)s, %(symbol)s, %(form_type)s, %(company_name)s,
    %(filed_at)s, %(accepted_at)s, %(available_at)s, %(available_at_source)s, %(link)s, %(source)s
)
ON CONFLICT (accession_number, available_at) DO UPDATE SET
    -- DEDUP + LATE-MAPPING FILL: never rewrite available_at (the point-in-time contract). Only fill in
    -- a symbol that was NULL (CIK mapped since), and let backfill add filed_at/accepted_at metadata.
    symbol      = COALESCE(filings.symbol, EXCLUDED.symbol),
    filed_at    = COALESCE(filings.filed_at, EXCLUDED.filed_at),
    accepted_at = COALESCE(filings.accepted_at, EXCLUDED.accepted_at),
    company_name = COALESCE(filings.company_name, EXCLUDED.company_name)
"""


def upsert_filings(conn: psycopg.Connection, filings: list[dict[str, object]]) -> int:
    """UPSERT a batch of filing dicts. Dedup is enforced by the PK + ON CONFLICT (idempotent)."""
    if not filings:
        return 0
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, filings)
    return len(filings)


def accessions_with_live_row(conn: psycopg.Connection, accessions: list[str]) -> set[str]:
    """Of ``accessions``, the subset already stored from the LIVE feed (available_at_source='atom_feed').

    The live atom ``<updated>`` dissemination instant is the AUTHORITATIVE ``available_at`` (the moment a
    real-time consumer could have known — see docs/EDGAR_INGESTION.md "the parity crux"). The backfill's
    ``acceptanceDateTime`` is an explicitly lower-confidence reconstruction used only for filings we never
    saw live. So when the live feed already captured an accession, a backfill row for it would NOT add a
    point-in-time-correct value — it would only double-row the filing under a second ``available_at``
    (~4h off, the ET-offset seam) and inflate the filing-frequency features. Backfill therefore skips
    those accessions and defers to the canonical live row.
    """
    if not accessions:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT accession_number FROM filings "
            "WHERE available_at_source = 'atom_feed' AND accession_number = ANY(%s)",
            (accessions,),
        )
        return {row[0] for row in cur.fetchall()}


SEAM_DUP_SELECT_SQL = """
WITH seam AS (
    SELECT accession_number
    FROM filings
    GROUP BY accession_number
    HAVING count(*) FILTER (WHERE available_at_source = 'atom_feed') > 0
       AND count(*) FILTER (WHERE available_at_source = 'submissions_accepted') > 0
)
SELECT accession_number, available_at
FROM filings
WHERE available_at_source = 'submissions_accepted'
  AND accession_number IN (SELECT accession_number FROM seam)
ORDER BY accession_number, available_at
"""


def find_seam_dup_rows(
    conn: psycopg.Connection,
) -> list[tuple[str, datetime]]:
    """The backfill (``submissions_accepted``) rows of accessions that ALSO have a live (``atom_feed``)
    row — i.e. the duplicate rows the cross-seam gap created. The live row is the canonical keeper; these
    are the rows the one-time dedup deletes. Read-only.
    """
    with conn.cursor() as cur:
        cur.execute(SEAM_DUP_SELECT_SQL)
        return [(str(row[0]), row[1]) for row in cur.fetchall()]


def delete_seam_dup_rows(conn: psycopg.Connection, rows: list[tuple[str, datetime]]) -> int:
    """Delete the given (accession_number, available_at) backfill duplicate rows. Idempotent: a re-run
    after a successful delete finds nothing to delete (the rows are gone) and returns 0. Targets each row
    by its full PK so it can NEVER touch the canonical live row (different available_at).
    """
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "DELETE FROM filings WHERE accession_number = %s AND available_at = %s "
            "AND available_at_source = 'submissions_accepted'",
            rows,
        )
        return cur.rowcount


def drop_seam_dups(conn: psycopg.Connection, filings: list[dict[str, object]]) -> list[dict[str, object]]:
    """Filter a BACKFILL batch down to filings whose accession is NOT already stored live.

    This closes the cross-seam double-row at ingest time (the #311 G4 gap): the live and backfill paths
    assign different ``available_at`` for the same accession, so the ``(accession_number, available_at)``
    PK does not dedupe across the seam. Deferring backfill to an existing live row keeps ONE canonical
    (authoritative) row per accession.
    """
    if not filings:
        return filings
    accessions = [str(filing["accession_number"]) for filing in filings]
    live = accessions_with_live_row(conn, accessions)
    if not live:
        return filings
    kept = [filing for filing in filings if str(filing["accession_number"]) not in live]
    skipped = len(filings) - len(kept)
    if skipped:
        logger.info("backfill: skipped %d filing(s) already captured live (seam dedup)", skipped)
    return kept


def poll_once(
    conn: psycopg.Connection,
    client: httpx.Client,
    cik_mapper: CikMapper,
    rate_limiter: TokenBucket,
) -> int:
    """One Atom-feed poll: refresh CIK map if due, fetch the feed (rate-limited), parse, UPSERT.
    Returns the number of filings written this poll."""
    cik_mapper.maybe_refresh()
    with rate_limiter.acquire():
        response = client.get(EDGAR_ATOM_URL)
        response.raise_for_status()
        feed_xml = response.text
    filings = parse_atom_feed(feed_xml, cik_mapper.mapping)
    written = upsert_filings(conn, filings)
    if written:
        logger.info("poll: %d filings upserted", written)
    return written


def parse_submissions(
    payload: dict[str, object], cik: str, cik_to_ticker: dict[str, str]
) -> list[dict[str, object]]:
    """Parse a data.sec.gov submissions payload into filing dicts (backfill mode).

    available_at for historical filings = acceptanceDateTime, flagged available_at_source=
    'submissions_accepted' (lower confidence than the live feed — see EDGAR_INGESTION.md). filed_at is
    the company filingDate (METADATA). This keeps the three-timestamp separation in backfill too.
    """
    filings_obj = payload["filings"]
    assert isinstance(filings_obj, dict)
    recent = filings_obj["recent"]
    assert isinstance(recent, dict)

    accessions = recent["accessionNumber"]
    forms = recent["form"]
    filing_dates = recent["filingDate"]
    acceptance_dts = recent["acceptanceDateTime"]
    primary_docs = recent["primaryDocument"]
    assert isinstance(accessions, list)
    assert isinstance(forms, list)
    assert isinstance(filing_dates, list)
    assert isinstance(acceptance_dts, list)
    assert isinstance(primary_docs, list)

    cik_norm = normalize_cik(cik)
    symbol = cik_to_ticker.get(cik_norm)
    company_obj = payload["name"] if "name" in payload else None
    company_name = str(company_obj) if company_obj is not None else None

    out: list[dict[str, object]] = []
    for accession, form, filing_date_str, acceptance_str, doc in zip(
        accessions, forms, filing_dates, acceptance_dts, primary_docs, strict=False
    ):
        form_type = str(form).upper()
        if FORM_FILTER is not None and form_type not in FORM_FILTER:
            continue
        accepted_at = parse_atom_updated(str(acceptance_str)) if acceptance_str else None
        if accepted_at is None:
            # No acceptance time -> no defensible available_at; skip rather than fabricate one.
            continue
        filed_at = parse_filing_date(str(filing_date_str)) if filing_date_str else None
        cik_digits = cik_norm.lstrip("0") or "0"
        accession_clean = str(accession).replace("-", "")
        link = f"https://www.sec.gov/Archives/edgar/data/{cik_digits}/{accession_clean}/{doc}"
        out.append(
            {
                "accession_number": str(accession),
                "cik": cik_norm,
                "symbol": symbol,
                "form_type": form_type,
                "company_name": company_name,
                "filed_at": filed_at,
                "accepted_at": accepted_at,
                "available_at": accepted_at,
                "available_at_source": "submissions_accepted",
                "link": link,
                "source": "backfill",
            }
        )
    return out


def parse_filing_date(date_str: str) -> datetime:
    """Company filingDate ('YYYY-MM-DD') -> midnight-UTC datetime. METADATA only (see the parity note)."""
    parsed_date: date = date.fromisoformat(date_str.strip())
    return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)


def backfill_symbol(
    conn: psycopg.Connection,
    client: httpx.Client,
    cik_mapper: CikMapper,
    rate_limiter: TokenBucket,
    symbol: str,
) -> int:
    """Backfill all recent submissions for one symbol via the submissions API. Returns count written."""
    cik = None
    for mapped_cik, mapped_ticker in cik_mapper.mapping.items():
        if mapped_ticker == symbol.upper():
            cik = mapped_cik
            break
    if cik is None:
        logger.warning("backfill: no CIK for %s", symbol)
        return 0
    url = f"{SUBMISSIONS_BASE}/CIK{cik}.json"
    with rate_limiter.acquire():
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    filings = parse_submissions(payload, cik, cik_mapper.mapping)
    filings = drop_seam_dups(conn, filings)
    written = upsert_filings(conn, filings)
    logger.info("backfill %s (CIK %s): %d filings upserted", symbol, cik, written)
    return written


def run_stream() -> None:
    """Live Atom-feed polling loop."""
    rate_limiter = TokenBucket(rate=SEC_MAX_RPS)
    client = httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    cik_mapper = CikMapper(client, rate_limiter)
    cik_mapper.refresh()
    logger.info("edgar stream starting: poll=%ds, rps=%.1f", POLL_SECONDS, SEC_MAX_RPS)
    with psycopg.connect(**db_kwargs(), autocommit=True) as conn:
        while True:
            try:
                poll_once(conn, client, cik_mapper, rate_limiter)
            except httpx.TimeoutException as exc:
                logger.warning("EDGAR poll timeout: %s", exc)
            except httpx.HTTPStatusError as exc:
                logger.warning("EDGAR poll HTTP %s", exc.response.status_code)
            except (psycopg.Error, ET.ParseError) as exc:
                logger.error("EDGAR poll error: %s", exc)
            time.sleep(POLL_SECONDS)


def run_backfill() -> None:
    """One-shot backfill of EDGAR_BACKFILL_SYMBOLS via the submissions API, then exit."""
    if not BACKFILL_SYMBOLS:
        logger.error("backfill mode requires EDGAR_BACKFILL_SYMBOLS")
        return
    rate_limiter = TokenBucket(rate=SEC_MAX_RPS)
    client = httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    cik_mapper = CikMapper(client, rate_limiter)
    cik_mapper.refresh()
    logger.info("edgar backfill starting: %d symbols", len(BACKFILL_SYMBOLS))
    total = 0
    with psycopg.connect(**db_kwargs(), autocommit=True) as conn:
        for symbol in BACKFILL_SYMBOLS:
            try:
                total += backfill_symbol(conn, client, cik_mapper, rate_limiter, symbol)
            except httpx.HTTPStatusError as exc:
                logger.warning("backfill %s HTTP %s", symbol, exc.response.status_code)
            except (psycopg.Error, KeyError) as exc:
                logger.error("backfill %s error: %s", symbol, exc)
    logger.info("edgar backfill done: %d filings total", total)


def main() -> None:
    if EDGAR_MODE == "backfill":
        run_backfill()
    else:
        run_stream()


if __name__ == "__main__":
    main()
