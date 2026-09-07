"""An unreadable NLTK corpus must degrade, never fail the transcription.

NLTK is **not** in the ASR or diarization path. It powers sentence splitting,
RAG chunking and topic extraction — every one of which has a working degraded
mode (coarser segments, regex splitting, regex tokens). So a corpus that cannot
be read is a reason to produce a slightly worse transcript, never a reason to
produce none.

Issue #491 established exactly that and guarded the call sites with
``except LookupError``. That was too narrow, and the gap shipped:

* ``LookupError`` is the resource-MISSING case.
* An unreadable-but-present corpus raises ``OSError`` — wrong ownership on the
  model cache (what ``scripts/fix-model-permissions.sh`` repairs), a truncated
  pickle, and since nltk 3.10 its **pathsec** CWE-59 hardening, which raises
  ``PermissionError`` for any corpus file with ``st_nlink > 1``.

A hardlinked model cache therefore failed **every transcription** with a
"Security Violation" naming nothing the operator could act on — the precise
outcome #491's guard existed to prevent.

Each test drives the real function with a patched NLTK that raises
``PermissionError``, and asserts the caller still returns usable output. The
``LookupError`` control beside it proves the widening did not *replace* the
original guard.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.utils.nltk_offline import NLTK_CORPUS_UNAVAILABLE

#: The real message nltk >=3.10 pathsec raises. Reproduced verbatim so a test
#: failure is recognisable as the production symptom.
PATHSEC_MESSAGE = (
    "Security Violation [pathsec.open]: refusing multiply-linked file "
    "'/home/appuser/.cache/nltk_data/tokenizers/punkt_tab/english/collocations.tab' "
    "(st_nlink=3); a hardlink can point at an outside-root inode (CWE-59)"
)

SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "First sentence here. Second sentence here."},
    {"start": 4.0, "end": 8.0, "text": "Third one. And a fourth."},
]


def test_pathsec_permission_error_is_in_the_caught_category():
    """The definition must cover what pathsec actually raises."""
    assert issubclass(PermissionError, NLTK_CORPUS_UNAVAILABLE), (
        "PermissionError is not caught — a hardlinked or unreadable corpus will "
        "still fail the transcription"
    )
    assert issubclass(LookupError, NLTK_CORPUS_UNAVAILABLE), (
        "LookupError dropped — issue #491's missing-corpus case regressed"
    )


def test_the_category_does_not_swallow_our_own_defects():
    """Deliberately not ``Exception``: a real bug must keep propagating."""
    for defect in (TypeError, AttributeError, ValueError, RuntimeError):
        assert not issubclass(defect, NLTK_CORPUS_UNAVAILABLE), (
            f"{defect.__name__} is being swallowed — that hides defects in our own code"
        )


def test_keyerror_and_indexerror_are_caught_and_that_is_pre_existing():
    """Honest record of the one thing this category cannot avoid.

    ``KeyError`` and ``IndexError`` are subclasses of ``LookupError``, so any
    guard that catches a missing NLTK resource **necessarily** also catches
    those two. That is a property of the exception NLTK chose, and it was
    already true of the original ``except LookupError`` — widening to include
    ``OSError`` did not introduce it and cannot remove it.

    Pinned rather than quietly tolerated so nobody 'discovers' it later and
    concludes the widening caused it. Narrowing this would mean catching
    ``LookupError`` and re-raising when it is a ``KeyError``/``IndexError``,
    which is only worth doing if a defect ever actually hides here.
    """
    assert issubclass(KeyError, LookupError)
    assert issubclass(IndexError, LookupError)
    assert issubclass(KeyError, NLTK_CORPUS_UNAVAILABLE), (
        "documented behaviour changed: KeyError is no longer caught"
    )


@pytest.mark.parametrize(
    "raised",
    [PermissionError(PATHSEC_MESSAGE), LookupError("Resource punkt not found")],
    ids=["pathsec-unreadable", "corpus-missing"],
)
def test_split_sentences_returns_segments_instead_of_raising(raised):
    """The transcription killer: this must return segments, not propagate.

    Both the initial load and the post-download retry are made to fail, which
    is the real shape — a corpus that cannot be read does not become readable
    because ``nltk.download`` ran.
    """
    from app.utils import segment_dedup

    # ``nltk.data.load`` is imported INSIDE the function, so the module
    # attribute does not exist to patch — patch the real source instead.
    with (
        patch("nltk.data.load", side_effect=raised),
        patch.object(segment_dedup, "nltk_downloads_permitted", return_value=False),
    ):
        result = segment_dedup.split_sentences_nltk(list(SEGMENTS))

    assert result == SEGMENTS, (
        "segments were altered or lost; the degraded path must return them unsplit"
    )


def test_split_sentences_still_propagates_a_real_defect():
    """The control. Without this, 'return segments' on any error would pass."""
    from app.utils import segment_dedup

    with patch("nltk.data.load", side_effect=TypeError("bug in our code")):
        with pytest.raises(TypeError):
            segment_dedup.split_sentences_nltk(list(SEGMENTS))


@pytest.mark.parametrize(
    "raised",
    [PermissionError(PATHSEC_MESSAGE), LookupError("Resource punkt_tab not found")],
    ids=["pathsec-unreadable", "corpus-missing"],
)
def test_tokenize_falls_back_to_regex(raised):
    """Topic extraction degrades to the regex tokenizer rather than raising."""
    from app.utils import text_preprocessing

    with (
        patch("nltk.word_tokenize", side_effect=raised),
        patch.object(text_preprocessing, "nltk_downloads_permitted", return_value=False),
    ):
        tokens = text_preprocessing._tokenize("the quarterly budget review meeting")

    assert tokens, "regex fallback produced no tokens"
    assert "budget" in tokens, f"regex fallback lost content: {tokens}"


@pytest.mark.parametrize(
    "raised",
    [PermissionError(PATHSEC_MESSAGE), LookupError("Resource punkt_tab not found")],
    ids=["pathsec-unreadable", "corpus-missing"],
)
def test_chunking_reaches_its_punkt_fallback_when_punkt_tab_is_unreadable(raised):
    """``_load_punkt_model`` must try the second location, not bail on the first.

    With the old ``except LookupError`` an unreadable ``punkt_tab`` escaped the
    function outright, so the ``punkt`` fallback below it was dead code for that
    failure mode even when it would have loaded fine.
    """
    from app.services.search import chunking_service

    sentinel = object()
    calls: list[str] = []

    class _FakeNltkData:
        def load(self, path: str):
            calls.append(path)
            if "punkt_tab" in path:
                raise raised
            return sentinel

    result = chunking_service._load_punkt_model(_FakeNltkData(), "english")

    assert len(calls) == 2, f"the punkt fallback was never attempted; calls={calls}"
    assert result is sentinel, "the working fallback tokenizer was not returned"


def test_chunking_latches_and_degrades_rather_than_raising():
    """A wholly unreadable corpus yields the regex splitter, not an exception.

    Also pins the latch (issue #449): once failed, the process stays on regex so
    one re-index cannot chunk its corpus two different ways. That is why
    repairing the corpus on disk needs a WORKER RESTART.
    """
    from app.services.search import chunking_service

    chunking_service.reset_sentence_splitter_state()
    try:
        with patch.object(
            chunking_service,
            "_load_punkt_model",
            side_effect=PermissionError(PATHSEC_MESSAGE),
        ):
            first = chunking_service._get_nltk_tokenizer("english")
        assert first is None, "expected the regex fallback signal (None)"
        assert chunking_service._nltk_load_failed is True, "the failure did not latch"

        # Latched: even a now-working corpus is not retried this process.
        with patch.object(chunking_service, "_load_punkt_model", return_value=object()):
            second = chunking_service._get_nltk_tokenizer("english")
        assert second is None, (
            "the latch was not honoured — chunk boundaries could change mid-reindex (#449)"
        )
    finally:
        chunking_service.reset_sentence_splitter_state()
