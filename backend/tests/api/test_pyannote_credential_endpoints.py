"""The pyannote.ai diarization credential is configurable, secret and enforced (issue #1204).

Before #1204 nothing wrote ``UserDiarizationSettings``, so the ``pyannote`` speaker-detection
choice could never work and the pipeline fell back without telling anyone. These tests pin the
credential surface that fixes it:

- the key is stored encrypted (the column never holds the plaintext) and decrypts back;
- the key never appears in a response body, a log record or an audit event;
- choosing ``pyannote`` without a key is refused with a readable 409;
- deleting the key reverts a ``pyannote`` selection to the default;
- a user only ever sees and changes their own key;
- the capability gate (404) and the #1109 diarization-source lock (409) both hold;
- "test connection" calls only the vendor's non-billable ``GET /v1/test``.

The vendor is a local fake HTTP server; the real pyannote.ai API is never contacted.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
from collections.abc import Iterator

import pytest
from fastapi import status

from app.auth.audit import AuditEventType
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models import UserSetting
from app.models.user_diarization_settings import UserDiarizationSettings

_BASE = "/api/user-settings/diarization/pyannote"
_TRANSCRIPTION = "/api/user-settings/transcription"
# Low-entropy on purpose (secret scanners flag realistic fakes); still distinctive enough
# that a substring hit can only be a leak.
_KEY = "fixture-key-aaaa-1111"
_KEY_OTHER = "fixture-key-bbbb-2222"


@pytest.fixture
def caps_off():
    def _install(*keys: str) -> None:
        overrides = dict.fromkeys(keys, False)
        set_capability_resolver(lambda _request: {**COMMUNITY_CAPABILITIES, **overrides})

    yield _install
    reset_capability_resolver()


@pytest.fixture
def audit_events(monkeypatch) -> list[dict]:
    from app.api.endpoints import diarization_settings

    events: list[dict] = []
    monkeypatch.setattr(diarization_settings.audit_logger, "log", lambda **kw: events.append(kw))
    return events


class _FakeVendor(http.server.BaseHTTPRequestHandler):
    """Answers like pyannote.ai's ``GET /v1/test``; records every request it sees."""

    accepted_key = _KEY
    seen: list[tuple[str, str]] = []

    def _record(self) -> None:
        type(self).seen.append((self.command, self.path))

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._record()
        auth = self.headers.get("Authorization", "")
        if self.path == "/v1/test" and auth == f"Bearer {type(self).accepted_key}":
            self._reply(200, {"status": "ok"})
        else:
            # Echo the presented credential, as a careless vendor error page might.
            self._reply(401, {"error": f"invalid token {auth}"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._record()
        self._reply(500, {"error": "billable endpoint must not be called"})

    def _reply(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def fake_vendor(monkeypatch) -> Iterator[type[_FakeVendor]]:
    from app.services.diarization import pyannote_provider

    handler = type("Vendor", (_FakeVendor,), {"seen": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(pyannote_provider, "_API_BASE", f"http://127.0.0.1:{server.server_port}")
    try:
        yield handler
    finally:
        server.shutdown()
        server.server_close()


def _row(db_session, user) -> UserDiarizationSettings | None:
    db_session.expire_all()
    row: UserDiarizationSettings | None = (
        db_session.query(UserDiarizationSettings)
        .filter(UserDiarizationSettings.user_id == user.id)
        .one_or_none()
    )
    return row


def _stored(db_session, user) -> UserDiarizationSettings:
    row = _row(db_session, user)
    assert row is not None, "expected a stored pyannote.ai credential row"
    return row


def _source(db_session, user) -> str | None:
    db_session.expire_all()
    row = (
        db_session.query(UserSetting)
        .filter(
            UserSetting.user_id == user.id,
            UserSetting.setting_key == "transcription_diarization_source",
        )
        .one_or_none()
    )
    return None if row is None else str(row.setting_value)


def _store(client, headers, key: str = _KEY):
    return client.put(_BASE, json={"api_key": key}, headers=headers)


# ---------------------------------------------------------------------------
# Storage and secrecy
# ---------------------------------------------------------------------------


class TestStorage:
    def test_get_without_a_key_reports_not_configured(self, client, user_token_headers):
        response = client.get(_BASE, headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["configured"] is False
        assert body["locked"] is False
        assert body["test_status"] is None

    def test_put_stores_the_key_encrypted_at_rest(
        self, client, user_token_headers, normal_user, db_session
    ):
        from app.utils.encryption import decrypt_api_key

        response = _store(client, user_token_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["configured"] is True

        row = _row(db_session, normal_user)
        assert row is not None
        assert row.provider == "pyannote"
        assert row.is_active is True
        stored = str(row.api_key)
        assert _KEY not in stored
        assert stored.startswith("v3:")
        assert decrypt_api_key(stored) == _KEY

    def test_put_twice_replaces_rather_than_duplicates(
        self, client, user_token_headers, normal_user, db_session
    ):
        from app.utils.encryption import decrypt_api_key

        assert _store(client, user_token_headers).status_code == status.HTTP_200_OK
        assert _store(client, user_token_headers, _KEY_OTHER).status_code == status.HTTP_200_OK
        rows = (
            db_session.query(UserDiarizationSettings)
            .filter(UserDiarizationSettings.user_id == normal_user.id)
            .all()
        )
        assert len(rows) == 1
        assert decrypt_api_key(str(rows[0].api_key)) == _KEY_OTHER

    @pytest.mark.parametrize("bad", ["", "   ", "has space inside", "short", "x" * 513])
    def test_put_rejects_malformed_keys(self, client, user_token_headers, bad):
        response = _store(client, user_token_headers, bad)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert bad.strip() == "" or bad not in response.text

    def test_key_never_in_any_response_log_or_audit_event(
        self, client, user_token_headers, caplog, audit_events, fake_vendor
    ):
        caplog.set_level(logging.DEBUG)
        bodies = [
            _store(client, user_token_headers).text,
            client.get(_BASE, headers=user_token_headers).text,
            client.post(f"{_BASE}/test", headers=user_token_headers).text,
            client.post(
                f"{_BASE}/test", json={"api_key": _KEY_OTHER}, headers=user_token_headers
            ).text,
            client.delete(_BASE, headers=user_token_headers).text,
        ]
        for body in bodies:
            assert _KEY not in body
            assert _KEY_OTHER not in body
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert _KEY not in logged
        assert _KEY_OTHER not in logged
        assert audit_events, "set/delete must be audited"
        assert _KEY not in repr(audit_events)
        assert _KEY_OTHER not in repr(audit_events)

    def test_set_and_delete_are_audited_without_the_secret(
        self, client, user_token_headers, normal_user, audit_events
    ):
        _store(client, user_token_headers)
        client.delete(_BASE, headers=user_token_headers)
        kinds = [event["event_type"] for event in audit_events]
        assert kinds == [AuditEventType.USER_CREDENTIAL_SET, AuditEventType.USER_CREDENTIAL_DELETE]
        for event in audit_events:
            assert event["user_id"] == normal_user.id
            assert event["details"] == {"provider": "pyannote.ai", "purpose": "diarization"}


# ---------------------------------------------------------------------------
# Selection rules
# ---------------------------------------------------------------------------


class TestSelection:
    def test_selecting_pyannote_without_a_key_is_refused(
        self, client, user_token_headers, normal_user, db_session
    ):
        response = client.put(
            _TRANSCRIPTION, json={"diarization_source": "pyannote"}, headers=user_token_headers
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "pyannote.ai API key" in response.json()["detail"]
        assert _source(db_session, normal_user) is None

    def test_selecting_pyannote_with_a_key_is_accepted(
        self, client, user_token_headers, normal_user, db_session
    ):
        _store(client, user_token_headers)
        response = client.put(
            _TRANSCRIPTION, json={"diarization_source": "pyannote"}, headers=user_token_headers
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["diarization_source"] == "pyannote"
        assert _source(db_session, normal_user) == "pyannote"

    def test_other_sources_need_no_key(self, client, user_token_headers):
        for source in ("provider", "local", "off"):
            response = client.put(
                _TRANSCRIPTION, json={"diarization_source": source}, headers=user_token_headers
            )
            assert response.status_code == status.HTTP_200_OK, (source, response.text)

    def test_resaving_an_unchanged_legacy_selection_is_not_blocked(
        self, client, user_token_headers, normal_user, db_session
    ):
        """A pre-#1204 row (pyannote, no key) must not make every other setting unsaveable;
        the file-time check reports it instead."""
        db_session.add(
            UserSetting(
                user_id=normal_user.id,
                setting_key="transcription_diarization_source",
                setting_value="pyannote",
            )
        )
        db_session.commit()
        response = client.put(
            _TRANSCRIPTION,
            json={"diarization_source": "pyannote", "min_speakers": 2, "max_speakers": 4},
            headers=user_token_headers,
        )
        assert response.status_code == status.HTTP_200_OK, response.text

    def test_delete_reverts_a_pyannote_selection(
        self, client, user_token_headers, normal_user, db_session
    ):
        _store(client, user_token_headers)
        client.put(
            _TRANSCRIPTION, json={"diarization_source": "pyannote"}, headers=user_token_headers
        )
        response = client.delete(_BASE, headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "deleted": True,
            "diarization_source": "provider",
            "source_reverted": True,
        }
        assert _row(db_session, normal_user) is None
        assert _source(db_session, normal_user) is None
        assert (
            client.get(_TRANSCRIPTION, headers=user_token_headers).json()["diarization_source"]
            == "provider"
        )

    def test_delete_leaves_another_source_alone(
        self, client, user_token_headers, normal_user, db_session
    ):
        _store(client, user_token_headers)
        client.put(_TRANSCRIPTION, json={"diarization_source": "off"}, headers=user_token_headers)
        body = client.delete(_BASE, headers=user_token_headers).json()
        assert body["source_reverted"] is False
        assert body["diarization_source"] == "off"
        assert _source(db_session, normal_user) == "off"

    def test_delete_without_a_key_is_404(self, client, user_token_headers):
        assert client.delete(_BASE, headers=user_token_headers).status_code == 404


# ---------------------------------------------------------------------------
# Tenancy, roles, capability and lock
# ---------------------------------------------------------------------------


class TestAccess:
    def test_requires_authentication(self, client):
        assert client.get(_BASE).status_code == status.HTTP_401_UNAUTHORIZED
        assert client.put(_BASE, json={"api_key": _KEY}).status_code == 401
        assert client.delete(_BASE).status_code == 401
        assert client.post(f"{_BASE}/test").status_code == 401

    def test_users_only_see_and_change_their_own_key(
        self, client, user_token_headers, admin_token_headers, normal_user, admin_user, db_session
    ):
        _store(client, user_token_headers)
        assert client.get(_BASE, headers=admin_token_headers).json()["configured"] is False
        # The admin's delete has nothing of theirs to remove and must not touch the user's.
        assert client.delete(_BASE, headers=admin_token_headers).status_code == 404
        assert _row(db_session, normal_user) is not None
        _store(client, admin_token_headers, _KEY_OTHER)
        from app.utils.encryption import decrypt_api_key

        assert decrypt_api_key(str(_stored(db_session, normal_user).api_key)) == _KEY
        assert decrypt_api_key(str(_stored(db_session, admin_user).api_key)) == _KEY_OTHER

    def test_capability_off_hides_the_surface(self, client, user_token_headers, caps_off):
        caps_off("asr.user_providers")
        assert client.get(_BASE, headers=user_token_headers).status_code == 404
        assert _store(client, user_token_headers).status_code == 404

    def test_locked_diarization_source_refuses_writes(
        self, client, user_token_headers, normal_user, db_session, caps_off
    ):
        caps_off("transcription.diarization_source")
        assert client.get(_BASE, headers=user_token_headers).json()["locked"] is True
        response = _store(client, user_token_headers)
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "managed by this deployment" in response.json()["detail"]
        assert _row(db_session, normal_user) is None
        assert client.post(f"{_BASE}/test", headers=user_token_headers).status_code == 409


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------


class TestConnection:
    def test_saved_key_is_validated_against_the_free_endpoint_only(
        self, client, user_token_headers, normal_user, db_session, fake_vendor
    ):
        _store(client, user_token_headers)
        response = client.post(f"{_BASE}/test", headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["success"] is True
        assert body["message"] == "Connected to pyannote.ai"
        assert body["code"] == "connected"
        assert fake_vendor.seen == [("GET", "/v1/test")]
        row = _stored(db_session, normal_user)
        assert row.test_status == "success"
        assert row.last_tested is not None
        assert client.get(_BASE, headers=user_token_headers).json()["test_status"] == "success"

    def test_rejected_key_gets_a_fixed_message_not_the_vendor_body(
        self, client, user_token_headers, normal_user, db_session, fake_vendor
    ):
        _store(client, user_token_headers, _KEY_OTHER)
        body = client.post(f"{_BASE}/test", headers=user_token_headers).json()
        assert body["success"] is False
        assert body["message"] == "pyannote.ai rejected this API key"
        assert body["code"] == "rejected"
        assert _stored(db_session, normal_user).test_status == "failed"

    def test_unsaved_key_can_be_tested_without_storing_it(
        self, client, user_token_headers, normal_user, db_session, fake_vendor
    ):
        body = client.post(
            f"{_BASE}/test", json={"api_key": _KEY}, headers=user_token_headers
        ).json()
        assert body["success"] is True
        assert _row(db_session, normal_user) is None

    def test_testing_with_nothing_saved_is_404(self, client, user_token_headers, fake_vendor):
        assert client.post(f"{_BASE}/test", headers=user_token_headers).status_code == 404
        assert fake_vendor.seen == []

    def test_unreachable_vendor_is_reported_without_internals(
        self, client, user_token_headers, monkeypatch
    ):
        from app.services.diarization import pyannote_provider

        monkeypatch.setattr(pyannote_provider, "_API_BASE", "http://127.0.0.1:9")
        _store(client, user_token_headers)
        body = client.post(f"{_BASE}/test", headers=user_token_headers).json()
        assert body["success"] is False
        assert body["message"] == "Could not reach pyannote.ai"
        assert body["code"] == "unreachable"
