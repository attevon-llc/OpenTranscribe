"""GDPR erasure deletes a speaker profile's avatar OBJECT, in every erasure scope.

The avatar is an uploaded photo of the person a profile names: personal data in object
storage that no row cascade reaches. ``_delete_profiles`` reads the key while the row exists
and ``_erase_avatar_objects`` deletes the object once the erasure's transaction has
committed. Until this file, the only test that would notice the object surviving was the
live-stack residue suite, so dropping the ``delete_file`` call failed nothing in the
throwaway-database run. These pin, per scope (account, one member of one organization, a
whole organization):

* the object is deleted, and only after the profile row is gone (the three-phase rule: no
  object-storage round trip inside the open transaction);
* a failed delete is recorded as a PARTIAL erasure that names the failure class only, never
  the storage key (#914), and the profile row is still deleted.

OpenSearch and the audit store are patched out: the voiceprint and profile-embedding planes
have their own tests in ``test_org_admin_gdpr.py``.
"""

import uuid as uuid_pkg
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.speaker import SpeakerProfile
from app.models.user import User


def _mk_user(db) -> User:
    from app.core.security import get_password_hash

    user = User(
        email=f"avatar_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="avatar subject",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_org(db, user: User) -> Organization:
    org = Organization(
        external_org_id=f"org_avatar_{uuid_pkg.uuid4().hex[:8]}", name="Avatar Org", is_active=True
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:member"))
    db.commit()
    return org


def _erase_account(db, user: User, org: Organization) -> dict[str, Any]:
    from app.services.gdpr_erasure_service import erase_user

    return erase_user(db, int(user.id))


def _erase_member(db, user: User, org: Organization) -> dict[str, Any]:
    from app.services.gdpr_erasure_service import erase_org_member_data

    return erase_org_member_data(db, int(user.id), int(org.id))


def _erase_org(db, user: User, org: Organization) -> dict[str, Any]:
    from app.services.gdpr_erasure_service import erase_organization

    return erase_organization(db, int(org.id))


_SCOPES: dict[str, Callable[[Any, User, Organization], dict[str, Any]]] = {
    "account": _erase_account,
    "org_member": _erase_member,
    "organization": _erase_org,
}


@pytest.fixture()
def subject(db_session):
    """A user in an organization, with an org-stamped speaker profile that has an avatar."""
    user = _mk_user(db_session)
    org = _mk_org(db_session, user)
    avatar = f"avatars/{uuid_pkg.uuid4().hex}.png"
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        organization_id=org.id,
        name="Pictured Person",
        avatar_path=avatar,
    )
    db_session.add(profile)
    db_session.commit()
    return user, org, int(profile.id), avatar


def _run(db, scope: str, user: User, org: Organization, delete_file) -> dict[str, Any]:
    with (
        patch("app.services.minio_service.delete_file", delete_file),
        patch("app.services.opensearch_service.remove_profile_embedding", return_value=None),
        patch("app.services.gdpr_erasure_service._erase_speaker_voiceprints", return_value=0),
        patch("app.services.gdpr_erasure_service.audit_logger", MagicMock()),
    ):
        return _SCOPES[scope](db, user, org)


@pytest.mark.parametrize("scope", sorted(_SCOPES))
def test_the_avatar_object_is_deleted_after_the_profile_row(db_session, subject, scope):
    user, org, profile_id, avatar = subject
    deleted: list[tuple[str, bool]] = []

    def _record(object_name: str) -> None:
        row = db_session.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
        deleted.append((object_name, row is None))

    summary = _run(db_session, scope, user, org, _record)

    assert deleted == [(avatar, True)]
    assert summary["speaker_profiles_deleted"] == 1
    assert summary["errors"] == []
    assert summary["complete"] is True


@pytest.mark.parametrize("scope", sorted(_SCOPES))
def test_a_failed_avatar_delete_is_a_partial_erasure_that_hides_the_key(db_session, subject, scope):
    user, org, profile_id, avatar = subject

    def _boom(object_name: str) -> None:
        raise RuntimeError(f"MinIO refused {object_name}")

    summary = _run(db_session, scope, user, org, _boom)

    assert summary["errors"] == [
        {"stage": "avatar", "error": "avatar delete failed (RuntimeError)"}
    ]
    assert avatar not in str(summary)
    assert summary["complete"] is False
    assert summary["speaker_profiles_deleted"] == 1
    assert db_session.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first() is None
