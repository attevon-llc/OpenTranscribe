"""Endpoint reads of columns the ORM annotates non-Optional but the DB leaves nullable.

Three separate premises fail here, all of them "the column can't be NULL":

* ``MediaFile.status`` is ``Mapped[FileStatus]`` but declared ``nullable=True``
  with a **Python-side** ``default=FileStatus.PENDING``. Reading ``.status.value``
  off a NULL row is an ``AttributeError``.
* ``upload_time`` / ``created_at`` / ``joined_at`` carry a ``server_default`` and
  no NOT NULL, and were read behind ``assert col is not None  #
  server_default=now()``. A ``server_default`` fills a column only for an INSERT
  that *omits* it, so an explicit ``UPDATE`` (or a raw-SQL insert naming the
  column, or a migration backfill) leaves NULL — and an ``assert`` in production
  code is a 500 with no useful body, which additionally **vanishes under
  ``python -O``**, leaving the raw ``AttributeError``/``TypeError`` behind it.
* ``TopicSuggestion.status`` is ``Mapped[str]`` with ``nullable=False`` in the
  model while the live column is ``is_nullable = YES DEFAULT 'pending'``. The
  DDL wins, and ``str(None)`` shipped the literal string ``"None"`` to clients.

Each test names its site and asserts ONE exact status or value. The NULL is
always written with an explicit ``UPDATE``: passing ``None`` to the constructor
makes the ORM omit the column and the default fills it, so a constructor-written
test cannot fail. ``_null_out``'s trailing assertion is the guard-the-guard,
following ``tests/unit/test_task_detection_service.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi import status
from sqlalchemy import update

from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import Collection
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCollection
from app.models.media import SpeakerProfile
from app.models.sharing import CollectionShare
from app.models.topic import TopicSuggestion

#: The sentinel the endpoints substitute for a NULL timestamp on a required
#: response field. Deliberately not "now": see ``UNKNOWN_TIMESTAMP`` in
#: ``endpoints/groups.py`` and ``endpoints/media_collections.py``.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _null_out(db, model, row_id, *columns: str) -> None:
    """NULL out ``columns`` with an explicit UPDATE, then prove the NULL landed."""
    db.execute(update(model).where(model.id == row_id).values(**dict.fromkeys(columns)))
    db.commit()
    row = db.get(model, row_id)
    db.refresh(row)
    for column in columns:
        assert getattr(row, column) is None, f"{model.__name__}.{column} did not land as NULL"


def _parse(value: str) -> datetime:
    """Parse an API timestamp, accepting either the ``Z`` or ``+00:00`` UTC form."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _make_file(db_session, owner, **kwargs) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "user_id": owner.id,
        "filename": "null_guard.wav",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
    }
    media_file = MediaFile(**{**defaults, **kwargs})
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _make_group(db_session, owner) -> UserGroup:
    group = UserGroup(
        owner_id=owner.id,
        name=f"grp-{uuid.uuid4().hex[:8]}",
        description="null guard group",
    )
    db_session.add(group)
    db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=owner.id, role="owner"))
    db_session.commit()
    db_session.refresh(group)
    return group


