"""Boundary-correctness tests for ``app/services/search/chunking_service.py``.

This module decides the chunk boundaries that become documents in the
``transcript_chunks`` OpenSearch index. Two things make those boundaries worth pinning:

* **Re-index determinism (issue #433).** A re-index that produces a different number of
  chunks for unchanged input is a real defect this repo has already shipped once. The
  index is rebuilt from the same segments, so ``chunk(segments)`` must be a pure function
  of its arguments.
* **The issue-#52 chat-leak boundary.** ``transcript_chunks`` stores transcript text
  **unredacted**; any path sending a chunk to an LLM must first call
  ``services/chat/redactor.mask_chunks()``. The masker only ever sees what the chunker
  emitted, so a boundary that drops text hides it from redaction accounting, and a
  boundary that duplicates text multiplies whatever the masker has to catch.

The properties asserted here, in the order they appear:

1. **Determinism** — equal-but-distinct inputs produce byte-identical chunk dicts.
2. **No text lost** — every input word reaches at least one chunk, in input order.
3. **Bounded duplication** — words repeat only across an overlap seam, exactly twice,
   and the total repeated count is exactly ``overlap_words`` per seam.
4. **Edges** — empty transcript, whitespace-only segments, a turn shorter than the chunk
   size, a turn longer than it, and ``overlap_words >= target_words`` (a loop guard).
5. **Speaker-turn grouping and the short-turn merge.**
6. **``organization_id`` is written only when set** — the personal-scope search filter is
   ``must_not exists organization_id``, so an unconditionally-written field would make
   every personal chunk invisible to personal search.

Two tests below are **characterization tests for reported defects**
(``test_grouper_aliases_...``, ``test_rechunking_the_same_list_object_...``). They assert
today's wrong behaviour on purpose, so the defect cannot drift unnoticed while it is open.
Each says so in its docstring and states what to replace it with once the production fix
lands.

There were three. ``test_a_cjk_transcript_collapses_to_one_chunk`` pinned the CJK
one-chunk defect and instructed its successor to make it fail when segmentation landed;
issue #448 landed and it did, and it is now
``test_a_cjk_transcript_is_split_into_many_chunks``. Script coverage beyond Japanese
(zh/ko/ar/hi/th, with Latin controls) lives in ``test_chunking_scripts.py``.
"""

from __future__ import annotations

import copy
from collections import Counter
from itertools import pairwise
from typing import Any

import pytest

from app.services.search import chunking_service
from app.services.search.chunking_service import chunk_transcript_by_speaker_turns

#: Metadata every call needs; none of it participates in boundary decisions.
BASE_KWARGS: dict[str, Any] = {
    "file_uuid": "11111111-2222-3333-4444-555555555555",
    "file_id": 42,
    "user_id": 7,
    "title": "Chunking Boundary Fixture",
    "speakers": ["S1", "S2"],
    "tags": ["fixture"],
    "upload_time": "2026-01-01T00:00:00Z",
}


@pytest.fixture
def sentence_splitter_state():
    """Save/restore the module-level NLTK tokenizer cache and the failure latch.

    ``chunking_service`` memoises punkt tokenizers in ``_nltk_tokenizers`` and latches
    ``_nltk_load_failed`` once a load fails. Both are process-global, so a test that
    touches either would otherwise leak its choice of sentence splitter into every later
    test in the worker.

    The latch replaced a five-minute retry cooldown in issue #449: a cooldown meant the
    splitter could change part-way through one re-index, so the same corpus was chunked
    two different ways in a single pass. Restoring it here is what keeps that permanence
    from making the suite order-dependent.
    """
    saved_tokenizers = dict(chunking_service._nltk_tokenizers)
    saved_latch = chunking_service._nltk_load_failed
    yield
    chunking_service._nltk_tokenizers.clear()
    chunking_service._nltk_tokenizers.update(saved_tokenizers)
    chunking_service._nltk_load_failed = saved_latch


def _force_regex_sentence_split() -> None:
    """Pin the sentence splitter to the regex fallback.

    Whether NLTK punkt data is present differs between the dev image, the host venv and
    CI. Tests whose expected boundaries depend on sentence splitting force the fallback
    so they assert one fixed set of boundaries everywhere. Requires the
    ``sentence_splitter_state`` fixture.
    """
    chunking_service._nltk_tokenizers.clear()
    chunking_service._nltk_load_failed = True


