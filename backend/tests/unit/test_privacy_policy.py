"""The privacy policy layer: restrict-only layers, fail-closed errors, consent, write backstop.

State is written straight into ``system_settings``, ``organization_policy`` and
``user_setting`` and read back through the public gates, so these tests exercise the same
queries the pipeline will. Every "off" case has an "on" control beside it, because a gate that
always answers False would pass the off cases.
"""

from __future__ import annotations

import itertools
import logging
import uuid as uuid_pkg
from typing import cast

import pytest
from sqlalchemy import text

from app.core import constants
from app.models.media import MediaFile
from app.models.media import SpeakerProfile
from app.models.organization import Organization
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.models.system_settings import SystemSettings
from app.services.privacy import policy
from app.services.privacy.consent import VoiceprintConsentRequest
from app.services.privacy.consent import clear_voiceprint_consent_hooks
from app.services.privacy.consent import register_voiceprint_consent_hook
from app.services.privacy.policy import PrivacyCeiling

INSTANCE_KEYS = {
    "voiceprints": constants.PRIVACY_VOICEPRINT_MATCHING_KEY,
    "gender": constants.PRIVACY_GENDER_INFERENCE_KEY,
}
USER_KEYS = {
    "voiceprints": constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
    "gender": constants.PRIVACY_USER_GENDER_DETECTION_KEY,
}
ORG_COLUMNS = {
    "voiceprints": "voiceprint_matching_enabled",
    "gender": "gender_inference_enabled",
}
FEATURES = ("voiceprints", "gender")


@pytest.fixture(autouse=True)
def _clean_privacy_state():
    """The resolver, the hooks and the backstop cache are process-wide; never leak them."""
    clear_voiceprint_consent_hooks()
    policy.reset_privacy_ceiling_resolver()
    yield
    clear_voiceprint_consent_hooks()
    policy.reset_privacy_ceiling_resolver()


def _org(db) -> Organization:
    org = Organization(name=f"org-{uuid_pkg.uuid4().hex[:8]}")
    db.add(org)
    db.flush()
    return org


def _set_instance(db, feature: str, value: str) -> None:
    db.add(SystemSettings(key=INSTANCE_KEYS[feature], value=value))
    db.flush()


def _set_org(db, org_id: int, **flags: bool) -> None:
    row = db.get(OrganizationPolicy, org_id)
    if row is None:
        row = OrganizationPolicy(organization_id=org_id)
        db.add(row)
    for feature, value in flags.items():
        setattr(row, ORG_COLUMNS[feature], value)
    db.flush()


def _set_user(db, user_id: int, feature: str, value: str | None) -> None:
    db.add(UserSetting(user_id=user_id, setting_key=USER_KEYS[feature], setting_value=value))
    db.flush()


def _file(db, owner_id: int, org_id: int | None) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        filename="policy_probe.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        user_id=owner_id,
        organization_id=org_id,
    )
    db.add(media_file)
    db.flush()
    return media_file


def _profile(db, owner_id: int, org_id: int | None) -> SpeakerProfile:
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner_id,
        organization_id=org_id,
        name=f"probe-{uuid_pkg.uuid4().hex[:8]}",
    )
    db.add(profile)
    db.flush()
    return profile


def _decision(db, feature: str, user_id, org_id):
    resolved = policy.resolve_privacy_policy(db, user_id=user_id, organization_id=org_id)
    return getattr(resolved, feature)


# --- the layer matrix ----------------------------------------------------------------------


def test_everything_is_on_with_no_rows_no_resolver_and_no_hooks(db_session, normal_user):
    org = _org(db_session)
    resolved = policy.resolve_privacy_policy(
        db_session, user_id=normal_user.id, organization_id=org.id
    )
    assert resolved == policy.PrivacyPolicy(policy.Decision(True), policy.Decision(True))
    assert resolved.voiceprints.locked_by is None


@pytest.mark.parametrize("feature", FEATURES)
@pytest.mark.parametrize(
    "instance_on,org_on,user_on", list(itertools.product([True, False], repeat=3))
)
def test_effective_is_the_and_of_instance_org_user_and_locked_by_is_the_highest_off(
    db_session, normal_user, feature, instance_on, org_on, user_on
):
    org = _org(db_session)
    _set_instance(db_session, feature, "true" if instance_on else "false")
    _set_org(db_session, org.id, **{feature: org_on})
    _set_user(db_session, normal_user.id, feature, "true" if user_on else "false")

    decision = _decision(db_session, feature, normal_user.id, org.id)

    assert decision.enabled is (instance_on and org_on and user_on)
    expected = next(
        (
            name
            for name, on in (("instance", instance_on), ("organization", org_on), ("user", user_on))
            if not on
        ),
        None,
    )
    assert decision.locked_by == expected


