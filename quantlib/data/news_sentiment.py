"""Baseline deterministic finance-sentiment scorer for the ``/store/news`` tape — Ben's first news
featurization step (news streams 24/7 but we attached ZERO sentiment).

The score is a LEXICON net-polarity over an article's ``headline`` + ``summary`` ONLY (never the price tape,
never look-ahead): ``(n_positive - n_negative) / (n_positive + n_negative)`` in ``[-1, +1]``, ``0.0`` when no
polarity word matches. It is:

  * **Deterministic + reproducible** — a pure function of the text and the frozen lexicon (``MODEL_VERSION``);
    the same article always scores the same value, on every host, with no model download or RNG. That is what
    makes a stored ``sentiment`` field PARITY-STABLE: a live-captured article and the same article seen by
    backfill carry the IDENTICAL score, because the score depends only on text that is identical on both sides
    (``headline``/``summary`` are the Alpaca-supplied content, not our arrival-time provenance).
  * **CPU-cheap** — set membership over two word sets + a few multi-word phrase scans; microseconds per
    article, so scoring at ingest never throttles the news_capture flush.
  * **Point-in-time SAFE** — computed from the article's OWN headline/summary at first sight and stamped onto
    the row, so it is fixed-at-first-sight exactly like ``available_at``. No future information enters it.

The lexicon is ported from the prior repo's hand-curated finance news word lists (the ``POSITIVE_WORDS`` /
``NEGATIVE_WORDS`` tier of its keyword feature module), the same ``(pos - neg) / total`` normalization. This is
the BASELINE; a heavier FinBERT / GPU sentiment pass is a FUTURE upgrade tier (a different ``MODEL_VERSION`` +
a separate parity story — explicitly out of scope here and NOT on the fast ingest path).

``MODEL_VERSION`` is stamped alongside each score so a future lexicon change (or a FinBERT upgrade) is
distinguishable in the store and can be selectively re-scored without ambiguity. Bump it whenever the lexicon
or the scoring math changes.
"""
from __future__ import annotations

MODEL_VERSION = "lexicon-v1"

# Hand-curated finance-news polarity words (lowercased, whole-token or phrase substrings). Ported from the
# prior repo's news keyword module POSITIVE_WORDS / NEGATIVE_WORDS tier — the deterministic baseline before
# any ML. Multi-word phrases (e.g. "raises guidance") are matched as substrings; single words are matched as
# whole tokens so "gains" does not match inside "bargains".
POSITIVE_WORDS: frozenset[str] = frozenset(
    {
        "approval",
        "approved",
        "awarded",
        "beat",
        "beats",
        "best",
        "breakthrough",
        "bullish",
        "clears",
        "exceeds",
        "expands",
        "expansion",
        "gain",
        "gains",
        "growing",
        "growth",
        "highest",
        "innovative",
        "jump",
        "jumps",
        "optimistic",
        "outperform",
        "raised",
        "raises",
        "rallies",
        "rally",
        "record",
        "robust",
        "soar",
        "soars",
        "solid",
        "spike",
        "spikes",
        "strong",
        "successful",
        "surge",
        "surges",
        "surpasses",
        "tops",
        "upbeat",
        "upgrade",
        "upgraded",
        "wins",
    }
)

NEGATIVE_WORDS: frozenset[str] = frozenset(
    {
        "bearish",
        "concern",
        "concerns",
        "contraction",
        "contracts",
        "crash",
        "crashes",
        "cuts",
        "decline",
        "declining",
        "disappointing",
        "disappoints",
        "downgrade",
        "downgraded",
        "drop",
        "drops",
        "fail",
        "failed",
        "fails",
        "fall",
        "falls",
        "fraud",
        "investigation",
        "lawsuit",
        "lowered",
        "lowers",
        "lowest",
        "miss",
        "misses",
        "pessimistic",
        "plunge",
        "plunges",
        "rejected",
        "rejection",
        "scandal",
        "sink",
        "sinks",
        "sluggish",
        "sued",
        "tumble",
        "tumbles",
        "underperform",
        "warning",
        "warns",
        "weak",
        "worst",
    }
)

# A phrase (multi-word) entry is matched as a raw substring; a single-token entry is matched against the
# tokenized word set so it never matches inside a larger word. Split the lexicons once at import time.
_POSITIVE_PHRASES: frozenset[str] = frozenset(word for word in POSITIVE_WORDS if " " in word)
_POSITIVE_TOKENS: frozenset[str] = frozenset(word for word in POSITIVE_WORDS if " " not in word)
_NEGATIVE_PHRASES: frozenset[str] = frozenset(word for word in NEGATIVE_WORDS if " " in word)
_NEGATIVE_TOKENS: frozenset[str] = frozenset(word for word in NEGATIVE_WORDS if " " not in word)

# Tokenization: lowercase, then split on any run of non-alphanumeric characters. Whole-token matching means
# "gains" matches the token "gains" but not the substring inside "campaigns".
_TOKEN_SPLIT = str.maketrans({char: " " for char in "\t\n\r\"'`!?.,;:()[]{}<>/\\|@#$%^&*+=~" + "-_"})


def _tokens(text: str) -> set[str]:
    return set(text.lower().translate(_TOKEN_SPLIT).split())


def _count_hits(text_lower: str, tokens: set[str], phrases: frozenset[str], single: frozenset[str]) -> int:
    """Number of distinct lexicon entries present: single tokens matched against ``tokens`` (whole-word),
    phrases matched as substrings of the lowercased text."""
    hits = len(tokens & single)
    hits += sum(1 for phrase in phrases if phrase in text_lower)
    return hits


def score_text(text: str) -> float:
    """Net lexicon polarity of ``text`` in ``[-1.0, +1.0]``: ``(pos - neg) / (pos + neg)``, ``0.0`` when no
    polarity word is present. Distinct lexicon entries are counted once each (set membership), so a word
    repeated in the text does not inflate the score — the score is a balance of how many positive vs negative
    distinct signals the headline/summary carries, not a raw word frequency."""
    if not text:
        return 0.0
    text_lower = text.lower()
    tokens = _tokens(text)
    pos = _count_hits(text_lower, tokens, _POSITIVE_PHRASES, _POSITIVE_TOKENS)
    neg = _count_hits(text_lower, tokens, _NEGATIVE_PHRASES, _NEGATIVE_TOKENS)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def score_article(headline: str | None, summary: str | None) -> float:
    """Baseline sentiment for an article from its ``headline`` + ``summary`` ONLY (no other field, no
    look-ahead). Either may be missing (None/empty) — the present text is scored; both empty → ``0.0``."""
    text = f"{headline or ''} {summary or ''}".strip()
    return score_text(text)
