"""Password sign-in records its audit events through the outbox (scale_foundations_plan.md 5.6).

``auth.login.success`` and ``auth.session.created`` were two synchronous OpenSearch writes on
the request thread (0.6-1.95 s each in the smoke runs). They are now staged in the audit outbox
in the session's own transaction, so the session, its ``last_login_at`` stamp and both events
commit together, and delivered after the response (``audit_outbox.deliver`` as the response's
background task); the beat relay retries whatever that misses.

Real endpoint, real database (the ``client`` savepoint harness), production authenticator.
"""

from __future__ import annotations

import pytest

from app.auth.audit import audit_logger
from app.core.config import settings
from app.models.audit_outbox import AuditOutbox
from app.services import audit_outbox

URL = "/api/auth/token"


@pytest.fixture(autouse=True)
def production_authenticator(client, monkeypatch):
    """The suite sets ``TESTING``, which routes login to an ORM-only test authenticator.
    Cleared after ``client`` has started the app, whose startup reads it too."""
    monkeypatch.setenv("TESTING", "False")


def _login(client, user, password: str):
    return client.post(URL, data={"username": user.email, "password": password})


class TestTheLoginAuditEventsGoThroughTheOutbox:
    @pytest.fixture
    def sink(self, monkeypatch) -> dict[str, list]:
        """What reached OpenSearch synchronously, and what was handed to delivery."""
        seen: dict[str, list] = {"indexed": [], "delivered": []}
        monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
        monkeypatch.setattr(settings, "AUDIT_LOG_TO_OPENSEARCH", True)
        monkeypatch.setattr(
            audit_logger, "_index_to_opensearch", lambda event: seen["indexed"].append(event)
        )
        monkeypatch.setattr(
            audit_outbox, "relay", lambda only=None, **kw: seen["delivered"].append(list(only))
        )
        return seen

    def _rows(self, db_session, user) -> dict[str, AuditOutbox]:
        db_session.expire_all()
        rows = (
            db_session.query(AuditOutbox)
            .filter(
                AuditOutbox.event_type.in_(("auth.login.success", "auth.session.created")),
                AuditOutbox.payload["user_id"].astext == str(user.id),
            )
            .all()
        )
        return {row.event_type: row for row in rows}

    def test_a_login_stages_both_events_and_indexes_neither_on_the_request(
        self, client, db_session, normal_user, sink
    ):
        response = _login(client, normal_user, "password123")  # the normal_user fixture's

        assert response.status_code == 200, response.text
        rows = self._rows(db_session, normal_user)
        assert set(rows) == {"auth.login.success", "auth.session.created"}
        assert rows["auth.login.success"].payload["details"]["auth_method"] == "local"
        assert rows["auth.login.success"].payload["username"] == normal_user.email
        indexed = {event["event_type"] for event in sink["indexed"]}
        assert not indexed & {"auth.login.success", "auth.session.created"}, indexed
        assert len(sink["delivered"]) == 1, "one delivery, after the response"
        assert sorted(sink["delivered"][0]) == sorted(str(r.event_uuid) for r in rows.values())

    def test_a_refused_login_stages_no_success_event(self, client, db_session, normal_user, sink):
        """Control: the failure path is unchanged and leaves no success record behind."""
        response = _login(client, normal_user, "not-the-password")

        assert response.status_code == 401
        assert self._rows(db_session, normal_user) == {}
        assert sink["delivered"] == []