def test_the_two_features_are_independent(db_session, normal_user):
    org = _org(db_session)
    _set_org(db_session, org.id, voiceprints=False)

    resolved = policy.resolve_privacy_policy(
        db_session, user_id=normal_user.id, organization_id=org.id
    )

    assert resolved.voiceprints == policy.Decision(False, "organization")
    assert resolved.gender == policy.Decision(True)


def test_personal_scope_never_reads_an_organization_row(db_session, normal_user):
    org = _org(db_session)
    _set_org(db_session, org.id, voiceprints=False, gender=False)

    personal = policy.resolve_privacy_policy(
        db_session, user_id=normal_user.id, organization_id=None
    )
    in_org = policy.resolve_privacy_policy(
        db_session, user_id=normal_user.id, organization_id=org.id
    )

    assert personal.voiceprints.enabled and personal.gender.enabled
    assert not in_org.voiceprints.enabled and not in_org.gender.enabled


def test_one_organization_off_does_not_affect_another(db_session, normal_user):
    off, on = _org(db_session), _org(db_session)
    _set_org(db_session, off.id, voiceprints=False)
    _set_org(db_session, on.id, voiceprints=True)

    assert not _decision(db_session, "voiceprints", normal_user.id, off.id).enabled
    assert _decision(db_session, "voiceprints", normal_user.id, on.id).enabled


def test_one_users_switch_does_not_affect_another_user(db_session, normal_user, other_user):
    _set_user(db_session, normal_user.id, "voiceprints", "false")

    assert not _decision(db_session, "voiceprints", normal_user.id, None).enabled
    assert _decision(db_session, "voiceprints", other_user.id, None).enabled


@pytest.mark.parametrize("feature", FEATURES)
@pytest.mark.parametrize("junk", ["", "maybe", "0", "off"])
def test_an_unparseable_instance_value_reads_as_off(db_session, normal_user, feature, junk):
    _set_instance(db_session, feature, junk)

    decision = _decision(db_session, feature, normal_user.id, None)

    assert decision == policy.Decision(False, "instance")


def test_a_null_instance_value_reads_as_off(db_session, normal_user):
    db_session.add(SystemSettings(key=INSTANCE_KEYS["voiceprints"], value=None))
    db_session.flush()

    assert _decision(db_session, "voiceprints", normal_user.id, None).enabled is False


@pytest.mark.parametrize("truthy", ["true", "TRUE", "1", "yes", "on"])
def test_truthy_spellings_read_as_on(db_session, normal_user, truthy):
    _set_instance(db_session, "voiceprints", truthy)

    assert _decision(db_session, "voiceprints", normal_user.id, None).enabled is True


def test_a_user_who_never_chose_inherits_while_a_user_who_chose_off_is_off(db_session, normal_user):
    assert _decision(db_session, "gender", normal_user.id, None).enabled
    _set_user(db_session, normal_user.id, "gender", "false")
    assert _decision(db_session, "gender", normal_user.id, None) == policy.Decision(False, "user")


# --- platform ceiling ----------------------------------------------------------------------


def test_the_ceiling_resolver_can_only_restrict(db_session, normal_user):
    org = _org(db_session)
    policy.set_privacy_ceiling_resolver(lambda db, org_id: PrivacyCeiling(voiceprints=False))

    resolved = policy.resolve_privacy_policy(
        db_session, user_id=normal_user.id, organization_id=org.id
    )

    assert resolved.voiceprints == policy.Decision(False, "platform")
    assert resolved.gender == policy.Decision(True)


def test_a_true_ceiling_does_not_override_a_lower_layer_that_is_off(db_session, normal_user):
    _set_instance(db_session, "voiceprints", "false")
    policy.set_privacy_ceiling_resolver(
        lambda db, org_id: PrivacyCeiling(voiceprints=True, gender=True)
    )

    assert _decision(db_session, "voiceprints", normal_user.id, None) == policy.Decision(
        False, "instance"
    )
    assert _decision(db_session, "gender", normal_user.id, None).enabled


