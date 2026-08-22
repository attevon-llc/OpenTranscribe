"""Watch-source content dedup across the three layers, for MEDIA files.

Deliberately media-only and free of the document plane: the document half of these
paths is tracked separately (see issues #546/#547 and their handover gist) and is
being extracted to its own branch, so nothing here may depend on it.

``ingest_prepared_file`` is exercised directly rather than through
``import_single_file``/Celery — the same convention
``test_watch_source_document_ingest.py`` uses. What is asserted is the DB side effect
of the dedup logic, not the scan scheduling or transfer machinery around it.

**No MinIO gate, on purpose.** Every assertion here is about a path that returns at
the dedup step, *before* any storage write. A test that skipped without the dev stack
would not guard the fix in CI, which is where the regression would otherwise land.
The ``finally`` cleanup exists for the pre-fix (red) run only: before the fix, the
same-source case falls through to a real import, which is precisely the bug.
"""

from __future__ import annotations

import contextlib
import shutil
import uuid as uuid_pkg
from pathlib import Path

import pytest

from app.models.media import MediaFile
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.services.imohash_service import compute_from_path
from app.services.watch_sources import processing

#: The committed 10 s / mono / 16 kHz fixture — see ``tests/fixtures/media/README.md``.
#: Real bytes are required: ``ingest_prepared_file`` magic-byte-validates before it
#: ever reaches the dedup step, so a synthetic buffer would be rejected as
#: ``skipped_invalid`` and the test would pass for the wrong reason.
_SAMPLE_AUDIO = Path(__file__).resolve().parents[1] / "fixtures" / "media" / "sample_short.wav"


def _make_source(db_session, owner, **overrides) -> WatchSource:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "user_id": owner.id,
        "created_by": owner.id,
        "name": f"watch-{uuid_pkg.uuid4().hex[:8]}",
        "source_type": "local",
        "is_enabled": True,
        "local_path": ".",
        "auto_transcribe": True,
    }
    defaults.update(overrides)
    ws = WatchSource(**defaults)
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def _make_row(db_session, source, **overrides) -> WatchSourceFile:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "watch_source_id": source.id,
        "remote_path": f"/watch/{uuid_pkg.uuid4().hex}.wav",
        "filename": "recording.wav",
        "status": "importing",
    }
    defaults.update(overrides)
    row = WatchSourceFile(**defaults)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _stage_audio(tmp_path: Path, name: str = "recording.wav") -> Path:
    """Copy the committed fixture into tmp_path and return its path."""
    dest = tmp_path / name
    shutil.copyfile(_SAMPLE_AUDIO, dest)
    return dest


@pytest.fixture
def _cleanup_media(db_session):
    """Remove any MediaFile the pre-fix code path imports for real.

    After the fix nothing is created and this is a no-op. Before it, the
    same-source case runs a full import — deleting it keeps a red run from
    leaving a stray library entry behind.
    """
    created: list[int] = []
    yield created
    for imohash in created:
        for mf in db_session.query(MediaFile).filter(MediaFile.imohash == imohash).all():
            if mf.storage_path:
                with contextlib.suppress(Exception):
                    from app.core.config import settings
                    from app.services.minio_service import minio_client

                    minio_client.remove_object(settings.MEDIA_BUCKET_NAME, mf.storage_path)
            with contextlib.suppress(Exception):
                db_session.delete(mf)
    with contextlib.suppress(Exception):
        db_session.commit()


def test_identical_content_at_two_paths_in_one_source_is_deduped(
    db_session, normal_user, tmp_path, _cleanup_media
):
    """Two different paths, same bytes, ONE source — the second must be skipped.

    Layers 1-2 filtered ``watch_source_id != source.id``, which excludes the source
    being scanned, so a source holding the same recording under two names imported
    it twice. Layer 3 does not cover this either: it compares against
    ``MediaFile.imohash``, and at the moment the second row is processed the first
    import's MediaFile carries the same fingerprint — but the reason recorded must
    identify it as a *same-source* duplicate, which is what the caller needs to know.
    """
    source = _make_source(db_session, normal_user)
    audio = _stage_audio(tmp_path)
    imohash = compute_from_path(str(audio))
    assert imohash, "fixture must produce a fingerprint or the test proves nothing"
    _cleanup_media.append(imohash)

    # The first copy, already imported under a different path in THIS source.
    _make_row(
        db_session,
        source,
        remote_path="/watch/board-meeting.wav",
        filename="board-meeting.wav",
        status="imported",
        imohash=imohash,
    )

    # The second copy arrives under another name.
    row = _make_row(
        db_session,
        source,
        remote_path="/watch/board-meeting-COPY.wav",
        filename="board-meeting-COPY.wav",
    )

    result = processing.ingest_prepared_file(
        db_session,
        source,
        str(audio),
        filename="board-meeting-COPY.wav",
        row=row,
        size=audio.stat().st_size,
    )

    assert result.status == "skipped_duplicate"
    assert result.skip_reason == "duplicate_same_source"


def test_identical_content_in_a_different_source_still_reports_other_source(
    db_session, normal_user, tmp_path, _cleanup_media
):
    """The cross-source reason must survive the same-source fix.

    Guards the half of the change that is easy to lose: dropping the
    ``watch_source_id != source.id`` filter must not collapse both cases into one
    reason, because "already imported by another watch source" and "this source has
    it twice" call for different operator action.
    """
    other_source = _make_source(db_session, normal_user)
    source = _make_source(db_session, normal_user)
    audio = _stage_audio(tmp_path)
    imohash = compute_from_path(str(audio))
    assert imohash
    _cleanup_media.append(imohash)

    _make_row(
        db_session,
        other_source,
        remote_path="/elsewhere/board-meeting.wav",
        status="imported",
        imohash=imohash,
    )
    row = _make_row(db_session, source, remote_path="/watch/board-meeting.wav")

    result = processing.ingest_prepared_file(
        db_session,
        source,
        str(audio),
        filename="board-meeting.wav",
        row=row,
        size=audio.stat().st_size,
    )

    assert result.status == "skipped_duplicate"
    assert result.skip_reason == "duplicate_other_source"


def test_a_non_imported_sibling_row_does_not_block_the_import(
    db_session, normal_user, tmp_path, _cleanup_media
):
    """Only an ``imported`` sibling counts as a duplicate.

    The negative control for the fix above. Widening the query to include the source
    being scanned makes it match many more rows, so the ``status == "imported"``
    guard is now load-bearing in a way it was not before: without it, a failed or
    still-in-flight attempt at the same content would permanently block the retry
    that #489's Retry button exists to trigger.
    """
    source = _make_source(db_session, normal_user)
    audio = _stage_audio(tmp_path)
    imohash = compute_from_path(str(audio))
    assert imohash
    _cleanup_media.append(imohash)

    _make_row(
        db_session,
        source,
        remote_path="/watch/failed-attempt.wav",
        status="error",
        imohash=imohash,
        error_message="download produced no bytes",
    )
    row = _make_row(db_session, source, remote_path="/watch/second-attempt.wav")

    result = processing.ingest_prepared_file(
        db_session,
        source,
        str(audio),
        filename="second-attempt.wav",
        row=row,
        size=audio.stat().st_size,
    )

    assert result.skip_reason != "duplicate_same_source"
