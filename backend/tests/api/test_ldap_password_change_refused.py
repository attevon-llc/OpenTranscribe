"""An LDAP account cannot be given a local password, through either password-writing route.

``local_password_allowed`` and ``assert_password_auth_possible`` are unit-tested; this pins the
HTTP surface the LDAP doc describes: ``PUT /api/users/me`` and the admin's
``PUT /api/users/{uuid}`` both answer 400 with the ``Cannot set a password for this account:``
prefix for an ``auth_type='ldap'`` account, and the stored credential marker is unchanged.
A local account through the same admin route is the control: the route itself works.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.direct_auth import create_access_token
from app.models.user import User

PREFIX = "Cannot set a password for this account:"
NEW_PASSWORD = "Fresh-Directory-Passphrase-71!"


def _ldap_user(db_session) -> User:
    uid = f"p3-ldap-{uuid_pkg.uuid4().hex[:10]}"
    user = User(
        email=f"{uid}@example.com",
        full_name="Directory Person",
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type="ldap",
        ldap_uid=uid,
        role="user",
        is_active=True,
        is_superuser=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _headers(user: User) -> dict[str, str]:
    token = create_access_token({"sub": str(user.uuid), "role": str(user.role)})
    return {"Authorization": f"Bearer {token}"}


def test_self_service_password_change_is_refused(client, db_session):
    user = _ldap_user(db_session)

    response = client.put(
        "/api/users/me",
        json={"password": NEW_PASSWORD, "current_password": "anything"},
        headers=_headers(user),
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith(PREFIX)
    db_session.refresh(user)
    assert user.hashed_password == EXTERNAL_AUTH_NO_PASSWORD


def test_an_admin_password_set_is_refused(client, db_session, super_admin_token_headers):
    user = _ldap_user(db_session)

    response = client.put(
        f"/api/users/{user.uuid}",
        json={"password": NEW_PASSWORD},
        headers=super_admin_token_headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith(PREFIX)
    db_session.refresh(user)
    assert user.hashed_password == EXTERNAL_AUTH_NO_PASSWORD


def test_the_admin_route_does_set_a_local_accounts_password(
    client, db_session, normal_user, super_admin_token_headers
):
    before = normal_user.hashed_password

    response = client.put(
        f"/api/users/{normal_user.uuid}",
        json={"password": NEW_PASSWORD},
        headers=super_admin_token_headers,
    )

    assert response.status_code == 200, response.text
    db_session.refresh(normal_user)
    assert normal_user.hashed_password != before
