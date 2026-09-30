"""Owner listings and owner operations stay inside the ACTIVE tenant.

A user's own rows are listed by ``user_id``, but a user can belong to several
tenants (personal workspace, one or more organizations). Every surface here keeps
the ``user_id == caller`` filter and additionally requires the row's tenant stamp
to name the active tenant: an organization's rows never show in personal scope or
in another organization, and personal rows never show inside an organization.

Each surface is exercised over the same tenant relations for a row stamped with
org A and owned by alice:

* ``same_org``            — alice acting in org A: visible.
* ``other_org``           — alice acting in org B (she is a member of both): hidden.
* ``personal``            — alice in her personal workspace: hidden.
* ``membership_removed``  — alice's org A membership row deleted, so the request
  resolves to personal scope: hidden.

and ``community`` — an org-less row with alice in personal scope (no orgs at all):
visible, which is the community-edition behaviour that must not change.

Requests run as a specific user in a specific tenant by overriding the two auth
dependencies — the shape ``test_collection_tenancy.py`` uses.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest

from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.custom_vocabulary import CustomVocabulary
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.topic import TopicSuggestion
from app.models.usage_event import UsageEvent
from app.models.user import User
from app.services.chat.usage import EVENT_TYPE_CHAT_TOKENS

# --------------------------------------------------------------------------- #
# World                                                                        #
# --------------------------------------------------------------------------- #


def _mk_user(db, label: str) -> User:
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=f"{label} user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


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


def _join(db, org: Organization, user: User, role: str = "org:member") -> None:
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
    db.commit()


def _mk_file(
    db,
    user: User,
    org_id: int | None,
    *,
    status: FileStatus = FileStatus.COMPLETED,
    uploaded_hours_ago: float = 0.0,
) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=fuuid,
        filename=f"owner_listing_{str(fuuid)[:8]}.mp3",
        storage_path=f"owner_listing/{fuuid}.mp3",
        content_type="audio/mpeg",
        file_size=1,
        user_id=user.id,
        organization_id=org_id,
        status=status,
        upload_time=datetime.now(UTC) - timedelta(hours=uploaded_hours_ago),
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


@contextmanager
def _acting_as(user: User, org_id: int | None, org_role: str | None = "org:member"):
    """Run requests as ``user`` in tenant ``org_id`` (None = personal workspace)."""
    from app.api.deps_context import RequestContext
    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user
    from app.main import app

    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=user, org_id=org_id, org_role=org_role if org_id is not None else None
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_context, None)


@pytest.fixture()
def world(db_session):
    """Org A and org B, alice a member of both, bob a member of org A only."""
    db = db_session
    org_a = _mk_org(db, "A")
    org_b = _mk_org(db, "B")
    alice = _mk_user(db, "alice")
    bob = _mk_user(db, "bob")
    _join(db, org_a, alice)
    _join(db, org_b, alice)
    _join(db, org_a, bob)
    return type("World", (), {"db": db, "org_a": org_a, "org_b": org_b, "alice": alice, "bob": bob})


# (relation, row is stamped with org A?, visible?)
RELATIONS = [
    pytest.param("same_org", True, True, id="same_org"),
    pytest.param("other_org", True, False, id="other_org"),
    pytest.param("personal", True, False, id="personal"),
    pytest.param("membership_removed", True, False, id="membership_removed"),
    pytest.param("community", False, True, id="community"),
]


def _row_org(w, stamped: bool) -> int | None:
    return w.org_a.id if stamped else None


def _acting_org(w, relation: str) -> int | None:
    """The tenant alice's request resolves to under ``relation``."""
    if relation == "same_org":
        return int(w.org_a.id)
    if relation == "other_org":
        return int(w.org_b.id)
    if relation == "membership_removed":
        w.db.query(OrganizationMembership).filter(
            OrganizationMembership.organization_id == w.org_a.id,
            OrganizationMembership.user_id == w.alice.id,
        ).delete()
        w.db.commit()
    return None