def test_platform_is_named_before_instance_when_both_are_off(db_session, normal_user):
    _set_instance(db_session, "voiceprints", "false")
    policy.set_privacy_ceiling_resolver(lambda db, org_id: PrivacyCeiling(voiceprints=False))

    assert _decision(db_session, "voiceprints", normal_user.id, None).locked_by == "platform"


def test_the_ceiling_resolver_is_asked_per_organization(db_session, normal_user):
    org = _org(db_session)
    seen: list[int | None] = []

    def resolver(db, org_id):
        seen.append(org_id)
        return PrivacyCeiling(voiceprints=org_id != org.id)

    policy.set_privacy_ceiling_resolver(resolver)

    assert not _decision(db_session, "voiceprints", normal_user.id, org.id).enabled
    assert _decision(db_session, "voiceprints", normal_user.id, None).enabled
    assert seen == [org.id, None]


def test_a_none_answer_from_the_resolver_means_no_opinion(db_session, normal_user):
    policy.set_privacy_ceiling_resolver(lambda db, org_id: None)

    assert _decision(db_session, "voiceprints", normal_user.id, None) == policy.Decision(True)


def test_a_raising_ceiling_resolver_denies_both_features(db_session, normal_user):
    def boom(db, org_id):
        raise RuntimeError("resolver outage")

    policy.set_privacy_ceiling_resolver(boom)

    resolved = policy.resolve_privacy_policy(
        db_session, user_id=normal_user.id, organization_id=None
    )

    assert resolved.voiceprints == policy.Decision(False, "platform")
    assert resolved.gender == policy.Decision(False, "platform")


@pytest.mark.parametrize("bad", [True, {"voiceprints": True}, "yes", 1])
def test_a_malformed_ceiling_result_denies_both_features(db_session, normal_user, bad):
    policy.set_privacy_ceiling_resolver(lambda db, org_id: bad)

    resolved = policy.resolve_privacy_policy(
        db_session, user_id=normal_user.id, organization_id=None
    )

    assert not resolved.voiceprints.enabled and not resolved.gender.enabled


def test_a_non_bool_ceiling_field_denies_only_that_feature(db_session, normal_user):
    # A resolver written against an untyped extension can hand back anything; the cast
    # deliberately smuggles a str past the annotation to prove the runtime check.
    wrong_type = cast(bool, "yes")
    policy.set_privacy_ceiling_resolver(lambda db, org_id: PrivacyCeiling(voiceprints=wrong_type))

    resolved = policy.resolve_privacy_policy(
        db_session, user_id=normal_user.id, organization_id=None
    )

    assert not resolved.voiceprints.enabled
    assert resolved.gender.enabled


def test_reset_restores_the_community_resolver(db_session, normal_user):
    policy.set_privacy_ceiling_resolver(lambda db, org_id: PrivacyCeiling(voiceprints=False))
    policy.reset_privacy_ceiling_resolver()

    assert _decision(db_session, "voiceprints", normal_user.id, None) == policy.Decision(True)


# --- fail closed on a storage error --------------------------------------------------------


@pytest.mark.ddl_exclusive
def test_an_unreadable_policy_store_denies_and_leaves_the_session_usable(db_session, normal_user):
    org = _org(db_session)
    db_session.execute(text("DROP TABLE organization_policy"))

    resolved = policy.resolve_privacy_policy(
        db_session, user_id=normal_user.id, organization_id=org.id
    )

    assert resolved.voiceprints == policy.Decision(False, "instance")
    assert resolved.gender == policy.Decision(False, "instance")
    # The failed read was isolated in a savepoint: the caller's transaction still works.
    assert db_session.execute(text("SELECT 1")).scalar() == 1


# --- consent -------------------------------------------------------------------------------


def test_a_consent_hook_denies_creation_but_not_gender(db_session, normal_user):
    register_voiceprint_consent_hook(lambda db, request: False)

    assert policy.voiceprint_decision(
        db_session, user_id=normal_user.id, organization_id=None
    ) == policy.Decision(False, "consent")
    assert policy.gender_decision(db_session, user_id=normal_user.id, organization_id=None).enabled


