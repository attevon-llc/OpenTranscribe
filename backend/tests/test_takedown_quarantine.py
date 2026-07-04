"""Abuse / DMCA / safe-harbor takedown (quarantine) regression tests — 6.4.

Covers the takedown invariants on the real DB via the savepoint ``db_session``
fixture (no MinIO/OpenSearch needed — the storage legal-hold is best-effort and
patched/ignored here, and the search exclusion is asserted at the predicate
level):

  * A quarantined file 404s for its OWNER on the per-resource access gate
    (``get_file_by_uuid_with_permission``) — and on every surface that goes
    through it (detail/stream/download/thumbnail).
  * An ADMIN still resolves the quarantined file (for review).
  * Releasing restores access for the owner and clears the legal-hold.
  * ``exclude_quarantined`` drops the file from a list/gallery query for normal
    users but leaves it for the admin "see all" branch.
  * Community invariance: a never-quarantined file is unaffected.
  * DMCA §512(g) owner notices (issue #262 item f): the OWNER is notified on
    quarantine (reason + counter-notice contact, no admin identity) and on
    release, and a notification failure never breaks either action.
"""

import json
import uuid as uuid_pkg

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.takedown_service import exclude_quarantined
from app.services.takedown_service import is_hidden_for
from app.services.takedown_service import quarantine_file
from app.services.takedown_service import release_file
from app.utils.uuid_helpers import get_file_by_uuid_with_permission


def _mk_user(db, *, admin: bool = False) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"{'admin' if admin else 'user'}_{uid}@example.com",
        full_name="Takedown test user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=admin,
        role="super_admin" if admin else "user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_file(db, *, owner: User) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=f"f_{str(fuuid)[:8]}.mp4",
        # No real object — keeps the best-effort S3 legal-hold a harmless no-op.
        storage_path="",
        content_type="video/mp4",
        file_size=1000,
        user_id=owner.id,
        status=FileStatus.COMPLETED,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


@pytest.fixture()
def world(db_session):
    db = db_session
    owner = _mk_user(db, admin=False)
    admin = _mk_user(db, admin=True)
    file = _mk_file(db, owner=owner)
    return db, owner, admin, file


class TestQuarantineAccessGate:
    def test_owner_can_reach_file_before_takedown(self, world):
        db, owner, _admin, file = world
        got = get_file_by_uuid_with_permission(db, str(file.uuid), owner.id, allow_public=True)
        assert got.id == file.id

    def test_quarantine_hides_file_from_owner(self, world):
        """A taken-down file 404s for its owner (every read surface uses this)."""
        db, owner, admin, file = world
        quarantine_file(db, file, admin=admin, reason="DMCA-12345", legal_hold=True)

        with pytest.raises(Exception) as exc:
            get_file_by_uuid_with_permission(db, str(file.uuid), owner.id, allow_public=True)
        assert getattr(exc.value, "status_code", None) == 404

    def test_quarantined_file_still_visible_to_admin(self, world):
        """Admins keep visibility (is_admin=True bypasses the gate) for review."""
        db, owner, admin, file = world
        quarantine_file(db, file, admin=admin, reason="abuse report 7", legal_hold=True)

        got = get_file_by_uuid_with_permission(
            db, str(file.uuid), admin.id, is_admin=True, allow_public=True
        )
        assert got.id == file.id
        assert got.is_quarantined is True

    def test_release_restores_owner_access(self, world):
        """Releasing the file restores the owner's access and clears the hold."""
        db, owner, admin, file = world
        quarantine_file(db, file, admin=admin, reason="mistaken", legal_hold=True)
        assert file.legal_hold is True

        release_file(db, file, admin=admin)
        assert file.is_quarantined is False
        assert file.legal_hold is False
        assert file.quarantined_by is None
        assert file.status == FileStatus.COMPLETED

        got = get_file_by_uuid_with_permission(db, str(file.uuid), owner.id, allow_public=True)
        assert got.id == file.id


