"""Inferred gender in the speaker, profile and cluster responses (#1280, plan D5).

While gender inference is off for a scope, the gender the model inferred serializes as
``null`` and the stored value survives; a gender a person confirmed is theirs and shows
either way. Each case has its "on" control, and the database row is re-read to prove the
value was hidden, not deleted.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.media import Speaker
from app.models.media import SpeakerProfile
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

SPEAKERS = "/api/speakers"
PROFILES = "/api/speaker-profiles"
CLUSTERS = "/api/speaker-clusters"


@pytest.fixture
def knn_off(monkeypatch):
    """The list endpoint asks OpenSearch for suggestions; there is no cluster here."""
    monkeypatch.setattr(
        "app.services.opensearch_service.get_speaker_embeddings_batch", lambda uuids: {}
    )


@pytest.fixture
def pair(db_session, normal_user):
    """One file with an inferred-male speaker and a person-confirmed-female one."""
    file = make_file(db_session, normal_user)
    inferred = make_speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_00",
        predicted_gender="male",
        gender_confirmed_by_user=False,
        attribute_confidence={"gender": 0.93, "age_range": 0.6},
    )
    confirmed = make_speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_01",
        predicted_gender="female",
        gender_confirmed_by_user=True,
    )
    db_session.commit()
    return {"file": file, "inferred": inferred, "confirmed": confirmed}


def _by_uuid(items):
    return {i["uuid"]: i for i in items}


def test_the_speaker_list_shows_inferred_gender_while_the_switch_is_on(
    client, user_token_headers, pair, knn_off
):
    items = _by_uuid(
        client.get(
            SPEAKERS, headers=user_token_headers, params={"file_uuid": pair["file"].uuid}
        ).json()
    )

    assert items[str(pair["inferred"].uuid)]["predicted_gender"] == "male"
    assert items[str(pair["inferred"].uuid)]["attribute_confidence"] == {
        "gender": 0.93,
        "age_range": 0.6,
    }


def test_the_speaker_list_withholds_inferred_gender_while_off_and_keeps_a_confirmed_one(
    client, user_token_headers, pair, knn_off, db_session
):
    set_instance_switch(db_session, "gender", False)

    items = _by_uuid(
        client.get(
            SPEAKERS, headers=user_token_headers, params={"file_uuid": pair["file"].uuid}
        ).json()
    )

    inferred = items[str(pair["inferred"].uuid)]
    assert inferred["predicted_gender"] is None
    assert inferred["attribute_confidence"] == {"age_range": 0.6}
    assert items[str(pair["confirmed"].uuid)]["predicted_gender"] == "female"
    # Hidden, not deleted.
    db_session.expire_all()
    assert db_session.get(Speaker, pair["inferred"].id).predicted_gender == "male"


def test_the_single_speaker_read_withholds_inferred_gender_while_off(
    client, user_token_headers, pair, db_session
):
    url = f"{SPEAKERS}/{pair['inferred'].uuid}"
    assert client.get(url, headers=user_token_headers).json()["predicted_gender"] == "male"

    set_instance_switch(db_session, "gender", False)

    assert client.get(url, headers=user_token_headers).json()["predicted_gender"] is None


def test_confirming_a_gender_still_works_while_inference_is_off(
    client, user_token_headers, pair, db_session
):
    """Typing in a gender is the person's own assertion, not an inference."""
    set_instance_switch(db_session, "gender", False)

    resp = client.post(
        f"{SPEAKERS}/{pair['inferred'].uuid}/confirm-gender",
        headers=user_token_headers,
        params={"gender": "female"},
    )

    assert resp.status_code == 200
    assert resp.json()["predicted_gender"] == "female"
    shown = client.get(f"{SPEAKERS}/{pair['inferred'].uuid}", headers=user_token_headers).json()
    assert shown["predicted_gender"] == "female"


def _profile_with_inferred_members(db, user) -> SpeakerProfile:
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=user.id, name="Pat")
    db.add(profile)
    db.flush()
    for i in range(2):
        make_speaker(
            db,
            user,
            make_file(db, user),
            name=f"SPEAKER_{i:02d}",
            predicted_gender="male",
            gender_confirmed_by_user=False,
            profile_id=profile.id,
        )
    db.commit()
    return profile


def test_the_profile_list_derives_a_gender_only_while_inference_is_on(
    client, user_token_headers, normal_user, db_session
):
    profile = _profile_with_inferred_members(db_session, normal_user)

    on = _by_uuid(client.get(f"{PROFILES}/profiles", headers=user_token_headers).json())
    assert on[str(profile.uuid)]["predicted_gender"] == "male"

    set_instance_switch(db_session, "gender", False)
    off = _by_uuid(client.get(f"{PROFILES}/profiles", headers=user_token_headers).json())
    assert off[str(profile.uuid)]["predicted_gender"] is None


def test_a_gender_confirmed_on_the_profile_shows_while_inference_is_off(
    client, user_token_headers, normal_user, db_session
):
    profile = _profile_with_inferred_members(db_session, normal_user)
    profile.predicted_gender = "female"
    db_session.commit()
    set_instance_switch(db_session, "gender", False)

    listing = _by_uuid(client.get(f"{PROFILES}/profiles", headers=user_token_headers).json())

    assert listing[str(profile.uuid)]["predicted_gender"] == "female"


def test_the_cluster_inbox_withholds_inferred_gender_while_off(
    client, user_token_headers, pair, db_session
):
    def inbox_genders() -> dict[str, str | None]:
        items = client.get(f"{CLUSTERS}/unverified/inbox", headers=user_token_headers).json()[
            "items"
        ]
        return {i["speaker_uuid"]: i["predicted_gender"] for i in items}

    assert inbox_genders()[str(pair["inferred"].uuid)] == "male"

    set_instance_switch(db_session, "gender", False)

    off = inbox_genders()
    assert off[str(pair["inferred"].uuid)] is None


def test_the_cluster_detail_withholds_inferred_gender_while_off(
    client, user_token_headers, normal_user, pair, db_session
):
    cluster = make_cluster(db_session, normal_user, pair["inferred"], pair["confirmed"])
    url = f"{CLUSTERS}/{cluster.uuid}"

    def member_genders() -> dict[str, str | None]:
        members = client.get(url, headers=user_token_headers).json()["members"]
        return {m["speaker_uuid"]: m["predicted_gender"] for m in members}

    assert member_genders()[str(pair["inferred"].uuid)] == "male"

    set_instance_switch(db_session, "gender", False)

    off = member_genders()
    assert off[str(pair["inferred"].uuid)] is None
    assert off[str(pair["confirmed"].uuid)] == "female"
