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
    org = Organization(external_org_id=f"org_{label}_{uid}", name=f"{label} Org", is_active=True)
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


# --------------------------------------------------------------------------- #
# (f) API read surfaces — org-stamped file under personal scope                #
# --------------------------------------------------------------------------- #
def _login(client, user, password: str = "password123") -> dict:
    """Local-auth login: the token carries no org claim, so resolve_org_context
    yields PERSONAL scope — exactly the removed-org-member situation."""
    resp = client.post(
        "/api/auth/token",
        data={"username": user.email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, f"Login failed for {user.email}: {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestApiReadSurfacesPersonalScope:
    """Scenario (a): the owner of an org-stamped file whose org membership is
    gone (personal scope) must lose access on the secondary read surfaces too —
    the ownership fast-path must not bypass the tenant gate on /subtitles,
    /stream-url, or /segments."""

    def test_org_stamped_file_blocked_on_read_surfaces(self, client, two_orgs):
        w = two_orgs
        headers = _login(client, w.user_a)
        fid = str(w.file_a.uuid)  # org-A-stamped, owned by user_a

        assert client.get(f"/api/files/{fid}/subtitles", headers=headers).status_code == 403
        assert client.get(f"/api/files/{fid}/stream-url", headers=headers).status_code == 403
        assert client.get(f"/api/files/{fid}/segments", headers=headers).status_code == 403

    def test_personal_file_read_surfaces_unaffected(self, client, two_orgs):
        """Community invariance: an org-less file is NOT blocked by the gate
        (the request may fail later for content reasons, but never with the
        tenant-gate 403)."""
        w = two_orgs
        personal = _mk_file(w.db, user=w.user_a, org_id=None)
        headers = _login(client, w.user_a)
        pid = str(personal.uuid)

        assert client.get(f"/api/files/{pid}/subtitles", headers=headers).status_code != 403
        assert client.get(f"/api/files/{pid}/stream-url", headers=headers).status_code != 403
        assert client.get(f"/api/files/{pid}/segments", headers=headers).status_code != 403


# --------------------------------------------------------------------------- #
# (g) cross-org collection shares                                              #
# --------------------------------------------------------------------------- #
def _mk_collection(db, *, user: User, org_id: int | None) -> Collection:
    coll = Collection(
        uuid=uuid_pkg.uuid4(),
        name=f"coll_{uuid_pkg.uuid4().hex[:8]}",
        user_id=user.id,
        organization_id=org_id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


class TestCrossOrgCollectionShareApi:
    """Scenario (b): an org-scoped collection may only be shared with active
    members of that organization; personal collections keep existing behavior.

    Since #262d the share endpoints are ALSO tenant-gated, so an org-stamped
    collection must be exercised in matching org context (dependency override
    below) — a personal-scope request 403s before the membership gate.
    """

    def _share(self, client, headers, coll, target_user, permission="viewer", org_ctx=None):
        from app.api.deps_context import RequestContext
        from app.api.deps_context import get_current_context
        from app.main import app

        if org_ctx is not None:
            user, org_id = org_ctx
            app.dependency_overrides[get_current_context] = lambda: RequestContext(
                user=user, org_id=org_id, org_role="org:member"
            )
        try:
            return client.post(
                f"/api/collections/{coll.uuid}/shares",
                headers=headers,
                json={
                    "target_type": "user",
                    "target_uuid": str(target_user.uuid),
                    "permission": permission,
                },
            )
        finally:
            if org_ctx is not None:
                app.dependency_overrides.pop(get_current_context, None)

    def test_org_collection_share_to_non_member_rejected(self, client, two_orgs):
        w = two_orgs
        coll = _mk_collection(w.db, user=w.user_b, org_id=w.org_b.id)
        headers = _login(client, w.user_b)

        resp = self._share(
            client, headers, coll, w.user_a, org_ctx=(w.user_b, w.org_b.id)
        )  # user_a is org-A only
        assert resp.status_code == 403
        assert "not a member" in resp.json()["detail"]

    def test_org_collection_share_to_member_allowed(self, client, two_orgs):
        w = two_orgs
        member = _mk_user(w.db, "carol")
        w.db.add(
            OrganizationMembership(organization_id=w.org_b.id, user_id=member.id, role="org:member")
        )
        w.db.commit()
        coll = _mk_collection(w.db, user=w.user_b, org_id=w.org_b.id)
        headers = _login(client, w.user_b)

        resp = self._share(client, headers, coll, member, org_ctx=(w.user_b, w.org_b.id))
        assert resp.status_code == 201, resp.text

    def test_personal_collection_share_unaffected(self, client, two_orgs):
        """org NULL keeps existing behavior: shareable with any user."""
        w = two_orgs
        coll = _mk_collection(w.db, user=w.user_b, org_id=None)
        headers = _login(client, w.user_b)

        resp = self._share(client, headers, coll, w.user_a)
        assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------- #
# (#262d) remaining collection sub-surfaces are tenant-gated                    #
# --------------------------------------------------------------------------- #
class TestCollectionSubSurfacesTenantGate:
    """Issue #262d: collection GET/update/delete, share list/create, and
    collection-media now thread ``ctx.org_id`` — an org-stamped collection is
    unreachable from PERSONAL scope even for its owner, and community
    (org-less) collections are unaffected."""

    def test_org_collection_sub_surfaces_blocked_in_personal_scope(self, client, two_orgs):
        w = two_orgs
        coll = _mk_collection(w.db, user=w.user_a, org_id=w.org_a.id)
        headers = _login(client, w.user_a)  # local token -> personal scope
        base = f"/api/collections/{coll.uuid}"

        assert client.get(base, headers=headers).status_code == 403
        assert client.put(base, headers=headers, json={"name": "renamed"}).status_code == 403
        assert client.get(f"{base}/media", headers=headers).status_code == 403
        assert client.get(f"{base}/shares", headers=headers).status_code == 403
        assert (
            client.post(
                f"{base}/shares",
                headers=headers,
                json={
                    "target_type": "user",
                    "target_uuid": str(w.user_b.uuid),
                    "permission": "viewer",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"{base}/media",
                headers=headers,
                json={"media_file_ids": [str(w.file_a.uuid)]},
            ).status_code
            == 403
        )
        assert (
            client.request(
                "DELETE",
                f"{base}/media",
                headers=headers,
                json={"media_file_ids": [str(w.file_a.uuid)]},
            ).status_code
            == 403
        )
        assert client.delete(base, headers=headers).status_code == 403
        # Share update/delete resolve the collection first — same gate.
        share_uuid = uuid_pkg.uuid4()
        assert (
            client.put(
                f"{base}/shares/{share_uuid}",
                headers=headers,
                json={"permission": "viewer"},
            ).status_code
            == 403
        )
        assert client.delete(f"{base}/shares/{share_uuid}", headers=headers).status_code == 403

    def test_org_collection_reachable_in_matching_org_context(self, client, two_orgs):
        from app.api.deps_context import RequestContext
        from app.api.deps_context import get_current_context
        from app.main import app

        w = two_orgs
        coll = _mk_collection(w.db, user=w.user_a, org_id=w.org_a.id)
        headers = _login(client, w.user_a)

        app.dependency_overrides[get_current_context] = lambda: RequestContext(
            user=w.user_a, org_id=w.org_a.id, org_role="org:member"
        )
        try:
            resp = client.get(f"/api/collections/{coll.uuid}", headers=headers)
        finally:
            app.dependency_overrides.pop(get_current_context, None)
        assert resp.status_code == 200

    def test_personal_collection_sub_surfaces_unaffected(self, client, two_orgs):
        """Community invariance: org-less collections behave exactly as before."""
        w = two_orgs
        coll = _mk_collection(w.db, user=w.user_a, org_id=None)
        headers = _login(client, w.user_a)

        assert client.get(f"/api/collections/{coll.uuid}", headers=headers).status_code == 200
        assert (
            client.put(
                f"/api/collections/{coll.uuid}", headers=headers, json={"description": "d"}
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/collections/{coll.uuid}/shares", headers=headers).status_code == 200
        )

    def test_add_media_rejects_cross_scope_file(self, client, two_orgs):
        """Under org context, a PERSONAL file can't be pulled into an org
        collection (it would leak via the collection detail)."""
        from app.api.deps_context import RequestContext
        from app.api.deps_context import get_current_context
        from app.main import app

        w = two_orgs
        coll = _mk_collection(w.db, user=w.user_a, org_id=w.org_a.id)
        personal_file = _mk_file(w.db, user=w.user_a, org_id=None)
        headers = _login(client, w.user_a)

        app.dependency_overrides[get_current_context] = lambda: RequestContext(
            user=w.user_a, org_id=w.org_a.id, org_role="org:member"
        )
        try:
            denied = client.post(
                f"/api/collections/{coll.uuid}/media",
                headers=headers,
                json={"media_file_ids": [str(personal_file.uuid)]},
            )
            allowed = client.post(
                f"/api/collections/{coll.uuid}/media",
                headers=headers,
                json={"media_file_ids": [str(w.file_a.uuid)]},  # org-A file
            )
        finally:
            app.dependency_overrides.pop(get_current_context, None)

        assert denied.status_code == 404  # not found in the active tenant scope
        assert allowed.status_code == 200
        assert allowed.json()["added"] == 1


class TestGroupShareOrgMembership:
    """Issue #262d: groups can span orgs — a group-targeted share of an
    org-stamped collection requires EVERY group member to be in that org."""

    def _mk_group(self, db, owner, members):
        from app.models.group import UserGroup
        from app.models.group import UserGroupMember

        group = UserGroup(name=f"grp_{uuid_pkg.uuid4().hex[:8]}", owner_id=owner.id)
        db.add(group)
        db.commit()
        db.refresh(group)
        for member in members:
            db.add(UserGroupMember(group_id=group.id, user_id=member.id, role="member"))
        db.commit()
        return group

    def _share_group(self, client, headers, coll, group):
        return client.post(
            f"/api/collections/{coll.uuid}/shares",
            headers=headers,
            json={
                "target_type": "group",
                "target_uuid": str(group.uuid),
                "permission": "viewer",
            },
        )

    def test_group_with_outside_member_rejected(self, client, two_orgs):
        w = two_orgs
        coll = _mk_collection(w.db, user=w.user_b, org_id=w.org_b.id)
        # Group spans orgs: user_b (org B) + user_a (org A only).
        group = self._mk_group(w.db, w.user_b, [w.user_b, w.user_a])
        headers = _login(client, w.user_b)

        # Simulate an org-B request so the tenant gate resolves the collection.
        from app.api.deps_context import RequestContext
        from app.api.deps_context import get_current_context
        from app.main import app

        app.dependency_overrides[get_current_context] = lambda: RequestContext(
            user=w.user_b, org_id=w.org_b.id, org_role="org:member"
        )
        try:
            resp = self._share_group(client, headers, coll, group)
        finally:
            app.dependency_overrides.pop(get_current_context, None)

        assert resp.status_code == 403
        assert "not members of that organization" in resp.json()["detail"]

    def test_group_fully_inside_org_allowed(self, client, two_orgs):
        w = two_orgs
        carol = _mk_user(w.db, "carol")
        w.db.add(
            OrganizationMembership(organization_id=w.org_b.id, user_id=carol.id, role="org:member")
        )
        w.db.commit()
        coll = _mk_collection(w.db, user=w.user_b, org_id=w.org_b.id)
        group = self._mk_group(w.db, w.user_b, [w.user_b, carol])
        headers = _login(client, w.user_b)

        from app.api.deps_context import RequestContext
        from app.api.deps_context import get_current_context
        from app.main import app

        app.dependency_overrides[get_current_context] = lambda: RequestContext(
            user=w.user_b, org_id=w.org_b.id, org_role="org:member"
        )
        try:
            resp = self._share_group(client, headers, coll, group)
        finally:
            app.dependency_overrides.pop(get_current_context, None)

        assert resp.status_code == 201, resp.text

    def test_personal_collection_group_share_unaffected(self, client, two_orgs):
        """Community invariance: org-less collections keep pre-existing group
        sharing behavior (any group the sharer belongs to)."""
        w = two_orgs
        coll = _mk_collection(w.db, user=w.user_b, org_id=None)
        group = self._mk_group(w.db, w.user_b, [w.user_b, w.user_a])
        headers = _login(client, w.user_b)

        resp = self._share_group(client, headers, coll, group)
        assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------- #
# (#262e) profile rows are org-stamped at API creation                          #
# --------------------------------------------------------------------------- #
class TestProfileOrgStamping:
    def test_create_profile_api_stamps_request_org(self, client, two_orgs):
        from app.api.deps_context import RequestContext
        from app.api.deps_context import get_current_context
        from app.main import app
        from app.models.media import SpeakerProfile

        w = two_orgs
        headers = _login(client, w.user_a)
        name = f"profile_{uuid_pkg.uuid4().hex[:8]}"

        app.dependency_overrides[get_current_context] = lambda: RequestContext(
            user=w.user_a, org_id=w.org_a.id, org_role="org:member"
        )
        try:
            resp = client.post(
                "/api/speaker-profiles/profiles", headers=headers, params={"name": name}
            )
        finally:
            app.dependency_overrides.pop(get_current_context, None)

        assert resp.status_code == 200, resp.text
        row = w.db.query(SpeakerProfile).filter(SpeakerProfile.uuid == resp.json()["uuid"]).first()
        assert row is not None
        assert row.organization_id == w.org_a.id

    def test_create_profile_api_personal_scope_unstamped(self, client, two_orgs):
        from app.models.media import SpeakerProfile

        w = two_orgs
        headers = _login(client, w.user_a)  # personal scope
        name = f"profile_{uuid_pkg.uuid4().hex[:8]}"

        resp = client.post("/api/speaker-profiles/profiles", headers=headers, params={"name": name})
        assert resp.status_code == 200, resp.text
        row = w.db.query(SpeakerProfile).filter(SpeakerProfile.uuid == resp.json()["uuid"]).first()
        assert row is not None
        assert row.organization_id is None

    def test_speakers_helper_creates_profile_with_speaker_org(self, two_orgs):
        """speakers.py create-new-profile paths inherit the SPEAKER's tenant."""
        from app.api.endpoints.speakers import _handle_create_new_profile_action
        from app.models.media import SpeakerProfile

        w = two_orgs
        _handle_create_new_profile_action(w.speaker_a, "Org Speaker", w.user_a, w.db)
        w.db.commit()

        profile = (
            w.db.query(SpeakerProfile).filter(SpeakerProfile.id == w.speaker_a.profile_id).first()
        )
        assert profile is not None
        assert profile.organization_id == w.org_a.id


# --------------------------------------------------------------------------- #
# (h) list_collections tenant scoping                                          #
# --------------------------------------------------------------------------- #
class TestListCollectionsTenantScope:
    """Scenario (c): the same-user collections listing is org-gated — personal
    scope hides org-stamped collections and org scope hides personal ones."""

    def test_personal_listing_excludes_org_collections(self, client, two_orgs):
        w = two_orgs
        org_coll = _mk_collection(w.db, user=w.user_a, org_id=w.org_a.id)
        personal_coll = _mk_collection(w.db, user=w.user_a, org_id=None)
        headers = _login(client, w.user_a)  # local token -> personal scope

        resp = client.get("/api/collections?ownership=mine", headers=headers)
        assert resp.status_code == 200
        names = {c["name"] for c in resp.json()}
        assert personal_coll.name in names
        assert org_coll.name not in names

    def test_org_listing_excludes_personal_collections(self, client, two_orgs):
        from app.api.deps_context import RequestContext
        from app.api.deps_context import get_current_context
        from app.main import app

        w = two_orgs
        org_coll = _mk_collection(w.db, user=w.user_a, org_id=w.org_a.id)
        personal_coll = _mk_collection(w.db, user=w.user_a, org_id=None)
        headers = _login(client, w.user_a)

        # Simulate an org-context request (external-IdP token) by overriding the
        # tenant-context dependency — auth itself still runs via the real token.
        app.dependency_overrides[get_current_context] = lambda: RequestContext(
            user=w.user_a, org_id=w.org_a.id, org_role="org:member"
        )
        try:
            resp = client.get("/api/collections?ownership=mine", headers=headers)
        finally:
            app.dependency_overrides.pop(get_current_context, None)

        assert resp.status_code == 200
        names = {c["name"] for c in resp.json()}
        assert org_coll.name in names
        assert personal_coll.name not in names
