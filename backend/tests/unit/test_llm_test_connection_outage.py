"""``/test-connection`` reports a settings outage, not "no provider configured"."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ServiceUnavailableError
from app.services.llm_service import LLMService
from app.services.llm_settings_outage import LLM_SETTINGS_UNREADABLE_MESSAGE

pytestmark = pytest.mark.unit

LEAK_MARKER = "SELECT secret_column FROM user_llm_settings"


def _ns(**kw: Any) -> Any:
    return SimpleNamespace(**kw)


@pytest.fixture
def unreadable_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(user_id):
        raise OperationalError(LEAK_MARKER, {}, Exception("connection reset"))

    monkeypatch.setattr(LLMService, "create_from_user_settings", _boom)


@pytest.fixture
def not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LLMService, "create_from_user_settings", lambda user_id: None)


def test_test_connection_reports_the_outage_not_no_provider(unreadable_settings):
    from app.api.endpoints.llm_status import test_llm_connection

    with pytest.raises(ServiceUnavailableError) as exc:
        test_llm_connection(current_user=_ns(id=1))

    assert exc.value.status_code == 503
    assert LLM_SETTINGS_UNREADABLE_MESSAGE in str(exc.value)


def test_test_connection_with_nothing_configured_keeps_its_message(not_configured):
    from app.api.endpoints.llm_status import test_llm_connection

    result = test_llm_connection(current_user=_ns(id=1))

    assert result["message"] == "No LLM service configured"
