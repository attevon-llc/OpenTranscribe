"""An organization collection is a file-read route for the members who may see it (#1275, D-3).

Before this, ``GET /collections/{uuid}/media`` listed a colleague's file to every member while
the file page answered that same member 403, the gallery's ``shared``/``all`` listings left the
file out, and search/chat retrieval never returned its excerpts. One rule now decides all of
them (``services/collection_visibility``): a member of an organization collection they may see
reads the organization's files in it. These tests hold the surfaces to the same answer and keep
the controls closed: a file outside any collection, and a person outside the organization.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.services.permission_service import PermissionService


@pytest.fixture()
def org_files(gov):
    """alice's file inside an org collection, and another of her files outside any."""
    in_collection = gov.file(gov.alice)
    loose = gov.file(gov.alice)
    collection = gov.collection(gov.admin)
    gov.put(collection, in_collection)
    return type(
        "Files", (), {"in_collection": in_collection, "loose": loose, "collection": collection}
    )


def test_member_opens_a_colleagues_file_through_the_org_collection(client, gov, org_files):
    """The chokepoint every file route shares answered 403 for exactly this reader."""
    with gov.acting_as(gov.bob):
        opened = client.get(f"/api/files/{org_files.in_collection.uuid}")
        assert opened.status_code == 200, opened.text
        assert client.get(f"/api/files/{org_files.loose.uuid}").status_code == 404


def test_a_person_outside_the_organization_cannot_open_it(client, gov, org_files):
    with gov.acting_as(gov.outsider):
        assert client.get(f"/api/files/{org_files.in_collection.uuid}").status_code == 404


def test_the_collection_route_grants_read_not_write(client, gov, org_files):
    """Seeing a file through a collection is not ownership: it never reads as editor."""
    assert (
        PermissionService.get_file_permission(gov.db, org_files.in_collection.id, gov.bob.id)
        == "viewer"
    )
    with gov.acting_as(gov.bob):
        renamed = client.put(f"/api/files/{org_files.in_collection.uuid}", json={"title": "mine"})
        assert renamed.status_code == 403, renamed.text


@pytest.mark.parametrize("ownership", ["shared", "all"])
def test_the_gallery_lists_the_collections_files(client, gov, org_files, ownership):
    with gov.acting_as(gov.bob):
        listed = client.get("/api/files", params={"ownership": ownership})
        assert listed.status_code == 200, listed.text
        uuids = (
            {row["uuid"] for row in listed.json()["items"]}
            if isinstance(listed.json(), dict)
            else {row["uuid"] for row in listed.json()}
        )
    assert str(org_files.in_collection.uuid) in uuids
    assert str(org_files.loose.uuid) not in uuids


def test_the_route_is_tenant_gated(gov, org_files):
    from app.services.collection_visibility import file_readable_via_org_collection
    from app.services.collection_visibility import org_collection_file_ids_stmt

    """Personal scope never lists organization files; another organization's scope neither."""
    for scope in (None, gov.other_org.id):
        stmt = org_collection_file_ids_stmt(gov.bob.id, scope)
        assert org_files.in_collection.id not in set(gov.db.execute(stmt).scalars())
    stmt = org_collection_file_ids_stmt(gov.bob.id, gov.org.id)
    assert org_files.in_collection.id in set(gov.db.execute(stmt).scalars())
    assert not file_readable_via_org_collection(gov.db, org_files.in_collection.id, gov.outsider.id)


def test_a_file_that_is_not_the_organizations_is_not_exposed(gov):
    """A stray membership row never carries another tenant's file across the boundary."""
    from app.services.collection_visibility import file_readable_via_org_collection

    foreign = gov.file(gov.alice, org=gov.other_org)
    collection = gov.collection(gov.admin)
    gov.put(collection, foreign)
    assert not file_readable_via_org_collection(gov.db, foreign.id, gov.bob.id)


def test_the_subquery_every_listing_uses_carries_the_route(gov, org_files):
    subquery = PermissionService.get_accessible_file_ids_subquery(
        gov.db, gov.bob.id, organization_id=gov.org.id
    )
    ids = set(gov.db.execute(select(subquery.c.id)).scalars())
    assert org_files.in_collection.id in ids
    assert org_files.loose.id not in ids


