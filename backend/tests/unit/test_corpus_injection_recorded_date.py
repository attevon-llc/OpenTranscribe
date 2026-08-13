"""The injector writes a meeting's real date, including on the re-run/skip path (#403 R7).

Two behaviours, and the second is the one that is easy to get wrong and expensive to discover:

1. A fresh injection resolves the corpus record's date through the **same** resolver a real
   upload uses, so the injector picks no winner of its own.
2. A **re-run over unchanged content** still refreshes the date. "Unchanged" is a statement
   about the meeting's segment hash, not about the row's derived metadata — and
   ``recorded_date`` is metadata the existing rows predate. Without this, a corpus injected
   before v390 could only acquire dates via ``--force``, which deletes and reinserts every
   segment, re-chunks and re-indexes the file, and therefore invalidates every retrieval
   baseline measured against the previous injection. The cheap path and the safe path are the
   same path, and this test is what keeps them that way.

⚠️ These assert on ``media_file.recorded_date``, never on
``metadata_important['rag_eval']['date']``. The eval block is the harness's gold source; a
product path reading it would be scoring the corpus against its own answer key.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from app.core.enums import RecordedDateSource
from app.scripts.corpus_injection import rows as rowbuild
from app.scripts.corpus_injection.injector import inject_meeting
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import TimingInfo
from app.scripts.corpus_injection.model import Turn

pytestmark = pytest.mark.unit


def _doc(meeting_id: str, *, date: str | None = "2025-01-03") -> MeetingDoc:
    return MeetingDoc(
        corpus="synthetic",
        meeting_id=meeting_id,
        title="Platform — standup #3",
        turns=[
            Turn(
                turn_index=0,
                speaker="Alina",
                text="Morning everyone, let's begin.",
                start=0.0,
                end=3.0,
            ),
            Turn(
                turn_index=1,
                speaker="Bo",
                text="Quick update on the migration.",
                start=3.0,
                end=6.0,
            ),
        ],
        language="en",
        timing=TimingInfo(source="synthetic", reference=None),
        extra={"license_tier": "A", "date": date or ""},
    )


@pytest.fixture
def owner(db_session):
    from app.models.user import User

    user = User(
        email=f"inject-rd-{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="local",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _injected(db_session, owner, doc):
    from app.models.media import MediaFile
    from app.scripts.corpus_injection import ids

    record, _turns = inject_meeting(db_session, doc, owner.id, seed="t68", tool_version="test")
    db_session.flush()
    media_file = (
        db_session.query(MediaFile)
        .filter(MediaFile.uuid == ids.file_uuid(doc.corpus, doc.meeting_id, "t68"))
        .one()
    )
    return record, media_file


def test_a_fresh_injection_dates_the_meeting_from_the_corpus_record(db_session, owner):
    """The corpus record is a row-with-no-media's container, so the source is ``container``."""
    record, media_file = _injected(db_session, owner, _doc(f"m-{uuid_pkg.uuid4().hex[:8]}"))

    assert record.action in ("created", "updated")
    assert media_file.recorded_date is not None
    assert media_file.recorded_date.date() == dt.date(2025, 1, 3)
    assert media_file.recorded_date_source == RecordedDateSource.CONTAINER.value
    assert media_file.recorded_date_locked is False, "an injected date is derived, not a user's"


def test_the_injected_date_is_not_the_upload_date(db_session, owner):
    """The defect this whole change exists to fix, asserted directly.

    Measured before the fix: `upload_time` had ONE distinct value across all 432 corpus
    files — the injection date — while the meetings spanned a year. If these two ever
    coincide again, every date-scoped answer is back to reporting when the bytes arrived.
    """
    _record, media_file = _injected(db_session, owner, _doc(f"m-{uuid_pkg.uuid4().hex[:8]}"))

    assert media_file.upload_time is not None
    assert media_file.recorded_date.date() != media_file.upload_time.date()


