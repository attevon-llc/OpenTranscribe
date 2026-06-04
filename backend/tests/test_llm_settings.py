"""Tests for user LLM settings: encryption, model, API endpoints, schemas, service.

Encryption tests always run. The remaining classes are gated behind
RUN_LLM_TESTS=true (they exercise the multi-configuration LLM settings API
against the test database — no external LLM calls are made).
"""

import os
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from app import models
from app import schemas
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

# Alias so pytest doesn't collect the imported utility as a test function
from app.utils.encryption import test_encryption as encryption_self_test

_llm_gate = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS", "false").lower() != "true",
    reason="LLM settings tests are opt-in (set RUN_LLM_TESTS=true to run)",
)


class TestEncryption:
    """Test encryption utilities"""

    def test_encryption_basic_functionality(self):
        """Test basic encryption/decryption"""
        test_key = "sk-test123456789abcdef"

        # Encrypt
        encrypted = encrypt_api_key(test_key)
        assert encrypted is not None
        assert encrypted != test_key

        # Decrypt
        decrypted = decrypt_api_key(encrypted)
        assert decrypted == test_key

    def test_encryption_empty_key(self):
        """Test encryption with empty/None keys"""
        assert encrypt_api_key(None) is None  # type: ignore[arg-type]
        assert encrypt_api_key("") is None
        assert encrypt_api_key("   ") is None

        assert decrypt_api_key(None) is None  # type: ignore[call-overload]
        assert decrypt_api_key("") is None
        assert decrypt_api_key("   ") is None

    def test_encryption_system_test(self):
        """Test encryption system validation"""
        assert encryption_self_test() is True


def _create_config_payload(name: str = "Test Config", **overrides) -> dict:
    """Valid payload for POST /api/llm-settings."""
    payload = {
        "name": name,
        "provider": "openai",
        "model_name": "gpt-4o-mini",
        "api_key": "sk-test123456789",  # gitleaks:allow — fake fixture key
        "base_url": "https://api.openai.com/v1",
        "max_tokens": 2000,
        "temperature": "0.3",
        "is_active": True,
    }
    payload.update(overrides)
    return payload


@_llm_gate
class TestLLMSettingsModel:
    """Test UserLLMSettings model"""

    def test_user_llm_settings_creation(self, db_session, normal_user):
        """Creating and persisting a UserLLMSettings row works end-to-end"""
        config = models.UserLLMSettings(
            user_id=normal_user.id,
            name="Model Test Config",
            provider="openai",
            model_name="gpt-4o-mini",
            api_key=encrypt_api_key("sk-model-test"),
            base_url="https://api.openai.com/v1",
            max_tokens=2000,
            temperature="0.3",
            is_active=True,
            test_status="untested",
        )
        db_session.add(config)
        db_session.commit()
        db_session.refresh(config)

        assert config.user_id == normal_user.id
        assert config.provider == "openai"
        assert config.model_name == "gpt-4o-mini"
        assert config.is_active is True
        assert config.has_api_key is True
        assert config.uuid is not None


