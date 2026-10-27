"""``services/privacy/redact_view``: what a response may still show under the policy (#1280).

Each "hidden" case has a "shown" control beside it (the layer on, a verified row, a confirmed
gender), so a view that hid everything would fail. State is written into the real
``organization_policy`` / ``user_setting`` / ``system_settings`` tables and read back through
the policy, as the endpoints do.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest
from fastapi import HTTPException

from app.core import constants
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.organization import Organization
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.services.privacy import redact_view
from app.services.privacy.consent import clear_voiceprint_consent_hooks
from app.services.privacy.consent import register_voiceprint_consent_hook
from app.services.privacy.policy import reset_privacy_ceiling_resolver
from app.services.privacy.redact_view import PrivacyView
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker


@pytest.fixture(autouse=True)
def _clean_policy_state():
    clear_voiceprint_consent_hooks()
    reset_privacy_ceiling_resolver()
    yield
    clear_voiceprint_consent_hooks()
    reset_privacy_ceiling_resolver()


def _org(db, *, voiceprints: bool = True, gender: bool = True) -> Organization:
    org = Organization(name=f"org-{uuid_pkg.uuid4().hex[:8]}")
    db.add(org)
    db.flush()
    db.add(
        OrganizationPolicy(
            organization_id=org.id,
            voiceprint_matching_enabled=voiceprints,
            gender_inference_enabled=gender,
        )
    )
    db.flush()
    return org


def _file_in(db, user, org: Organization | None) -> Any:
    media_file = make_file(db, user)
    media_file.organization_id = org.id if org else None
    db.flush()
    return media_file


# ---------------------------------------------------------------------------
# Per-file decisions
# ---------------------------------------------------------------------------


def test_only_the_files_of_an_organization_with_voiceprints_off_are_reported(
    db_session, normal_user
):
    off_org = _org(db_session, voiceprints=False)
    on_org = _org(db_session)
    in_off = _file_in(db_session, normal_user, off_org)
    in_on = _file_in(db_session, normal_user, on_org)
    personal = _file_in(db_session, normal_user, None)

    off = PrivacyView(db_session).voiceprints_off_file_ids([in_off.id, in_on.id, personal.id])

    assert off == {in_off.id}
    assert PrivacyView(db_session).voiceprints(in_off.id).locked_by == "organization"


def test_a_file_that_does_not_exist_is_treated_as_off(db_session):
    """Fail closed: a policy question that cannot be answered is a no."""
    assert PrivacyView(db_session).voiceprints_off_file_ids([987_654_321]) == {987_654_321}
    assert PrivacyView(db_session).gender(987_654_321).enabled is False


def test_the_owners_own_switch_turns_their_files_off_and_names_the_user_layer(
    db_session, normal_user
):
    file = _file_in(db_session, normal_user, None)
    assert PrivacyView(db_session).voiceprints(file.id).enabled is True

    db_session.add(
        UserSetting(
            user_id=normal_user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db_session.flush()

    decision = PrivacyView(db_session).voiceprints(file.id)
    assert decision.enabled is False
    assert decision.locked_by == "user"


def test_a_consent_hook_that_says_no_turns_a_file_off_and_one_that_says_yes_does_not(
    db_session, normal_user
):
    file = _file_in(db_session, normal_user, None)

    register_voiceprint_consent_hook(lambda db, request: True)
    assert PrivacyView(db_session).voiceprints(file.id).enabled is True

    clear_voiceprint_consent_hooks()
    register_voiceprint_consent_hook(lambda db, request: False)
    decision = PrivacyView(db_session).voiceprints(file.id)
    assert decision.enabled is False
    assert decision.locked_by == "consent"


def test_require_voiceprints_raises_the_409_with_the_locking_layer(db_session, normal_user):
    org = _org(db_session, voiceprints=False)

    with pytest.raises(HTTPException) as caught:
        redact_view.require_voiceprints(db_session, user_id=normal_user.id, organization_id=org.id)

    assert caught.value.status_code == 409
    detail: object = caught.value.detail
    assert detail == {"code": "voiceprints_disabled", "locked_by": "organization"}
    # Control: a scope that is on raises nothing.
    redact_view.require_voiceprints(db_session, user_id=normal_user.id, organization_id=None)


def test_organizations_with_voiceprints_off_lists_only_the_off_scopes(db_session, normal_user):
    off_org = _org(db_session, voiceprints=False)
    on_org = _org(db_session)

    off = redact_view.organizations_with_voiceprints_off(
        db_session, normal_user.id, [off_org.id, on_org.id, None]
    )

    assert off == {off_org.id}


# ---------------------------------------------------------------------------
# Rows: suggestions and gender
# ---------------------------------------------------------------------------


def _speaker(db, user, file, **kw) -> Any:
    return make_speaker(db, user, file, **kw)


def test_a_voiceprint_written_suggestion_is_hidden_in_memory_and_kept_in_the_database(
    db_session, normal_user
):
    off_org = _org(db_session, voiceprints=False)
    file = _file_in(db_session, normal_user, off_org)
    speaker = _speaker(
        db_session,
        normal_user,
        file,
        suggested_name="Alice",
        confidence=0.9,
        suggestion_source="profile_match",
        verified=False,
    )
    db_session.commit()

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert speaker.suggested_name is None
    assert speaker.suggestion_source is None
    assert speaker.confidence is None
    db_session.expire_all()
    stored = db_session.get(Speaker, speaker.id)
    assert (stored.suggested_name, stored.suggestion_source, stored.confidence) == (
        "Alice",
        "profile_match",
        0.9,
    )


@pytest.mark.parametrize(
    ("source", "verified"),
    [("voice_match", True), ("llm_analysis", False), ("metadata_hint", False)],
)
def test_a_verified_voice_name_and_a_non_voiceprint_suggestion_are_not_hidden(
    db_session, normal_user, source, verified
):
    off_org = _org(db_session, voiceprints=False)
    file = _file_in(db_session, normal_user, off_org)
    speaker = _speaker(
        db_session,
        normal_user,
        file,
        suggested_name="Alice",
        confidence=0.9,
        suggestion_source=source,
        verified=verified,
    )

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert (speaker.suggested_name, speaker.suggestion_source) == ("Alice", source)


def test_a_suggestion_stays_when_the_layer_is_on(db_session, normal_user):
    file = _file_in(db_session, normal_user, _org(db_session))
    speaker = _speaker(
        db_session,
        normal_user,
        file,
        suggested_name="Alice",
        confidence=0.9,
        suggestion_source="voice_match",
        verified=False,
    )

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert speaker.suggested_name == "Alice"


def test_inferred_gender_is_withheld_and_a_confirmed_one_is_not(db_session, normal_user):
    off_org = _org(db_session, gender=False)
    file = _file_in(db_session, normal_user, off_org)
    inferred = _speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_00",
        predicted_gender="male",
        attribute_confidence={"gender": 0.93, "age_range": 0.6, "gender_alignment": "match"},
        gender_confirmed_by_user=False,
    )
    confirmed = _speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_01",
        predicted_gender="female",
        attribute_confidence={"gender": 0.8},
        gender_confirmed_by_user=True,
    )

    redact_view.redact_speaker_rows(db_session, [inferred, confirmed])

    assert inferred.predicted_gender is None
    # Only what the gender model produced goes; the age estimate is a separate switch.
    assert inferred.attribute_confidence == {"age_range": 0.6}
    assert confirmed.predicted_gender == "female"
    assert confirmed.attribute_confidence == {"gender": 0.8}


def test_inferred_gender_stays_when_gender_inference_is_on(db_session, normal_user):
    file = _file_in(db_session, normal_user, _org(db_session))
    speaker = _speaker(
        db_session, normal_user, file, predicted_gender="male", gender_confirmed_by_user=False
    )

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert speaker.predicted_gender == "male"


def test_speaker_gender_is_what_to_act_on_not_what_is_stored(db_session, normal_user):
    off_org = _org(db_session, gender=False)
    file = _file_in(db_session, normal_user, off_org)
    inferred = _speaker(db_session, normal_user, file, name="A", predicted_gender="male")
    confirmed = _speaker(
        db_session,
        normal_user,
        file,
        name="B",
        predicted_gender="female",
        gender_confirmed_by_user=True,
    )
    view = PrivacyView(db_session)

    assert view.speaker_gender(inferred) is None
    assert view.speaker_gender(confirmed) == "female"


# ---------------------------------------------------------------------------
# Profile consensus
# ---------------------------------------------------------------------------


def _profile_with_members(db, user, org, members: list[tuple[str, bool]]) -> SpeakerProfile:
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=user.id, name="P")
    profile.organization_id = org.id if org else None
    db.add(profile)
    db.flush()
    for i, (gender, confirmed) in enumerate(members):
        file = _file_in(db, user, org)
        _speaker(
            db,
            user,
            file,
            name=f"SPEAKER_{i:02d}",
            predicted_gender=gender,
            gender_confirmed_by_user=confirmed,
            profile_id=profile.id,
        )
    return profile


MEMBERS = [("male", False), ("male", False), ("female", True)]


def test_the_profile_majority_counts_inferred_genders_while_the_switch_is_on(
    db_session, normal_user
):
    org = _org(db_session)
    profile = _profile_with_members(db_session, normal_user, org, MEMBERS)

    assert redact_view.derived_profile_genders(db_session, [profile]) == {profile.id: "male"}


def test_the_profile_majority_counts_only_confirmed_genders_while_the_switch_is_off(
    db_session, normal_user
):
    org = _org(db_session, gender=False)
    profile = _profile_with_members(db_session, normal_user, org, MEMBERS)

    assert redact_view.derived_profile_genders(db_session, [profile]) == {profile.id: "female"}


def test_a_profile_with_only_inferred_genders_has_none_while_the_switch_is_off(
    db_session, normal_user
):
    org = _org(db_session, gender=False)
    profile = _profile_with_members(db_session, normal_user, org, [("male", False)])

    assert redact_view.derived_profile_genders(db_session, [profile]) == {}
