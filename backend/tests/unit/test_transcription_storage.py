"""Persistence tests for ``app/tasks/transcription/storage.py``.

This module is the **single write path that ends a transcription**. Every ASR route in
the product — local WhisperX, the cloud providers, the legacy task, and the rediarize
retry — funnels into ``save_transcript_segments()`` and
``update_media_file_transcription_status()`` (see ``finalize.py`` L174/L287,
``rediarize_task.py`` L341). It is also the point where ``MediaFile.status`` flips to
``COMPLETED``.

That makes its failure mode the worst one in ``app/tasks``: nothing here raises a 500 that
anybody sees. A defect either **loses transcript rows**, **doubles them on a retry**, or
**writes a wrong scalar onto the MediaFile** — and the user just gets a file that looks
finished and is quietly wrong. It had no tests at all.

What is pinned here, in order:

1. **Round-trip fidelity** — every segment reaches the DB with its own field values, not
   its neighbour's.
2. **Re-save replaces, never appends.** The delete-then-insert guard at L40-L50 is the only
   thing standing between a retried transcription and a doubled transcript.
3. **Word-timestamp filtering** — entries without both ``start`` and ``end`` are dropped,
   and a segment left with nothing stores SQL ``NULL`` rather than ``[]``.
4. **The completion state transition** — status, language, and the model-provenance
   columns.
5. Two **characterization tests for open defects** in the duration write at L147
   (``test_completing_with_no_segments_zeroes_the_probed_duration`` and
   ``test_duration_comes_from_the_last_segment_not_the_latest_one``), and one in
   ``get_unique_speaker_names`` at L200
   (``test_unique_speaker_name_order_is_not_stable_across_processes``). Each asserts
   today's WRONG behaviour on purpose so the defect cannot drift while it is open, and
   each docstring says what to replace it with once a fix lands.

Following the characterization-test convention of ``tests/unit/test_chunking_service.py``.
"""

from __future__ import annotations

import ast
import uuid as uuid_module
from typing import Any

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.tasks.transcription.storage import generate_full_transcript
from app.tasks.transcription.storage import get_unique_speaker_names
from app.tasks.transcription.storage import save_transcript_segments
from app.tasks.transcription.storage import update_media_file_transcription_status

#: A duration a real pipeline would already have written from ffprobe, in
#: ``metadata_extractor.py`` L455, well before transcription finishes.
PROBED_DURATION = 3600.0


def _segment(start: float, end: float, text: str, **extra: Any) -> dict[str, Any]:
    """Build a processed segment in the shape ``process_segments_with_speakers`` emits."""
    seg: dict[str, Any] = {"start": start, "end": end, "text": text}
    seg.update(extra)
    return seg


@pytest.fixture
def media_file(db_session, normal_user) -> MediaFile:
    """A MediaFile mid-processing, with the duration ffprobe would already have set."""
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=normal_user.id,
        filename="storage_test.mp3",
        storage_path=f"user_{normal_user.id}/storage_test.mp3",
        file_size=4096,
        content_type="audio/mpeg",
        status=FileStatus.PROCESSING,
        duration=PROBED_DURATION,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _stored_segments(db_session, file_id: int) -> list[TranscriptSegment]:
    """Every persisted segment for a file, in start-time order."""
    rows: list[TranscriptSegment] = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(TranscriptSegment.start_time)
        .all()
    )
    return rows


# --------------------------------------------------------------------------------------
# 1. Round-trip fidelity
# --------------------------------------------------------------------------------------


