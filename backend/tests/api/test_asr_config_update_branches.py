"""Characterization of every field branch of ``PUT /api/asr-settings/config/{uuid}``.

``update_asr_config`` applies each field of the partial update independently; these tests
pin the observable result of each one (name uniqueness, key replace/keep/encrypt-failure,
provider change side effects, region allowlist, active and shared toggles) so the handler
can be decomposed without changing behaviour.
"""

from __future__ import annotations

import pytest

from app.api.endpoints import asr_settings as endpoint
from app.models.user_asr_settings import UserASRSettings

_ASR = "/api/asr-settings"


def _create(client, headers, name, **kw):
    body = {"name": name, "provider": "local", "model_name": "large-v3-turbo", **kw}
    resp = client.post(_ASR, json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put(client, headers, uuid, body):
    return client.put(f"{_ASR}/config/{uuid}", json=body, headers=headers)


def _row(db_session, uuid) -> UserASRSettings:
    db_session.expire_all()
    row: UserASRSettings = (
        db_session.query(UserASRSettings).filter(UserASRSettings.uuid == uuid).one()
    )
    return row


def test_rename_to_another_configs_name_is_409_and_changes_nothing(
    client, user_token_headers, db_session
):
    _create(client, user_token_headers, "Taken")
    other = _create(client, user_token_headers, "Mine")
    resp = _put(client, user_token_headers, other["uuid"], {"name": "Taken"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Configuration named 'Taken' already exists"
    assert _row(db_session, other["uuid"]).name == "Mine"


def test_renaming_a_config_to_its_own_name_succeeds(client, user_token_headers):
    cfg = _create(client, user_token_headers, "Same")
    resp = _put(client, user_token_headers, cfg["uuid"], {"name": "Same"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Same"


def test_new_api_key_is_stored_encrypted_and_resets_test_status(
    client, user_token_headers, db_session
):
    cfg = _create(
        client, user_token_headers, "Keyed", provider="deepgram", model_name="nova-3", api_key="k1"
    )
    row = _row(db_session, cfg["uuid"])
    row.test_status = "success"
    db_session.flush()
    old_blob = row.api_key
    resp = _put(client, user_token_headers, cfg["uuid"], {"api_key": "brand-new-key"})
    assert resp.status_code == 200
    assert resp.json()["has_api_key"] is True
    assert "brand-new-key" not in resp.text
    row = _row(db_session, cfg["uuid"])
    assert row.api_key and row.api_key != old_blob and "brand-new-key" not in row.api_key
    assert row.test_status is None


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_or_null_api_key_keeps_the_stored_key(client, user_token_headers, db_session, blank):
    cfg = _create(
        client, user_token_headers, "Keep", provider="deepgram", model_name="nova-3", api_key="k1"
    )
    row = _row(db_session, cfg["uuid"])
    row.test_status = "success"
    db_session.flush()
    before = row.api_key
    resp = _put(client, user_token_headers, cfg["uuid"], {"api_key": blank, "model_name": "nova-2"})
    assert resp.status_code == 200
    row = _row(db_session, cfg["uuid"])
    assert row.api_key == before
    assert row.test_status == "success"
    assert row.model_name == "nova-2"


def test_api_key_with_broken_encryption_is_500_and_key_is_untouched(
    client, user_token_headers, db_session, monkeypatch
):
    cfg = _create(
        client, user_token_headers, "Enc", provider="deepgram", model_name="nova-3", api_key="k1"
    )
    before = _row(db_session, cfg["uuid"]).api_key
    monkeypatch.setattr(endpoint, "test_encryption", lambda: False)
    resp = _put(client, user_token_headers, cfg["uuid"], {"api_key": "k2"})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Encryption system is not working properly"
    assert _row(db_session, cfg["uuid"]).api_key == before


def test_api_key_that_fails_to_encrypt_is_500(client, user_token_headers, monkeypatch):
    cfg = _create(
        client, user_token_headers, "Enc2", provider="deepgram", model_name="nova-3", api_key="k1"
    )
    monkeypatch.setattr(endpoint, "encrypt_api_key", lambda _v: "")
    resp = _put(client, user_token_headers, cfg["uuid"], {"api_key": "k2"})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to encrypt API key"


def test_access_key_id_is_stored_encrypted_and_blank_keeps_it(
    client, user_token_headers, db_session
):
    cfg = _create(
        client,
        user_token_headers,
        "Aws",
        provider="aws",
        model_name="standard",
        api_key="secret",
        access_key_id="AKIAOLD",
        region="us-east-1",
    )
    original = _row(db_session, cfg["uuid"]).access_key_id
    assert _put(client, user_token_headers, cfg["uuid"], {"access_key_id": "  "}).status_code == 200
    assert _row(db_session, cfg["uuid"]).access_key_id == original
    resp = _put(client, user_token_headers, cfg["uuid"], {"access_key_id": "AKIANEW"})
    assert resp.status_code == 200
    new = _row(db_session, cfg["uuid"]).access_key_id
    assert new and new != original and "AKIANEW" not in new


def test_access_key_id_encryption_failures_are_500(client, user_token_headers, monkeypatch):
    cfg = _create(
        client,
        user_token_headers,
        "Aws2",
        provider="aws",
        model_name="standard",
        api_key="secret",
        access_key_id="AKIAOLD",
        region="us-east-1",
    )
    monkeypatch.setattr(endpoint, "test_encryption", lambda: False)
    resp = _put(client, user_token_headers, cfg["uuid"], {"access_key_id": "AKIANEW"})
    assert (resp.status_code, resp.json()["detail"]) == (
        500,
        "Encryption system is not working properly",
    )
    monkeypatch.setattr(endpoint, "test_encryption", lambda: True)
    monkeypatch.setattr(endpoint, "encrypt_api_key", lambda _v: None)
    resp = _put(client, user_token_headers, cfg["uuid"], {"access_key_id": "AKIANEW"})
    assert (resp.status_code, resp.json()["detail"]) == (500, "Failed to encrypt access key ID")


def test_region_is_validated_against_the_existing_provider_and_stored(
    client, user_token_headers, db_session
):
    cfg = _create(
        client,
        user_token_headers,
        "Reg",
        provider="aws",
        model_name="standard",
        api_key="secret",
        access_key_id="AKIAOLD",
        region="us-east-1",
    )
    bad = _put(client, user_token_headers, cfg["uuid"], {"region": "mars-west-1"})
    assert bad.status_code == 400
    assert bad.json()["detail"] == "Unknown AWS region 'mars-west-1'"
    assert _row(db_session, cfg["uuid"]).region == "us-east-1"
    assert _put(client, user_token_headers, cfg["uuid"], {"region": "eu-west-1"}).status_code == 200
    assert _row(db_session, cfg["uuid"]).region == "eu-west-1"


def test_base_url_and_model_name_are_updated(client, user_token_headers, db_session):
    cfg = _create(client, user_token_headers, "Url")
    resp = _put(
        client,
        user_token_headers,
        cfg["uuid"],
        {"base_url": "https://asr.example.com", "model_name": "large-v3"},
    )
    assert resp.status_code == 200
    row = _row(db_session, cfg["uuid"])
    assert (row.base_url, row.model_name) == ("https://asr.example.com", "large-v3")


def test_same_provider_keeps_the_key_and_a_changed_provider_clears_it_and_test_status(
    client, user_token_headers, db_session
):
    cfg = _create(
        client, user_token_headers, "Prov", provider="deepgram", model_name="nova-3", api_key="k1"
    )
    row = _row(db_session, cfg["uuid"])
    row.test_status, row.test_message = "success", "ok"
    db_session.flush()
    key = row.api_key
    assert (
        _put(
            client,
            user_token_headers,
            cfg["uuid"],
            {"provider": "deepgram", "model_name": "nova-3"},
        ).status_code
        == 200
    )
    row = _row(db_session, cfg["uuid"])
    assert (row.api_key, row.test_status, row.test_message) == (key, None, None)
    _put(
        client,
        user_token_headers,
        cfg["uuid"],
        {"provider": "assemblyai", "model_name": "universal"},
    )
    row = _row(db_session, cfg["uuid"])
    assert (row.api_key, row.provider) == (None, "assemblyai")


def test_is_active_toggle_off_on_a_non_active_config_is_stored(
    client, user_token_headers, db_session
):
    _create(client, user_token_headers, "First")  # auto-activates
    second = _create(client, user_token_headers, "Second")
    resp = _put(client, user_token_headers, second["uuid"], {"is_active": False})
    assert resp.status_code == 200
    assert _row(db_session, second["uuid"]).is_active is False
    assert _put(client, user_token_headers, second["uuid"], {"is_active": True}).status_code == 200
    assert _row(db_session, second["uuid"]).is_active is True
