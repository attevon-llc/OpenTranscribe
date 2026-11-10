"""A Bedrock row whose credential blob no longer decrypts must not break the LLM settings API.

The list answers 200 and reports the credentials as unset; an edit that supplies a whole new
key pair replaces the blob (the recovery path); an edit that would keep part of it answers the
409 ``stored_value_unreadable`` contract the sibling LLM settings routes use.
"""

from __future__ import annotations

from fastapi import status

from app.utils.encryption import STORED_VALUE_UNREADABLE_CODE

_BASE = "/api/llm-settings"
KEY_ID = "AKIAEXAMPLE0FAKE0KEY"  # gitleaks:allow - fabricated fixture
SECRET = "fakeSecretAccessKey0123456789ABCDEFGHIJ"  # gitleaks:allow - fabricated fixture
MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def _create_unreadable(client, headers, db_session) -> str:
    created = client.post(
        _BASE,
        json={
            "name": "Unreadable Bedrock",
            "provider": "bedrock",
            "model_name": MODEL,
            "aws_region": "us-east-1",
            "aws_access_key_id": KEY_ID,
            "aws_secret_access_key": SECRET,
            "max_tokens": 200000,
            "temperature": "0.3",
        },
        headers=headers,
    ).json()
    from app import models

    row = db_session.query(models.UserLLMSettings).filter_by(uuid=created["uuid"]).one()
    row.api_key = "not-a-decryptable-blob"
    db_session.commit()
    return str(created["uuid"])


def test_list_survives_an_undecryptable_bedrock_blob(client, user_token_headers, db_session):
    uuid = _create_unreadable(client, user_token_headers, db_session)

    resp = client.get(_BASE, headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    row = next(c for c in resp.json()["configurations"] if c["uuid"] == uuid)
    assert row["has_aws_credentials"] is False
    assert row["aws_region"] is None


def test_edit_with_a_new_key_pair_replaces_an_undecryptable_blob(
    client, user_token_headers, db_session
):
    uuid = _create_unreadable(client, user_token_headers, db_session)

    resp = client.put(
        f"{_BASE}/config/{uuid}",
        json={
            "aws_region": "eu-west-1",
            "aws_access_key_id": KEY_ID,
            "aws_secret_access_key": SECRET,
        },
        headers=user_token_headers,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["has_aws_credentials"] is True


def test_edit_that_keeps_the_blob_answers_stored_value_unreadable(
    client, user_token_headers, db_session
):
    uuid = _create_unreadable(client, user_token_headers, db_session)

    resp = client.put(
        f"{_BASE}/config/{uuid}", json={"aws_region": "eu-west-1"}, headers=user_token_headers
    )

    assert resp.status_code == status.HTTP_409_CONFLICT
    assert resp.json()["code"] == STORED_VALUE_UNREADABLE_CODE
