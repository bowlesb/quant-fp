"""Unit tests for the EDGAR ingestor's pure parsing/mapping logic — runnable WITHOUT network or DB.

Gates Phase 1 of the EDGAR collector (docs/EDGAR_INGESTION.md):
  * Atom <entry> -> filing dict with the THREE timestamps, and the parity contract that the live feed
    sets available_at (the <updated> instant) but NOT filed_at (the conflation the old code made).
  * CIK->ticker mapping from a company_tickers.json payload.
  * submissions-API backfill keeps the same separation and flags lower-confidence available_at.

The ingestor module is import-safe (DB config is lazy in db_kwargs()), so these import it directly and
feed sample payloads — no SEC requests, no Postgres.
"""

import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

SERVICE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "edgar"
)
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

import main as edgar  # noqa: E402

# Mirrors the REAL SEC current-filings Atom feed: titles end with a ROLE suffix ('(Filer)' / '(Issuer)'
# / '(Reporting)' / '(Subject)') AFTER the '(CIK)' group, and the look-ahead-safe CIK lives in the
# '<link href>' path '.../edgar/data/<CIK>/...'. The old fixtures put a bare '(CIK)' at end-of-title,
# which the title-only extractor matched — but the live feed never does, so every live insert hit
# cik=NULL. These fixtures reproduce the live shape.
SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Latest Filings</title>
  <entry>
    <title>8-K - APPLE INC (0000320193) (Filer)</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000077/0000320193-26-000077-index.htm"/>
    <summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-06-14 &lt;b&gt;AccNo:&lt;/b&gt; 0000320193-26-000077 &lt;b&gt;Size:&lt;/b&gt; 12 KB</summary>
    <updated>2026-06-14T13:30:05-04:00</updated>
    <category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000077</id>
  </entry>
  <entry>
    <title>4 - SOME OBSCURE CO (0009999999) (Reporting)</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/9999999/000099999926000001/0009999999-26-000001-index.htm"/>
    <summary type="html">&lt;b&gt;AccNo:&lt;/b&gt; 0009999999-26-000001</summary>
    <updated>2026-06-14T13:31:00Z</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0009999999-26-000001</id>
  </entry>
  <entry>
    <title>424B2 - BofA Finance LLC (0001682472) (Filer)</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/1682472/000191870426016700/0001918704-26-016700-index.htm"/>
    <summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-06-14 &lt;b&gt;AccNo:&lt;/b&gt; 0001918704-26-016700 &lt;b&gt;Size:&lt;/b&gt; 300 KB</summary>
    <updated>2026-06-14T13:32:00-04:00</updated>
    <category scheme="https://www.sec.gov/" label="form type" term="424B2"/>
    <id>urn:tag:sec.gov,2008:accession-number=0001918704-26-016700</id>
  </entry>