def _monologue(sentence_count: int = 50, words_per_sentence: int = 10) -> str:
    """A monologue whose every token is unique, so coverage is decidable by set equality.

    Sentences start with a capitalised token and end in a period, which the regex
    fallback and NLTK punkt split identically — the fixture does not depend on which
    splitter is active.
    """
    sentences = []
    for i in range(sentence_count):
        base = i * words_per_sentence
        head = f"Word{base:03d}"
        tail = " ".join(f"w{base + j:03d}" for j in range(1, words_per_sentence))
        sentences.append(f"{head} {tail}.")
    return " ".join(sentences)


def _segment(text: str, speaker: str, start: float, end: float, **extra) -> dict[str, Any]:
    return {"start": start, "end": end, "text": text, "speaker": speaker, **extra}


def _monologue_segments() -> list[dict[str, Any]]:
    """One 500-word single-speaker turn spanning 500 seconds — 1.0 s per word.

    The round rate makes every interpolated timestamp an exact expected value rather
    than something the test has to recompute from the implementation.
    """
    return [_segment(_monologue(), "S1", 0.0, 500.0)]


# ---------------------------------------------------------------------------
# 1. Determinism  (issue #433)
# ---------------------------------------------------------------------------


def test_chunking_is_deterministic_across_equal_but_distinct_inputs():
    """Two structurally equal inputs must produce byte-identical chunk documents.

    This is the re-index contract: a rebuild reads the same segments out of Postgres and
    must write the same documents. Full-dict equality, not a chunk count — #433 was a
    count change, but a boundary that shifted while the count held would be just as wrong
    and a count assertion would miss it.
    """
    first = chunk_transcript_by_speaker_turns(_monologue_segments(), **BASE_KWARGS)
    second = chunk_transcript_by_speaker_turns(_monologue_segments(), **BASE_KWARGS)

    assert len(first) == 3
    assert first == second


def test_chunking_is_deterministic_for_a_multi_speaker_transcript():
    """The same contract with speaker turns in play, where grouping state is involved."""
    segments = [
        _segment("Alpha one two three.", "S1", 0.0, 2.0),
        _segment("Alpha four five six.", "S1", 2.0, 4.0),
        _segment("Beta seven eight nine ten eleven twelve.", "S2", 4.0, 7.0),
        _segment("Alpha thirteen fourteen fifteen sixteen seventeen.", "S1", 7.0, 10.0),
    ]

    first = chunk_transcript_by_speaker_turns(copy.deepcopy(segments), **BASE_KWARGS)
    second = chunk_transcript_by_speaker_turns(copy.deepcopy(segments), **BASE_KWARGS)

    assert [chunk["speaker"] for chunk in first] == ["S1", "S2", "S1"]
    assert first == second


# ---------------------------------------------------------------------------
# 2. No text lost
# ---------------------------------------------------------------------------


def test_no_word_is_lost_when_a_long_turn_is_split():
    """Every input word must survive into at least one chunk.

    A dropped word is invisible downstream: it is absent from search, absent from chat
    retrieval, and — because it never reaches ``mask_chunks()`` — absent from any
    accounting of what the redactor saw.
    """
    text = _monologue()
    expected_words = text.split()
    chunks = chunk_transcript_by_speaker_turns([_segment(text, "S1", 0.0, 500.0)], **BASE_KWARGS)

    emitted = [word for chunk in chunks for word in chunk["content"].split()]

    assert len(chunks) == 3
    assert len(expected_words) == 500
    assert set(emitted) == set(expected_words)
    assert len(set(emitted)) == 500


def test_word_order_is_preserved_across_the_chunk_sequence():
    """First appearances, read across chunks in order, reconstruct the input exactly.

    Stronger than set equality: it rules out a chunker that emits every word but
    reorders or interleaves them, which would produce chunks whose text never occurred
    in the recording.
    """
    text = _monologue()
    expected_words = text.split()
    chunks = chunk_transcript_by_speaker_turns([_segment(text, "S1", 0.0, 500.0)], **BASE_KWARGS)

    first_appearance: list[str] = []
    already_seen: set[str] = set()
    for chunk in chunks:
        for word in chunk["content"].split():
            if word not in already_seen:
                already_seen.add(word)
                first_appearance.append(word)

    assert len(chunks) == 3
    assert first_appearance == expected_words


