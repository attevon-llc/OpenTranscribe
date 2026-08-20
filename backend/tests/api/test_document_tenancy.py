"""Document tenancy (lane C0 task 1): ``documents.py`` depended on
``get_current_active_user`` rather than ``get_current_context``, so no document ever
carried an ``organization_id`` and every read scoped by ``user_id`` alone. Two bugs at
once: an org's own documents become unreachable once org gating is enforced elsewhere
(over-restriction), and they stay visible to a personal-scope listing regardless of who
else in the org can see them (leak).

``get_current_context``'s org resolution reads ``request.state.external_identity``,
which only a cloud-edition auth middleware sets — there is no way to establish an org
context through this repo's community-edition HTTP test client. So, matching the
established convention for this exact class of test (``tests/test_org_admin_gdpr.py``'s
``TestRequireOrgAdmin``), the tenant-gating logic is exercised by calling the endpoint
functions directly with a hand-built ``RequestContext`` — and the one thing that DOES
need the real HTTP path (multipart upload) overrides the ``get_current_context``
dependency the same way the ``client`` fixture already overrides ``get_db``.
"""

from __future__ import annotations

import io
import uuid as uuid_pkg

import pytest
from fastapi import HTTPException

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints import documents as documents_ep
from app.main import app
from app.models.document import Document
from app.models.organization import Organization
from app.models.user import User

_HTML_DOC = b"<html><body><h1>Report</h1><p>Org-scoped content.</p></body></html>"


def _mk_user(db_session, label: str, *, role: str = "user") -> User:
    from app.core.security import get_password_hash

    uid = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"tenancy_{label}_{uid}@example.com",
        full_name=f"{label} user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        # role is the sole authorization truth; is_superuser is a derived mirror
        # and `ck_user_superuser_matches_role` enforces it is True ONLY for
        # 'super_admin' — a plain 'admin' has is_superuser=False.
        is_superuser=(role == "super_admin"),
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _mk_org(db_session, label: str) -> Organization:
    uid = uuid_pkg.uuid4().hex[:8]
    org = Organization(external_org_id=f"org_{label}_{uid}", name=f"{label} org", is_active=True)
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


