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


@pytest.mark.parametrize("source", ["llm_analysis", "metadata_hint"])
def test_a_non_voiceprint_suggestion_is_not_hidden(db_session, normal_user, source):
    off_org = _org(db_session, voiceprints=False)
    file = _file_in(db_session, normal_user, off_org)
    speaker = _speaker(
        db_session,
        normal_user,
        file,
        suggested_name="Alice",
        confidence=0.9,
        suggestion_source=source,
        verified=False,
    )

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert (speaker.suggested_name, speaker.suggestion_source) == ("Alice", source)


@pytest.mark.parametrize("identity_source", ["human", None])
def test_a_voice_suggestion_on_a_person_verified_speaker_is_hidden_but_the_name_stays(
    db_session, normal_user, identity_source
):
    off_org = _org(db_session, voiceprints=False)
    file = _file_in(db_session, normal_user, off_org)
    speaker = _speaker(
        db_session,
        normal_user,
        file,
        display_name="Bea",
        suggested_name="Alice",
        confidence=0.9,
        suggestion_source="voice_match",
        suggestion_detail={"origin": "voice_match", "matched_speaker_uuid": str(uuid_pkg.uuid4())},
        verified=True,
        identity_source=identity_source,
    )

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert (speaker.suggested_name, speaker.suggestion_source) == (None, None)
    assert (speaker.confidence, speaker.suggestion_detail) == (None, None)
    assert (speaker.display_name, speaker.verified) == ("Bea", True), "the person's word stays"


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
        attribute_confidence={
            "gender": 0.93,
            "age_range": 0.6,
            "alignment": "match",
            "alignment_hint": "Zylofenix Quorra",
        },
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


# ---------------------------------------------------------------------------
# Names and links a voiceprint made (speaker.identity_source, Phase 0 of the provenance plan)
# ---------------------------------------------------------------------------


def _identified(db, user, org, *, source, name="Alice", label="SPEAKER_00") -> Any:
    """A speaker named and linked the way an auto-accepted profile match leaves it."""
    org_id = org.id if org else None
    profile = (
        db.query(SpeakerProfile)
        .filter_by(user_id=user.id, organization_id=org_id, name=name)
        .one_or_none()
    )
    if profile is None:
        profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=user.id, name=name)
        profile.organization_id = org_id
        db.add(profile)
        db.flush()
    return _speaker(
        db,
        user,
        _file_in(db, user, org),
        name=label,
        display_name=name,
        verified=True,
        profile_id=profile.id,
        suggested_name=name,
        suggestion_source="profile_match",
        confidence=0.82,
        identity_source=source,
        resolved_display_name=name,
        computed_status="verified",
    )


def _identity_view(speaker) -> tuple:
    return (
        speaker.display_name,
        speaker.verified,
        speaker.profile_id,
        speaker.suggested_name,
        speaker.resolved_display_name,
    )


def test_a_voiceprint_made_identity_is_hidden_in_memory_and_kept_in_the_database(
    db_session, normal_user
):
    from app.services.speaker_status_service import SpeakerStatusService

    speaker = _identified(
        db_session, normal_user, _org(db_session, voiceprints=False), source="voiceprint"
    )
    db_session.commit()
    stored_before = _identity_view(speaker)

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert _identity_view(speaker) == (None, False, None, None, "SPEAKER_00")
    assert speaker.profile is None
    assert speaker.computed_status == SpeakerStatusService.STATUS_UNVERIFIED
    db_session.expire(speaker)
    assert _identity_view(db_session.get(Speaker, speaker.id)) == stored_before


def test_a_voiceprint_made_identity_is_shown_while_voiceprints_are_on(db_session, normal_user):
    speaker = _identified(db_session, normal_user, _org(db_session), source="voiceprint")
    db_session.commit()
    before = _identity_view(speaker)

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert _identity_view(speaker) == before
    assert before[0] == "Alice"


def test_a_voiceprint_link_hides_only_the_link(db_session, normal_user):
    speaker = _identified(
        db_session, normal_user, _org(db_session, voiceprints=False), source="voiceprint_link"
    )
    db_session.commit()

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert (speaker.display_name, speaker.verified, speaker.profile_id) == ("Alice", True, None)
    assert speaker.resolved_display_name == "Alice"


