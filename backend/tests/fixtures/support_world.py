"""A multi-tenant world for the support-access suites (issue #1122).

``support_world`` builds what every grant test needs and nothing else: two organizations,
an org admin for the first, the owner of a file inside it, a platform admin and a
super_admin, with real bearer tokens and the real dependency chain (only the cloud IdP
step, ``resolve_org_context``, is replaced, exactly as the ``org_context`` fixture does).

The grant lifecycle is driven through the HTTP API so a test exercises the routes it is
named for; ``SupportWorld.set_times`` moves a grant through time by writing its timestamps,
because status is computed from them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import FileStatus
from app.core.security import get_password_hash
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.support_access import SupportAccessGrant
from app.models.user import User
from app.services.platform_access import reset_tenancy_mode_cache

GRANT_HEADER = "X-Support-Access-Grant"
#: Random per process: these accounts exist only inside a rolled-back transaction, and a
#: literal credential in the tree is something secret scanners rightly flag.
_PASSWORD = uuid.uuid4().hex


def make_user(db, *, role: str, label: str) -> User:
    """A committed active user with a known password."""
    user = User(
        email=f"sa_{label}_{uuid.uuid4().hex[:8]}@example.com",
        full_name=f"SA {label}",
        hashed_password=get_password_hash(_PASSWORD),
        is_active=True,
        is_superuser=role == "super_admin",
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def login_headers(client, user: User) -> dict[str, str]:
    response = client.post(
        "/api/auth/token",
        data={"username": user.email, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_file(db, owner: User, organization_id: int | None) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"sa_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=FileStatus.COMPLETED,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


@dataclass
class SupportWorld:
    """Handles to everything a support-access test touches."""

    client: TestClient
    db: Session
    org: Organization
    other_org: Organization
    owner: User
    org_admin: User
    admin: User
    super_admin: User
    owner_headers: dict[str, str]
    org_admin_headers: dict[str, str]
    admin_headers: dict[str, str]
    super_headers: dict[str, str]
    file: MediaFile
    other_org_file: MediaFile
    personal_file: MediaFile
    roles: dict[int, tuple[int | None, str | None]] = field(default_factory=dict)
    sends: MagicMock = field(default_factory=MagicMock)

    # --- the request / decide / use cycle, through the real routes ---------------------

    def request(
        self, *, headers=None, org=None, level="read", minutes=60, **target: str
    ) -> dict[str, Any]:
        """POST /support-access/grants as the platform admin; returns the grant body."""
        body: dict[str, Any] = {
            "access_level": level,
            "reason": "Investigating a customer-reported transcript problem",
            "duration_minutes": minutes,
        }
        if target:
            body.update(target)
        else:
            body["organization_uuid"] = str((org or self.org).uuid)
        response = self.client.post(
            "/api/support-access/grants", headers=headers or self.admin_headers, json=body
        )
        assert response.status_code == 201, response.text
        created: dict[str, Any] = response.json()
        return created

    def approve(self, grant_uuid: str, *, minutes: int | None = None) -> dict[str, Any]:
        """The org admin of ``self.org`` approves."""
        response = self.client.post(
            f"/api/org-admin/support-access/{grant_uuid}/approve",
            headers=self.org_admin_headers,
            json={"duration_minutes": minutes} if minutes else {},
        )
        assert response.status_code == 200, response.text
        approved: dict[str, Any] = response.json()
        return approved

    def active_grant(self, *, level="read", org=None, headers=None, minutes=60) -> str:
        """A request approved by the tenant; returns the grant uuid."""
        grant = self.request(headers=headers, org=org, level=level, minutes=minutes)
        self.approve(grant["uuid"])
        return str(grant["uuid"])

    def with_grant(self, grant_uuid: str, headers=None) -> dict[str, str]:
        return {**(headers or self.admin_headers), GRANT_HEADER: grant_uuid}

    def row(self, grant_uuid: str) -> SupportAccessGrant:
        self.db.expire_all()
        found = (
            self.db.query(SupportAccessGrant)
            .filter(SupportAccessGrant.uuid == uuid.UUID(grant_uuid))
            .one()
        )
        return found

    def set_times(self, grant_uuid: str, **delta_hours: float) -> None:
        """Rewrite timestamps as offsets from now, in hours (``starts_at=-2, expires_at=-1``)."""
        row = self.row(grant_uuid)
        now = datetime.now(UTC)
        for name, hours in delta_hours.items():
            setattr(row, name, now + timedelta(hours=hours))
        self.db.commit()


@pytest.fixture
def support_world(
    db_session,
    client,
    monkeypatch,
    organizations_capability_on,
    normal_user,
    other_user,
    admin_user,
    super_admin_user,
    user_token_headers,
    other_user_auth_headers,
    admin_token_headers,
    super_admin_token_headers,
):
    """Multi-tenant deployment: ``org`` (with an org admin and a file), ``other_org``."""
    monkeypatch.setattr(settings, "TENANCY_MODE", "auto")
    monkeypatch.setattr(settings, "DEPLOYMENT_EDITION", "community")
    reset_tenancy_mode_cache()

    org = Organization(name=f"sa-org-{uuid.uuid4().hex[:8]}")
    other_org = Organization(name=f"sa-other-{uuid.uuid4().hex[:8]}")
    db_session.add_all([org, other_org])
    db_session.commit()
    db_session.add(
        OrganizationMembership(organization_id=org.id, user_id=other_user.id, role="org:admin")
    )
    db_session.commit()

    world = SupportWorld(
        client=client,
        db=db_session,
        org=org,
        other_org=other_org,
        owner=normal_user,
        org_admin=other_user,
        admin=admin_user,
        super_admin=super_admin_user,
        owner_headers={"Authorization": user_token_headers["Authorization"]},
        org_admin_headers={"Authorization": other_user_auth_headers["Authorization"]},
        admin_headers={"Authorization": admin_token_headers["Authorization"]},
        super_headers={"Authorization": super_admin_token_headers["Authorization"]},
        file=make_file(db_session, normal_user, org.id),
        other_org_file=make_file(db_session, normal_user, other_org.id),
        personal_file=make_file(db_session, normal_user, None),
    )
    world.roles[other_user.id] = (org.id, "org:admin")

    def _resolve(_request, _db, user):
        return world.roles.get(int(user.id), (None, None))

    monkeypatch.setattr("app.api.deps_context.resolve_org_context", _resolve)
    monkeypatch.setattr("app.services.support_access_lifecycle.send_ws_event", world.sends)

    # The use log is written in its own session in production; under the savepoint harness
    # that session cannot see the test's uncommitted rows, so it reuses the test session.
    import contextlib

    @contextlib.contextmanager
    def _same_session():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.db.session_utils.session_scope", _same_session)
    yield world
    reset_tenancy_mode_cache()
