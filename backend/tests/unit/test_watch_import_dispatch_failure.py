"""Watch-source auto-import: a dispatch failure after a successful import must be
VISIBLE, not swallowed into a bare ``logger.error`` (issue #906).

By the time ``_dispatch_pipeline`` runs, ``_finalize_media_ingest`` has already
committed the ``WatchSourceFile`` tracking row to ``status="imported"`` and the new
``MediaFile`` row exists in Postgres — the bytes really were imported. If the
transcription dispatch that follows then fails silently, the file sits there forever
with no error recorded anywhere a human or the watch-sources UI would see it.

Follows the ``media_file_stubs`` pattern from
``test_org_context_background_imports.py``: ``validate_uploaded_file`` and
``upload_file_tuned`` are stubbed so this stays a unit test with no MinIO
dependency (CI has none), and ``_notify_file_created`` is stubbed to avoid a
Redis-based WS publish. ``_dispatch_pipeline``'s own target,
``app.api.endpoints.files.upload.dispatch_upload_pipeline``, is patched at its
source so the local import inside ``_dispatch_pipeline`` picks up the patched
version.

⚠️ **Visible is not the same as verbatim** (issues #891 / #914). This suite originally
asserted the caught exception's own text reached ``error_message``, and
``processing.py:_dispatch_pipeline`` obliged by interpolating it. That is the #891
exemplar leak in a second route: a dispatch failure here is almost always the Celery
broker refusing a connection, and the redis-py/kombu message for that quotes the broker
URL **with its embedded password**, straight onto a field the watch-source file listing
renders (``api/endpoints/watch_sources.py``). ``_dispatch_pipeline`` now persists only
the exception CLASS and logs the rest, so the three assertions below are:

* the fixed literal IS present — the user still learns dispatch failed, and which class,
* the credential-bearing detail is ABSENT from ``error_message``,
* that same detail IS in the log, so the failure stays diagnosable.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg

import pytest

from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile

PROCESSING_LOGGER = "app.services.watch_sources.processing"

# Shaped like the real thing: redis-py's ConnectionError for a refused broker quotes the
# whole URL, credential included. ``BROKER_CREDENTIAL`` is the sentinel that must never
# reach a persisted, user-rendered field.
BROKER_CREDENTIAL = "hunter2-broker-secret"  # noqa: S105 - fake sentinel, not a credential
LEAKY_DISPATCH_ERROR = (
    f"Error 111 connecting to redis://:{BROKER_CREDENTIAL}@redis:6379/0. Connection refused."
)


def _mk_source(db, owner) -> WatchSource:
    source = WatchSource(
        uuid=uuid_pkg.uuid4(),
        name=f"src_{uuid_pkg.uuid4().hex[:8]}",
        source_type="local",
        user_id=owner.id,
        created_by=owner.id,
        auto_transcribe=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _mk_tracking_row(db, source: WatchSource, filename: str) -> WatchSourceFile:
    row = WatchSourceFile(
        uuid=uuid_pkg.uuid4(),
        watch_source_id=source.id,
        remote_path=f"/watch/{filename}",
        filename=filename,
        status="importing",
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def media_file_stubs(monkeypatch, tmp_path):
    """Stub the storage/validation externals of ingest_prepared_file."""
    import app.services.watch_sources.processing as processing

    monkeypatch.setattr(
        processing, "validate_uploaded_file", lambda fp, mime, fn: (True, "video/mp4")
    )
    import app.services.minio_service as minio_service

    monkeypatch.setattr(minio_service, "upload_file_tuned", lambda **kwargs: None)
    monkeypatch.setattr(processing, "_notify_file_created", lambda *a, **k: None)

    local_file = tmp_path / "sample.mp4"
    local_file.write_bytes(b"\x00" * 128)
    return str(local_file)


class TestWatchImportDispatchFailureVisibility:
    def test_a_failed_dispatch_after_a_successful_import_is_recorded_on_the_row(
        self, db_session, normal_user, media_file_stubs, monkeypatch, caplog
    ):
        import app.api.endpoints.files.upload as upload_module
        from app.services.watch_sources.processing import ingest_prepared_file

        monkeypatch.setattr(
            upload_module,
            "dispatch_upload_pipeline",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError(LEAKY_DISPATCH_ERROR)),
        )

        source = _mk_source(db_session, normal_user)
        row = _mk_tracking_row(db_session, source, "sample.mp4")

        with caplog.at_level(logging.ERROR, logger=PROCESSING_LOGGER):
            result = ingest_prepared_file(
                db_session, source, media_file_stubs, filename="sample.mp4", row=row
            )

        # DELIBERATE: the row stays "imported" — the bytes really were imported.
        # Flipping to "error" would make the next scan re-import it (imohash dedup
        # matches its own MediaFile) and mislabel it "skipped_duplicate", which
        # would never transcribe either. See processing.py's inline comment.
        assert result.status == "imported"
        assert result.media_file_id is not None

        # 1. The failure is VISIBLE, as a fixed sentence naming the exception class —
        #    that is the whole point of issue #906 and it must survive #914's sanitizing.
        assert (
            result.error_message
            == "Imported, but transcription dispatch failed: Pipeline dispatch failed (RuntimeError)"
        )

        # 2. ...and OPAQUE: none of the caught exception's own text reaches the field the
        #    watch-source file listing renders (#891/#914).
        assert BROKER_CREDENTIAL not in result.error_message
        assert LEAKY_DISPATCH_ERROR not in result.error_message

        # 3. The real cause is still diagnosable — the raw detail goes to the log instead.
        assert LEAKY_DISPATCH_ERROR in caplog.text
        assert any(
            record.levelno >= logging.ERROR and record.name == PROCESSING_LOGGER
            for record in caplog.records
        ), f"no ERROR record from {PROCESSING_LOGGER}; records={[r.name for r in caplog.records]}"

    def test_a_successful_dispatch_leaves_no_error_message(
        self, db_session, normal_user, media_file_stubs, monkeypatch
    ):
        """CONTROL: same fixture shape, dispatch succeeds."""
        import app.api.endpoints.files.upload as upload_module
        from app.services.watch_sources.processing import ingest_prepared_file

        monkeypatch.setattr(upload_module, "dispatch_upload_pipeline", lambda *a, **k: "task-id")

        source = _mk_source(db_session, normal_user)
        row = _mk_tracking_row(db_session, source, "sample.mp4")

        result = ingest_prepared_file(
            db_session, source, media_file_stubs, filename="sample.mp4", row=row
        )

        assert result.status == "imported"
        assert result.error_message is None
