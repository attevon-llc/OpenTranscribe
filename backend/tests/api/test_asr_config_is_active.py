"""``UserASRSettings.is_active`` is honoured by the ASR settings endpoints (#1207).

An inactive configuration is never used for a job, cannot be made the active one, is not
auto-activated as a first config, and is not promoted when the active config is deleted.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.models import UserSetting

_BASE = "/api/asr-settings"

pytestmark = pytest.mark.usefixtures("pipeline_env")


def _active_id(db_session, user) -> str | None:
    db_session.expire_all()
    row = (
        db_session.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.setting_key == "active_asr_config_id")
        .one_or_none()
    )
    return None if row is None else str(row.setting_value)


def test_set_active_refuses_an_inactive_config(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    current = add_asr_config("deepgram", "nova-3", active=True)
    off = add_asr_config("assemblyai", "universal", enabled=False)

    response = client.post(
        f"{_BASE}/set-active", json={"config_uuid": str(off.uuid)}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["code"] == "asr_config_inactive"
    assert _active_id(db_session, normal_user) == str(current.id)


def test_set_active_accepts_the_same_config_once_it_is_switched_on(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    """Control for the refusal: only ``is_active`` differs."""
    add_asr_config("deepgram", "nova-3", active=True)
    on = add_asr_config("assemblyai", "universal")

    response = client.post(
        f"{_BASE}/set-active", json={"config_uuid": str(on.uuid)}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _active_id(db_session, normal_user) == str(on.id)


def test_a_first_config_created_inactive_is_not_auto_activated(
    client, user_token_headers, normal_user, db_session
):
    response = client.post(
        _BASE,
        json={
            "name": "deepgram-off",
            "provider": "deepgram",
            "model_name": "nova-3",
            "is_active": False,
        },
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert response.json()["auto_activated"] is False
    assert _active_id(db_session, normal_user) is None


def test_status_and_list_do_not_present_an_inactive_pointer_as_active(
    client, user_token_headers, normal_user, add_asr_config
):
    cfg = add_asr_config("deepgram", "nova-3", active=True, enabled=False)

    status_body = client.get(f"{_BASE}/status", headers=user_token_headers).json()
    list_body = client.get(_BASE, headers=user_token_headers).json()

    assert status_body["active_config"] is None
    assert status_body["using_local_default"] is True
    assert list_body["active_config_uuid"] is None
    assert [c["uuid"] for c in list_body["configs"]] == [str(cfg.uuid)]


def test_deleting_the_active_config_never_promotes_an_inactive_one(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    add_asr_config("assemblyai", "universal", enabled=False)
    active = add_asr_config("deepgram", "nova-3", active=True)

    response = client.delete(f"{_BASE}/config/{active.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["config_uuid"] is None
    assert _active_id(db_session, normal_user) is None