def test_no_word_is_lost_on_the_word_count_fallback_path():
    """The fallback taken when sentence splitting finds no boundary must also lose nothing.

    Text with no sentence punctuation yields a single "sentence", which routes to
    ``_split_long_turn_by_words``. That path advances by ``target - overlap`` and is the
    one where an off-by-one drops a word at every seam.
    """
    tokens = [f"u{i:03d}" for i in range(120)]
    segments = [_segment(" ".join(tokens), "S1", 0.0, 120.0)]

    chunks = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=50, overlap_words=10
    )

    emitted = [word for chunk in chunks for word in chunk["content"].split()]
    assert [len(chunk["content"].split()) for chunk in chunks] == [50, 50, 40]
    assert [chunk["content"].split()[0] for chunk in chunks] == ["u000", "u040", "u080"]
    assert set(emitted) == set(tokens)


# ---------------------------------------------------------------------------
# 3. Bounded duplication
# ---------------------------------------------------------------------------


def test_duplication_is_confined_to_the_overlap_seams(sentence_splitter_state):
    """Repeated words are the deliberate overlap and nothing else.

    Three chunks means two seams. With ``overlap_words=40`` and a fixture of ten-word
    sentences, each seam repeats exactly 40 words and each repeated word appears in
    exactly two chunks — 80 repeated tokens in total. Unbounded overlap would inflate the
    index, and (since the chunks are what chat retrieval feeds to the LLM) would send the
    same passage to the model several times over.
    """
    _force_regex_sentence_split()
    text = _monologue()
    chunks = chunk_transcript_by_speaker_turns([_segment(text, "S1", 0.0, 500.0)], **BASE_KWARGS)

    counts = Counter(word for chunk in chunks for word in chunk["content"].split())
    repeated = {word: n for word, n in counts.items() if n > 1}

    assert len(chunks) == 3
    assert len(repeated) == 80
    assert set(repeated.values()) == {2}


def test_consecutive_chunks_overlap_by_the_requested_word_budget(sentence_splitter_state):
    """The seam is a genuine shared suffix/prefix, not two unrelated repeats.

    Asserted on the chunk *text*: the tail of chunk N is the head of chunk N+1. A
    "overlap" implemented by re-emitting arbitrary words would satisfy the counting test
    above and fail this one.
    """
    _force_regex_sentence_split()
    chunks = chunk_transcript_by_speaker_turns(_monologue_segments(), **BASE_KWARGS)

    assert len(chunks) == 3
    seams = 0
    for earlier, later in pairwise(chunks):
        tail = earlier["content"].split()[-40:]
        head = later["content"].split()[:40]
        assert tail == head
        seams += 1
    assert seams == 2


def test_a_turn_that_fits_in_one_chunk_duplicates_nothing():
    """No seam, therefore no repeats — the control for the overlap tests above."""
    tokens = [f"t{i:03d}" for i in range(150)]
    segments = [_segment(" ".join(tokens) + ".", "S1", 0.0, 150.0)]

    chunks = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)

    assert len(chunks) == 1
    counts = Counter(chunks[0]["content"].split())
    assert max(counts.values()) == 1
    assert len(counts) == 150


# ---------------------------------------------------------------------------
# 4. Edges
# ---------------------------------------------------------------------------


def test_an_empty_transcript_produces_no_chunks():
    assert chunk_transcript_by_speaker_turns([], **BASE_KWARGS) == []


def test_segments_with_only_whitespace_produce_no_chunks():
    """Empty ASR segments are dropped rather than indexed as blank documents."""
    segments = [
        _segment("   ", "S1", 0.0, 1.0),
        _segment("", "S2", 1.0, 2.0),
        _segment("\n\t ", "S1", 2.0, 3.0),
    ]

    assert chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS) == []


def test_a_single_short_segment_becomes_exactly_one_chunk():
    """The whole chunk document is pinned, not just the count.

    ``organization_id`` is asserted **absent**: personal-scope search filters on
    ``must_not exists organization_id``, so writing the key with a ``None`` value would
    hide every personal chunk from personal search.
    """
    segments = [_segment("Hello there friend.", "S1", 0.0, 3.0)]

    chunks = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, content_type="audio/wav", duration=3.0, file_size=1024
    )

    assert len(chunks) == 1
    assert chunks[0] == {
        "file_id": 42,
        "file_uuid": "11111111-2222-3333-4444-555555555555",
        "user_id": 7,
        "chunk_index": 0,
        "content": "Hello there friend.",
        "title": "Chunking Boundary Fixture",
        "speaker": "S1",
        "speakers": ["S1", "S2"],
        "tags": ["fixture"],
        "upload_time": "2026-01-01T00:00:00Z",
        "language": "en",
        "start_time": 0.0,
        "end_time": 3.0,
        "content_type": "audio/wav",
        "duration": 3.0,
        "file_size": 1024,
        "collection_ids": [],
    }