def test_every_hook_must_allow(db_session, normal_user):
    register_voiceprint_consent_hook(lambda db, request: True)
    assert policy.voiceprint_decision(
        db_session, user_id=normal_user.id, organization_id=None
    ).enabled
    register_voiceprint_consent_hook(lambda db, request: False)
    assert not policy.voiceprint_decision(
        db_session, user_id=normal_user.id, organization_id=None
    ).enabled


def test_a_raising_hook_denies(db_session, normal_user, caplog):
    def broken(db, request):
        raise RuntimeError("hook bug")

    register_voiceprint_consent_hook(broken)

    with caplog.at_level(logging.ERROR):
        decision = policy.voiceprint_decision(
            db_session, user_id=normal_user.id, organization_id=None
        )

    assert decision == policy.Decision(False, "consent")
    assert "broken" in caplog.text


@pytest.mark.parametrize("answer", [1, "yes", None, object()])
def test_a_hook_that_does_not_return_the_literal_true_denies(db_session, normal_user, answer):
    register_voiceprint_consent_hook(lambda db, request: answer)

    assert not policy.voiceprint_decision(
        db_session, user_id=normal_user.id, organization_id=None
    ).enabled


def test_a_hook_whose_query_fails_denies_and_the_session_survives(db_session, normal_user):
    def broken_query(db, request):
        db.execute(text("SELECT * FROM table_that_does_not_exist"))
        return True

    register_voiceprint_consent_hook(broken_query)

    decision = policy.voiceprint_decision(db_session, user_id=normal_user.id, organization_id=None)

    assert decision == policy.Decision(False, "consent")
    assert db_session.execute(text("SELECT 1")).scalar() == 1


def test_the_create_request_carries_the_subject_tenant_and_file(db_session, normal_user):
    org = _org(db_session)
    media_file = _file(db_session, normal_user.id, org.id)
    seen: list[VoiceprintConsentRequest] = []

    def hook(db, request):
        seen.append(request)
        return True

    register_voiceprint_consent_hook(hook)

    assert policy.voiceprints_allowed_for_file(db_session, media_file.id)

    assert seen == [
        VoiceprintConsentRequest(
            purpose="create",
            subject_user_id=normal_user.id,
            organization_id=org.id,
            media_file_id=media_file.id,
        )
    ]


def test_hooks_are_not_asked_when_a_layer_already_says_no(db_session, normal_user):
    _set_instance(db_session, "voiceprints", "false")
    asked: list[int] = []

    def hook(db, request):
        asked.append(1)
        return True

    register_voiceprint_consent_hook(hook)

    decision = policy.voiceprint_decision(db_session, user_id=normal_user.id, organization_id=None)

    assert decision == policy.Decision(False, "instance")
    assert asked == []


def test_registering_the_same_hook_twice_asks_it_once(db_session, normal_user):
    asked: list[int] = []

    def hook(db, request):
        asked.append(1)
        return True

    register_voiceprint_consent_hook(hook)
    register_voiceprint_consent_hook(hook)
    policy.voiceprint_decision(db_session, user_id=normal_user.id, organization_id=None)

    assert asked == [1]


def test_with_hooks_registered_an_unknown_subject_is_denied(db_session):
    register_voiceprint_consent_hook(lambda db, request: True)

    assert not policy.voiceprint_decision(db_session, user_id=None, organization_id=None).enabled


# --- file gates ----------------------------------------------------------------------------


def test_the_file_gate_uses_the_files_owner_and_organization(db_session, normal_user, other_user):
    off_org, on_org = _org(db_session), _org(db_session)
    _set_org(db_session, off_org.id, voiceprints=False, gender=False)
    in_off_org = _file(db_session, normal_user.id, off_org.id)
    in_on_org = _file(db_session, normal_user.id, on_org.id)
    personal = _file(db_session, normal_user.id, None)

    assert not policy.voiceprints_allowed_for_file(db_session, in_off_org.id)
    assert not policy.gender_allowed_for_file(db_session, in_off_org.id)
    assert policy.voiceprints_allowed_for_file(db_session, in_on_org.id)
    assert policy.gender_allowed_for_file(db_session, in_on_org.id)
    assert policy.voiceprints_allowed_for_file(db_session, personal.id)


def test_the_file_gate_reads_the_owners_switch_not_anyone_elses(
    db_session, normal_user, other_user
):
    _set_user(db_session, other_user.id, "voiceprints", "false")
    mine = _file(db_session, normal_user.id, None)
    theirs = _file(db_session, other_user.id, None)

    assert policy.voiceprints_allowed_for_file(db_session, mine.id)
    assert policy.voiceprint_decision_for_file(db_session, theirs.id) == policy.Decision(
        False, "user"
    )


