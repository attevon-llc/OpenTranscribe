"""Characterization of the policy-layer refusals (L2 group G-E).

These pin status, detail (dict details included), headers and ``str(exc)`` of the refusals
raised below the API layer, plus the exact HTTP bytes behind a real route. They are written
to pass both while the sites raise ``HTTPException`` and after they raise ``DomainError``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.exceptions import ConflictError
from app.core.exceptions import DomainError
from app.core.exceptions import InvalidInputError
from app.core.exceptions import ResourceNotFoundError
from app.core.exceptions import UnprocessableInputError
from app.main import handle_domain_error

pytestmark = pytest.mark.xdist_group("l2_ge_policy_errors")

REFUSAL = (HTTPException, DomainError)


def _app_with(
    route_fn: Any, path: str = "/probe", dependencies: list[Any] | None = None
) -> TestClient:
    probe = FastAPI()
    probe.add_exception_handler(DomainError, handle_domain_error)  # type: ignore[arg-type]
    probe.add_api_route(path, route_fn, methods=["GET"], dependencies=dependencies)
    return TestClient(probe, raise_server_exceptions=False)


def _pin(exc: BaseException, status: int, detail: Any) -> None:
    assert exc.status_code == status  # type: ignore[attr-defined]
    assert exc.detail == detail  # type: ignore[attr-defined]
    assert getattr(exc, "headers", None) is None
    assert str(exc) == f"{status}: {detail}"


# ── account_security_service ────────────────────────────────────────────────────


def test_enforce_password_policy_refusal_is_400_with_joined_errors():
    from app.auth.password_policy import validate_password
    from app.services.account_security_service import enforce_password_policy

    user = SimpleNamespace(email="alice@example.com", full_name="Alice Smith")
    expected = "; ".join(
        validate_password("a", email="alice@example.com", full_name="Alice Smith").errors
    )
    assert expected  # the weak password is genuinely refused
    with pytest.raises(REFUSAL) as exc:
        enforce_password_policy("a", user)  # type: ignore[arg-type]
    _pin(exc.value, 400, expected)


def test_password_endpoint_body_is_400_detail():
    from app.services.account_security_service import enforce_password_policy

    def route() -> dict:
        enforce_password_policy("a", SimpleNamespace(email="a@example.com", full_name="A"))  # type: ignore[arg-type]
        return {}

    res = _app_with(route).get("/probe")
    assert res.status_code == 400
    assert list(res.json()) == ["detail"]
    assert isinstance(res.json()["detail"], str) and res.json()["detail"]


def test_assert_password_auth_possible_refuses_ldap_with_reason():
    from app.auth.utils import local_password_allowed
    from app.services.account_security_service import assert_password_auth_possible

    user = SimpleNamespace(auth_type="ldap", allow_local_fallback=False)
    _, reason = local_password_allowed("ldap", False)
    with pytest.raises(REFUSAL) as exc:
        assert_password_auth_possible(user)  # type: ignore[arg-type]
    _pin(exc.value, 400, f"Cannot set a password for this account: {reason}")


def test_assert_local_fallback_settable_refuses_local():
    from app.services.account_security_service import assert_local_fallback_settable

    detail = (
        "allow_local_fallback only applies to accounts whose identity is owned "
        "by PKI or an OIDC provider."
    )
    with pytest.raises(REFUSAL) as exc:
        assert_local_fallback_settable("local")
    _pin(exc.value, 400, detail)

    def route() -> dict:
        assert_local_fallback_settable("local")
        return {}

    res = _app_with(route).get("/probe")
    assert (res.status_code, res.json()) == (400, {"detail": detail})


# ── asr_provider_policy ─────────────────────────────────────────────────────────


def test_refuse_config_change_409_dict_details_and_endpoint_body(db_session):
    from app.services import asr_provider_policy as policy
    from app.services.pipeline_plan import ISSUE_MESSAGES

    policy.save_policy(db_session, ["gladia"], own_keys=False)
    user = SimpleNamespace(role="user")

    not_allowed = {
        "code": "asr_provider_not_allowed",
        "message": ISSUE_MESSAGES["asr_provider_not_allowed"],
    }
    with pytest.raises(REFUSAL) as exc:
        policy.refuse_config_change(db_session, user, "deepgram", carries_credential=False)
    _pin(exc.value, 409, not_allowed)

    own_keys = {"code": "own_asr_keys_disabled", "message": policy.OWN_KEYS_DISABLED_MESSAGE}
    with pytest.raises(REFUSAL) as exc:
        policy.refuse_config_change(db_session, user, "gladia", carries_credential=True)
    _pin(exc.value, 409, own_keys)

    def route(provider: str, key: bool) -> dict:
        policy.refuse_config_change(db_session, user, provider, carries_credential=key)
        return {}

    client = _app_with(route)
    res = client.get("/probe", params={"provider": "deepgram", "key": "false"})
    assert (res.status_code, res.json()) == (409, {"detail": not_allowed})
    res = client.get("/probe", params={"provider": "gladia", "key": "true"})
    assert (res.status_code, res.json()) == (409, {"detail": own_keys})


# ── lite_mode ───────────────────────────────────────────────────────────────────


def test_plan_refusal_value_is_a_409_refusal_with_dict_detail():
    from app.services.pipeline_plan import ISSUE_MESSAGES
    from app.utils.lite_mode import plan_refusal

    err = plan_refusal("local_asr_unavailable")
    assert isinstance(err, REFUSAL)
    _pin(
        err,
        409,
        {"code": "local_asr_unavailable", "message": ISSUE_MESSAGES["local_asr_unavailable"]},
    )


def test_refuse_on_lite_raises_only_on_lite_and_endpoint_body(monkeypatch):
    from app.core.config import settings
    from app.services.pipeline_plan import ISSUE_MESSAGES
    from app.utils.lite_mode import refuse_on_lite

    expected = {
        "code": "local_asr_unavailable",
        "message": ISSUE_MESSAGES["local_asr_unavailable"],
    }
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "full")
    refuse_on_lite("local_asr_unavailable")
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "lite")
    with pytest.raises(REFUSAL) as exc:
        refuse_on_lite("local_asr_unavailable")
    _pin(exc.value, 409, expected)

    def route() -> dict:
        refuse_on_lite("local_asr_unavailable")
        return {}

    res = _app_with(route).get("/probe")
    assert (res.status_code, res.json()) == (409, {"detail": expected})


# ── whisper_model_choice ────────────────────────────────────────────────────────


def test_unservable_whisper_model_is_422(monkeypatch):
    from app.core.config import settings
    from app.services.asr import model_discovery
    from app.transcription.config import LIGHTWEIGHT_MODELS
    from app.utils import whisper_model_choice as wmc

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "full")
    monkeypatch.setattr(wmc, "deployment_model_name", lambda: "large-v3")
    monkeypatch.setattr(model_discovery, "resolve_loadable_model_name", lambda name: name)
    detail = (
        "Whisper model 'medium' is not available for per-file selection. This "
        "deployment runs 'large-v3' on the GPU; the only other choices are the "
        f"lightweight CPU models ({', '.join(sorted(LIGHTWEIGHT_MODELS))}). Omit "
        "whisper_model to use the deployment model."
    )
    with pytest.raises(REFUSAL) as exc:
        wmc.require_servable_whisper_model("medium")
    _pin(exc.value, 422, detail)

    def route() -> dict:
        wmc.require_servable_whisper_model("medium")
        return {}

    res = _app_with(route).get("/probe")
    assert (res.status_code, res.json()) == (422, {"detail": detail})


# ── core.capabilities (a FastAPI dependency) ────────────────────────────────────


def test_require_capability_dependency_404_body_headers_and_str():
    from app.core.capabilities import require_capability

    dep = require_capability("billing")  # off in community
    with pytest.raises(REFUSAL) as exc:
        dep(SimpleNamespace(state=SimpleNamespace()))  # type: ignore[arg-type]
    _pin(exc.value, 404, "Not Found")

    def route() -> dict:
        return {"ok": True}

    client = _app_with(route, dependencies=[Depends(dep)])
    res = client.get("/probe")
    assert res.status_code == 404
    assert res.json() == {"detail": "Not Found"}
    assert res.content == b'{"detail":"Not Found"}'
    assert "www-authenticate" not in res.headers

    on = _app_with(route, dependencies=[Depends(require_capability("watch_sources"))])
    assert on.get("/probe").json() == {"ok": True}


def test_domain_classes_for_this_group_exist_with_expected_status():
    assert ConflictError.status_code == 409
    assert InvalidInputError.status_code == 400
    assert UnprocessableInputError.status_code == 422
    assert ResourceNotFoundError.status_code == 404
