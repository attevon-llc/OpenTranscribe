"""Functional tests for the five ``/api/chat/projects`` routes (``chat/projects.py``).

Projects group chat conversations and pin a retrieval scope plus a system-prompt
layer. Before this file **none of the five routes was referenced by any test** —
``scripts/audit-route-coverage.py`` listed all five — so the whole CRUD surface,
its privacy rule and its delete semantics were unpinned.

Two invariants are the reason this file exists:

* **Deleting a project must NOT delete its conversations.** The FK is
  ``ON DELETE SET NULL`` and ``delete_project`` additionally detaches in the ORM
  so the identity map agrees with the database. Losing a grouping is recoverable;
  losing the threads is not. ``test_delete_leaves_conversations_ungrouped`` is the
  test a future "clean up orphans" refactor has to get past.
* **A project belonging to someone else answers 404, not 403.** ``get_owned_project``
  deliberately makes an invisible project indistinguishable from a missing one, so
  probing cannot enumerate other accounts' projects.

The whole ``/api/chat`` surface is behind the ``chat.rag`` capability, and
``require_capability`` answers **404** rather than 403 — pinned below, because a
gate that 403s tells an anonymous prober the feature exists.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models.chat import ChatConversation
from app.models.chat import ChatProject

PROJECTS = "/api/chat/projects"

#: A syntactically valid uuid that is never inserted. A literal, NOT ``uuid4()``:
#: a parametrize argument is evaluated at import time and becomes part of the test
#: id, so a random one gives each xdist worker a different id and collection fails.
ABSENT_UUID = "00000000-0000-4000-8000-00000000dead"


def _make_project(db, user, *, name: str, archived: bool = False, scope=None) -> ChatProject:
    project = ChatProject(
        user_id=user.id,
        organization_id=None,
        name=name,
        scope=scope,
        is_archived=archived,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_conversation(db, user, project: ChatProject | None) -> ChatConversation:
    convo = ChatConversation(
        user_id=user.id,
        organization_id=None,
        project_id=project.id if project else None,
        title="thread",
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return convo


# ---------------------------------------------------------------------------
# Authentication and the capability gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", PROJECTS),
        ("POST", PROJECTS),
        ("GET", f"{PROJECTS}/{ABSENT_UUID}"),
        ("PATCH", f"{PROJECTS}/{ABSENT_UUID}"),
        ("DELETE", f"{PROJECTS}/{ABSENT_UUID}"),
    ],
)
def test_every_route_requires_authentication(client, method, path):
    response = client.request(method, path, json={"name": "x"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_the_surface_is_404_when_the_chat_capability_is_off(client, user_token_headers):
    """``require_capability`` hides a disabled surface rather than forbidding it.

    Catches the gate being "fixed" to 403: a deployment that turned chat off would
    then confirm the feature's existence to every caller, and the SPA's 404-means-
    absent handling would start rendering an error instead of hiding the tab.
    """
    set_capability_resolver(lambda _request: {"chat.rag": False})
    try:
        response = client.get(PROJECTS, headers=user_token_headers)
    finally:
        reset_capability_resolver()
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_the_surface_is_reachable_with_the_capability_at_its_community_default(
    client, user_token_headers
):
    """The control for the test above — ``chat.rag`` is on by default here."""
    response = client.get(PROJECTS, headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# Privacy: another account's project is indistinguishable from a missing one
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["GET", "PATCH", "DELETE"])
def test_another_users_project_is_404_not_403(
    client, db_session, other_user_auth_headers, normal_user, method
):
    """A project is private to its creator, and the refusal must not confirm it exists.

    Catches ``get_owned_project`` dropping the ``user_id`` filter (which would return
    someone else's project) and equally catches it being "improved" to 403, which
    would let an attacker enumerate other accounts' project uuids.
    """
    victim = _make_project(db_session, normal_user, name="Victim Project")

    response = client.request(
        method,
        f"{PROJECTS}/{victim.uuid}",
        headers=other_user_auth_headers,
        json={"name": "hijacked"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_an_unknown_uuid_is_404(client, user_token_headers):
    response = client.get(f"{PROJECTS}/{ABSENT_UUID}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def test_create_returns_201_with_the_pinned_scope_and_prompt(client, user_token_headers):
    """``POST`` answers 201 and echoes the full detail shape, not the summary.

    ``has_scope`` is derived, not stored — the UI says "new chats inherit a scope"
    off this flag, so a create that dropped ``scope`` would silently produce
    unscoped projects that still looked configured.
    """
    response = client.post(
        PROJECTS,
        headers=user_token_headers,
        json={
            "name": "  Quarterly Review  ",
            "description": "recurring",
            "system_prompt": "Answer in bullet points.",
            "scope": {"tag_names": ["finance"]},
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    # The name is stripped on write; a stored "  Quarterly Review  " sorts wrongly.
    assert body["name"] == "Quarterly Review"
    assert body["description"] == "recurring"
    assert body["system_prompt"] == "Answer in bullet points."
    assert body["scope"]["tag_names"] == ["finance"]
    assert body["scope"]["file_uuids"] == []
    assert body["has_scope"] is True
    assert body["conversation_count"] == 0
    assert body["is_archived"] is False


def test_create_without_a_scope_reports_has_scope_false(client, user_token_headers):
    """The other half of the derived flag: no pinned recordings, no inherited scope."""
    response = client.post(PROJECTS, headers=user_token_headers, json={"name": "Unscoped"})

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["has_scope"] is False


def test_create_rejects_an_empty_name(client, user_token_headers):
    response = client.post(PROJECTS, headers=user_token_headers, json={"name": ""})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_with_an_unusable_llm_config_is_404(client, user_token_headers):
    """``resolve_llm_config_id`` refuses a config the caller may not use."""
    response = client.post(
        PROJECTS,
        headers=user_token_headers,
        json={"name": "Borrowed model", "llm_config_uuid": ABSENT_UUID},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
def test_list_is_case_insensitive_alphabetical_and_hides_archived(
    client, db_session, user_token_headers, normal_user
):
    """Order is ``lower(name)`` and archived projects are excluded by default.

    Catches a plain ``order_by(name)`` (which sorts every lowercase name after
    every uppercase one, scattering the sidebar) and catches the archive filter
    being dropped, which would resurrect archived groups in the picker.
    """
    for name in ("zebra", "Apple", "mango"):
        _make_project(db_session, normal_user, name=name)
    _make_project(db_session, normal_user, name="Boxed", archived=True)

    response = client.get(PROJECTS, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [p["name"] for p in body["projects"]] == ["Apple", "mango", "zebra"]
    assert body["total"] == 3


def test_list_can_include_archived_projects(client, db_session, user_token_headers, normal_user):
    _make_project(db_session, normal_user, name="Boxed", archived=True)

    response = client.get(PROJECTS, headers=user_token_headers, params={"include_archived": True})

    assert response.status_code == status.HTTP_200_OK
    assert [p["name"] for p in response.json()["projects"]] == ["Boxed"]


def test_list_reports_real_conversation_counts(client, db_session, user_token_headers, normal_user):
    """The count comes from ``chat_conversation``, not from a stored column.

    A hardcoded 0 (or a count of every conversation regardless of project) would
    pass a shape-only assertion; two projects with different populations will not.
    """
    busy = _make_project(db_session, normal_user, name="Busy")
    _make_project(db_session, normal_user, name="Quiet")
    _make_conversation(db_session, normal_user, busy)
    _make_conversation(db_session, normal_user, busy)
    _make_conversation(db_session, normal_user, None)  # ungrouped, counts for nobody

    response = client.get(PROJECTS, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    counts = {p["name"]: p["conversation_count"] for p in response.json()["projects"]}
    assert counts == {"Busy": 2, "Quiet": 0}


# ---------------------------------------------------------------------------
# Read one / update
# ---------------------------------------------------------------------------
def test_get_one_returns_the_detail_shape(client, db_session, user_token_headers, normal_user):
    project = _make_project(db_session, normal_user, name="Detail", scope={"tag_names": ["ops"]})
    _make_conversation(db_session, normal_user, project)

    response = client.get(f"{PROJECTS}/{project.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["uuid"] == str(project.uuid)
    assert body["scope"]["tag_names"] == ["ops"]
    assert body["conversation_count"] == 1


def test_patch_applies_only_the_supplied_fields(
    client, db_session, user_token_headers, normal_user
):
    """PATCH is partial: an omitted key must leave the stored value alone."""
    project = _make_project(db_session, normal_user, name="Before")
    project.description = "keep me"
    project.system_prompt = "keep me too"
    db_session.commit()

    response = client.patch(
        f"{PROJECTS}/{project.uuid}", headers=user_token_headers, json={"name": "After"}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["name"] == "After"
    assert body["description"] == "keep me"
    assert body["system_prompt"] == "keep me too"


def test_patch_with_an_empty_system_prompt_clears_the_layer(
    client, db_session, user_token_headers, normal_user
):
    """``""`` clears the prompt layer; ``None`` means "not supplied".

    The handler's ``body.system_prompt or None`` is the whole distinction, and it
    is the only way the UI can remove a project prompt once set.
    """
    project = _make_project(db_session, normal_user, name="Prompted")
    project.system_prompt = "standing instructions"
    db_session.commit()

    response = client.patch(
        f"{PROJECTS}/{project.uuid}", headers=user_token_headers, json={"system_prompt": ""}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["system_prompt"] is None


def test_patch_can_archive_a_project(client, db_session, user_token_headers, normal_user):
    project = _make_project(db_session, normal_user, name="Retiring")

    response = client.patch(
        f"{PROJECTS}/{project.uuid}", headers=user_token_headers, json={"is_archived": True}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["is_archived"] is True
    assert client.get(PROJECTS, headers=user_token_headers).json()["projects"] == []


# ---------------------------------------------------------------------------
# Delete — the ON DELETE SET NULL invariant
# ---------------------------------------------------------------------------
def test_delete_leaves_conversations_ungrouped(client, db_session, user_token_headers, normal_user):
    """Deleting a project must strip the grouping and KEEP the threads.

    This is the invariant most likely to be broken by a future refactor: the
    relationship is deliberately not ``delete-orphan`` and the FK is
    ``ON DELETE SET NULL``. A cascade here destroys a user's chat history, and no
    shape assertion on the 204 would notice.
    """
    project = _make_project(db_session, normal_user, name="Doomed")
    convo = _make_conversation(db_session, normal_user, project)
    convo_id = convo.id

    response = client.delete(f"{PROJECTS}/{project.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_204_NO_CONTENT
    db_session.expire_all()
    survivor = db_session.query(ChatConversation).filter(ChatConversation.id == convo_id).first()
    assert survivor is not None, "deleting a project destroyed its conversation"
    assert survivor.project_id is None
    assert db_session.query(ChatProject).filter(ChatProject.id == project.id).first() is None


def test_delete_is_404_on_a_second_call(client, db_session, user_token_headers, normal_user):
    """Not idempotent by design — the row is gone, so the lookup 404s."""
    project = _make_project(db_session, normal_user, name="Once")

    first = client.delete(f"{PROJECTS}/{project.uuid}", headers=user_token_headers)
    second = client.delete(f"{PROJECTS}/{project.uuid}", headers=user_token_headers)

    assert first.status_code == status.HTTP_204_NO_CONTENT
    assert second.status_code == status.HTTP_404_NOT_FOUND