# --------------------------------------------------------------------------- #
# GET /tasks, GET /tasks/{task_id}                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_task_listing_and_lookup_follow_the_active_tenant(
    client, world, relation, stamped, visible
):
    w = world
    media_file = _mk_file(w.db, w.alice, _row_org(w, stamped))
    task_id = f"celery-{uuid_pkg.uuid4().hex}"
    w.db.add(
        TaskModel(
            id=task_id,
            user_id=w.alice.id,
            media_file_id=media_file.id,
            task_type="transcription",
            status="completed",
        )
    )
    w.db.commit()

    with _acting_as(w.alice, _acting_org(w, relation)):
        listed = client.get("/api/tasks", params={"page_size": 100})
        assert listed.status_code == 200, listed.text
        file_uuids = {item["media_file_id"] for item in listed.json()["items"]}
        assert (str(media_file.uuid) in file_uuids) is visible

        by_task_id = client.get(f"/api/tasks/{task_id}")
        by_legacy_id = client.get(f"/api/tasks/task_{media_file.id}")

    expected = 200 if visible else 404
    assert by_task_id.status_code == expected, by_task_id.text
    assert by_legacy_id.status_code == expected, by_legacy_id.text


# --------------------------------------------------------------------------- #
# GET /tasks/progress/active                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_active_progress_names_only_files_of_the_active_tenant(
    client, world, monkeypatch, relation, stamped, visible
):
    from app.services.progress_tracker import ProgressTracker

    w = world
    media_file = _mk_file(w.db, w.alice, _row_org(w, stamped))
    state = {
        "task_type": "reindex",
        "user_id": w.alice.id,
        "total": 2,
        "processed": 1,
        "status": "running",
        "failed_items": [str(media_file.uuid), "not-a-uuid"],
    }
    monkeypatch.setattr(ProgressTracker, "get_active_tasks", classmethod(lambda cls, uid: [state]))

    with _acting_as(w.alice, _acting_org(w, relation)):
        response = client.get("/api/tasks/progress/active")

    assert response.status_code == 200, response.text
    failed = response.json()[0]["failed_items"]
    assert failed == ([str(media_file.uuid)] if visible else [])


# --------------------------------------------------------------------------- #
# GET /my-files/status, POST /my-files/request-recovery                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_my_files_status_counts_only_the_active_tenant(client, world, relation, stamped, visible):
    w = world
    failed = _mk_file(w.db, w.alice, _row_org(w, stamped), status=FileStatus.ERROR)

    with _acting_as(w.alice, _acting_org(w, relation)):
        response = client.get("/api/my-files/status")

    assert response.status_code == 200, response.text
    body = response.json()
    problem = {f["uuid"] for f in body["problem_files"]["files"]}
    recent = {f["uuid"] for f in body["recent_files"]["files"]}
    assert (str(failed.uuid) in problem) is visible
    assert (str(failed.uuid) in recent) is visible
    assert body["status_counts"]["error"] == (1 if visible else 0)


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_problem_file_sweep_for_a_user_request_stays_in_its_tenant(
    world, relation, stamped, visible
):
    from app.services.task_detection_service import task_detection_service

    w = world
    stuck = _mk_file(
        w.db, w.alice, _row_org(w, stamped), status=FileStatus.PROCESSING, uploaded_hours_ago=48
    )
    org_id = _acting_org(w, relation)

    found = task_detection_service.find_user_problem_files(w.db, w.alice.id, organization_id=org_id)
    assert (stuck.id in {f.id for f in found}) is visible
    # The admin sweep (no tenant threaded) still sees every file of the user.
    assert stuck.id in {
        f.id for f in task_detection_service.find_user_problem_files(w.db, w.alice.id)
    }


def test_request_recovery_passes_the_active_tenant(client, world, monkeypatch):
    from app.tasks import recovery

    w = world
    captured: dict = {}

    def fake_delay(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return type("R", (), {"id": "t"})()

    monkeypatch.setattr(recovery.recover_user_files_task, "delay", fake_delay)
    with _acting_as(w.alice, w.org_b.id):
        response = client.post("/api/my-files/request-recovery")

    assert response.status_code == 200, response.text
    assert captured["args"] == (w.alice.id,)
    assert captured["kwargs"] == {"tenant_scoped": True, "organization_id": w.org_b.id}


# --------------------------------------------------------------------------- #
# GET /files/management/stuck                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_stuck_file_listing_follows_the_active_tenant(
    client, world, monkeypatch, relation, stamped, visible
):
    from app.api.endpoints.files import management

    w = world
    stuck = _mk_file(w.db, w.alice, _row_org(w, stamped), status=FileStatus.PROCESSING)
    monkeypatch.setattr(management, "check_for_stuck_files", lambda db, hours: [stuck.id])

    with _acting_as(w.alice, _acting_org(w, relation)):
        response = client.get("/api/files/management/stuck")

    assert response.status_code == 200, response.text
    listed = {f["uuid"] for f in response.json()["stuck_files"]}
    assert (str(stuck.uuid) in listed) is visible


