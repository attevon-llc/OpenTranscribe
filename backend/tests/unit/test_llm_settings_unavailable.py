"""A settings-database outage is reported as such, never as "configure a provider".

``is_llm_available`` used to turn any exception into ``False``, so llm_status, summarization
and summary_status told the user to configure a provider that was in fact configured.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ServiceUnavailableError
from app.services.llm_service import LLMService
from app.services.llm_service import is_llm_available
from app.services.llm_settings_outage import LLM_SETTINGS_UNREADABLE_MESSAGE

pytestmark = pytest.mark.unit


def _ns(**kw: Any) -> Any:
    """A stand-in typed ``Any``: the handlers read only a few attributes."""
    return SimpleNamespace(**kw)


LEAK_MARKER = "SELECT secret_column FROM user_llm_settings"


@pytest.fixture
def unreadable_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(user_id):
        raise OperationalError(LEAK_MARKER, {}, Exception("connection reset"))

    monkeypatch.setattr(LLMService, "create_from_user_settings", _boom)


@pytest.fixture
def not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LLMService, "create_from_user_settings", lambda user_id: None)


def test_is_llm_available_raises_on_a_settings_outage(unreadable_settings):
    with pytest.raises(OperationalError):
        asyncio.run(is_llm_available(user_id=1))


def test_is_llm_available_is_false_when_nothing_is_configured(not_configured):
    assert asyncio.run(is_llm_available(user_id=1)) is False


def test_llm_status_reports_settings_unavailable(unreadable_settings):
    from app.api.endpoints.llm_status import get_llm_status

    result = asyncio.run(get_llm_status(current_user=_ns(id=1), db=_ns()))

    assert result["available"] is False
    assert result["status"] == "settings_unavailable"
    assert result["message"] == LLM_SETTINGS_UNREADABLE_MESSAGE
    assert "configure" not in result["message"].lower()
    assert LEAK_MARKER not in str(result)


def test_llm_status_not_configured_keeps_the_configure_message(not_configured):
    from app.api.endpoints.llm_status import get_llm_status

    result = asyncio.run(get_llm_status(current_user=_ns(id=1), db=_ns()))

    assert "status" not in result
    assert "configure" in result["message"].lower()


def test_summary_status_flags_the_outage_instead_of_no_provider(unreadable_settings, monkeypatch):
    from app.api.endpoints.files import summary_status as mod

    media_file = _ns(
        id=1,
        uuid="u",
        summary_status="pending",
        summary_data=None,
        summary_opensearch_id=None,
        status="completed",
        filename="f.wav",
    )
    monkeypatch.setattr(
        "app.services.uuid_helpers.get_file_by_uuid_with_permission", lambda *a, **k: media_file
    )
    monkeypatch.setattr("app.utils.summary_settings.is_summary_enabled_system", lambda db: True)
    monkeypatch.setattr(
        "app.utils.summary_settings.is_summary_enabled_for_user", lambda db, uid: True
    )
    ctx = _ns(bypass=_ns(user_is_admin=False), org_id=None)

    result = asyncio.run(mod.get_summary_status("u", db=_ns(), current_user=_ns(id=1), ctx=ctx))

    assert result["llm_available"] is False
    assert result["llm_settings_unavailable"] is True


def test_retry_summary_returns_503_with_the_settings_sentence(unreadable_settings, monkeypatch):
    from app.api.endpoints.files import summary_status as mod

    media_file = _ns(id=1, uuid="u", summary_status="failed", status="completed")
    monkeypatch.setattr(
        "app.services.uuid_helpers.get_file_by_uuid_with_permission", lambda *a, **k: media_file
    )
    monkeypatch.setattr(mod, "require_processing_allowed_for_file", lambda db, f: None)
    # The legal-hold check needs a real database; this file is not held.
    hold_checks: list[str] = []
    monkeypatch.setattr(
        "app.api.hold_refusal.refuse_if_frozen",
        lambda db, file_id, *, actor, action: hold_checks.append(action),
    )
    ctx = _ns(bypass=_ns(user_is_admin=False), org_id=None)

    with pytest.raises(ServiceUnavailableError) as exc:
        asyncio.run(mod.retry_summary("u", db=_ns(), current_user=_ns(id=1), ctx=ctx))

    assert exc.value.status_code == 503
    assert LLM_SETTINGS_UNREADABLE_MESSAGE in str(exc.value)
    assert hold_checks == ["summary_generate"]


def test_speaker_id_trigger_returns_503_on_an_outage(unreadable_settings, monkeypatch):
    from app.api.endpoints import summarization as mod

    media_file = _ns(id=1, uuid="u", speakers=[_ns()])
    monkeypatch.setattr(mod, "get_file_by_uuid_with_permission", lambda *a, **k: media_file)
    monkeypatch.setattr(mod, "require_processing_allowed_for_file", lambda db, f: None)
    started: list[str] = []
    monkeypatch.setattr(mod.identify_speakers_llm_task, "delay", lambda **k: started.append("x"))
    # The legal-hold check needs a real database; this file is not held.
    hold_checks: list[str] = []
    monkeypatch.setattr(
        mod, "refuse_if_frozen", lambda db, file_id, *, actor, action: hold_checks.append(action)
    )
    ctx = _ns(bypass=_ns(user_is_admin=False), org_id=None)

    with pytest.raises(ServiceUnavailableError) as exc:
        asyncio.run(mod.identify_speakers("u", current_user=_ns(id=1), ctx=ctx, db=_ns()))

    assert LLM_SETTINGS_UNREADABLE_MESSAGE in str(exc.value)
    assert started == []
    assert hold_checks == ["identify_speakers"]