def test_every_segment_round_trips_with_its_own_field_values(db_session, media_file):
    """Each saved row carries its own start/end/text/confidence, not a neighbour's.

    A bulk insert that reuses one record dict, or zips the columns wrongly, produces
    exactly the right number of rows with the wrong contents — which a count-only
    assertion would pass.
    """
    segments = [
        _segment(0.0, 1.5, "first", confidence=0.91),
        _segment(1.5, 3.25, "second", confidence=0.82),
        _segment(3.25, 7.0, "third", confidence=0.73),
    ]

    save_transcript_segments(db_session, media_file.id, segments)

    rows = _stored_segments(db_session, media_file.id)
    assert len(rows) == 3, "expected one row per input segment"
    assert [r.text for r in rows] == ["first", "second", "third"]
    assert [r.start_time for r in rows] == [0.0, 1.5, 3.25]
    assert [r.end_time for r in rows] == [1.5, 3.25, 7.0]
    assert [r.confidence for r in rows] == [
        pytest.approx(0.91),
        pytest.approx(0.82),
        pytest.approx(0.73),
    ]
    assert len({r.uuid for r in rows}) == 3, "each row needs its own uuid"


def test_saving_an_empty_segment_list_writes_nothing_and_does_not_raise(db_session, media_file):
    """A silent recording must not produce a phantom row or an exception."""
    save_transcript_segments(db_session, media_file.id, [])

    assert _stored_segments(db_session, media_file.id) == []


def test_overlap_marking_round_trips_and_non_overlapping_segments_stay_clean(
    db_session, media_file
):
    """Overlap flags group simultaneous speech in the UI; they must not bleed across rows.

    Note what this test deliberately does NOT claim: that the ``uuid_module.UUID(...)``
    coercion at L66 is load-bearing. It is not — deleting it leaves this test green,
    because psycopg2 adapts the string itself. The fail-fast behaviour that coercion *does*
    buy is pinned by the next test instead.
    """
    group_id = uuid_module.uuid4()
    segments = [
        _segment(
            0.0,
            1.0,
            "alpha",
            is_overlap=True,
            overlap_group_id=str(group_id),
            overlap_confidence=0.64,
        ),
        _segment(1.0, 2.0, "beta", is_overlap=False),
    ]

    save_transcript_segments(db_session, media_file.id, segments)

    rows = _stored_segments(db_session, media_file.id)
    assert len(rows) == 2
    assert rows[0].is_overlap is True
    assert rows[0].overlap_group_id == group_id
    assert rows[0].overlap_confidence == pytest.approx(0.64)
    assert rows[1].is_overlap is False
    assert rows[1].overlap_group_id is None
    assert rows[1].overlap_confidence is None


def test_a_malformed_overlap_group_id_fails_in_python_not_inside_the_transaction(
    db_session, media_file
):
    """The L66 coercion is what makes a bad group id a ``ValueError`` and not a DB error.

    ``save_transcript_segments`` runs inside the finalize path's ``session_scope``. A
    malformed value that reaches Postgres raises a ``DataError`` that puts the connection
    into "current transaction is aborted", so every *subsequent* statement in that scope —
    including ``update_media_file_transcription_status`` — fails too and the file is left
    in PROCESSING forever. Failing in Python keeps the session usable.

    Drop the coercion and this test fails with ``psycopg2.errors.InvalidTextRepresentation``
    instead of ``ValueError``.
    """
    with pytest.raises(ValueError):
        save_transcript_segments(
            db_session,
            media_file.id,
            [_segment(0.0, 1.0, "bad", is_overlap=True, overlap_group_id="not-a-uuid")],
        )

    # The session is still usable — a subsequent write in the same scope succeeds. Under a
    # DataError the connection would be in "current transaction is aborted" and this raises.
    save_transcript_segments(db_session, media_file.id, [_segment(0.0, 1.0, "recovered")])
    assert [r.text for r in _stored_segments(db_session, media_file.id)] == ["recovered"]


# --------------------------------------------------------------------------------------
# 2. Re-save replaces rather than appends
# --------------------------------------------------------------------------------------


