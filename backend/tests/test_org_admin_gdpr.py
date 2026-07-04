"""Tests for sub-step 6.2: org-admin audit-log read, GDPR erasure, org-admin guard.

Three concerns, all GPU-free and DB-backed via the savepoint ``db_session``:

* ``require_org_admin`` raises 403 for non-org-admins and passes for org-admins.
* The org-admin audit-log read is **scoped** to the caller's org members (it
  filters by the org's member user-ids; it can never see another org's events).
* ``erase_user`` / ``erase_organization`` remove the expected rows. Storage
  (MinIO) and OpenSearch are mocked so these run without a live stack.

Community invariance is asserted: a personal context (no org) is never an
org-admin, so the guard 403s and the org-scoped surfaces are unreachable.
"""

import uuid as uuid_pkg
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.api.deps_context import RequestContext
from app.api.deps_context import require_org_admin
from app.models.media import Collection
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.user import User


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _mk_user(db, label: str) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"{label}_{uid}@example.com",
        full_name=f"{label} user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_org(db, label: str) -> Organization:
    uid = str(uuid_pkg.uuid4())[:8]
    org = Organization(external_org_id=f"org_{label}_{uid}", name=f"{label} Org", is_active=True)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _mk_file(db, *, user: User, org_id: int | None) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=f"f_{str(fuuid)[:8]}.mp4",
        storage_path=f"media/test/{fuuid}.mp4",
        content_type="video/mp4",
        file_size=1000,
        user_id=user.id,
        organization_id=org_id,
        status="completed",
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


class World:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture()
def two_orgs(db_session):
    """Two orgs, an admin member each, plus files/speakers/profiles/collections."""
    db = db_session
    org_a = _mk_org(db, "A")
    org_b = _mk_org(db, "B")
    admin_a = _mk_user(db, "admin_a")
    member_a = _mk_user(db, "member_a")
    admin_b = _mk_user(db, "admin_b")
    db.add_all(
        [
            OrganizationMembership(organization_id=org_a.id, user_id=admin_a.id, role="org:admin"),
            OrganizationMembership(
                organization_id=org_a.id, user_id=member_a.id, role="org:member"
            ),
            OrganizationMembership(organization_id=org_b.id, user_id=admin_b.id, role="org:admin"),
        ]
    )
    db.commit()

    file_a = _mk_file(db, user=member_a, org_id=org_a.id)
    speaker_a = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=member_a.id,
        organization_id=org_a.id,
        media_file_id=file_a.id,
        name="SPEAKER_00",
    )
    profile_a = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=member_a.id,
        organization_id=org_a.id,
        name=f"Profile {uuid_pkg.uuid4().hex[:6]}",
    )
    coll_a = Collection(
        uuid=uuid_pkg.uuid4(),
        user_id=member_a.id,
        organization_id=org_a.id,
        name=f"coll_{uuid_pkg.uuid4().hex[:6]}",
    )
    db.add_all([speaker_a, profile_a, coll_a])
    db.commit()

    return World(
        db=db,
        org_a=org_a,
        org_b=org_b,
        admin_a=admin_a,
        member_a=member_a,
        admin_b=admin_b,
        file_a=file_a,
        speaker_a=speaker_a,
        profile_a=profile_a,
        coll_a=coll_a,
    )


# --------------------------------------------------------------------------- #
# require_org_admin guard                                                      #
# --------------------------------------------------------------------------- #
class TestRequireOrgAdmin:
    def test_personal_context_is_403(self, two_orgs):
        """Community invariance: no org context -> not an org admin -> 403."""
        ctx = RequestContext(user=two_orgs.admin_a)  # org_id=None, org_role=None
        with pytest.raises(HTTPException) as exc:
            require_org_admin(ctx)
        assert exc.value.status_code == 403

    def test_org_member_is_403(self, two_orgs):
        """A plain org member is NOT an org admin."""
        ctx = RequestContext(
            user=two_orgs.member_a, org_id=two_orgs.org_a.id, org_role="org:member"
        )
        with pytest.raises(HTTPException) as exc:
            require_org_admin(ctx)
        assert exc.value.status_code == 403

    def test_org_admin_passes(self, two_orgs):
        """An org admin passes and the context is returned unchanged."""
        ctx = RequestContext(user=two_orgs.admin_a, org_id=two_orgs.org_a.id, org_role="org:admin")
        assert require_org_admin(ctx) is ctx


