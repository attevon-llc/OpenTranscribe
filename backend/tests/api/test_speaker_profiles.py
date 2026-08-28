"""Characterization tests for ``api/endpoints/speaker_profiles.py``.

Wave-3 (speakers domain). Pins the CURRENT observable behavior of the speaker
profile + speaker collection endpoints (prefix ``/api/speaker-profiles``):

- ``GET/POST/PUT/DELETE /profiles``                    (profile CRUD)
- ``GET  /profiles/{uuid}/occurrences``                (occurrences read)
- ``POST/DELETE /profiles/{uuid}/avatar``              (avatar upload validation)
- ``POST /profiles/{uuid}/confirm-gender``             (gender confirm)
- ``POST /speakers/{uuid}/assign-profile``             (assignment)
- ``GET  /speakers/{uuid}/suggestions``                (suggestions read)
- ``GET/POST /collections``                            (speaker collection CRUD)

This module was JUST refactored (require_resource_owner adoption, ErrorHandler,
and the 403-unmasking fix in ``list_speaker_profiles``) — these tests
characterize the committed behavior. The 403 ownership contracts are already
pinned in ``test_ownership_contracts.py``; here we add the functional coverage
(happy paths, 401/404/422/400 validation, name-conflict, avatar type/size gates)
on savepoint rows. MinIO is live (no mocking the avatar upload path).

Run: ``venv/bin/pytest tests/api/test_speaker_profiles.py -v -n0``
"""

from __future__ import annotations

import io
import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCollection
from app.models.media import SpeakerProfile

PREFIX = "/api/speaker-profiles"


def _make_profile(db_session, owner, *, name=None) -> SpeakerProfile:
    prof = SpeakerProfile(user_id=owner.id, name=name or f"Profile {uuid.uuid4().hex[:6]}")
    db_session.add(prof)
    db_session.commit()
    db_session.refresh(prof)
    return prof


def _make_collection(db_session, owner, *, name=None) -> SpeakerCollection:
    col = SpeakerCollection(user_id=owner.id, name=name or f"SpkCol {uuid.uuid4().hex[:6]}")
    db_session.add(col)
    db_session.commit()
    db_session.refresh(col)
    return col