def test_resaving_replaces_the_previous_transcript_instead_of_doubling_it(db_session, media_file):
    """The retry path (rediarize, recovery re-run) must not append to the old transcript.

    Without the delete at L48 a re-run leaves both copies interleaved, and the file still
    reads as COMPLETED. The assertion is on the surviving *text*, not just the count: a
    delete that removed the new rows instead of the old ones would keep the count right.
    """
    save_transcript_segments(
        db_session,
        media_file.id,
        [
            _segment(0.0, 1.0, "old-one"),
            _segment(1.0, 2.0, "old-two"),
            _segment(2.0, 3.0, "old-three"),
        ],
    )
    assert len(_stored_segments(db_session, media_file.id)) == 3, "precondition: first save landed"

    save_transcript_segments(
        db_session,
        media_file.id,
        [_segment(0.0, 1.0, "new-one"), _segment(1.0, 2.0, "new-two")],
    )

    rows = _stored_segments(db_session, media_file.id)
    assert [r.text for r in rows] == ["new-one", "new-two"]


def test_resaving_only_clears_the_target_files_segments(db_session, normal_user, media_file):
    """The delete is filtered by ``media_file_id`` — a sibling file's transcript survives.

    An unfiltered ``TranscriptSegment.delete()`` would wipe the whole table on every
    completion, which is unrecoverable and would still leave this file looking correct.
    """
    other = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=normal_user.id,
        filename="bystander.mp3",
        storage_path=f"user_{normal_user.id}/bystander.mp3",
        file_size=1024,
        content_type="audio/mpeg",
        status=FileStatus.COMPLETED,
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    save_transcript_segments(db_session, other.id, [_segment(0.0, 1.0, "bystander-text")])
    save_transcript_segments(db_session, media_file.id, [_segment(0.0, 1.0, "target-a")])
    save_transcript_segments(db_session, media_file.id, [_segment(0.0, 1.0, "target-b")])

    assert [r.text for r in _stored_segments(db_session, other.id)] == ["bystander-text"]
    assert [r.text for r in _stored_segments(db_session, media_file.id)] == ["target-b"]


def test_an_empty_resave_leaves_the_existing_transcript_untouched(db_session, media_file):
    """``save_transcript_segments`` returns early on ``[]`` — *before* the delete.

    That ordering is load-bearing: an empty result from a failed re-run must not erase a
    transcript the user already has. Pinning it stops the early-return being "tidied" to
    after the delete.
    """
    save_transcript_segments(db_session, media_file.id, [_segment(0.0, 1.0, "keep-me")])

    save_transcript_segments(db_session, media_file.id, [])

    assert [r.text for r in _stored_segments(db_session, media_file.id)] == ["keep-me"]


# --------------------------------------------------------------------------------------
# 3. Word-level timestamp filtering
# --------------------------------------------------------------------------------------


def test_word_entries_without_both_timestamps_are_dropped_and_the_rest_survive(
    db_session, media_file
):
    """Word timestamps drive the click-to-seek UI; a half-specified word would break it."""
    segments = [
        _segment(
            0.0,
            2.0,
            "one two three four",
            words=[
                {"word": "one", "start": 0.0, "end": 0.4, "score": 0.9},
                {"word": "two", "start": 0.4},  # no end
                {"word": "three", "end": 1.4},  # no start
                {"word": "four", "start": 1.4, "end": 2.0, "probability": 0.55},
            ],
        )
    ]

    save_transcript_segments(db_session, media_file.id, segments)

    rows = _stored_segments(db_session, media_file.id)
    assert len(rows) == 1
    stored_words = rows[0].words
    assert stored_words is not None, "the segment had two usable words"
    assert [w["word"] for w in stored_words] == ["one", "four"]
    assert stored_words[0]["score"] == pytest.approx(0.9)
    # `probability` is the faster-whisper spelling and is read as the score fallback.
    assert stored_words[1]["score"] == pytest.approx(0.55)