# --------------------------------------------------------------------------- #
# Org-admin audit-log read scoping                                            #
# --------------------------------------------------------------------------- #
class TestOrgAuditScope:
    def test_member_id_set_excludes_other_org(self, two_orgs):
        """The org-scope user-id set contains only the caller's org members."""
        from app.api.endpoints.org_admin import _org_member_user_ids

        ids_a = set(_org_member_user_ids(two_orgs.db, two_orgs.org_a.id))
        assert ids_a == {two_orgs.admin_a.id, two_orgs.member_a.id}
        assert two_orgs.admin_b.id not in ids_a  # org-B's admin is invisible

    def test_query_scoped_to_member_terms(self, two_orgs):
        """The org-admin read passes a ``terms`` filter on the org member ids;
        an org admin can never read another org's events."""
        from app.api.endpoints.org_admin import get_org_audit_logs

        ctx = RequestContext(user=two_orgs.admin_a, org_id=two_orgs.org_a.id, org_role="org:admin")
        captured = {}

        def _fake_query(**kwargs):
            captured.update(kwargs)
            return {"logs": [], "total": 0, "offset": 0, "limit": 100}

        with patch("app.api.endpoints.org_admin.query_audit_logs", _fake_query):
            # Direct call: pass the params FastAPI would resolve from Query(...).
            get_org_audit_logs(
                start_date=None,
                end_date=None,
                event_type=None,
                user_id=None,
                outcome=None,
                limit=100,
                offset=0,
                db=two_orgs.db,
                ctx=ctx,
            )

        assert set(captured["scope_user_ids"]) == {
            two_orgs.admin_a.id,
            two_orgs.member_a.id,
        }
        assert two_orgs.admin_b.id not in captured["scope_user_ids"]

    def test_user_id_filter_outside_org_is_403(self, two_orgs):
        """Filtering by a user_id outside the org is rejected (can't probe out)."""
        from app.api.endpoints.org_admin import get_org_audit_logs

        ctx = RequestContext(user=two_orgs.admin_a, org_id=two_orgs.org_a.id, org_role="org:admin")
        with pytest.raises(HTTPException) as exc:
            get_org_audit_logs(
                start_date=None,
                end_date=None,
                event_type=None,
                user_id=two_orgs.admin_b.id,
                outcome=None,
                limit=100,
                offset=0,
                db=two_orgs.db,
                ctx=ctx,
            )
        assert exc.value.status_code == 403

    def test_empty_scope_returns_nothing(self):
        """A scope of zero member-ids returns no events (not everything)."""
        from app.auth.audit import query_audit_logs

        with patch("app.auth.audit.settings") as s:
            s.AUDIT_LOG_TO_OPENSEARCH = True
            result = query_audit_logs(scope_user_ids=[])
        assert result["logs"] == []
        assert result["total"] == 0


# --------------------------------------------------------------------------- #
# Org attribution on audit events (issue #262a)                                #
# --------------------------------------------------------------------------- #
class _FakeAuditOS:
    """Captures the OpenSearch query body the audit read builds."""

    def __init__(self):
        self.captured_body = None

    def search(self, index, body):
        self.captured_body = body
        return {"hits": {"hits": [], "total": {"value": 0}}}


