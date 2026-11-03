"""A database error must not read as "no such config" or swap the provider."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from app.services.llm_service import LLMConfigFailure
from app.services.llm_service import LLMConfigurationError
from app.services.llm_service import LLMService


def _db_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


def test_create_from_config_id_raises_on_database_outage_instead_of_returning_none():
    session = MagicMock()
    session.query.side_effect = _db_error()
    with patch("app.db.base.SessionLocal", return_value=session), pytest.raises(OperationalError):
        LLMService.create_from_config_id(user_id=1, config_id=5)
    session.close.assert_called_once()


def test_create_from_config_id_names_an_unusable_stored_config():
    """An unusable stored config raises the fixed-sentence error; it never reads as "none"."""
    row = MagicMock(provider="not-a-provider", api_key=None, base_url=None, max_tokens=4096)
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = row
    with (
        patch("app.db.base.SessionLocal", return_value=session),
        pytest.raises(LLMConfigurationError) as raised,
    ):
        LLMService.create_from_config_id(user_id=1, config_id=5)
    assert raised.value.reason is LLMConfigFailure.UNKNOWN_PROVIDER


def test_create_from_user_settings_does_not_swap_in_the_system_provider_on_outage():
    session = MagicMock()
    with (
        patch("app.db.base.SessionLocal", return_value=session),
        patch.object(LLMService, "_resolve_user_llm_settings", side_effect=_db_error()),
        patch.object(LLMService, "create_from_system_settings") as system,
        pytest.raises(OperationalError),
    ):
        LLMService.create_from_user_settings(user_id=1)
    system.assert_not_called()
    session.close.assert_called_once()