def test_a_turn_longer_than_the_target_splits_at_pinned_boundaries(sentence_splitter_state):
    """Chunk count, indices and interpolated timestamps, all pinned exactly.

    500 words over 500 seconds is 1.0 s/word, so each expected timestamp is the word
    offset: chunk 1 starts at word 160 (``200 - 40`` overlap) and chunk 2 at word 320.
    The final chunk's end is snapped to the turn's own end rather than interpolated.
    """
    _force_regex_sentence_split()
    chunks = chunk_transcript_by_speaker_turns(_monologue_segments(), **BASE_KWARGS)

    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1, 2]
    assert [(chunk["start_time"], chunk["end_time"]) for chunk in chunks] == [
        (0.0, 200.0),
        (160.0, 360.0),
        (320.0, 500.0),
    ]
    assert chunks[-1]["end_time"] == 500.0


def test_chunk_index_is_contiguous_from_zero_across_multiple_speakers(sentence_splitter_state):
    """``chunk_index`` numbers the whole file, not each turn — a per-turn reset would
    collide document ids across speakers."""
    _force_regex_sentence_split()
    segments = [
        _segment(_monologue(), "S1", 0.0, 500.0),
        _segment(
            "Beta replies with a handful of words to close the discussion here.", "S2", 500.0, 505.0
        ),
        _segment(_monologue(), "S1", 505.0, 1005.0),
    ]

    chunks = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)

    assert len(chunks) == 7
    assert [chunk["chunk_index"] for chunk in chunks] == list(range(7))
    assert [chunk["speaker"] for chunk in chunks] == ["S1", "S1", "S1", "S2", "S1", "S1", "S1"]


def test_overlap_larger_than_the_target_is_clamped_and_terminates():
    """``overlap_words >= target_words`` would step backwards forever on the word-split
    path. The clamp to ``target - 1`` is the guard; this test would hang without it,
    which is the failure mode being prevented.
    """
    chunks = chunk_transcript_by_speaker_turns(
        _monologue_segments(), **BASE_KWARGS, target_words=20, overlap_words=50
    )

    assert len(chunks) == 49
    assert [chunk["chunk_index"] for chunk in chunks] == list(range(49))
    emitted = {word for chunk in chunks for word in chunk["content"].split()}
    assert emitted == set(_monologue().split())


# ---------------------------------------------------------------------------
# 5. Speaker turns and the short-turn merge
# ---------------------------------------------------------------------------


def test_consecutive_same_speaker_segments_become_one_turn():
    """Grouping is what makes a chunk a coherent passage rather than an ASR segment.

    Each segment is deliberately **longer than 20 words**. Below that threshold the
    short-turn merge in ``chunk_transcript_by_speaker_turns`` re-joins adjacent
    same-speaker chunks after the fact, so a fixture of two-word segments produces the
    right answer even with turn grouping disabled entirely — it cannot tell the two
    mechanisms apart. Verified by disabling the grouping branch: with short segments this
    test still passed, with these it fails.
    """
    first_half = " ".join(f"a{i:03d}" for i in range(25)) + "."
    second_half = " ".join(f"b{i:03d}" for i in range(25)) + "."
    segments = [
        _segment(first_half, "S1", 0.0, 25.0),
        _segment(second_half, "S1", 25.0, 50.0),
        _segment("Bee speaks last.", "S2", 50.0, 52.0),
    ]

    chunks = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)

    assert [(c["speaker"], c["content"], c["start_time"], c["end_time"]) for c in chunks] == [
        ("S1", f"{first_half} {second_half}", 0.0, 50.0),
        ("S2", "Bee speaks last.", 50.0, 52.0),
    ]


def test_a_speaker_change_always_starts_a_new_chunk():
    """Two speakers' words must never share a chunk: the chunk carries a single
    ``speaker`` field, so a merged chunk would attribute one speaker's words to the other
    everywhere that field is read."""
    segments = [
        _segment("Aay speaks briefly.", "S1", 0.0, 1.0),
        _segment("Bee answers briefly.", "S2", 1.0, 2.0),
        _segment("Aay speaks again.", "S1", 2.0, 3.0),
    ]

    chunks = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)

    assert [c["speaker"] for c in chunks] == ["S1", "S2", "S1"]
    assert [c["content"] for c in chunks] == [
        "Aay speaks briefly.",
        "Bee answers briefly.",
        "Aay speaks again.",
    ]
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2]


