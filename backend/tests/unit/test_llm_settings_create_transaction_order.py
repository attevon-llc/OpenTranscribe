"""``POST /api/llm-settings`` encrypts before it opens a transaction.

The auth dependency ends its own read transaction, so the handler's first query opens the
request's transaction. Any crypto after that query ran with a pooled connection idle in a
transaction; under the old PBKDF2 KDF that was three derives, about a second. Every
collaborator here is a recorder, so the test reads the order of events, not a timing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.endpoints import llm_settings

CRYPTO = {"test_encryption", "encrypt_api_key", "encrypt_bedrock_fields"}


def _recording_session(events: list[str]) -> MagicMock:
    """A Session stand-in: a call to any public Session method is a DB event."""
    db = MagicMock(spec=Session)
    for name in dir(Session):
        if not name.startswith("_") and callable(getattr(Session, name)):
            getattr(db, name).side_effect = lambda *_a, _n=name, **_kw: events.append(f"db.{_n}")
    return db


@pytest.fixture
def events(monkeypatch) -> list[str]:
    recorded: list[str] = []

    def _recorder(name: str, result: Any = None):
        def _call(*_a: Any, **_kw: Any) -> Any:
            recorded.append(name)
            return result

        return _call

    monkeypatch.setattr(llm_settings, "_assert_safe_llm_endpoint", _recorder("ssrf_check"))
    monkeypatch.setattr(llm_settings, "test_encryption", _recorder("test_encryption", True))
    monkeypatch.setattr(llm_settings, "encrypt_api_key", _recorder("encrypt_api_key", "v4:x"))
    monkeypatch.setattr(
        llm_settings, "_encrypt_bedrock_fields", _recorder("encrypt_bedrock_fields", "v4:b")
    )
    monkeypatch.setattr(
        llm_settings, "find_own_llm_config_by_name", _recorder("db.find_own_llm_config_by_name")
    )
    monkeypatch.setattr(llm_settings, "count_own_llm_configs", _recorder("db.count", 2))
    monkeypatch.setattr(llm_settings, "_set_active_configuration", _recorder("db.set_active"))
    return recorded


def _create(events: list[str], **fields: Any) -> None:
    settings_in = schemas.UserLLMSettingsCreate(
        name="cfg", model_name="m", base_url="https://api.example.com/v1", **fields
    )
    llm_settings.create_user_llm_configuration(
        db=_recording_session(events),
        settings_in=settings_in,
        current_user=models.User(id=7),
    )


def _first_db_event(events: list[str]) -> int:
    return next(i for i, e in enumerate(events) if e.startswith("db."))


@pytest.mark.parametrize(
    ("fields", "crypto_call"),
    [
        pytest.param(
            {"provider": schemas.LLMProvider.OPENAI, "api_key": "sk-test"},
            "encrypt_api_key",
            id="api-key",
        ),
        pytest.param(
            {"provider": schemas.LLMProvider.BEDROCK, "aws_region": "us-east-1"},
            "encrypt_bedrock_fields",
            id="bedrock",
        ),
    ],
)
def test_all_crypto_runs_before_the_first_database_call(events, fields, crypto_call):
    _create(events, **fields)

    first_db = _first_db_event(events)
    crypto_at = [i for i, e in enumerate(events) if e in CRYPTO]
    assert {"test_encryption", crypto_call} <= {events[i] for i in crypto_at}
    assert max(crypto_at) < first_db, events
    assert events[first_db] == "db.find_own_llm_config_by_name"
