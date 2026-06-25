"""External-frame REFERENCE feature groups ported to the ``CleanEngine`` interface (news_sentiment,
edgar_filing_frequency) + the shared session-event-tape plumbing.

These two groups are POINT-IN-TIME EVENT-TAPE groups: intraday-invariant in shape (no carried minute-fold
state) but driven by a per-session snapshot of an EVENT tape (news articles / SEC filings), not the rolling
bar buffer and not a fixed daily matrix. Each (symbol, minute) cell is a pure function of that symbol's events
with ``available_at <= minute`` (the look-ahead-safe gate) plus trailing-window aggregates. ``available_at`` is
fixed at first sight, so the gated set at any minute is identical live vs backfill — parity-true by
construction (the clean engine's backfill==replay invariant is trivially met: there is no fold, only a re-gate
at each minute).

SESSION EVENT-TAPE SCHEMA (a third session-snapshot kind alongside the daily matrices, built once per session):
  A ragged per-symbol event tape stored CSR-style — one flat array of event fields sorted by symbol then
  ``available_at``, plus a per-symbol offset index into it (so symbol ``i``'s events are the flat slice
  ``[off[i]:off[i+1]]``, already ascending in ``available_at``):
    ``session["<tape>_at"]``     : ``(n_events,)`` int64 ``available_at`` epoch-seconds, sorted within each symbol.
    ``session["<tape>_off"]``    : ``(n_symbols + 1,)`` int64 CSR offsets (``off[i]..off[i+1]`` = symbol i's slice).
    ``session["<tape>_<field>"]``: ``(n_events,)`` payload arrays parallel to ``_at`` (news: ``sentiment``;
        edgar: ``form`` as an int code, see ``_EDGAR_FORM_CODE``).
  ``<tape>`` is ``news`` / ``edgar``. A symbol with no events has ``off[i] == off[i+1]`` (an empty slice → count
  features 0, mean/recency NaN). Absent tape → the group emits its all-empty defaults.
The current minute is ``window.minute_epoch`` (epoch-seconds); a trailing window of ``W`` minutes/days gates on
``(minute − W, minute]`` (strict lower, inclusive upper — the legacy ``available_at > lower & available_at <=
minute``).
"""

from __future__ import annotations

import numpy as np

from quantlib.features.clean_engine import Window

_NEWS_WINDOWS_M: dict[str, int] = {"60m": 60, "1d": 1440, "7d": 10080}
_NEWS_COUNT_WINDOWS_M: dict[str, int] = {"60m": 60, "1d": 1440}

_EDGAR_COUNT_WINDOWS_D: tuple[int, ...] = (7, 30, 90)
# SEC form_type label -> (feature suffix, int code used in the session tape's ``edgar_form`` payload).
_EDGAR_FORMS: dict[str, str] = {"8-K": "8k", "10-Q": "10q", "10-K": "10k", "4": "form4"}
_EDGAR_FORM_CODE: dict[str, int] = {"8-K": 0, "10-Q": 1, "10-K": 2, "4": 3}
_EDGAR_BURST_BASELINE_D = 365
_EDGAR_BURST_WINDOW_D = 7
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_DAY = 86400


def _symbol_slices(off: np.ndarray, idx: int) -> slice:
    """The flat-array slice for symbol ``idx`` from the CSR offsets (``off[idx]..off[idx+1]``)."""
    return slice(int(off[idx]), int(off[idx + 1]))


