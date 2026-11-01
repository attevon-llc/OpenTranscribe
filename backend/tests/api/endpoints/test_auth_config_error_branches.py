"""Pin the exception branches of the auth-config write handlers.

``PUT /{category}`` and ``POST /migrate`` each have a broad ``except Exception``
that turns any failure into an opaque 500, preceded by a narrow
``except (HTTPException, DomainError): raise`` so a deliberate 403/404 raised below
is not reported as an internal error (issue #431). Neither branch had a test, so
dropping the re-raise, leaking the exception text, or losing the failure audit
record on the update path would have passed the suite.

Run: ``../scripts/run-backend-tests.sh --throwaway-db tests/api/endpoints/test_auth_config_error_branches.py``
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi import status

from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.core.exceptions import AccessDeniedError
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


class TestUpdateUnexpectedFailure:
    def test_is_an_opaque_500_that_does_not_leak_the_exception(
        self, client, super_admin_token_headers, monkeypatch
    ):
        monkeypatch.setattr(AuthConfigService, "bulk_update_category", _boom)

        response = client.put(
            f"{BASE}/mfa", headers=super_admin_token_headers, json={"mfa_issuer_name": "x"}
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.json()["detail"] == INTERNAL_DETAIL
        assert "hunter2" not in response.text

    def test_is_audited_as_a_failure_by_key_name_only(
        self, client, super_admin_token_headers, super_admin_user, monkeypatch
    ):
        monkeypatch.setattr(AuthConfigService, "bulk_update_category", _boom)
        events = _collect_audit(monkeypatch)
        actor_id, actor_email = super_admin_user.id, str(super_admin_user.email)

        client.put(
            f"{BASE}/ldap",
            headers=super_admin_token_headers,
            json={"ldap_bind_password": "s3cret-value", "ldap_server": "h"},
        )

        failures = _settings_changes(events, "auth_config_update")
        assert len(failures) == 1, events
        record = failures[0]
        assert record["outcome"] is AuditOutcome.FAILURE
        assert record["error_code"] == "AUTH_CONFIG_UPDATE_FAILED"
        assert record["user_id"] == actor_id
        assert record["username"] == actor_email
        assert record["details"]["requested_keys"] == ["ldap_bind_password", "ldap_server"]
        assert "s3cret-value" not in repr(record)

    @pytest.mark.parametrize(
        ("raised", "expected_status"),
        [
            (HTTPException(status_code=403, detail="deliberate"), 403),
            (AccessDeniedError("deliberate"), 403),
        ],
        ids=["http_exception", "domain_error"],
    )
    def test_a_deliberate_refusal_is_not_reported_as_a_500(
        self, client, super_admin_token_headers, monkeypatch, raised, expected_status
    ):
        def refuse(*_a, **_k):
            raise raised

        monkeypatch.setattr(AuthConfigService, "bulk_update_category", refuse)
        events = _collect_audit(monkeypatch)

        response = client.put(
            f"{BASE}/mfa", headers=super_admin_token_headers, json={"mfa_issuer_name": "x"}
        )

        assert response.status_code == expected_status
        assert response.json()["detail"] == "deliberate"
        assert _settings_changes(events, "auth_config_update") == []


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

    @pytest.mark.parametrize(
        ("raised", "expected_status"),
        [
            (HTTPException(status_code=409, detail="deliberate"), 409),
            (AccessDeniedError("deliberate"), 403),
        ],
        ids=["http_exception", "domain_error"],
    )
    def test_a_deliberate_refusal_is_not_reported_as_a_500(
        self, client, super_admin_token_headers, monkeypatch, raised, expected_status
    ):
        def refuse(*_a, **_k):
            raise raised

        monkeypatch.setattr(AuthConfigService, "migrate_from_env", refuse)

        response = client.post(f"{BASE}/migrate", headers=super_admin_token_headers)

        assert response.status_code == expected_status
        assert response.json()["detail"] == "deliberate"
