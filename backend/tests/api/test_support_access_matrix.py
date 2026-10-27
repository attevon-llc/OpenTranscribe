"""Platform staff reach a tenant only through an approved, live grant (issue #1281).

``docs/security/platform-access-matrix.tsv`` lists every path; this file proves the claim the
matrix makes, by driving the real routes:

* ``test_every_chokepoint_demands_a_valid_grant``: for one representative route per chokepoint
  kind (file, collection, speaker, speaker profile, comment, watch source, task, thumbnail) a
  platform admin is refused with no grant, a pending, an expired or a revoked one, and with a
  valid grant for a DIFFERENT tenant, for the personal workspace instead of the organization, or
  holding a personal grant instead of an organization one; with a valid grant for the right
  tenant the same request is served and a use row naming the resource is written.
* ``test_a_platform_admin_without_a_grant_sees_no_tenant_content``: a sweep. Every parameterless
  GET a platform role can call returns none of three tenants' seeded content to an admin or a
  super_admin holding no grant. A route that cannot be called (no Redis here, say) is counted and
  limited, never silently dropped.
* ``test_cross_tenant_filename_listings_are_audited``: platform listings that name other tenants'
  files leave one audit event.
* ``test_an_org_grant_widens_lists_only_inside_its_tenant``: an organization grant sees its own
  tenant's rows through the list routes and nothing from another tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import Comment
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.media import Task
from app.models.support_access import SupportAccessUse
from app.models.watch_source import WatchSource

# ---------------------------------------------------------------------------
# A world with the same set of resources in three tenants
# ---------------------------------------------------------------------------

TENANTS = ("org", "other", "personal")


@dataclass
class Resource:
    """One chokepoint kind's representative route, in one tenant."""

    path: str
    resource_uuid: str | None  # the uuid a use row must name once the guard passed
    canary: str | None  # text only this resource's response may contain


@dataclass
class Seeded:
    resources: dict[tuple[str, str], Resource]
    canaries: dict[str, str]


def _seed_tenant(w, label: str, org_id: int | None, file) -> tuple[dict[str, Resource], str]:
    tag = f"zz{label}{uuid.uuid4().hex[:6]}"
    file.title = f"{tag}-title"
    # the degraded-embeddings listing names files by ``filename``, not by title
    file.filename = f"{tag}-name.wav"
    speaker = Speaker(
        user_id=w.owner.id,
        organization_id=org_id,
        media_file_id=file.id,
        name="SPEAKER_00",
        display_name=f"{tag}-spk",
    )
    profile = SpeakerProfile(user_id=w.owner.id, organization_id=org_id, name=f"{tag}-prof")
    collection = Collection(name=f"{tag}-coll", user_id=w.owner.id, organization_id=org_id)
    comment = Comment(media_file_id=file.id, user_id=w.owner.id, text=f"{tag}-comment")
    source = WatchSource(
        name=f"{tag}-ws",
        source_type="local",
        local_path="inbox",
        user_id=w.owner.id,
        organization_id=org_id,
    )
    # The id is opaque, as a Celery id is; it is the task route's own canary.
    task = Task(
        id=uuid.uuid4().hex,
        user_id=w.owner.id,
        media_file_id=file.id,
        task_type="transcription",
        status="in_progress",
    )
    w.db.add_all([speaker, profile, collection, comment, source, task])
    w.db.flush()
    w.db.add(CollectionMember(collection_id=collection.id, media_file_id=file.id))
    w.db.commit()
    for obj in (speaker, profile, collection, comment, source):
        w.db.refresh(obj)
    file_uuid = str(file.uuid)
    return (
        {
            "media_file": Resource(f"/api/files/{file_uuid}", file_uuid, f"{tag}-title"),
            "collection": Resource(
                f"/api/collections/{collection.uuid}", str(collection.uuid), f"{tag}-coll"
            ),
            "speaker": Resource(f"/api/speakers/{speaker.uuid}", str(speaker.uuid), f"{tag}-spk"),
            "speaker_profile": Resource(
                f"/api/speaker-profiles/profiles/{profile.uuid}/occurrences",
                str(profile.uuid),
                None,
            ),
            "comment": Resource(
                f"/api/comments/{comment.uuid}", str(comment.uuid), f"{tag}-comment"
            ),
            "watch_source": Resource(
                f"/api/watch-sources/{source.uuid}", str(source.uuid), f"{tag}-ws"
            ),
            "task": Resource(f"/api/tasks/{task.id}", None, task.id),
            "thumbnail": Resource(f"/api/files/{file_uuid}/thumbnail", file_uuid, None),
        },
        tag,
    )


