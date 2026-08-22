"""Functional tests for the watch-source email routes (``watch_sources.py``).

Three routes ``scripts/audit-route-coverage.py`` listed as referenced by no test:

* ``POST /api/watch-sources/email-configs/{config_uuid}/test`` — super_admin;
* ``POST /api/watch-sources/{source_uuid}/emails`` — link (upsert), source owner;
* ``DELETE /api/watch-sources/{source_uuid}/emails/{config_uuid}`` — unlink, idempotent.

The privilege **asymmetry** between them is the point of the first two tests and is
easy to lose in a refactor: creating and testing an email config is super_admin work
because it holds mailbox credentials, but *any source owner* may subscribe their own
source to a config that already exists, and there is no separate gate on which one
they pick.

**No mail is sent and no network is touched.** ``watch_email_service.test_connection``
is replaced at the service boundary with a recorder — every test that does so says so
in its own name — because the real function opens an SMTP/Graph session against
whatever host the config names. Credentials are never read, decrypted or asserted on:
the config rows below are created with no secret at all, and the only stored fields
these tests look at are the three test-outcome columns.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import patch

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from app.models.email_notification_config import EmailNotificationConfig
from app.models.email_notification_config import WatchSourceEmail
from app.models.watch_source import WatchSource

BASE = "/api/watch-sources"

#: Never inserted. A literal, not ``uuid4()`` — a parametrize argument is evaluated at
#: import time and becomes part of the test id, so a random one gives each xdist worker
#: a different id and the whole suite fails collection.
ABSENT_UUID = "00000000-0000-4000-8000-0000000000ff"


def _make_source(db_session, owner) -> WatchSource:
    source = WatchSource(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        created_by=owner.id,
        name=f"watch-{uuid_pkg.uuid4().hex[:8]}",
        source_type="local",
        local_path=f"pytest/{uuid_pkg.uuid4().hex[:8]}",
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    return source


def _make_email_config(db_session, *, provider: str = "smtp") -> EmailNotificationConfig:
    """A credential-free mailer row — no password is set, so none can leak."""
    config = EmailNotificationConfig(
        uuid=uuid_pkg.uuid4(),
        name=f"mailer-{uuid_pkg.uuid4().hex[:8]}",
        provider=provider,
        smtp_host="smtp.invalid.example.com",
        smtp_port=587,
        from_address="noreply@example.com",
    )
    db_session.add(config)
    db_session.commit()
    db_session.refresh(config)
    return config


def _links(db_session: Session, source: WatchSource) -> list[WatchSourceEmail]:
    db_session.expire_all()
    return (
        db_session.query(WatchSourceEmail)
        .filter(WatchSourceEmail.watch_source_id == source.id)
        .all()
    )


# ---------------------------------------------------------------------------
# POST /{source_uuid}/emails — link / upsert
# ---------------------------------------------------------------------------
def test_the_source_owner_may_link_an_existing_config(
    client, db_session, user_token_headers, normal_user
):
    """The asymmetry: an ordinary user links their own source to a config.

    Catches the route being tightened to super_admin along with its config-editing
    siblings, which would make the notification panel unusable for every non-admin
    source owner.
    """
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session)

    response = client.post(
        f"{BASE}/{source.uuid}/emails",
        headers=user_token_headers,
        json={
            "email_config_uuid": str(config.uuid),
            "additional_recipients": "ops@example.com",
            "notify_on_success": False,
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"success": True}
    (link,) = _links(db_session, source)
    assert link.email_config_id == config.id
    assert link.additional_recipients == "ops@example.com"
    assert link.notify_on_success is False
    assert link.notify_on_error is True  # schema default, not supplied above


def test_relinking_the_same_config_updates_instead_of_duplicating(
    client, db_session, user_token_headers, normal_user
):
    """Upsert, not insert: re-posting must not 409 and must not fork a second row.

    A unique constraint covers ``(watch_source_id, email_config_id)``, so a handler
    that inserted blindly would 500 on the retry that this contract makes safe.
    """
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session)
    body = {"email_config_uuid": str(config.uuid), "additional_recipients": "first@example.com"}

    first = client.post(f"{BASE}/{source.uuid}/emails", headers=user_token_headers, json=body)
    second = client.post(
        f"{BASE}/{source.uuid}/emails",
        headers=user_token_headers,
        json={**body, "additional_recipients": "second@example.com", "notify_on_error": False},
    )

    assert first.status_code == status.HTTP_200_OK
    assert second.status_code == status.HTTP_200_OK
    (link,) = _links(db_session, source)
    assert link.additional_recipients == "second@example.com"
    assert link.notify_on_error is False


def test_linking_an_unknown_config_is_404(client, db_session, user_token_headers, normal_user):
    source = _make_source(db_session, normal_user)

    response = client.post(
        f"{BASE}/{source.uuid}/emails",
        headers=user_token_headers,
        json={"email_config_uuid": ABSENT_UUID},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert _links(db_session, source) == []


def test_linking_someone_elses_source_is_403(
    client, db_session, other_user_auth_headers, normal_user
):
    """``_get_source_or_404`` answers 403 once the row is found but not owned."""
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session)

    response = client.post(
        f"{BASE}/{source.uuid}/emails",
        headers=other_user_auth_headers,
        json={"email_config_uuid": str(config.uuid)},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert _links(db_session, source) == []


def test_linking_to_an_unknown_source_is_404(client, db_session, user_token_headers):
    config = _make_email_config(db_session)

    response = client.post(
        f"{BASE}/{ABSENT_UUID}/emails",
        headers=user_token_headers,
        json={"email_config_uuid": str(config.uuid)},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_linking_requires_authentication(client):
    response = client.post(f"{BASE}/{ABSENT_UUID}/emails", json={"email_config_uuid": ABSENT_UUID})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# DELETE /{source_uuid}/emails/{config_uuid} — unlink
# ---------------------------------------------------------------------------
def test_unlinking_removes_the_link_and_keeps_the_config(
    client, db_session, user_token_headers, normal_user
):
    """Only the junction row goes; the mailer stays available to other sources.

    Cascading into ``email_notification_config`` here would delete a shared,
    super_admin-managed credential row on an ordinary user's request.
    """
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session)
    client.post(
        f"{BASE}/{source.uuid}/emails",
        headers=user_token_headers,
        json={"email_config_uuid": str(config.uuid)},
    )

    response = client.delete(
        f"{BASE}/{source.uuid}/emails/{config.uuid}", headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"success": True}
    assert _links(db_session, source) == []
    survivor = (
        db_session.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.id == config.id)
        .first()
    )
    assert survivor is not None, "unlinking destroyed the email config itself"


def test_unlinking_an_unlinked_config_still_succeeds(
    client, db_session, user_token_headers, normal_user
):
    """Idempotent by design, so a retry after a dropped response does not 404."""
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session)

    response = client.delete(
        f"{BASE}/{source.uuid}/emails/{config.uuid}", headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"success": True}


def test_unlinking_an_unknown_config_is_404(client, db_session, user_token_headers, normal_user):
    """An unknown *config* is a caller mistake, not an already-applied delete."""
    source = _make_source(db_session, normal_user)

    response = client.delete(
        f"{BASE}/{source.uuid}/emails/{ABSENT_UUID}", headers=user_token_headers
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_unlinking_on_someone_elses_source_is_403(
    client, db_session, other_user_auth_headers, user_token_headers, normal_user
):
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session)
    client.post(
        f"{BASE}/{source.uuid}/emails",
        headers=user_token_headers,
        json={"email_config_uuid": str(config.uuid)},
    )

    response = client.delete(
        f"{BASE}/{source.uuid}/emails/{config.uuid}", headers=other_user_auth_headers
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert len(_links(db_session, source)) == 1


def test_unlinking_requires_authentication(client):
    response = client.delete(f"{BASE}/{ABSENT_UUID}/emails/{ABSENT_UUID}")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# POST /email-configs/{config_uuid}/test — super_admin, no mail leaves the box
# ---------------------------------------------------------------------------
def test_a_successful_test_is_persisted_onto_the_config_with_a_standin_mailer(
    client, db_session, super_admin_token_headers
):
    """A passing test records ``success`` so the panel can show it without re-testing.

    The mailer is a stand-in (see the module docstring) — the real call opens a live
    SMTP/Graph session. What is under test is the persistence of the outcome, which
    the UI reads instead of re-probing on every render.
    """
    config = _make_email_config(db_session)

    with patch(
        "app.services.watch_email_service.test_connection",
        return_value=(True, "Connected to smtp.invalid.example.com"),
    ):
        response = client.post(
            f"{BASE}/email-configs/{config.uuid}/test", headers=super_admin_token_headers
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "success": True,
        "message": "Connected to smtp.invalid.example.com",
    }
    db_session.expire_all()
    db_session.refresh(config)
    assert config.test_status == "success"
    assert config.test_message == "Connected to smtp.invalid.example.com"
    assert config.last_tested_at is not None


def test_a_failed_test_is_a_200_body_not_an_error_status_with_a_standin_mailer(
    client, db_session, super_admin_token_headers
):
    """A refused connection is a *successful test* that reports failure.

    Catches the handler being "improved" to raise 502/500: the panel distinguishes
    "we tried and it failed" (show the provider's message) from "the request broke",
    and a non-2xx would surface as a generic error with the diagnostic thrown away.
    """
    config = _make_email_config(db_session)

    with patch(
        "app.services.watch_email_service.test_connection",
        return_value=(False, "Authentication failed"),
    ):
        response = client.post(
            f"{BASE}/email-configs/{config.uuid}/test", headers=super_admin_token_headers
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"success": False, "message": "Authentication failed"}
    db_session.expire_all()
    db_session.refresh(config)
    assert config.test_status == "failed"


def test_testing_an_unknown_config_is_404(client, super_admin_token_headers):
    response = client.post(
        f"{BASE}/email-configs/{ABSENT_UUID}/test", headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.parametrize("tier", ["user", "admin"])
def test_testing_a_config_is_refused_below_super_admin(
    client, db_session, user_token_headers, admin_token_headers, tier
):
    """A plain **admin** is not enough: this reaches out with stored credentials.

    Two tiers in one test on purpose — the interesting boundary is admin, and pinning
    only the plain user would let a relaxation to ``get_current_admin_user`` pass.
    No stand-in is installed, because a refused request must not reach the mailer at
    all; if the gate regressed, the assertion below fails rather than a mail being
    attempted.
    """
    config = _make_email_config(db_session)
    headers = user_token_headers if tier == "user" else admin_token_headers

    response = client.post(f"{BASE}/email-configs/{config.uuid}/test", headers=headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    db_session.expire_all()
    db_session.refresh(config)
    assert config.test_status is None


def test_testing_a_config_requires_authentication(client):
    response = client.post(f"{BASE}/email-configs/{ABSENT_UUID}/test")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /{source_uuid}/emails — read the links back
# ---------------------------------------------------------------------------
def test_listing_links_returns_each_links_own_options(
    client, db_session, user_token_headers, normal_user
):
    """The per-link options are the whole point of per-source linkage.

    ``EmailLinkResponse`` existed as a schema with no endpoint returning it, so there
    was no way to read back what a link was configured to do — which made the
    notification panel unbuildable, not merely unbuilt.
    """
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session)
    client.post(
        f"{BASE}/{source.uuid}/emails",
        json={
            "email_config_uuid": str(config.uuid),
            "additional_recipients": "oncall@example.com",
            "notify_on_success": False,
            "notify_on_error": True,
        },
        headers=user_token_headers,
    )

    response = client.get(f"{BASE}/{source.uuid}/emails", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert len(body) == 1
    assert body[0]["email_config_uuid"] == str(config.uuid)
    assert body[0]["email_config_name"] == config.name
    assert body[0]["additional_recipients"] == "oncall@example.com"
    assert body[0]["notify_on_success"] is False
    assert body[0]["notify_on_error"] is True


def test_listing_links_on_an_unlinked_source_is_an_empty_list(
    client, db_session, user_token_headers, normal_user
):
    """Empty is a valid answer, not a 404 — the source exists and has no links."""
    source = _make_source(db_session, normal_user)

    response = client.get(f"{BASE}/{source.uuid}/emails", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []


def test_listing_links_on_someone_elses_source_is_403(
    client, db_session, other_user_auth_headers, normal_user
):
    source = _make_source(db_session, normal_user)

    response = client.get(f"{BASE}/{source.uuid}/emails", headers=other_user_auth_headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_listing_links_requires_authentication(client, db_session, normal_user):
    source = _make_source(db_session, normal_user)

    response = client.get(f"{BASE}/{source.uuid}/emails")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /{source_uuid}/emails/available — the owner-readable picker
# ---------------------------------------------------------------------------
def test_available_configs_are_readable_by_an_ordinary_source_owner(
    client, db_session, user_token_headers, normal_user
):
    """The reason this route exists at all.

    Linking is owner-level but ``GET /email-configs`` is super_admin, so an ordinary
    owner had the right to subscribe their source and no way to discover what to
    subscribe it to. A 403 here means that asymmetry is back.
    """
    source = _make_source(db_session, normal_user)
    config = _make_email_config(db_session)

    response = client.get(f"{BASE}/{source.uuid}/emails/available", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert str(config.uuid) in [c["uuid"] for c in response.json()]


def test_available_configs_exclude_what_is_already_linked(
    client, db_session, user_token_headers, normal_user
):
    """Server-side, so the client does not subtract two lists to render one picker."""
    source = _make_source(db_session, normal_user)
    linked = _make_email_config(db_session)
    unlinked = _make_email_config(db_session)
    client.post(
        f"{BASE}/{source.uuid}/emails",
        json={"email_config_uuid": str(linked.uuid)},
        headers=user_token_headers,
    )

    response = client.get(f"{BASE}/{source.uuid}/emails/available", headers=user_token_headers)

    uuids = [c["uuid"] for c in response.json()]
    assert str(unlinked.uuid) in uuids
    assert str(linked.uuid) not in uuids


def test_available_configs_expose_exactly_the_minimal_projection(
    client, db_session, user_token_headers, normal_user
):
    """The security assertion for this route, and the reason it is exact.

    Every authenticated user can read this. ``EmailConfigResponse`` — the shape the
    super_admin list returns — carries ``from_address``, ``smtp_host`` and
    ``smtp_username``; if this route is ever "simplified" to reuse it, or a field is
    added to the option schema without thinking, the deployment's mail configuration
    starts leaking to everyone. An equality check on the key set is what makes that
    impossible to do by accident; a subset check would not.
    """
    source = _make_source(db_session, normal_user)
    _make_email_config(db_session)

    response = client.get(f"{BASE}/{source.uuid}/emails/available", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert set(response.json()[0]) == {
        "uuid",
        "name",
        "provider",
        "is_enabled",
        "has_default_recipients",
    }


def test_available_configs_report_whether_recipients_exist_without_naming_them(
    client, db_session, user_token_headers, normal_user
):
    """A link with no recipients anywhere is silently skipped at send time.

    The boolean is what lets the UI warn about that; the addresses themselves are not
    the owner's business, which is why it is a flag and not the value.
    """
    source = _make_source(db_session, normal_user)
    _make_email_config(db_session)

    response = client.get(f"{BASE}/{source.uuid}/emails/available", headers=user_token_headers)

    option = response.json()[0]
    assert option["has_default_recipients"] is False
    assert "default_recipients" not in option


def test_available_configs_on_someone_elses_source_is_403(
    client, db_session, other_user_auth_headers, normal_user
):
    source = _make_source(db_session, normal_user)

    response = client.get(f"{BASE}/{source.uuid}/emails/available", headers=other_user_auth_headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_available_configs_requires_authentication(client, db_session, normal_user):
    source = _make_source(db_session, normal_user)

    response = client.get(f"{BASE}/{source.uuid}/emails/available")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
