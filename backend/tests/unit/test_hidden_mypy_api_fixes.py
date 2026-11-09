"""Behaviour pinned by the removal of the ``type: ignore`` comments in api/schemas/main."""

import asyncio

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app import schemas
from app.auth.rate_limit import rate_limit_exceeded_handler
from app.core.validation_errors import validation_error_handler
from app.schemas.user import AuthMethodsResponse


def _request() -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    )


@pytest.mark.parametrize("field", ["name", "provider", "model_name", "is_active", "is_shared"])
def test_update_schema_rejects_explicit_null_for_not_null_columns(field):
    with pytest.raises(ValidationError) as caught:
        schemas.UserASRSettingsUpdate.model_validate({field: None})
    assert caught.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("field", ["api_key", "base_url", "region", "access_key_id"])
def test_update_schema_still_accepts_null_for_nullable_columns(field):
    assert schemas.UserASRSettingsUpdate.model_validate({field: None}).model_fields_set == {field}


def test_a_verifier_written_against_request_still_works_on_a_websocket():
    """A verifier only reads headers/cookies; annotations are not enforced at runtime, so one
    written against ``Request`` before the ``HTTPConnection`` change behaves the same (its
    parameter is left unannotated here because mypy would rightly reject the old annotation)."""
    from app.auth.provider_registry import ExternalIdentity
    from app.auth.provider_registry import register_verifier
    from app.auth.provider_registry import unregister_verifier
    from app.auth.provider_registry import verify_external_token
    from tests.helpers import fake_websocket

    seen = []

    class _OldStyle:
        def verify(self, token: str, request) -> ExternalIdentity | None:
            seen.append((token, request.headers.get("x-test")))
            return ExternalIdentity(provider="old", external_id="1", email="a@b.example")

    register_verifier("old-style", _OldStyle())
    try:
        assert verify_external_token("tok", fake_websocket()) is not None
    finally:
        unregister_verifier("old-style")
    assert seen == [("tok", None)]


def test_exception_handlers_refuse_an_exception_they_were_not_registered_for():
    with pytest.raises(ValueError, match="stray"):
        rate_limit_exceeded_handler(_request(), ValueError("stray"))
    with pytest.raises(ValueError, match="stray"):
        asyncio.run(validation_error_handler(_request(), ValueError("stray")))


@pytest.mark.parametrize("oidc", [True, False])
def test_deprecated_keycloak_flag_always_mirrors_oidc_flag(oidc):
    body = dict.fromkeys(
        [
            "pki_enabled",
            "ldap_enabled",
            "local_enabled",
            "allow_registration",
            "mfa_enabled",
            "mfa_required",
            "login_banner_enabled",
        ],
        False,
    )
    response = AuthMethodsResponse(
        methods=[],
        oidc_enabled=oidc,
        external_providers=[],
        login_banner_text="",
        login_banner_classification="",
        keycloak_enabled=not oidc,
        **body,
    )
    assert response.model_dump()["keycloak_enabled"] is oidc