def test_a_short_turn_merges_into_the_previous_chunk_of_the_same_speaker():
    """The under-20-word merge, on the only input shape that can reach it.

    Turns alternate speakers by construction, so ``chunks[-1]["speaker"] == turn["speaker"]``
    is normally impossible. It becomes reachable when an intervening segment is dropped
    for having no text: S1 / empty-S2 / S1 leaves two adjacent S1 turns. The merged chunk
    must extend its ``end_time`` to the absorbed turn's end, or the chunk's timestamp
    would point at only the first half of its own content.
    """
    segments = [
        _segment("Aay one two.", "S1", 0.0, 1.0),
        _segment("   ", "S2", 1.0, 2.0),
        _segment("Cee three four.", "S1", 2.0, 9.0),
    ]

    chunks = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)

    assert len(chunks) == 1
    assert chunks[0]["speaker"] == "S1"
    assert chunks[0]["content"] == "Aay one two. Cee three four."
    assert chunks[0]["start_time"] == 0.0
    assert chunks[0]["end_time"] == 9.0
    assert chunks[0]["chunk_index"] == 0


def test_a_short_turn_by_a_different_speaker_is_not_merged():
    """The other half of the merge rule. Without this, a merge keyed on length alone
    would fold a two-word interjection into the previous speaker's chunk."""
    segments = [
        _segment("Aay says something reasonably long here.", "S1", 0.0, 3.0),
        _segment("Yeah.", "S2", 3.0, 4.0),
    ]

    chunks = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)

    assert [c["speaker"] for c in chunks] == ["S1", "S2"]
    assert chunks[1]["content"] == "Yeah."
    assert chunks[1]["chunk_index"] == 1


def test_a_segment_with_no_speaker_key_resolves_to_the_canonical_unknown_label():
    """GH #42: the chunk-index writers upstream (``search_indexing_task``,
    ``reindex_task``) always populate ``speaker`` with
    ``canonical_speaker_label()``'s output before handing segments here, so this
    default is a defensive fallback for a segment dict that omits the key
    entirely rather than the normal path. It used to default to a bare
    ``"Unknown"`` — a THIRD spelling of "unidentified" beside
    ``UNKNOWN_SPEAKER_LABEL`` ("Unknown Speaker") and the legacy lowercase
    ``"Unknown speaker"`` some API formatters emitted — which split the
    unidentified population in the index into documents no single `speaker`
    term filter or facet could ever cover together.

    A segment missing the key altogether (not merely ``speaker=None``) is the
    only input that reaches ``dict.get``'s default, so the fixture constructs
    the dict by hand rather than through ``_segment()``, which always sets it.
    """
    from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABEL

    segments = [
        {"start": 0.0, "end": 2.0, "text": "Nobody was attributed to this."},
    ]

    chunks = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)

    assert len(chunks) == 1
    assert chunks[0]["speaker"] == UNKNOWN_SPEAKER_LABEL
    assert chunks[0]["speaker"] != "Unknown"


def test_the_unattributed_default_is_read_from_the_shared_constant_not_a_literal(
    monkeypatch,
):
    """Structural regression guard, not a restatement of the resolver.

    A test that only asserts ``speaker == UNKNOWN_SPEAKER_LABEL`` would keep
    passing even if someone re-hardcoded the literal ``"Unknown Speaker"``
    string inline here instead of importing the constant — the two strings are
    equal today, so a plain equality check cannot tell "sourced from the single
    canonical constant" apart from "coincidentally spells the same value".

    This proves the sourcing instead: monkeypatch the constant `chunking_service`
    imported (module-local binding, so this cannot leak into
    ``app.utils.speaker_labels`` for any other test) to a value nothing could
    coincidentally match, and require the emitted chunk to carry THAT value. A
    hardcoded literal fallback would keep emitting the old string here and this
    assertion would fail — which is exactly the recurrence this test exists to
    catch.
    """
    sentinel = "__CANONICAL_UNKNOWN_SENTINEL__"
    monkeypatch.setattr(chunking_service, "UNKNOWN_SPEAKER_LABEL", sentinel)

    segments = [
        {"start": 0.0, "end": 2.0, "text": "Nobody was attributed to this."},
    ]

    chunks = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)

    assert len(chunks) == 1
    assert chunks[0]["speaker"] == sentinel


# ---------------------------------------------------------------------------
# 6. Tenant scoping on the emitted document
# ---------------------------------------------------------------------------


def test_organization_id_is_written_only_for_an_org_file():
    """The personal-scope query is ``must_not exists organization_id``.

    Writing the key unconditionally (even as ``None``) would make every personal chunk
    fail that filter and vanish from personal search, so "absent" and "null" are not
    interchangeable here.
    """
    segments = [_segment("Aay one two three.", "S1", 0.0, 2.0)]

    personal = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)
    org_scoped = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS, organization_id=9)

    assert "organization_id" not in personal[0]
    assert org_scoped[0]["organization_id"] == 9


