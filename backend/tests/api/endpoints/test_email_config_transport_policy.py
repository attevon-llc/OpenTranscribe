"""The email-config routes refuse an unencrypted SMTP setup with a 422 that says why."""

from __future__ import annotations

import uuid as uuid_pkg

from fastapi import status

from app.models.email_notification_config import EmailNotificationConfig

BASE = "/api/watch-sources/email-configs"


def _payload(**over):
    body = {
        "name": f"mailer-{uuid_pkg.uuid4().hex[:8]}",
        "provider": "smtp",
        "from_address": "noreply@example.org",
        "smtp_host": "mail.example.org",
        "smtp_port": 25,
        "smtp_use_tls": False,
        "smtp_username": "user",
        "smtp_password": "pw",
    }
    body.update(over)
    return body


def test_create_refuses_credentials_without_tls(client, super_admin_token_headers):
    response = client.post(BASE, headers=super_admin_token_headers, json=_payload())
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "credentials are only sent over" in response.text


def test_create_accepts_starttls(client, super_admin_token_headers, db_session):
    response = client.post(
        BASE, headers=super_admin_token_headers, json=_payload(smtp_port=587, smtp_use_tls=True)
    )
    assert response.status_code == status.HTTP_200_OK
    db_session.query(EmailNotificationConfig).filter(
        EmailNotificationConfig.uuid == response.json()["uuid"]
    ).delete()
    db_session.commit()


def test_update_refuses_turning_tls_off_while_credentials_are_stored(
    client, super_admin_token_headers, db_session
):
    created = client.post(
        BASE, headers=super_admin_token_headers, json=_payload(smtp_port=587, smtp_use_tls=True)
    )
    config_uuid = created.json()["uuid"]
    try:
        response = client.put(
            f"{BASE}/{config_uuid}",
            headers=super_admin_token_headers,
            json={"smtp_use_tls": False, "smtp_port": 25},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "credentials are only sent over" in response.text
    finally:
        db_session.query(EmailNotificationConfig).filter(
            EmailNotificationConfig.uuid == config_uuid
        ).delete()
        db_session.commit()
