"""The speaker name directory does not offer a name a voiceprint made while voiceprints are off.

``GET /speakers?for_filter=true`` is the gallery's speaker picker and the chat file-scope
roster. It groups by name across files, so a hidden name that stayed in it would be offered as
a person and filter files by it. Each hidden case has a control (the switch on, or a name a
person typed) so "hide everything" cannot pass.
"""

from __future__ import annotations

import pytest

from app.core import constants
from app.models.prompt import UserSetting
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

URL = "/api/speakers"


def _headers(user_token_headers: dict) -> dict:
    return {k: v for k, v in user_token_headers.items() if not k.startswith("_")}


def _seed(db, user):
    """Alice: named and linked by a voiceprint. Bob: typed, unlinked. Cara: typed, machine link."""
    alice_profile = make_profile(db, user, "Alice")
    cara_profile = make_profile(db, user, "Cara")
    alice = make_speaker(
        db,
        user,
        make_file(db, user),
        display_name="Alice",
        profile_id=alice_profile.id,
        identity_source="voiceprint",
        verified=True,
    )
    bob = make_speaker(
        db,
        user,
        make_file(db, user),
        display_name="Bob",
        identity_source="human",
        verified=True,
    )
    cara = make_speaker(
        db,
        user,
        make_file(db, user),
        display_name="Cara",
        profile_id=cara_profile.id,
        identity_source="voiceprint_link",
        verified=True,
    )
    db.commit()
    return alice, bob, cara, alice_profile, cara_profile


def _switch_off(db, user) -> None:
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db.commit()


def _directory(client, headers, **params):
    response = client.get(URL, params={"for_filter": "true", **params}, headers=headers)
    assert response.status_code == 200, response.text
    return {(r["display_name"] or r["name"]): r for r in response.json()}


def test_a_name_a_voiceprint_made_is_offered_while_on(
    client, user_token_headers, db_session, normal_user
):
    alice, *_rest, alice_profile, _cara_profile = _seed(db_session, normal_user)

    offered = _directory(client, _headers(user_token_headers))

    assert set(offered) == {"Alice", "Bob", "Cara"}
    assert offered["Alice"]["profile_id"] == str(alice_profile.uuid)
    assert offered["Alice"]["is_unnamed"] is False
    assert alice.display_name == "Alice"


def test_a_hidden_machine_name_is_not_offered(client, user_token_headers, db_session, normal_user):
    _seed(db_session, normal_user)
    _switch_off(db_session, normal_user)
    headers = _headers(user_token_headers)

    offered = _directory(client, headers)
    assert set(offered) == {"Bob", "Cara"}, "Alice is the machine's; Bob and Cara are typed"

    with_unnamed = _directory(client, headers, include_unnamed="true")
    assert "Alice" not in with_unnamed
    assert with_unnamed["SPEAKER_00"]["is_unnamed"] is True, (
        "the hidden speaker is listed under its diarizer label, flagged, never as a person"
    )
    assert with_unnamed["Bob"]["is_unnamed"] is False


def test_a_typed_name_stays_offered_but_its_machine_link_is_hidden(
    client, user_token_headers, db_session, normal_user
):
    _seed(db_session, normal_user)
    headers = _headers(user_token_headers)
    on = _directory(client, headers)
    assert on["Cara"]["profile_id"] is not None, "control: the link is shown while on"

    _switch_off(db_session, normal_user)
    off = _directory(client, headers)

    assert off["Cara"]["profile_id"] is None
    assert off["Cara"]["is_unnamed"] is False
    assert "Bob" in off


def test_profile_scoping_follows_the_links_that_are_shown(
    client, user_token_headers, db_session, normal_user
):
    _alice, _bob, _cara, alice_profile, cara_profile = _seed(db_session, normal_user)
    headers = _headers(user_token_headers)
    assert set(_directory(client, headers, profile_id=str(cara_profile.uuid))) == {"Cara"}
    assert set(_directory(client, headers, is_profile="true")) == {"Alice", "Cara"}

    _switch_off(db_session, normal_user)

    assert _directory(client, headers, profile_id=str(cara_profile.uuid)) == {}
    assert _directory(client, headers, profile_id=str(alice_profile.uuid)) == {}
    assert _directory(client, headers, is_profile="true") == {}
    assert set(_directory(client, headers, is_profile="false")) == {"Bob", "Cara"}


@pytest.mark.parametrize("term", ["Alice", "alic"])
def test_searching_for_a_hidden_name_finds_nothing(
    term, client, user_token_headers, db_session, normal_user
):
    _seed(db_session, normal_user)
    headers = _headers(user_token_headers)
    assert "Alice" in _directory(client, headers, q=term), "control: found while on"

    _switch_off(db_session, normal_user)

    assert _directory(client, headers, q=term) == {}
