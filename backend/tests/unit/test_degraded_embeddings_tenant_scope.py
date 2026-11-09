"""The degraded-embeddings listing never names another tenant's file in a multi-tenant deployment."""

from __future__ import annotations

from contextlib import contextmanager

from app.api.endpoints import search_reindex
from app.services.platform_bypass import PlatformBypass
from app.services.request_context import RequestContext
from app.services.search.embedding_provenance import DegradedFile
from app.services.search_reindex_queries import titles_for_uuids
from tests.fixtures.tag_world import make_file
from tests.fixtures.tag_world import make_org
from tests.fixtures.tag_world import make_user


def test_scope_wide_title_lookup_stays_inside_the_active_organization(db_session):
    # owner_user_id=None is what a scope-wide caller passes: the owner narrowing is off, so the
    # tenant predicate alone must keep the other organization's file out.
    org_a, org_b = make_org(db_session, "a"), make_org(db_session, "b")
    owner_a, owner_b = make_user(db_session, "ownerA"), make_user(db_session, "ownerB")
    mine = make_file(db_session, owner_a, org_a.id)
    foreign = make_file(db_session, owner_b, org_b.id)
    db_session.commit()

    titles = titles_for_uuids(
        db_session,
        [str(mine.uuid), str(foreign.uuid)],
        organization_id=org_a.id,
        owner_user_id=None,
        tenant_scoped=True,
    )

    assert set(titles) == {str(mine.uuid)}


def test_org_admin_route_lists_only_their_own_organizations_degraded_files(db_session, monkeypatch):
    org_a, org_b = make_org(db_session, "a"), make_org(db_session, "b")
    admin_a, owner_b = make_user(db_session, "adminA"), make_user(db_session, "ownerB")
    mine = make_file(db_session, admin_a, org_a.id)
    foreign = make_file(db_session, owner_b, org_b.id)
    db_session.commit()

    surveyed = [
        DegradedFile(admin_a.id, str(mine.uuid)),
        DegradedFile(owner_b.id, str(foreign.uuid)),
    ]
    monkeypatch.setattr(
        "app.services.search.embedding_provenance.survey_degraded_files",
        lambda limit: (surveyed, False),
    )

    @contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)
    ctx = RequestContext(
        user=admin_a, org_id=org_a.id, org_role="org:admin", bypass=PlatformBypass()
    )

    body = search_reindex.get_degraded_embeddings(limit=500, ctx=ctx, current_user=admin_a)

    assert [f["file_uuid"] for f in body["files"]] == [str(mine.uuid)]
    assert str(foreign.uuid) not in str(body)
