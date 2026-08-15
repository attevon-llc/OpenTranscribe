"""Chunking must work for every script the transcriber supports (issue #448).

WhisperX transcribes 100+ languages, but the chunker measured and split text as
though every language were written like English. A **10,500-character Chinese
transcript became ONE chunk** — measured, not theorised — which means:

* retrieval over it is useless (no granularity, one timestamp span for hours of
  audio), and
* the embedding model silently truncates at its token window, so most of the
  transcript is never embedded at all.

This is a SEARCH defect, not only a RAG one: it breaks finding your own words in
your own recordings, which works regardless of the English-only chat scope.

Four compounding causes, each of which had to be fixed:

1. ``_PUNKT_LANG_MAP.get(language, "english")`` handed unmapped languages to the
   ENGLISH punkt model, which loads fine, runs fine, and returns Chinese as one
   sentence. punkt had not failed — it was merely wrong, so the regex fallback
   (which already listed ``。！？``) was never reached.
2. That regex required ``\\s+`` AFTER the terminator. CJK has no spaces, so it
   could not have matched a boundary even when reached.
3. Chunk budgets are counted in words via ``str.split()``, which reports any
   amount of Chinese as **one word**, so every size check passed.
4. Rejoining split sentences with ``" "`` inserted spaces the original never had.

Every test here pairs a non-Latin case with an English/German control, because
the failure mode of an over-eager fix is splitting ``3.14`` and ``Dr. Chen``.
"""

from __future__ import annotations

import pytest

from app.services.search.chunking_service import _split_into_sentences
from app.services.search.chunking_service import chunk_transcript_by_speaker_turns
from app.services.search.chunking_service import count_words


@pytest.fixture(autouse=True, scope="module")
def _ensure_punkt():
    """`_split_into_sentences` falls back to a regex silently when punkt is
    missing (chunking_service.py's own documented, deliberate degradation for
    a worker that hasn't downloaded it yet) — and the regex disagrees with
    punkt on abbreviations by design (`test_english_boundaries_are_unchanged`
    exists to pin that difference). A clean CI checkout has no cached
    nltk_data, so without this the "Dr. Chen" case silently exercises the
    fallback instead of the smart path this file is meant to regression-test.
    Same download call already used elsewhere in this codebase
    (`app/utils/text_preprocessing.py`, `app/utils/segment_dedup.py`)."""
    import nltk

    nltk.download("punkt_tab", quiet=True)


#: Three sentences each, in the script's own terminators.
THREE_SENTENCES: dict[str, str] = {
    "en": "The team shipped on Tuesday. Dana raised a concern. We agreed to revisit it.",
    "de": "Das Team lieferte am Dienstag. Dana äußerte Bedenken. Wir wollten es prüfen.",
    "zh": "团队在星期二发布了产品。达娜提出了一个担忧。我们同意重新审视这个问题。",
    "ja": "チームは火曜日に出荷しました。ダナが懸念を提起しました。我々はそれを再検討することに同意した。",
    "ko": "팀은 화요일에 출시했습니다. 다나가 우려를 제기했습니다. 우리는 이를 재검토하기로 했습니다.",
    "ar": "أطلق الفريق يوم الثلاثاء. أثارت دانا مخاوف. اتفقنا على إعادة النظر.",
    "hi": "टीम ने मंगलवार को शिप किया। दाना ने चिंता जताई। हम इसकी समीक्षा करने पर सहमत हुए।",
}


def _chunk(text: str, language: str) -> list[dict]:
    """Run the real chunker over a single speaker turn."""
    return chunk_transcript_by_speaker_turns(
        [{"speaker": "S1", "text": text, "start_time": 0.0, "end_time": 600.0}],
        file_uuid="u",
        file_id=1,
        user_id=1,
        title="t",
        speakers=["S1"],
        tags=[],
        upload_time="2026-01-01T00:00:00Z",
        language=language,
    )


@pytest.mark.parametrize("language", sorted(THREE_SENTENCES))
def test_three_sentences_split_into_three(language: str) -> None:
    """Every supported script finds its own sentence boundaries.

    ``zh``/``ja`` returned 1 before the fix (English punkt), and so did ``hi``
    — Devanagari is written WITH spaces, so it cleared the scriptio-continua
    check and was handed to punkt, which does not know the danda ``।``.
    """
    sentences = _split_into_sentences(THREE_SENTENCES[language], language)
    assert len(sentences) == 3, f"{language}: got {len(sentences)} sentences: {sentences}"


@pytest.mark.parametrize(
    ("text", "expected", "why"),
    [
        ("Pi is 3.14 and that matters. Next sentence here.", 2, "a decimal point"),
        ("Dr. Chen spoke first. Then Dana replied.", 2, "an abbreviation"),
        ("What? Yes! Indeed.", 3, "consecutive terminators"),
    ],
)
def test_english_boundaries_are_unchanged(text: str, expected: int, why: str) -> None:
    """The control: the fix must not make Latin splitting more eager.

    The whitespace requirement after ``.``/``!``/``?`` is what prevents these,
    and it is deliberately kept — only the CJK/Devanagari terminators had it
    relaxed. Dropping it wholesale would split on {why} and quietly shred every
    English transcript in the product.
    """
    assert len(_split_into_sentences(text, "en")) == expected


def test_word_count_sees_cjk_characters() -> None:
    """A size budget that reports a whole transcript as one word enforces nothing.

    This is the cause that made the others invisible: even with correct sentence
    splitting, `len(text.split()) == 1` meant no chunk ever exceeded the target.
    """
    chinese = THREE_SENTENCES["zh"] * 100
    assert len(chinese.split()) == 1, "precondition: str.split() cannot see CJK words"
    assert count_words(chinese) > 1000, "count_words must measure CJK by character"


def test_word_count_matches_split_for_latin() -> None:
    """The control for the above: Latin counting must not change.

    If `count_words` disagreed with `str.split()` on English, every existing
    chunk boundary in the product would move — a silent reindex-wide change.
    """
    english = THREE_SENTENCES["en"] * 100
    assert count_words(english) == len(english.split())


@pytest.mark.parametrize("language", ["zh", "ja", "th"])
def test_a_long_non_latin_transcript_is_chunked(language: str) -> None:
    """The headline defect: one chunk for an entire recording.

    Thai is included deliberately and has NO sentence terminators at all, so it
    still splits into one "sentence" — it is chunked only because the word count
    now sees its characters and the word-count fallback engages. Without that,
    a language with no punctuation would still produce a single blob.
    """
    thai = "ทีมงานเปิดตัวในวันอังคาร ดาน่าแสดงความกังวล เราตกลงที่จะทบทวน"
    text = (THREE_SENTENCES.get(language) or thai) * 300

    chunks = _chunk(text, language)

    assert len(chunks) > 10, f"{language}: {len(text)} chars produced {len(chunks)} chunk(s)"
    largest = max(len(c["content"]) for c in chunks)
    assert largest < len(text) / 5, (
        f"{language}: largest chunk is {largest} of {len(text)} chars — the "
        f"transcript is not really being divided."
    )


def test_chunking_does_not_inject_spaces_into_cjk() -> None:
    """Chunk text must be a faithful slice of the transcript.

    Both the word-slicing path (`" ".join(text.split()[i:j])`) and the
    sentence-rejoining path (`" ".join(sentences)`) inserted a space between
    every character / after every 。. That is wrong twice over: the reader sees
    mangled text, and the embedding model receives a different string than the
    one the rest of the pipeline indexed.
    """
    chinese = THREE_SENTENCES["zh"] * 300
    chunks = _chunk(chinese, "zh")

    assert chunks, "precondition: the transcript produced chunks"
    for chunk in chunks:
        assert " " not in chunk["content"], (
            f"whitespace injected into Chinese chunk text: {chunk['content'][:60]!r}"
        )


def test_english_chunking_is_unaffected() -> None:
    """The end-to-end control.

    Every assertion above concerns non-Latin text. Without this, a change that
    fixed Chinese by breaking English would pass the whole module.
    """
    english = THREE_SENTENCES["en"] * 300
    chunks = _chunk(english, "en")

    assert 15 < len(chunks) < 40, f"English chunk count moved to {len(chunks)}"
    assert all(c["content"].strip() for c in chunks)
    # The transcript must be recoverable from its chunks, modulo overlap.
    assert "Dana raised a concern" in chunks[0]["content"]
