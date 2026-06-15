"""Cross-tenant (organization) isolation regression tests — sub-step 1.2.

Covers the SQL choke points, the cross-org sharing gate, and the OpenSearch
search/speaker filter builders. The SQL-plane tests run WITHOUT OpenSearch (they
hit the real DB via the savepoint ``db_session`` fixture). The OpenSearch-plane
tests assert the FILTER BODIES the search/kNN queries emit — pure-Python, no
cluster needed — so they also run in fast CI.

Two key invariants are asserted throughout:
  * Default-deny: org-A may not reach org-B's resources by UUID, by share, or by
    search; org-B voiceprints never surface for org-A.
  * Community invariance: with no org context (``UNSCOPED``) behavior is
    unchanged, and with personal scope (``None``) org-stamped rows are excluded.
"""

import uuid as uuid_pkg

import pytest

from app.core.tenancy import UNSCOPED
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.sharing import CollectionShare
from app.models.user import User
from app.services.permission_service import PermissionService
from app.utils.uuid_helpers import get_file_by_uuid_with_permission


# --------------------------------------------------------------------------- #
# Two-org / two-user fixture                                                   #
# --------------------------------------------------------------------------- #
class TwoOrgWorld:
    """Holder for a fully-populated two-tenant scenario."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


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
    org = Organization(clerk_org_id=f"org_{label}_{uid}", name=f"{label} Org", is_active=True)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _mk_file(db, *, user: User, org_id: int | None, public: bool = False) -> MediaFile:
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
        is_public=public,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


@pytest.fixture()
def two_orgs(db_session):
    """Two orgs, one member each, one file + one speaker per org."""
    db = db_session
    org_a = _mk_org(db, "A")
    org_b = _mk_org(db, "B")
    user_a = _mk_user(db, "alice")
    user_b = _mk_user(db, "bob")
    db.add(OrganizationMembership(organization_id=org_a.id, user_id=user_a.id, role="org:admin"))
    db.add(OrganizationMembership(organization_id=org_b.id, user_id=user_b.id, role="org:admin"))
    db.commit()

    file_a = _mk_file(db, user=user_a, org_id=org_a.id)
    file_b = _mk_file(db, user=user_b, org_id=org_b.id)

    speaker_a = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=user_a.id,
        organization_id=org_a.id,
        media_file_id=file_a.id,
        name="SPEAKER_00",
    )
    speaker_b = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=user_b.id,
        organization_id=org_b.id,
        media_file_id=file_b.id,
        name="SPEAKER_00",
    )
    db.add_all([speaker_a, speaker_b])
    db.commit()

    return TwoOrgWorld(
        db=db,
        org_a=org_a,
        org_b=org_b,
        user_a=user_a,
        user_b=user_b,
        file_a=file_a,
        file_b=file_b,
        speaker_a=speaker_a,
        speaker_b=speaker_b,
    )


# --------------------------------------------------------------------------- #
# (a)/(b) SQL choke points — file UUID + sharing                              #
# --------------------------------------------------------------------------- #
class TestSqlFileAccess:
    def test_org_a_cannot_reach_org_b_file_by_uuid(self, two_orgs):
        """org-A user (org_a scope) is denied org-B's file UUID (default-deny)."""
        w = two_orgs
        with pytest.raises(Exception) as exc:  # HTTPException 403
            get_file_by_uuid_with_permission(
                w.db,
                str(w.file_b.uuid),
                w.user_a.id,
                organization_id=w.org_a.id,
            )
        assert getattr(exc.value, "status_code", None) == 403

    def test_org_a_can_reach_own_org_file(self, two_orgs):
        """Same-org access still works under org context."""
        w = two_orgs
        got = get_file_by_uuid_with_permission(
            w.db, str(w.file_a.uuid), w.user_a.id, organization_id=w.org_a.id
        )
        assert got.id == w.file_a.id

    def test_personal_scope_excludes_org_file(self, two_orgs):
        """A personal request (org_id=None) cannot reach the user's OWN org file."""
        w = two_orgs
        with pytest.raises(Exception) as exc:
            get_file_by_uuid_with_permission(
                w.db, str(w.file_a.uuid), w.user_a.id, organization_id=None
            )
        assert getattr(exc.value, "status_code", None) == 403

    def test_unscoped_legacy_path_unchanged(self, two_orgs):
        """UNSCOPED (community/legacy) lets the owner reach their org-stamped file
        exactly as before the isolation change."""
        w = two_orgs
        got = get_file_by_uuid_with_permission(
            w.db, str(w.file_a.uuid), w.user_a.id, organization_id=UNSCOPED
        )
        assert got.id == w.file_a.id
        # default (no kwarg) is UNSCOPED too
        got2 = get_file_by_uuid_with_permission(w.db, str(w.file_a.uuid), w.user_a.id)
        assert got2.id == w.file_a.id


