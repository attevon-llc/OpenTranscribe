"""``/admin/youtube-settings``: stored policy, bounds, authorization, audit and the env seed."""

from __future__ import annotations

import pytest

from app.auth.audit_events import AuditEventType
from app.core.youtube_defaults import DEFAULT_YOUTUBE_RECOVERY_BATCH_SIZE
from app.core.youtube_defaults import DEFAULT_YOUTUBE_USER_RATE_LIMIT_PER_HOUR
from app.services import youtube_policy

URL = "/api/admin/youtube-settings"
_ENV = [env for _key, env, _desc in youtube_policy.POLICY_KEYS.values()]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_when_nothing_is_stored(client, admin_token_headers):
    body = client.get(URL, headers=admin_token_headers).json()
    assert body["user_rate_limit_per_hour"] == DEFAULT_YOUTUBE_USER_RATE_LIMIT_PER_HOUR
    assert body["recovery_batch_size"] == DEFAULT_YOUTUBE_RECOVERY_BATCH_SIZE
    assert body["auto_retry_enabled"] is True


def test_put_persists_and_is_audited(client, admin_token_headers, monkeypatch):
    events = []
    monkeypatch.setattr(
        "app.api.endpoints.youtube_settings.audit_logger.log", lambda **kw: events.append(kw)
    )
    resp = client.put(
        URL,
        json={"user_rate_limit_per_hour": 7, "auto_retry_enabled": False},
        headers=admin_token_headers,
    )
    assert resp.status_code == 200
    assert client.get(URL, headers=admin_token_headers).json()["user_rate_limit_per_hour"] == 7
    assert [e["event_type"] for e in events] == [AuditEventType.ADMIN_SETTINGS_CHANGE]
    assert events[0]["details"]["changed"] == {
        "auto_retry_enabled": False,
        "user_rate_limit_per_hour": 7,
    }


@pytest.mark.parametrize(
    "body",
    [{"user_rate_limit_per_hour": 0}, {"recovery_batch_size": 101}, {"unknown_field": 1}],
)
def test_invalid_bodies_are_rejected(client, admin_token_headers, body):
    assert client.put(URL, json=body, headers=admin_token_headers).status_code == 422


def test_an_empty_update_is_a_bad_request(client, admin_token_headers):
    assert client.put(URL, json={}, headers=admin_token_headers).status_code == 400


def test_a_normal_user_cannot_read_or_write(client, user_token_headers):
    assert client.get(URL, headers=user_token_headers).status_code == 403
    assert (
        client.put(URL, json={"recovery_batch_size": 2}, headers=user_token_headers).status_code
        == 403
    )


def test_seed_copies_the_env_once_and_never_overwrites(db_session, monkeypatch):
    monkeypatch.setenv("YOUTUBE_USER_RATE_LIMIT_PER_HOUR", "11")
    monkeypatch.setenv("YOUTUBE_AUTO_RETRY_ENABLED", "false")
    assert sorted(youtube_policy.seed_from_env(db_session)) == [
        "YOUTUBE_AUTO_RETRY_ENABLED",
        "YOUTUBE_USER_RATE_LIMIT_PER_HOUR",
    ]
    policy = youtube_policy.get_youtube_policy(db_session)
    assert (policy.user_rate_limit_per_hour, policy.auto_retry_enabled) == (11, False)

    youtube_policy.update_youtube_policy(db_session, {"user_rate_limit_per_hour": 5})
    monkeypatch.setenv("YOUTUBE_USER_RATE_LIMIT_PER_HOUR", "99")
    assert youtube_policy.seed_from_env(db_session) == []
    assert youtube_policy.get_youtube_policy(db_session).user_rate_limit_per_hour == 5


def test_seed_ignores_unset_and_clamps_garbage(db_session, monkeypatch):
    assert youtube_policy.seed_from_env(db_session) == []
    monkeypatch.setenv("YOUTUBE_RECOVERY_BATCH_SIZE", "100000")
    youtube_policy.seed_from_env(db_session)
    assert (
        youtube_policy.get_youtube_policy(db_session).recovery_batch_size
        == DEFAULT_YOUTUBE_RECOVERY_BATCH_SIZE
    )