</feed>
"""

# 0009999999 + 0001682472 deliberately UNMAPPED -> symbol must be None, but the row is KEPT (cik set).
CIK_MAP = {"0000320193": "AAPL"}


def test_parse_company_tickers() -> None:
    payload = {
        "0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."},
        "1": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
    }
    mapping = edgar.parse_company_tickers(payload)
    assert mapping["0000320193"] == "AAPL"  # zero-padded key, upper-cased ticker
    assert mapping["0001318605"] == "TSLA"


def test_normalize_cik() -> None:
    assert edgar.normalize_cik("320193") == "0000320193"
    assert edgar.normalize_cik("0000320193") == "0000320193"


def test_parse_atom_updated_is_utc() -> None:
    parsed = edgar.parse_atom_updated("2026-06-14T13:30:05-04:00")
    assert parsed == datetime(2026, 6, 14, 17, 30, 5, tzinfo=timezone.utc)
    assert edgar.parse_atom_updated("2026-06-14T13:31:00Z") == datetime(
        2026, 6, 14, 13, 31, 0, tzinfo=timezone.utc
    )


def test_parse_atom_entry_three_timestamps_and_parity() -> None:
    root = ET.fromstring(SAMPLE_FEED)
    entry = root.findall("atom:entry", edgar.ATOM_NS)[0]
    filing = edgar.parse_atom_entry(entry, CIK_MAP)
    assert filing is not None
    assert filing["accession_number"] == "0000320193-26-000077"
    assert filing["cik"] == "0000320193"
    assert filing["symbol"] == "AAPL"
    assert filing["form_type"] == "8-K"
    assert filing["company_name"] == "APPLE INC"
    # THE PARITY CRUX: available_at is the <updated> dissemination instant; filed_at/accepted_at are
    # NOT set from the live feed (the old code conflated <updated> into filed_at — we must not).
    assert filing["available_at"] == datetime(
        2026, 6, 14, 17, 30, 5, tzinfo=timezone.utc
    )
    assert filing["available_at_source"] == "atom_feed"
    assert filing["filed_at"] is None
    assert filing["accepted_at"] is None
    assert filing["source"] == "stream"


def test_parse_atom_feed_keeps_unmapped_cik() -> None:
    filings = edgar.parse_atom_feed(SAMPLE_FEED, CIK_MAP)
    assert len(filings) == 3
    unmapped = [f for f in filings if f["cik"] == "0009999999"][0]
    # Unmapped CIK is KEPT with symbol=None (never dropped) — mapping may resolve later.
    assert unmapped["symbol"] is None
    assert unmapped["accession_number"] == "0009999999-26-000001"
    assert unmapped["available_at"] == datetime(
        2026, 6, 14, 13, 31, 0, tzinfo=timezone.utc
    )
    # EVERY parsed filing has a non-null CIK (the NOT-NULL column that broke the live insert).
    assert all(filing["cik"] is not None for filing in filings)


def test_extract_cik_from_link_path_with_role_suffix() -> None:
    # The live-feed shape the old extractor missed: title ends with '(CIK) (ROLE)', so the CIK is NOT
    # at end-of-string; the link path '.../edgar/data/<CIK>/...' is the robust source.
    title = "424B2 - BofA Finance LLC (0001682472) (Filer)"
    link = "https://www.sec.gov/Archives/edgar/data/1682472/000191870426016700/0001918704-26-016700-index.htm"
    assert edgar.extract_cik(title, "", link) == "0001682472"
    # Link path wins even when the title has no parsable CIK at all.
    assert edgar.extract_cik("no cik here", "", link) == "0001682472"
    # Title fallback (with trailing role suffix) when there is no usable link.
    assert edgar.extract_cik(title, "", None) == "0001682472"
    # CIK= query-param fallback (e.g. the getcompany link form).
    assert (
        edgar.extract_cik(
            "x", "", "https://www.sec.gov/cgi-bin/browse-edgar?CIK=0000320193"
        )
        == "0000320193"
    )
    # No extractable CIK anywhere -> None (caller SKIPs the row rather than inserting null cik).
    assert edgar.extract_cik("no cik", "", "https://www.sec.gov/x") is None


def test_parse_atom_entry_424b2_cik_from_link() -> None:
    # The exact form that failed live: 424B2 with the CIK only in the link path (and not in CIK_MAP).
    root = ET.fromstring(SAMPLE_FEED)
    entry = root.findall("atom:entry", edgar.ATOM_NS)[2]
    filing = edgar.parse_atom_entry(entry, CIK_MAP)
    assert filing is not None
    assert filing["form_type"] == "424B2"
    assert filing["cik"] == "0001682472"  # extracted + normalized from the link path
    assert (
        filing["symbol"] is None
    )  # legitimately unmapped — ticker null is OK, cik is NOT
    assert filing["company_name"] == "BofA Finance LLC"
    assert filing["accession_number"] == "0001918704-26-016700"


def test_form_filter_restricts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(edgar, "FORM_FILTER", {"8-K"})
    filings = edgar.parse_atom_feed(SAMPLE_FEED, CIK_MAP)
    assert len(filings) == 1
    assert filings[0]["form_type"] == "8-K"


def test_extract_accession_fallbacks() -> None:
    assert (
        edgar.extract_accession("<b>AccNo:</b> 0000320193-26-000077", "")
        == "0000320193-26-000077"
    )
    assert (
        edgar.extract_accession(
            "", "urn:tag:sec.gov,2008:accession-number=0000320193-26-000077"
        )
        == "0000320193-26-000077"
    )
    assert edgar.extract_accession("no accession here", "") is None


SAMPLE_SUBMISSIONS = {
    "name": "APPLE INC",
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-26-000050", "0000320193-26-000049"],
            "form": ["10-Q", "4"],
            "filingDate": ["2026-05-02", "2026-05-01"],
            "acceptanceDateTime": [
                "2026-05-02T16:30:11.000Z",
                "2026-05-01T18:05:00.000Z",
            ],
            "primaryDocument": ["aapl-20260502.htm", "form4.xml"],
        }
    },
}


def test_parse_submissions_flags_lower_confidence() -> None:
    filings = edgar.parse_submissions(
        SAMPLE_SUBMISSIONS, "0000320193", {"0000320193": "AAPL"}
    )
    assert len(filings) == 2
    tenq = filings[0]
    assert tenq["accession_number"] == "0000320193-26-000050"
    assert tenq["symbol"] == "AAPL"
    assert tenq["form_type"] == "10-Q"
    # Backfill keeps the separation: available_at = acceptanceDateTime, filed_at = company filingDate.
    assert tenq["available_at"] == datetime(2026, 5, 2, 16, 30, 11, tzinfo=timezone.utc)
    assert tenq["accepted_at"] == datetime(2026, 5, 2, 16, 30, 11, tzinfo=timezone.utc)
    assert tenq["filed_at"] == datetime(2026, 5, 2, 0, 0, 0, tzinfo=timezone.utc)
    # Lower-confidence flag for deep history (not the live feed moment).
    assert tenq["available_at_source"] == "submissions_accepted"
    assert tenq["source"] == "backfill"
    assert "edgar/data/320193/000032019326000050/aapl-20260502.htm" in str(tenq["link"])


def test_parse_filing_date_midnight_utc() -> None:
    assert edgar.parse_filing_date("2026-05-02") == datetime(
        2026, 5, 2, tzinfo=timezone.utc
    )