@pytest.fixture
def seeded(support_world) -> Seeded:
    w = support_world
    resources: dict[tuple[str, str], Resource] = {}
    canaries: dict[str, str] = {}
    for label, org_id, file in (
        ("org", w.org.id, w.file),
        ("other", w.other_org.id, w.other_org_file),
        ("personal", None, w.personal_file),
    ):
        made, tag = _seed_tenant(w, label, org_id, file)
        canaries[label] = tag
        for kind, resource in made.items():
            resources[(kind, label)] = resource
    return Seeded(resources, canaries)


KINDS = (
    "media_file",
    "collection",
    "speaker",
    "speaker_profile",
    "comment",
    "watch_source",
    "task",
    "thumbnail",
)
REFUSED_STATUSES = {400, 403, 404}


def _body(response) -> str:
    return str(response.text)


def _use_rows(w, grant_uuid: str, resource_uuid: str) -> list[SupportAccessUse]:
    row = w.row(grant_uuid)
    rows: list[SupportAccessUse] = (
        w.db.query(SupportAccessUse)
        .filter(
            SupportAccessUse.grant_id == row.id,
            SupportAccessUse.resource_uuid == uuid.UUID(resource_uuid),
        )
        .all()
    )
    return rows


def _code(response) -> str:
    detail = response.json().get("detail")
    return str(detail.get("code")) if isinstance(detail, dict) else ""


def _revoke(w, grant_uuid: str) -> None:
    done = w.client.post(
        f"/api/support-access/grants/{grant_uuid}/revoke", headers=w.org_admin_headers
    )
    assert done.status_code == 200, done.text


def _personal_grant(w) -> str:
    grant = w.request(subject_user_uuid=str(w.owner.uuid))
    approved = w.client.post(
        f"/api/users/me/support-access/{grant['uuid']}/approve", headers=w.owner_headers, json={}
    )
    assert approved.status_code == 200, approved.text
    return str(grant["uuid"])


# ---------------------------------------------------------------------------
# The quartet, per chokepoint kind
# ---------------------------------------------------------------------------


#: Chokepoint kinds whose representative ROUTE does not hand the request bypass to the helper, so a
#: valid grant is refused there (it fails closed). The guard itself is proven by calling the helper
#: with the bypass a grant produces; the route is pinned as refusing, and flipping that is the
#: moment this table and docs/security/platform-access-matrix.md must change.
HELPER_ONLY = {
    "collection": lambda db, w, res, bypass, org_id: _collection_helper(db, w, res, bypass, org_id),
    "speaker_profile": lambda db, w, res, bypass, org_id: _profile_helper(
        db, w, res, bypass, org_id
    ),
}


def _collection_helper(db, w, res, bypass, org_id):
    from app.utils.uuid_helpers import get_collection_by_uuid_with_permission

    return get_collection_by_uuid_with_permission(
        db, res.resource_uuid, w.admin.id, organization_id=org_id, bypass=bypass
    )


def _profile_helper(db, w, res, bypass, org_id):
    from app.utils.uuid_helpers import require_profile_in_scope

    return require_profile_in_scope(
        db, res.resource_uuid, w.admin, organization_id=org_id, owner_only=False, bypass=bypass
    )


def _bypass_for(w, grant_uuid: str):
    """The bypass the real resolver builds for this grant; it raises for an unusable one."""
    from app.services.platform_bypass import build_bypass
    from app.services.support_access_service import resolve_active_grant

    view = resolve_active_grant(w.db, grant_uuid, w.admin)
    org_id = view.organization_id
    return org_id, build_bypass(w.db, w.admin, org_id, grant=view)


def _helper_refuses(w, kind, grant_uuid: str, res: Resource) -> bool:
    from fastapi import HTTPException

    org_id, bypass = _bypass_for(w, grant_uuid)
    try:
        HELPER_ONLY[kind](w.db, w, res, bypass, org_id)
    except HTTPException as refusal:
        assert refusal.status_code in REFUSED_STATUSES
        return True
    return False


