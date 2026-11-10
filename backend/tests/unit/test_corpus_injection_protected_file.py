"""Re-injecting a meeting never rewrites an injected file that is held or quarantined.

An injected meeting is a real ``MediaFile``; an operator can hold or quarantine it like any
other. A hold freezes the file as it stands (``hold_guard``), so a re-run, even with ``--force``
and changed content, must leave its segments and its metadata (the recorded date too) alone and
say so in the record (``skipped_protected``), which the CLI then dispatches nothing for.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from sqlalchemy import text

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.scripts.corpus_injection.injector import inject_meeting
from tests.fixtures.tag_world import make_user
from tests.unit.test_corpus_injection_recorded_date import _doc

pytestmark = pytest.mark.unit


@pytest.fixture
def owner(db_session):
    return make_user(db_session, "inject-hold")


def _segments(db, file_id: int) -> list[str]:
    rows = db.query(TranscriptSegment.text).filter(TranscriptSegment.media_file_id == file_id)
    return sorted(str(r[0]) for r in rows)


@pytest.mark.parametrize("how", ["legal_hold", "is_quarantined"])
def test_a_protected_injected_file_is_left_as_it_stands(db_session, owner, how):
    doc = _doc(f"m-{uuid_pkg.uuid4().hex[:8]}")
    first, _ = inject_meeting(db_session, doc, owner.id, seed="hold", tool_version="test")
    db_session.flush()
    file_id = int(first.media_file_id)
    db_session.execute(text(f"UPDATE media_file SET {how} = true WHERE id = :i"), {"i": file_id})
    db_session.execute(
        text("UPDATE media_file SET recorded_date = NULL, title = 'as held' WHERE id = :i"),
        {"i": file_id},
    )
    db_session.flush()
    db_session.expire_all()
    before = _segments(db_session, file_id)

    doc.turns[0].text = "Changed content a re-run would write."
    record, turns = inject_meeting(
        db_session, doc, owner.id, seed="hold", tool_version="test", force=True
    )
    db_session.flush()
    db_session.expire_all()

    assert record.action == "skipped_protected"
    assert turns, "the manifest's turn table is still returned"
    assert _segments(db_session, file_id) == before
    row = db_session.get(MediaFile, file_id)
    assert row.title == "as held" and row.recorded_date is None
