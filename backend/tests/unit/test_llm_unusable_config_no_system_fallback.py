"""A user's ACTIVE-but-unusable LLM configuration must never fall back to the system provider.

Scenario: a user runs a local Ollama with masking off (correct for a local model). Later the
config breaks. Celery paths decided masking for the *user's* provider before resolving it, so
falling through to a system provider that is a cloud API sent the transcript off-machine
unmasked. Three states: no active config -> system fallback; active and usable -> the user's
service; active but unusable -> ``LLMConfigurationError``, never the system provider.
"""

from __future__ import annotations

from unittest.mock import Mock
from unittest.mock import patch

import pytest

from app.services.llm_service import LLMConfigFailure
from app.services.llm_service import LLMConfigurationError
from app.services.llm_service import LLMService
from app.utils.encryption import encrypt_api_key

CLOUD_URL = "https://api.openai.com/v1"
LOCAL_URL = "http://localhost:11434"


def _pointer(value: str | None) -> Mock:
    row = Mock()
    row.setting_value = value
    return row


def _config(**over) -> Mock:
    row = Mock()
    row.provider = "ollama"
    row.model_name = "llama3"
    row.api_key = None
    row.base_url = LOCAL_URL
    row.max_tokens = 4096
    row.temperature = "0.3"
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _db(*rows) -> Mock:
    db = Mock()
    db.query.return_value.filter.return_value.first.side_effect = list(rows)
    return db


