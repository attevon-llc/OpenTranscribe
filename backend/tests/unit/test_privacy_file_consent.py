"""The per-file voiceprint consent layer of the privacy decision (issue #1356).

Restrict-only: a file can turn voiceprints off, never on. The layer sits after the stored layers
(platform, instance, organization, user) and before the consent hooks.
"""

from __future__ import annotations

import pytest

from app.core import constants
from app.services.privacy import policy
from app.services.privacy.consent import VoiceprintConsentRequest
from app.services.privacy.consent import clear_voiceprint_consent_hooks
from app.services.privacy.consent import register_voiceprint_consent_hook
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.privacy_switches import upsert_system_setting


@pytest.fixture(autouse=True)
def _clean():
    policy.reset_privacy_ceiling_resolver()
    policy.clear_write_backstop_cache()
    clear_voiceprint_consent_hooks()
    yield
    clear_voiceprint_consent_hooks()
    policy.reset_privacy_ceiling_resolver()
    policy.clear_write_backstop_cache()


def _state(db, media_file, state):
    media_file.voiceprint_consent = state
    db.flush()


def test_declined_file_denies_with_locked_by_file(db_session, normal_user):
    mf = file_in(db_session, normal_user, None)
    _state(db_session, mf, "declined")

    decision = policy.voiceprint_decision_for_file(db_session, mf.id)

    assert (decision.enabled, decision.locked_by) == (False, "file")


def test_unspecified_and_granted_files_are_allowed_by_default(db_session, normal_user):
    unspecified = file_in(db_session, normal_user, None)
    granted = file_in(db_session, normal_user, None)
    _state(db_session, granted, "granted")

    assert policy.voiceprints_allowed_for_file(db_session, unspecified.id)
    assert policy.voiceprints_allowed_for_file(db_session, granted.id)


def test_granted_file_does_not_override_an_organization_that_is_off(db_session, normal_user):
    org = make_org(db_session, voiceprints=False)
    mf = file_in(db_session, normal_user, org)
    _state(db_session, mf, "granted")

    decision = policy.voiceprint_decision_for_file(db_session, mf.id)

    assert (decision.enabled, decision.locked_by) == (False, "organization")


def test_granted_file_does_not_override_the_instance_switch(db_session, normal_user):
    set_instance_switch(db_session, "voiceprints", False)
    mf = file_in(db_session, normal_user, None)
    _state(db_session, mf, "granted")

    decision = policy.voiceprint_decision_for_file(db_session, mf.id)

    assert (decision.enabled, decision.locked_by) == (False, "instance")


def test_required_setting_denies_everything_but_granted(db_session, normal_user):
    upsert_system_setting(
        db_session, constants.PRIVACY_VOICEPRINT_UPLOAD_CONSENT_REQUIRED_KEY, "true"
    )
    unspecified = file_in(db_session, normal_user, None)
    granted = file_in(db_session, normal_user, None)
    _state(db_session, granted, "granted")

    denied = policy.voiceprint_decision_for_file(db_session, unspecified.id)

    assert (denied.enabled, denied.locked_by) == (False, "file")
    assert policy.voiceprints_allowed_for_file(db_session, granted.id)


def test_required_setting_defaults_off(db_session, normal_user):
    assert constants.DEFAULT_PRIVACY_VOICEPRINT_UPLOAD_CONSENT_REQUIRED is False
    mf = file_in(db_session, normal_user, None)

    assert policy.voiceprints_allowed_for_file(db_session, mf.id)


def test_file_layer_runs_before_hooks_and_hooks_get_the_file_id(db_session, normal_user):
    seen: list[VoiceprintConsentRequest] = []

    def hook(db, request):
        seen.append(request)
        return True

    register_voiceprint_consent_hook(hook)
    declined = file_in(db_session, normal_user, None)
    _state(db_session, declined, "declined")
    allowed = file_in(db_session, normal_user, None)

    assert not policy.voiceprints_allowed_for_file(db_session, declined.id)
    assert seen == []  # a cheap deny first: the hook was never asked
    assert policy.voiceprints_allowed_for_file(db_session, allowed.id)
    assert [r.media_file_id for r in seen] == [allowed.id]


def test_unknown_file_denies(db_session, normal_user):
    assert not policy.voiceprints_allowed_for_file(db_session, 2_000_000_000)


def test_declined_file_ids_excludes_declined_unknown_and_required(db_session, normal_user):
    a = file_in(db_session, normal_user, None)
    b = file_in(db_session, normal_user, None)
    _state(db_session, b, "declined")

    assert policy.declined_file_ids(db_session, [a.id, b.id, None]) == {b.id}
    assert policy.declined_file_ids(db_session, [2_000_000_000]) == {2_000_000_000}

    upsert_system_setting(
        db_session, constants.PRIVACY_VOICEPRINT_UPLOAD_CONSENT_REQUIRED_KEY, "true"
    )
    assert policy.declined_file_ids(db_session, [a.id, b.id]) == {a.id, b.id}