class TestQuarantineState:
    def test_quarantine_sets_metadata(self, world):
        db, _owner, admin, file = world
        quarantine_file(db, file, admin=admin, reason="copyright claim", legal_hold=True)
        assert file.is_quarantined is True
        assert file.quarantine_reason == "copyright claim"
        assert file.quarantined_at is not None
        assert file.quarantined_by == admin.id
        assert file.legal_hold is True
        assert file.status == FileStatus.QUARANTINED

    def test_quarantine_without_legal_hold(self, world):
        db, _owner, admin, file = world
        quarantine_file(db, file, admin=admin, reason="no hold", legal_hold=False)
        assert file.is_quarantined is True
        assert file.legal_hold is False

    def test_is_hidden_for_helper(self, world):
        db, _owner, admin, file = world
        # Not quarantined: visible to everyone.
        assert is_hidden_for(file, is_admin=False) is False
        quarantine_file(db, file, admin=admin, reason="x", legal_hold=False)
        # Quarantined: hidden from non-admin, visible to admin.
        assert is_hidden_for(file, is_admin=False) is True
        assert is_hidden_for(file, is_admin=True) is False


class TestExcludeQuarantinedPredicate:
    def test_exclude_drops_quarantined_for_user(self, world):
        """The gallery/list predicate removes a taken-down file for normal users."""
        db, owner, admin, file = world
        quarantine_file(db, file, admin=admin, reason="hidden", legal_hold=False)

        base = db.query(MediaFile).filter(MediaFile.user_id == owner.id)
        visible = exclude_quarantined(base).all()
        ids = {f.id for f in visible}
        assert file.id not in ids

    def test_admin_branch_keeps_quarantined(self, world):
        """include_quarantined=True (admin 'see all') leaves the file in."""
        db, owner, admin, file = world
        quarantine_file(db, file, admin=admin, reason="hidden", legal_hold=False)

        base = db.query(MediaFile).filter(MediaFile.user_id == owner.id)
        visible = exclude_quarantined(base, include_quarantined=True).all()
        ids = {f.id for f in visible}
        assert file.id in ids

    def test_non_quarantined_file_unaffected(self, world):
        """Community invariance: a normal file passes the exclusion unchanged."""
        db, owner, _admin, file = world
        base = db.query(MediaFile).filter(MediaFile.user_id == owner.id)
        ids = {f.id for f in exclude_quarantined(base).all()}
        assert file.id in ids


