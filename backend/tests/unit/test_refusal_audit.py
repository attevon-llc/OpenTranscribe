"""``audit_user_update_refusal``: the audit record behind every admin user-write refusal.

``api/endpoints/users.py`` and ``api/endpoints/admin.py`` each had a byte-identical
helper. The record's shape is the contract: FAILURE outcome, ``ADMIN_USER_UPDATE`` type,
caller as actor and the account the write targeted as subject (issue #443).
"""

from app.auth import refusal_audit
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.models.user import User


def test_a_refusal_is_a_failed_admin_user_update_naming_actor_and_target(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(refusal_audit.audit_logger, "log", lambda **kw: calls.append(kw))
    target = User(id=5, email="target@example.com")
    actor = User(id=2, email="admin@example.com")

    refusal_audit.audit_user_update_refusal(
        "email_change_denied", target, actor, "10.0.0.9", "UA/1"
    )

    assert calls == [
        {
            "event_type": AuditEventType.ADMIN_USER_UPDATE,
            "outcome": AuditOutcome.FAILURE,
            "user_id": 2,
            "username": "admin@example.com",
            "target_user_id": 5,
            "target_username": "target@example.com",
            "source_ip": "10.0.0.9",
            "user_agent": "UA/1",
            "details": {"action": "email_change_denied"},
        }
    ]
