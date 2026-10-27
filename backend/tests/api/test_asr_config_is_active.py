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


# --- switching a configuration OFF is a switch too (#1225 class) -----------------------------


def _put_active(client, headers, cfg, value: bool):
    return client.put(f"{_BASE}/config/{cfg.uuid}", json={"is_active": value}, headers=headers)


def _row_is_active(db_session, cfg) -> bool:
    db_session.expire_all()
    return bool(db_session.get(type(cfg), cfg.id).is_active)


def test_switching_off_the_active_config_is_refused_when_the_default_is_unrunnable(
    client, user_token_headers, normal_user, db_session, add_asr_config, pipeline_env
):
    """An inactive config is treated as absent, so this is a silent switch to the default."""
    pipeline_env("lite")
    current = add_asr_config("deepgram", "nova-3", active=True)

    response = _put_active(client, user_token_headers, current, False)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["code"] == "local_asr_unavailable"
    assert _row_is_active(db_session, current) is True
    assert _active_id(db_session, normal_user) == str(current.id)


def test_switching_off_the_active_config_still_works_where_the_default_runs(
    client, user_token_headers, db_session, add_asr_config
):
    """Control: the same request on a full deployment, where local Whisper is a valid default."""
    current = add_asr_config("deepgram", "nova-3", active=True)

    response = _put_active(client, user_token_headers, current, False)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _row_is_active(db_session, current) is False


def test_switching_off_a_config_nobody_uses_is_not_a_switch(
    client, user_token_headers, db_session, add_asr_config, pipeline_env
):
    pipeline_env("lite")
    add_asr_config("deepgram", "nova-3", active=True)
    spare = add_asr_config("assemblyai", "universal")

    response = _put_active(client, user_token_headers, spare, False)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _row_is_active(db_session, spare) is False


def test_switching_a_config_on_is_never_refused(
    client, user_token_headers, db_session, add_asr_config, pipeline_env
):
    pipeline_env("lite")
    off = add_asr_config("deepgram", "nova-3", enabled=False)

    response = _put_active(client, user_token_headers, off, True)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _row_is_active(db_session, off) is True


def test_switching_off_a_shared_config_a_member_uses_is_refused(
    client,
    other_user,
    other_user_auth_headers,
    normal_user,
    db_session,
    add_asr_config,
    pipeline_env,
):
    """The owner's own pointer is elsewhere; a member's pointer would be sent to the default."""
    pipeline_env("lite")
    shared = add_asr_config("deepgram", "nova-3", owner=other_user, shared=True)
    db_session.add(
        UserSetting(
            user_id=normal_user.id, setting_key="active_asr_config_id", setting_value=str(shared.id)
        )
    )
    db_session.commit()

    response = _put_active(client, other_user_auth_headers, shared, False)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["code"] == "local_asr_unavailable"
    assert _row_is_active(db_session, shared) is True
    assert _active_id(db_session, normal_user) == str(shared.id)


# --- unsharing or deleting a shared configuration moves its members the same way -------------


def _member_on_shared(db_session, add_asr_config, owner, member):
    shared = add_asr_config("deepgram", "nova-3", owner=owner, shared=True)
    db_session.add(
        UserSetting(
            user_id=member.id, setting_key="active_asr_config_id", setting_value=str(shared.id)
        )
    )
    db_session.commit()
    return shared


def _is_shared(db_session, cfg) -> bool:
    db_session.expire_all()
    row = db_session.get(type(cfg), cfg.id)
    return False if row is None else bool(row.is_shared)


def test_unsharing_a_config_a_member_uses_is_refused_when_the_default_is_unrunnable(
    client,
    other_user,
    other_user_auth_headers,
    normal_user,
    db_session,
    add_asr_config,
    pipeline_env,
):
    pipeline_env("lite")
    shared = _member_on_shared(db_session, add_asr_config, other_user, normal_user)

    response = client.put(
        f"{_BASE}/config/{shared.uuid}", json={"is_shared": False}, headers=other_user_auth_headers
    )

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["code"] == "local_asr_unavailable"
    assert _is_shared(db_session, shared) is True
    assert _active_id(db_session, normal_user) == str(shared.id)


def test_unsharing_works_where_the_member_default_runs(
    client, other_user, other_user_auth_headers, normal_user, db_session, add_asr_config
):
    """Control: on a full deployment the member falls back to local Whisper, as before."""
    shared = _member_on_shared(db_session, add_asr_config, other_user, normal_user)

    response = client.put(
        f"{_BASE}/config/{shared.uuid}", json={"is_shared": False}, headers=other_user_auth_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _is_shared(db_session, shared) is False
    assert _active_id(db_session, normal_user) is None


def test_unsharing_is_not_refused_over_the_owners_own_pointer(
    client, normal_user, user_token_headers, db_session, add_asr_config, pipeline_env
):
    """Only members are assessed: the owner keeps the config, so nothing moves for them."""
    pipeline_env("lite")
    mine = add_asr_config("deepgram", "nova-3", active=True, shared=True)

    response = client.put(
        f"{_BASE}/config/{mine.uuid}", json={"is_shared": False}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _is_shared(db_session, mine) is False
    assert _active_id(db_session, normal_user) == str(mine.id)


def test_deleting_a_config_a_member_uses_is_refused_when_the_default_is_unrunnable(
    client,
    other_user,
    other_user_auth_headers,
    normal_user,
    db_session,
    add_asr_config,
    pipeline_env,
):
    pipeline_env("lite")
    shared = _member_on_shared(db_session, add_asr_config, other_user, normal_user)

    response = client.delete(f"{_BASE}/config/{shared.uuid}", headers=other_user_auth_headers)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["code"] == "local_asr_unavailable"
    assert db_session.get(type(shared), shared.id) is not None
    assert _active_id(db_session, normal_user) == str(shared.id)


def test_deleting_works_where_the_member_default_runs(
    client, other_user, other_user_auth_headers, normal_user, db_session, add_asr_config
):
    """Control: on a full deployment the delete goes through and the member's pointer clears."""
    shared = _member_on_shared(db_session, add_asr_config, other_user, normal_user)
    shared_id = shared.id

    response = client.delete(f"{_BASE}/config/{shared.uuid}", headers=other_user_auth_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.expire_all()
    assert db_session.get(type(shared), shared_id) is None
    assert _active_id(db_session, normal_user) is None
