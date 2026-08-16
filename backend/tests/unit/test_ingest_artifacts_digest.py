"""The extractive digest: deterministic, window-sized, and provably provenanced.

Determinism is the load-bearing property. This output feeds Stage 3's index, so a digest
that varies run to run makes every phase-over-phase retrieval delta in the epic partly
noise — the same class of defect as the non-total segment ordering that invalidated the
first Stage 1 baseline (#433).
"""

from __future__ import annotations

import json

import pytest

from app.services.ingest_artifacts import sizing
from app.services.ingest_artifacts.digest import MIN_SENTENCE_WORDS
from app.services.ingest_artifacts.digest import build_digest
from app.services.ingest_artifacts.digest import candidate_sentences
from app.services.ingest_artifacts.digest import digest_text
from app.services.ingest_artifacts.provenance import validate_provenance

_SCRIPT = [
    ("Dana", "Let us start with the quarterly budget review for the new product line."),
    ("Dana", "I think we should cut the marketing spend by about fifteen percent this quarter."),
    ("Marcus", "Yeah."),
    (
        "Marcus",
        "I disagree with cutting marketing now because the launch is six weeks away and "
        "we still need the awareness campaign running.",
    ),
    (
        "Priya",
        "The engineering timeline slipped again so the launch date is probably moving to "
        "November regardless of what we decide about marketing.",
    ),
    ("Dana", "If the launch moves to November then the whole spending question changes."),
    ("Marcus", "We should revisit the budget once engineering confirms the November date."),
    ("Priya", "I will confirm the November launch date with the engineering leads by Friday."),
]


def _segments(repeats: int = 1, *, seconds: float = 6.0) -> list[dict]:
    segments = []
    segment_id = 1
    clock = 0.0
    for _ in range(repeats):
        for speaker, text in _SCRIPT:
            segments.append(
                {
                    "id": segment_id,
                    "text": text,
                    "start_time": clock,
                    "end_time": clock + seconds,
                    "speaker": speaker,
                }
            )
            segment_id += 1
            clock += seconds
    return segments


def test_the_same_transcript_produces_a_byte_identical_digest():
    """Not "equal-ish": the JSONB is compared serialised, floats and all."""
    segments = _segments(3)
    first = json.dumps(build_digest(segments), sort_keys=True)
    second = json.dumps(build_digest(segments), sort_keys=True)
    assert first == second


def test_reordering_the_input_changes_the_digest():
    """The guard on the determinism test above: prove it is not trivially constant.

    If build_digest ignored its input order, the byte-identity test would pass for the
    wrong reason. Adjacency defines turns and sections, so a shuffled read MUST differ —
    which is also why the loader orders by (start_time, end_time, id).
    """
    segments = _segments(3)
    shuffled = list(reversed(segments))
    assert build_digest(shuffled) != build_digest(segments)


def test_every_section_fits_the_measured_embedding_window():
    """G8, enforced. Sections are cut to the budget derived in ``sizing``."""
    digest = build_digest(_segments(12))
    assert digest["sections"], "a 12x transcript must produce at least one section"
    for section in digest["sections"]:
        assert section["word_count"] <= sizing.DIGEST_SECTION_MAX_WORDS, (
            f"section {section['index']} is {section['word_count']} words; the measured "
            f"128-wordpiece window allows {sizing.DIGEST_SECTION_MAX_WORDS}"
        )
        assert sizing.fits_embedding_window(section["text"])


def test_a_long_transcript_gets_more_sections_than_a_short_one():
    """The digest scales with the recording rather than describing only its opening."""
    short = build_digest(_segments(1))
    long = build_digest(_segments(20))
    assert len(long["sections"]) > len(short["sections"])
    assert len(long["sections"]) <= sizing.MAX_DIGEST_SECTIONS


def test_sections_are_contiguous_in_time_and_carry_a_real_span():
    digest = build_digest(_segments(20))
    spans = [(s["start_time"], s["end_time"]) for s in digest["sections"]]
    assert all(end >= start for start, end in spans)
    assert spans == sorted(spans), "sections must read in chronological order"
    assert any(start > 0 for start, _ in spans), "not every section can start at 0:00"


def test_every_sentence_is_verbatim_source_text():
    """Extractive means extractive — the re-masking path re-derives from these segments."""
    segments = _segments(2)
    by_id = {s["id"]: s["text"] for s in segments}
    sections = build_digest(segments)["sections"]
    checked = 0
    assert sections, "no sections — the loop below would pass vacuously"
    for section in sections:
        for sentence in section["sentences"]:
            sources = " ".join(by_id[i] for i in sentence["provenance"]["segment_ids"])
            assert sentence["text"] in sources
            checked += 1
    assert checked >= len(sections), "at least one sentence per section must have been checked"


