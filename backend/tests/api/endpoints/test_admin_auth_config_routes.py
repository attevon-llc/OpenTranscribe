"""Functional tests for the auth-mail designation and env-migration routes.

Two modules, both mounted under ``/api/admin/auth-config``:

* ``auth_email_delivery.py`` — ``GET``/``PUT /email/designation``. Had **no
  functional coverage**: ``unit/test_auth_mail_designation.py`` covers the
  service, nothing issued a request. The designated row carries password resets,
  invitations and verification links, so a designation that silently stops
  resolving surfaces only as undelivered credentials.
* ``auth_config.py`` — ``POST /migrate``. The existing
  ``api/test_auth_config_endpoints.py`` asserts only that a plain user gets 403;
  the handler body — the idempotence that makes a "one-time migration" safe to
  re-run — was never executed.

The invariants pinned here:

* both are **super_admin**, so a plain ``admin`` gets 403 (this is the tier that
  decides which SMTP server receives password-reset links);
* the read path distinguishes ``not_designated`` / ``active`` / ``disabled``,
  which is the UI's only signal that auth mail is broken;
* designating a missing, disabled or malformed config is a readable 400 **and is
  not persisted** — a bad designation must fail in the dialog, not at the next
  password reset;
* ``/migrate`` only writes keys absent from the DB, so a second call migrates 0.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.core.constants import AUTH_EMAIL_CONFIG_SETTING_KEY
from app.models.auth_config import AuthConfig
from app.models.email_notification_config import EmailNotificationConfig
from app.models.system_settings import SystemSettings
from app.services import auth_mail_config_service as designation

BASE = "/api/admin/auth-config"
DESIGNATION = f"{BASE}/email/designation"
MIGRATE = f"{BASE}/migrate"


@pytest.fixture
def no_designation(db_session):
    """Clear any ambient designation so ``not_designated`` is observable.

    Rolled back with the test's savepoint, so the deployment's real designation is
    restored at teardown.
    """
    db_session.query(SystemSettings).filter(
        SystemSettings.key == AUTH_EMAIL_CONFIG_SETTING_KEY
    ).delete(synchronize_session=False)
    db_session.commit()
    return db_session


def _make_email_config(db_session, *, enabled: bool = True) -> EmailNotificationConfig:
    """An SMTP email provider row unique to this run."""
    config = EmailNotificationConfig(
        name=f"smtp-{uuid_pkg.uuid4().hex[:8]}",
        provider="smtp",
        smtp_host="smtp.invalid",
        smtp_port=587,
        from_address="noreply@example.com",
        is_enabled=enabled,
    )
    db_session.add(config)
    db_session.commit()
    db_session.refresh(config)
    return config


# ---------------------------------------------------------------------------
# Privilege tier
# ---------------------------------------------------------------------------
_ROUTES = [
    ("GET", DESIGNATION, None),
    ("PUT", DESIGNATION, {"config_uuid": None}),
    ("POST", MIGRATE, None),
]


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_plain_admin_is_refused_on_every_route(client, admin_token_headers, method, path, body):
    """An ``admin`` must not choose which server receives password-reset mail.

    Catches these routes being re-gated to ``get_current_admin_user``: an admin
    could point auth mail at a provider they control and then trigger a reset for
    the break-glass account, capturing the link.
    """
    response = client.request(method, path, headers=admin_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_a_plain_user_is_refused_on_every_route(client, user_token_headers, method, path, body):
    response = client.request(method, path, headers=user_token_headers, json=body)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_every_route_requires_authentication(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /email/designation
# ---------------------------------------------------------------------------
def test_get_reports_not_designated_when_nothing_is_chosen(
    client, super_admin_token_headers, no_designation
):
    """No designation means the env SMTP transport, reported as such.

    Catches ``resolves`` defaulting to True with no row designated — the panel
    would show auth mail as healthy on a deployment where it is only working by
    virtue of an env fallback that may not be configured either.
    """
    response = client.get(DESIGNATION, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "not_designated"
    assert body["config_uuid"] is None
    assert body["config_name"] is None
    assert body["resolves"] is False


def test_put_designates_an_enabled_config_and_get_agrees(
    client, super_admin_token_headers, no_designation, db_session
):
    """The happy path: an enabled config becomes ``active`` and resolves.

    Catches a PUT that shapes a response without persisting — the panel would show
    the designation applied while password resets kept going out on the old
    transport until the next page load.
    """
    config = _make_email_config(db_session)

    response = client.put(
        DESIGNATION, headers=super_admin_token_headers, json={"config_uuid": str(config.uuid)}
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "active"
    assert body["config_uuid"] == str(config.uuid)
    assert body["config_name"] == config.name
    assert body["provider"] == "smtp"
    assert body["resolves"] is True

    reread = client.get(DESIGNATION, headers=super_admin_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["config_uuid"] == str(config.uuid)


def test_get_reports_disabled_for_a_designation_that_stopped_resolving(
    client, super_admin_token_headers, no_designation, db_session
):
    """A row disabled *after* being designated must be reported, not hidden.

    This is the failure mode the ``status`` field exists for: the read path falls
    back to env SMTP quietly, so without this signal a broken designation shows up
    only as password resets that never arrive. Catches ``resolves`` being computed
    from "a row exists" rather than "the row is enabled".
    """
    config = _make_email_config(db_session)
    assert (
        client.put(
            DESIGNATION, headers=super_admin_token_headers, json={"config_uuid": str(config.uuid)}
        ).status_code
        == status.HTTP_200_OK
    )

    config.is_enabled = False
    db_session.commit()

    response = client.get(DESIGNATION, headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "disabled"
    assert body["is_enabled"] is False
    assert body["resolves"] is False


def test_put_can_clear_the_designation(
    client, super_admin_token_headers, no_designation, db_session
):
    """Clearing is a legitimate choice meaning "use the env SMTP transport".

    Catches an empty body being treated as "no change" (the operator could never
    undo a designation) or as an error.
    """
    config = _make_email_config(db_session)
    client.put(
        DESIGNATION, headers=super_admin_token_headers, json={"config_uuid": str(config.uuid)}
    )

    response = client.put(DESIGNATION, headers=super_admin_token_headers, json={"config_uuid": ""})
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "not_designated"
    assert designation.designated_uuid(db_session) is None


def test_put_refuses_a_disabled_config(
    client, super_admin_token_headers, no_designation, db_session
):
    """Designating a disabled row is rejected at write time, not read time.

    Catches the enabled check being dropped: the designation would be accepted and
    then silently fail to deliver, which is the exact failure this service's
    docstring says it exists to prevent.
    """
    config = _make_email_config(db_session, enabled=False)

    response = client.put(
        DESIGNATION, headers=super_admin_token_headers, json={"config_uuid": str(config.uuid)}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "disabled" in response.json()["detail"]
    assert designation.designated_uuid(db_session) is None


def test_put_refuses_a_uuid_that_names_no_config(
    client, super_admin_token_headers, no_designation, db_session
):
    response = client.put(
        DESIGNATION, headers=super_admin_token_headers, json={"config_uuid": str(uuid_pkg.uuid4())}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert designation.designated_uuid(db_session) is None


def test_put_refuses_a_malformed_uuid_with_a_400_not_a_500(
    client, super_admin_token_headers, no_designation, db_session
):
    """An unparseable value is an operator typo, not a server fault.

    Catches the ``ValueError``→400 mapping being dropped: the value reaches a
    Postgres UUID comparison and the request dies as an opaque 500 with the real
    cause only in the log.
    """
    response = client.put(
        DESIGNATION, headers=super_admin_token_headers, json={"config_uuid": "not-a-uuid"}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert designation.designated_uuid(db_session) is None


# ---------------------------------------------------------------------------
# POST /migrate
# ---------------------------------------------------------------------------
def test_migrate_writes_the_missing_key_then_migrates_nothing_on_a_re_run(
    client, super_admin_token_headers, db_session
):
    """ "One-time migration" must actually migrate, and be safe to run twice.

    A deliberate gap is opened first (``mfa_enabled`` is deleted inside the
    savepoint) so the first call **has** to migrate at least that key — asserting
    only ``migrated_count >= 0`` would pass against a handler that migrated
    nothing at all, which is exactly the state of a deployment where every key is
    already present.

    The second call is the other half: ``migrate_from_env`` skips keys already in
    the DB, so a re-run migrates 0. Catches the ``existing`` check being dropped —
    a second click would overwrite every DB-configured auth value with whatever is
    still in ``.env``, silently reverting deliberate admin changes (including
    re-enabling a disabled auth method).
    """
    gap_key = "mfa_enabled"
    db_session.query(AuthConfig).filter(AuthConfig.config_key == gap_key).delete(
        synchronize_session=False
    )
    db_session.commit()
    assert db_session.query(AuthConfig).filter(AuthConfig.config_key == gap_key).count() == 0

    first = client.post(MIGRATE, headers=super_admin_token_headers)
    assert first.status_code == status.HTTP_200_OK
    first_body = first.json()
    assert first_body["success"] is True
    assert first_body["migrated_count"] >= 1
    restored = db_session.query(AuthConfig).filter(AuthConfig.config_key == gap_key).one()
    assert restored.category == "mfa"

    second = client.post(MIGRATE, headers=super_admin_token_headers)
    assert second.status_code == status.HTTP_200_OK
    assert second.json()["migrated_count"] == 0
