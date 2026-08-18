"""A missing NLTK corpus must degrade, never raise (issue #491).

NLTK corpora are fetched **at runtime, on first use**, from inside the
transcription and topic pipelines. ``nltk.download`` swallows its own network
errors and returns falsy, so on an airgapped or firewalled host the call either
hangs on a socket timeout or returns quietly and the caller finds the corpus
still missing one line later.

Two of the three download sites had a retry with **no second ``except``**, so
that "one line later" was an unhandled ``LookupError``:

* ``text_preprocessing._get_stopwords`` — raised out of ``preprocess_for_topics``
  into ``topic_extraction_service``, failing topic extraction outright;
* ``segment_dedup.split_sentences_nltk`` — worse, because **nothing up the stack
  catches it**: ``clean_segments`` calls it unguarded, so a missing corpus failed
  the whole TRANSCRIPTION rather than producing coarser segments.

Their neighbour ``_tokenize`` has had the correct nested shape all along; these
two simply never grew it.

The tests drive the real functions with the corpus made unreachable, rather than
asserting that a ``try`` exists — a structural test would pass against a bare
``except`` that swallowed the error and returned nothing useful.
"""

from __future__ import annotations

import pytest

from app.utils import segment_dedup
from app.utils import text_preprocessing
from app.utils.nltk_offline import nltk_downloads_permitted
from app.utils.nltk_offline import nltk_offline


@pytest.fixture(autouse=True)
def _clear_stopword_cache():
    """``_get_stopwords`` is ``lru_cache``d; one test's failure must not persist."""
    text_preprocessing._get_stopwords.cache_clear()
    yield
    text_preprocessing._get_stopwords.cache_clear()


# --------------------------------------------------------------------------- #
# The offline assertion itself                                                 #
# --------------------------------------------------------------------------- #


def test_offline_is_declared_by_exactly_one_and_only_the_documented_value(monkeypatch):
    """Mirrors ``HF_HUB_OFFLINE``: the string ``"1"``, nothing else.

    A truthy-string check would make ``NLTK_OFFLINE=0`` mean *offline*, which is
    the opposite of what an operator typing it intends.
    """
    monkeypatch.delenv("NLTK_OFFLINE", raising=False)
    assert nltk_offline() is False

    monkeypatch.setenv("NLTK_OFFLINE", "1")
    assert nltk_offline() is True

    for value in ("0", "", "true", "yes"):
        monkeypatch.setenv("NLTK_OFFLINE", value)
        assert nltk_offline() is False, f"NLTK_OFFLINE={value!r} must not mean offline"