def test_a_word_without_a_score_defaults_to_full_confidence(db_session, media_file):
    """Neither ``score`` nor ``probability`` present → 1.0, never a missing key."""
    save_transcript_segments(
        db_session,
        media_file.id,
        [_segment(0.0, 1.0, "solo", words=[{"word": "solo", "start": 0.0, "end": 1.0}])],
    )

    rows = _stored_segments(db_session, media_file.id)
    assert len(rows) == 1
    assert rows[0].words == [{"word": "solo", "start": 0.0, "end": 1.0, "score": 1.0}]


def test_a_segment_whose_words_are_all_unusable_stores_null_not_an_empty_list(
    db_session, media_file
):
    """``[]`` and ``NULL`` read differently downstream — pin which one is written."""
    save_transcript_segments(
        db_session,
        media_file.id,
        [
            _segment(0.0, 1.0, "no usable words", words=[{"word": "x"}]),
            _segment(1.0, 2.0, "no words key at all"),
            _segment(2.0, 3.0, "explicitly empty", words=[]),
        ],
    )

    rows = _stored_segments(db_session, media_file.id)
    assert len(rows) == 3
    assert [r.words for r in rows] == [None, None, None]


# --------------------------------------------------------------------------------------
# 4. The completion state transition
# --------------------------------------------------------------------------------------


def test_completion_flips_status_and_records_the_processing_provenance(db_session, media_file):
    """The transition the whole pipeline exists to reach, asserted on the real row."""
    segments = [_segment(0.0, 10.0, "hello"), _segment(10.0, 42.5, "world")]

    update_media_file_transcription_status(
        db_session,
        media_file.id,
        segments,
        language="de",
        whisper_model="large-v3-turbo",
        diarization_model="pyannote/speaker-diarization-community-1",
        embedding_mode="v4",
        asr_provider="deepgram",
        asr_model="nova-3",
        diarization_disabled=False,
    )

    db_session.refresh(media_file)
    assert media_file.status == FileStatus.COMPLETED
    assert media_file.language == "de"
    assert media_file.completed_at is not None
    assert media_file.whisper_model == "large-v3-turbo"
    assert media_file.diarization_model == "pyannote/speaker-diarization-community-1"
    assert media_file.embedding_mode == "v4"
    assert media_file.asr_provider == "deepgram"
    assert media_file.asr_model == "nova-3"
    assert media_file.diarization_disabled is False
    assert media_file.duration == pytest.approx(42.5)


def test_omitted_provenance_fields_do_not_erase_what_is_already_stored(db_session, media_file):
    """``None`` means "no new information", not "clear the column".

    The cloud-ASR path passes ``diarization_model=None`` when diarization is off; that must
    not blank a value an earlier pass recorded.
    """
    media_file.whisper_model = "large-v3"
    media_file.embedding_mode = "v3"
    db_session.commit()

    update_media_file_transcription_status(
        db_session,
        media_file.id,
        [_segment(0.0, 5.0, "x")],
        language="en",
        whisper_model=None,
        diarization_model=None,
        embedding_mode=None,
    )

    db_session.refresh(media_file)
    assert media_file.whisper_model == "large-v3"
    assert media_file.embedding_mode == "v3"
    assert media_file.status == FileStatus.COMPLETED


def test_diarization_disabled_is_recorded_even_though_it_is_falsy(db_session, media_file):
    """``diarization_disabled`` is written unconditionally — ``True`` must survive."""
    update_media_file_transcription_status(
        db_session, media_file.id, [_segment(0.0, 5.0, "x")], diarization_disabled=True
    )

    db_session.refresh(media_file)
    assert media_file.diarization_disabled is True