class NewsSentimentClean:
    """SESSION EVENT-TAPE: per-symbol baseline-sentiment intensity/abnormality off the news tape, point-in-time
    as of the minute. news_sentiment_mean_{60m,1d,7d}, news_sentiment_sum_{60m,1d}, news_count_{60m,1d},
    news_sentiment_last, news_minutes_since_last. Reads the ``news`` CSR tape from ``window.session``. Legacy:
    ``NewsSentimentGroup``."""

    name = "news_sentiment"
    input_cols = ()
    feature_names = (
        tuple(f"news_sentiment_mean_{s}" for s in _NEWS_WINDOWS_M)
        + tuple(f"news_sentiment_sum_{s}" for s in _NEWS_COUNT_WINDOWS_M)
        + tuple(f"news_count_{s}" for s in _NEWS_COUNT_WINDOWS_M)
        + ("news_sentiment_last", "news_minutes_since_last")
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        out: dict[str, np.ndarray] = {name: np.full(n, np.nan) for name in self.feature_names}
        for suffix in _NEWS_COUNT_WINDOWS_M:
            out[f"news_count_{suffix}"] = np.zeros(n)
            out[f"news_sentiment_sum_{suffix}"] = np.zeros(n)
        at = window.session.get("news_at")
        off = window.session.get("news_off")
        sentiment = window.session.get("news_sentiment")
        if at is None or off is None or sentiment is None:
            return out
        now = int(window.minute_epoch)
        for i in range(n):
            sl = _symbol_slices(off, i)
            sym_at = at[sl]
            if sym_at.size == 0:
                continue
            gated = sym_at <= now  # available_at <= minute (point-in-time)
            if not gated.any():
                continue
            sym_sent = sentiment[sl]
            for suffix, minutes in _NEWS_WINDOWS_M.items():
                lower = now - minutes * _SECONDS_PER_MINUTE
                in_win = gated & (sym_at > lower)  # (minute − W, minute]
                if in_win.any():
                    out[f"news_sentiment_mean_{suffix}"][i] = float(sym_sent[in_win].mean())
            for suffix, minutes in _NEWS_COUNT_WINDOWS_M.items():
                lower = now - minutes * _SECONDS_PER_MINUTE
                in_win = gated & (sym_at > lower)
                out[f"news_sentiment_sum_{suffix}"][i] = float(sym_sent[in_win].sum())
                out[f"news_count_{suffix}"][i] = float(in_win.sum())
            # most-recent article as of the minute (the tape is sorted ascending within a symbol).
            last_pos = np.nonzero(gated)[0][-1]
            out["news_sentiment_last"][i] = float(sym_sent[last_pos])
            out["news_minutes_since_last"][i] = float((now - int(sym_at[last_pos])) // _SECONDS_PER_MINUTE)
        return out


class EdgarFilingFrequencyClean:
    """SESSION EVENT-TAPE: per-symbol SEC filing frequency/timing/form-type features off the filings tape,
    point-in-time as of the minute. edgar_filing_count_{7,30,90}d, edgar_minutes_since_last_filing,
    edgar_minutes_since_last_8k, edgar_count_{8k,10q,10k,form4}_90d, edgar_filing_burst. Reads the ``edgar`` CSR
    tape (``edgar_at`` / ``edgar_off`` / ``edgar_form`` int codes) from ``window.session``. Legacy:
    ``EdgarFilingFrequencyGroup``."""

    name = "edgar_filing_frequency"
    input_cols = ()
    feature_names = (
        tuple(f"edgar_filing_count_{w}d" for w in _EDGAR_COUNT_WINDOWS_D)
        + ("edgar_minutes_since_last_filing", "edgar_minutes_since_last_8k")
        + tuple(f"edgar_count_{s}_90d" for s in _EDGAR_FORMS.values())
        + ("edgar_filing_burst",)
    )

    def compute(self, window: Window) -> dict[str, np.ndarray]:
        n = window.n
        out: dict[str, np.ndarray] = {name: np.full(n, np.nan) for name in self.feature_names}
        for w in _EDGAR_COUNT_WINDOWS_D:
            out[f"edgar_filing_count_{w}d"] = np.zeros(n)
        for suffix in _EDGAR_FORMS.values():
            out[f"edgar_count_{suffix}_90d"] = np.zeros(n)
        at = window.session.get("edgar_at")
        off = window.session.get("edgar_off")
        form = window.session.get("edgar_form")
        if at is None or off is None or form is None:
            return out
        now = int(window.minute_epoch)
        code_8k = _EDGAR_FORM_CODE["8-K"]
        for i in range(n):
            sl = _symbol_slices(off, i)
            sym_at = at[sl]
            if sym_at.size == 0:
                continue
            gated = sym_at <= now
            if not gated.any():
                continue
            sym_form = form[sl]
            for w in _EDGAR_COUNT_WINDOWS_D:
                lower = now - w * _SECONDS_PER_DAY
                out[f"edgar_filing_count_{w}d"][i] = float((gated & (sym_at > lower)).sum())
            last_pos = np.nonzero(gated)[0][-1]
            out["edgar_minutes_since_last_filing"][i] = float(
                (now - int(sym_at[last_pos])) // _SECONDS_PER_MINUTE
            )
            gated_8k = gated & (sym_form == code_8k)
            if gated_8k.any():
                last_8k = np.nonzero(gated_8k)[0][-1]
                out["edgar_minutes_since_last_8k"][i] = float(
                    (now - int(sym_at[last_8k])) // _SECONDS_PER_MINUTE
                )
            lower_90 = now - 90 * _SECONDS_PER_DAY
            in_90 = gated & (sym_at > lower_90)
            for form_label, suffix in _EDGAR_FORMS.items():
                code = _EDGAR_FORM_CODE[form_label]
                out[f"edgar_count_{suffix}_90d"][i] = float((in_90 & (sym_form == code)).sum())
            recent = float((gated & (sym_at > now - _EDGAR_BURST_WINDOW_D * _SECONDS_PER_DAY)).sum())
            baseline = float((gated & (sym_at > now - _EDGAR_BURST_BASELINE_D * _SECONDS_PER_DAY)).sum())
            expected = baseline * (_EDGAR_BURST_WINDOW_D / _EDGAR_BURST_BASELINE_D)
            if expected > 0:
                out["edgar_filing_burst"][i] = recent / expected
        return out