class TestQuarantineAuditOrgStamp:
    """Issue #262a: takedown/release audit events carry the file's tenant."""

    def test_quarantine_audit_stamped_with_file_org(self, world):
        import uuid as _uuid
        from unittest.mock import patch

        from app.models.organization import Organization

        db, _owner, admin, file = world
        org = Organization(
            external_org_id=f"org_{_uuid.uuid4().hex[:8]}", name="Q Org", is_active=True
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        file.organization_id = org.id
        db.commit()

        calls = []
        with patch("app.services.takedown_service.audit_logger") as fake_audit:
            fake_audit.log.side_effect = lambda **kw: calls.append(kw)
            quarantine_file(db, file, admin=admin, reason="dmca", legal_hold=False)
            release_file(db, file, admin=admin, clear_legal_hold=False)

        assert len(calls) == 2
        assert all(c["organization_id"] == org.id for c in calls)

    def test_personal_file_audit_has_no_org(self, world):
        from unittest.mock import patch

        db, _owner, admin, file = world
        calls = []
        with patch("app.services.takedown_service.audit_logger") as fake_audit:
            fake_audit.log.side_effect = lambda **kw: calls.append(kw)
            quarantine_file(db, file, admin=admin, reason="x", legal_hold=False)

        assert calls[0]["organization_id"] is None


class TestCollectionCountsExcludeQuarantined:
    """Issue #262g: collection member counts (and the paginated member list)
    must not include quarantined files for non-admins — a count/list mismatch
    would leak the takedown."""

    def _mk_collection_with_files(self, db, owner, *, quarantine_one: bool):
        from app.models.media import Collection
        from app.models.media import CollectionMember

        coll = Collection(
            uuid=uuid_pkg.uuid4(),
            name=f"coll_{uuid_pkg.uuid4().hex[:8]}",
            user_id=owner.id,
        )
        db.add(coll)
        db.commit()
        db.refresh(coll)

        visible = _mk_file(db, owner=owner)
        hidden = _mk_file(db, owner=owner)
        if quarantine_one:
            hidden.is_quarantined = True
            db.commit()
        db.add_all(
            [
                CollectionMember(collection_id=coll.id, media_file_id=visible.id),
                CollectionMember(collection_id=coll.id, media_file_id=hidden.id),
            ]
        )
        db.commit()
        return coll, visible, hidden

    def test_visible_media_counts_helper(self, world):
        from app.api.endpoints.media_collections import _visible_media_counts

        db, owner, _admin, _file = world
        coll, _visible, _hidden = self._mk_collection_with_files(db, owner, quarantine_one=True)

        user_counts = _visible_media_counts(db, [coll.id], include_quarantined=False)
        admin_counts = _visible_media_counts(db, [coll.id], include_quarantined=True)
        assert user_counts.get(coll.id, 0) == 1
        assert admin_counts.get(coll.id, 0) == 2

    def test_visible_media_counts_empty_collection(self, world):
        """A collection with zero members simply has no row (callers .get 0)."""
        from app.api.endpoints.media_collections import _visible_media_counts

        db = world[0]
        assert _visible_media_counts(db, [], include_quarantined=False) == {}

    def test_list_collections_count_hides_quarantined(self, client, world):
        db, owner, _admin, _file = world
        coll, _visible, _hidden = self._mk_collection_with_files(db, owner, quarantine_one=True)

        resp = client.post(
            "/api/auth/token",
            data={"username": owner.email, "password": "password123"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        listing = client.get("/api/collections?ownership=mine", headers=headers)
        assert listing.status_code == 200
        counts = {c["name"]: c["media_count"] for c in listing.json()}
        assert counts[coll.name] == 1  # quarantined member hidden from the count

    def test_collection_media_listing_hides_quarantined(self, client, world):
        db, owner, _admin, _file = world
        coll, visible, hidden = self._mk_collection_with_files(db, owner, quarantine_one=True)

        resp = client.post(
            "/api/auth/token",
            data={"username": owner.email, "password": "password123"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        media = client.get(f"/api/collections/{coll.uuid}/media", headers=headers)
        assert media.status_code == 200
        body = media.json()
        uuids = {item["uuid"] for item in body["items"]}
        assert str(visible.uuid) in uuids
        assert str(hidden.uuid) not in uuids
        assert body["total"] == 1


class TestPriorStatusRestore:
    def test_release_restores_pre_quarantine_status(self, world):
        """A file quarantined while ERROR must release back to ERROR, not
        COMPLETED (v371 pre_quarantine_status)."""
        db, _owner, admin, file = world
        file.status = FileStatus.ERROR
        db.commit()

        quarantine_file(db, file, admin=admin, reason="test")
        assert file.status == FileStatus.QUARANTINED
        assert file.pre_quarantine_status == FileStatus.ERROR.value

        release_file(db, file, admin=admin)
        assert file.status == FileStatus.ERROR
        assert file.pre_quarantine_status is None

    def test_requarantine_keeps_original_prior_status(self, world):
        """Re-quarantining an already-quarantined file must not overwrite the
        recorded prior status with QUARANTINED."""
        db, _owner, admin, file = world
        file.status = FileStatus.COMPLETED
        db.commit()

        quarantine_file(db, file, admin=admin, reason="first")
        quarantine_file(db, file, admin=admin, reason="second (refresh)")
        assert file.pre_quarantine_status == FileStatus.COMPLETED.value

        release_file(db, file, admin=admin)
        assert file.status == FileStatus.COMPLETED

    def test_release_falls_back_to_completed_without_recorded_status(self, world):
        """Rows quarantined before v371 have no recorded prior status — release
        falls back to COMPLETED."""
        db, _owner, admin, file = world
        quarantine_file(db, file, admin=admin, reason="test")
        file.pre_quarantine_status = None
        db.commit()

        release_file(db, file, admin=admin)
        assert file.status == FileStatus.COMPLETED


class TestOwnerNotification:
    """DMCA §512(g) owner notices — the quarantined file 404s for its owner,
    so the persistent notification is the owner's only takedown/counter-notice
    surface (issue #262 item f)."""

    @pytest.fixture()
    def sent(self, monkeypatch):
        """Capture ``send_task_notification`` calls (patched at its home module,
        which the service imports lazily at call time)."""
        calls: list[dict] = []

        def fake_send(user_id, event_type, **kwargs):
            calls.append({"user_id": user_id, "event_type": event_type, **kwargs})
            return True

        monkeypatch.setattr("app.services.notification_service.send_task_notification", fake_send)
        return calls

    def test_owner_notified_on_quarantine(self, world, sent, monkeypatch):
        """Quarantine sends the OWNER one `file_takedown` notice carrying the
        file name, the reason, and the abuse-contact address — and nothing
        identifying the acting admin."""
        from app.core.config import settings

        db, owner, admin, file = world
        monkeypatch.setattr(settings, "ABUSE_CONTACT_EMAIL", "abuse@deploy.example")

        quarantine_file(db, file, admin=admin, reason="DMCA notice #777")

        assert len(sent) == 1
        note = sent[0]
        assert note["user_id"] == owner.id
        assert note["event_type"] == "file_takedown"
        assert note["status"] == "warning"
        extra = note["extra"]
        assert extra["file_uuid"] == str(file.uuid)
        assert extra["filename"] == file.filename
        assert extra["reason"] == "DMCA notice #777"
        assert extra["abuse_contact_email"] == "abuse@deploy.example"
        # No dead link: the file 404s for the owner, so no `file_id` (the panel
        # only renders a file link when `file_id` is present).
        assert "file_id" not in extra
        # No leakage of the acting admin's identity anywhere in the payload.
        blob = json.dumps(note, default=str)
        assert admin.email not in blob
        assert "quarantined_by" not in blob
        # The message itself carries the counter-notice contact + file UUID.
        assert "abuse@deploy.example" in note["message"]
        assert str(file.uuid) in note["message"]

    def test_takedown_notice_without_abuse_contact(self, world, sent, monkeypatch):
        """With ABUSE_CONTACT_EMAIL unset the notice still goes out, directing
        the owner to the service operator."""
        from app.core.config import settings

        db, owner, admin, file = world
        monkeypatch.setattr(settings, "ABUSE_CONTACT_EMAIL", "")

        quarantine_file(db, file, admin=admin, reason="AUP violation")

        assert len(sent) == 1
        assert sent[0]["extra"]["abuse_contact_email"] == ""
        assert "service operator" in sent[0]["message"]

    def test_owner_notified_on_release(self, world, sent):
        """Release sends the OWNER a `file_takedown_released` notice with a
        working file link (access is restored, so `file_id` is included)."""
        db, owner, admin, file = world
        quarantine_file(db, file, admin=admin, reason="disputed")
        release_file(db, file, admin=admin)

        assert len(sent) == 2
        note = sent[1]
        assert note["user_id"] == owner.id
        assert note["event_type"] == "file_takedown_released"
        assert note["status"] == "completed"
        assert note["extra"]["file_id"] == str(file.uuid)
        assert note["extra"]["filename"] == file.filename
        blob = json.dumps(note, default=str)
        assert admin.email not in blob

    def test_notice_prefers_title_over_filename(self, world, sent):
        """The human-facing name is the media title when one was extracted."""
        db, _owner, admin, file = world
        file.title = "Quarterly All-Hands"
        db.commit()

        quarantine_file(db, file, admin=admin, reason="report 9")
        assert sent[0]["extra"]["filename"] == "Quarterly All-Hands"

    def test_notification_failure_never_breaks_takedown(self, world, monkeypatch):
        """Failure containment: a raising notifier must not break the takedown
        or the release — the DB action and audit still complete."""

        def boom(*_args, **_kwargs):
            raise RuntimeError("redis down")

        monkeypatch.setattr("app.services.notification_service.send_task_notification", boom)
        db, owner, admin, file = world

        quarantine_file(db, file, admin=admin, reason="DMCA-1", legal_hold=True)
        assert file.is_quarantined is True
        assert file.legal_hold is True

        release_file(db, file, admin=admin)
        assert file.is_quarantined is False