@pytest.fixture
def cloud_system_provider(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-system-cloud-key")
    monkeypatch.setattr(settings, "OPENAI_MODEL_NAME", "gpt-system")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", CLOUD_URL, raising=False)


BROKEN = {
    "non_integer_pointer": ((_pointer("not-a-number"),), LLMConfigFailure.BAD_POINTER),
    "dangling_pointer": ((_pointer("7"), None), LLMConfigFailure.NOT_FOUND),
    "unknown_provider": (
        (_pointer("7"), _config(provider="nonesuch")),
        LLMConfigFailure.UNKNOWN_PROVIDER,
    ),
    "undecryptable_key": (
        (_pointer("7"), _config(provider="openai", api_key="not-a-valid-ciphertext")),
        LLMConfigFailure.CREDENTIALS,
    ),
    "bedrock_no_region": (
        (_pointer("7"), _config(provider="bedrock", base_url=None, api_key=None)),
        LLMConfigFailure.BEDROCK_REGION,
    ),
    "vllm_null_base_url": ((_pointer("7"), _config(provider="vllm", base_url=None)), None),
    "custom_null_base_url": ((_pointer("7"), _config(provider="custom", base_url=None)), None),
}


def _case(name):
    rows, reason = BROKEN[name]
    return rows, reason


@pytest.mark.parametrize("name", list(BROKEN))
class TestUnusableActiveConfig:
    def _patched(self, name, monkeypatch):
        monkeypatch.setattr("app.services.bedrock_settings.resolve_region", lambda _b: None)
        monkeypatch.setattr("app.services.llm_service.resolve_region", lambda _b: None)
        rows, reason = _case(name)
        return rows, reason

    def test_create_from_user_settings_refuses(self, name, cloud_system_provider, monkeypatch):
        rows, reason = self._patched(name, monkeypatch)
        with patch("app.db.base.SessionLocal", return_value=_db(*rows)):
            with pytest.raises(LLMConfigurationError) as caught:
                LLMService.create_from_user_settings(user_id=1)
        if reason is not None:
            assert caught.value.reason == reason
        # Fixed sentence only: no raw exception text, no key material.
        assert "sk-system" not in str(caught.value)

    def test_create_from_settings_returns_none_not_system(
        self, name, cloud_system_provider, monkeypatch
    ):
        rows, _ = self._patched(name, monkeypatch)
        with patch("app.db.base.SessionLocal", return_value=_db(*rows)):
            assert LLMService.create_from_settings(user_id=1) is None

    def test_describe_and_is_configured_do_not_describe_system(
        self, name, cloud_system_provider, monkeypatch
    ):
        rows, _ = self._patched(name, monkeypatch)
        assert LLMService.describe_for_user(_db(*rows), 1) is None
        assert LLMService.is_configured_for_user(_db(*rows), 1) is False


def test_create_from_config_id_refuses_unusable(cloud_system_provider):
    with patch(
        "app.db.base.SessionLocal", return_value=_db(_config(provider="vllm", base_url=None))
    ):
        with pytest.raises(LLMConfigurationError):
            LLMService.create_from_config_id(user_id=1, config_id=7)
    with patch("app.db.base.SessionLocal", return_value=_db(None)):
        with pytest.raises(LLMConfigurationError):
            LLMService.create_from_config_id(user_id=1, config_id=7)


def test_unexpected_failure_after_resolving_does_not_fall_back(cloud_system_provider):
    """A failure after the user config resolved (here: building the service) is not a fallback."""
    db = _db(_pointer("7"), _config())
    with (
        patch("app.db.base.SessionLocal", return_value=db),
        patch("app.services.llm_service.LLMService.__init__", side_effect=TypeError("boom")),
        pytest.raises(LLMConfigurationError),
    ):
        LLMService.create_from_user_settings(user_id=1)


def test_db_errors_propagate_not_swallowed(cloud_system_provider):
    db = Mock()
    db.query.side_effect = RuntimeError("db down")
    with patch("app.db.base.SessionLocal", return_value=db), pytest.raises(RuntimeError):
        LLMService.create_from_user_settings(user_id=1)


class TestControls:
    def test_no_config_still_gets_system_provider(self, cloud_system_provider):
        for rows in ((None,), (_pointer(""),)):
            with patch("app.db.base.SessionLocal", return_value=_db(*rows)):
                service = LLMService.create_from_user_settings(user_id=1)
            assert service is not None
            assert service.config.provider.value == "openai"
            assert service.config.model == "gpt-system"
            service.close()
        described = LLMService.describe_for_user(_db(None), 1)
        assert described is not None
        assert described[0] == "openai"

    def test_working_config_uses_users_provider(self, cloud_system_provider):
        db = _db(_pointer("7"), _config())
        with patch("app.db.base.SessionLocal", return_value=db):
            service = LLMService.create_from_user_settings(user_id=1)
        assert service is not None
        assert service.config.provider.value == "ollama"
        assert service.config.base_url == LOCAL_URL
        service.close()
        assert LLMService.describe_for_user(_db(_pointer("7"), _config()), 1) == (
            "ollama",
            "llama3",
            LOCAL_URL,
        )

    def test_working_openai_key_roundtrip(self, cloud_system_provider):
        db = _db(
            _pointer("7"),
            _config(provider="openai", api_key=encrypt_api_key("sk-user"), base_url=CLOUD_URL),
        )
        with patch("app.db.base.SessionLocal", return_value=db):
            service = LLMService.create_from_user_settings(user_id=1)
        assert service is not None
        assert service.config.api_key == "sk-user"
        service.close()


class TestTaskPathsSendNothing:
    def test_summary_task_does_not_touch_system_provider_or_send_text(self, cloud_system_provider):
        from app.tasks import summarization

        inputs = {
            "user_id": 1,
            "file_id": 5,
            "full_transcript": "SECRET transcript text",
            "speaker_stats": {},
            "output_language": "English",
            "organization_context": None,
        }
        with (
            patch("app.db.base.SessionLocal", return_value=_db(_pointer("7"), None)),
            patch.object(LLMService, "create_from_system_settings") as system,
            patch.object(LLMService, "generate_summary") as generate,
            patch.object(LLMService, "_send_llm_request") as send,
        ):
            assert summarization._generate_llm_summary(inputs) is None
        system.assert_not_called()
        generate.assert_not_called()
        send.assert_not_called()

    def test_summary_availability_probe_is_false(self, cloud_system_provider):
        from app.tasks import summarization

        probe = summarization._llm_is_configured
        with (
            patch("app.db.base.SessionLocal", return_value=_db(_pointer("7"), None)),
            patch.object(LLMService, "create_from_system_settings") as system,
        ):
            assert probe(1) is False
        system.assert_not_called()

    def test_speaker_task_helper_skips(self, cloud_system_provider):
        from app.tasks import speaker_identification_task as sit

        with (
            patch("app.db.base.SessionLocal", return_value=_db(_pointer("7"), None)),
            patch.object(LLMService, "create_from_system_settings") as system,
        ):
            assert sit._create_llm_service(1) is None
        system.assert_not_called()