def _make_file_and_speaker(db_session, owner):
    mf = MediaFile(
        user_id=owner.id,
        filename="prof.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    spk = Speaker(user_id=owner.id, media_file_id=mf.id, name="SPEAKER_00")
    db_session.add(spk)
    db_session.commit()
    db_session.refresh(spk)
    return mf, spk


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------


def test_list_profiles_unauthorized(client):
    assert client.get(f"{PREFIX}/profiles").status_code == status.HTTP_401_UNAUTHORIZED


def test_list_profiles_owner_sees_own(client, user_token_headers, normal_user, db_session):
    prof = _make_profile(db_session, normal_user, name="MyProfile")
    resp = client.get(f"{PREFIX}/profiles", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    by_uuid = {p["uuid"]: p for p in resp.json()}
    assert str(prof.uuid) in by_uuid
    item = by_uuid[str(prof.uuid)]
    assert item["name"] == "MyProfile"
    assert item["is_shared"] is False
    assert item["owner_name"] is None


def test_list_profiles_excludes_other_users_private(
    client, other_user_auth_headers, normal_user, db_session
):
    prof = _make_profile(db_session, normal_user)
    resp = client.get(f"{PREFIX}/profiles", headers=other_user_auth_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert str(prof.uuid) not in {p["uuid"] for p in resp.json()}


def test_create_profile_200_roundtrip(client, user_token_headers, normal_user, db_session):
    name = f"Created {uuid.uuid4().hex[:6]}"
    resp = client.post(
        f"{PREFIX}/profiles",
        headers=user_token_headers,
        params={"name": name, "description": "desc"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    body = resp.json()
    assert body["name"] == name
    assert body["description"] == "desc"
    assert "uuid" in body


def test_create_profile_duplicate_name_400(client, user_token_headers, normal_user, db_session):
    prof = _make_profile(db_session, normal_user, name="DupeName")
    resp = client.post(f"{PREFIX}/profiles", headers=user_token_headers, params={"name": prof.name})
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Speaker profile with this name already exists"


def test_create_profile_missing_name_422(client, user_token_headers):
    """``name`` is a required query param → 422 when absent."""
    resp = client.post(f"{PREFIX}/profiles", headers=user_token_headers)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_update_profile_owner_200(client, user_token_headers, normal_user, db_session):
    prof = _make_profile(db_session, normal_user)
    resp = client.put(
        f"{PREFIX}/profiles/{prof.uuid}",
        headers=user_token_headers,
        params={"name": "Renamed", "description": "new desc"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["description"] == "new desc"


def test_update_profile_name_conflict_400(client, user_token_headers, normal_user, db_session):
    _make_profile(db_session, normal_user, name="Taken")
    prof = _make_profile(db_session, normal_user, name="Original")
    resp = client.put(
        f"{PREFIX}/profiles/{prof.uuid}",
        headers=user_token_headers,
        params={"name": "Taken"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Speaker profile with this name already exists"


def test_update_profile_nonexistent_404(client, user_token_headers):
    resp = client.put(
        f"{PREFIX}/profiles/{uuid.uuid4()}",
        headers=user_token_headers,
        params={"name": "x"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Speaker profile not found"


def test_update_profile_malformed_uuid_400(client, user_token_headers):
    resp = client.put(
        f"{PREFIX}/profiles/not-a-uuid", headers=user_token_headers, params={"name": "x"}
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Invalid UUID format: not-a-uuid"


def test_delete_profile_owner_204(client, user_token_headers, normal_user, db_session):
    prof = _make_profile(db_session, normal_user)
    resp = client.delete(f"{PREFIX}/profiles/{prof.uuid}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert db_session.query(SpeakerProfile).filter(SpeakerProfile.id == prof.id).first() is None


def test_delete_profile_nonexistent_404(client, user_token_headers):
    resp = client.delete(f"{PREFIX}/profiles/{uuid.uuid4()}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Speaker profile not found"


def test_delete_profile_unassigns_speakers(client, user_token_headers, normal_user, db_session):
    """Deleting a profile unassigns its speakers (profile_id -> None) not delete."""
    prof = _make_profile(db_session, normal_user)
    mf, spk = _make_file_and_speaker(db_session, normal_user)
    spk.profile_id = prof.id
    db_session.commit()
    spk_id = spk.id

    resp = client.delete(f"{PREFIX}/profiles/{prof.uuid}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    db_session.expire_all()
    surviving = db_session.query(Speaker).filter(Speaker.id == spk_id).first()
    assert surviving is not None
    assert surviving.profile_id is None


# ---------------------------------------------------------------------------
# Occurrences
# ---------------------------------------------------------------------------


def test_occurrences_owner_200_empty(client, user_token_headers, normal_user, db_session):
    """A profile with no linked speakers returns an empty occurrences list."""
    prof = _make_profile(db_session, normal_user)
    resp = client.get(f"{PREFIX}/profiles/{prof.uuid}/occurrences", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == []


def test_occurrences_nonexistent_404(client, user_token_headers):
    resp = client.get(f"{PREFIX}/profiles/{uuid.uuid4()}/occurrences", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Speaker profile not found"


# ---------------------------------------------------------------------------
# confirm-gender
# ---------------------------------------------------------------------------


def test_confirm_gender_owner_200(client, user_token_headers, normal_user, db_session):
    prof = _make_profile(db_session, normal_user)
    resp = client.post(
        f"{PREFIX}/profiles/{prof.uuid}/confirm-gender",
        headers=user_token_headers,
        params={"gender": "female"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert resp.json()["predicted_gender"] == "female"


def test_confirm_gender_invalid_value_400(client, user_token_headers, normal_user, db_session):
    """An out-of-domain gender value → 400 (validated in-body, BEFORE the 404/403
    lookup), so even a nonexistent profile returns 400 here."""
    resp = client.post(
        f"{PREFIX}/profiles/{uuid.uuid4()}/confirm-gender",
        headers=user_token_headers,
        params={"gender": "other"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Gender must be 'male' or 'female'"


def test_confirm_gender_missing_param_422(client, user_token_headers, normal_user, db_session):
    prof = _make_profile(db_session, normal_user)
    resp = client.post(f"{PREFIX}/profiles/{prof.uuid}/confirm-gender", headers=user_token_headers)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# Avatar upload validation (MinIO live; we exercise the validation gates that
# reject BEFORE any storage I/O so no real object is created)
# ---------------------------------------------------------------------------


def test_avatar_upload_invalid_type_400(client, user_token_headers, normal_user, db_session):
    prof = _make_profile(db_session, normal_user)
    files = {"file": ("note.txt", io.BytesIO(b"not an image"), "text/plain")}
    resp = client.post(
        f"{PREFIX}/profiles/{prof.uuid}/avatar", headers=user_token_headers, files=files
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"].startswith("Invalid file type")


def test_avatar_upload_too_large_400(client, user_token_headers, normal_user, db_session):
    prof = _make_profile(db_session, normal_user)
    big = io.BytesIO(b"\x00" * (2 * 1024 * 1024 + 1))
    files = {"file": ("big.png", big, "image/png")}
    resp = client.post(
        f"{PREFIX}/profiles/{prof.uuid}/avatar", headers=user_token_headers, files=files
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "File too large. Maximum size is 2MB."


def test_avatar_upload_nonexistent_profile_404(client, user_token_headers):
    files = {"file": ("a.png", io.BytesIO(b"\x89PNG"), "image/png")}
    resp = client.post(
        f"{PREFIX}/profiles/{uuid.uuid4()}/avatar", headers=user_token_headers, files=files
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Speaker profile not found"


def test_delete_avatar_owner_204_noop(client, user_token_headers, normal_user, db_session):
    """Deleting an avatar on a profile that has none is a no-op 204."""
    prof = _make_profile(db_session, normal_user)
    resp = client.delete(f"{PREFIX}/profiles/{prof.uuid}/avatar", headers=user_token_headers)
    assert resp.status_code == status.HTTP_204_NO_CONTENT


# ---------------------------------------------------------------------------
# assign-profile + suggestions
# ---------------------------------------------------------------------------


def test_assign_profile_unknown_speaker_404(client, user_token_headers, normal_user, db_session):
    prof = _make_profile(db_session, normal_user)
    resp = client.post(
        f"{PREFIX}/speakers/{uuid.uuid4()}/assign-profile",
        headers=user_token_headers,
        params={"profile_uuid": str(prof.uuid)},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Speaker not found"


def test_assign_profile_other_user_speaker_403(
    client, other_user_auth_headers, normal_user, db_session
):
    """A speaker on someone else's file → 403 'Not authorized to access this
    speaker' (file-permission gate at :319)."""
    prof = _make_profile(db_session, normal_user)
    _mf, spk = _make_file_and_speaker(db_session, normal_user)
    resp = client.post(
        f"{PREFIX}/speakers/{spk.uuid}/assign-profile",
        headers=other_user_auth_headers,
        params={"profile_uuid": str(prof.uuid)},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Not authorized to access this speaker"


def test_assign_profile_unknown_profile_404(client, user_token_headers, normal_user, db_session):
    _mf, spk = _make_file_and_speaker(db_session, normal_user)
    resp = client.post(
        f"{PREFIX}/speakers/{spk.uuid}/assign-profile",
        headers=user_token_headers,
        params={"profile_uuid": str(uuid.uuid4())},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Speaker profile not found"


# ---------------------------------------------------------------------------
# assign-profile: post-commit region atomicity (issue #620 item 8)
#
# assign_speaker_to_profile (the SERVICE call) commits the DB write before this
# handler builds its response / calls update_speaker_collections. A failure in
# either of those AFTER that commit must never surface as a 500 -- the client
# would be told the assignment did not happen when it durably did, and the
# handler's `db.rollback()` cannot undo an already-committed write anyway.
# ---------------------------------------------------------------------------


def test_assign_profile_happy_path_200(client, user_token_headers, normal_user, db_session):
    """Baseline positive control the failure-mode tests below are contrasted against."""
    prof = _make_profile(db_session, normal_user)
    _mf, spk = _make_file_and_speaker(db_session, normal_user)
    resp = client.post(
        f"{PREFIX}/speakers/{spk.uuid}/assign-profile",
        headers=user_token_headers,
        params={"profile_uuid": str(prof.uuid)},
    )
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["status"] == "assigned"
    assert body["speaker_id"] == str(spk.uuid)
    db_session.refresh(spk)
    assert spk.profile_id == prof.id
    assert spk.verified is True


def test_assign_profile_opensearch_failure_still_returns_200_and_writes_durably(
    client, user_token_headers, normal_user, db_session
):
    """Red-first: before this fix, an exception here (e.g. an expired ORM attribute
    read) propagated out of the try/except as a 500 -- even though
    assign_speaker_to_profile had already committed the assignment. The client was
    told the assignment failed when it had, in fact, durably succeeded.
    """
    from unittest.mock import patch

    prof = _make_profile(db_session, normal_user)
    _mf, spk = _make_file_and_speaker(db_session, normal_user)

    with patch(
        "app.api.endpoints.speaker_profiles.update_speaker_collections",
        side_effect=RuntimeError("OpenSearch is down"),
    ):
        resp = client.post(
            f"{PREFIX}/speakers/{spk.uuid}/assign-profile",
            headers=user_token_headers,
            params={"profile_uuid": str(prof.uuid)},
        )

    assert resp.status_code == status.HTTP_200_OK, (
        f"a post-commit OpenSearch failure must not surface as a 500: {resp.text}"
    )
    body = resp.json()
    assert body["status"] == "assigned"

    # The durable state is what matters: the commit already happened inside
    # assign_speaker_to_profile, independent of this response.
    db_session.refresh(spk)
    assert spk.profile_id == prof.id, "the assignment must be committed despite the failure"
    assert spk.verified is True, "verified must be committed despite the failure"


def test_assign_profile_opensearch_failure_response_does_not_claim_rollback(
    client, user_token_headers, normal_user, db_session
):
    """Negative control: the degraded response must not resemble the normal success
    body (which includes profile_name/confidence/verified) -- it is a distinct,
    minimal shape so a caller cannot mistake it for the full happy path.
    """
    from unittest.mock import patch

    prof = _make_profile(db_session, normal_user)
    _mf, spk = _make_file_and_speaker(db_session, normal_user)

    with patch(
        "app.api.endpoints.speaker_profiles.update_speaker_collections",
        side_effect=RuntimeError("OpenSearch is down"),
    ):
        resp = client.post(
            f"{PREFIX}/speakers/{spk.uuid}/assign-profile",
            headers=user_token_headers,
            params={"profile_uuid": str(prof.uuid)},
        )

    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert "profile_name" not in body, (
        "the degraded response should not fabricate fields it could not safely build"
    )
    assert body == {"status": "assigned", "profile_id": str(prof.uuid)}


def test_assign_profile_pre_commit_failure_still_500s_and_writes_nothing(
    client, user_token_headers, normal_user, db_session
):
    """Control for the OTHER direction: a failure BEFORE assign_speaker_to_profile's
    commit (still inside the outer try/except) must still 500 and must NOT be
    reported as a success -- the post-commit guard must not have widened to swallow
    every exception in the handler.

    Deliberately does NOT re-query the speaker row through `db_session` afterward:
    this fixture's savepoint-restart isolation (conftest.py's `db_session`, "handles
    the case where the code under test calls commit()") does not extend to a
    subsequent `db.rollback()` on the SAME session -- measured directly, a
    `session.rollback()` issued after one or more prior `commit()`s rolls the
    session back past the last savepoint into the fixture's own outer transaction
    setup, not just to the savepoint boundary the endpoint's rollback should be
    scoped to. That is a pre-existing test-harness limitation unrelated to this fix
    (the production code's `db.rollback()` behaves correctly against real Postgres;
    only re-probing it through this exact fixture afterward is unsafe) and out of
    scope for issue #620 to fix. The mock's own call assertion is harness-safe
    evidence that the pre-commit path was taken and never wrote.
    """
    from unittest.mock import patch

    prof = _make_profile(db_session, normal_user)
    _mf, spk = _make_file_and_speaker(db_session, normal_user)

    with patch(
        "app.api.endpoints.speaker_profiles.SpeakerMatchingService.assign_speaker_to_profile",
        side_effect=RuntimeError("pre-commit failure"),
    ) as mock_assign:
        resp = client.post(
            f"{PREFIX}/speakers/{spk.uuid}/assign-profile",
            headers=user_token_headers,
            params={"profile_uuid": str(prof.uuid)},
        )

    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert mock_assign.call_count == 1, (
        "expected the request to reach (and fail inside) assign_speaker_to_profile, "
        "confirming the failure was on the pre-commit path"
    )
    assert "assigned" not in resp.text, (
        "a pre-commit failure response must not resemble or claim any success"
    )


def test_suggestions_owner_200_already_profiled_empty(
    client, user_token_headers, normal_user, db_session
):
    """A speaker that already has a profile short-circuits to [] (no embeddings)."""
    prof = _make_profile(db_session, normal_user)
    _mf, spk = _make_file_and_speaker(db_session, normal_user)
    spk.profile_id = prof.id
    db_session.commit()
    resp = client.get(f"{PREFIX}/speakers/{spk.uuid}/suggestions", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == []


def test_suggestions_other_user_speaker_403(
    client, other_user_auth_headers, normal_user, db_session
):
    _mf, spk = _make_file_and_speaker(db_session, normal_user)
    resp = client.get(f"{PREFIX}/speakers/{spk.uuid}/suggestions", headers=other_user_auth_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Not authorized to access this speaker"


def test_suggestions_threshold_over_one_422(client, user_token_headers, normal_user, db_session):
    """``threshold`` is constrained le=1.0."""
    _mf, spk = _make_file_and_speaker(db_session, normal_user)
    resp = client.get(
        f"{PREFIX}/speakers/{spk.uuid}/suggestions",
        headers=user_token_headers,
        params={"threshold": 1.5},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# Speaker collections
# ---------------------------------------------------------------------------


def test_list_collections_unauthorized(client):
    assert client.get(f"{PREFIX}/collections").status_code == status.HTTP_401_UNAUTHORIZED


def test_list_collections_owner_sees_own(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user, name="MyCol")
    resp = client.get(f"{PREFIX}/collections", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    by_uuid = {c["uuid"]: c for c in resp.json()}
    assert str(col.uuid) in by_uuid
    assert by_uuid[str(col.uuid)]["member_count"] == 0


def test_create_collection_200_roundtrip(client, user_token_headers, normal_user, db_session):
    name = f"Col {uuid.uuid4().hex[:6]}"
    resp = client.post(
        f"{PREFIX}/collections",
        headers=user_token_headers,
        params={"name": name, "is_public": True},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert resp.json()["name"] == name
    assert resp.json()["is_public"] is True


def test_create_collection_duplicate_name_400(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user, name="DupCol")
    resp = client.post(
        f"{PREFIX}/collections", headers=user_token_headers, params={"name": col.name}
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Collection with this name already exists"


def test_list_profiles_by_collection_owner_empty_200(
    client, user_token_headers, normal_user, db_session
):
    """Owner filtering by their OWN (empty) collection passes the ownership gate
    and returns [] — pins the happy collection-filter path."""
    col = _make_collection(db_session, normal_user)
    resp = client.get(
        f"{PREFIX}/profiles",
        headers=user_token_headers,
        params={"collection_uuid": str(col.uuid)},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == []
