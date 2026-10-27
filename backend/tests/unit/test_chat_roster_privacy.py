"""The chat speaker roster honours the file's voiceprint switch (#1280).

"What did Alice say about pricing?" is resolved against the speakers the asker can access. While
a file's voiceprints are off a name a voiceprint made does not resolve, a profile link a
voiceprint made is not reported, and such a name cannot even be probed through the lookup's row
cap. A typed name resolves whether or not it has a machine-made link. Each case has an "on"
control.
"""

from __future__ import annotations

import pytest

from app.services.chat.speaker_resolver import build_candidate_roster
from app.services.privacy import policy
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.provenance_world import ALICE
from tests.fixtures.provenance_world import BOB
from tests.fixtures.provenance_world import CAROL
from tests.fixtures.provenance_world import build_world
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


def _roster(db, user, names):
    roster = build_candidate_roster(db, user.id, names, organization_id=None)
    assert not roster.declined
    return {entry.name: entry for entry in roster.entries}


def test_a_voiceprint_name_does_not_resolve_while_the_files_voiceprints_are_off(
    db_session, normal_user
):
    world = build_world(db_session, normal_user)
    from tests.fixtures.privacy_switches import set_instance_switch

    set_instance_switch(db_session, "voiceprints", False)

    entries = _roster(db_session, normal_user, [ALICE, CAROL, BOB])

    assert ALICE not in entries
    assert entries[CAROL].profile_id is None  # the typed name resolves, the machine link does not
    assert entries[BOB].profile_id == world.profiles["bob"].id


def test_every_name_and_link_resolves_while_voiceprints_are_on(db_session, normal_user):
    world = build_world(db_session, normal_user)

    entries = _roster(db_session, normal_user, [ALICE, CAROL, BOB])

    assert entries[ALICE].profile_id == world.profiles["alice"].id
    assert entries[CAROL].profile_id == world.profiles["carol"].id
    assert entries[BOB].profile_id == world.profiles["bob"].id


def test_the_decision_is_the_files_not_the_askers(db_session, normal_user):
    """One owner's off organization hides its names; their personal file keeps its own."""
    build_world(db_session, normal_user, organization=make_org(db_session, voiceprints=False))
    build_world(db_session, normal_user)

    roster = build_candidate_roster(db_session, normal_user.id, [ALICE])

    assert not roster.declined
    assert {e.name: e.file_count for e in roster.entries} == {ALICE: 1}, (
        "only the personal file's Alice resolves; the off organization's does not"
    )


def test_a_hidden_name_cannot_be_probed_through_the_row_cap(db_session, normal_user):
    """A cap that trips only for hidden rows would tell the asker the name exists."""
    from app.services.chat.speaker_resolver import _CANDIDATE_ROW_CAP
    from tests.fixtures.privacy_switches import set_instance_switch

    media_file = make_file(db_session, normal_user)
    for i in range(_CANDIDATE_ROW_CAP + 5):
        make_speaker(
            db_session,
            normal_user,
            media_file,
            name=f"SPEAKER_{i:03d}",
            display_name="Quorra Hiddenname",
            verified=True,
            identity_source="voiceprint",
        )

    on = build_candidate_roster(db_session, normal_user.id, ["Quorra Hiddenname"])
    assert on.declined is True  # control: visible rows over the cap decline the lookup

    set_instance_switch(db_session, "voiceprints", False)
    off = build_candidate_roster(db_session, normal_user.id, ["Quorra Hiddenname"])

    assert off.declined is False
    assert off.entries == ()


def test_a_hidden_voice_suggestion_is_not_a_candidate_column(db_session, normal_user):
    from tests.fixtures.privacy_switches import set_instance_switch

    media_file = make_file(db_session, normal_user)
    make_speaker(
        db_session,
        normal_user,
        media_file,
        suggested_name="Ophelia Marchbanks",
        suggestion_source="voice_match",
        confidence=0.9,
    )

    assert "Ophelia Marchbanks" in _roster(db_session, normal_user, ["Ophelia Marchbanks"])

    set_instance_switch(db_session, "voiceprints", False)

    assert "Ophelia Marchbanks" not in _roster(db_session, normal_user, ["Ophelia Marchbanks"])


def test_a_hidden_voice_suggestion_cannot_be_probed_through_the_row_cap(db_session, normal_user):
    from app.services.chat.speaker_resolver import _CANDIDATE_ROW_CAP
    from tests.fixtures.privacy_switches import set_instance_switch

    media_file = make_file(db_session, normal_user)
    for i in range(_CANDIDATE_ROW_CAP + 5):
        make_speaker(
            db_session,
            normal_user,
            media_file,
            name=f"SPEAKER_{i:03d}",
            suggested_name="Ophelia Marchbanks",
            suggestion_source="voice_match",
            confidence=0.9,
        )

    assert build_candidate_roster(db_session, normal_user.id, ["Ophelia Marchbanks"]).declined

    set_instance_switch(db_session, "voiceprints", False)
    off = build_candidate_roster(db_session, normal_user.id, ["Ophelia Marchbanks"])

    assert off.declined is False
    assert off.entries == ()