def test_collection_ids_default_to_an_empty_list_not_none():
    """``None`` and ``[]`` index differently; the terms filter needs a list."""
    segments = [_segment("Aay one two three.", "S1", 0.0, 2.0)]

    without = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)
    with_ids = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS, collection_ids=[3, 5])

    assert without[0]["collection_ids"] == []
    assert with_ids[0]["collection_ids"] == [3, 5]


# ---------------------------------------------------------------------------
# 7. Word-level timestamps
# ---------------------------------------------------------------------------


#: 60 whitespace-separated tokens with no sentence punctuation, so the turn takes the
#: word-count split path. The turn is *declared* to span 0–100 s while its word timings
#: actually run 0–30 s: interpolation and word timings therefore give different answers
#: for the same input, which is what makes the pair of tests below a real contrast.
_TIMESTAMP_WORDS: list[dict[str, Any]] = [
    {"word": f"v{i:03d}", "start": i * 0.5, "end": i * 0.5 + 0.5} for i in range(60)
]
_TIMESTAMP_TEXT = " ".join(str(word["word"]) for word in _TIMESTAMP_WORDS)


def test_word_timestamps_are_preferred_over_interpolation():
    """When word timings exist the chunk must use them, not a linear estimate.

    Chunk boundaries are how a search hit or a chat citation seeks into the audio, so a
    chunk whose timestamps were estimated from a turn's declared span points at the wrong
    moment whenever the ASR turn boundary is loose. Here the estimate would be 0–50 s;
    the word list gives 0–15 s.
    """
    segments = [_segment(_TIMESTAMP_TEXT, "S1", 0.0, 100.0, words=copy.deepcopy(_TIMESTAMP_WORDS))]

    chunks = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=30, overlap_words=5
    )

    assert [len(chunk["content"].split()) for chunk in chunks] == [30, 30, 10]
    assert [(chunk["start_time"], chunk["end_time"]) for chunk in chunks] == [
        (0.0, 15.0),
        (12.5, 27.5),
        (25.0, 30.0),
    ]


def test_missing_word_timestamps_fall_back_to_linear_interpolation():
    """The control: identical text and turn span, no ``words`` key.

    The differing expectations are the point — if the implementation ignored word timings
    entirely, this test would still pass and the one above would fail, which is exactly
    the discrimination a single test could not provide.
    """
    segments = [_segment(_TIMESTAMP_TEXT, "S1", 0.0, 100.0)]

    chunks = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=30, overlap_words=5
    )

    assert [len(chunk["content"].split()) for chunk in chunks] == [30, 30, 10]
    assert [(chunk["start_time"], chunk["end_time"]) for chunk in chunks] == [
        (0.0, 50.0),
        (41.67, 91.67),
        (83.33, 100.0),
    ]


# ---------------------------------------------------------------------------
# 8. Characterization tests for reported defects — NOT desired behaviour
# ---------------------------------------------------------------------------


def test_grouper_aliases_and_mutates_the_caller_word_timestamps():
    """A chunker must be a PURE FUNCTION of its arguments.

    Regression test. ``_group_segments_into_speaker_turns`` used to seed a turn with
    ``seg.get("words") or []`` — the caller's own list object — and then ``.extend()`` it,
    so the first segment's ``words`` grew to hold the whole turn and grew AGAIN on every
    later call (measured 1 -> 2 -> 3). Fixed by copying: ``list(seg.get("words") or [])``.

    Historical note, kept because it explains why this matters more than tidiness:

    ``_group_segments_into_speaker_turns`` seeds a turn with ``seg.get("words") or []``,
    which for a segment that has word timings is the caller's own list object, then calls
    ``.extend()`` on it for every following segment of the same turn. The first segment's
    ``words`` list therefore grows to hold the whole turn's words, and grows again on
    every subsequent call.

    the aliasing flipped ``_compute_chunk_timestamp``'s
    ``len(word_ts) >= words_before + chunk_word_count`` guard on the second pass, so
    re-indexing one unchanged corpus produced different chunks — issue #433's shape,
    reappearing a layer below where it was fixed.
    """
    segments = [
        _segment("Alpha one.", "S1", 0.0, 2.0, words=[{"word": "Alpha", "start": 0.0, "end": 0.5}]),
        _segment("Beta two.", "S1", 2.0, 4.0, words=[{"word": "Beta", "start": 2.0, "end": 2.5}]),
        _segment(
            "Gamma three.", "S1", 4.0, 6.0, words=[{"word": "Gamma", "start": 4.0, "end": 4.5}]
        ),
    ]
    before = copy.deepcopy(segments)

    chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)
    assert segments == before, "chunking mutated the caller's segments"

    # Twice more: aliasing grew the list on EVERY call, so one pass is not enough to prove it.
    chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)
    chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS)
    assert segments == before, "chunking mutated the caller's segments on a later call"
    assert [w["word"] for w in segments[0]["words"]] == ["Alpha"]