def test_the_file_gate_is_fresh_each_call(db_session, normal_user):
    media_file = _file(db_session, normal_user.id, None)
    assert policy.voiceprints_allowed_for_file(db_session, media_file.id)

    _set_instance(db_session, "voiceprints", "false")

    assert not policy.voiceprints_allowed_for_file(db_session, media_file.id)


def test_a_missing_file_denies_and_the_session_survives(db_session):
    assert policy.voiceprints_allowed_for_file(db_session, 2_000_000_000) is False
    assert policy.gender_allowed_for_file(db_session, 2_000_000_000) is False
    assert db_session.execute(text("SELECT 1")).scalar() == 1


# --- shared profiles -----------------------------------------------------------------------


def test_own_profiles_are_kept_and_shared_ones_follow_the_owners_switch(
    db_session, normal_user, other_user
):
    mine = _profile(db_session, normal_user.id, None)
    theirs = _profile(db_session, other_user.id, None)

    assert policy.filter_shareable_profiles(db_session, normal_user.id, [mine.id, theirs.id]) == [
        mine.id,
        theirs.id,
    ]

    _set_user(db_session, other_user.id, "voiceprints", "false")

    assert policy.filter_shareable_profiles(db_session, normal_user.id, [mine.id, theirs.id]) == [
        mine.id
    ]


def test_a_profile_in_an_organization_that_is_off_is_dropped_for_other_users(
    db_session, normal_user, other_user
):
    org = _org(db_session)
    in_org = _profile(db_session, other_user.id, org.id)
    personal = _profile(db_session, other_user.id, None)
    _set_org(db_session, org.id, voiceprints=False)

    kept = policy.filter_shareable_profiles(db_session, normal_user.id, [in_org.id, personal.id])

    assert kept == [personal.id]


def test_a_requesters_own_profile_is_kept_even_when_their_own_switch_is_off(
    db_session, normal_user
):
    _set_user(db_session, normal_user.id, "voiceprints", "false")
    mine = _profile(db_session, normal_user.id, None)

    assert policy.filter_shareable_profiles(db_session, normal_user.id, [mine.id]) == [mine.id]


def test_the_share_hook_is_asked_once_per_owner_with_the_requester(
    db_session, normal_user, other_user
):
    first = _profile(db_session, other_user.id, None)
    second = _profile(db_session, other_user.id, None)
    seen: list[VoiceprintConsentRequest] = []

    def hook(db, request):
        seen.append(request)
        return request.purpose == "share"

    register_voiceprint_consent_hook(hook)

    kept = policy.filter_shareable_profiles(db_session, normal_user.id, [first.id, second.id])

    assert kept == [first.id, second.id]
    assert seen == [
        VoiceprintConsentRequest(
            purpose="share",
            subject_user_id=other_user.id,
            organization_id=None,
            profile_ids=(first.id, second.id),
            requester_user_id=normal_user.id,
        )
    ]


def test_a_denying_share_hook_drops_the_owners_profiles_but_not_the_requesters(
    db_session, normal_user, other_user
):
    mine = _profile(db_session, normal_user.id, None)
    theirs = _profile(db_session, other_user.id, None)
    register_voiceprint_consent_hook(lambda db, request: False)

    assert policy.filter_shareable_profiles(db_session, normal_user.id, [theirs.id, mine.id]) == [
        mine.id
    ]


def test_a_raising_share_hook_drops_the_owners_profiles(db_session, normal_user, other_user):
    theirs = _profile(db_session, other_user.id, None)

    def broken(db, request):
        raise RuntimeError("hook bug")

    register_voiceprint_consent_hook(broken)

    assert policy.filter_shareable_profiles(db_session, normal_user.id, [theirs.id]) == []


def test_unknown_and_duplicate_profile_ids_and_empty_input(db_session, normal_user):
    mine = _profile(db_session, normal_user.id, None)

    assert policy.filter_shareable_profiles(
        db_session, normal_user.id, [mine.id, 2_000_000_000, mine.id]
    ) == [mine.id]
    assert policy.filter_shareable_profiles(db_session, normal_user.id, []) == []


# --- the write backstop --------------------------------------------------------------------