def test_a_rerun_over_unchanged_content_still_refreshes_the_date(db_session, owner):
    """The skip path, and the reason the benchmark can run without a reindex.

    The row is written, its date is cleared to simulate a corpus injected *before* v390, and
    the identical meeting is injected again. The second run must report ``skipped`` — proving
    it really took the unchanged-content branch and did not quietly rewrite the segments —
    **and** still restore the date.
    """
    doc = _doc(f"m-{uuid_pkg.uuid4().hex[:8]}")
    _record, media_file = _injected(db_session, owner, doc)

    media_file.recorded_date = None
    media_file.recorded_date_source = None
    media_file.recorded_date_confidence = None
    media_file.recorded_date_candidates = None
    db_session.flush()

    record2, media_file2 = _injected(db_session, owner, doc)

    assert record2.action == "skipped", (
        "the re-run did not take the unchanged-content branch, so this test is not "
        "exercising the skip path at all"
    )
    assert media_file2.recorded_date is not None, "the skip path did not refresh the date"
    assert media_file2.recorded_date.date() == dt.date(2025, 1, 3)
    assert media_file2.recorded_date_source == RecordedDateSource.CONTAINER.value


def test_the_skip_path_does_not_touch_the_segments(db_session, owner):
    """The control that makes the test above worth having.

    Refreshing the date on the skip path is only safe because it rewrites nothing else. If a
    re-run reinserted segments, their ids would move, the file would re-chunk, and every
    retrieval baseline measured against the previous injection would stop being comparable —
    which is precisely what using ``--force`` would have cost.
    """
    from app.models.media import TranscriptSegment

    doc = _doc(f"m-{uuid_pkg.uuid4().hex[:8]}")
    _record, media_file = _injected(db_session, owner, doc)
    before = sorted(
        db_session.query(TranscriptSegment.id, TranscriptSegment.start_time)
        .filter(TranscriptSegment.media_file_id == media_file.id)
        .all()
    )
    assert before, "no segments were written, so this control proves nothing"

    record2, media_file2 = _injected(db_session, owner, doc)
    after = sorted(
        db_session.query(TranscriptSegment.id, TranscriptSegment.start_time)
        .filter(TranscriptSegment.media_file_id == media_file2.id)
        .all()
    )

    assert record2.action == "skipped"
    assert after == before, "the skip path rewrote segments — chunk ids and baselines would move"


def test_a_corpus_with_no_date_is_left_undated_rather_than_invented(db_session, owner):
    """QMSum's meetings carry no date and must stay that way.

    An injector that fabricated one — from the injection clock, say — would make every QMSum
    date-scoped answer confidently wrong instead of honestly absent, which is strictly worse.
    """
    _record, media_file = _injected(
        db_session, owner, _doc(f"m-{uuid_pkg.uuid4().hex[:8]}", date=None)
    )

    assert media_file.recorded_date is None
    assert media_file.recorded_date_source == RecordedDateSource.NONE.value, (
        "'we looked and found nothing' must be recorded, not left NULL as 'never looked'"
    )


def test_a_malformed_corpus_date_is_declined_not_guessed(db_session, owner):
    """Same rule as the filename source: an unparseable date says nothing."""
    _record, media_file = _injected(
        db_session, owner, _doc(f"m-{uuid_pkg.uuid4().hex[:8]}", date="Q1 2025")
    )

    assert media_file.recorded_date is None
    assert media_file.recorded_date_source == RecordedDateSource.NONE.value


def test_the_candidate_helper_reads_the_corpus_record_not_the_eval_block():
    """Pins the boundary: the helper takes a ``MeetingDoc``, never a ``MediaFile`` row.

    If this ever grows a `media_file` parameter it would be able to read
    `metadata_important['rag_eval']`, and the injector would stop being the only writer of
    that fact. Cheap to assert, and the failure it prevents is a benchmark that scores itself.
    """
    import inspect

    params = list(inspect.signature(rowbuild.recorded_date_candidate).parameters)
    assert params == ["doc"], f"unexpected signature {params} — see this test's docstring"