def test_a_missing_media_file_is_logged_and_skipped_rather_than_raising(db_session, media_file):
    """An id with no row must not blow up the finalize path or touch a sibling row.

    ``update_media_file_transcription_status`` is called inside the GPU worker's critical
    path; an unhandled exception there strands the *other* files in the batch too.
    """
    missing_id = media_file.id + 10_000_000

    update_media_file_transcription_status(db_session, missing_id, [_segment(0.0, 9.0, "x")])

    db_session.refresh(media_file)
    assert media_file.status == FileStatus.PROCESSING, "the real file must be untouched"
    assert media_file.duration == pytest.approx(PROBED_DURATION)
    assert db_session.query(MediaFile).filter(MediaFile.id == missing_id).first() is None


# --------------------------------------------------------------------------------------
# 5. Characterization tests for OPEN defects
# --------------------------------------------------------------------------------------


def test_completing_with_no_segments_keeps_the_probed_duration(db_session, media_file):
    """A file that produced no segments must KEEP its probed duration (issue #455).

    It used to write **0.0**: ``duration = segments[-1]["end"] if segments else 0.0``
    unconditionally overwrote the column, so a silent or music-only recording — or a
    provider that returned nothing — lost the real ffprobe duration written by
    ``metadata_extractor.py`` L455 and was then marked COMPLETED. The true length was not
    recoverable from the DB afterwards.

    Not cosmetic downstream: ``recovery_tasks.youtube_metadata_backfill`` matches recovered
    rows to YouTube metadata *by duration*, so a zeroed row could never be matched again,
    and the gallery renders ``formatted_duration`` from this column.
    """
    assert media_file.duration == pytest.approx(PROBED_DURATION), "precondition: ffprobe ran"

    update_media_file_transcription_status(db_session, media_file.id, [])

    db_session.refresh(media_file)
    assert media_file.duration == pytest.approx(PROBED_DURATION), (
        "the probed duration was overwritten for a file with no segments"
    )
    assert media_file.status == FileStatus.COMPLETED


def test_duration_is_the_latest_segment_end(db_session, media_file):
    """Duration is the LATEST end, not the last list element (issue #455).

    ``segments[-1]["end"]`` assumed the list was sorted by time. Overlap marking and the
    speaker-boundary resegmentation both reorder segments, and the cloud-ASR adapters emit
    provider order — so the last element is not necessarily the one that ends last. When it
    was not, the stored duration was **shorter than the transcript**, and the player could
    not seek to the end of it.
    """
    out_of_order = [
        _segment(0.0, 10.0, "first"),
        _segment(20.0, 30.0, "last to end"),
        _segment(10.0, 20.0, "middle"),
    ]

    update_media_file_transcription_status(db_session, media_file.id, out_of_order)

    db_session.refresh(media_file)
    assert media_file.duration == pytest.approx(30.0), (
        "duration must be max(end), not the end of whichever segment happens to be last"
    )


