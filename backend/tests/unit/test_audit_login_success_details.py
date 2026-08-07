"""P1.2 — ``log_login_success`` carries method-specific diagnostics.

OIDC needs to log which claim *names* a token carried (never values) so an admin
can see, after a first login, that "groups" existed and "realm_access" did not.
This pins the merge behaviour of the new optional ``details`` param without
touching the many existing call sites that pass only ``auth_method``.
"""

from app.auth.audit import AuditEventType
from app.auth.audit import AuditLogger
from app.auth.audit import AuditOutcome


def _capture_log_calls(monkeypatch, logger: AuditLogger) -> list[dict]:
    calls: list[dict] = []

    def _fake_log(self, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(AuditLogger, "log", _fake_log)
    return calls


class TestLogLoginSuccessDetails:
    def test_details_omitted_behaves_exactly_as_before(self, monkeypatch):
        logger = AuditLogger()
        calls = _capture_log_calls(monkeypatch, logger)

        logger.log_login_success(
            user_id=1,
            username="user@example.com",
            source_ip="127.0.0.1",
            user_agent="pytest",
            auth_method="local",
        )

        assert calls[0]["details"] == {"auth_method": "local"}
        assert calls[0]["event_type"] == AuditEventType.AUTH_LOGIN_SUCCESS
        assert calls[0]["outcome"] == AuditOutcome.SUCCESS

    def test_extra_details_are_merged_alongside_auth_method(self, monkeypatch):
        logger = AuditLogger()
        calls = _capture_log_calls(monkeypatch, logger)

        logger.log_login_success(
            user_id=1,
            username="user@example.com",
            source_ip="127.0.0.1",
            user_agent="pytest",
            auth_method="oidc",
            details={
                "claim_keys": ["email", "groups", "sub"],
                "roles_claim": "groups",
                "roles_claim_source": "id_token",
            },
        )

        assert calls[0]["details"] == {
            "claim_keys": ["email", "groups", "sub"],
            "roles_claim": "groups",
            "roles_claim_source": "id_token",
            "auth_method": "oidc",
        }

    def test_auth_method_kwarg_wins_over_a_details_key_of_the_same_name(self, monkeypatch):
        """`auth_method` is the caller's contract, not something a details dict can override."""
        logger = AuditLogger()
        calls = _capture_log_calls(monkeypatch, logger)

        logger.log_login_success(
            user_id=1,
            username="user@example.com",
            source_ip="127.0.0.1",
            user_agent="pytest",
            auth_method="oidc",
            details={"auth_method": "spoofed"},
        )

        assert calls[0]["details"]["auth_method"] == "oidc"
