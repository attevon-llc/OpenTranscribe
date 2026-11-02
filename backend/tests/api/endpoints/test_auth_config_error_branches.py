"""Pin the exception branches of the auth-config migrate handler.

``POST /migrate`` has a broad ``except Exception`` that turns any failure into an
opaque 500, preceded by ``except HTTPException: raise`` so a deliberate 403/404 raised
below is not reported as an internal error (issue #431). Dropping the re-raise,
leaking the exception text, or losing the failure audit record would have passed
the suite.

Run: ``../scripts/run-backend-tests.sh --throwaway-db tests/api/endpoints/test_auth_config_error_branches.py``
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.services.auth_config_service import AuthConfigService

BASE = "/api/admin/auth-config"
INTERNAL_DETAIL = "An internal error occurred. Please try again."
LEAKY_TEXT = "connection to db-host-7 refused: password=hunter2"


def _boom(*_args, **_kwargs):
    raise RuntimeError(LEAKY_TEXT)


def _collect_audit(monkeypatch) -> list[dict]:
    from app.api.endpoints import auth_config as auth_config_module

    events: list[dict] = []
    monkeypatch.setattr(auth_config_module.audit_logger, "log", lambda **kw: events.append(kw))
    return events


def _settings_changes(events: list[dict], action: str) -> list[dict]:
    return [
        e
        for e in events
        if e["event_type"] is AuditEventType.ADMIN_SETTINGS_CHANGE
        and e["details"].get("action") == action
    ]


class TestMigrate:
    def test_success_is_audited_with_actor_and_count(
        self, client, super_admin_token_headers, super_admin_user, monkeypatch
    ):
        monkeypatch.setattr(AuthConfigService, "migrate_from_env", lambda db, user_id: 7)
        events = _collect_audit(monkeypatch)
        actor_id, actor_email = super_admin_user.id, str(super_admin_user.email)

        response = client.post(f"{BASE}/migrate", headers=super_admin_token_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["migrated_count"] == 7
        records = _settings_changes(events, "auth_config_migrate_from_env")
        assert len(records) == 1, events
        assert records[0]["outcome"] is AuditOutcome.SUCCESS
        assert records[0]["user_id"] == actor_id
        assert records[0]["username"] == actor_email
        assert records[0]["details"]["migrated_count"] == 7

    def test_unexpected_failure_is_an_opaque_500(
        self, client, super_admin_token_headers, monkeypatch
    ):
        monkeypatch.setattr(AuthConfigService, "migrate_from_env", _boom)

        response = client.post(f"{BASE}/migrate", headers=super_admin_token_headers)

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.json()["detail"] == INTERNAL_DETAIL
        assert "hunter2" not in response.text

    def test_unexpected_failure_is_audited_as_a_failure(
        self, client, super_admin_token_headers, super_admin_user, monkeypatch
    ):
        monkeypatch.setattr(AuthConfigService, "migrate_from_env", _boom)
        events = _collect_audit(monkeypatch)
        actor_id, actor_email = super_admin_user.id, str(super_admin_user.email)

        response = client.post(f"{BASE}/migrate", headers=super_admin_token_headers)

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        records = _settings_changes(events, "auth_config_migrate_from_env")
        assert len(records) == 1, events
        record = records[0]
        assert record["outcome"] is AuditOutcome.FAILURE
        assert record["error_code"] == "AUTH_CONFIG_MIGRATE_FAILED"
        assert record["user_id"] == actor_id
        assert record["username"] == actor_email
        assert "hunter2" not in repr(record)

    @pytest.mark.parametrize(
        ("raised", "expected_status"),
        [
            (HTTPException(status_code=409, detail="deliberate"), 409),
        ],
        ids=["http_exception"],
    )
    def test_a_deliberate_refusal_is_not_reported_as_a_500(
        self, client, super_admin_token_headers, monkeypatch, raised, expected_status
    ):
        def refuse(*_a, **_k):
            raise raised

        monkeypatch.setattr(AuthConfigService, "migrate_from_env", refuse)
        events = _collect_audit(monkeypatch)

        response = client.post(f"{BASE}/migrate", headers=super_admin_token_headers)

        assert response.status_code == expected_status
        assert response.json()["detail"] == "deliberate"
        assert _settings_changes(events, "auth_config_migrate_from_env") == []