# --------------------------------------------------------------------------- #
# Custom vocabulary                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_custom_vocabulary_is_listed_exported_and_edited_in_its_tenant(
    client, world, relation, stamped, visible
):
    w = world
    term_text = f"term-{uuid_pkg.uuid4().hex[:8]}"
    with _acting_as(w.alice, _row_org(w, stamped)):
        created = client.post("/api/custom-vocabulary", json={"term": term_text})
    assert created.status_code == 201, created.text
    term_id = created.json()["id"]
    row = w.db.query(CustomVocabulary).filter(CustomVocabulary.id == term_id).one()
    assert row.organization_id == _row_org(w, stamped)

    with _acting_as(w.alice, _acting_org(w, relation)):
        listed = client.get("/api/custom-vocabulary")
        exported = client.get("/api/custom-vocabulary/export")
        updated = client.put(f"/api/custom-vocabulary/{term_id}", json={"is_active": True})

    assert listed.status_code == 200, listed.text
    assert (term_text in {t["term"] for t in listed.json()["terms"]}) is visible
    assert (term_text in {t["term"] for t in exported.json()["terms"]}) is visible
    assert updated.status_code == (200 if visible else 404), updated.text


def test_custom_vocabulary_delete_and_bulk_follow_the_active_tenant(client, world):
    w = world
    with _acting_as(w.alice, w.org_a.id):
        org_term = client.post("/api/custom-vocabulary", json={"term": "org-only"}).json()
        bulk = client.post(
            "/api/custom-vocabulary/bulk", json={"terms": [{"term": "bulk-org-term"}]}
        )
    assert bulk.status_code == 201, bulk.text
    bulk_row = w.db.query(CustomVocabulary).filter(
        CustomVocabulary.user_id == w.alice.id, CustomVocabulary.term == "bulk-org-term"
    )
    assert bulk_row.one().organization_id == w.org_a.id

    with _acting_as(w.alice, None):
        personal_term = client.post("/api/custom-vocabulary", json={"term": "mine"}).json()
        assert client.delete(f"/api/custom-vocabulary/{org_term['id']}").status_code == 404
        assert client.delete("/api/custom-vocabulary/all").status_code == 204

    remaining = {
        t.id for t in w.db.query(CustomVocabulary).filter(CustomVocabulary.user_id == w.alice.id)
    }
    assert org_term["id"] in remaining, "delete-all in personal scope wiped an org's terms"
    assert personal_term["id"] not in remaining


def test_transcription_vocabulary_comes_from_the_files_tenant(world):
    from app.tasks.transcription.cloud_asr import load_vocabulary_terms

    w = world
    w.db.add_all(
        [
            CustomVocabulary(user_id=w.alice.id, organization_id=None, term="personal-term"),
            CustomVocabulary(user_id=w.alice.id, organization_id=w.org_a.id, term="org-a-term"),
            CustomVocabulary(user_id=w.alice.id, organization_id=w.org_b.id, term="org-b-term"),
            CustomVocabulary(user_id=None, organization_id=w.org_b.id, term="org-b-shared"),
        ]
    )
    w.db.commit()
    org_file = _mk_file(w.db, w.alice, w.org_a.id)
    personal_file = _mk_file(w.db, w.alice, None)

    org_terms = set(load_vocabulary_terms(w.db, w.alice.id, org_file.id))
    personal_terms = set(load_vocabulary_terms(w.db, w.alice.id, personal_file.id))

    assert {"org-a-term"} <= org_terms
    assert not {"personal-term", "org-b-term", "org-b-shared"} & org_terms
    assert "personal-term" in personal_terms
    assert not {"org-a-term", "org-b-term", "org-b-shared"} & personal_terms


# --------------------------------------------------------------------------- #
# POST /files/retroactive-auto-label                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_retroactive_auto_label_only_picks_files_of_the_active_tenant(
    world, relation, stamped, visible
):
    from app.tasks.auto_labeling import pending_suggestion_ids

    w = world
    media_file = _mk_file(w.db, w.alice, _row_org(w, stamped))
    suggestion = TopicSuggestion(media_file_id=media_file.id, user_id=w.alice.id)
    w.db.add(suggestion)
    w.db.commit()

    ids = pending_suggestion_ids(
        w.db, w.alice.id, tenant_scoped=True, organization_id=_acting_org(w, relation)
    )
    assert (suggestion.id in ids) is visible