def test_rechunking_the_same_list_object_can_change_the_output():
    """Chunking the same list twice must give identical chunks — issue #433's shape.

    Direct consequence of the aliasing above. The accumulated word list is longer on the
    second call, which flips ``_compute_chunk_timestamp``'s
    ``len(word_ts) >= words_before + chunk_word_count`` guard for chunks that had fallen
    back to interpolation — so a chunk's timestamps change with no change to the input.

    This is the issue-#433 shape (a re-index producing different documents for unchanged
    input). It needs a caller that reuses one segments list across two chunking passes;
    a sweep of 330 target/overlap/segment-count combinations reproduces it. Reported, not
    fixed. **When the aliasing is fixed this test must fail** — replace it with
    ``assert first == second``.
    """
    segments = []
    tick = 0.0
    index = 0
    for seg_no in range(4):
        tokens, words = [], []
        for _ in range(10):
            token = f"w{index:03d}"
            tokens.append(token)
            words.append({"word": token, "start": round(tick, 2), "end": round(tick + 0.5, 2)})
            tick += 0.5
            index += 1
        segment = _segment(" ".join(tokens), "S1", seg_no * 5.0, (seg_no + 1) * 5.0)
        if seg_no < 2:  # only the first two segments carry word timings
            segment["words"] = words
        segments.append(segment)

    first = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=30, overlap_words=5
    )
    second = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=30, overlap_words=5
    )

    # IDENTICAL, field for field. Before the aliasing fix these diverged: chunk 0 ended at
    # 15.0 on the first call and 10.0 on the second, from the SAME input object, because the
    # accumulated word list flipped _compute_chunk_timestamp's interpolate-vs-use-timings
    # guard. Content matched throughout, which is why the defect survived review — only the
    # TIMESTAMPS moved, and only on the second pass.
    assert first == second, "chunking the same list twice produced different chunks"
    assert first[0]["end_time"] == second[0]["end_time"] == 15.0

    # A third pass, because the aliasing grew the list on every call rather than once.
    third = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=30, overlap_words=5
    )
    assert third == first


def test_a_cjk_transcript_is_split_into_many_chunks():
    """FIXED (issue #448). This test was the defect's characterization and said so.

    It previously asserted ``len(chunks) == 1`` and instructed its successor:
    *"When CJK segmentation lands, this test must fail — replace the count with
    the expected split."* It did exactly that, which is the whole point of pinning
    a defect rather than merely filing it.

    What was wrong, and all of it had to be fixed together:

    * ``len(text.split())`` reported the entire transcript as **one word**, so
      ``1 <= target_words`` held and the splitter was never reached;
    * unmapped languages were handed to the ENGLISH punkt model, which returns
      Japanese as one sentence — punkt had not failed, it was merely wrong;
    * the regex fallback listed ``。！？`` but required whitespace after them,
      which CJK text does not have.

    The preconditions are kept as assertions, not comments: they are what makes
    the outcome meaningful, and if ``str.split()`` ever stopped reporting 1 here
    the test would be measuring something else.
    """
    japanese = "".join(f"これはテストの文章です{i}。" for i in range(200))
    segments = [_segment(japanese, "S1", 0.0, 600.0)]

    chunks = chunk_transcript_by_speaker_turns(segments, **BASE_KWARGS, language="ja")

    assert len(japanese) == 2890
    assert len(japanese.split()) == 1, "precondition: whitespace cannot delimit this text"

    assert len(chunks) > 5, f"2,890 characters produced only {len(chunks)} chunk(s)"
    assert max(len(c["content"]) for c in chunks) < len(japanese) / 3

    # Nothing invented and nothing lost: the concatenated chunks must still be
    # the transcript. A splitter that inserted separators would corrupt both what
    # the reader sees and what the embedding model receives.
    for chunk in chunks:
        assert chunk["content"] in japanese, "chunk text is not a slice of the transcript"