@pytest.fixture()
def bridged_session(monkeypatch, gov):
    """``session_scope`` opens its own connection, which cannot see the savepointed rows."""
    import contextlib

    @contextlib.contextmanager
    def _scope():
        yield gov.db

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)


def test_search_clause_adds_the_collection_arm_for_a_member_only(gov, org_files, bridged_session):
    from app.services.search.access_filter import access_filter_clause

    by_user = {"terms": {"accessible_user_ids": [gov.bob.id]}}
    assert access_filter_clause(gov.bob.id, gov.org.id) == {
        "bool": {
            "should": [by_user, {"terms": {"collection_ids": [org_files.collection.id]}}],
            "minimum_should_match": 1,
        }
    }
    # Personal scope and a non-member get today's per-file term and nothing wider.
    assert access_filter_clause(gov.bob.id, None) == by_user
    outsider = {"terms": {"accessible_user_ids": [gov.outsider.id]}}
    assert access_filter_clause(gov.outsider.id, gov.org.id) == outsider


def test_search_clause_drops_the_arm_above_the_term_cap(
    gov, org_files, bridged_session, monkeypatch, caplog
):
    from app.services.search import access_filter

    monkeypatch.setattr(access_filter, "MAX_VISIBLE_COLLECTION_TERMS", 0)
    with caplog.at_level("WARNING", logger=access_filter.logger.name):
        clause = access_filter.access_filter_clause(gov.bob.id, gov.org.id)
    assert clause == {"terms": {"accessible_user_ids": [gov.bob.id]}}
    assert "falls back to per-file access" in caplog.text


def test_hybrid_filters_use_the_same_clause(gov, org_files, bridged_session):
    from app.services.search.hybrid_search_service import HybridSearchService

    filters = HybridSearchService()._build_filters(
        user_id=gov.bob.id,
        speakers=None,
        tags=None,
        date_from=None,
        date_to=None,
        organization_id=gov.org.id,
    )
    arm = {"terms": {"collection_ids": [org_files.collection.id]}}
    assert any("bool" in f and arm in f["bool"].get("should", []) for f in filters)


class _RecordingOpenSearch:
    def __init__(self):
        self.bodies: list[dict] = []

    def update_by_query(self, **kwargs):
        self.bodies.append(kwargs["body"])
        return {"updated": 1}


def test_the_access_rewrite_refreshes_collection_ids(gov, org_files, bridged_session, monkeypatch):
    """Adding a file to a collection must reach the chunks' ``collection_ids``, the arm's key."""
    from app.tasks.search_indexing_task import update_file_access_index

    fake = _RecordingOpenSearch()
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: fake)
    monkeypatch.setattr("app.services.chat.retrieval_cache.bump_corpus_version", lambda: None)

    result = update_file_access_index.run([org_files.in_collection.id])

    assert result["errors"] == 0
    (body,) = fake.bodies
    assert body["script"]["params"]["cids"] == [org_files.collection.id]
    assert "ctx._source.collection_ids = params.cids" in body["script"]["source"]


@pytest.mark.parametrize(("org", "dispatches"), [(True, True), (False, False)])
def test_collection_membership_changes_reindex_org_collections_without_shares(
    client, gov, monkeypatch, org, dispatches
):
    """An org collection is a read route even with no share row; a private personal one is not."""
    from app.api.endpoints import media_collections

    sent: list[list[int]] = []
    monkeypatch.setattr(
        media_collections.update_file_access_index, "delay", lambda ids: sent.append(list(ids))
    )
    collection = gov.collection(gov.alice, org=gov.org if org else None)
    media_file = gov.file(gov.alice, org=gov.org if org else None)

    with gov.acting_as(gov.alice, org=gov.org if org else None):
        added = client.post(
            f"/api/collections/{collection.uuid}/media",
            json={"media_file_ids": [str(media_file.uuid)]},
        )
        assert added.status_code == 200, added.text
        removed = client.request(
            "DELETE",
            f"/api/collections/{collection.uuid}/media",
            json={"media_file_ids": [str(media_file.uuid)]},
        )
        assert removed.status_code == 200, removed.text

    assert sent == ([[media_file.id], [media_file.id]] if dispatches else [])
