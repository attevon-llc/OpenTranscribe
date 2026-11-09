"""Behaviour pinned by the removal of the ``type: ignore`` comments in api/schemas/main."""

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import schemas
from app.api.endpoints.asr_settings import _apply_provider_update
from app.auth.rate_limit import rate_limit_exceeded_handler
from app.core.validation_errors import validation_error_handler
from app.schemas.user import AuthMethodsResponse


def _request() -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    )


def test_explicit_null_provider_is_a_422_not_an_attribute_error():
    class _Config:
        provider = "openai"
        api_key = "stored"

    update = schemas.UserASRSettingsUpdate.model_validate({"provider": None})
    with pytest.raises(HTTPException) as caught:
        _apply_provider_update(_Config(), update)
    assert caught.value.status_code == 422
    assert _Config.api_key == "stored"


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