class _AbbreviationAwareTokenizer:
    """Stand-in for punkt: splits on '. ' except after a known abbreviation.

    Module level so both splitter tests share ONE definition. Injected rather than
    depending on real NLTK data, which is present in the dev image but not
    necessarily in CI or the host venv — a test that silently degraded to the regex
    on both sides would assert that the splitters agree, which is the opposite of
    what it is for.
    """

    _ABBREVIATIONS = ("Dr.", "Mr.", "p.m.", "U.S.", "e.g.")

    def tokenize(self, raw: str) -> list[str]:
        sentences: list[str] = []
        current: list[str] = []
        for token in raw.split():
            current.append(token)
            if token.endswith(".") and token not in self._ABBREVIATIONS:
                sentences.append(" ".join(current))
                current = []
        if current:
            sentences.append(" ".join(current))
        return sentences


def test_sentence_splitter_availability_changes_the_chunk_boundaries(sentence_splitter_state):
    """The two splitters DO disagree — which is why the choice must be latched (#449).

    ``split_into_sentences`` uses NLTK punkt when it can and a bare regex otherwise, and
    the two disagree on abbreviations — punkt keeps ``Dr.`` and ``p.m.`` inside a
    sentence, the regex splits on them. Sentence boundaries drive chunk boundaries, so a
    worker with the punkt corpora and one without produce different documents from the
    same transcript.

    That cross-process difference is **not** fixed here and cannot be fixed by this
    module alone: it is decided by whether the image has the corpora. What IS fixed is
    the far worse version — the splitter changing *within a single re-index*, which
    ``test_the_splitter_choice_is_latched_for_the_process`` now pins. This test remains
    because it is the evidence that the latch matters: if the two splitters agreed, none
    of it would be worth the machinery.

    Hermetic — the "punkt" side uses an injected abbreviation-aware tokenizer, so the
    result does not depend on whether NLTK data is present in this environment.
    """
    unit = (
        "Dr. Smith met Mr. Jones at 3 p.m. in the U.S. office. "
        "They reviewed the Q3 numbers e.g. revenue and churn carefully together. "
    )
    text = (unit * 12).strip()
    segments = [_segment(text, "S1", 0.0, 300.0)]

    _force_regex_sentence_split()
    with_regex = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=40, overlap_words=8
    )

    chunking_service._nltk_load_failed = False
    chunking_service._nltk_tokenizers["english"] = _AbbreviationAwareTokenizer()
    with_punkt = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=40, overlap_words=8
    )

    assert len(with_regex) == 9
    assert len(with_punkt) == 12
    assert with_regex != with_punkt


def test_the_splitter_choice_is_latched_for_the_process(sentence_splitter_state):
    """Once punkt has failed to load, nothing switches the splitter back mid-run (#449).

    The defect this replaces: a failed load set a five-minute
    ``_nltk_unavailable_until`` cooldown, after which the module tried punkt again. A
    re-index taking longer than five minutes therefore chunked its early files with the
    regex and its later files with punkt — **the same corpus, chunked two ways, in one
    pass**, with nothing recording which. That is issue #433's failure mode arriving by a
    second route.

    The latch is asserted through the module's own selection logic, not by reading the
    flag: a tokenizer is made available AFTER the failure is latched, exactly as the
    cooldown expiring used to make one available, and the output must not move.
    """
    unit = "Dr. Smith met Mr. Jones at 3 p.m. in the U.S. office. "
    segments = [_segment((unit * 12).strip(), "S1", 0.0, 300.0)]

    _force_regex_sentence_split()
    before = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=40, overlap_words=8
    )

    # punkt becomes loadable. Under the cooldown this is precisely the moment the
    # splitter flipped; under the latch it must be ignored for this process.
    chunking_service._nltk_tokenizers["english"] = _AbbreviationAwareTokenizer()
    after = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=40, overlap_words=8
    )

    assert after == before, (
        "The sentence splitter changed part-way through a run: a tokenizer becoming "
        "available after a failed load switched the boundaries. Chunk boundaries must "
        "be stable for the life of the process."
    )

    # The control: clearing the latch — which only tests do — must let punkt back in,
    # or the assertion above would hold for the trivial reason that the injected
    # tokenizer is never consulted at all.
    chunking_service.reset_sentence_splitter_state()
    chunking_service._nltk_tokenizers["english"] = _AbbreviationAwareTokenizer()
    unlatched = chunk_transcript_by_speaker_turns(
        segments, **BASE_KWARGS, target_words=40, overlap_words=8
    )
    assert unlatched != before, (
        "Clearing the latch did not change the boundaries, so this test cannot "
        "distinguish a working latch from an unused tokenizer."
    )
