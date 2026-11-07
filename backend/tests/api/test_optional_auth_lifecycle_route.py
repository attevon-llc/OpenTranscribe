"""An optional-auth route serves a lifecycle-refused account exactly as an anonymous caller.

``GET /api/files/{uuid}/thumbnail`` resolves its caller through
``get_optional_current_user``, as do ``GET /system/capabilities`` and the capability
gate's platform-staff bypass. Every account state that ``get_current_active_user``
refuses must get the anonymous answer there: the 401 a caller with no token gets for a
non-public file. An account in good standing still gets the owner's answer.

``thumbnail_path=None`` is the discriminator, as in ``test_thumbnail_gate_branches.py``:
the "Thumbnail not available" 404 is reached only after every gate passed.
"""

# mypy: disable-error-code="arg-type"
# ORM rows are handed to helpers typed for the real models; declared once here.
from __future__ import annotations

import uuid as uuid_pkg
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.approval import APPROVAL_PENDING
from app.auth.approval import APPROVAL_REJECTED
from app.auth.direct_auth import create_access_token
from app.core.security import get_password_hash
from app.db.base import get_db
from app.main import app
from app.models.auth_config import AuthConfig
from app.models.media import MediaFile
from app.models.user import User
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("account_approval")

_AUTHORIZED = "Thumbnail not available for this file"
_ANONYMOUS = 401
PROXY_PEER = "10.0.0.7"
EMAIL_HEADER = "X-Forwarded-Email"


def _account(db_session, **fields: Any) -> User:
    values: dict[str, Any] = {
        "email": f"optional-route-{uuid_pkg.uuid4().hex[:8]}@example.com",
        "full_name": "Optional Route",
        "hashed_password": get_password_hash("irrelevant-Passphrase99!"),
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "is_superuser": False,
        # Acknowledged by default so the banner case below differs in one attribute.
        "banner_acknowledged_at": datetime.now(UTC),
    }
    values.update(fields)
    user = User(**values)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _private_file(db_session, owner: User) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=None,
        filename=f"optional_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        is_public=False,
        thumbnail_path=None,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token({"sub": str(user.uuid), "role": str(user.role)})
    return {"Authorization": f"Bearer {token}"}


def _thumbnail(client: TestClient, user: User, db_session, **headers: str):
    media_file = _private_file(db_session, user)
    return client.get(
        f"/api/files/{media_file.uuid}/thumbnail", headers={**_bearer(user), **headers}
    )


def _require_approval(db_session, actor: User) -> None:
    AuthConfigService.bulk_update_category(
        db=db_session,
        category="local",
        config_dict={"require_account_approval": True},
        user_id=actor.id,
    )


def _banner_enabled(db_session) -> AuthConfig:
    row = db_session.query(AuthConfig).filter(AuthConfig.config_key == "login_banner_enabled")
    enabled: AuthConfig | None = row.first()
    if enabled is None:
        enabled = AuthConfig(config_key="login_banner_enabled", category="local")
        db_session.add(enabled)
    enabled.config_value = "true"
    db_session.flush()
    return enabled


class TestEveryRefusedStateIsAnonymous:
    def test_no_token_is_the_anonymous_baseline(self, client, db_session):
        media_file = _private_file(db_session, _account(db_session))

        response = client.get(f"/api/files/{media_file.uuid}/thumbnail")

        assert response.status_code == _ANONYMOUS

    def test_an_account_in_good_standing_gets_the_owner_answer(self, client, db_session):
        """Control for every case below."""
        response = _thumbnail(client, _account(db_session), db_session)

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == _AUTHORIZED

    def test_a_deactivated_account(self, client, db_session):
        response = _thumbnail(client, _account(db_session, is_active=False), db_session)

        assert response.status_code == _ANONYMOUS, response.text

    def test_a_pending_account_while_approval_is_required(
        self, client, db_session, super_admin_user
    ):
        _require_approval(db_session, super_admin_user)

        pending = _account(db_session, approval_status=APPROVAL_PENDING)

        assert _thumbnail(client, pending, db_session).status_code == _ANONYMOUS

    def test_a_rejected_account(self, client, db_session):
        rejected = _account(db_session, approval_status=APPROVAL_REJECTED)

        assert _thumbnail(client, rejected, db_session).status_code == _ANONYMOUS

    def test_an_expired_account(self, client, db_session):
        expired = _account(db_session, account_expires_at=datetime.now(UTC) - timedelta(hours=1))

        assert _thumbnail(client, expired, db_session).status_code == _ANONYMOUS

    def test_an_account_owing_a_password_change(self, client, db_session):
        owing = _account(db_session, must_change_password=True)

        assert _thumbnail(client, owing, db_session).status_code == _ANONYMOUS

    def test_an_account_that_never_acknowledged_the_banner(self, client, db_session):
        _banner_enabled(db_session)
        owing = _account(db_session, banner_acknowledged_at=None)

        assert _thumbnail(client, owing, db_session).status_code == _ANONYMOUS

    def test_an_acknowledged_banner_still_gets_the_owner_answer(self, client, db_session):
        """Control: an enabled banner refuses only an account that owes it."""
        _banner_enabled(db_session)

        response = _thumbnail(client, _account(db_session), db_session)

        assert response.json()["detail"] == _AUTHORIZED

    def test_an_acknowledgment_older_than_the_banner_text(self, client, db_session):
        _banner_enabled(db_session)
        text = db_session.query(AuthConfig).filter(AuthConfig.config_key == "login_banner_text")
        row = text.first()
        if row is None:
            row = AuthConfig(config_key="login_banner_text", category="local")
            db_session.add(row)
        row.config_value = "Monitoring in effect."
        db_session.flush()
        row.updated_at = datetime.now(UTC)
        db_session.flush()
        stale = _account(db_session, banner_acknowledged_at=datetime.now(UTC) - timedelta(days=1))

        assert _thumbnail(client, stale, db_session).status_code == _ANONYMOUS


class TestAProxyIdentityMismatch:
    @pytest.fixture
    def proxy_enabled(self, db_session, super_admin_user) -> None:
        AuthConfigService.bulk_update_category(
            db_session,
            "proxy",
            {
                "proxy_enabled": True,
                "proxy_trusted_proxies": "10.0.0.0/8",
                "proxy_email_header": EMAIL_HEADER,
            },
            user_id=int(super_admin_user.id),
        )

    @pytest.fixture
    def proxy_client(self, db_session) -> Iterator[TestClient]:
        """A client whose peer is inside the trusted range, so the header is honoured."""

        def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        with TestClient(app, client=(PROXY_PEER, 40000)) as test_client:
            yield test_client
        app.dependency_overrides.pop(get_db, None)

    def test_a_different_asserted_identity_is_anonymous(
        self, proxy_client, db_session, proxy_enabled
    ):
        proxy_user = _account(db_session, auth_type="proxy")

        response = _thumbnail(
            proxy_client, proxy_user, db_session, **{EMAIL_HEADER: "someone.else@example.com"}
        )

        assert response.status_code == _ANONYMOUS, response.text

    def test_the_matching_asserted_identity_gets_the_owner_answer(
        self, proxy_client, db_session, proxy_enabled
    ):
        """Control: same client, same header, the account's own address."""
        proxy_user = _account(db_session, auth_type="proxy")

        response = _thumbnail(
            proxy_client, proxy_user, db_session, **{EMAIL_HEADER: str(proxy_user.email)}
        )

        assert response.json()["detail"] == _AUTHORIZED
