"""Five read surfaces that showed a quarantined (taken-down) file to its owner (#1101 plan §1.3).

Quarantine used to be a predicate placed by hand at each read site; these five never got it.
They now route through ``app.services.file_availability``, the one availability rule, so each
test here is red on the tree before that rule and green after. Every test has a control (the
same file not quarantined) so a route that is simply broken cannot pass it.

* ``GET /files/owners`` listed the owner of a file only a quarantined file could reach.
* ``GET /collections/for-files`` loaded the selection by bare uuid, so it read the collection
  membership of a quarantined file back to its owner.
* ``GET /files/management/stuck`` resolved each stuck file through ``get_media_file_by_id``,
  which had no quarantine check, and returned a quarantined file's uuid and filename.
* ``GET /speaker-profiles/profiles`` counted a quarantined file's speakers in a profile's
  ``media_count`` / ``instance_count``.
* ``GET /speaker-clusters/{uuid}`` listed a quarantined file's speaker as a cluster member, with
  the file's uuid and title.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerProfile

_TITLE = "Qorvantel quarterly"


def _file(db_session, owner, *, quarantined: bool, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{_TITLE}.wav",
        title=_TITLE,
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=overrides.pop("status", "completed"),
        is_public=False,
        user_id=owner.id,
        is_quarantined=quarantined,
        **overrides,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _speaker(db_session, owner, media_file, **kw) -> Speaker:
    row = Speaker(
        uuid=uuid.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="Vexmorrow",
        **kw,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# --- GET /files/owners ------------------------------------------------------------------------


def _owner_ids(client, headers) -> set[str]:
    response = client.get("/api/files/owners", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    return {str(row["uuid"]) for row in response.json()}


def test_owners_facet_does_not_list_the_owner_of_only_a_quarantined_file(
    client, user_token_headers, normal_user, db_session
):
    _file(db_session, normal_user, quarantined=True)
    assert str(normal_user.uuid) not in _owner_ids(client, user_token_headers)


def test_owners_facet_lists_the_owner_of_an_available_file(
    client, user_token_headers, normal_user, db_session
):
    _file(db_session, normal_user, quarantined=False)
    assert str(normal_user.uuid) in _owner_ids(client, user_token_headers)


# --- GET /collections/for-files ---------------------------------------------------------------


def _collection_with(db_session, owner, media_file) -> Collection:
    collection = Collection(
        uuid=uuid.uuid4(), name=f"gaps-{uuid.uuid4().hex[:8]}", user_id=owner.id
    )
    db_session.add(collection)
    db_session.commit()
    db_session.add(
        CollectionMember(
            uuid=uuid.uuid4(), collection_id=collection.id, media_file_id=media_file.id
        )
    )
    db_session.commit()
    return collection


def test_collections_on_files_refuses_a_quarantined_file(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user, quarantined=True)
    collection = _collection_with(db_session, normal_user, media_file)

    response = client.get(
        "/api/collections/for-files",
        params={"file_uuids": [str(media_file.uuid)]},
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert collection.name not in response.text


def test_collections_on_files_answers_for_an_available_file(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user, quarantined=False)
    collection = _collection_with(db_session, normal_user, media_file)

    response = client.get(
        "/api/collections/for-files",
        params={"file_uuids": [str(media_file.uuid)]},
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert [c["name"] for c in response.json()] == [collection.name]


# --- GET /files/management/stuck (get_media_file_by_id) ---------------------------------------


def _stuck_uuids(client, headers) -> set[str]:
    response = client.get("/api/files/management/stuck", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    return {row["uuid"] for row in response.json()["stuck_files"]}


def test_stuck_files_do_not_name_a_quarantined_file(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user, quarantined=True, status="orphaned")
    assert str(media_file.uuid) not in _stuck_uuids(client, user_token_headers)


def test_stuck_files_name_an_available_stuck_file(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user, quarantined=False, status="orphaned")
    assert str(media_file.uuid) in _stuck_uuids(client, user_token_headers)


# --- GET /speaker-profiles/profiles -----------------------------------------------------------


def _profile_counts(client, headers, profile) -> tuple[int, int]:
    response = client.get("/api/speaker-profiles/profiles", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    entry = next(p for p in response.json() if str(p["uuid"]) == str(profile.uuid))
    return entry["media_count"], entry["instance_count"]


def _profile_over_two_files(db_session, owner, *, second_quarantined: bool) -> SpeakerProfile:
    profile = SpeakerProfile(uuid=uuid.uuid4(), user_id=owner.id, name=f"P {uuid.uuid4().hex[:8]}")
    db_session.add(profile)
    db_session.commit()
    for quarantined in (False, second_quarantined):
        media_file = _file(db_session, owner, quarantined=quarantined)
        _speaker(db_session, owner, media_file, profile_id=profile.id)
    return profile


def test_profile_counts_leave_out_a_quarantined_file(
    client, user_token_headers, normal_user, db_session
):
    profile = _profile_over_two_files(db_session, normal_user, second_quarantined=True)
    assert _profile_counts(client, user_token_headers, profile) == (1, 1)


def test_profile_counts_include_every_available_file(
    client, user_token_headers, normal_user, db_session
):
    profile = _profile_over_two_files(db_session, normal_user, second_quarantined=False)
    assert _profile_counts(client, user_token_headers, profile) == (2, 2)


# --- GET /speaker-clusters/{uuid} -------------------------------------------------------------


def _cluster_over(db_session, owner, *, quarantined: bool) -> tuple[SpeakerCluster, MediaFile]:
    media_file = _file(db_session, owner, quarantined=quarantined)
    speaker = _speaker(db_session, owner, media_file)
    cluster = SpeakerCluster(uuid=uuid.uuid4(), user_id=owner.id, member_count=1)
    db_session.add(cluster)
    db_session.flush()
    db_session.add(
        SpeakerClusterMember(
            uuid=uuid.uuid4(), cluster_id=cluster.id, speaker_id=speaker.id, confidence=0.9
        )
    )
    speaker.cluster_id = cluster.id
    db_session.commit()
    db_session.refresh(cluster)
    return cluster, media_file


def test_cluster_detail_does_not_list_a_quarantined_files_speaker(
    client, user_token_headers, normal_user, db_session
):
    cluster, media_file = _cluster_over(db_session, normal_user, quarantined=True)

    response = client.get(f"/api/speaker-clusters/{cluster.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["members"] == []
    assert str(media_file.uuid) not in response.text
    assert _TITLE not in response.text


def test_cluster_detail_lists_an_available_files_speaker(
    client, user_token_headers, normal_user, db_session
):
    cluster, media_file = _cluster_over(db_session, normal_user, quarantined=False)

    response = client.get(f"/api/speaker-clusters/{cluster.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    members = response.json()["members"]
    assert [m["media_file_uuid"] for m in members] == [str(media_file.uuid)]
    assert members[0]["media_file_title"] == _TITLE