def test_every_sentence_carries_valid_provenance_with_real_timestamps():
    sections = build_digest(_segments(4))["sections"]
    assert sections, "no sections — the loop below would pass vacuously"
    checked = 0
    for section in sections:
        for sentence in section["sentences"]:
            validate_provenance(sentence["provenance"])
            assert sentence["provenance"]["segment_ids"]
            assert sentence["provenance"]["end_time"] >= sentence["provenance"]["start_time"]
            checked += 1
    assert checked >= len(sections)


def test_backchannels_are_not_candidates():
    """ "Yeah." is a superb PageRank hub and a useless summary sentence."""
    texts = [s.text for s in candidate_sentences(_segments(2))]
    assert "Yeah." not in texts
    assert all(len(t.split()) >= MIN_SENTENCE_WORDS for t in texts)


def test_a_sentence_spanning_two_segments_cites_both():
    """ASR splits mid-sentence; the digest must not silently drop half the provenance."""
    segments = [
        {
            "id": 1,
            "text": "The migration runs on Friday",
            "start_time": 0.0,
            "end_time": 4.0,
            "speaker": "Dana",
        },
        {
            "id": 2,
            "text": "and finishes before the demo.",
            "start_time": 4.0,
            "end_time": 8.0,
            "speaker": "Dana",
        },
    ]
    sentences = candidate_sentences(segments)
    assert len(sentences) == 1
    assert sentences[0].segment_ids == (1, 2)
    assert sentences[0].start_time == 0.0
    assert sentences[0].end_time == 8.0


def test_an_empty_transcript_yields_an_empty_but_valid_digest():
    """A valid outcome, not a failure — callers must not treat it as one."""
    digest = build_digest([])
    assert digest["sections"] == []
    assert digest["word_count"] == 0
    assert digest_text(digest) == ""


def test_a_transcript_of_only_backchannels_yields_no_sections():
    segments = [
        {"id": i, "text": "Yeah.", "start_time": float(i), "end_time": i + 1.0, "speaker": "Dana"}
        for i in range(10)
    ]
    assert build_digest(segments)["sections"] == []


def test_the_payload_records_the_window_it_was_sized_for():
    """Stage 3 must be able to tell a pre-measurement digest from a post-measurement one."""
    digest = build_digest(_segments(2))
    assert digest["embedding_window_wordpieces"] == sizing.EMBEDDING_MAX_WORDPIECES
    assert digest["section_max_words"] == sizing.DIGEST_SECTION_MAX_WORDS
    assert digest["schema_version"] >= 1


def test_section_max_words_is_derived_from_the_measurement_not_hardcoded():
    """If someone edits a constant, the derivation must still hold.

    The number that matters is the MEASURED 128-token window; the section budget is what
    is left after Stage 3's header reserve, at the measured wordpieces-per-word.
    """
    expected = int(
        (
            sizing.EMBEDDING_MAX_WORDPIECES
            - sizing.EMBEDDING_SPECIAL_TOKENS
            - sizing.HEADER_WORDPIECE_RESERVE
        )
        / sizing.MEASURED_WORDPIECES_PER_WORD
    )
    assert expected == sizing.DIGEST_SECTION_MAX_WORDS
    assert sizing.DIGEST_SECTION_TARGET_WORDS < sizing.DIGEST_SECTION_MAX_WORDS


def test_the_measured_window_is_128_not_the_addendums_assumed_256():
    """A regression guard on the finding itself.

    Addendum G8 worked from ~256 wordpieces. Measurement against the deployed model says
    128. If someone "corrects" this back to 256, digest sections double in length and half
    of each one silently stops contributing to its own embedding —
    ``tests/integration/test_embedding_window_truncation.py`` is the live proof.
    """
    assert sizing.EMBEDDING_MAX_WORDPIECES == 128
    assert sizing.DIGEST_SECTION_MAX_WORDS < 100


@pytest.mark.parametrize("language", ["en", "de", "fr", "xx"])
def test_an_unknown_language_still_produces_a_digest(language):
    """The gate is 100% of transcribed files; an unmapped language cannot be an exception."""
    digest = build_digest(_segments(3), language=language)
    assert digest["sections"]
    assert digest["language"] == language