class TestCrossOrgSharing:
    def _share_file_b_into_org_a_user(self, w):
        """Build a (mis-scoped) collection-share that grants org-A's user a view
        on org-B's file — the leak the gate must close."""
        db = w.db
        # Collection that holds file_b but is owned by org_b/user_b
        coll = Collection(
            uuid=uuid_pkg.uuid4(),
            name=f"shared_{uuid_pkg.uuid4().hex[:6]}",
            user_id=w.user_b.id,
            organization_id=w.org_b.id,
        )
        db.add(coll)
        db.commit()
        db.refresh(coll)
        db.add(CollectionMember(collection_id=coll.id, media_file_id=w.file_b.id))
        db.add(
            CollectionShare(
                collection_id=coll.id,
                shared_by_id=w.user_b.id,
                target_type="user",
                target_user_id=w.user_a.id,
                permission="viewer",
            )
        )
        db.commit()
        return coll

    def test_cross_org_shared_file_denied_under_org_scope(self, two_orgs):
        """Even with a direct user share, org-A (org scope) cannot resolve a
        permission on org-B's file."""
        w = two_orgs
        self._share_file_b_into_org_a_user(w)

        perm = PermissionService.get_file_permission(
            w.db, w.file_b.id, w.user_a.id, organization_id=w.org_a.id
        )
        assert perm is None

        # And the high-level helper 403s too.
        with pytest.raises(Exception) as exc:
            get_file_by_uuid_with_permission(
                w.db, str(w.file_b.uuid), w.user_a.id, organization_id=w.org_a.id
            )
        assert getattr(exc.value, "status_code", None) == 403

    def test_cross_org_shared_file_excluded_from_accessible_subquery(self, two_orgs):
        """The list/gallery subquery must not surface a cross-org shared file."""
        w = two_orgs
        self._share_file_b_into_org_a_user(w)

        subq = PermissionService.get_accessible_file_ids_subquery(
            w.db, w.user_a.id, organization_id=w.org_a.id
        )
        ids = {row[0] for row in w.db.query(subq.c.id).all()}
        assert w.file_a.id in ids  # own org file present
        assert w.file_b.id not in ids  # org-B file gated out

    def test_unscoped_subquery_preserves_legacy_share_visibility(self, two_orgs):
        """Without org context (legacy), the existing share resolution is intact:
        the shared file IS visible (proves the gate is opt-in, not a behavior
        change for community)."""
        w = two_orgs
        self._share_file_b_into_org_a_user(w)
        subq = PermissionService.get_accessible_file_ids_subquery(w.db, w.user_a.id)  # UNSCOPED
        ids = {row[0] for row in w.db.query(subq.c.id).all()}
        assert w.file_b.id in ids


# --------------------------------------------------------------------------- #
# (d) transcript search filter                                                #
# --------------------------------------------------------------------------- #
class TestSearchFilterOrgGate:
    def test_org_context_adds_org_term(self):
        from app.services.search.hybrid_search_service import HybridSearchService

        svc = HybridSearchService()
        filters = svc._build_filters(
            user_id=7,
            speakers=None,
            tags=None,
            date_from=None,
            date_to=None,
            organization_id=42,
        )
        assert {"terms": {"accessible_user_ids": [7]}} in filters
        assert {"term": {"organization_id": 42}} in filters

    def test_personal_scope_excludes_org_docs(self):
        from app.services.search.hybrid_search_service import HybridSearchService

        svc = HybridSearchService()
        filters = svc._build_filters(
            user_id=7,
            speakers=None,
            tags=None,
            date_from=None,
            date_to=None,
            organization_id=None,
        )
        # must_not exists organization_id keeps org docs out of personal search
        assert {"bool": {"must_not": {"exists": {"field": "organization_id"}}}} in filters
        # ...and no org term is present
        assert not any("term" in f and "organization_id" in f.get("term", {}) for f in filters)


# --------------------------------------------------------------------------- #
# (e) speaker / voiceprint kNN filter                                         #
# --------------------------------------------------------------------------- #
class TestSpeakerFilterOrgGate:
    def test_org_filter_clauses_org_context(self):
        from app.services.search.tenant_scope import org_filter_clauses

        assert org_filter_clauses(99) == [{"term": {"organization_id": 99}}]

    def test_org_filter_clauses_personal_excludes_org_docs(self):
        from app.services.search.tenant_scope import org_filter_clauses

        clauses = org_filter_clauses(None)
        assert clauses == [{"bool": {"must_not": {"exists": {"field": "organization_id"}}}}]

    def test_speaker_helper_matches_shared_helper(self):
        """The opensearch_service speaker helper delegates to the shared one so all
        planes encode tenancy identically (org-B voiceprints can't reach org-A)."""
        from app.services.opensearch_service import _speaker_org_filter_clauses
        from app.services.search.tenant_scope import org_filter_clauses

        assert _speaker_org_filter_clauses(5) == org_filter_clauses(5)
        assert _speaker_org_filter_clauses(None) == org_filter_clauses(None)
        # org-A search (org_id=A) yields a term that org-B docs (org_id=B) fail.
        clause = _speaker_org_filter_clauses(1)[0]
        assert clause == {"term": {"organization_id": 1}}