class TestAuditOrgAttribution:
    def test_log_event_carries_organization_id(self):
        """audit_logger.log(..., organization_id=N) stamps the event payload."""
        import json
        from unittest.mock import MagicMock

        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditOutcome
        from app.auth.audit import audit_logger

        fake_logger = MagicMock()
        with (
            patch("app.auth.audit.settings") as s,
            patch.object(audit_logger, "_logger", fake_logger),
        ):
            s.AUDIT_LOG_ENABLED = True
            s.AUDIT_LOG_TO_OPENSEARCH = False
            s.AUDIT_LOG_FORMAT = "json"
            audit_logger.log(
                event_type=AuditEventType.ADMIN_FILE_QUARANTINE,
                outcome=AuditOutcome.SUCCESS,
                user_id=7,
                username="a@example.com",
                organization_id=42,
            )

        event = json.loads(fake_logger.info.call_args[0][0])
        assert event["organization_id"] == 42
        assert event["user_id"] == 7

    def test_log_event_defaults_to_no_org(self):
        """No-context writers (e.g. local login) emit organization_id=None —
        the legacy/member-attributed class of events."""
        import json
        from unittest.mock import MagicMock

        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditOutcome
        from app.auth.audit import audit_logger

        fake_logger = MagicMock()
        with (
            patch("app.auth.audit.settings") as s,
            patch.object(audit_logger, "_logger", fake_logger),
        ):
            s.AUDIT_LOG_ENABLED = True
            s.AUDIT_LOG_TO_OPENSEARCH = False
            s.AUDIT_LOG_FORMAT = "json"
            audit_logger.log(
                event_type=AuditEventType.AUTH_LOGIN_SUCCESS,
                outcome=AuditOutcome.SUCCESS,
                user_id=7,
            )

        event = json.loads(fake_logger.info.call_args[0][0])
        assert event["organization_id"] is None

    def test_org_scope_clause_shape(self):
        """The org-admin visibility clause: org-stamped events (incl. user_id
        NULL) OR legacy un-stamped events attributed via member ids. An event
        stamped with a DIFFERENT org matches neither branch."""
        from app.auth.audit import build_org_scope_clause

        clause = build_org_scope_clause(42, [1, 2])
        assert clause == {
            "bool": {
                "should": [
                    {"term": {"organization_id": 42}},
                    {
                        "bool": {
                            "must_not": [{"exists": {"field": "organization_id"}}],
                            "filter": [{"terms": {"user_id": [1, 2]}}],
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }

    def test_get_org_audit_logs_passes_org_scope(self, two_orgs):
        """The org-admin endpoint threads BOTH the member set (legacy events)
        and its own org id (stamped events, incl. failed logins with user_id
        NULL) into the shared query."""
        from app.api.endpoints.org_admin import get_org_audit_logs

        ctx = RequestContext(user=two_orgs.admin_a, org_id=two_orgs.org_a.id, org_role="org:admin")
        captured = {}

        def _fake_query(**kwargs):
            captured.update(kwargs)
            return {"logs": [], "total": 0, "offset": 0, "limit": 100}

        with patch("app.api.endpoints.org_admin.query_audit_logs", _fake_query):
            get_org_audit_logs(
                start_date=None,
                end_date=None,
                event_type=None,
                user_id=None,
                outcome=None,
                limit=100,
                offset=0,
                db=two_orgs.db,
                ctx=ctx,
            )

        assert captured["scope_org_id"] == two_orgs.org_a.id
        assert set(captured["scope_user_ids"]) == {two_orgs.admin_a.id, two_orgs.member_a.id}

    def test_query_audit_logs_builds_org_scope_body(self):
        """query_audit_logs(scope_org_id=...) embeds the org-scope clause in
        the OpenSearch bool query."""
        from app.auth.audit import build_org_scope_clause
        from app.auth.audit import query_audit_logs

        fake = _FakeAuditOS()
        with (
            patch("app.auth.audit.settings") as s,
            patch("app.auth.audit._build_audit_opensearch_client", return_value=fake),
        ):
            s.AUDIT_LOG_TO_OPENSEARCH = True
            result = query_audit_logs(scope_user_ids=[1, 2], scope_org_id=42)

        assert result["total"] == 0
        assert fake.captured_body is not None
        must = fake.captured_body["query"]["bool"]["must"]
        assert build_org_scope_clause(42, [1, 2]) in must
        # The bare member-terms filter must NOT also be applied — it would
        # exclude org-stamped events with user_id NULL (failed logins).
        assert {"terms": {"user_id": [1, 2]}} not in must

    def test_org_scope_with_empty_member_set_still_queries(self):
        """With an org id, an empty member set must NOT short-circuit — the
        org's STAMPED events (e.g. failed logins, user_id NULL) stay visible."""
        from app.auth.audit import query_audit_logs

        fake = _FakeAuditOS()
        with (
            patch("app.auth.audit.settings") as s,
            patch("app.auth.audit._build_audit_opensearch_client", return_value=fake),
        ):
            s.AUDIT_LOG_TO_OPENSEARCH = True
            query_audit_logs(scope_user_ids=[], scope_org_id=42)

        assert fake.captured_body is not None  # query executed, not short-circuited
        must = fake.captured_body["query"]["bool"]["must"]
        assert {"term": {"organization_id": 42}} in must[0]["bool"]["should"]

    def test_member_only_scope_unchanged(self):
        """Without an org id the pre-existing member-terms scoping is intact
        (super-admin/global callers are unaffected)."""
        from app.auth.audit import query_audit_logs

        fake = _FakeAuditOS()
        with (
            patch("app.auth.audit.settings") as s,
            patch("app.auth.audit._build_audit_opensearch_client", return_value=fake),
        ):
            s.AUDIT_LOG_TO_OPENSEARCH = True
            query_audit_logs(scope_user_ids=[1, 2])

        assert fake.captured_body is not None
        assert {"terms": {"user_id": [1, 2]}} in fake.captured_body["query"]["bool"]["must"]

    def test_request_org_id_only_trusts_resolved_int(self):
        """request.state.org_id may transiently hold the provider's RAW string
        (pre-membership-mirror); only the refined int is a valid audit stamp."""
        from types import SimpleNamespace

        from app.auth.audit import request_org_id

        assert request_org_id(SimpleNamespace(state=SimpleNamespace(org_id=5))) == 5
        assert request_org_id(SimpleNamespace(state=SimpleNamespace(org_id="org_raw123"))) is None
        assert request_org_id(SimpleNamespace(state=SimpleNamespace())) is None

    def test_gdpr_org_erasure_events_are_org_stamped(self, two_orgs):
        """The org-admin GDPR erasure audit events carry the tenant id."""
        w = two_orgs
        calls = []

        def _capture(**kwargs):
            calls.append(kwargs)

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            patch(
                "app.services.file_cleanup_service._cleanup_opensearch_for_file",
                return_value=None,
            ),
            patch(
                "app.services.gdpr_erasure_service._erase_speaker_voiceprints",
                return_value=0,
            ),
            patch(
                "app.services.opensearch_service.remove_profile_embedding",
                return_value=True,
            ),
            patch("app.services.gdpr_erasure_service.audit_logger") as fake_audit,
        ):
            fake_audit.log.side_effect = _capture
            from app.services.gdpr_erasure_service import erase_org_member_data

            erase_org_member_data(
                w.db,
                int(w.member_a.id),
                int(w.org_a.id),
                actor_user_id=int(w.admin_a.id),
                actor_email=str(w.admin_a.email),
            )

        assert calls, "erasure must audit"
        assert calls[-1]["organization_id"] == w.org_a.id


# --------------------------------------------------------------------------- #
# GDPR erasure                                                                 #
# --------------------------------------------------------------------------- #
class TestEraseUser:
    def test_erase_user_removes_rows(self, two_orgs):
        """erase_user removes the user's files, speaker, profile, collection,
        membership, and the user row itself."""
        db = two_orgs.db
        member_id = two_orgs.member_a.id

        # Mock storage + OpenSearch (purge_media_file + voiceprint delete_by_query).
        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            patch(
                "app.services.file_cleanup_service._cleanup_opensearch_for_file",
                return_value=None,
            ),
            patch(
                "app.services.gdpr_erasure_service._erase_speaker_voiceprints",
                return_value=3,
            ),
            patch(
                "app.services.opensearch_service.remove_profile_embedding",
                return_value=True,
            ),
        ):
            from app.services.gdpr_erasure_service import erase_user

            summary = erase_user(db, member_id)

        assert summary["users_deleted"] == 1
        assert summary["media_files_deleted"] == 1
        assert summary["speaker_profiles_deleted"] == 1
        assert summary["collections_deleted"] == 1
        assert summary["voiceprints_deleted"] == 3
        assert summary["sla_days"] == 30
        assert summary["errors"] == []

        # Rows are gone.
        assert db.query(User).filter(User.id == member_id).first() is None
        assert db.query(MediaFile).filter(MediaFile.user_id == member_id).count() == 0
        assert db.query(SpeakerProfile).filter(SpeakerProfile.user_id == member_id).count() == 0
        assert (
            db.query(OrganizationMembership)
            .filter(OrganizationMembership.user_id == member_id)
            .count()
            == 0
        )

    def test_erase_user_idempotent(self, two_orgs):
        """Erasing a non-existent user id is a no-op with zeroed counters."""
        from app.services.gdpr_erasure_service import erase_user

        summary = erase_user(two_orgs.db, 999_999_999)
        assert summary["already_erased"] is True
        assert summary["users_deleted"] == 0


class TestEraseOrganization:
    def test_erase_org_removes_org_data(self, two_orgs):
        """erase_organization removes org files/profiles/collections + the org row,
        but keeps the member's user account."""
        db = two_orgs.db
        org_id = two_orgs.org_a.id
        member_id = two_orgs.member_a.id

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            patch(
                "app.services.file_cleanup_service._cleanup_opensearch_for_file",
                return_value=None,
            ),
            patch(
                "app.services.gdpr_erasure_service._erase_speaker_voiceprints",
                return_value=5,
            ),
            patch(
                "app.services.opensearch_service.remove_profile_embedding",
                return_value=True,
            ),
        ):
            from app.services.gdpr_erasure_service import erase_organization

            summary = erase_organization(db, org_id)

        assert summary["media_files_deleted"] == 1
        assert summary["speaker_profiles_deleted"] == 1
        assert summary["collections_deleted"] == 1
        assert summary["voiceprints_deleted"] == 5
        assert summary["memberships_removed"] >= 1
        assert summary["errors"] == []

        # Org + its org-scoped data gone; member user survives.
        assert db.query(Organization).filter(Organization.id == org_id).first() is None
        assert db.query(MediaFile).filter(MediaFile.organization_id == org_id).count() == 0
        assert (
            db.query(OrganizationMembership)
            .filter(OrganizationMembership.organization_id == org_id)
            .count()
            == 0
        )
        assert db.query(User).filter(User.id == member_id).first() is not None

    def test_erase_org_idempotent(self, two_orgs):
        from app.services.gdpr_erasure_service import erase_organization

        summary = erase_organization(two_orgs.db, 999_999_999)
        assert summary["already_erased"] is True


# --------------------------------------------------------------------------- #
# Org-scoped member erasure + legal-hold guard (0.5.0 review fixes)            #
# --------------------------------------------------------------------------- #
class TestOrgScopedErasure:
    def test_erase_org_member_data_is_org_scoped(self, two_orgs):
        """The org-admin erasure destroys ONLY the target's rows in that org —
        personal-scope files, other-org files, and the account survive."""
        w = two_orgs
        db = w.db
        personal_file = _mk_file(db, user=w.member_a, org_id=None)
        other_org_file = _mk_file(db, user=w.member_a, org_id=w.org_b.id)

        from app.services.gdpr_erasure_service import erase_org_member_data

        summary = erase_org_member_data(
            db,
            int(w.member_a.id),
            int(w.org_a.id),
            actor_user_id=int(w.admin_a.id),
            actor_email=str(w.admin_a.email),
        )

        assert summary["media_files_deleted"] == 1  # only file_a (org A)
        assert db.query(MediaFile).filter(MediaFile.id == personal_file.id).first() is not None
        assert db.query(MediaFile).filter(MediaFile.id == other_org_file.id).first() is not None
        assert db.query(MediaFile).filter(MediaFile.id == w.file_a.id).first() is None
        # Org-scoped profile/collection gone; the ACCOUNT survives.
        assert db.query(SpeakerProfile).filter(SpeakerProfile.id == w.profile_a.id).first() is None
        assert db.query(Collection).filter(Collection.id == w.coll_a.id).first() is None
        assert db.query(User).filter(User.id == w.member_a.id).first() is not None

    def test_erase_user_skips_legal_hold_files(self, two_orgs):
        """GDPR Art. 17(3)(e): files under an active legal hold are preserved and
        reported; the user row is retained until the hold releases."""
        w = two_orgs
        db = w.db
        held = _mk_file(db, user=w.member_a, org_id=None)
        held.legal_hold = True
        held.is_quarantined = True
        db.commit()

        from app.services.gdpr_erasure_service import erase_user

        summary = erase_user(db, int(w.member_a.id))

        assert summary["legal_holds_skipped"] == 1
        assert db.query(MediaFile).filter(MediaFile.id == held.id).first() is not None
        # Non-held file was erased; account retained because a hold remains.
        assert db.query(MediaFile).filter(MediaFile.id == w.file_a.id).first() is None
        assert summary["users_deleted"] == 0
        assert db.query(User).filter(User.id == w.member_a.id).first() is not None