@_llm_gate
class TestLLMSettingsAPI:
    """Test LLM settings API endpoints against the real app + test database"""

    def test_get_providers(self, client, user_token_headers):
        """Supported providers are listed with their defaults"""
        response = client.get("/api/llm-settings/providers", headers=user_token_headers)

        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        assert len(data["providers"]) > 0

        provider = data["providers"][0]
        assert "provider" in provider
        assert "default_model" in provider
        assert "requires_api_key" in provider
        assert "supports_custom_url" in provider
        assert "description" in provider

    def test_get_status_no_settings(self, client, user_token_headers):
        """A fresh user has no settings and uses the system default"""
        response = client.get("/api/llm-settings/status", headers=user_token_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["has_settings"] is False
        assert data["using_system_default"] is True
        assert data["total_configurations"] == 0

    def test_list_configurations_empty(self, client, user_token_headers):
        """A fresh user has an empty configurations list"""
        response = client.get("/api/llm-settings", headers=user_token_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["configurations"] == []
        assert data["total"] == 0
        assert data["active_configuration_id"] is None

    def test_create_configuration(self, client, user_token_headers):
        """Creating a configuration stores it, encrypts the key, and activates it"""
        response = client.post(
            "/api/llm-settings",
            headers=user_token_headers,
            json=_create_config_payload(),
        )

        assert response.status_code == 200, response.json()
        data = response.json()
        assert data["name"] == "Test Config"
        assert data["provider"] == "openai"
        assert data["model_name"] == "gpt-4o-mini"
        assert data["has_api_key"] is True
        # Raw API key must never be returned
        assert "sk-test123456789" not in response.text

        # First configuration becomes the active one
        status_response = client.get("/api/llm-settings/status", headers=user_token_headers)
        status = status_response.json()
        assert status["has_settings"] is True
        assert status["total_configurations"] == 1

    def test_create_duplicate_name_rejected(self, client, user_token_headers):
        """Creating two configurations with the same name is rejected"""
        first = client.post(
            "/api/llm-settings",
            headers=user_token_headers,
            json=_create_config_payload(name="Dup Config"),
        )
        assert first.status_code == 200

        duplicate = client.post(
            "/api/llm-settings",
            headers=user_token_headers,
            json=_create_config_payload(name="Dup Config"),
        )
        assert duplicate.status_code == 400
        assert "already exists" in duplicate.json()["detail"]

    def test_test_connection_invalid_provider(self, client, user_token_headers):
        """Unknown providers are rejected by schema validation"""
        response = client.post(
            "/api/llm-settings/test",
            headers=user_token_headers,
            json={"provider": "invalid_provider", "model_name": "test-model"},
        )
        assert response.status_code == 422

    def test_endpoints_require_auth(self, client):
        """All LLM settings endpoints require authentication"""
        assert client.get("/api/llm-settings").status_code == 401
        assert client.get("/api/llm-settings/status").status_code == 401
        assert client.post("/api/llm-settings", json=_create_config_payload()).status_code == 401


@_llm_gate
class TestLLMSettingsSchemas:
    """Test LLM settings Pydantic schemas"""

    def test_llm_settings_create_validation(self):
        """Test creation schema validation"""
        settings_obj = schemas.UserLLMSettingsCreate(
            name="Schema Test",
            provider="openai",
            model_name="gpt-4o-mini",
            max_tokens=2000,
            temperature="0.3",
        )
        assert settings_obj.provider == "openai"
        assert settings_obj.max_tokens == 2000

    def test_llm_settings_validation_errors(self):
        """Test schema validation errors"""
        # Invalid max_tokens (below the 512 floor)
        with pytest.raises(ValueError, match="max_tokens must be between"):
            schemas.UserLLMSettingsCreate(
                name="Bad Tokens",
                provider="openai",
                model_name="gpt-4",
                max_tokens=0,
            )

        # Out-of-range temperature
        with pytest.raises(ValueError, match="temperature must be between"):
            schemas.UserLLMSettingsCreate(
                name="Bad Temp",
                provider="openai",
                model_name="gpt-4",
                temperature="3.0",
            )

        # Non-numeric temperature
        with pytest.raises(ValueError, match="temperature must be a valid number"):
            schemas.UserLLMSettingsCreate(
                name="NaN Temp",
                provider="openai",
                model_name="gpt-4",
                temperature="warm",
            )

        # Missing required name
        with pytest.raises(ValueError):
            schemas.UserLLMSettingsCreate(provider="openai", model_name="gpt-4")

    def test_connection_test_request(self):
        """Test connection test request schema"""
        request = schemas.ConnectionTestRequest(
            provider="vllm",
            model_name="llama2:7b",
            base_url="http://localhost:8012/v1",
        )

        assert request.provider == "vllm"
        assert request.base_url == "http://localhost:8012/v1"
        assert request.config_id is None

    def test_provider_defaults(self):
        """Test provider defaults schema"""
        defaults = schemas.ProviderDefaults(
            provider="openai",
            default_model="gpt-4o-mini",
            default_base_url="https://api.openai.com/v1",
            requires_api_key=True,
            supports_custom_url=True,
            max_context_length=128000,
            description="OpenAI's GPT models",
        )

        assert defaults.provider == "openai"
        assert defaults.requires_api_key is True
        assert defaults.max_context_length == 128000


@_llm_gate
class TestLLMServiceIntegration:
    """Test LLMService factory methods (mocked DB session, no external calls)"""

    def test_create_from_user_settings_not_found(self):
        """No active configuration and no system provider -> service is None"""
        with patch("app.db.base.SessionLocal") as mock_session_local:
            mock_db = Mock()
            # No active_llm_config_id UserSetting row
            mock_db.query.return_value.filter.return_value.first.return_value = None
            mock_session_local.return_value = mock_db

            from app.services.llm_service import LLMService

            service = LLMService.create_from_user_settings(user_id=999)
            # Falls through to system settings; LLM_PROVIDER is unset in tests
            assert service is None

    def test_create_from_user_settings_success(self):
        """An active configuration row produces a configured service"""
        active_setting = Mock()
        active_setting.setting_value = "1"

        mock_config = Mock()
        mock_config.provider = "openai"
        mock_config.model_name = "gpt-4o-mini"
        mock_config.api_key = encrypt_api_key("sk-test123456789")
        mock_config.base_url = "https://api.openai.com/v1"
        mock_config.max_tokens = 2000
        mock_config.temperature = "0.3"
        mock_config.is_active = True

        with patch("app.db.base.SessionLocal") as mock_session_local:
            mock_db = Mock()
            # First query: UserSetting active config id; second: the config itself
            mock_db.query.return_value.filter.return_value.first.side_effect = [
                active_setting,
                mock_config,
            ]
            mock_session_local.return_value = mock_db

            from app.services.llm_service import LLMService

            service = LLMService.create_from_user_settings(user_id=1)

            assert service is not None
            assert service.config.provider.value == "openai"
            assert service.config.model == "gpt-4o-mini"
            assert service.config.max_tokens == 2000

    def test_create_from_settings_with_fallback(self):
        """create_from_settings tries user settings and returns None without them"""
        with patch(
            "app.services.llm_service.LLMService.create_from_user_settings"
        ) as mock_user_settings:
            mock_user_settings.return_value = None

            from app.services.llm_service import LLMService

            service = LLMService.create_from_settings(user_id=1)

            mock_user_settings.assert_called_once_with(1)
            # No system fallback inside create_from_settings — explicit config required
            assert service is None


@_llm_gate
class TestLLMSettingsIntegration:
    """Integration tests for LLM settings"""

    def test_encryption_integration(self):
        """Test that encryption works end-to-end"""
        test_keys = [
            "sk-1234567890abcdef",
            "api_key_123",
            "Bearer token123",
            "very-long-api-key-with-special-chars!@#$%^&*()",
        ]

        for key in test_keys:
            encrypted = encrypt_api_key(key)
            assert encrypted is not None
            assert encrypted != key

            decrypted = decrypt_api_key(encrypted)
            assert decrypted == key

    def test_full_configuration_flow(self, client, user_token_headers):
        """Create -> list -> status flow against the real API"""
        create = client.post(
            "/api/llm-settings",
            headers=user_token_headers,
            json=_create_config_payload(name="Flow Config", max_tokens=4000),
        )
        assert create.status_code == 200, create.json()
        config_uuid = create.json()["uuid"]

        listing = client.get("/api/llm-settings", headers=user_token_headers)
        assert listing.status_code == 200
        data = listing.json()
        assert data["total"] == 1
        assert data["configurations"][0]["uuid"] == config_uuid
        assert data["configurations"][0]["max_tokens"] == 4000

        status = client.get("/api/llm-settings/status", headers=user_token_headers)
        assert status.json()["has_settings"] is True


if __name__ == "__main__":
    pytest.main([__file__])
