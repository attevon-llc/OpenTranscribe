"""Bedrock configurations carry their own region and AWS credentials, write-only.

The contract pinned here, against the real routes and the real database:

* the credentials are stored ENCRYPTED (the column never holds the plaintext) and are
  never present in any response, log line or error message;
* a blank credential field on edit keeps what is stored; ``clear_aws_credentials`` drops it;
* a shared configuration's credentials never reach the user it is shared with;
* "Test connection" uses the entered (unsaved) credentials and answers fixed sentences;
* the encrypted blob cannot be revealed through the API-key reveal route.

The AWS key material is fabricated (AKIA/aws-docs style); no AWS call is made.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import status

_BASE = "/api/llm-settings"
KEY_ID = "AKIAEXAMPLE0FAKE0KEY"  # gitleaks:allow - fabricated fixture
SECRET = "fakeSecretAccessKey0123456789ABCDEFGHIJ"  # gitleaks:allow - fabricated fixture
TOKEN = "fakeSessionTokenValue0123456789"  # gitleaks:allow - fabricated fixture
MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
SECRETS = (KEY_ID, SECRET, TOKEN)


def _payload(name="Bedrock Cfg", **over):
    payload = {
        "name": name,
        "provider": "bedrock",
        "model_name": MODEL,
        "aws_region": "us-east-1",
        "aws_access_key_id": KEY_ID,
        "aws_secret_access_key": SECRET,
        "aws_session_token": TOKEN,
        "max_tokens": 200000,
        "temperature": "0.3",
    }
    payload.update(over)
    return payload


def _assert_no_secret(obj) -> None:
    text = json.dumps(obj) if not isinstance(obj, str) else obj
    for secret in SECRETS:
        assert secret not in text


def _stored_column(db_session, uuid_str: str) -> str | None:
    from app import models

    row = db_session.query(models.UserLLMSettings).filter_by(uuid=uuid_str).one()
    stored: str | None = row.api_key
    return stored


def test_create_stores_the_credentials_encrypted_and_returns_only_flags(
    client, user_token_headers, db_session
):
    resp = client.post(_BASE, json=_payload(), headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    _assert_no_secret(body)
    assert body["aws_region"] == "us-east-1"
    assert body["has_aws_credentials"] is True
    assert body["has_api_key"] is False

    stored = _stored_column(db_session, body["uuid"])
    assert stored
    for secret in SECRETS:
        assert secret not in stored
    from app.services.bedrock_settings import connection_from_encrypted

    connection = connection_from_encrypted(stored)
    assert (connection.access_key_id, connection.secret_access_key) == (KEY_ID, SECRET)
    assert connection.session_token == TOKEN
    assert connection.region == "us-east-1"


def test_no_credentials_means_the_server_chain(client, user_token_headers, db_session):
    resp = client.post(
        _BASE,
        json=_payload(aws_access_key_id=None, aws_secret_access_key=None, aws_session_token=None),
        headers=user_token_headers,
    )

    body = resp.json()
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert body["has_aws_credentials"] is False
    assert body["aws_region"] == "us-east-1"


@pytest.mark.parametrize(
    "bad",
    [
        {"aws_secret_access_key": None},  # key id without secret
        {"aws_access_key_id": None},  # secret without key id
        {"aws_region": "not a region"},
        {"aws_access_key_id": "short"},
        {"aws_access_key_id": None, "aws_secret_access_key": None},  # token without a pair
    ],
)
def test_invalid_aws_fields_are_rejected_without_echoing_secrets(client, user_token_headers, bad):
    resp = client.post(_BASE, json=_payload(**bad), headers=user_token_headers)

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    _assert_no_secret(resp.text)


def test_responses_and_logs_never_carry_a_secret(client, user_token_headers, caplog):
    caplog.set_level(logging.DEBUG)
    created = client.post(_BASE, json=_payload(), headers=user_token_headers).json()

    listing = client.get(_BASE, headers=user_token_headers)
    detail = client.get(f"{_BASE}/config/{created['uuid']}", headers=user_token_headers)
    status_resp = client.get(f"{_BASE}/status", headers=user_token_headers)

    for resp in (listing, detail, status_resp):
        assert resp.status_code == status.HTTP_200_OK
        _assert_no_secret(resp.text)
    _assert_no_secret(caplog.text)


def test_blank_fields_on_edit_keep_the_stored_credentials(client, user_token_headers, db_session):
    created = client.post(_BASE, json=_payload(), headers=user_token_headers).json()
    before = _stored_column(db_session, created["uuid"])

    resp = client.put(
        f"{_BASE}/config/{created['uuid']}",
        json={"model_name": "global.anthropic.claude-sonnet-4-6", "aws_access_key_id": ""},
        headers=user_token_headers,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["has_aws_credentials"] is True
    db_session.expire_all()
    assert _stored_column(db_session, created["uuid"]) == before


def test_new_key_pair_replaces_the_stored_one_and_drops_the_old_token(
    client, user_token_headers, db_session
):
    created = client.post(_BASE, json=_payload(), headers=user_token_headers).json()

    resp = client.put(
        f"{_BASE}/config/{created['uuid']}",
        json={
            "aws_access_key_id": "AKIAEXAMPLE0NEW0KEYS",  # gitleaks:allow - fabricated fixture
            "aws_secret_access_key": "newFakeSecretAccessKey0123456789ABCDEF",  # gitleaks:allow
        },
        headers=user_token_headers,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    from app.services.bedrock_settings import connection_from_encrypted

    db_session.expire_all()
    connection = connection_from_encrypted(_stored_column(db_session, created["uuid"]))
    assert connection.access_key_id == "AKIAEXAMPLE0NEW0KEYS"  # gitleaks:allow
    assert connection.session_token is None
    assert connection.region == "us-east-1"


def test_clear_aws_credentials_returns_to_the_server_chain_and_keeps_the_region(
    client, user_token_headers, db_session
):
    created = client.post(_BASE, json=_payload(), headers=user_token_headers).json()

    resp = client.put(
        f"{_BASE}/config/{created['uuid']}",
        json={"clear_aws_credentials": True},
        headers=user_token_headers,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["has_aws_credentials"] is False
    assert body["aws_region"] == "us-east-1"
    db_session.expire_all()
    from app.services.bedrock_settings import connection_from_encrypted

    connection = connection_from_encrypted(_stored_column(db_session, created["uuid"]))
    assert not connection.has_credentials


def test_the_credentials_blob_cannot_be_revealed_through_the_api_key_route(
    client, user_token_headers
):
    created = client.post(_BASE, json=_payload(), headers=user_token_headers).json()

    resp = client.get(f"{_BASE}/config/{created['uuid']}/api-key", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == {"api_key": None}
    _assert_no_secret(resp.text)


def test_a_shared_configurations_credentials_never_reach_the_other_user(
    client, admin_token_headers, other_user_auth_headers
):
    created = client.post(
        _BASE, json=_payload(name="Shared Bedrock", is_shared=True), headers=admin_token_headers
    )
    assert created.status_code == status.HTTP_200_OK, created.text

    listing = client.get(_BASE, headers=other_user_auth_headers)

    assert listing.status_code == status.HTTP_200_OK
    shared = [c for c in listing.json()["shared_configurations"] if c["name"] == "Shared Bedrock"]
    assert shared, listing.text
    assert shared[0]["has_aws_credentials"] is True
    _assert_no_secret(listing.text)


def test_test_connection_uses_the_entered_unsaved_credentials(client, user_token_headers):
    fake = MagicMock()
    fake.converse_stream.return_value = {
        "stream": iter([{"contentBlockDelta": {"delta": {"text": "hi"}}}])
    }

    with patch("boto3.client", return_value=fake) as boto_client:
        resp = client.post(
            f"{_BASE}/test",
            json={
                "provider": "bedrock",
                "model_name": MODEL,
                "aws_region": "us-west-2",
                "aws_access_key_id": KEY_ID,
                "aws_secret_access_key": SECRET,
            },
            headers=user_token_headers,
        )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["success"] is True
    kwargs = boto_client.call_args.kwargs
    assert kwargs["region_name"] == "us-west-2"
    assert kwargs["aws_access_key_id"] == KEY_ID
    assert kwargs["aws_secret_access_key"] == SECRET
    _assert_no_secret(resp.text)


def test_test_connection_without_credentials_leaves_them_to_the_server_chain(
    client, user_token_headers
):
    fake = MagicMock()
    fake.converse_stream.return_value = {
        "stream": iter([{"contentBlockDelta": {"delta": {"text": "hi"}}}])
    }

    with patch("boto3.client", return_value=fake) as boto_client:
        client.post(
            f"{_BASE}/test",
            json={"provider": "bedrock", "model_name": MODEL, "aws_region": "us-east-1"},
            headers=user_token_headers,
        )

    kwargs = boto_client.call_args.kwargs
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


def test_test_connection_failures_are_fixed_sentences_without_raw_text(client, user_token_headers):
    from botocore.exceptions import ClientError

    raw = f"User: arn:aws:iam::123456789012:user/x is not authorized {SECRET}"
    error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": raw}}, "x")
    fake = MagicMock()
    fake.converse_stream.side_effect = error

    with patch("boto3.client", return_value=fake):
        resp = client.post(
            f"{_BASE}/test",
            json={
                "provider": "bedrock",
                "model_name": MODEL,
                "aws_region": "us-east-1",
                "aws_access_key_id": KEY_ID,
                "aws_secret_access_key": SECRET,
            },
            headers=user_token_headers,
        )

    body = resp.json()
    assert body["success"] is False
    assert "denied access" in body["message"]
    assert "arn:aws" not in resp.text
    _assert_no_secret(resp.text)


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("AccessDeniedException", "You don't have access to the model with the id", "not enabled"),
        ("UnrecognizedClientException", "The security token included is invalid", "rejected"),
        (
            "ValidationException",
            "Invocation of model ID with on-demand throughput isn't supported",
            "inference profile",
        ),
        ("ResourceNotFoundException", "Could not resolve the foundation model", "not enabled"),
        ("ThrottlingException", "Too many requests", "throttling"),
    ],
)
def test_the_first_run_failures_are_told_apart(code, message, expected):
    from botocore.exceptions import ClientError

    from app.services.bedrock_settings import describe_failure

    exc = ClientError({"Error": {"Code": code, "Message": message}}, "ConverseStream")

    assert expected in describe_failure(exc)


def test_provider_switch_drops_the_stored_value_instead_of_misreading_it(
    client, user_token_headers, db_session
):
    created = client.post(_BASE, json=_payload(), headers=user_token_headers).json()

    resp = client.put(
        f"{_BASE}/config/{created['uuid']}",
        json={
            "provider": "openai",
            "model_name": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
        },
        headers=user_token_headers,
    )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["has_api_key"] is False
    db_session.expire_all()
    assert _stored_column(db_session, created["uuid"]) is None


def test_server_credential_status_is_admin_only(client, user_token_headers, admin_token_headers):
    plain = client.get(f"{_BASE}/bedrock/server-credentials", headers=user_token_headers)
    admin = client.get(f"{_BASE}/bedrock/server-credentials", headers=admin_token_headers)

    assert plain.status_code == status.HTTP_403_FORBIDDEN
    assert admin.status_code == status.HTTP_200_OK
    body = admin.json()
    assert set(body) == {"found", "source", "region"}
    assert isinstance(body["found"], bool)


def test_server_credential_status_reports_the_kind_never_the_values():
    from botocore.credentials import Credentials

    from app.services import bedrock_settings

    creds = Credentials(KEY_ID, SECRET, TOKEN, method="env")
    session = MagicMock()
    session.get_credentials.return_value = creds
    with patch("botocore.session.get_session", return_value=session):
        status_ = bedrock_settings.server_credential_status()

    assert status_ == {"found": True, "source": "environment"}
    _assert_no_secret(json.dumps(status_))


def test_server_credential_status_not_found():
    from app.services import bedrock_settings

    session = MagicMock()
    session.get_credentials.return_value = None
    with patch("botocore.session.get_session", return_value=session):
        assert bedrock_settings.server_credential_status() == {"found": False, "source": None}
