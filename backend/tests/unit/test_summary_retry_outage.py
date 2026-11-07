"""A settings outage skips a summary retry instead of reading as "LLM unavailable"."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ServiceUnavailableError
from app.services.llm_service import LLMService

pytestmark = pytest.mark.unit

LEAK_MARKER = "SELECT secret_column FROM user_llm_settings"


def _ns(**kw: Any) -> Any:
    return SimpleNamespace(**kw)


def _settings_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user-less retry check never reaches ``create_from_user_settings``; fail one level up."""

    def _boom(user_id=None, **kw):
        raise OperationalError(LEAK_MARKER, {}, Exception("connection reset"))

    monkeypatch.setattr(LLMService, "create_from_settings", _boom)


def test_summary_retry_skips_an_outage_without_touching_the_file(monkeypatch):
    _settings_read_fails(monkeypatch)
    from app.tasks import summary_retry

    touched: list[str] = []
    monkeypatch.setattr(
        "app.services.uuid_helpers.get_by_uuid_optional", lambda *a, **k: touched.append("read")
    )
    monkeypatch.setattr(
        summary_retry, "reset_summary_for_retry", lambda *a: touched.append("reset")
    )

    with pytest.raises(ServiceUnavailableError):
        asyncio.run(summary_retry.retry_summary_if_available(_ns(), "u"))

    assert touched == []


def test_check_llm_availability_does_not_swallow_an_outage(monkeypatch):
    _settings_read_fails(monkeypatch)
    from app.tasks.summary_retry import check_llm_availability

    with pytest.raises(OperationalError):
        asyncio.run(check_llm_availability())
