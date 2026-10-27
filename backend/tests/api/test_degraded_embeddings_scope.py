"""The degraded-embeddings listing names only the active tenant's files, whatever widens it.

``GET /api/search/degraded-embeddings`` drops its per-owner filter where the request's bypass
sees the whole scope (an organization grant), so the organization predicate on the file is the
only thing keeping that list inside the tenant; where the owner filter stays, the predicate is
what keeps a caller's own files from other tenants out of the personal workspace's list. These
tests pin it from both directions:

* a bypass that sees the whole of an organization lists exactly that organization's files and
  counts every other tenant's as outside the scope, without naming one;
* a platform admin in the personal workspace is not shown their own organization-stamped files
  (the owner filter alone would admit them).
"""

from __future__ import annotations

from unittest.mock import patch

from fixtures.support_world import make_file

DEGRADED = "/api/search/degraded-embeddings"


def _survey(*files):
    from app.services.search.embedding_provenance import DegradedFile

    return patch(
        "app.services.search.embedding_provenance.survey_degraded_files",
        return_value=(
            [DegradedFile(user_id=f.user_id, file_uuid=str(f.uuid)) for f in files],
            False,
        ),
    )


def _call_with_org_grant(w, surveyed):
    """The handler called directly with the bypass an organization grant builds.

    The route itself refuses a grant (see the next test), so the listing's widening branch is
    unreachable over HTTP today. It is still code, and it is what would run the day that
    refusal is lifted, so its tenant predicate is pinned here rather than left to chance.
    """
    from app.api.deps_context import RequestContext
    from app.api.endpoints.search import get_degraded_embeddings
    from app.services.platform_bypass import build_bypass
    from app.services.support_access_service import resolve_active_grant

    view = resolve_active_grant(w.db, w.active_grant(), w.admin)
    bypass = build_bypass(w.db, w.admin, view.organization_id, grant=view)
    assert bypass.sees_all_in_scope, "the precondition: this bypass widens the list"
    ctx = RequestContext(user=w.admin, org_id=view.organization_id, org_role=None, bypass=bypass)
    with _survey(*surveyed):
        body: dict = get_degraded_embeddings(limit=500, ctx=ctx, current_user=w.admin)
    return body


def test_an_org_scope_that_sees_everything_lists_only_that_organizations_files(support_world):
    w = support_world
    body = _call_with_org_grant(w, (w.file, w.other_org_file, w.personal_file))

    assert [f["file_uuid"] for f in body["files"]] == [str(w.file.uuid)], "the granted tenant"
    assert body["files"][0]["title"] == w.file.filename
    assert body["total_files"] == 3
    assert body["outside_scope_files"] == 2
    assert body["affected_users"] == 1
    named = str(body["files"])
    for foreign in (w.other_org_file, w.personal_file):
        assert foreign.filename not in named
        assert str(foreign.uuid) not in named


def test_the_route_refuses_a_grant(support_world):
    w = support_world
    with _survey(w.file):
        response = w.client.get(DEGRADED, headers=w.with_grant(w.active_grant()))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "support_grant_action_not_permitted"


def test_without_a_grant_the_same_survey_names_nothing_of_the_organization(support_world):
    """The control for the first test: the bypass is what widens the list, not the survey."""
    w = support_world
    with _survey(w.file, w.other_org_file, w.personal_file):
        response = w.client.get(DEGRADED, headers=w.admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"] == []
    assert body["outside_scope_files"] == 3
    assert w.file.filename not in response.text


def test_a_personal_workspace_admin_is_not_shown_their_own_organization_files(support_world):
    w = support_world
    in_org = make_file(w.db, w.admin, w.org.id)
    in_other_org = make_file(w.db, w.admin, w.other_org.id)
    personal = make_file(w.db, w.admin, None)
    with _survey(in_org, in_other_org, personal, w.personal_file):
        response = w.client.get(DEGRADED, headers=w.admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert [f["file_uuid"] for f in body["files"]] == [str(personal.uuid)]
    assert body["total_files"] == 4
    assert body["outside_scope_files"] == 3
    for stamped in (in_org, in_other_org, w.personal_file):
        assert stamped.filename not in response.text
