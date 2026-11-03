"""Gallery bulk ``add_tag`` / ``remove_tag`` through ``POST /api/files/management/bulk-action``.

The batch's tag is resolved once; each file then gets its own outcome (``added``,
``already_present``, ``removed``, ``not_present`` or ``failed``) and its own commit, so one
bad file never undoes or blocks the others. One reindex covers exactly the changed files.
"""

from __future__ import annotations

import pytest
from fixtures.tag_recorders import count_commits
from fixtures.tag_recorders import record_reindex
from fixtures.tag_recorders import share_file
from fixtures.tag_world import attach
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word
from sqlalchemy import event
from sqlalchemy import text

from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.platform_access import TenancyMode
from app.services.platform_bypass import PlatformBypass
from app.services.tag_bulk import ADD_TAG_ACTION
from app.services.tag_bulk import CHANGED_OUTCOMES
from app.services.tag_bulk import REMOVE_TAG_ACTION
from app.services.tag_bulk import REQUIRED_PERMISSION
from app.services.tag_bulk import TAG_ACTIONS
from app.services.tag_bulk import BulkTagOutcome
from app.services.tag_bulk import BulkTagResolution
from app.services.tag_bulk import BulkTagResult
from app.services.tag_bulk import apply_tag_to_file
from app.services.tag_bulk import refresh_tagged_files
from app.services.tag_bulk import resolve_bulk_tag
from app.services.tag_service import InvalidTagNameError

BULK = "/api/files/management/bulk-action"


