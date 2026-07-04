"""Issue #262c — org context for background imports is CAPTURED, never guessed.

Watch-source imports stamp ``watch_source.organization_id`` (captured at
source-creation time, v372) and playlist placeholders receive the originating
request's ``ctx.org_id`` through the task kwargs. The first-active-membership
resolver (``resolve_owner_org_id``) must no longer influence either path — the
tests below give the owner a DIFFERENT org membership and assert it never
leaks into the created rows. Savepoint-isolated via ``db_session``.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile


def _mk_org(db, label: str) -> Organization:
    org = Organization(
        external_org_id=f"org_{label}_{uuid_pkg.uuid4().hex[:8]}",
        name=f"{label} Org",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _mk_source(db, owner, *, organization_id: int | None) -> WatchSource:
    source = WatchSource(
        uuid=uuid_pkg.uuid4(),
        name=f"src_{uuid_pkg.uuid4().hex[:8]}",
        source_type="local",
        user_id=owner.id,
        created_by=owner.id,
        organization_id=organization_id,
        auto_transcribe=False,  # skip pipeline dispatch in the ingest tail
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


class TestWatchImportOrgStamp:
    def test_import_stamps_source_org(self, db_session, normal_user, media_file_stubs):
        from app.models.media import MediaFile
        from app.services.watch_sources.processing import ingest_prepared_file

        org = _mk_org(db_session, "watch")
        source = _mk_source(db_session, normal_user, organization_id=org.id)
        row = _mk_tracking_row(db_session, source, "sample.mp4")

        result = ingest_prepared_file(
            db_session, source, media_file_stubs, filename="sample.mp4", row=row
        )

        assert result.status == "imported"
        created = db_session.query(MediaFile).filter(MediaFile.id == result.media_file_id).first()
        assert created is not None
        assert created.organization_id == org.id

    def test_import_never_guesses_from_membership(self, db_session, normal_user, media_file_stubs):
        """A PERSONAL source (org NULL) imports personal files even when the
        owner holds an org membership — the old first-membership guess is gone."""
        from app.models.media import MediaFile
        from app.services.watch_sources.processing import ingest_prepared_file

        other_org = _mk_org(db_session, "other")
        db_session.add(
            OrganizationMembership(
                organization_id=other_org.id, user_id=normal_user.id, role="org:member"
            )
        )
        db_session.commit()

        source = _mk_source(db_session, normal_user, organization_id=None)
        row = _mk_tracking_row(db_session, source, "sample.mp4")

        result = ingest_prepared_file(
            db_session, source, media_file_stubs, filename="sample.mp4", row=row
        )

        assert result.status == "imported"
        created = db_session.query(MediaFile).filter(MediaFile.id == result.media_file_id).first()
        assert created is not None
        assert created.organization_id is None  # NOT other_org.id


class TestPlaylistPlaceholderOrgStamp:
    def _entry(self):
        return {
            "video_id": uuid_pkg.uuid4().hex[:11],
            "url": "https://www.youtube.com/watch?v=abc",
            "title": "Video",
            "duration": 10,
            "uploader": "someone",
            "playlist_index": 1,
        }

    def test_placeholder_stamps_threaded_org(self, db_session, normal_user):
        from app.services.media_download_service import _create_playlist_video_placeholder

        org = _mk_org(db_session, "pl")
        media_file = _create_playlist_video_placeholder(
            db_session,
            normal_user.id,
            self._entry(),
            {"playlist_id": "PL1", "playlist_title": "T"},
            "https://www.youtube.com/playlist?list=PL1",
            organization_id=org.id,
        )
        assert media_file.organization_id == org.id

    def test_placeholder_personal_despite_membership(self, db_session, normal_user):
        """organization_id=None means PERSONAL — a membership in some org must
        not be resolved into the placeholder (the removed guess)."""
        from app.services.media_download_service import _create_playlist_video_placeholder

        other_org = _mk_org(db_session, "plother")
        db_session.add(
            OrganizationMembership(
                organization_id=other_org.id, user_id=normal_user.id, role="org:member"
            )
        )
        db_session.commit()

        media_file = _create_playlist_video_placeholder(
            db_session,
            normal_user.id,
            self._entry(),
            {"playlist_id": "PL2", "playlist_title": "T"},
            "https://www.youtube.com/playlist?list=PL2",
            organization_id=None,
        )
        assert media_file.organization_id is None

    def test_process_playlist_videos_threads_org(self, db_session, normal_user):
        from app.services.media_download_service import _process_playlist_videos

        org = _mk_org(db_session, "plthread")
        created, skipped = _process_playlist_videos(
            db_session,
            normal_user.id,
            [self._entry()],
            {"playlist_id": "PL3", "playlist_title": "T"},
            "https://www.youtube.com/playlist?list=PL3",
            1,
            organization_id=org.id,
        )
        assert len(created) == 1 and not skipped
        assert created[0].organization_id == org.id
