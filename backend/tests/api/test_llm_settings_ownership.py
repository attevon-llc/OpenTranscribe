"""Ownership boundary for the LLM capability-probe routes (issue audit B10).

``GET``/``POST /api/llm-settings/config/{config_uuid}/context-window[-probe]`` and
``.../reasoning[-probe]`` were added by #533 alongside 30 service-layer tests in
``tests/unit/test_llm_context_window.py`` — but that file has no HTTP client
infrastructure and never drives the routes themselves, so the question "can a
different authenticated user read or trigger a probe against someone else's
config" had no test.

Read ``app/api/endpoints/llm_settings.py``: all four handlers resolve
``config_uuid`` via ``get_llm_config_by_uuid`` — an UNSCOPED lookup with no
owner filter — and only afterward call ``require_resource_owner`` (unless the
config is ``is_shared``). That gate raises **403**, not 404, exactly like the
sibling ``GET .../api-key`` route pinned in ``test_llm_settings_endpoints.py``.
So a non-owner probing another user's config gets a 403 "Not authorized to
access this configuration" — which does confirm the config's *existence* to a
caller who is not its owner (a UUID that belongs to nobody would 404 instead),
but this is the same posture the rest of this router already ships, not a new
gap. Pinned here so a refactor cannot silently turn it into 404 or, worse, 200.
"""

from __future__ import annotations

from fastapi import status

_BASE = "/api/llm-settings"


def _create_payload(name: str = "Ownership Probe Cfg") -> dict[str, object]:
    return {
        "name": name,
        "provider": "openai",
        "model_name": "gpt-4o-mini",
        "api_key": "sk-fixture-key-123",  # gitleaks:allow - fake fixture key
        "base_url": "https://api.openai.com/v1",
        "max_tokens": 2000,
        "temperature": "0.3",
    }


def _create_config(client, headers, name: str = "Ownership Probe Cfg") -> str:
    created = client.post(_BASE, json=_create_payload(name), headers=headers)
    assert created.status_code == status.HTTP_200_OK, created.text
    return str(created.json()["uuid"])


def test_context_window_get_by_non_owner_is_403(
    client, user_token_headers, other_user_auth_headers
):
    """A non-owner reading user A's recorded context-window verdict is refused.

    Confirmed 403 (matching ``require_resource_owner``'s existing contract on
    this router), not the 404 an anti-enumeration design would prefer and not a
    200 that would be an ownership-bypass bug.
    """
    config_uuid = _create_config(client, user_token_headers)

    response = client.get(
        f"{_BASE}/config/{config_uuid}/context-window", headers=other_user_auth_headers
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not authorized to access this configuration"


def test_context_window_probe_by_non_owner_is_403(
    client, user_token_headers, other_user_auth_headers
):
    """The mutating probe route refuses a non-owner before it ever dials the
    provider — the ownership check runs first, so this needs no network mock."""
    config_uuid = _create_config(client, user_token_headers, name="Ownership Probe Cfg 2")

    response = client.post(
        f"{_BASE}/config/{config_uuid}/context-window-probe", headers=other_user_auth_headers
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not authorized to access this configuration"


def test_reasoning_get_by_non_owner_is_403(client, user_token_headers, other_user_auth_headers):
    config_uuid = _create_config(client, user_token_headers, name="Ownership Probe Cfg 3")

    response = client.get(
        f"{_BASE}/config/{config_uuid}/reasoning", headers=other_user_auth_headers
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not authorized to access this configuration"


def test_reasoning_probe_by_non_owner_is_403(client, user_token_headers, other_user_auth_headers):
    config_uuid = _create_config(client, user_token_headers, name="Ownership Probe Cfg 4")

    response = client.post(
        f"{_BASE}/config/{config_uuid}/reasoning-probe", headers=other_user_auth_headers
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not authorized to access this configuration"
