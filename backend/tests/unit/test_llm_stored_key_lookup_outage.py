"""A failed read of a stored API key is a 503, not a connection test without a key.

Both the model-discovery route and the connection test look the caller's stored key up from
a config id. They used to log the database error and carry on keyless, so an outage was
reported as a provider authentication failure.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import create_autospec

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.exceptions import ServiceUnavailableError
from app.models.user import User
from app.schemas.llm_settings import ConnectionTestRequest
from app.services.llm_settings_outage import LLM_SETTINGS_UNREADABLE_MESSAGE

pytestmark = pytest.mark.unit

LEAK_MARKER = "SELECT secret_column FROM user_llm_settings"


def _unreadable(*_a: Any, **_kw: Any) -> None:
    raise OperationalError(LEAK_MARKER, {}, Exception("connection reset"))


def test_model_discovery_key_lookup_outage_is_a_503(monkeypatch):
    from app.api.endpoints import llm_model_discovery as mod

    monkeypatch.setattr(mod, "find_own_llm_config_by_uuid", _unreadable)

    with pytest.raises(ServiceUnavailableError) as exc:
        mod._get_stored_api_key(create_autospec(Session), str(uuid.uuid4()), 1)

    assert exc.value.status_code == 503
    assert LLM_SETTINGS_UNREADABLE_MESSAGE in str(exc.value)
    assert LEAK_MARKER not in str(exc.value)


def test_model_discovery_without_a_stored_key_still_returns_none(monkeypatch):
    from app.api.endpoints import llm_model_discovery as mod

    monkeypatch.setattr(mod, "find_own_llm_config_by_uuid", lambda *a, **kw: None)

    assert mod._get_stored_api_key(create_autospec(Session), str(uuid.uuid4()), 1) is None


def test_connection_test_key_lookup_outage_is_a_503(monkeypatch):
    from app.api.endpoints import llm_connection_tests as mod

    monkeypatch.setattr(mod, "find_own_llm_config_by_uuid", _unreadable)
    monkeypatch.setattr(mod, "_assert_safe_llm_endpoint", lambda *a, **kw: None)
    request = ConnectionTestRequest(
        provider="openai", model_name="m", base_url="http://x", config_id=uuid.uuid4()
    )

    with pytest.raises(ServiceUnavailableError) as exc:
        mod._run_connection_test(create_autospec(Session), create_autospec(User), request)

    assert LLM_SETTINGS_UNREADABLE_MESSAGE in str(exc.value)