@pytest.mark.parametrize("kind", KINDS)
def test_every_chokepoint_demands_a_valid_grant(support_world, seeded, kind):
    w = support_world
    target = seeded.resources[(kind, "org")]
    elsewhere = seeded.resources[(kind, "other")]
    personal = seeded.resources[(kind, "personal")]

    def call(resource: Resource, headers):
        return w.client.get(resource.path, headers=headers)

    # 1. no grant at all: the platform role alone reaches nothing of a tenant's
    plain = call(target, w.admin_headers)
    assert plain.status_code in REFUSED_STATUSES, plain.text
    if target.canary:
        assert target.canary not in _body(plain)

    # 2. pending: requested but not approved by the tenant
    pending = w.request()
    response = call(target, w.with_grant(pending["uuid"]))
    assert response.status_code == 403
    assert _code(response) == "support_grant_not_active"

    # 3. expired
    expired = w.active_grant()
    w.set_times(expired, starts_at=-2, expires_at=-1)
    response = call(target, w.with_grant(expired))
    assert response.status_code == 403
    assert _code(response) == "support_grant_expired"

    # 4. revoked
    revoked = w.active_grant()
    _revoke(w, revoked)
    response = call(target, w.with_grant(revoked))
    assert response.status_code == 403
    assert _code(response) == "support_grant_revoked"

    live = w.active_grant()
    mine = _personal_grant(w)
    if kind in HELPER_ONLY:
        # 5-6. the guard refuses another tenant, the personal workspace, and a personal grant
        assert _helper_refuses(w, kind, live, elsewhere)
        assert _helper_refuses(w, kind, live, personal)
        assert _helper_refuses(w, kind, mine, target)
        # 7. and serves the granted tenant, recording the use
        assert not _helper_refuses(w, kind, live, target)
        assert _use_rows(w, live, target.resource_uuid or ""), "honoured but not recorded"
        # the route does not forward the bypass today: it fails closed even for a valid grant
        assert call(target, w.with_grant(live)).status_code in REFUSED_STATUSES
        return

    # 5. valid, but for the organization and not the tenant the resource is in
    for foreign in (elsewhere, personal):
        response = call(foreign, w.with_grant(live))
        assert response.status_code in REFUSED_STATUSES, (foreign.path, response.text)
        if foreign.canary:
            assert foreign.canary not in _body(response)
        if foreign.resource_uuid:
            assert not _use_rows(w, live, foreign.resource_uuid)

    # 6. a PERSONAL grant (approved by that person) never reaches the organization's resource
    response = call(target, w.with_grant(mine))
    assert response.status_code in REFUSED_STATUSES, response.text
    if target.canary:
        assert target.canary not in _body(response)
    if target.resource_uuid:
        assert not _use_rows(w, mine, target.resource_uuid)

    # 7. the control that keeps all of the above honest: the same request is served under the
    #    valid grant for the right tenant, and the use is recorded against the resource
    served = call(target, w.with_grant(live))
    if kind == "thumbnail":
        # the guard passed and there is simply no thumbnail stored for the seeded file; the
        # message tells that apart from the refusals above, which say access was denied
        assert served.status_code == 404
        assert "Thumbnail not available" in served.text
    else:
        assert served.status_code == 200, served.text
    if target.canary:
        assert target.canary in _body(served)
    if target.resource_uuid and kind != "task":
        assert _use_rows(w, live, target.resource_uuid), "the grant was honoured but not recorded"


def test_a_read_grant_cannot_write_to_any_chokepoint_kind(support_world, seeded):
    w = support_world
    live = w.active_grant(level="read")
    headers = w.with_grant(live)
    writes = [
        ("put", seeded.resources[("media_file", "org")].path, {"title": "changed"}),
        ("delete", seeded.resources[("collection", "org")].path, None),
        ("delete", seeded.resources[("comment", "org")].path, None),
        ("delete", seeded.resources[("watch_source", "org")].path, None),
        ("put", seeded.resources[("speaker", "org")].path, {"display_name": "changed"}),
    ]
    for method, path, body in writes:
        kwargs: dict[str, Any] = {"json": body} if body is not None else {}
        response = getattr(w.client, method)(path, headers=headers, **kwargs)
        assert response.status_code == 403, (method, path, response.text)
        assert _code(response) == "support_grant_write_required", (method, path)


# ---------------------------------------------------------------------------
# The sweep: no grant, no content, on every route a platform role can call
# ---------------------------------------------------------------------------

#: Query parameters a GET needs to run at all, so the sweep exercises its query rather than a 422.
QUERY_FILL: dict[str, dict[str, Any]] = {
    "/api/admin/settings/retention-config/preview": {"retention_days": 1},
    "/api/search": {"query": "zz"},
    "/api/search/count": {"query": "zz"},
}
#: Never swept: an open stream, or a redirect to an external identity provider.
NOT_SWEPT = {
    "/api/files/bulk-export-stream",
    "/api/auth/oidc/login",
    "/api/auth/oidc/callback",
    "/api/auth/saml/login",
    "/api/auth/saml/sls",
}
ADMIN_DEPS = {
    "get_current_admin_user",
    "get_current_active_superuser",
    "get_current_super_admin_user",
}