def _mk_document(db_session, owner: User, *, organization_id: int | None) -> Document:
    doc_uuid = uuid_pkg.uuid4()
    doc = Document(
        uuid=doc_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"tenancy-{doc_uuid.hex[:8]}.html",
        storage_path=f"documents/test/{doc_uuid.hex}.html",
        file_size=256,
        content_type="text/html",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


# ---------------------------------------------------------------------------
# _get_owned_document — the tenant gate + the tenant-gated admin bypass
# ---------------------------------------------------------------------------


class TestGetOwnedDocumentTenantGate:
    def test_a_personal_document_is_reachable_from_personal_scope(self, db_session):
        owner = _mk_user(db_session, "owner")
        doc = _mk_document(db_session, owner, organization_id=None)
        ctx = RequestContext(user=owner)  # org_id=None -> personal scope

        found = documents_ep._get_owned_document(db_session, doc.uuid, ctx)
        assert found.id == doc.id

    def test_an_orgs_document_is_invisible_from_personal_scope(self, db_session):
        """The LEAK half: before this fix, every document was fetched by user_id
        alone, so an org member's own personal-scope request could still resolve a
        document that (once org gating exists elsewhere) should only be reachable
        from within the org."""
        owner = _mk_user(db_session, "owner")
        org = _mk_org(db_session, "a")
        doc = _mk_document(db_session, owner, organization_id=org.id)
        ctx = RequestContext(user=owner)  # personal scope, even though owner made it

        with pytest.raises(HTTPException) as exc:
            documents_ep._get_owned_document(db_session, doc.uuid, ctx)
        assert exc.value.status_code == 404

    def test_an_orgs_document_is_invisible_from_a_different_org(self, db_session):
        owner = _mk_user(db_session, "owner")
        org_a = _mk_org(db_session, "a")
        org_b = _mk_org(db_session, "b")
        doc = _mk_document(db_session, owner, organization_id=org_a.id)
        ctx = RequestContext(user=owner, org_id=org_b.id, org_role="org:member")

        with pytest.raises(HTTPException) as exc:
            documents_ep._get_owned_document(db_session, doc.uuid, ctx)
        assert exc.value.status_code == 404

    def test_an_orgs_document_is_reachable_from_its_own_org_by_its_owner(self, db_session):
        """The OVER-RESTRICTION half: before this fix nothing ever stamped
        organization_id, so this exact case (an org member reading their own org's
        document while acting in that org's context) had no way to be represented —
        the row was always personal-scope. This is the fix's positive case."""
        owner = _mk_user(db_session, "owner")
        org = _mk_org(db_session, "a")
        doc = _mk_document(db_session, owner, organization_id=org.id)
        ctx = RequestContext(user=owner, org_id=org.id, org_role="org:member")

        found = documents_ep._get_owned_document(db_session, doc.uuid, ctx)
        assert found.id == doc.id

    def test_an_admin_in_personal_scope_cannot_reach_another_orgs_document(self, db_session):
        """The tenant-gated admin bypass: a GLOBAL admin acting with no org context
        must not reach an org's document just because they are an admin —
        `get_file_by_uuid_with_permission`'s unconditional admin bypass is
        deliberately NOT what documents inherit (see the docstring on
        `_get_owned_document`)."""
        owner = _mk_user(db_session, "owner")
        admin = _mk_user(db_session, "admin", role="admin")
        org = _mk_org(db_session, "a")
        doc = _mk_document(db_session, owner, organization_id=org.id)
        ctx = RequestContext(user=admin)  # admin, but personal scope

        with pytest.raises(HTTPException) as exc:
            documents_ep._get_owned_document(db_session, doc.uuid, ctx)
        assert exc.value.status_code == 404

    def test_an_admin_within_the_documents_own_org_context_can_reach_it(self, db_session):
        """The admin bypass DOES still work — scoped to the tenant the admin is
        currently acting within, not global."""
        owner = _mk_user(db_session, "owner")
        admin = _mk_user(db_session, "admin", role="admin")
        org = _mk_org(db_session, "a")
        doc = _mk_document(db_session, owner, organization_id=org.id)
        ctx = RequestContext(user=admin, org_id=org.id, org_role="org:admin")

        found = documents_ep._get_owned_document(db_session, doc.uuid, ctx)
        assert found.id == doc.id

    def test_an_admin_in_personal_scope_can_still_reach_a_personal_scope_strangers_document(
        self, db_session
    ):
        """Community-edition invariance: with no orgs involved at all, the existing
        (pre-fix) admin-sees-everyone-personal behaviour is unchanged."""
        owner = _mk_user(db_session, "owner")
        admin = _mk_user(db_session, "admin", role="admin")
        doc = _mk_document(db_session, owner, organization_id=None)
        ctx = RequestContext(user=admin)

        found = documents_ep._get_owned_document(db_session, doc.uuid, ctx)
        assert found.id == doc.id


# ---------------------------------------------------------------------------
# list_documents — scope_to_context, not a raw user_id filter
# ---------------------------------------------------------------------------


class TestListDocumentsScoping:
    @pytest.mark.asyncio
    async def test_an_org_context_lists_only_that_orgs_documents(self, db_session):
        owner = _mk_user(db_session, "owner")
        org_a = _mk_org(db_session, "a")
        org_b = _mk_org(db_session, "b")
        _mk_document(db_session, owner, organization_id=org_a.id)
        _mk_document(db_session, owner, organization_id=org_b.id)
        _mk_document(db_session, owner, organization_id=None)  # personal, must be excluded

        ctx = RequestContext(user=owner, org_id=org_a.id, org_role="org:member")
        # skip/limit are `Query(...)` FastAPI defaults — only resolved to plain ints
        # by the ASGI dependency-injection layer, so a direct call must supply real
        # values or `.offset()`/`.limit()` receive the `Query` sentinel object itself.
        result = await documents_ep.list_documents(skip=0, limit=50, ctx=ctx, db=db_session)

        assert result.total == 1

    @pytest.mark.asyncio
    async def test_personal_scope_excludes_every_org_document(self, db_session):
        owner = _mk_user(db_session, "owner")
        org = _mk_org(db_session, "a")
        _mk_document(db_session, owner, organization_id=org.id)
        personal = _mk_document(db_session, owner, organization_id=None)

        ctx = RequestContext(user=owner)
        result = await documents_ep.list_documents(skip=0, limit=50, ctx=ctx, db=db_session)

        assert result.total == 1
        assert result.documents[0].uuid == personal.uuid


# ---------------------------------------------------------------------------
# upload_document — the organization_id stamp, through the real HTTP path with
# get_current_context overridden (multipart encoding is not worth hand-rolling).
# ---------------------------------------------------------------------------


class TestUploadStampsTheActiveOrg:
    def test_upload_in_org_context_stamps_organization_id(self, client, db_session):
        owner = _mk_user(db_session, "owner")
        org = _mk_org(db_session, "a")
        # Captured as plain ints BEFORE the request: `upload_document` itself calls
        # `db.close()` mid-handler (by design — see its own comment on releasing the
        # connection before the MinIO PUT), and the `client` fixture's `get_db`
        # override hands out this SAME session, so `org`/`owner` are expired and
        # detached by the time the request returns. Touching `org.id` again after
        # that point raises `DetachedInstanceError`, not a production bug.
        org_id, owner_id = org.id, owner.id
        ctx = RequestContext(user=owner, org_id=org_id, org_role="org:member")

        app.dependency_overrides[get_current_context] = lambda: ctx
        try:
            response = client.post(
                "/api/documents",
                files={"file": ("org.html", io.BytesIO(_HTML_DOC), "text/html")},
            )
        finally:
            app.dependency_overrides.pop(get_current_context, None)

        assert response.status_code == 201, response.text
        doc_uuid = response.json()["uuid"]
        row = db_session.query(Document).filter(Document.uuid == doc_uuid).one()
        assert row.organization_id == org_id
        assert row.user_id == owner_id

    def test_upload_in_personal_scope_stamps_no_organization(self, client, db_session):
        owner = _mk_user(db_session, "owner")
        ctx = RequestContext(user=owner)  # no org

        app.dependency_overrides[get_current_context] = lambda: ctx
        try:
            response = client.post(
                "/api/documents",
                files={"file": ("personal.html", io.BytesIO(_HTML_DOC), "text/html")},
            )
        finally:
            app.dependency_overrides.pop(get_current_context, None)

        assert response.status_code == 201, response.text
        doc_uuid = response.json()["uuid"]
        row = db_session.query(Document).filter(Document.uuid == doc_uuid).one()
        assert row.organization_id is None
