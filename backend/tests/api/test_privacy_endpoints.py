"""``/api/privacy/*`` and ``/api/admin/privacy-policy``: what applies, and who may change it.

``/privacy/effective`` is the one place a screen learns WHY voiceprints or gender are off, so its
``locked_by`` must name the highest layer that forbade it, for the file's owner and organization
(not the caller's), and must not be a way to probe a file the caller cannot see. The caller's own
switch is locked, not ignored, while a layer above it is off. The instance layer is super_admin
only, audited, and takes effect on the write backstop at once in the process that changed it.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.core import constants
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.models.system_settings import SystemSettings
from app.services.privacy import consent
from app.services.privacy.policy import assert_voiceprint_write_allowed
from app.services.privacy.policy import clear_write_backstop_cache

EFFECTIVE = "/api/privacy/effective"
ME = "/api/privacy/me"
INSTANCE = "/api/admin/privacy-policy"


@pytest.fixture(autouse=True)
def _clean():
    set_capability_resolver(lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": True})
    clear_write_backstop_cache()
    consent.clear_voiceprint_consent_hooks()
    yield
    consent.clear_voiceprint_consent_hooks()
    reset_capability_resolver()
    clear_write_backstop_cache()


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


def _set_user(db, user, key: str, value: str) -> None:
    db.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db.commit()


def _set_instance(db, key: str, value: str) -> None:
    db.add(SystemSettings(key=key, value=value))
    db.commit()


def _org_off(db, org, **switches) -> None:
    db.add(OrganizationPolicy(organization_id=org.id, **switches))
    db.commit()


# --------------------------------------------------------------------------- #
# GET /privacy/effective
# --------------------------------------------------------------------------- #


def test_everything_is_on_by_default(client, gov):
    with gov.acting_as(gov.alice):
        body = client.get(EFFECTIVE).json()
    assert body["organization_uuid"] == str(gov.org.uuid)
    assert body["voiceprint_matching"] == {"enabled": True, "locked_by": None}
    assert body["gender_inference"] == {"enabled": True, "locked_by": None}


def test_personal_scope_has_no_organization(client, gov):
    with gov.acting_as(gov.alice, org=None):
        body = client.get(EFFECTIVE).json()
    assert body["organization_uuid"] is None


@pytest.mark.parametrize(
    "layer, arrange",
    [
        (
            "instance",
            lambda db, gov: _set_instance(db, constants.PRIVACY_VOICEPRINT_MATCHING_KEY, "false"),
        ),
        (
            "organization",
            lambda db, gov: _org_off(db, gov.org, voiceprint_matching_enabled=False),
        ),
        (
            "user",
            lambda db, gov: _set_user(
                db, gov.alice, constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY, "false"
            ),
        ),
    ],
)
def test_locked_by_names_the_layer_that_turned_voiceprints_off(client, gov, layer, arrange):
    arrange(gov.db, gov)
    with gov.acting_as(gov.alice):
        body = client.get(EFFECTIVE).json()
    assert body["voiceprint_matching"] == {"enabled": False, "locked_by": layer}
    assert body["gender_inference"]["enabled"] is True, "the two features are independent"


def test_the_highest_layer_wins_the_name(client, gov):
    _set_instance(gov.db, constants.PRIVACY_VOICEPRINT_MATCHING_KEY, "false")
    _org_off(gov.db, gov.org, voiceprint_matching_enabled=False)
    with gov.acting_as(gov.alice):
        assert client.get(EFFECTIVE).json()["voiceprint_matching"]["locked_by"] == "instance"


def test_gender_is_governed_separately(client, gov):
    _org_off(gov.db, gov.org, gender_inference_enabled=False)
    with gov.acting_as(gov.alice):
        body = client.get(EFFECTIVE).json()
    assert body["gender_inference"] == {"enabled": False, "locked_by": "organization"}
    assert body["voiceprint_matching"]["enabled"] is True


def test_a_consent_hook_that_denies_is_reported_as_consent(client, gov):
    consent.register_voiceprint_consent_hook(lambda db, request: False)
    with gov.acting_as(gov.alice):
        body = client.get(EFFECTIVE).json()
    assert body["voiceprint_matching"] == {"enabled": False, "locked_by": "consent"}


def test_for_a_file_the_answer_is_the_files_owner_and_organization_not_the_callers(client, gov):
    """Bob views Alice's organization file; Alice's own switch applies, not Bob's."""
    f = gov.file(gov.alice)
    _set_user(gov.db, gov.alice, constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY, "false")
    collection = gov.collection(gov.alice)
    gov.put(collection, f)  # an organization collection: members may read the file
    with gov.acting_as(gov.bob):
        own = client.get(EFFECTIVE).json()
        for_file = client.get(EFFECTIVE, params={"file_uuid": str(f.uuid)})
    assert own["voiceprint_matching"]["enabled"] is True, "control: Bob's own scope is on"
    assert for_file.status_code == status.HTTP_200_OK, for_file.text
    assert for_file.json()["voiceprint_matching"] == {"enabled": False, "locked_by": "user"}


