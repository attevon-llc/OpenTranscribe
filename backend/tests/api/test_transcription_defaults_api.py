"""Deployment-wide VAD defaults: stored by an admin, honoured by the task path, audited."""

from __future__ import annotations

from app.auth.audit_events import AuditEventType
from app.core.constants import DEFAULT_VAD_MIN_SILENCE_MS
from app.models import UserSetting
from app.services.transcription_defaults import get_deployment_vad_defaults
from app.tasks.transcription.user_settings import _get_user_transcription_settings

URL = "/api/admin/transcription-defaults"


def test_untouched_deployment_keeps_the_coded_defaults(client, admin_token_headers):
    body = client.get(URL, headers=admin_token_headers).json()
    assert body["vad_min_silence_ms"] == DEFAULT_VAD_MIN_SILENCE_MS


def test_put_is_audited_and_changes_what_a_task_without_user_values_uses(
    client, admin_token_headers, db_session, normal_user, monkeypatch
):
    events = []
    monkeypatch.setattr(
        "app.api.endpoints.transcription_defaults.audit_logger.log", lambda **kw: events.append(kw)
    )
    assert (
        client.put(URL, json={"vad_min_silence_ms": 1234}, headers=admin_token_headers).status_code
        == 200
    )
    assert [e["event_type"] for e in events] == [AuditEventType.ADMIN_SETTINGS_CHANGE]
    assert get_deployment_vad_defaults(db_session).vad_min_silence_ms == 1234
    assert (
        _get_user_transcription_settings(db_session, normal_user.id)["vad_min_silence_ms"] == 1234
    )


def test_a_users_own_value_still_wins(client, admin_token_headers, db_session, normal_user):
    client.put(URL, json={"vad_min_silence_ms": 1234}, headers=admin_token_headers)
    db_session.add(
        UserSetting(
            user_id=normal_user.id,
            setting_key="transcription_vad_min_silence_ms",
            setting_value="900",
        )
    )
    db_session.commit()
    assert _get_user_transcription_settings(db_session, normal_user.id)["vad_min_silence_ms"] == 900


def test_out_of_range_values_are_rejected(client, admin_token_headers):
    assert (
        client.put(URL, json={"vad_threshold": 2.0}, headers=admin_token_headers).status_code == 422
    )
    assert client.put(URL, json={}, headers=admin_token_headers).status_code == 400


def test_a_normal_user_cannot_write_the_defaults(client, user_token_headers):
    assert (
        client.put(URL, json={"vad_min_silence_ms": 500}, headers=user_token_headers).status_code
        == 403
    )
