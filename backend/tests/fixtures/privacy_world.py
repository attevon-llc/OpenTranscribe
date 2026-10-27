"""State and seams for the privacy-policy enforcement suites (issue #1280).

The policy reads ``system_settings``, ``organization_policy`` and ``user_setting`` rows. The
code that enforces it opens its OWN short session (``session_scope``), which under the
savepoint harness cannot see the test's uncommitted rows. ``privacy_world`` points every one
of those factories at the test's ``db_session`` so the real policy queries run against the
rows a test wrote, and resets the process-wide state (consent hooks, platform resolver, the
write-backstop cache) around each test.

``privacy_allows_all`` is the opposite tool, for suites that test something else and pass fake
ids: it answers "allowed" at the policy's file gates so an unrelated stage test is not refused
by a file row it never created. Opt-in; nothing autouse.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

FEATURES = ("voiceprints", "gender")


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def privacy_world(db_session, monkeypatch):
    """The real policy against the test's rows, with clean process-wide state."""
    from app.services.privacy import policy
    from app.services.privacy.consent import clear_voiceprint_consent_hooks

    clear_voiceprint_consent_hooks()
    policy.reset_privacy_ceiling_resolver()
    policy.clear_write_backstop_cache()

    def scope():
        return _yield_session(db_session)

    monkeypatch.setattr("app.services.privacy.gates.session_scope", scope)
    monkeypatch.setattr("app.db.session_utils.session_scope", scope)

    yield SimpleNamespace(
        db=db_session,
        set_user=lambda user, feature, value: set_user_switch(db_session, user, feature, value),
        set_instance=lambda feature, value: set_instance_switch(db_session, feature, value),
        set_org=lambda org_id, **flags: set_org_switches(db_session, org_id, **flags),
        new_org=lambda: new_org(db_session),
        new_file=lambda user, org_id=None: new_file(db_session, user, org_id),
        reset=policy.clear_write_backstop_cache,
    )

    clear_voiceprint_consent_hooks()
    policy.reset_privacy_ceiling_resolver()
    policy.clear_write_backstop_cache()


@pytest.fixture
def privacy_allows_all(monkeypatch):
    """Answer "allowed" at the policy's file gates, for suites that pass fake ids."""
    monkeypatch.setattr(
        "app.services.privacy.policy.voiceprints_allowed_for_file", lambda db, file_id: True
    )
    monkeypatch.setattr(
        "app.services.privacy.policy.gender_allowed_for_file", lambda db, file_id: True
    )


def new_org(db):
    from app.models.organization import Organization

    org = Organization(name=f"org-{uuid_pkg.uuid4().hex[:8]}")
    db.add(org)
    db.flush()
    return org


def new_file(db, owner, org_id: int | None = None):
    from app.models.media import MediaFile

    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        filename="privacy_probe.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        user_id=owner.id,
        organization_id=org_id,
    )
    db.add(media_file)
    db.flush()
    return media_file


def set_instance_switch(db, feature: str, value: str) -> None:
    from app.core import constants
    from app.models.system_settings import SystemSettings

    key = {
        "voiceprints": constants.PRIVACY_VOICEPRINT_MATCHING_KEY,
        "gender": constants.PRIVACY_GENDER_INFERENCE_KEY,
    }[feature]
    row = db.query(SystemSettings).filter(SystemSettings.key == key).first()
    if row is None:
        db.add(SystemSettings(key=key, value=value))
    else:
        row.value = value
    db.flush()


def set_org_switches(db, org_id: int, **flags: bool) -> None:
    from app.models.organization_policy import OrganizationPolicy

    columns = {"voiceprints": "voiceprint_matching_enabled", "gender": "gender_inference_enabled"}
    row = db.get(OrganizationPolicy, org_id)
    if row is None:
        row = OrganizationPolicy(organization_id=org_id)
        db.add(row)
    for feature, value in flags.items():
        setattr(row, columns[feature], value)
    db.flush()


def set_user_switch(db, user, feature: str, value: str | None) -> None:
    from app.core import constants
    from app.models.prompt import UserSetting

    key = {
        "voiceprints": constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
        "gender": constants.PRIVACY_USER_GENDER_DETECTION_KEY,
    }[feature]
    row = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.setting_key == key)
        .first()
    )
    if row is None:
        db.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    else:
        row.setting_value = value
    db.flush()
