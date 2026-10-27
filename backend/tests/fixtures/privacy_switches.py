"""Row builders for the privacy-policy suites (#1280): switches, organizations, scoped files.

Imported directly by the suites that need them (not a ``pytest_plugins`` entry): they are
plain functions over the savepoint ``db_session``, so a test sets the exact layer it is
about and reads the result back through the real policy.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

from app.core import constants
from app.models.organization import Organization
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from app.models.system_settings import SystemSettings
from tests.fixtures.speaker_cleanup_seams import make_file

_INSTANCE_KEYS = {
    "voiceprints": constants.PRIVACY_VOICEPRINT_MATCHING_KEY,
    "gender": constants.PRIVACY_GENDER_INFERENCE_KEY,
}
_USER_KEYS = {
    "voiceprints": constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
    "gender": constants.PRIVACY_USER_GENDER_DETECTION_KEY,
}


def set_instance_switch(db, feature: str, value: bool) -> None:
    """Set the instance layer (``system_settings``) for ``voiceprints`` or ``gender``."""
    key = _INSTANCE_KEYS[feature]
    text = "true" if value else "false"
    row = db.query(SystemSettings).filter(SystemSettings.key == key).first()
    if row is None:
        db.add(SystemSettings(key=key, value=text))
    else:
        row.value = text
    db.commit()
    db.expire_all()


def set_user_switch(db, user, feature: str, value: bool) -> None:
    """Set the user layer for ``voiceprints`` or ``gender``."""
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key=_USER_KEYS[feature],
            setting_value="true" if value else "false",
        )
    )
    db.commit()
    db.expire_all()


def make_org(db, *, voiceprints: bool = True, gender: bool = True) -> Organization:
    """An organization with an ``organization_policy`` row carrying the given switches."""
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


def file_in(db, user, org: Organization | None) -> Any:
    """A media file of ``user`` stamped with ``org`` (None = personal)."""
    media_file = make_file(db, user)
    media_file.organization_id = org.id if org is not None else None
    db.flush()
    return media_file