@pytest.fixture
def backstop(db_session):
    policy.clear_write_backstop_cache()
    yield lambda user_id, org_id: policy.assert_voiceprint_write_allowed(
        user_id, org_id, db=db_session
    )
    policy.clear_write_backstop_cache()


def test_the_backstop_allows_an_enabled_scope_and_refuses_a_disabled_one(
    db_session, normal_user, backstop
):
    off, on = _org(db_session), _org(db_session)
    _set_org(db_session, off.id, voiceprints=False)

    assert backstop(normal_user.id, on.id) is True
    assert backstop(normal_user.id, off.id) is False


def test_the_backstop_honours_the_user_instance_and_platform_layers(
    db_session, normal_user, other_user, backstop
):
    _set_user(db_session, normal_user.id, "voiceprints", "false")
    assert backstop(normal_user.id, None) is False
    assert backstop(other_user.id, None) is True

    policy.clear_write_backstop_cache()
    _set_instance(db_session, "voiceprints", "false")
    assert backstop(other_user.id, None) is False

    policy.clear_write_backstop_cache()
    db_session.query(SystemSettings).delete()
    policy.set_privacy_ceiling_resolver(lambda db, org_id: PrivacyCeiling(voiceprints=False))
    assert backstop(other_user.id, None) is False


def test_the_backstop_caches_within_the_ttl_and_rereads_after_it(
    db_session, normal_user, backstop, monkeypatch
):
    assert backstop(normal_user.id, None) is True

    _set_instance(db_session, "voiceprints", "false")
    assert backstop(normal_user.id, None) is True  # cached: the documented staleness window

    monkeypatch.setattr(constants, "PRIVACY_WRITE_BACKSTOP_TTL_SECONDS", 0.0)
    policy.clear_write_backstop_cache()
    assert backstop(normal_user.id, None) is False


def test_clearing_the_cache_applies_a_change_at_once(db_session, normal_user, backstop):
    assert backstop(normal_user.id, None) is True
    _set_instance(db_session, "voiceprints", "false")

    policy.clear_write_backstop_cache()

    assert backstop(normal_user.id, None) is False


def test_changing_the_ceiling_resolver_clears_the_cache(db_session, normal_user, backstop):
    assert backstop(normal_user.id, None) is True

    policy.set_privacy_ceiling_resolver(lambda db, org_id: PrivacyCeiling(voiceprints=False))

    assert backstop(normal_user.id, None) is False


def test_a_denied_scope_is_logged_once(db_session, normal_user, backstop, monkeypatch, caplog):
    _set_instance(db_session, "voiceprints", "false")
    monkeypatch.setattr(constants, "PRIVACY_WRITE_BACKSTOP_TTL_SECONDS", 0.0)

    with caplog.at_level(logging.WARNING, logger=policy.logger.name):
        for _ in range(3):
            assert backstop(normal_user.id, None) is False

    assert caplog.text.count("voiceprint write refused by policy") == 1
    assert "locked_by=instance" in caplog.text


def test_the_backstop_cache_is_per_scope(db_session, normal_user, other_user, backstop):
    _set_user(db_session, normal_user.id, "voiceprints", "false")

    assert backstop(normal_user.id, None) is False
    assert backstop(other_user.id, None) is True
    assert backstop(normal_user.id, None) is False


@pytest.mark.ddl_exclusive
def test_a_backstop_that_cannot_decide_denies_and_does_not_cache_the_denial(
    db_session, normal_user, backstop
):
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "v445_add_organization_policy.py"
    )
    spec = importlib.util.spec_from_file_location("v445", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    org = _org(db_session)
    db_session.execute(text("DROP TABLE organization_policy"))
    assert backstop(normal_user.id, org.id) is False

    db_session.execute(text(migration.UPGRADE_SQL))
    assert backstop(normal_user.id, org.id) is True


def test_the_backstop_opens_its_own_session_when_none_is_given(monkeypatch, normal_user):
    """Without ``db`` it must call the session factory, not require a caller session."""
    from contextlib import contextmanager

    opened: list[int] = []

    @contextmanager
    def fake_scope():
        opened.append(1)
        raise RuntimeError("no database in this test")
        yield  # pragma: no cover

    monkeypatch.setattr("app.db.session_utils.session_scope", fake_scope)
    policy.clear_write_backstop_cache()

    assert policy.assert_voiceprint_write_allowed(normal_user.id, None) is False
    assert opened == [1]