def test_offline_refuses_the_download_and_says_which_step_was_skipped(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("NLTK_OFFLINE", "1")
    with caplog.at_level(logging.WARNING):
        assert nltk_downloads_permitted(corpus="the punkt tokenizer") is False

    message = " ".join(record.getMessage() for record in caplog.records)
    assert "punkt" in message, "the operator is not told WHICH corpus is missing"
    assert "download-models" in message, "the operator is not told what to run"


def test_online_permits_the_download(monkeypatch):
    """The control: without the assertion, nothing changes about today's behaviour."""
    monkeypatch.delenv("NLTK_OFFLINE", raising=False)
    assert nltk_downloads_permitted() is True


# --------------------------------------------------------------------------- #
# Topic extraction degrades                                                    #
# --------------------------------------------------------------------------- #


def _break_corpus(monkeypatch, target: str) -> list[str]:
    """Make the named corpus unreachable AND the download a no-op.

    Returns the list that records download attempts, so a test can assert the
    offline guard actually suppressed the network call.
    """
    import nltk

    attempts: list[str] = []

    def _failing_download(name, *args, **kwargs):
        attempts.append(name)
        return False  # what nltk.download really returns when it cannot fetch

    monkeypatch.setattr(nltk, "download", _failing_download)
    return attempts


def test_missing_stopwords_degrades_instead_of_raising(monkeypatch):
    """The defect: the retry had no second ``except``, so this raised.

    ``preprocess_for_topics`` calls this, and ``topic_extraction_service`` calls
    that — neither catches, so topic extraction failed outright on a deployment
    that had provisioned every model it was told to.
    """
    import nltk.corpus

    _break_corpus(monkeypatch, "stopwords")
    monkeypatch.setattr(
        nltk.corpus.stopwords, "words", lambda *a, **k: (_ for _ in ()).throw(LookupError("gone"))
    )

    words = text_preprocessing._get_stopwords()

    assert words, "the fallback returned nothing at all"
    assert "um" in words, "the transcript filler must survive a missing NLTK corpus"


def test_topic_preprocessing_still_produces_output_without_stopwords(monkeypatch):
    """End to end: the caller that used to blow up now returns usable text."""
    import nltk.corpus

    _break_corpus(monkeypatch, "stopwords")
    monkeypatch.setattr(
        nltk.corpus.stopwords, "words", lambda *a, **k: (_ for _ in ()).throw(LookupError("gone"))
    )

    result = text_preprocessing.preprocess_for_topics(
        "SPEAKER_01: We should raise pricing by ten percent next quarter."
    )

    assert "pricing" in result
    assert "SPEAKER_01" not in result, "the domain cleanup half must still run"


def test_offline_suppresses_the_stopwords_download_attempt(monkeypatch):
    """With the assertion set, the network is not reached at all."""
    import nltk.corpus

    attempts = _break_corpus(monkeypatch, "stopwords")
    monkeypatch.setattr(
        nltk.corpus.stopwords, "words", lambda *a, **k: (_ for _ in ()).throw(LookupError("gone"))
    )
    monkeypatch.setenv("NLTK_OFFLINE", "1")

    text_preprocessing._get_stopwords()

    assert attempts == [], f"a download was attempted despite NLTK_OFFLINE=1: {attempts}"


def test_without_the_assertion_the_download_is_still_attempted(monkeypatch):
    """The control for the test above — otherwise it would pass with the retry deleted."""
    import nltk.corpus

    attempts = _break_corpus(monkeypatch, "stopwords")
    monkeypatch.setattr(
        nltk.corpus.stopwords, "words", lambda *a, **k: (_ for _ in ()).throw(LookupError("gone"))
    )
    monkeypatch.delenv("NLTK_OFFLINE", raising=False)

    text_preprocessing._get_stopwords()

    assert attempts == ["stopwords"], f"the recovery download no longer runs: {attempts}"


# --------------------------------------------------------------------------- #
# Sentence splitting degrades — the transcription path                         #
# --------------------------------------------------------------------------- #


SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "We raised pricing. The team agreed.", "speaker": "S1"},
]


def test_missing_punkt_leaves_segments_unsplit_instead_of_failing_transcription(monkeypatch):
    """``clean_segments`` calls this unguarded, so raising failed the whole job.

    Sentence splitting is an enhancement: without it a segment simply stays
    multi-sentence, which is what every pre-splitting transcript looked like.
    """
    import nltk.data

    _break_corpus(monkeypatch, "punkt_tab")
    monkeypatch.setattr(
        nltk.data, "load", lambda *a, **k: (_ for _ in ()).throw(LookupError("no punkt"))
    )

    result = segment_dedup.split_sentences_nltk(list(SEGMENTS))

    assert result == SEGMENTS, "the segments were altered rather than passed through"


def test_clean_segments_survives_a_missing_punkt(monkeypatch):
    """The caller that had no try/except at all."""
    import nltk.data

    _break_corpus(monkeypatch, "punkt_tab")
    monkeypatch.setattr(
        nltk.data, "load", lambda *a, **k: (_ for _ in ()).throw(LookupError("no punkt"))
    )

    result = segment_dedup.clean_segments(
        list(SEGMENTS), enable_sentence_splitting=True, enable_dedup=False
    )

    assert len(result) == 1
    assert result[0]["text"] == SEGMENTS[0]["text"]


def test_offline_suppresses_the_punkt_download_attempt(monkeypatch):
    import nltk.data

    attempts = _break_corpus(monkeypatch, "punkt_tab")
    monkeypatch.setattr(
        nltk.data, "load", lambda *a, **k: (_ for _ in ()).throw(LookupError("no punkt"))
    )
    monkeypatch.setenv("NLTK_OFFLINE", "1")

    segment_dedup.split_sentences_nltk(list(SEGMENTS))

    assert attempts == [], f"a download was attempted despite NLTK_OFFLINE=1: {attempts}"


def test_a_working_punkt_still_splits(monkeypatch):
    """The control: none of the above may be satisfied by disabling splitting."""
    monkeypatch.delenv("NLTK_OFFLINE", raising=False)

    result = segment_dedup.split_sentences_nltk(list(SEGMENTS))

    assert len(result) == 2, f"punkt is available here and must still split: {result}"
