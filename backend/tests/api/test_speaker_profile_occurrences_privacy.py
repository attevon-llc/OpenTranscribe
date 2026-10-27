"""A profile's occurrences omit the files a voiceprint linked to it while voiceprints are off.

The occurrences list answers "where does this person appear". A file whose only evidence is a
link the voiceprint made would otherwise keep appearing after the switch is off. A link a
person made always counts. Each hiding case has an "on" control and a human-link control.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.core import constants
from app.models.organization import Organization
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.services.speaker_matching_service import SpeakerMatchingService
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker


def _headers(user_token_headers: dict) -> dict:
    return {k: v for k, v in user_token_headers.items() if not k.startswith("_")}


def _linked(db, user, profile, source, *, org=None):
    media_file = make_file(db, user)
    if org is not None:
        media_file.organization_id = org.id
    return make_speaker(
        db,
        user,
        media_file,
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source=source,
    ), media_file


def _occurrence_files(client, headers, profile) -> set[int]:
    response = client.get(
        f"/api/speaker-profiles/profiles/{profile.uuid}/occurrences", headers=headers
    )
    assert response.status_code == 200, response.text
    return {row["media_file_id"] for row in response.json()}


def _switch_off(db, user) -> None:
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db.commit()


def test_occurrences_include_machine_links_while_on(
    client, user_token_headers, db_session, normal_user
):
    profile = make_profile(db_session, normal_user, "Alice")
    _, machine_file = _linked(db_session, normal_user, profile, "voiceprint")
    _, link_file = _linked(db_session, normal_user, profile, "voiceprint_link")
    _, human_file = _linked(db_session, normal_user, profile, "human")
    db_session.commit()

    files = _occurrence_files(client, _headers(user_token_headers), profile)

    assert files == {machine_file.id, link_file.id, human_file.id}


def test_occurrences_omit_machine_links_while_off(
    client, user_token_headers, db_session, normal_user
):
    profile = make_profile(db_session, normal_user, "Alice")
    _linked(db_session, normal_user, profile, "voiceprint")
    _linked(db_session, normal_user, profile, "voiceprint_link")
    _, human_file = _linked(db_session, normal_user, profile, "human")
    _, unknown_file = _linked(db_session, normal_user, profile, None)
    db_session.commit()
    _switch_off(db_session, normal_user)

    files = _occurrence_files(client, _headers(user_token_headers), profile)

    assert files == {human_file.id, unknown_file.id}, (
        "a person's link (and a legacy one, read as a person's) stays; the machine's does not"
    )


def test_each_occurrence_is_judged_in_its_own_files_scope(db_session, normal_user):
    """The scope is the speaker's FILE's, never the caller's argument."""
    org = Organization(name=f"org-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.flush()
    db_session.add(OrganizationPolicy(organization_id=org.id, voiceprint_matching_enabled=False))
    profile = make_profile(db_session, normal_user, "Alice")
    _, personal_file = _linked(db_session, normal_user, profile, "voiceprint")
    _, off_org_file = _linked(db_session, normal_user, profile, "voiceprint", org=org)
    _, off_org_human = _linked(db_session, normal_user, profile, "human", org=org)
    db_session.commit()

    rows = SpeakerMatchingService(db_session, None).find_speaker_occurrences(
        int(profile.id), int(normal_user.id)
    )

    shown = {row["media_file_id"] for row in rows}
    assert personal_file.id in shown, "control: the personal scope is on"
    assert off_org_file.id not in shown
    assert off_org_human.id in shown