@pytest.mark.parametrize("source", ["human", None])
def test_a_typed_name_is_never_hidden(db_session, normal_user, source):
    speaker = _identified(
        db_session, normal_user, _org(db_session, voiceprints=False), source=source
    )
    db_session.commit()
    profile_id = speaker.profile_id

    redact_view.redact_speaker_rows(db_session, [speaker])

    assert (speaker.display_name, speaker.verified, speaker.profile_id) == (
        "Alice",
        True,
        profile_id,
    )


def test_a_view_labels_a_row_with_its_files_answer(db_session, normal_user):
    off = _identified(
        db_session, normal_user, _org(db_session, voiceprints=False), source="voiceprint"
    )
    on = _identified(db_session, normal_user, _org(db_session), source="voiceprint")
    view = PrivacyView(db_session)

    assert (view.label_for_row(off), view.label_for_row(on)) == ("SPEAKER_00", "Alice")


def _columns_row(db, speaker, *, with_identity: bool):
    columns = [
        Speaker.id,
        Speaker.name,
        Speaker.display_name,
        Speaker.suggested_name,
        Speaker.confidence,
        Speaker.suggestion_source,
        Speaker.verified,
        Speaker.media_file_id,
    ]
    if with_identity:
        columns.append(Speaker.identity_source)
    return db.query(*columns).filter(Speaker.id == speaker.id).one()


def test_a_columns_only_row_that_forgot_identity_source_fails_loudly_when_off(
    db_session, normal_user
):
    from app.utils.speaker_labels import canonical_speaker_label_for_row

    speaker = _identified(
        db_session, normal_user, _org(db_session, voiceprints=False), source="voiceprint"
    )

    with pytest.raises(TypeError, match="identity_source"):
        canonical_speaker_label_for_row(
            _columns_row(db_session, speaker, with_identity=False), db_session
        )
    assert (
        canonical_speaker_label_for_row(
            _columns_row(db_session, speaker, with_identity=True), db_session
        )
        == "SPEAKER_00"
    )


def test_a_columns_only_row_without_identity_source_still_labels_while_on(db_session, normal_user):
    from app.utils.speaker_labels import canonical_speaker_label_for_row

    speaker = _identified(db_session, normal_user, _org(db_session), source="voiceprint")

    row = _columns_row(db_session, speaker, with_identity=False)
    assert canonical_speaker_label_for_row(row, db_session) == "Alice"