def test_retroactive_auto_label_endpoint_passes_the_active_tenant(client, world, monkeypatch):
    from app.tasks import auto_labeling

    w = world
    captured: dict = {}

    def fake_delay(**kwargs):
        captured.update(kwargs)
        return type("R", (), {"id": "t"})()

    monkeypatch.setattr(auto_labeling.retroactive_auto_label_task, "delay", fake_delay)
    with _acting_as(w.alice, w.org_a.id):
        response = client.post("/api/files/retroactive-auto-label", json={})

    assert response.status_code == 202, response.text
    assert captured["tenant_scoped"] is True
    assert captured["organization_id"] == w.org_a.id


# --------------------------------------------------------------------------- #
# GET /usage/me, /usage/me/daily                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_usage_reports_only_the_active_tenant(client, world, relation, stamped, visible):
    w = world
    w.db.add(
        UsageEvent(
            user_id=w.alice.id,
            organization_id=_row_org(w, stamped),
            event_type=EVENT_TYPE_CHAT_TOKENS,
            quantity=Decimal(123),
            event_metadata={"provider": "p", "model": "m", "prompt_tokens": 100},
        )
    )
    w.db.commit()

    with _acting_as(w.alice, _acting_org(w, relation)):
        summary = client.get("/api/usage/me")
        daily = client.get("/api/usage/me/daily")

    assert summary.status_code == 200, summary.text
    assert summary.json()["totals"]["total_tokens"] == (123 if visible else 0)
    assert sum(d["total_tokens"] for d in daily.json()["series"]) == (123 if visible else 0)


# --------------------------------------------------------------------------- #
# GET /search/reindex/status, GET /search/index-health                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_reindex_status_counts_only_the_active_tenant(
    client, world, monkeypatch, relation, stamped, visible
):
    from app.api.endpoints import search
    from app.db import session_utils
    from app.models.media import TranscriptSegment

    w = world
    media_file = _mk_file(w.db, w.alice, _row_org(w, stamped))
    w.db.add(
        TranscriptSegment(media_file_id=media_file.id, start_time=0.0, end_time=1.0, text="hi")
    )
    w.db.commit()

    @contextmanager
    def same_session():
        yield w.db

    monkeypatch.setattr(session_utils, "session_scope", same_session)
    monkeypatch.setattr(search, "_running_reindex_run_id", lambda uid: None)

    with _acting_as(w.alice, _acting_org(w, relation)):
        response = client.get("/api/search/reindex/status")

    assert response.status_code == 200, response.text
    assert response.json()["total_files"] == (1 if visible else 0)


class _FakeCat:
    def aliases(self, **_):
        return []

    def indices(self, index, **_):
        return [{"index": name, "docs.count": "4242"} for name in index.split(",")]


class _FakeOpenSearch:
    cat = _FakeCat()

    def __init__(self):
        self.count_queries: list[dict] = []

    def count(self, index, body):
        self.count_queries.append(body)
        return {"count": 3}


@pytest.mark.parametrize("is_admin", [False, True], ids=["user", "admin"])
def test_index_health_instance_counts_are_admin_only(client, world, monkeypatch, is_admin):
    import app.services.opensearch_service as opensearch_service

    w = world
    fake = _FakeOpenSearch()
    monkeypatch.setattr(opensearch_service, "opensearch_client", fake)
    if is_admin:
        w.alice.role = "admin"
        w.db.commit()

    with _acting_as(w.alice, w.org_a.id):
        response = client.get("/api/search/index-health")

    assert response.status_code == 200, response.text
    entries = list(response.json().values())
    assert entries and all(e["status"] == "green" for e in entries)
    if is_admin:
        assert all(e["doc_count"] == 4242 for e in entries)
        assert fake.count_queries == []
    else:
        assert all(e["doc_count"] == 3 for e in entries), "a user gets their own counts"
        assert len(fake.count_queries) == len(entries)
        for query in fake.count_queries:
            filters = query["query"]["bool"]["filter"]
            assert {"term": {"user_id": w.alice.id}} in filters
            assert {"term": {"organization_id": w.org_a.id}} in filters


# --------------------------------------------------------------------------- #
# POST /chat/messages/{uuid}/cancel                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_chat_cancel_requires_the_conversations_tenant(
    client, world, monkeypatch, relation, stamped, visible
):
    from app.services.chat import limits

    w = world
    conversation = ChatConversation(user_id=w.alice.id, organization_id=_row_org(w, stamped))
    w.db.add(conversation)
    w.db.commit()
    message = ChatMessage(conversation_id=conversation.id, role="assistant", content="...")
    w.db.add(message)
    w.db.commit()
    flagged: list[str] = []
    monkeypatch.setattr(limits, "request_cancel", flagged.append)

    with _acting_as(w.alice, _acting_org(w, relation)):
        response = client.post(f"/api/chat/messages/{message.uuid}/cancel")

    assert response.status_code == (200 if visible else 404), response.text
    assert flagged == ([str(message.uuid)] if visible else [])


# --------------------------------------------------------------------------- #
# GET /files/bulk-export-stream                                               #
# --------------------------------------------------------------------------- #


def test_bulk_export_job_is_bound_to_the_user_who_prepared_it(client, world):
    from app.api.endpoints.files.subtitles import bulk_job_owned_by
    from app.api.endpoints.files.subtitles import new_bulk_job_id

    w = world
    job = new_bulk_job_id(w.alice.id)
    assert bulk_job_owned_by(job, w.alice.id)
    assert not bulk_job_owned_by(job, w.bob.id)
    assert not bulk_job_owned_by(uuid_pkg.uuid4().hex, w.alice.id), "unsigned ids are refused"

    with _acting_as(w.bob, w.org_a.id):
        response = client.get("/api/files/bulk-export-stream", params={"job": job})
    assert response.status_code == 404, response.text


def test_bulk_export_prepare_issues_a_job_bound_to_the_caller(client, world, monkeypatch):
    from app.api.endpoints.files.subtitles import bulk_job_owned_by
    from app.tasks import media_download

    w = world
    media_file = _mk_file(w.db, w.alice, w.org_a.id)
    monkeypatch.setattr(media_download.prepare_bulk_subtitles_task, "delay", lambda **kw: None)

    with _acting_as(w.alice, w.org_a.id):
        response = client.post(
            "/api/files/bulk-export/prepare", json={"file_uuids": [str(media_file.uuid)]}
        )

    assert response.status_code == 200, response.text
    job = response.json()["job_id"]
    assert bulk_job_owned_by(job, w.alice.id)
    assert not bulk_job_owned_by(job, w.bob.id)


# --------------------------------------------------------------------------- #
# PermissionService.check_file_access / check_collection_access               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("relation", "stamped", "visible"), RELATIONS)
def test_check_file_access_honours_an_explicit_tenant(world, relation, stamped, visible):
    from fastapi import HTTPException

    from app.services.permission_service import PermissionService

    w = world
    media_file = _mk_file(w.db, w.alice, _row_org(w, stamped))
    org_id = _acting_org(w, relation)

    # Without a tenant the legacy behaviour is unchanged: the owner has access.
    assert PermissionService.check_file_access(w.db, media_file.id, w.alice.id) == "owner"
    if visible:
        assert (
            PermissionService.check_file_access(
                w.db, media_file.id, w.alice.id, organization_id=org_id
            )
            == "owner"
        )
    else:
        with pytest.raises(HTTPException) as exc:
            PermissionService.check_file_access(
                w.db, media_file.id, w.alice.id, organization_id=org_id
            )
        assert exc.value.status_code == 403


def test_check_collection_access_honours_an_explicit_tenant(world):
    from fastapi import HTTPException

    from app.models.media import Collection
    from app.services.permission_service import PermissionService

    w = world
    coll = Collection(name=f"c-{uuid_pkg.uuid4().hex[:6]}", user_id=w.alice.id)
    w.db.add(coll)
    w.db.commit()

    assert PermissionService.check_collection_access(w.db, coll.id, w.alice.id) == "owner"
    assert (
        PermissionService.check_collection_access(w.db, coll.id, w.alice.id, organization_id=None)
        == "owner"
    )
    with pytest.raises(HTTPException):
        PermissionService.check_collection_access(
            w.db, coll.id, w.alice.id, organization_id=w.org_a.id
        )