def test_a_file_in_another_organization_answers_with_that_organization(client, gov):
    f = gov.file(gov.alice)
    _org_off(gov.db, gov.org, voiceprint_matching_enabled=False)
    with gov.acting_as(gov.alice):
        body = client.get(EFFECTIVE, params={"file_uuid": str(f.uuid)}).json()
    assert body["organization_uuid"] == str(gov.org.uuid)
    assert body["voiceprint_matching"]["locked_by"] == "organization"


def test_a_file_the_caller_cannot_see_is_a_404_like_the_file_page(client, gov):
    private = gov.file(gov.alice, org=None)
    missing = "00000000-0000-4000-8000-000000000000"
    with gov.acting_as(gov.bob):
        hidden = client.get(EFFECTIVE, params={"file_uuid": str(private.uuid)})
        absent = client.get(EFFECTIVE, params={"file_uuid": missing})
    assert hidden.status_code == absent.status_code == status.HTTP_404_NOT_FOUND
    assert hidden.json() == absent.json()


def test_anonymous_callers_are_refused(client):
    assert client.get(EFFECTIVE).status_code == status.HTTP_401_UNAUTHORIZED
    assert client.get(ME).status_code == status.HTTP_401_UNAUTHORIZED
    assert (
        client.put(ME, json={"voiceprint_matching_enabled": False}).status_code
        == status.HTTP_401_UNAUTHORIZED
    )


# --------------------------------------------------------------------------- #
# GET|PUT /privacy/me
# --------------------------------------------------------------------------- #


def test_the_users_own_switch_defaults_on_and_persists(client, gov):
    with gov.acting_as(gov.alice):
        assert client.get(ME).json() == {"voiceprint_matching_enabled": True, "locked_by": None}
        off = client.put(ME, json={"voiceprint_matching_enabled": False})
        assert off.status_code == status.HTTP_200_OK
        assert off.json() == {"voiceprint_matching_enabled": False, "locked_by": None}
        assert client.get(ME).json()["voiceprint_matching_enabled"] is False
        assert client.get(EFFECTIVE).json()["voiceprint_matching"]["locked_by"] == "user"
        client.put(ME, json={"voiceprint_matching_enabled": True})
        assert client.get(EFFECTIVE).json()["voiceprint_matching"]["enabled"] is True
    with gov.acting_as(gov.bob):
        assert client.get(ME).json()["voiceprint_matching_enabled"] is True, "Bob is unaffected"


@pytest.mark.parametrize("layer", ["instance", "organization"])
def test_the_switch_is_locked_while_a_layer_above_is_off(client, gov, layer):
    if layer == "instance":
        _set_instance(gov.db, constants.PRIVACY_VOICEPRINT_MATCHING_KEY, "false")
    else:
        _org_off(gov.db, gov.org, voiceprint_matching_enabled=False)
    with gov.acting_as(gov.alice):
        assert client.get(ME).json()["locked_by"] == layer
        refused = client.put(ME, json={"voiceprint_matching_enabled": False})
    assert refused.status_code == status.HTTP_409_CONFLICT
    assert refused.json()["detail"] == {"code": "policy_locked", "locked_by": layer}
    gov.db.expire_all()
    stored = (
        gov.db.query(UserSetting)
        .filter(
            UserSetting.user_id == gov.alice.id,
            UserSetting.setting_key == constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
        )
        .count()
    )
    assert stored == 0, "a refused write stores nothing"