def test_unique_speaker_names_are_returned_in_a_stable_order(run_in_clean_process):
    """Byte-identical input must give a byte-identical list, in any process (issue #455).

    This was a CHARACTERIZATION guard pinning the opposite: ``get_unique_speaker_names``
    was ``list(set(...))``, and Python randomises string hashing per process
    (``PYTHONHASHSEED``), so the order changed between worker restarts for byte-identical
    input. That list is written straight into the full-document OpenSearch record
    (``search_indexing_task`` → ``index_transcript``), so **re-indexing an unchanged file
    produced a different document** — the same non-determinism class as issue #433, which
    this repo has shipped once already — and it made any snapshot or diff over the index
    noisy enough to hide a real change.

    Both are now ``sorted``: ``storage.get_unique_speaker_names`` and the sibling
    ``search_indexing_task``'s own ``speaker_names``. The guard fired when that landed,
    exactly as its own instructions said it would, and this is the assertion it asked for.

    Two children with DIFFERENT ``PYTHONHASHSEED`` values are the only way to observe this —
    an in-process test cannot, because the seed is fixed for the life of the interpreter.
    **That is why the defect survived as long as it did.** A single-process comparison
    cannot distinguish "sorted" from "happened to hash the same way this run", which is
    also why the assertion below is equality across two seeds rather than a sortedness
    check on one.
    """
    code = (
        "from app.tasks.transcription.storage import get_unique_speaker_names\n"
        "names = ['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_02', 'SPEAKER_03',"
        " 'Alice', 'Bob', 'Carol', 'Dave']\n"
        "print(get_unique_speaker_names([{'speaker': n} for n in names]))\n"
    )

    first = run_in_clean_process(code, PYTHONHASHSEED="1")
    second = run_in_clean_process(code, PYTHONHASHSEED="4")

    assert first == second, (
        "the same speakers produced different list ORDER under a different hash seed, so "
        "re-indexing an unchanged file writes a different OpenSearch document "
        "(issue #433's class of bug)"
    )
    # `ast.literal_eval`, not `eval` — same parse, and no S307 suppression
    # needed. It is avoidable here, so per this repo's rule it is not taken.
    #
    # Written without the literal directive spelling on purpose: ruff scans the
    # text of EVERY comment for one, so prose describing a suppression parsed as
    # a malformed one and printed `Invalid # noqa directive` on every run of the
    # whole repo — permanent noise from a comment whose point is that nothing is
    # suppressed here.
    returned = ast.literal_eval(first.strip())

    # Two controls, because the equality above can hold trivially:
    #   - SORTED is the actual property. Two processes could agree on the same WRONG
    #     order, which equality alone would pass.
    #   - the full set really came back. A child that returned 3 of 8 names
    #     deterministically would satisfy both equality and sortedness.
    assert returned == sorted(returned), f"not in sorted order: {returned}"
    assert len(returned) == 8, f"expected all 8 distinct speakers, got {len(returned)}"


# --------------------------------------------------------------------------------------
# 6. The pure helpers
# --------------------------------------------------------------------------------------


def test_full_transcript_joins_segment_text_in_order_with_single_spaces():
    """The string handed to summarization, the LLM chat corpus and the full-doc index."""
    segments = [
        _segment(0.0, 1.0, "Hello"),
        _segment(1.0, 2.0, "there"),
        _segment(2.0, 3.0, "world"),
    ]

    assert generate_full_transcript(segments) == "Hello there world"


def test_full_transcript_of_no_segments_is_the_empty_string():
    assert generate_full_transcript([]) == ""


def test_full_transcript_preserves_text_verbatim_including_inner_punctuation():
    """No stripping or normalisation — the join must not silently rewrite the transcript."""
    segments = [_segment(0.0, 1.0, "  leading"), _segment(1.0, 2.0, "trailing  ")]

    assert generate_full_transcript(segments) == "  leading trailing  "


def test_unique_speaker_names_deduplicates_repeated_labels():
    """Order is asserted separately (and is a known defect); content is asserted here."""
    segments = [
        _segment(0.0, 1.0, "a", speaker="SPEAKER_00"),
        _segment(1.0, 2.0, "b", speaker="SPEAKER_01"),
        _segment(2.0, 3.0, "c", speaker="SPEAKER_00"),
        _segment(3.0, 4.0, "d", speaker="SPEAKER_01"),
    ]

    assert sorted(get_unique_speaker_names(segments)) == ["SPEAKER_00", "SPEAKER_01"]


def test_unique_speaker_names_raises_when_a_segment_has_no_speaker_key():
    """Pins the contract: this helper requires ``process_segments_with_speakers`` output.

    It subscripts ``segment["speaker"]`` rather than using ``.get``. Any future caller that
    passes raw WhisperX segments gets a KeyError, which in
    ``search_indexing_task.py`` is swallowed by the broad ``except`` around the
    full-document index — losing the document silently. Pinned so that a change to ``.get``
    (which would substitute ``None`` into the index instead) is a deliberate one.
    """
    with pytest.raises(KeyError):
        get_unique_speaker_names([_segment(0.0, 1.0, "no speaker here")])