def _sweepable_paths() -> list[str]:
    from fastapi.routing import APIRoute
    from fixtures.platform_access_matrix import dependency_names

    from app.main import app

    paths = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        if "{" in route.path or route.path in NOT_SWEPT:
            continue
        names = dependency_names(route.dependant)
        if names & ADMIN_DEPS or "get_current_context" in names:
            paths.append(route.path)
    return sorted(paths)


def test_the_sweep_covers_the_platform_surface():
    paths = _sweepable_paths()
    assert len(paths) > 80
    for must in (
        "/api/files",
        "/api/speakers",
        "/api/speakers/debug/cross-media-by-name",
        "/api/admin/stats",
        "/api/admin/files/quarantined",
        "/api/tasks",
    ):
        assert must in paths, must


def _degraded_survey(w, *extra):
    """The three tenants' files (and ``extra`` ones), as a degraded window would have left them."""
    from app.services.search.embedding_provenance import DegradedFile

    files = [
        DegradedFile(user_id=w.owner.id, file_uuid=str(f.uuid))
        for f in (w.file, w.other_org_file, w.personal_file)
    ] + [DegradedFile(user_id=f.user_id, file_uuid=str(f.uuid)) for f in extra]
    return patch(
        "app.services.search.embedding_provenance.survey_degraded_files",
        return_value=(files, False),
    )


def test_a_platform_admin_without_a_grant_sees_no_tenant_content(support_world, seeded):
    w = support_world
    # The sweep is only as strong as what it can find: seed a name every diagnostic route matches.
    probe_name = f"{seeded.canaries['personal']}-spk"
    paths = _sweepable_paths()
    answered = 0
    unreachable: list[str] = []
    leaks: list[str] = []
    with _degraded_survey(w):
        for who, headers in (("admin", w.admin_headers), ("super_admin", w.super_headers)):
            for path in paths:
                params = dict(QUERY_FILL.get(path, {}))
                if path == "/api/speakers/debug/cross-media-by-name":
                    params["speaker_name"] = probe_name
                try:
                    response = w.client.get(path, headers=headers, params=params)
                except Exception:  # a route needing Redis or a worker; counted, never ignored
                    unreachable.append(path)
                    continue
                answered += 1
                for label, tag in seeded.canaries.items():
                    if tag in response.text:
                        leaks.append(f"{who} GET {path} returned the {label} tenant's content")
    assert not leaks, "\n".join(leaks)
    # a sweep that answered nothing proves nothing
    assert answered > len(paths), f"only {answered} answers over {len(paths)} routes"
    assert len(set(unreachable)) <= 6, f"too many routes could not be called: {set(unreachable)}"


def test_the_diagnostic_speaker_route_is_scoped_to_the_caller_in_a_multi_tenant_deployment(
    support_world, seeded
):
    """The one route in the sweep that takes a name; checked alone so its failure says why."""
    w = support_world
    name = f"{seeded.canaries['personal']}-spk"
    response = w.client.get(
        "/api/speakers/debug/cross-media-by-name",
        params={"speaker_name": name},
        headers=w.admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["speakers_found"] == 0
    assert seeded.canaries["personal"] not in response.text


def test_the_diagnostic_speaker_route_under_an_org_grant_stays_in_that_tenant(
    support_world, seeded
):
    w = support_world
    headers = w.with_grant(w.active_grant())
    own = w.client.get(
        "/api/speakers/debug/cross-media-by-name",
        params={"speaker_name": f"{seeded.canaries['org']}-spk"},
        headers=headers,
    )
    assert own.status_code == 200
    assert own.json()["speakers_found"] == 1
    for label in ("other", "personal"):
        foreign = w.client.get(
            "/api/speakers/debug/cross-media-by-name",
            params={"speaker_name": f"{seeded.canaries[label]}-spk"},
            headers=headers,
        )
        assert foreign.status_code == 200
        assert foreign.json()["speakers_found"] == 0, label
        assert seeded.canaries[label] not in foreign.text


# ---------------------------------------------------------------------------
# Lists widen inside the granted tenant and nowhere else
# ---------------------------------------------------------------------------

#: (path, query, how the org tenant's row shows in the list: a suffix of the tag, "TASK" for the
#: task id, or None when an organization grant lists nothing there today and must still list
#: nothing of any other tenant)
LIST_ROUTES: tuple[tuple[str, dict[str, Any], str | None, str], ...] = (
    ("/api/files", {}, "-title", "media_file"),
    ("/api/speakers", {}, "-spk", "speaker"),
    ("/api/speaker-profiles/profiles", {}, "-prof", "speaker_profile"),
    ("/api/collections", {}, None, "collection"),
    ("/api/watch-sources", {"scope": "all"}, "-ws", "watch_source"),
    ("/api/tasks", {}, "TASK", "task"),
)


@pytest.mark.parametrize(
    ("path", "params", "marker", "kind"), LIST_ROUTES, ids=[r[0] for r in LIST_ROUTES]
)
def test_an_org_grant_widens_lists_only_inside_its_tenant(
    support_world, seeded, path, params, marker, kind
):
    w = support_world
    headers = w.with_grant(w.active_grant())
    response = w.client.get(path, headers=headers, params=params)
    assert response.status_code == 200, response.text
    for label in ("other", "personal"):
        assert seeded.canaries[label] not in response.text, (path, label)
        task_id = seeded.resources[("task", label)].canary
        assert task_id not in response.text, (path, label, "task id")
    plain = w.client.get(path, headers=w.admin_headers, params=params)
    assert plain.status_code == 200
    assert seeded.canaries["org"] not in plain.text, "no grant must see none of the org's rows"
    if marker == "TASK":
        want = seeded.resources[("task", "org")].canary
    elif marker is not None:
        want = seeded.canaries["org"] + marker
    else:
        want = None
    if want is not None:
        # the grant is what lets the list show the tenant's rows at all
        assert want in response.text, (path, "granted tenant's rows absent")
        assert want not in plain.text


# ---------------------------------------------------------------------------
# Platform listings that name other tenants' files are audited
# ---------------------------------------------------------------------------

AUDITED_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("/api/admin/files/quarantined", {}),
    ("/api/admin/settings/retention-config/preview", {"retention_days": 1}),
    ("/api/admin/takedown-notices", {}),
    ("/api/admin/takedown-notices/export", {}),
)