# =============================================================================
# user_files.py — MediaFile.status and MediaFile.upload_time
# =============================================================================
def test_my_files_status_lists_a_file_whose_status_is_null(
    client, user_token_headers, normal_user, db_session
):
    """``GET /my-files/status`` must render a NULL-status file, not 500.

    Site: ``user_files.py`` ``get_user_file_status`` — ``"status":
    file.status.value`` in the recent-files loop (the problem-files loop is
    unreachable with a NULL status because its filter compares ``status`` to
    literals, and ``NULL = 'error'`` is never true).

    ``AttributeError`` on ``None.value`` is caught by the handler's broad
    ``except Exception``, which returns 500 — so ONE bad row takes out the whole
    status dashboard for that account, not just its own entry. Against the old
    code this test fails with 500 instead of 200.
    """
    media_file = _make_file(db_session, normal_user, upload_time=datetime.now(UTC))
    _null_out(db_session, MediaFile, media_file.id, "status")

    response = client.get("/api/my-files/status", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    entry = next(
        (f for f in response.json()["recent_files"]["files"] if f["uuid"] == str(media_file.uuid)),
        None,
    )
    assert entry is not None, "the NULL-status file was dropped from recent_files"
    assert entry["status"] is None
    assert entry["display_status"] == "Unknown"
    assert entry["status_badge_class"] == "status-unknown"


def test_my_files_status_lists_an_error_file_whose_upload_time_is_null(
    client, user_token_headers, normal_user, db_session
):
    """``GET /my-files/status`` must render an ERROR file with no ``upload_time``.

    Site: ``user_files.py`` ``get_user_file_status`` — ``assert file.upload_time
    is not None  # filtered on upload_time above`` in the problem-files loop.

    The comment's premise is false for one third of its own filter: the ERROR arm
    is ``MediaFile.status == FileStatus.ERROR`` with **no** predicate on
    ``upload_time``, so a NULL row reaches the loop, the assert fires, and the
    broad handler reports 500 for the entire dashboard. Against the old code this
    test fails with 500.
    """
    media_file = _make_file(db_session, normal_user, status="error")
    _null_out(db_session, MediaFile, media_file.id, "upload_time")

    response = client.get("/api/my-files/status", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    entry = next(
        (f for f in response.json()["problem_files"]["files"] if f["uuid"] == str(media_file.uuid)),
        None,
    )
    assert entry is not None, "the NULL-upload_time ERROR file was dropped from problem_files"
    assert entry["age_hours"] is None
    assert entry["upload_time"] is None


def test_file_detailed_status_reports_a_null_status(
    client, user_token_headers, normal_user, db_session
):
    """``GET /my-files/{uuid}/status`` must report a NULL status as null.

    Site: ``user_files.py`` ``get_file_detailed_status`` — ``"status":
    media_file.status.value``. AttributeError → broad handler → 500, so the
    detail panel for the one file a user is trying to diagnose is the one page
    that cannot load. Against the old code this test fails with 500.
    """
    media_file = _make_file(db_session, normal_user)
    _null_out(db_session, MediaFile, media_file.id, "status")

    response = client.get(f"/api/my-files/{media_file.uuid}/status", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["file"]["status"] is None


def test_file_detailed_status_reports_a_null_upload_time(
    client, user_token_headers, normal_user, db_session
):
    """``GET /my-files/{uuid}/status`` must survive a NULL ``upload_time``.

    Site: ``user_files.py`` ``get_file_detailed_status`` — ``assert
    media_file.upload_time is not None  # server_default=now() on persisted row``.
    This handler resolves the file by UUID with no filter on ``upload_time`` at
    all, so NULL is directly reachable and the AssertionError becomes a 500.
    An unmeasurable age is reported as 0 so the stuck-file heuristic (which needs
    a *measured* hour) cannot accuse the file on the strength of a missing field.
    """
    media_file = _make_file(db_session, normal_user)
    _null_out(db_session, MediaFile, media_file.id, "upload_time")

    response = client.get(f"/api/my-files/{media_file.uuid}/status", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["file"]["upload_time"] is None
    assert body["file_age_hours"] == 0.0
    assert body["is_stuck"] is False


def test_my_files_retry_rejects_a_null_status_with_400(
    client, user_token_headers, normal_user, db_session
):
    """``POST /my-files/{uuid}/retry`` on a NULL-status file must be a 400, not a 500.

    Site: ``user_files.py`` ``retry_file_processing`` — the ``.status.value``
    inside the 400's own detail string.

    A NULL status is not in ``[ERROR, PROCESSING]``, so the guard correctly
    decides to refuse — and then raises AttributeError while *formatting the
    refusal*, which the broad handler converts into an opaque
    "An internal error occurred". The deliberate 400 is downgraded to a 500 by
    the code meant to produce it. Against the old code this test fails with 500.
    """
    media_file = _make_file(db_session, normal_user)
    _null_out(db_session, MediaFile, media_file.id, "status")

    response = client.post(f"/api/my-files/{media_file.uuid}/retry", headers=user_token_headers)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "unknown" in response.json()["detail"]


# =============================================================================
# tasks.py — MediaFile.status
# =============================================================================
def test_tasks_retry_rejects_a_null_status_with_400(
    client, user_token_headers, normal_user, db_session
):
    """``POST /tasks/retry/{uuid}`` on a NULL-status file must be a 400, not a 500.

    Site: ``tasks.py`` ``retry_file_processing`` — ``.status.value`` in the 400
    detail. Same shape as the ``/my-files`` sibling above: the refusal path is
    the only path that can reach the unguarded read.
    """
    media_file = _make_file(db_session, normal_user)
    _null_out(db_session, MediaFile, media_file.id, "status")

    response = client.post(f"/api/tasks/retry/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "unknown" in response.json()["detail"]


def test_fix_inconsistent_file_reports_a_null_status(
    client, admin_token_headers, normal_user, db_session
):
    """``POST /tasks/system/fix-file/{uuid}`` must report a NULL status as null.

    Site: ``tasks.py`` ``fix_inconsistent_file`` — ``"new_status":
    media_file.status.value``. The fix itself succeeds; the response builder is
    what raises, so the operator sees a 500 for a repair that actually ran.
    Against the old code this test fails with 500.
    """
    media_file = _make_file(db_session, normal_user)
    _null_out(db_session, MediaFile, media_file.id, "status")

    response = client.post(
        f"/api/tasks/system/fix-file/{media_file.uuid}", headers=admin_token_headers
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["new_status"] is None


# =============================================================================
# groups.py — UserGroup.created_at / updated_at, UserGroupMember.joined_at
# =============================================================================
def test_list_groups_survives_a_null_created_at(
    client, user_token_headers, normal_user, db_session
):
    """``GET /groups`` must list a group whose ``created_at`` is NULL.

    Site: ``groups.py`` ``list_groups`` — ``assert group.created_at is not None
    # server_default=now()``. ``groups.py`` has no broad ``except`` and the app
    registers no catch-all exception handler, so the AssertionError propagates
    out of the handler: an unhandled 500 in production (the caller's ENTIRE group
    list disappears over one row's missing timestamp) and a raised
    ``AssertionError`` here, since ``TestClient`` re-raises server exceptions.
    Either way this test fails against the old code.
    """
    group = _make_group(db_session, normal_user)
    _null_out(db_session, UserGroup, group.id, "created_at")

    response = client.get("/api/groups", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    entry = next((g for g in response.json() if g["uuid"] == str(group.uuid)), None)
    assert entry is not None, "the NULL-created_at group was dropped from the list"
    assert _parse(entry["created_at"]) == EPOCH


def test_group_detail_survives_null_group_and_membership_timestamps(
    client, user_token_headers, normal_user, db_session
):
    """``GET /groups/{uuid}`` must render with NULL ``created_at``/``updated_at``/``joined_at``.

    Sites: ``groups.py`` ``get_group`` — the two ``assert group.created_at /
    updated_at is not None`` before ``GroupDetail`` and the ``assert m.joined_at
    is not None`` in the member loop. All three feed **required** ``datetime``
    fields on the response schemas, so a NULL cannot simply be forwarded; the
    epoch sentinel keeps the row renderable and is obviously not a real value.
    Against the old code the first assert reached propagates out of the handler
    (unhandled 500 in production; a raised AssertionError under ``TestClient``).
    """
    group = _make_group(db_session, normal_user)
    membership = (
        db_session.query(UserGroupMember)
        .filter(
            UserGroupMember.group_id == group.id,
            UserGroupMember.user_id == normal_user.id,
        )
        .one()
    )
    _null_out(db_session, UserGroup, group.id, "created_at", "updated_at")
    _null_out(db_session, UserGroupMember, membership.id, "joined_at")

    response = client.get(f"/api/groups/{group.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert _parse(body["created_at"]) == EPOCH
    assert _parse(body["updated_at"]) == EPOCH
    member = next(m for m in body["members"] if m["user_uuid"] == str(normal_user.uuid))
    assert _parse(member["joined_at"]) == EPOCH


def test_update_group_survives_a_null_created_at(
    client, user_token_headers, normal_user, db_session
):
    """``PUT /groups/{uuid}`` must respond after renaming a NULL-``created_at`` group.

    Site: ``groups.py`` ``_build_group_response`` — ``assert group.created_at is
    not None``. This is the response builder shared by create and update; only the
    update path can reach it with a NULL, because a freshly inserted row always
    got the server_default. The rename **commits before the assert**, so against
    the old code the caller sees a failure for a change already applied -
    the worst possible failure for a mutating endpoint.
    """
    group = _make_group(db_session, normal_user)
    _null_out(db_session, UserGroup, group.id, "created_at")

    response = client.put(
        f"/api/groups/{group.uuid}",
        headers=user_token_headers,
        json={"name": f"renamed-{uuid.uuid4().hex[:8]}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert _parse(response.json()["created_at"]) == EPOCH


# =============================================================================
# media_collections.py — CollectionShare.created_at / Collection.created_at
# =============================================================================
def test_list_collection_shares_survives_a_null_created_at(
    client, user_token_headers, normal_user, other_user, db_session
):
    """``GET /collections/{uuid}/shares`` must list a share with a NULL ``created_at``.

    Site: ``media_collections.py`` ``_build_share_response`` — ``assert
    share.created_at is not None  # server_default=now()``. ``Share.created_at``
    is a required ``datetime``, so the guard substitutes the epoch sentinel rather
    than forwarding None into Pydantic (which would fail validation and 500 in a
    different way). Against the old code the AssertionError escapes the list
    comprehension, so the owner cannot see - or therefore revoke - ANY of the
    collection's shares.
    """
    collection = Collection(user_id=normal_user.id, name=f"col-{uuid.uuid4().hex[:8]}")
    db_session.add(collection)
    db_session.commit()
    share = CollectionShare(
        collection_id=collection.id,
        shared_by_id=normal_user.id,
        target_type="user",
        target_user_id=other_user.id,
        permission="viewer",
    )
    db_session.add(share)
    db_session.commit()
    db_session.refresh(share)
    _null_out(db_session, CollectionShare, share.id, "created_at")

    response = client.get(f"/api/collections/{collection.uuid}/shares", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    entry = next(s for s in response.json() if s["uuid"] == str(share.uuid))
    assert _parse(entry["created_at"]) == EPOCH


def test_shared_with_me_survives_a_null_share_created_at(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    """``GET /collections/shared-with-me`` must list a share with a NULL ``created_at``.

    Site: ``media_collections.py`` ``list_shared_collections`` — ``assert
    shared_at is not None  # both created_at columns have server_default=now()``.
    Both source columns (``collection_share.created_at`` and
    ``collection.created_at``) are nullable, so neither leg of the fallback
    establishes the premise. Against the old code the AssertionError escapes the
    loop, so the recipient's whole shared-with-me list fails rather than one row.
    """
    collection = Collection(user_id=normal_user.id, name=f"col-{uuid.uuid4().hex[:8]}")
    db_session.add(collection)
    db_session.commit()
    share = CollectionShare(
        collection_id=collection.id,
        shared_by_id=normal_user.id,
        target_type="user",
        target_user_id=other_user.id,
        permission="viewer",
    )
    db_session.add(share)
    db_session.commit()
    db_session.refresh(share)
    _null_out(db_session, CollectionShare, share.id, "created_at")

    response = client.get("/api/collections/shared-with-me", headers=other_user_auth_headers)

    assert response.status_code == status.HTTP_200_OK
    entry = next(c for c in response.json() if c["uuid"] == str(collection.uuid))
    assert _parse(entry["shared_at"]) == EPOCH


# =============================================================================
# speakers.py — Speaker.created_at
# =============================================================================
def test_list_speakers_survives_a_null_created_at(
    client, user_token_headers, normal_user, db_session
):
    """``GET /speakers`` must list a speaker whose ``created_at`` is NULL.

    Site: ``speakers.py`` ``_build_speaker_dict`` — ``assert speaker.created_at is
    not None  # server_default=now()`` guarding ``speaker.created_at.isoformat()``.
    The response is a plain dict, so an unknown creation time is reported as null.
    ``speakers.py`` DOES have a broad ``except Exception``, so against the old code
    the AssertionError is converted into a 500 and the caller's whole speaker list
    disappears over one row's timestamp - this test fails with 500.
    """
    media_file = _make_file(db_session, normal_user)
    speaker = Speaker(
        user_id=normal_user.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="Null Timestamp Speaker",
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    _null_out(db_session, Speaker, speaker.id, "created_at")

    response = client.get("/api/speakers", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    entry = next((s for s in response.json() if s["uuid"] == str(speaker.uuid)), None)
    assert entry is not None, "the NULL-created_at speaker was dropped from the list"
    assert entry["created_at"] is None


# =============================================================================
# speaker_profiles.py — SpeakerProfile / SpeakerCollection timestamps
# =============================================================================
def test_list_speaker_profiles_survives_null_timestamps(
    client, user_token_headers, normal_user, db_session
):
    """``GET /speaker-profiles/profiles`` must list a profile with NULL timestamps.

    Site: ``speaker_profiles.py`` ``list_speaker_profiles`` — ``assert
    profile.created_at is not None`` / ``updated_at`` in the listing loop.

    This pair was NOT in the review's site list but is the same defect: the loop
    iterates rows from a query with no predicate on either column, so both are
    reachable, and ``speaker_profiles.py`` has a broad ``except Exception`` that
    turns the AssertionError into a 500 for the whole list. (The *other* asserts in
    this module sit immediately after ``db.add`` / ``commit`` / ``refresh``, where
    the INSERT really did omit the column and the ``server_default`` really did
    fire — those are dead and deliberately left alone.)
    """
    profile = SpeakerProfile(user_id=normal_user.id, name=f"prof-{uuid.uuid4().hex[:8]}")
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    _null_out(db_session, SpeakerProfile, profile.id, "created_at", "updated_at")

    response = client.get("/api/speaker-profiles/profiles", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    entry = next((p for p in response.json() if p["uuid"] == str(profile.uuid)), None)
    assert entry is not None, "the NULL-timestamp profile was dropped from the list"
    assert entry["created_at"] is None
    assert entry["updated_at"] is None


def test_list_speaker_collections_survives_null_timestamps(
    client, user_token_headers, normal_user, db_session
):
    """``GET /speaker-profiles/collections`` must list a collection with NULL timestamps.

    Site: ``speaker_profiles.py`` ``list_speaker_collections`` — the sibling
    ``assert collection.created_at / updated_at is not None`` pair, same shape and
    same broad handler converting it to a 500.
    """
    collection = SpeakerCollection(user_id=normal_user.id, name=f"sc-{uuid.uuid4().hex[:8]}")
    db_session.add(collection)
    db_session.commit()
    db_session.refresh(collection)
    _null_out(db_session, SpeakerCollection, collection.id, "created_at", "updated_at")

    response = client.get("/api/speaker-profiles/collections", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    entry = next((c for c in response.json() if c["uuid"] == str(collection.uuid)), None)
    assert entry is not None, "the NULL-timestamp speaker collection was dropped from the list"
    assert entry["created_at"] is None
    assert entry["updated_at"] is None


# =============================================================================
# topics.py — TopicSuggestion.status
# =============================================================================
def test_topic_suggestions_report_a_null_status_as_pending(
    client, user_token_headers, normal_user, db_session
):
    """``GET /files/{uuid}/suggestions`` must not ship the string ``"None"``.

    Site: ``topics.py`` ``get_topic_suggestions`` — ``status=str(suggestion.status)``.

    ``TopicSuggestion.status`` is ``Mapped[str]`` with ``nullable=False`` in the
    model, but the live column is ``is_nullable = YES DEFAULT 'pending'`` — the
    DDL is the authority and the annotation is simply wrong. This one is not a
    crash and so had no symptom at all: ``str(None)`` produces the four-character
    string ``"None"``, which validates fine against the schema's ``status: str``
    and reaches the client as a status value no consumer has a branch for. The
    fallback is the DEFAULT the column itself declares. Against the old code this
    test fails with ``"None" != "pending"``.
    """
    media_file = _make_file(db_session, normal_user)
    suggestion = TopicSuggestion(
        media_file_id=media_file.id,
        user_id=normal_user.id,
        suggested_tags=[],
        suggested_collections=[],
        status="reviewed",
    )
    db_session.add(suggestion)
    db_session.commit()
    db_session.refresh(suggestion)
    _null_out(db_session, TopicSuggestion, suggestion.id, "status")

    response = client.get(f"/api/files/{media_file.uuid}/suggestions", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "pending"


def test_topic_suggestions_report_a_real_status_unchanged(
    client, user_token_headers, normal_user, db_session
):
    """Control for the test above: a normal row still reports its real status.

    Without this, the ``"pending"`` assertion would also pass against an
    implementation that hardcoded ``"pending"`` unconditionally — the fallback
    would be indistinguishable from a constant.
    """
    media_file = _make_file(db_session, normal_user)
    suggestion = TopicSuggestion(
        media_file_id=media_file.id,
        user_id=normal_user.id,
        suggested_tags=[],
        suggested_collections=[],
        status="reviewed",
    )
    db_session.add(suggestion)
    db_session.commit()

    response = client.get(f"/api/files/{media_file.uuid}/suggestions", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "reviewed"


def test_my_files_status_reports_a_real_status_unchanged(
    client, user_token_headers, normal_user, db_session
):
    """Control for the NULL-status guards: a real status still round-trips.

    Same reason as above — a guard that returned ``None``/"Unknown"
    unconditionally would satisfy every assertion in this module. This is the
    opposite outcome from the same code path, driven only by the column's value.
    """
    media_file = _make_file(db_session, normal_user, status="error", upload_time=datetime.now(UTC))

    response = client.get("/api/my-files/status", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    entry = next(
        (f for f in response.json()["recent_files"]["files"] if f["uuid"] == str(media_file.uuid)),
        None,
    )
    assert entry is not None
    assert entry["status"] == "error"
    assert entry["display_status"] == "Error"
    assert entry["status_badge_class"] == "status-error"


def test_group_detail_reports_real_timestamps_unchanged(
    client, user_token_headers, normal_user, db_session
):
    """Control for the epoch sentinel: a real ``created_at`` is never replaced.

    A guard written as ``created_at = UNKNOWN_TIMESTAMP`` unconditionally would
    pass every group assertion in this module. This pins that the sentinel
    appears only when the column is NULL.
    """
    group = _make_group(db_session, normal_user)

    response = client.get(f"/api/groups/{group.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    created_at = _parse(response.json()["created_at"])
    assert created_at > datetime.now(UTC) - timedelta(hours=1)
