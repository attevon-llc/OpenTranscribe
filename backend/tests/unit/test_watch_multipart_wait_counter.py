"""``_handle_group``'s wait counter must not inherit a failed import's error count.

``WatchSourceFile.retry_count`` carries two unrelated meanings depending on the row's
status:

* ``_record_error`` (``services/watch_sources/processing.py``) increments it per failed
  import attempt;
* ``_handle_group`` (here) uses it as the number of SCANS a multi-part group has waited
  for its missing parts, and stitches once ``(waited + 1) >= wait_scans``.

A file that failed as a standalone import and later joined a group therefore entered
the wait already "aged" by its failures, and an incomplete recording could be stitched
on the very first grouping scan. Stitching an incomplete group produces a silently
truncated recording that then goes through transcription as if whole, so this is a
data-correctness bug rather than a scheduling nuisance.

These call ``_handle_group`` directly (not through ``scan_single``) — the same
convention the other watch-source task tests use: what is asserted is whether a stitch
is dispatched, not the scan machinery around it.

``_handle_group`` opens its OWN ``session_scope``, which under the savepoint harness is
a second connection that cannot see the fixture's uncommitted rows — it blocks on their
locks until the test times out. So the module attribute is swapped for one yielding the
test session, the same way ``test_speaker_plane_session_lifetime.py`` does it. It must
be patched on ``watch_source_tasks`` rather than on ``app.db.session_utils``: the module
did ``from … import session_scope`` at import time, so patching the source module would
rebind a name nothing reads.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.services.watch_sources.base import RemoteFileInfo
from app.services.watch_sources.multipart import MultipartGroup
from app.tasks import watch_source_tasks

#: The shipped default (``WatchSource.multipart_wait_scans``). Pinned rather than
#: imported so a future change to the default surfaces here as a decision instead of
#: silently re-tuning what these tests assert.
WAIT_SCANS = 3


def _fi(name: str, hours_offset: int = 0) -> RemoteFileInfo:
    return RemoteFileInfo(
        path=f"/watch/{name}",
        name=name,
        size=1000,
        modified_time=datetime(2026, 6, 1, tzinfo=UTC) + timedelta(hours=hours_offset),
    )


def _incomplete_group() -> MultipartGroup:
    """Parts 1 and 3 — part 2 has not arrived, so this must NOT be stitched yet."""
    return MultipartGroup(
        base_name="board-meeting",
        extension=".mp4",
        parts=[(1, _fi("board-meeting_P001.mp4")), (3, _fi("board-meeting_P003.mp4", 2))],
        is_complete=False,
    )


def _make_source(db_session, owner, **overrides) -> WatchSource:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "user_id": owner.id,
        "created_by": owner.id,
        "name": f"watch-{uuid_pkg.uuid4().hex[:8]}",
        "source_type": "local",
        "is_enabled": True,
        "local_path": ".",
        "multipart_enabled": True,
        "multipart_wait_scans": WAIT_SCANS,
    }
    defaults.update(overrides)
    ws = WatchSource(**defaults)
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def _seed_part(db_session, source, fi: RemoteFileInfo, **overrides) -> WatchSourceFile:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "watch_source_id": source.id,
        "remote_path": fi.path,
        "filename": fi.name,
        "status": "error",
        "retry_count": 0,
    }
    defaults.update(overrides)
    row = WatchSourceFile(**defaults)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture(autouse=True)
def _scoped_session(db_session, monkeypatch):
    """Make ``_handle_group``'s own session the test session.

    Mirrors the real ``session_scope``, which COMMITS on clean exit — without that the
    row mutations are never flushed and the stored-counter assertions would pass
    vacuously. Safe under the savepoint harness, which intercepts the commit and rolls
    back after the test.
    """

    @contextmanager
    def _scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(watch_source_tasks, "session_scope", _scope)


@pytest.fixture
def _no_stitch():
    """Patch the stitch dispatch so nothing is queued, and assert on the call."""
    with patch.object(watch_source_tasks.stitch_and_import, "delay") as delay:
        yield delay


def test_a_previously_failed_part_does_not_trigger_an_early_stitch(
    db_session, normal_user, _no_stitch
):
    """retry_count=2 from failed imports must not count as two scans already waited.

    With ``wait_scans=3``, a row entering the group carrying two prior *failures* was
    bumped to 3, making ``(3 + 1) >= 3`` true on the FIRST grouping scan — so a
    recording missing part 2 was stitched immediately.
    """
    source = _make_source(db_session, normal_user)
    group = _incomplete_group()
    # Part 1 failed twice as a standalone import before multipart was enabled.
    _seed_part(db_session, source, group.parts[0][1], status="error", retry_count=2)

    dispatched = watch_source_tasks._handle_group(source.id, group, WAIT_SCANS)

    assert dispatched is False
    _no_stitch.assert_not_called()


def test_the_wait_counter_restarts_from_zero_when_a_row_joins_the_group(
    db_session, normal_user, _no_stitch
):
    """The counter is reset on entry, not merely ignored.

    Asserting the stored value (rather than only the dispatch decision) is what pins
    the fix: a change that suppressed the early stitch some other way — say by
    special-casing ``status == "error"`` — would leave the inherited count in place to
    resurface on the next scan.
    """
    source = _make_source(db_session, normal_user)
    group = _incomplete_group()
    seeded = _seed_part(db_session, source, group.parts[0][1], status="error", retry_count=2)

    watch_source_tasks._handle_group(source.id, group, WAIT_SCANS)

    db_session.expire_all()
    row = db_session.get(WatchSourceFile, seeded.id)
    assert row.status == "waiting_for_parts"
    assert row.retry_count == 1, "first scan of the wait, not the third"


def test_a_row_already_waiting_keeps_ageing(db_session, normal_user, _no_stitch):
    """The reset applies on ENTRY only — an established wait must still advance.

    Without this, resetting on every scan would make an incomplete group wait forever
    and the missing-parts timeout would never fire.
    """
    source = _make_source(db_session, normal_user)
    group = _incomplete_group()
    seeded = _seed_part(
        db_session, source, group.parts[0][1], status="waiting_for_parts", retry_count=1
    )

    watch_source_tasks._handle_group(source.id, group, WAIT_SCANS)

    db_session.expire_all()
    row = db_session.get(WatchSourceFile, seeded.id)
    assert row.retry_count == 2


def test_an_incomplete_group_still_stitches_once_it_has_waited_long_enough(
    db_session, normal_user, _no_stitch
):
    """The timeout itself must survive the fix.

    ``wait_scans`` exists so a permanently missing part does not strand the recording
    forever. A test suite that only proved the early stitch was suppressed would pass
    just as happily if the stitch never happened at all.
    """
    source = _make_source(db_session, normal_user)
    group = _incomplete_group()
    for _, fi in group.parts:
        _seed_part(db_session, source, fi, status="waiting_for_parts", retry_count=WAIT_SCANS - 1)

    dispatched = watch_source_tasks._handle_group(source.id, group, WAIT_SCANS)

    assert dispatched is True
    _no_stitch.assert_called_once()


def test_a_complete_group_stitches_immediately_regardless_of_the_counter(
    db_session, normal_user, _no_stitch
):
    """Completeness short-circuits the wait — unchanged by the fix."""
    source = _make_source(db_session, normal_user)
    group = MultipartGroup(
        base_name="board-meeting",
        extension=".mp4",
        parts=[(1, _fi("board-meeting_P001.mp4")), (2, _fi("board-meeting_P002.mp4", 1))],
        is_complete=True,
    )
    _seed_part(db_session, source, group.parts[0][1], status="error", retry_count=2)

    dispatched = watch_source_tasks._handle_group(source.id, group, WAIT_SCANS)

    assert dispatched is True
    _no_stitch.assert_called_once()
