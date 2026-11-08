"""``POST /api/auth/demo/session`` against a real database (issue #628, D5).

Nothing is stubbed between the route and Postgres: the shared login mint runs, so the refresh
token row, the auth cookies and the audit event are the ones every other sign-in produces.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.auth.roles import role_implies_superuser
from app.core.config import settings
from app.models.refresh_token import RefreshToken
from app.models.user import User

URL = "/api/auth/demo/session"


@pytest.fixture
def demo(monkeypatch, normal_user):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    monkeypatch.setattr(settings, "DEMO_USER_EMAIL", normal_user.email)
    return normal_user


def _audit_details(caplog) -> list[dict]:
    events = []
    for record in caplog.records:
        if record.name != "audit":
            continue
        try:
            events.append(json.loads(record.getMessage()))
        except ValueError:
            continue
    return events


def test_a_visitor_gets_a_real_session_with_no_password(client, db_session, demo, caplog):
    caplog.set_level(logging.INFO, logger="audit")

    response = client.post(URL)

    assert response.status_code == 200, response.text
    assert response.json()["access_token"]
    assert response.cookies.get("access_token")
    assert response.cookies.get("refresh_token")
    rows = db_session.query(RefreshToken).filter(RefreshToken.user_id == demo.id).all()
    assert len(rows) == 1
    assert rows[0].revoked_at is None
    logins = [e for e in _audit_details(caplog) if e.get("event_type") == "auth.login.success"]
    assert [e["details"]["auth_method"] for e in logins] == ["demo"]
    assert logins[0]["username"] == demo.email


def test_the_session_it_mints_is_accepted_by_the_api(client, demo):
    token = client.post(URL).json()["access_token"]

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 200
    assert me.json()["email"] == demo.email


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_an_elevated_demo_user_is_refused_and_gets_no_session(client, db_session, demo, role):
    db_session.query(User).filter(User.id == demo.id).update(
        {"role": role, "is_superuser": role_implies_superuser(role)}
    )
    db_session.commit()

    response = client.post(URL)

    assert response.status_code == 503
    assert not response.cookies.get("access_token")
    assert db_session.query(RefreshToken).filter(RefreshToken.user_id == demo.id).count() == 0


def test_outside_demo_mode_there_is_no_endpoint(client, db_session, monkeypatch, normal_user):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "DEMO_USER_EMAIL", normal_user.email)

    response = client.post(URL)

    assert response.status_code == 404
    assert (
        db_session.query(RefreshToken).filter(RefreshToken.user_id == normal_user.id).count() == 0
    )
