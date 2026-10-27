"""Gallery, collection list, speaker filter and profile counts honour the voiceprint switch (#1280).

The file list prints a speaker chip per speaker, the list filters by speaker name, a collection
lists its files the same way, and the profile list counts the appearances of each profile. While
the instance has voiceprints off, a name a voiceprint made is neither shown nor matchable and a
link a voiceprint made is not counted; a typed name, and a person's link, are. Every case has an
"on" control, and switching back on restores everything (nothing was deleted).
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.models.media import Collection
from app.models.media import CollectionMember
from app.services.privacy import policy
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.provenance_world import ALICE
from tests.fixtures.provenance_world import BOB
from tests.fixtures.provenance_world import CAROL
from tests.fixtures.provenance_world import build_world

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


@pytest.fixture
def world(db_session, normal_user):
    built = build_world(db_session, normal_user)
    collection = Collection(
        uuid=uuid_pkg.uuid4(), name=f"c-{uuid_pkg.uuid4().hex[:8]}", user_id=normal_user.id
    )
    db_session.add(collection)
    db_session.flush()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=built.file.id))
    db_session.commit()
    built.collection = collection  # type: ignore[attr-defined]
    return built


def _off(db_session) -> None:
    set_instance_switch(db_session, "voiceprints", False)


def _chips(response) -> list[str]:
    assert response.status_code == status.HTTP_200_OK, response.text
    items = response.json()["items"]
    assert items, "the listing returned no files"
    return [name for item in items for name in item["speaker_summary"]["primary_speakers"]]


def test_the_gallery_shows_every_name_while_voiceprints_are_on(client, user_token_headers, world):
    chips = _chips(client.get("/api/files", headers=user_token_headers))

    assert {ALICE, CAROL, BOB} == set(chips)


def test_the_gallery_chips_hide_a_voiceprint_name_while_voiceprints_are_off(
    client, user_token_headers, world, db_session
):
    _off(db_session)

    chips = _chips(client.get("/api/files", headers=user_token_headers))

    assert ALICE not in chips
    assert {"SPEAKER_00", CAROL, BOB} == set(chips)


def test_the_collection_list_hides_a_voiceprint_name_while_voiceprints_are_off(
    client, user_token_headers, world, db_session
):
    url = f"/api/collections/{world.collection.uuid}/media"
    assert ALICE in _chips(client.get(url, headers=user_token_headers))  # control

    _off(db_session)

    chips = _chips(client.get(url, headers=user_token_headers))
    assert ALICE not in chips
    assert {"SPEAKER_00", CAROL, BOB} == set(chips)


def test_the_speaker_filter_matches_a_voiceprint_name_only_while_voiceprints_are_on(
    client, user_token_headers, world, db_session
):
    def matches(name: str, url: str = "/api/files") -> int:
        response = client.get(url, params={"speaker": name}, headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        return int(response.json()["total"])

    collection_url = f"/api/collections/{world.collection.uuid}/media"
    assert matches(ALICE) == 1
    assert matches(ALICE, collection_url) == 1

    _off(db_session)

    assert matches(ALICE) == 0
    assert matches(ALICE, collection_url) == 0
    assert matches(CAROL) == 1  # a typed name still matches
    assert matches(BOB) == 1
    assert matches("SPEAKER_00") == 1  # the diarizer name always matches

    set_instance_switch(db_session, "voiceprints", True)

    assert matches(ALICE) == 1  # nothing was deleted


def _counts(client, headers, world) -> dict[str, tuple[int, int]]:
    response = client.get("/api/speaker-profiles/profiles", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    by_uuid = {p["uuid"]: (p["instance_count"], p["media_count"]) for p in response.json()}
    return {key: by_uuid[str(profile.uuid)] for key, profile in world.profiles.items()}


def test_profile_counts_include_every_link_while_voiceprints_are_on(
    client, user_token_headers, world
):
    assert _counts(client, user_token_headers, world) == {
        "alice": (1, 1),
        "carol": (1, 1),
        "bob": (1, 1),
    }


def test_profile_counts_leave_out_machine_links_while_voiceprints_are_off(
    client, user_token_headers, world, db_session
):
    _off(db_session)

    assert _counts(client, user_token_headers, world) == {
        "alice": (0, 0),
        "carol": (0, 0),
        "bob": (1, 1),
    }


def test_a_page_of_files_asks_the_policy_once_per_scope_not_once_per_speaker(
    client, user_token_headers, db_session, normal_user, monkeypatch
):
    for _ in range(4):
        build_world(db_session, normal_user)
    db_session.commit()
    asked: list[object] = []
    real = policy.voiceprint_decision

    def counting(db, **kwargs):
        if kwargs.get("media_file_id") is not None:
            asked.append(kwargs["media_file_id"])
        return real(db, **kwargs)

    monkeypatch.setattr(policy, "voiceprint_decision", counting)

    response = client.get("/api/files", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["total"] == 4
    assert len(asked) == 1  # one personal scope, whatever the number of files or speakers
