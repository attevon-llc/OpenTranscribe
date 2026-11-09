"""Comment permission matrix: every way a person can reach a file, every comment verb.

A user in an organization reported they could not add a comment on the file detail page.
The owner's rule is that every feature works for organization members and for individual
users alike, so this file walks the matrix rather than one happy path: a personal user on
their own file, an organization member on their own organization file, a member on another
member's file reached through an organization collection or a viewer/editor share, an
organization admin, a personal user on a file shared with them, and a file under a legal
hold. For each reachable cell it lists, creates (both create routes), edits and deletes; on the
held file the edit and delete are refused (a hold freezes the file's content, v0.7.0).

Requests run as a named user in a named tenant by overriding the two auth dependencies, the
same shape ``tests/api/test_tag_tenancy.py`` uses, so the org context is exactly the one the
cell names.
"""

from __future__ import annotations

import uuid as uuid_pkg
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.sharing import CollectionShare
from app.models.user import User
from tests.fixtures.tag_world import join

COMMENTS = "/api/comments"


def _mk_user(db, label: str) -> User:
    user = User(
        email=f"cm_{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
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


def _mk_org(db) -> Organization:
    org = Organization(
        external_org_id=f"org_cm_{uuid_pkg.uuid4().hex[:8]}", name="Comment Org", is_active=True
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _mk_file(db, owner: User, org_id: int | None, *, legal_hold: bool = False) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=fuuid,
        filename=f"cm_{str(fuuid)[:8]}.wav",
        storage_path=f"media/test/{fuuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        user_id=owner.id,
        organization_id=org_id,
        status="completed",
        legal_hold=legal_hold,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _mk_collection(
    db, owner: User, org_id: int | None, media_file: MediaFile, *, visibility: str | None = None
) -> Collection:
    kwargs = {"visibility": visibility} if visibility else {}
    coll = Collection(
        name=f"cm coll {uuid_pkg.uuid4().hex[:6]}",
        user_id=owner.id,
        organization_id=org_id,
        **kwargs,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    db.add(CollectionMember(collection_id=coll.id, media_file_id=media_file.id))
    db.commit()
    return coll


def _share(db, coll: Collection, by: User, to: User, permission: str) -> None:
    db.add(
        CollectionShare(
            collection_id=coll.id,
            shared_by_id=by.id,
            target_type="user",
            target_user_id=to.id,
            permission=permission,
        )
    )
    db.commit()


@contextmanager
def _acting_as(user: User, org_id: int | None, org_role: str | None = None) -> Iterator[None]:
    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user
    from app.main import app
    from app.services.request_context import RequestContext

    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=user, org_id=org_id, org_role=org_role if org_id is not None else None
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_context, None)


@dataclass
class Cell:
    actor: User
    org_id: int | None
    org_role: str | None
    media_file: MediaFile


def _cell(db_session, name: str) -> Cell:
    """Build the world for one matrix cell and return who acts on which file."""
    db = db_session
    if name == "personal_own_file":
        me = _mk_user(db, "solo")
        return Cell(me, None, None, _mk_file(db, me, None))
    if name == "personal_shared_viewer":
        owner, me = _mk_user(db, "owner"), _mk_user(db, "friend")
        mf = _mk_file(db, owner, None)
        _share(db, _mk_collection(db, owner, None, mf), owner, me, "viewer")
        return Cell(me, None, None, mf)
    if name == "personal_shared_editor":
        owner, me = _mk_user(db, "owner"), _mk_user(db, "friend")
        mf = _mk_file(db, owner, None)
        _share(db, _mk_collection(db, owner, None, mf), owner, me, "editor")
        return Cell(me, None, None, mf)
    if name == "legal_hold_own_file":
        me = _mk_user(db, "held")
        return Cell(me, None, None, _mk_file(db, me, None, legal_hold=True))

    org = _mk_org(db)
    author, me = _mk_user(db, "author"), _mk_user(db, "member")
    join(db, org, author)
    if name == "org_member_own_file":
        join(db, org, me)
        return Cell(me, org.id, "org:member", _mk_file(db, me, org.id))
    mf = _mk_file(db, author, org.id)
    if name == "org_member_via_org_collection":
        join(db, org, me)
        _mk_collection(db, author, org.id, mf)
        return Cell(me, org.id, "org:member", mf)
    if name == "org_member_viewer_share":
        join(db, org, me)
        coll = _mk_collection(db, author, org.id, mf, visibility="restricted")
        _share(db, coll, author, me, "viewer")
        return Cell(me, org.id, "org:member", mf)
    if name == "org_member_editor_share":
        join(db, org, me)
        coll = _mk_collection(db, author, org.id, mf, visibility="restricted")
        _share(db, coll, author, me, "editor")
        return Cell(me, org.id, "org:member", mf)
    if name == "org_admin_via_org_collection":
        join(db, org, me, role="org:admin")
        _mk_collection(db, author, org.id, mf)
        return Cell(me, org.id, "org:admin", mf)
    raise AssertionError(f"unknown cell {name}")


CELLS = [
    "personal_own_file",
    "personal_shared_viewer",
    "personal_shared_editor",
    "legal_hold_own_file",
    "org_member_own_file",
    "org_member_via_org_collection",
    "org_member_viewer_share",
    "org_member_editor_share",
    "org_admin_via_org_collection",
]


#: Cells whose file is under a legal hold (its existing comments are frozen).
HELD_CELLS = frozenset({"legal_hold_own_file"})


@pytest.mark.parametrize("cell_name", CELLS)
class TestCommentMatrix:
    def test_detail_page_is_readable(self, client, db_session, cell_name):
        """Control: the cell can open the file detail page, so commenting must work too."""
        cell = _cell(db_session, cell_name)
        with _acting_as(cell.actor, cell.org_id, cell.org_role):
            response = client.get(f"/api/files/{cell.media_file.uuid}/info")
        assert response.status_code == 200, response.text

    def test_add_list_edit_delete_via_nested_route(self, client, db_session, cell_name):
        """The exact calls ``CommentSection.svelte`` makes."""
        cell = _cell(db_session, cell_name)
        base = f"{COMMENTS}/files/{cell.media_file.uuid}/comments"
        with _acting_as(cell.actor, cell.org_id, cell.org_role):
            created = client.post(
                base,
                json={
                    "text": "a note",
                    "timestamp": 3.5,
                    "media_file_id": str(cell.media_file.uuid),
                },
            )
            assert created.status_code == 200, created.text
            body = created.json()
            assert body["text"] == "a note"
            assert body["user_id"] == str(cell.actor.uuid)
            assert body["user"]["uuid"] == str(cell.actor.uuid)
            assert body["media_file_id"] == str(cell.media_file.uuid)

            listed = client.get(base)
            assert listed.status_code == 200, listed.text
            assert [c["uuid"] for c in listed.json()] == [body["uuid"]]

            edited = client.put(f"{COMMENTS}/{body['uuid']}", json={"text": "edited"})
            deleted = client.delete(f"{COMMENTS}/{body['uuid']}")
        remaining = (
            db_session.query(Comment).filter(Comment.media_file_id == cell.media_file.id).count()
        )
        if cell_name in HELD_CELLS:
            # A legal hold freezes the file's content: a new comment is allowed (and recorded),
            # but an existing one can be neither changed nor removed.
            for refused in (edited, deleted):
                assert refused.status_code == 409, refused.text
                assert refused.json()["detail"]["error"] == "FILE_UNDER_LEGAL_HOLD"
            assert remaining == 1
            return
        assert edited.status_code == 200, edited.text
        assert edited.json()["text"] == "edited"
        assert deleted.status_code == 204, deleted.text
        assert remaining == 0

    def test_add_without_a_timestamp(self, client, db_session, cell_name):
        """A general comment (no timestamp) is a valid comment: the column is nullable."""
        cell = _cell(db_session, cell_name)
        base = f"{COMMENTS}/files/{cell.media_file.uuid}/comments"
        with _acting_as(cell.actor, cell.org_id, cell.org_role):
            created = client.post(base, json={"text": "general", "timestamp": None})
        assert created.status_code == 200, created.text
        assert created.json()["timestamp"] is None

    def test_add_and_list_via_standalone_route(self, client, db_session, cell_name):
        cell = _cell(db_session, cell_name)
        with _acting_as(cell.actor, cell.org_id, cell.org_role):
            created = client.post(
                COMMENTS,
                json={
                    "text": "fallback",
                    "timestamp": 1.0,
                    "media_file_id": str(cell.media_file.uuid),
                },
            )
            assert created.status_code == 200, created.text
            listed = client.get(COMMENTS, params={"media_file_id": str(cell.media_file.uuid)})
            assert listed.status_code == 200, listed.text
            assert [c["uuid"] for c in listed.json()] == [created.json()["uuid"]]
            single = client.get(f"{COMMENTS}/{created.json()['uuid']}")
            assert single.status_code == 200, single.text
