"""The three deterministic date sources, and the resolver that ranks them.

Pure functions, no database, no stack. Every "it finds the date" test here is paired with a
**negative control that must return ``None``**, because the failure mode this whole change
exists to prevent is not "we missed a date" — it is "we produced one and were wrong". An
extractor that returned a date for everything would pass a suite of positives alone.

The three refusals that carry the design, each with its own test:

* ``03/04/2024`` in a filename is **ambiguous** and is refused rather than resolved by
  assuming a locale nobody told us.
* "the deadline is March the fifteenth" is a date **being talked about**, not a meeting
  dating itself, and the anchor requirement is what separates them.
* "it's Tuesday the fifteenth" is anchored, is in the opening, and is still refused,
  because recovering the month and year would mean assuming the recording happened near
  its upload — the assumption this change exists to stop making.

``reference`` is passed everywhere so the plausibility window is fixed. Without it these
become time bombs: a "future date is rejected" case written against a hardcoded year starts
passing for the wrong reason, then fails, as the real clock moves.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.enums import PRECEDENCE
from app.core.enums import RecordedDateSource
from app.services.ingest_artifacts.date_sources import from_container
from app.services.ingest_artifacts.date_sources import from_filename
from app.services.ingest_artifacts.date_sources import from_transcript
from app.services.ingest_artifacts.recorded_date import DateCandidate
from app.services.ingest_artifacts.recorded_date import resolve

#: A fixed "today" so the plausibility window never moves under the suite.
TODAY = dt.date(2026, 8, 13)
MARCH_15 = dt.datetime(2024, 3, 15, tzinfo=dt.UTC)


def _turn(text: str) -> dict[str, str]:
    return {"text": text}


# --------------------------------------------------------------------- container


def test_the_container_date_is_taken_as_stated():
    candidate = from_container(dt.datetime(2024, 3, 15, 9, 4, tzinfo=dt.UTC), reference=TODAY)
    assert candidate is not None
    assert candidate.source is RecordedDateSource.CONTAINER
    assert candidate.date.date() == dt.date(2024, 3, 15)
    # The instant survives — a container knows the time of day and throwing it away would
    # lose information the column can hold.
    assert candidate.date.hour == 9


def test_a_naive_container_datetime_is_read_as_utc_rather_than_refused():
    """Some encoders write a wall clock with no zone. Dropping those loses a real source.

    Asserting the **offset**, not merely that some tzinfo is attached: "it has a
    timezone" is satisfied by attaching the wrong one, which would shift the date across
    a midnight boundary and put the recording in the wrong day — and a wrong day is the
    entire defect this module exists to prevent. The wall-clock components must also
    survive unchanged; interpreting a naive time as UTC must not also *convert* it.
    """
    candidate = from_container(dt.datetime(2024, 3, 15, 9, 4), reference=TODAY)
    assert candidate is not None
    assert candidate.date.utcoffset() == dt.timedelta(0)
    assert candidate.date == dt.datetime(2024, 3, 15, 9, 4, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("when", "why"),
    [
        (dt.datetime(1969, 7, 20, tzinfo=dt.UTC), "before the epoch band"),
        (dt.datetime(2039, 1, 1, tzinfo=dt.UTC), "implausibly far in the future"),
        (None, "the container said nothing"),
    ],
)
def test_the_container_declines_implausible_and_absent_dates(when, why):
    assert from_container(when, reference=TODAY) is None, why


def test_the_epoch_default_is_rejected_because_encoders_write_it_into_unset_fields():
    """``1970-01-01`` is the single most common junk value in this field."""
    assert from_container(dt.datetime(1969, 12, 31, tzinfo=dt.UTC), reference=TODAY) is None


# --------------------------------------------------------------------- filename


@pytest.mark.parametrize(
    "filename",
    [
        "2024-03-15_standup.mp4",
        "2024_03_15 standup.mp4",
        "2024.03.15-standup.m4a",
        "20240315_standup.mp4",
        "Meeting Mar 15 2024.m4a",
        "Meeting March 15, 2024.m4a",
        "board-March-15-2024.wav",
        "15 March 2024 board.wav",
        "15th of March, 2024.wav",
        "Sept 15 2024 review.mp4",
        "/nested/path/2024-03-15_standup.mp4",
    ],
)
def test_a_date_in_the_filename_is_recovered(filename):
    candidate = from_filename(filename, reference=TODAY)
    assert candidate is not None, filename
    assert candidate.source is RecordedDateSource.FILENAME
    assert candidate.date.date() in (dt.date(2024, 3, 15), dt.date(2024, 9, 15)), filename
    # The evidence names what was matched, so a wrong date is diagnosable rather than
    # merely wrong.
    assert candidate.evidence.startswith("filename:")


def test_an_unambiguous_day_first_numeric_filename_is_resolved():
    """``25/03/2024`` — 25 cannot be a month, so the roles are forced, not assumed."""
    candidate = from_filename("25-03-2024 retro.mp4", reference=TODAY)
    assert candidate is not None
    assert candidate.date.date() == dt.date(2024, 3, 25)


def test_an_unambiguous_month_first_numeric_filename_is_resolved():
    candidate = from_filename("03-25-2024 retro.mp4", reference=TODAY)
    assert candidate is not None
    assert candidate.date.date() == dt.date(2024, 3, 25)


def test_an_ambiguous_numeric_filename_is_refused_not_guessed():
    """The control that makes the two tests above mean something.

    ``03/04/2024`` is 3 April or 4 March depending on a locale nobody told this
    application. Picking one produces a date that is wrong half the time and carries the
    same provenance and confidence as a date that is right — indistinguishable to the user,
    which is the whole failure this change exists to end.
    """
    assert from_filename("03-04-2024 retro.mp4", reference=TODAY) is None
    assert from_filename("04/03/2024 retro.mp4", reference=TODAY) is None


@pytest.mark.parametrize(
    ("filename", "why"),
    [
        ("standup.mp4", "no date at all"),
        ("", "no filename"),
        (None, "no filename"),
        ("recording_1920x1080.mp4", "a resolution is not a date"),
        ("2024-13-05_standup.mp4", "month 13 is not a month"),
        ("2023-02-30_standup.mp4", "30 February is not a day"),
        ("clip_2039-01-01.mp4", "implausibly far in the future"),
        ("track_1965-04-02.mp3", "before the accepted band"),
    ],
)
def test_the_filename_source_declines_rather_than_inventing(filename, why):
    assert from_filename(filename, reference=TODAY) is None, why


def test_a_long_digit_run_is_not_mistaken_for_a_compact_date():
    """A hash or an id must not be parsed as ``YYYYMMDD`` because eight of its digits fit."""
    assert from_filename("dump_920240315773.bin", reference=TODAY) is None


# ------------------------------------------------------------------- transcript


@pytest.mark.parametrize(
    "line",
    [
        "Okay, today is March 15, 2024, let's get started.",
        "Right — it's the 15th of March 2024 and we're all here.",
        "This is the March 15 2024 board meeting.",
        "Recorded on 2024-03-15, everyone.",
    ],
)
def test_a_meeting_that_states_its_own_date_is_read(line):
    candidate = from_transcript([_turn(line)], reference=TODAY)
    assert candidate is not None, line
    assert candidate.source is RecordedDateSource.TRANSCRIPT
    assert candidate.date.date() == dt.date(2024, 3, 15)


def test_a_date_merely_mentioned_is_not_treated_as_the_meeting_date():
    """The anchor requirement, and the reason it exists.

    "The deadline is March the fifteenth" is the common sentence; a meeting dating itself
    is the rare one. Without the anchor the two are the same string shape, and every
    transcript that discusses a date would date itself to it.
    """
    assert (
        from_transcript(
            [_turn("The deadline for the migration is March 15, 2024, so we need to move.")],
            reference=TODAY,
        )
        is None
    )


def test_an_incomplete_spoken_date_is_refused_rather_than_completed_from_the_clock():
    """ "It's Tuesday the fifteenth" — anchored, in the opening, and still not enough.

    Filling in the month and year would mean assuming the recording happened near its
    upload. That assumption is the defect this change exists to remove, so reproducing it
    inside the fix would be self-defeating.
    """
    assert from_transcript([_turn("Okay it's Tuesday the fifteenth, let's start.")]) is None


def test_a_date_stated_late_in_the_meeting_is_ignored():
    """Position is the second guard, and it has to be tested independently of the anchor.

    A date forty turns in is a deadline, a birthday or a historical reference far more
    often than it is the date of the recording — even when it is phrased with an anchor.
    """
    segments = [_turn("Morning everyone.")] * 40
    segments.append(_turn("Just noting today is March 15, 2024 for the minutes."))
    assert from_transcript(segments, reference=TODAY) is None


def test_the_opening_window_is_what_makes_the_late_date_test_pass():
    """The control for the test above: the identical sentence, inside the window, is read.

    Without this, a `from_transcript` that never matched anything at all would make the
    late-date test pass, and the suite would report a working guard over dead code.
    """
    segments = [_turn("Morning everyone.")]
    segments.append(_turn("Just noting today is March 15, 2024 for the minutes."))
    candidate = from_transcript(segments, reference=TODAY)
    assert candidate is not None
    assert candidate.date.date() == dt.date(2024, 3, 15)


def test_an_empty_transcript_is_an_honest_absence():
    assert from_transcript([], reference=TODAY) is None


# --------------------------------------------------------------------- resolver


def _candidate(source: RecordedDateSource, day: int, confidence: float = 0.5) -> DateCandidate:
    return DateCandidate(
        source=source,
        date=dt.datetime(2024, 3, day, tzinfo=dt.UTC),
        confidence=confidence,
        evidence=f"test {source.value}",
    )


def test_nothing_found_resolves_to_none_with_a_source_of_none_not_a_null_source():
    """ "We looked and found nothing" must be sayable, and must not look like "not run"."""
    resolution = resolve([None, None, None])
    assert resolution.date is None
    assert resolution.source is RecordedDateSource.NONE
    assert resolution.confidence is None
    assert resolution.candidates_json() is None
    assert not resolution.conflict
    assert not resolution.is_resolved


def test_precedence_beats_confidence():
    """A high-confidence filename must not outrank a low-confidence container.

    Precedence is a policy that was written down; confidence is an ordinal hint. Letting
    the hint win would make the documented ordering decorative and the real rule invisible.
    """
    resolution = resolve(
        [
            _candidate(RecordedDateSource.FILENAME, 20, confidence=0.99),
            _candidate(RecordedDateSource.CONTAINER, 15, confidence=0.10),
        ]
    )
    assert resolution.source is RecordedDateSource.CONTAINER
    assert resolution.date == MARCH_15


def test_a_manual_value_outranks_every_derived_source():
    resolution = resolve(
        [
            _candidate(RecordedDateSource.CONTAINER, 20, confidence=0.99),
            _candidate(RecordedDateSource.FILENAME, 21),
            _candidate(RecordedDateSource.TRANSCRIPT, 22),
            _candidate(RecordedDateSource.MANUAL, 15),
        ]
    )
    assert resolution.source is RecordedDateSource.MANUAL
    assert resolution.date == MARCH_15


def test_the_losing_candidates_are_kept_not_discarded():
    """A disagreement the user cannot inspect is a decision nobody can audit."""
    resolution = resolve(
        [
            _candidate(RecordedDateSource.FILENAME, 20),
            _candidate(RecordedDateSource.CONTAINER, 15),
        ]
    )
    stored = resolution.candidates_json()
    assert stored is not None
    assert [entry["source"] for entry in stored] == ["container", "filename"]
    assert all(entry["evidence"] for entry in stored), "evidence must survive serialisation"


def test_disagreeing_sources_are_flagged_as_a_conflict():
    resolution = resolve(
        [
            _candidate(RecordedDateSource.CONTAINER, 14),
            _candidate(RecordedDateSource.FILENAME, 15),
        ]
    )
    assert resolution.conflict
    # And it still answers — refusing would throw away the best guess AND the evidence.
    assert resolution.date == dt.datetime(2024, 3, 14, tzinfo=dt.UTC)


def test_agreement_at_different_times_of_day_is_not_a_conflict():
    """The control for the flag: it compares calendar days, not instants.

    A container stamp at 09:04 and a filename saying the same date are the same answer.
    Comparing instants would raise the flag on nearly every file, and a flag that always
    fires is one the UI learns to hide.
    """
    resolution = resolve(
        [
            DateCandidate(
                source=RecordedDateSource.CONTAINER,
                date=dt.datetime(2024, 3, 15, 9, 4, tzinfo=dt.UTC),
                confidence=0.9,
                evidence="container",
            ),
            _candidate(RecordedDateSource.FILENAME, 15),
        ]
    )
    assert not resolution.conflict


def test_confidence_is_not_lowered_by_a_conflict():
    """They are separate facts and must stay separately readable.

    Blending them makes a high-confidence source contradicted by another indistinguishable
    from a mediocre uncontested one, and neither number can be recovered afterwards.
    """
    contested = resolve(
        [
            _candidate(RecordedDateSource.CONTAINER, 14, confidence=0.9),
            _candidate(RecordedDateSource.FILENAME, 15),
        ]
    )
    uncontested = resolve([_candidate(RecordedDateSource.CONTAINER, 14, confidence=0.9)])
    assert contested.confidence == uncontested.confidence == 0.9
    assert contested.conflict and not uncontested.conflict


def test_two_candidates_of_the_same_rank_resolve_deterministically():
    """Two identical files ingested a month apart must not disagree.

    Determinism is a correctness property in this package (the digest baseline depends on
    it), and set/dict iteration order is not stable across processes with an unpinned
    ``PYTHONHASHSEED``.
    """
    first = _candidate(RecordedDateSource.FILENAME, 20, confidence=0.6)
    second = _candidate(RecordedDateSource.FILENAME, 15, confidence=0.8)
    assert resolve([first, second]).date == resolve([second, first]).date == MARCH_15


def test_every_derivable_source_is_ranked_so_none_can_be_silently_unselectable():
    """A source missing from ``PRECEDENCE`` would never win however good its evidence."""
    assert set(PRECEDENCE) == set(RecordedDateSource) - {RecordedDateSource.NONE}
    for source in PRECEDENCE:
        resolution = resolve([_candidate(source, 15)])
        assert resolution.source is source, f"{source} cannot be selected even unopposed"