def test_machine_identity_clauses_select_exactly_the_off_scope_rows(
    db_session, normal_user, other_user
):
    from sqlalchemy import select

    from app.models.media import MediaFile

    # normal_user's own switch is off: their PERSONAL files (organization NULL) are off.
    db_session.add(
        UserSetting(
            user_id=normal_user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    off_org, on_org = _org(db_session, voiceprints=False), _org(db_session)
    rows = {
        "personal off": _identified(db_session, normal_user, None, source="voiceprint"),
        "org off": _identified(db_session, other_user, off_org, source="voiceprint"),
        "org off link": _identified(db_session, other_user, off_org, source="voiceprint_link"),
        "org off human": _identified(db_session, other_user, off_org, source="human"),
        "org on": _identified(db_session, other_user, on_org, source="voiceprint"),
        "personal on": _identified(db_session, other_user, None, source="voiceprint"),
    }
    db_session.flush()
    ids = {int(s.id): label for label, s in rows.items()}
    files = select(MediaFile.id).where(MediaFile.id.in_([s.media_file_id for s in rows.values()]))

    name_hidden, link_hidden = redact_view.machine_identity_clauses(db_session, files)

    def matching(clause) -> set[str]:
        hit = db_session.execute(select(Speaker.id).where(Speaker.id.in_(ids), clause)).scalars()
        return {ids[int(i)] for i in hit}

    assert matching(name_hidden) == {"personal off", "org off"}
    assert matching(link_hidden) == {"personal off", "org off", "org off link"}
    shown = dict(
        db_session.execute(
            select(Speaker.id, redact_view.visible_display_name_expr(name_hidden)).where(
                Speaker.id.in_(ids)
            )
        ).all()
    )
    assert {ids[int(k)]: v for k, v in shown.items()} == {
        "personal off": None,
        "org off": None,
        "org off link": "Alice",
        "org off human": "Alice",
        "org on": "Alice",
        "personal on": "Alice",
    }


def test_machine_identity_clauses_cost_nothing_when_no_scope_is_off(db_session, normal_user):
    from sqlalchemy import select
    from sqlalchemy.sql.elements import False_

    from app.models.media import MediaFile

    speaker = _identified(db_session, normal_user, _org(db_session), source="voiceprint")
    files = select(MediaFile.id).where(MediaFile.id == speaker.media_file_id)

    name_hidden, link_hidden = redact_view.machine_identity_clauses(db_session, files)

    assert isinstance(name_hidden, False_) and isinstance(link_hidden, False_)


def test_a_stored_status_describes_the_data_and_a_response_the_policy(db_session, normal_user):
    """Hiding is decided per read and never written down (D5): the persisted
    ``resolved_display_name`` keeps the name while voiceprints are off, the response does not."""
    from app.services.speaker_status_service import SpeakerStatusService

    speaker = _identified(
        db_session, normal_user, _org(db_session, voiceprints=False), source="voiceprint"
    )
    db_session.commit()

    assert SpeakerStatusService.update_speaker_status_in_db(db_session, int(speaker.id))
    db_session.expire_all()
    stored = db_session.get(Speaker, speaker.id)
    assert stored.resolved_display_name == "Alice"

    SpeakerStatusService.add_computed_status(stored)
    assert stored.resolved_display_name == "SPEAKER_00"


@pytest.mark.parametrize("verified", [True, False, None])
@pytest.mark.parametrize("source", ["voiceprint", "voiceprint_link", "human", None])
@pytest.mark.parametrize("suggestion_source", ["voice_match", "profile_match"])
def test_the_suggestion_predicate_does_not_read_who_verified(verified, source, suggestion_source):
    from types import SimpleNamespace

    row = SimpleNamespace(
        suggestion_source=suggestion_source, verified=verified, identity_source=source
    )
    assert redact_view.hides_voiceprint_suggestion(row) is True


@pytest.mark.parametrize(
    "suggestion_source", ["llm_analysis", "metadata_hint", "user_skipped", None]
)
def test_the_suggestion_predicate_leaves_a_suggestion_no_voiceprint_wrote(suggestion_source):
    from types import SimpleNamespace

    row = SimpleNamespace(suggestion_source=suggestion_source, verified=False, identity_source=None)
    assert redact_view.hides_voiceprint_suggestion(row) is False


# --- the file-scope question fails closed ---------------------------------------------------


def test_a_row_naming_a_file_with_no_session_to_ask_reads_as_off():
    from types import SimpleNamespace

    assert redact_view.voiceprints_off_for_row(SimpleNamespace(media_file_id=5)) is True


def test_a_row_naming_no_file_reads_as_on():
    from types import SimpleNamespace

    assert redact_view.voiceprints_off_for_row(SimpleNamespace(media_file_id=None)) is False


def test_a_policy_read_that_raises_reads_as_off(db_session, normal_user, monkeypatch):
    file = _file_in(db_session, normal_user, _org(db_session))
    speaker = _speaker(db_session, normal_user, file)
    assert redact_view.voiceprints_off_for_row(speaker, db_session) is False  # control

    def boom(self, file_id):
        raise RuntimeError("policy store unreachable")

    monkeypatch.setattr(PrivacyView, "voiceprints", boom)
    assert redact_view.voiceprints_off_for_row(speaker, db_session) is True


def test_a_row_that_did_not_select_verified_hides_its_voice_suggestion():
    from types import SimpleNamespace

    from app.utils.speaker_labels import canonical_speaker_label_for_row

    row = SimpleNamespace(
        name="SPEAKER_00",
        display_name=None,
        suggested_name="Alice",
        confidence=0.9,
        suggestion_source="voice_match",
        identity_source=None,
        media_file_id=5,
    )
    assert redact_view.hides_voiceprint_suggestion(row) is True
    # No session can be asked about file 5, so the label fails closed as well.
    assert canonical_speaker_label_for_row(row) == "SPEAKER_00"
    assert canonical_speaker_label_for_row(row, voiceprint_names=True) == "Alice"