def _bulk(client, headers, files, action: str, tag_name: str | None) -> list[dict]:
    payload: dict = {"file_uuids": [str(f.uuid) for f in files], "action": action}
    if tag_name is not None:
        payload["tag_name"] = tag_name
    response = client.post(BULK, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    rows: list[dict] = response.json()
    return rows


def _tag_ids_on(db, media_file) -> list[int]:
    db.expire_all()
    return sorted(
        tag_id
        for (tag_id,) in db.query(FileTag.tag_id).filter(FileTag.media_file_id == media_file.id)
    )


def test_the_rails_vocabulary():
    assert (ADD_TAG_ACTION, REMOVE_TAG_ACTION) == ("add_tag", "remove_tag")
    assert frozenset({"add_tag", "remove_tag"}) == TAG_ACTIONS
    assert REQUIRED_PERMISSION == "editor"
    assert [o.value for o in BulkTagOutcome] == [
        "added",
        "already_present",
        "removed",
        "not_present",
        "failed",
    ]
    assert frozenset({BulkTagOutcome.ADDED, BulkTagOutcome.REMOVED}) == CHANGED_OUTCOMES
    assert [BulkTagResult(o, "m").success for o in BulkTagOutcome] == [
        True,
        True,
        True,
        True,
        False,
    ]


def test_add_reports_added_and_already_present_and_leaves_one_association_each(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    reindex = record_reindex(monkeypatch)
    tag = make_tag(db_session, word("Bulk Add"), user_id=normal_user.id)
    carrying = make_file(db_session, normal_user)
    fresh_a, fresh_b = make_file(db_session, normal_user), make_file(db_session, normal_user)
    attach(db_session, carrying, tag)

    rows = _bulk(
        client, user_token_headers, [fresh_a, carrying, fresh_b], "add_tag", tag.name.upper()
    )

    assert [(r["file_uuid"], r["outcome"], r["success"]) for r in rows] == [
        (str(fresh_a.uuid), "added", True),
        (str(carrying.uuid), "already_present", True),
        (str(fresh_b.uuid), "added", True),
    ]
    assert rows[0]["message"] == f"Tag '{tag.name}' added"
    assert rows[1]["message"] == f"File already carried tag '{tag.name}'"
    assert [_tag_ids_on(db_session, f) for f in (fresh_a, carrying, fresh_b)] == [[tag.id]] * 3
    assert db_session.query(Tag).filter(Tag.normalized_name == tag.normalized_name).count() == 1
    assert reindex.calls == [[fresh_a.id, fresh_b.id]]


def test_add_of_a_new_name_creates_one_tag_for_the_whole_batch(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    record_reindex(monkeypatch)
    files = [make_file(db_session, normal_user) for _ in range(3)]
    name = word("Batch New")

    rows = _bulk(client, user_token_headers, files, "add_tag", name)

    created = db_session.query(Tag).filter(Tag.name == name).all()
    assert len(created) == 1
    assert (created[0].user_id, created[0].source) == (normal_user.id, "manual")
    assert [r["outcome"] for r in rows] == ["added"] * 3
    assert [_tag_ids_on(db_session, f) for f in files] == [[created[0].id]] * 3


def test_remove_reports_removed_and_not_present(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    reindex = record_reindex(monkeypatch)
    tag = make_tag(db_session, word("Bulk Rm"), user_id=normal_user.id)
    carrying, bare = make_file(db_session, normal_user), make_file(db_session, normal_user)
    attach(db_session, carrying, tag)

    rows = _bulk(client, user_token_headers, [carrying, bare], "remove_tag", tag.name)

    assert [(r["outcome"], r["success"], r["message"]) for r in rows] == [
        ("removed", True, f"Tag '{tag.name}' removed"),
        ("not_present", True, f"File did not carry tag '{tag.name}'"),
    ]
    assert _tag_ids_on(db_session, carrying) == []
    assert reindex.calls == [[carrying.id]]


def test_remove_of_an_unknown_name_creates_nothing(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    reindex = record_reindex(monkeypatch)
    media_file = make_file(db_session, normal_user)
    name = word("Typo")

    rows = _bulk(client, user_token_headers, [media_file], "remove_tag", name)

    assert [(r["outcome"], r["success"], r["message"]) for r in rows] == [
        ("not_present", True, "No such tag; nothing to remove")
    ]
    assert db_session.query(Tag).filter(Tag.name == name).count() == 0
    assert reindex.calls == []


@pytest.mark.parametrize("tag_name", [None, "", " - "])
def test_a_missing_or_blank_tag_name_is_a_422(
    client, db_session, normal_user, user_token_headers, tag_name
):
    media_file = make_file(db_session, normal_user)
    payload: dict = {"file_uuids": [str(media_file.uuid)], "action": "add_tag"}
    if tag_name is not None:
        payload["tag_name"] = tag_name
    response = client.post(BULK, headers=user_token_headers, json=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": "Tag name is required"}


def test_a_viewer_shared_file_fails_alone_and_an_editor_shared_one_is_tagged(
    client, db_session, normal_user, other_user, user_token_headers, monkeypatch
):
    reindex = record_reindex(monkeypatch)
    viewer_file = make_file(db_session, other_user)
    editor_file = make_file(db_session, other_user)
    own_file = make_file(db_session, normal_user)
    share_file(db_session, other_user, normal_user, viewer_file, "viewer")
    share_file(db_session, other_user, normal_user, editor_file, "editor")
    name = word("Shared Edit")

    rows = _bulk(client, user_token_headers, [viewer_file, editor_file, own_file], "add_tag", name)

    assert rows[0] == {
        "file_uuid": str(viewer_file.uuid),
        "success": False,
        "message": "Requires editor permission on this file",
        "error": "HTTP_ERROR",
        "outcome": "failed",
    }
    assert [(r["outcome"], r["success"]) for r in rows[1:]] == [("added", True), ("added", True)]
    assert _tag_ids_on(db_session, viewer_file) == []
    assert reindex.calls == [[editor_file.id, own_file.id]]


def test_a_database_error_on_one_file_fails_only_that_file(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    reindex = record_reindex(monkeypatch)
    before, poisoned, after = (make_file(db_session, normal_user) for _ in range(3))
    tag = make_tag(db_session, word("Poison"), user_id=normal_user.id)

    def poison(session, flush_context, instances):
        if any(
            isinstance(obj, FileTag) and obj.media_file_id == poisoned.id for obj in session.new
        ):
            session.connection().execute(text("SELECT 1/0"))

    event.listen(db_session, "before_flush", poison)
    try:
        rows = _bulk(client, user_token_headers, [before, poisoned, after], "add_tag", tag.name)
    finally:
        event.remove(db_session, "before_flush", poison)

    assert [r["outcome"] for r in rows] == ["added", "failed", "added"]
    assert rows[1]["error"] == "TAG_UPDATE_FAILED"
    assert rows[1]["message"].startswith("Tag change failed (")
    assert "1/0" not in rows[1]["message"] and "division" not in rows[1]["message"].lower()
    assert [_tag_ids_on(db_session, f) for f in (before, poisoned, after)] == [
        [tag.id],
        [],
        [tag.id],
    ]
    assert reindex.calls == [[before.id, after.id]]


# ---------------------------------------------------------------------------
# Service level
# ---------------------------------------------------------------------------


def test_resolving_for_remove_never_creates_and_for_add_commits(
    db_session, normal_user, monkeypatch
):
    commits = count_commits(monkeypatch, db_session)
    name = word("Resolve")

    missing = resolve_bulk_tag(
        db_session, "remove_tag", name, user_id=normal_user.id, organization_id=None
    )
    assert missing == BulkTagResolution(None)
    assert db_session.query(Tag).filter(Tag.name == name).count() == 0
    assert commits == []

    added = resolve_bulk_tag(
        db_session, "add_tag", name, user_id=normal_user.id, organization_id=None
    )
    assert added.tag is not None and added.tag.name == name
    assert added.created_tag_id == added.tag.id
    assert commits == [1]
    again = resolve_bulk_tag(
        db_session, "remove_tag", name.upper(), user_id=normal_user.id, organization_id=None
    )
    assert again == BulkTagResolution(added.tag)
    existing = resolve_bulk_tag(
        db_session, "add_tag", name, user_id=normal_user.id, organization_id=None
    )
    assert existing == BulkTagResolution(added.tag)

    with pytest.raises(InvalidTagNameError):
        resolve_bulk_tag(db_session, "add_tag", None, user_id=normal_user.id, organization_id=None)


def test_closing_a_batch_that_created_no_tag_deletes_none(db_session, normal_user, monkeypatch):
    """Only the batch that inserted a tag may discard it, even on a shared session."""
    record_reindex(monkeypatch)
    name = word("Other Batch")
    resolve_bulk_tag(db_session, "add_tag", name, user_id=normal_user.id, organization_id=None)

    refresh_tagged_files(db_session, [], user_id=normal_user.id)

    db_session.expire_all()
    assert db_session.query(Tag).filter(Tag.name == name).count() == 1


def test_a_strangers_file_is_refused_per_file_with_the_permission_code(
    db_session, normal_user, other_user
):
    tag = make_tag(db_session, word("Stranger"), user_id=normal_user.id)
    theirs = make_file(db_session, other_user)

    result = apply_tag_to_file(
        db_session, file_id=theirs.id, tag=tag, add=True, user_id=normal_user.id
    )

    assert result == BulkTagResult(
        BulkTagOutcome.FAILED,
        message="Not authorized to access this file",
        error="PERMISSION_DENIED",
    )
    assert _tag_ids_on(db_session, theirs) == []


def test_a_platform_admin_bypass_may_tag_any_users_file(db_session, admin_user, other_user):
    tag = make_tag(db_session, word("Bypass"), user_id=None)
    theirs = make_file(db_session, other_user)
    bypass = PlatformBypass(user=admin_user, mode=TenancyMode.SINGLE)

    result = apply_tag_to_file(
        db_session, file_id=theirs.id, tag=tag, add=True, user_id=admin_user.id, bypass=bypass
    )

    assert result.outcome is BulkTagOutcome.ADDED
    assert _tag_ids_on(db_session, theirs) == [tag.id]


def test_an_add_where_every_file_fails_leaves_no_new_tag(
    client, db_session, normal_user, other_user, user_token_headers, monkeypatch
):
    reindex = record_reindex(monkeypatch)
    viewer_file = make_file(db_session, other_user)
    strangers_file = make_file(db_session, other_user)
    share_file(db_session, other_user, normal_user, viewer_file, "viewer")
    name = word("All Fail")

    rows = _bulk(client, user_token_headers, [viewer_file, strangers_file], "add_tag", name)

    assert [r["outcome"] for r in rows] == ["failed", "failed"]
    db_session.expire_all()
    assert db_session.query(Tag).filter(Tag.name == name).count() == 0
    assert reindex.calls == []


def test_an_add_where_some_files_fail_keeps_the_one_new_tag(
    client, db_session, normal_user, other_user, user_token_headers, monkeypatch
):
    record_reindex(monkeypatch)
    viewer_file = make_file(db_session, other_user)
    own_file = make_file(db_session, normal_user)
    share_file(db_session, other_user, normal_user, viewer_file, "viewer")
    name = word("Some Fail")

    rows = _bulk(client, user_token_headers, [viewer_file, own_file], "add_tag", name)

    assert [r["outcome"] for r in rows] == ["failed", "added"]
    db_session.expire_all()
    created = db_session.query(Tag).filter(Tag.name == name).all()
    assert len(created) == 1
    assert _tag_ids_on(db_session, own_file) == [created[0].id]


def test_an_add_of_an_existing_name_where_every_file_fails_keeps_that_tag(
    client, db_session, normal_user, other_user, user_token_headers, monkeypatch
):
    record_reindex(monkeypatch)
    tag = make_tag(db_session, word("Kept"), user_id=normal_user.id)
    strangers_file = make_file(db_session, other_user)

    rows = _bulk(client, user_token_headers, [strangers_file], "add_tag", tag.name)

    assert [r["outcome"] for r in rows] == ["failed"]
    db_session.expire_all()
    assert db_session.query(Tag).filter(Tag.id == tag.id).count() == 1


def test_a_lost_creation_race_never_deletes_the_winning_tag(
    client, db_session, normal_user, other_user, user_token_headers, monkeypatch
):
    """The batch's lookup misses a row another request just committed; that row is not ours."""
    from app.services import tag_bulk

    record_reindex(monkeypatch)
    winner = make_tag(db_session, word("Race Won"), user_id=normal_user.id)
    strangers_file = make_file(db_session, other_user)
    real_lookup = tag_bulk.lookup_existing_tag
    lookups: list[int] = []

    def miss_once(*args, **kwargs):
        lookups.append(1)
        return None if len(lookups) == 1 else real_lookup(*args, **kwargs)

    monkeypatch.setattr(tag_bulk, "lookup_existing_tag", miss_once)

    rows = _bulk(client, user_token_headers, [strangers_file], "add_tag", winner.name)

    assert lookups == [1]
    assert [r["outcome"] for r in rows] == ["failed"]
    db_session.expire_all()
    assert db_session.query(Tag).filter(Tag.id == winner.id).count() == 1
    assert db_session.query(Tag).filter(Tag.name == winner.name).count() == 1
