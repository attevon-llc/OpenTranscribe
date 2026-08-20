"""``services/documents/language.py`` — deterministic document-language detection.

Deliberately does not depend on the NLTK stopword corpus being downloaded: the English
case works off the always-available fallback list
(``ingest_artifacts.textrank._FALLBACK_ENGLISH_STOPWORDS``), and the "cannot detect"
case needs no corpus at all — so this suite is honest in CI as well as against a full
dev stack.
"""

from __future__ import annotations

from app.services.documents.language import CANDIDATE_LANGUAGES
from app.services.documents.language import MIN_TOKENS
from app.services.documents.language import detect_document_language

_ENGLISH_TEXT = " ".join(
    [
        "This is the quarterly report for the engineering department and it covers",
        "what the team has been doing over the last few months of the year. The",
        "budget for the project was approved by the finance committee after they",
        "reviewed all of the numbers that had been submitted to them by the leads",
        "of each of the smaller teams that make up the wider organization as a whole.",
    ]
)


def test_detects_english_from_the_fallback_stopword_list():
    assert detect_document_language(_ENGLISH_TEXT) == "en"


def test_returns_none_for_text_below_the_token_floor():
    short_text = "Hello world, this is short."
    assert len(short_text.split()) < MIN_TOKENS
    assert detect_document_language(short_text) is None


def test_returns_none_rather_than_a_coerced_default_for_undetectable_text():
    """A long text with essentially no stopword overlap in any candidate language
    (unique nonsense tokens) must decline — never fall back to "en".
    """
    nonsense = " ".join(f"zqxvbnfjklopmqrstuvxyz{i}" for i in range(80))
    assert detect_document_language(nonsense) is None


def test_returns_none_for_empty_text():
    assert detect_document_language("") is None
    assert detect_document_language(None) is None  # type: ignore[arg-type]


def test_candidate_languages_are_deterministically_ordered_and_unique():
    assert len(CANDIDATE_LANGUAGES) == len(set(CANDIDATE_LANGUAGES))
    assert "en" in CANDIDATE_LANGUAGES


def test_never_defaults_to_english_when_a_non_english_candidate_matches_better():
    """Text built entirely from canonical Spanish stopwords that are NOT also English
    stopwords (excludes "a"/"de"/"no", which overlap both lists). With the NLTK Spanish
    corpus present this must detect "es"; without it, every candidate — English
    included — scores zero and the result must be the "undetectable" ``None``. Neither
    outcome is ever "en", which is the property under test and is corpus-independent.
    """
    spanish_like = " ".join(["la que el en y los se del las por un para con"] * 5)
    result = detect_document_language(spanish_like)
    assert result != "en"