@pytest.mark.parametrize(("path", "params"), AUDITED_CALLS, ids=[p for p, _ in AUDITED_CALLS])
def test_cross_tenant_filename_listings_are_audited(support_world, seeded, path, params):
    w = support_world
    with patch("app.auth.audit.audit_logger.log") as audit:
        response = w.client.get(path, headers=w.admin_headers, params=params)
    assert response.status_code == 200, response.text
    events = [
        c for c in audit.call_args_list if c.args and c.args[0] == "platform_admin.metadata.access"
    ]
    assert len(events) == 1, f"{path} must leave exactly one metadata-access event"
    assert events[0].kwargs["details"]["route"] == path.split("?")[0]
    assert w.other_org_file.filename not in str(events[0]), "the event must carry ids, not names"


# ---------------------------------------------------------------------------
# The degraded-embeddings listing stays inside the caller's active tenant
# ---------------------------------------------------------------------------

DEGRADED = "/api/search/degraded-embeddings"


def test_degraded_listing_names_only_files_of_the_active_tenant(support_world, seeded):
    from fixtures.support_world import make_file

    w = support_world
    own = make_file(w.db, w.admin, None)
    with _degraded_survey(w, own):
        response = w.client.get(DEGRADED, headers=w.admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [f["file_uuid"] for f in body["files"]] == [str(own.uuid)]
    assert body["files"][0]["title"] == own.filename, "the caller's own file is named"
    assert body["total_files"] == 1, "a plain admin is told only about the files listed to them"
    assert body["outside_scope_files"] == 0
    assert body["affected_users"] == 1
    for tag in seeded.canaries.values():
        assert tag not in response.text
    assert str(w.owner.id) not in {str(f["user_id"]) for f in body["files"]}


def test_degraded_listing_in_an_org_context_is_the_callers_rows_inside_that_org(
    support_world, seeded
):
    w = support_world
    w.roles[w.admin.id] = (w.org.id, "org:member")
    with _degraded_survey(w):
        response = w.client.get(DEGRADED, headers=w.admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"] == [], "the org's file belongs to its owner, not to the platform admin"
    assert body["total_files"] == 0
    for tag in seeded.canaries.values():
        assert tag not in response.text


def test_degraded_listing_stays_instance_wide_in_a_single_tenant_deployment(
    support_world, seeded, monkeypatch
):
    from app.core.config import settings
    from app.services.platform_access import reset_tenancy_mode_cache

    w = support_world
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()
    try:
        with _degraded_survey(w):
            response = w.client.get(DEGRADED, headers=w.admin_headers)
    finally:
        reset_tenancy_mode_cache()
    assert response.status_code == 200, response.text
    body = response.json()
    assert {f["file_uuid"] for f in body["files"]} == {
        str(f.uuid) for f in (w.file, w.other_org_file, w.personal_file)
    }
    assert w.personal_file.filename in response.text
    assert body["outside_scope_files"] == 0