def test_the_users_switch_only_locks_in_the_organization_that_turned_it_off(client, gov):
    _org_off(gov.db, gov.org, voiceprint_matching_enabled=False)
    with gov.acting_as(gov.alice, org=None):  # personal workspace: no organization layer
        assert client.get(ME).json()["locked_by"] is None
        assert (
            client.put(ME, json={"voiceprint_matching_enabled": False}).status_code
            == status.HTTP_200_OK
        )


def test_a_non_boolean_is_rejected(client, gov):
    with gov.acting_as(gov.alice):
        assert (
            client.put(ME, json={"voiceprint_matching_enabled": "false"}).status_code
            == status.HTTP_422_UNPROCESSABLE_CONTENT
        )


# --------------------------------------------------------------------------- #
# GET|PUT /admin/privacy-policy
# --------------------------------------------------------------------------- #


def test_the_instance_layer_defaults_on_and_updates_partially(
    client, gov, super_admin_token_headers, events
):
    assert client.get(INSTANCE, headers=super_admin_token_headers).json() == {
        "voiceprint_matching_enabled": True,
        "gender_inference_enabled": True,
    }
    response = client.put(
        INSTANCE, headers=super_admin_token_headers, json={"gender_inference_enabled": False}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "voiceprint_matching_enabled": True,
        "gender_inference_enabled": False,
    }
    with gov.acting_as(gov.alice):
        assert client.get(EFFECTIVE).json()["gender_inference"] == {
            "enabled": False,
            "locked_by": "instance",
        }
    changes = [e for e in events if e["event_type"] == AuditEventType.ADMIN_SETTINGS_CHANGE]
    assert [e["details"] for e in changes] == [
        {"setting": constants.PRIVACY_GENDER_INFERENCE_KEY, "from": True, "to": False}
    ]


def test_setting_a_switch_to_its_current_value_is_not_an_event(
    client, super_admin_token_headers, events
):
    client.put(
        INSTANCE, headers=super_admin_token_headers, json={"voiceprint_matching_enabled": True}
    )
    assert not [e for e in events if e["event_type"] == AuditEventType.ADMIN_SETTINGS_CHANGE]


@pytest.mark.parametrize(
    "payload", [{}, {"voiceprint_matching_enabled": "no"}, {"gender_inference_enabled": 0}]
)
def test_invalid_instance_updates_are_422(client, super_admin_token_headers, payload):
    response = client.put(INSTANCE, headers=super_admin_token_headers, json=payload)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_a_switch_off_stops_this_processs_write_backstop_at_once(
    client, gov, super_admin_token_headers
):
    """The 10 s cache must not keep allowing writes in the process that changed the setting."""
    assert assert_voiceprint_write_allowed(gov.alice.id, gov.org.id, db=gov.db) is True  # primes it
    client.put(
        INSTANCE, headers=super_admin_token_headers, json={"voiceprint_matching_enabled": False}
    )
    assert assert_voiceprint_write_allowed(gov.alice.id, gov.org.id, db=gov.db) is False


def test_only_a_super_admin_reaches_the_instance_layer(
    client, user_token_headers, admin_token_headers
):
    body = {"voiceprint_matching_enabled": False}
    anonymous = client.put(INSTANCE, json=body)
    assert anonymous.status_code == status.HTTP_403_FORBIDDEN
    for headers in (user_token_headers, admin_token_headers):
        assert client.get(INSTANCE, headers=headers).status_code == status.HTTP_403_FORBIDDEN
        assert (
            client.put(INSTANCE, headers=headers, json=body).status_code
            == status.HTTP_403_FORBIDDEN
        )
