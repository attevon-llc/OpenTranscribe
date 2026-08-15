"""Tests for ``clean_garbage_words`` in ``app/tasks/transcription/finalize.py``.

``clean_garbage_words`` is the last thing that **edits transcript text** before it is
persisted. Both finalize paths call it (``_process_transcription_result`` L147 and
``_process_and_save_critical`` L270) on *every* segment of *every* file whenever the
DB-backed ``garbage_cleanup_enabled`` setting is on — which it is by default — and its
output goes straight into ``save_transcript_segments``.

Its job is to replace WhisperX's noise artefacts (very long unbroken tokens produced from
fan hum or static) with ``[background noise]``. The risk is that it is a *silent* text
rewriter: it returns a count nobody checks against a threshold, and there is no way to
recover the original text from the database afterwards. An over-eager threshold, or a
rewrite that desynchronises text from its word timestamps, is invisible until a user reads
their own transcript. It had no tests.

Pinned here:

1. **The replacement rule and its exact boundary** — ``>`` not ``>=``, per-token not
   per-segment.
2. **The count is the number of replacements**, not of affected segments.
3. **Non-text segment keys survive** the copy.
4. Four **characterization tests for open defects**, each named ``..._defect`` in its
   docstring: word-timestamp desynchronisation, a shared mutable ``words`` list, whitespace
   normalisation of untouched segments, and the unreachable ``" " not in word`` guard.

Following the characterization-test convention of ``tests/unit/test_chunking_service.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.tasks.transcription.finalize import clean_garbage_words

#: The module's own default threshold, restated so a change to it is a visible diff here.
DEFAULT_MAX_WORD_LENGTH = 50


def _noise(length: int) -> str:
    """A single unbroken token of ``length`` characters — the WhisperX artefact shape."""
    return "x" * length


# --------------------------------------------------------------------------------------
# 1. The replacement rule and its boundary
# --------------------------------------------------------------------------------------


def test_a_token_longer_than_the_threshold_is_replaced_and_counted():
    segments = [{"text": f"the fan said {_noise(120)} loudly"}]

    cleaned, count = clean_garbage_words(segments, max_word_length=50)

    assert count == 1
    assert cleaned[0]["text"] == "the fan said [background noise] loudly"


def test_the_threshold_is_exclusive_at_exactly_max_word_length():
    """``len(word) > max_word_length`` — a token of exactly the threshold is legitimate.

    Real words do reach these lengths in agglutinative languages and in chemistry, so the
    off-by-one here is the difference between cleaning noise and deleting content.
    """
    at_threshold = _noise(DEFAULT_MAX_WORD_LENGTH)
    over_threshold = _noise(DEFAULT_MAX_WORD_LENGTH + 1)

    kept, kept_count = clean_garbage_words([{"text": at_threshold}], DEFAULT_MAX_WORD_LENGTH)
    replaced, replaced_count = clean_garbage_words(
        [{"text": over_threshold}], DEFAULT_MAX_WORD_LENGTH
    )

    assert kept_count == 0
    assert kept[0]["text"] == at_threshold
    assert replaced_count == 1
    assert replaced[0]["text"] == "[background noise]"


def test_the_default_threshold_is_fifty_characters():
    """The default applies wherever a caller omits the DB-configured value."""
    at_default, count_at = clean_garbage_words([{"text": _noise(50)}])
    over_default, count_over = clean_garbage_words([{"text": _noise(51)}])

    assert count_at == 0
    assert at_default[0]["text"] == _noise(50)
    assert count_over == 1
    assert over_default[0]["text"] == "[background noise]"


def test_the_count_is_replacements_not_affected_segments():
    """Two artefacts in one segment count twice — the count is an ops signal, not a flag."""
    segments = [
        {"text": f"{_noise(80)} and {_noise(90)}"},
        {"text": "entirely clean"},
        {"text": _noise(200)},
    ]

    cleaned, count = clean_garbage_words(segments, 50)

    assert count == 3
    assert cleaned[0]["text"] == "[background noise] and [background noise]"
    assert cleaned[1]["text"] == "entirely clean"
    assert cleaned[2]["text"] == "[background noise]"


def test_a_transcript_with_no_artefacts_is_returned_with_a_zero_count():
    segments = [{"text": "hello there"}, {"text": "general kenobi"}]

    cleaned, count = clean_garbage_words(segments, 50)

    assert count == 0
    assert [s["text"] for s in cleaned] == ["hello there", "general kenobi"]


def test_an_empty_segment_list_is_a_no_op():
    assert clean_garbage_words([], 50) == ([], 0)


def test_a_segment_without_a_text_key_becomes_empty_text_rather_than_raising():
    """Cloud-ASR adapters have shipped segments without ``text``; this must not raise.

    An exception here is raised inside the GPU worker's critical path, after the transcript
    exists in memory but before it is written — the file is then stranded in PROCESSING.
    """
    cleaned, count = clean_garbage_words([{"start": 0.0, "end": 1.0}], 50)

    assert count == 0
    assert cleaned[0]["text"] == ""
    assert cleaned[0]["start"] == 0.0


def test_every_other_segment_key_survives_the_rewrite():
    """Only ``text`` may change — speaker attribution and timings must pass through."""
    segments = [
        {
            "start": 4.5,
            "end": 9.25,
            "text": f"noise {_noise(99)}",
            "speaker": "SPEAKER_01",
            "speaker_id": 77,
            "confidence": 0.88,
            "is_overlap": True,
        }
    ]

    cleaned, count = clean_garbage_words(segments, 50)

    assert count == 1
    assert cleaned[0]["start"] == 4.5
    assert cleaned[0]["end"] == 9.25
    assert cleaned[0]["speaker"] == "SPEAKER_01"
    assert cleaned[0]["speaker_id"] == 77
    assert cleaned[0]["confidence"] == 0.88
    assert cleaned[0]["is_overlap"] is True


def test_the_caller_s_segment_dicts_are_not_rewritten_in_place():
    """``segment.copy()`` — the caller keeps the original text for logging and retry."""
    original = {"text": f"before {_noise(70)} after"}
    segments = [original]

    cleaned, count = clean_garbage_words(segments, 50)

    assert count == 1
    assert original["text"] == f"before {_noise(70)} after"
    assert cleaned[0] is not original


# --------------------------------------------------------------------------------------
# 2. Characterization tests for OPEN defects
# --------------------------------------------------------------------------------------


def test_word_timestamps_are_cleaned_alongside_the_text():
    """CHARACTERIZATION — pins current WRONG behaviour. DEFECT: finalize.py L45-L65.

    ``clean_garbage_words`` rewrites ``segment["text"]`` but never touches
    ``segment["words"]``. Both are persisted, side by side, by
    ``save_transcript_segments`` (``storage.py`` L88 and L93), and the frontend renders the
    word array for click-to-seek and karaoke highlighting.

    So after cleanup the stored transcript says ``[background noise]`` while the stored
    word-level data still contains the raw artefact — the two views of the same segment
    disagree, and the word count no longer matches the text. The whole point of the feature
    (not showing the user a 200-character noise token) is defeated in the word view.

    The word array is persisted ALONGSIDE the text and is what the UI renders for
    click-to-seek, so cleaning only the text left the raw artefact visible in the view
    users interact with most (issue #456). Timings and scores must survive — only the
    token is replaced.
    """
    garbage = _noise(120)
    segments: list[dict[str, Any]] = [
        {
            "text": f"hello {garbage}",
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.4, "score": 0.9},
                {"word": garbage, "start": 0.4, "end": 3.0, "score": 0.1},
            ],
        }
    ]

    cleaned, count = clean_garbage_words(segments, 50)

    assert count == 1
    assert cleaned[0]["text"] == "hello [background noise]"
    stored_words = cleaned[0]["words"]
    assert len(stored_words) == 2, "entries must be rewritten, never dropped"
    assert stored_words[1]["word"] == "[background noise]"
    assert not any(len(w["word"]) > 50 for w in stored_words), "garbage survived in words"
    # Timings are the whole point of the word array — replacing the token must not
    # cost the seek position.
    assert stored_words[1]["start"] == 0.4
    assert stored_words[1]["end"] == 3.0
    assert stored_words[1]["score"] == 0.1


def test_the_cleaned_copy_does_not_share_its_words_list():
    """CHARACTERIZATION — pins current WRONG behaviour. DEFECT: finalize.py L61.

    ``segment.copy()`` is a **shallow** copy, so the "cleaned" segment and the caller's
    original share one ``words`` list object. ``test_the_caller_s_segment_dicts_are_not_
    rewritten_in_place`` above is therefore only true of ``text``: anything that later
    mutates ``cleaned[i]["words"]`` — which is exactly what the fix for the defect above
    would do — silently edits the caller's ``result["segments"]`` too, which the finalize
    path goes on to use for embeddings and indexing.

    WHEN FIXED (``copy.deepcopy``, or an explicit new list for ``words``) this test will
    The cleaned segment must own its own list (issue #456). `segment.copy()` is shallow,
    so a shared list means cleaning `words` in place would also rewrite the caller's
    `result["segments"]` — which is then used for embeddings and indexing.
    """
    words = [{"word": "hello", "start": 0.0, "end": 0.4}]
    original = {"text": f"hello {_noise(80)}", "words": words}

    cleaned, count = clean_garbage_words([original], 50)

    assert count == 1
    assert cleaned[0]["words"] is not words, "the copy must not share the caller's list"
    assert words[0]["word"] == "hello", "the caller's own entries must be untouched"


def test_an_unchanged_segment_keeps_its_text_byte_for_byte():
    """CHARACTERIZATION — pins current WRONG behaviour. DEFECT: finalize.py L62.

    ``" ".join(text.split())`` is applied to **every** segment, not just the ones that had
    an artefact replaced. Whisper emits segment text with a leading space
    (``" Hello there."``); cleanup silently strips it, collapses any internal run of
    whitespace, and drops newlines. With the feature enabled — the default — every
    transcript in the product is whitespace-normalised on the way to the database, and the
    original spacing is not recoverable.

    That also means enabling or disabling the setting changes stored text for files that
    contained no garbage at all, so two otherwise identical runs are not byte-comparable.

    Fixed in issue #456: the rejoin now happens only for a segment that actually had an
    artefact replaced, so a segment with nothing to clean comes back byte-for-byte. That
    also makes the setting's on/off states byte-comparable for files containing no
    garbage, which they were not before.
    """
    segments = [{"text": " Hello  there.\nSecond line. "}]

    cleaned, count = clean_garbage_words(segments, 50)

    assert count == 0, "nothing in this segment is an artefact"
    assert cleaned[0]["text"] == " Hello  there.\nSecond line. ", (
        "an untouched segment was whitespace-normalised on its way to the database"
    )


def test_the_no_space_guard_can_never_fire_so_a_long_run_of_words_is_wiped_wholesale():
    """CHARACTERIZATION — pins current WRONG behaviour. DEFECT: finalize.py L53.

    The condition is ``if len(word) > max_word_length and " " not in word``. But ``word``
    comes from ``text.split()``, which splits *on* whitespace — no token it yields can
    ever contain a space. The second clause is dead: it can never be False, so it protects
    nothing.

    The comment above it ("spaces would indicate it's not a single garbage word") describes
    an intent the code does not implement. The observable consequence is below: a passage of
    several legitimate long tokens separated by spaces is not recognised as "not a single
    garbage word" — every token over the threshold is replaced individually, so a run of
    real speech can be reduced to a row of ``[background noise]`` markers.

    WHEN FIXED — either by deleting the dead clause (behaviour unchanged, and this test
    stays green) or by implementing the commented intent against the *segment* text
    (behaviour changes, and this test fails) — decide deliberately. If the intent is
    implemented, replace the assertion with one that the text is returned unchanged.
    """
    two_long_tokens = f"{_noise(60)} {_noise(70)}"

    cleaned, count = clean_garbage_words([{"text": two_long_tokens}], 50)

    assert count == 2
    # WRONG per the comment's stated intent: the run contains a space, so it should have
    # been left alone as "not a single garbage word".
    assert cleaned[0]["text"] == "[background noise] [background noise]"


@pytest.mark.parametrize("threshold", [0, 1, 4])
def test_a_tiny_threshold_replaces_essentially_everything(threshold: int):
    """The DB-configured threshold is unvalidated, so pin what a bad value actually does.

    ``system_settings_service.get_garbage_cleanup_config`` reads ``max_word_length`` from
    admin-editable settings with no lower bound. At a small value the function destroys the
    transcript rather than erroring, and the only signal is the count.
    """
    tokens = ["alpha", "beta", "gamma"]
    segments = [{"text": " ".join(tokens)}]

    cleaned, count = clean_garbage_words(segments, threshold)

    expected = [w for w in tokens if len(w) > threshold]
    assert len(expected) > 0, "fixture must contain tokens over the threshold"
    assert count == len(expected)
    assert "[background noise]" in cleaned[0]["text"]
