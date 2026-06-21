"""The baseline deterministic finance-sentiment scorer (quantlib.data.news_sentiment).

These pin the contract that makes the stored ``sentiment`` field parity-stable: the score is a PURE,
DETERMINISTIC, BOUNDED function of an article's headline+summary text and the frozen lexicon — identical on
every host, with no model download or RNG. (The feature-group / parity tests live in
tests/test_fp_news_sentiment_feature.py.)"""
from __future__ import annotations

from quantlib.data.news_sentiment import MODEL_VERSION, score_article, score_text


def test_neutral_text_scores_zero() -> None:
    """No polarity word present → 0.0 (neutral), never NaN or an error."""
    assert score_text("The company held its annual shareholder meeting on Tuesday.") == 0.0
    assert score_text("") == 0.0
    assert score_article(None, None) == 0.0


def test_positive_and_negative_are_signed_and_bounded() -> None:
    assert score_text("shares surge to record highs on strong growth") > 0.0
    assert score_text("stock plunges on fraud lawsuit and weak guidance") < 0.0
    # the score is the normalized net polarity in [-1, 1]
    for text in ("surge rally beats", "plunge crash fraud", "surge plunge", "neutral filler"):
        assert -1.0 <= score_text(text) <= 1.0


def test_all_positive_is_plus_one_all_negative_is_minus_one() -> None:
    assert score_text("surge rally upgrade") == 1.0
    assert score_text("plunge crash downgrade") == -1.0


def test_balanced_polarity_is_zero() -> None:
    """One positive + one negative distinct word → (1 - 1) / 2 = 0.0."""
    assert score_text("surge plunge") == 0.0


def test_whole_word_matching_not_substring() -> None:
    """A polarity token must match as a whole word, not inside a larger word: 'gains' must not fire inside
    'bargains', so a sentence with only 'bargains' is neutral."""
    assert score_text("the store offered bargains today") == 0.0
    assert score_text("the stock gains today") > 0.0


def test_distinct_words_counted_once_not_frequency() -> None:
    """Repeating a word does not inflate the score — the score balances DISTINCT positive vs negative signals,
    so 'surge surge surge' is still a pure-positive +1.0, same as a single 'surge'."""
    assert score_text("surge surge surge") == 1.0
    assert score_text("surge surge plunge") == 0.0  # 1 distinct pos, 1 distinct neg


def test_deterministic_and_reproducible() -> None:
    """The same text always scores the same value — the property the stored field relies on."""
    text = "Company beats earnings, raises guidance; record revenue but a looming lawsuit warns of risk"
    assert score_text(text) == score_text(text)


def test_score_article_combines_headline_and_summary() -> None:
    """headline + summary are both scored; either may be missing."""
    assert score_article("surge", None) == score_article(None, "surge") == 1.0
    assert score_article("surge", "plunge") == 0.0


def test_model_version_is_stamped_constant() -> None:
    assert isinstance(MODEL_VERSION, str) and MODEL_VERSION
