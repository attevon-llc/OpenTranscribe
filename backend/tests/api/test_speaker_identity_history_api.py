"""``GET /api/speakers/{uuid}/identity-history`` (issue #1336).

The history is read like the speaker: whoever may read the speaker may read it, nobody else
(refused exactly as the speaker is). What a voiceprint produced is withheld while the file's voiceprints are off, and
the person behind a change is named only to who may see ``identity.by``.
"""

from __future__ import annotations

from app.core import constants
from app.models.prompt import UserSetting
from app.utils import speaker_identity_source as ids
from app.utils.speaker_identity_detail import ORIGIN_TYPED
from app.utils.speaker_identity_detail import ORIGIN_VOICE_MATCH
from app.utils.speaker_identity_detail import voiceprint_detail
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker


def _url(speaker) -> str:
    return f"/api/speakers/{speaker.uuid}/identity-history"


def _history(db, user):
    """A voiceprint names the speaker, then the owner renames it."""
    profile = make_profile(db, user, "Alice")
    speaker = make_speaker(db, user, make_file(db, user))
    db.commit()
    speaker.display_name = "Alice"
    speaker.profile_id = profile.id
    ids.record_machine_identity(
        speaker,
        previous_display_name=None,
        detail=voiceprint_detail(ORIGIN_VOICE_MATCH, confidence=0.9, embedding_model="m-1"),
    )
    db.commit()
    speaker.display_name = "Alicia"
    ids.record_human_identity(speaker, user_id=user.id, origin=ORIGIN_TYPED)
    db.commit()
    return speaker, profile


def test_the_owner_reads_the_history_newest_first_with_who_and_which_model(
    client, db_session, normal_user, user_token_headers
):
    speaker, profile = _history(db_session, normal_user)
    response = client.get(_url(speaker), headers=user_token_headers)
    assert response.status_code == 200, response.text
    rename, match = response.json()

    assert rename["kind"] == "rename"
    assert rename["actor"]["kind"] == "user"
    assert rename["actor"]["user"]["uuid"] == str(normal_user.uuid)
    assert rename["after"]["name"] == "Alicia"
    # The old name was the voiceprint's: it is not copied into the person's entry.
    assert "name" not in rename["before"] and rename["before"]["withheld"] == ["name"]

    assert (match["actor"], match["model"], match["confidence"]) == (
        {"kind": "system", "user": None},
        "m-1",
        0.9,
    )
    assert match["after"]["name"] == "Alice"
    assert match["after"]["profile"] == {"uuid": str(profile.uuid), "name": "Alice"}
    assert "profile_id" not in match["after"], "internal ids never leave the API"


def test_the_voiceprints_entry_is_withheld_while_voiceprints_are_off(
    client, db_session, normal_user, user_token_headers
):
    speaker, _ = _history(db_session, normal_user)
    db_session.add(
        UserSetting(
            user_id=normal_user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db_session.commit()
    response = client.get(_url(speaker), headers=user_token_headers)
    assert response.status_code == 200
    assert [e["kind"] for e in response.json()] == ["rename"]
    assert response.json()[0]["model"] is None


def test_another_account_is_refused_exactly_as_it_is_refused_the_speaker(
    client, db_session, normal_user, other_user_auth_headers, user_token_headers
):
    speaker, _ = _history(db_session, normal_user)
    refused = client.get(f"/api/speakers/{speaker.uuid}", headers=other_user_auth_headers)
    assert refused.status_code == 403
    response = client.get(_url(speaker), headers=other_user_auth_headers)
    assert response.status_code == refused.status_code
    assert "Alicia" not in response.text
    missing = client.get(
        "/api/speakers/00000000-0000-0000-0000-000000000000/identity-history",
        headers=user_token_headers,
    )
    assert missing.status_code == 404


def test_the_limit_is_bounded(client, db_session, normal_user, user_token_headers):
    speaker, _ = _history(db_session, normal_user)
    assert len(client.get(_url(speaker) + "?limit=1", headers=user_token_headers).json()) == 1
    assert client.get(_url(speaker) + "?limit=0", headers=user_token_headers).status_code == 422
    assert client.get(_url(speaker) + "?limit=201", headers=user_token_headers).status_code == 422
