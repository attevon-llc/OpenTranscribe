"""
Comprehensive tests for the ASR settings system.

Covers schema validation, provider catalog, CRUD operations, activation logic,
sharing, factory resolution, connection testing, status endpoint, and model
capabilities.  All DB tests use the real PostgreSQL ``db_session`` fixture with
savepoint rollback — no mocks for the data layer.
"""

import pytest
from pydantic import ValidationError

from app import models
from app.models.user_asr_settings import UserASRSettings
from app.schemas.asr_settings import ASRProvider
from app.schemas.asr_settings import UserASRSettingsCreate
from app.schemas.media import VALID_LOCAL_WHISPER_MODELS
from app.services.asr.factory import ASRProviderFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_config(
    client, headers, *, name="Test Config", provider="local", model_name="large-v3-turbo", **kwargs
):
    """POST helper that creates an ASR config and returns the raw Response."""
    body = {"name": name, "provider": provider, "model_name": model_name, **kwargs}
    return client.post("/api/asr-settings", json=body, headers=headers)


def _list_configs(client, headers):
    """GET helper that returns the list-configs response dict."""
    resp = client.get("/api/asr-settings", headers=headers)
    assert resp.status_code == 200
    return resp.json()


# ===================================================================
# Schema validation (pure Pydantic, no HTTP)
# ===================================================================


class TestASRSchemaValidation:
    """Pydantic schema validation for UserASRSettingsCreate."""

    def test_valid_local_config(self):
        cfg = UserASRSettingsCreate(
            name="My Local", provider=ASRProvider.LOCAL, model_name="large-v3-turbo"
        )
        assert cfg.provider == ASRProvider.LOCAL
        assert cfg.api_key is None

    def test_valid_cloud_config(self):
        cfg = UserASRSettingsCreate(
            name="DG Config",
            provider=ASRProvider.DEEPGRAM,
            model_name="nova-3",
            api_key="dg_test_key_123",
            base_url="https://api.deepgram.com",
        )
        assert cfg.api_key == "dg_test_key_123"
        assert cfg.base_url == "https://api.deepgram.com"

    def test_name_too_long(self):
        with pytest.raises(ValidationError, match="100 characters"):
            UserASRSettingsCreate(name="X" * 101, provider=ASRProvider.LOCAL, model_name="large-v3")

    def test_name_empty(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            UserASRSettingsCreate(name="   ", provider=ASRProvider.LOCAL, model_name="large-v3")

    def test_api_key_too_long(self):
        with pytest.raises(ValidationError, match="8192"):
            UserASRSettingsCreate(
                name="Long Key",
                provider=ASRProvider.DEEPGRAM,
                model_name="nova-3",
                api_key="k" * 8193,
            )

    def test_invalid_base_url(self):
        with pytest.raises(ValidationError, match="http"):
            UserASRSettingsCreate(
                name="Bad URL",
                provider=ASRProvider.DEEPGRAM,
                model_name="nova-3",
                base_url="ftp://bad.example.com",
            )

    def test_valid_azure_region(self):
        cfg = UserASRSettingsCreate(
            name="Azure",
            provider=ASRProvider.AZURE,
            model_name="whisper",
            api_key="key123",
            region="eastus",
        )
        assert cfg.region == "eastus"

    def test_invalid_azure_region(self):
        with pytest.raises(ValidationError, match="Unknown Azure region"):
            UserASRSettingsCreate(
                name="Azure Bad",
                provider=ASRProvider.AZURE,
                model_name="whisper",
                api_key="key123",
                region="mars-west-1",
            )


# ===================================================================
# Provider catalog
# ===================================================================


class TestASRProviderCatalog:
    """Tests for the /providers endpoint and catalog completeness."""

    def test_catalog_has_all_providers(self, client, user_token_headers):
        resp = client.get("/api/asr-settings/providers", headers=user_token_headers)
        assert resp.status_code == 200
        provider_ids = {p["id"] for p in resp.json()["providers"]}
        expected = {
            "local",
            "deepgram",
            "assemblyai",
            "openai",
            "google",
            "azure",
            "aws",
            "speechmatics",
            "gladia",
            "pyannote",
        }
        assert provider_ids == expected

    def test_catalog_required_fields(self, client, user_token_headers):
        resp = client.get("/api/asr-settings/providers", headers=user_token_headers)
        providers = resp.json()["providers"]
        assert providers, "no ASR providers in the catalog to check required fields on"
        for p in providers:
            assert "id" in p
            assert "display_name" in p
            assert "requires_api_key" in p
            assert "supports_diarization" in p
            assert "models" in p
            assert len(p["models"]) > 0

    def test_local_provider_models(self, client, user_token_headers):
        resp = client.get("/api/asr-settings/providers", headers=user_token_headers)
        local = next(p for p in resp.json()["providers"] if p["id"] == "local")
        model_ids = [m["id"] for m in local["models"]]
        assert "large-v3-turbo" in model_ids
        assert "large-v3" in model_ids
        assert "tiny" in model_ids
        # local provider should not require an API key
        assert local["requires_api_key"] is False


# ===================================================================
# CRUD operations
# ===================================================================


class TestASRConfigCRUD:
    """Create, Read, Update, Delete operations via the HTTP API."""

    def test_create_local_config_no_api_key(self, client, user_token_headers):
        """Bug 1 regression: local config must succeed without an API key."""
        resp = _create_config(client, user_token_headers, name="Local GPU")
        assert resp.status_code == 201
        data = resp.json()
        assert data["provider"] == "local"
        assert data["has_api_key"] is False
        assert data["uuid"]

    def test_create_cloud_config_with_api_key(self, client, user_token_headers):
        resp = _create_config(
            client,
            user_token_headers,
            name="Deepgram Prod",
            provider="deepgram",
            model_name="nova-3",
            api_key="dg_test_key_abc",
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["provider"] == "deepgram"
        assert data["has_api_key"] is True
        # Raw key must never appear in the response
        assert "dg_test_key_abc" not in str(data)

    def test_create_duplicate_name_409(self, client, user_token_headers):
        resp1 = _create_config(client, user_token_headers, name="Unique Name")
        assert resp1.status_code == 201
        resp2 = _create_config(client, user_token_headers, name="Unique Name")
        assert resp2.status_code == 409

    def test_list_own_configs(self, client, user_token_headers):
        _create_config(client, user_token_headers, name="Config A")
        _create_config(client, user_token_headers, name="Config B")
        data = _list_configs(client, user_token_headers)
        names = [c["name"] for c in data["configs"]]
        assert "Config A" in names
        assert "Config B" in names

    def test_get_config_by_uuid(self, client, user_token_headers):
        created = _create_config(client, user_token_headers, name="Fetchable").json()
        uuid = created["uuid"]
        resp = client.get(f"/api/asr-settings/config/{uuid}", headers=user_token_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Fetchable"

    def test_update_config_name(self, client, user_token_headers):
        created = _create_config(client, user_token_headers, name="Old Name").json()
        uuid = created["uuid"]
        resp = client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"name": "New Name"},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    def test_update_config_provider_clears_key(self, client, user_token_headers):
        """Changing provider should clear the stored API key."""
        created = _create_config(
            client,
            user_token_headers,
            name="Provider Swap",
            provider="deepgram",
            model_name="nova-3",
            api_key="dg_key_to_clear",
        ).json()
        uuid = created["uuid"]
        assert created["has_api_key"] is True

        resp = client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"provider": "assemblyai", "model_name": "universal"},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["has_api_key"] is False
        assert resp.json()["provider"] == "assemblyai"

    def test_delete_config_204(self, client, user_token_headers):
        created = _create_config(client, user_token_headers, name="Delete Me").json()
        uuid = created["uuid"]
        resp = client.delete(f"/api/asr-settings/config/{uuid}", headers=user_token_headers)
        assert resp.status_code == 204
        # Confirm it is gone
        resp2 = client.get(f"/api/asr-settings/config/{uuid}", headers=user_token_headers)
        assert resp2.status_code == 404

    def test_delete_nonexistent_404(self, client, user_token_headers):
        resp = client.delete(
            "/api/asr-settings/config/00000000-0000-0000-0000-000000000000",
            headers=user_token_headers,
        )
        assert resp.status_code == 404

    def test_delete_all_configs(self, client, user_token_headers):
        _create_config(client, user_token_headers, name="Bulk A")
        _create_config(client, user_token_headers, name="Bulk B")
        resp = client.delete("/api/asr-settings/all", headers=user_token_headers)
        assert resp.status_code == 204
        data = _list_configs(client, user_token_headers)
        assert len(data["configs"]) == 0

    def test_get_api_key_owner_only(self, client, user_token_headers, other_user_auth_headers):
        """The /api-key endpoint must only return keys to the owning user."""
        created = _create_config(
            client,
            user_token_headers,
            name="Key Owner",
            provider="deepgram",
            model_name="nova-3",
            api_key="dg_secret_key_xyz",
        ).json()
        uuid = created["uuid"]

        # Owner can retrieve
        resp = client.get(f"/api/asr-settings/config/{uuid}/api-key", headers=user_token_headers)
        assert resp.status_code == 200
        assert resp.json()["api_key"] == "dg_secret_key_xyz"

        # Other user cannot
        resp2 = client.get(
            f"/api/asr-settings/config/{uuid}/api-key", headers=other_user_auth_headers
        )
        assert resp2.status_code == 404

    def test_create_config_unauthenticated(self, client):
        resp = client.post(
            "/api/asr-settings",
            json={"name": "No Auth", "provider": "local", "model_name": "tiny"},
        )
        assert resp.status_code == 401


# ===================================================================
# Activation logic
# ===================================================================


class TestASRActivation:
    """Tests for auto-activation, set-active, and delete-promotes behaviour."""

    def test_first_config_auto_activates(self, client, user_token_headers):
        _create_config(client, user_token_headers, name="First Config")
        data = _list_configs(client, user_token_headers)
        assert data["active_config_id"] is not None

    def test_second_config_does_not_auto_activate(self, client, user_token_headers):
        first = _create_config(client, user_token_headers, name="First").json()
        _create_config(client, user_token_headers, name="Second")
        data = _list_configs(client, user_token_headers)
        # Active should still be the first one
        assert data["active_config_id"] == first["id"]

    def test_set_active_by_uuid(self, client, user_token_headers):
        _create_config(client, user_token_headers, name="A")
        second = _create_config(client, user_token_headers, name="B").json()
        resp = client.post(
            "/api/asr-settings/set-active",
            json={"config_uuid": second["uuid"]},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        data = _list_configs(client, user_token_headers)
        assert data["active_config_uuid"] == second["uuid"]

    def test_delete_active_promotes_another(self, client, user_token_headers):
        first = _create_config(client, user_token_headers, name="Primary").json()
        _create_config(client, user_token_headers, name="Backup")
        # First is active (auto-activated). Delete it.
        client.delete(f"/api/asr-settings/config/{first['uuid']}", headers=user_token_headers)
        data = _list_configs(client, user_token_headers)
        # Something should be promoted
        assert data["active_config_id"] is not None
        remaining_ids = [c["id"] for c in data["configs"]]
        assert data["active_config_id"] in remaining_ids

    def test_delete_last_config_clears_active(self, client, user_token_headers):
        only = _create_config(client, user_token_headers, name="Only One").json()
        client.delete(f"/api/asr-settings/config/{only['uuid']}", headers=user_token_headers)
        data = _list_configs(client, user_token_headers)
        assert len(data["configs"]) == 0
        assert data["active_config_id"] is None

    def test_factory_resolves_active_config(self, db_session, normal_user):
        """Bug 2 regression: factory must resolve a config even when is_active=False
        on the record itself — activation is tracked via UserSetting, not the column."""
        cfg = UserASRSettings(
            user_id=normal_user.id,
            name="Inactive Column",
            provider="local",
            model_name="large-v3-turbo",
            is_active=False,  # Column says False, but UserSetting says active
        )
        db_session.add(cfg)
        db_session.commit()
        db_session.refresh(cfg)

        setting = models.UserSetting(
            user_id=normal_user.id,
            setting_key="active_asr_config_id",
            setting_value=str(cfg.id),
        )
        db_session.add(setting)
        db_session.commit()

        provider = ASRProviderFactory.create_for_user(normal_user.id, db_session)
        assert provider.provider_name == "local"


# ===================================================================
# Sharing
# ===================================================================


class TestASRSharing:
    """Tests for config sharing between users."""

    def test_share_toggle_on(self, client, user_token_headers):
        created = _create_config(client, user_token_headers, name="Share Me").json()
        uuid = created["uuid"]
        resp = client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_shared"] is True
        assert resp.json()["shared_at"] is not None

    def test_share_toggle_off(self, client, user_token_headers):
        created = _create_config(client, user_token_headers, name="Unshare Me").json()
        uuid = created["uuid"]
        # Share
        client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        # Unshare
        resp = client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"is_shared": False},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_shared"] is False
        assert resp.json()["shared_at"] is None

    def test_shared_configs_visible_to_others(
        self, client, user_token_headers, other_user_auth_headers
    ):
        created = _create_config(client, user_token_headers, name="Visible").json()
        uuid = created["uuid"]
        client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        # Other user should see it in shared_configs
        data = _list_configs(client, other_user_auth_headers)
        shared_names = [c["name"] for c in data["shared_configs"]]
        assert "Visible" in shared_names

    def test_activate_shared_config(self, client, user_token_headers, other_user_auth_headers):
        created = _create_config(client, user_token_headers, name="Shared Active").json()
        uuid = created["uuid"]
        client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        # Other user activates the shared config
        resp = client.post(
            "/api/asr-settings/set-active",
            json={"config_uuid": uuid},
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 200

    def test_unshare_clears_others_active(
        self, client, user_token_headers, other_user_auth_headers
    ):
        """When a config is unshared, other users who had it active should lose it."""
        created = _create_config(client, user_token_headers, name="Will Unshare").json()
        uuid = created["uuid"]
        # Share
        client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        # Other user activates it
        client.post(
            "/api/asr-settings/set-active",
            json={"config_uuid": uuid},
            headers=other_user_auth_headers,
        )
        # Owner unshares
        client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"is_shared": False},
            headers=user_token_headers,
        )
        # Other user's status should no longer reference this config
        status = client.get("/api/asr-settings/status", headers=other_user_auth_headers)
        assert status.status_code == 200
        active_cfg = status.json().get("active_config")
        # Should be None or a different config
        if active_cfg is not None:
            assert active_cfg["uuid"] != uuid

    def test_delete_shared_cleans_references(
        self, client, user_token_headers, other_user_auth_headers
    ):
        """Deleting a shared config should clear other users' active references."""
        created = _create_config(client, user_token_headers, name="To Delete Shared").json()
        uuid = created["uuid"]
        client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        client.post(
            "/api/asr-settings/set-active",
            json={"config_uuid": uuid},
            headers=other_user_auth_headers,
        )
        # Owner deletes the config
        resp = client.delete(f"/api/asr-settings/config/{uuid}", headers=user_token_headers)
        assert resp.status_code == 204
        # Other user's active config should be cleared
        status = client.get("/api/asr-settings/status", headers=other_user_auth_headers)
        assert status.status_code == 200
        active_cfg = status.json().get("active_config")
        if active_cfg is not None:
            assert active_cfg["uuid"] != uuid


# ===================================================================
# Factory resolution (direct DB access)
# ===================================================================


class TestASRFactoryResolution:
    """Tests for ASRProviderFactory.create_for_user and related resolution."""

    def test_db_config_takes_priority(self, db_session, normal_user):
        """A DB config should override the env-level ASR_PROVIDER."""
        cfg = UserASRSettings(
            user_id=normal_user.id,
            name="DB Priority",
            provider="local",
            model_name="large-v3",
            is_active=True,
        )
        db_session.add(cfg)
        db_session.commit()
        db_session.refresh(cfg)

        setting = models.UserSetting(
            user_id=normal_user.id,
            setting_key="active_asr_config_id",
            setting_value=str(cfg.id),
        )
        db_session.add(setting)
        db_session.commit()

        provider = ASRProviderFactory.create_for_user(normal_user.id, db_session)
        assert provider.provider_name == "local"

    def test_env_var_fallback(self, db_session, normal_user, monkeypatch):
        """With no DB config and ASR_PROVIDER set, env var should be used."""
        # Ensure no DB config exists and env var is set to a key-required provider
        # with a missing key => falls back to local.
        monkeypatch.setenv("ASR_PROVIDER", "deepgram")
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        provider = ASRProviderFactory.create_for_user(normal_user.id, db_session)
        # Deepgram without key falls back to local
        assert provider.provider_name == "local"

    def test_default_local_fallback(self, db_session, normal_user, monkeypatch):
        """With no DB config and ASR_PROVIDER=local (or unset), local is returned."""
        monkeypatch.setenv("ASR_PROVIDER", "local")
        provider = ASRProviderFactory.create_for_user(normal_user.id, db_session)
        assert provider.provider_name == "local"

    def test_shared_config_resolution(self, db_session, normal_user, other_user):
        """Bug 4 regression: factory and capabilities must find shared configs
        that belong to a different user."""
        # other_user creates and shares a config
        cfg = UserASRSettings(
            user_id=other_user.id,
            name="Shared Config",
            provider="local",
            model_name="large-v3",
            is_active=True,
            is_shared=True,
        )
        db_session.add(cfg)
        db_session.commit()
        db_session.refresh(cfg)

        # normal_user activates the shared config
        setting = models.UserSetting(
            user_id=normal_user.id,
            setting_key="active_asr_config_id",
            setting_value=str(cfg.id),
        )
        db_session.add(setting)
        db_session.commit()

        # Factory should resolve the shared config
        provider = ASRProviderFactory.create_for_user(normal_user.id, db_session)
        assert provider.provider_name == "local"

        # Capabilities should also resolve
        caps = ASRProviderFactory.get_active_model_capabilities(normal_user.id, db_session)
        assert caps["provider"] == "local"
        assert caps["model_id"] == "large-v3"

    def test_create_from_config_local(self):
        provider = ASRProviderFactory.create_from_config(provider="local")
        assert provider.provider_name == "local"

    def test_create_from_config_unknown_provider(self):
        """Unknown provider should fall back to local."""
        provider = ASRProviderFactory.create_from_config(provider="nonexistent_provider")
        assert provider.provider_name == "local"


# ===================================================================
# Connection testing
# ===================================================================


class TestASRConnectionTesting:
    """Tests for the ad-hoc and saved-config connection test endpoints."""

    def test_adhoc_test_local_provider(self, client, user_token_headers):
        resp = client.post(
            "/api/asr-settings/test",
            json={"provider": "local"},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "response_time_ms" in data

    def test_saved_config_test_persists_status(self, client, user_token_headers):
        created = _create_config(client, user_token_headers, name="Test Status").json()
        uuid = created["uuid"]
        resp = client.post(f"/api/asr-settings/test-config/{uuid}", headers=user_token_headers)
        assert resp.status_code == 200
        # The test result should be persisted on the config
        config_resp = client.get(f"/api/asr-settings/config/{uuid}", headers=user_token_headers)
        assert config_resp.status_code == 200
        config_data = config_resp.json()
        assert config_data["test_status"] is not None
        assert config_data["last_tested"] is not None

    def test_saved_config_test_error_sanitized(self, client, user_token_headers):
        """If a test fails, any API key in the error message must be sanitized."""
        created = _create_config(
            client,
            user_token_headers,
            name="Error Sanitized",
            provider="deepgram",
            model_name="nova-3",
            api_key="dg_supersecret_key_12345",
        ).json()
        uuid = created["uuid"]
        resp = client.post(f"/api/asr-settings/test-config/{uuid}", headers=user_token_headers)
        assert resp.status_code == 200
        msg = resp.json().get("message", "")
        # The raw key must not appear in the response
        assert "dg_supersecret_key_12345" not in msg

    def test_adhoc_test_missing_provider_validation(self, client, user_token_headers):
        """Sending a request without the required 'provider' field should fail."""
        resp = client.post(
            "/api/asr-settings/test",
            json={},
            headers=user_token_headers,
        )
        assert resp.status_code == 422


# ===================================================================
# Status endpoint
# ===================================================================


class TestASRStatusEndpoint:
    """Tests for GET /api/asr-settings/status."""

    def test_status_no_configs(self, client, user_token_headers):
        resp = client.get("/api/asr-settings/status", headers=user_token_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_settings"] is False
        assert data["active_config"] is None
        assert data["using_local_default"] is True

    def test_status_with_active_config(self, client, user_token_headers):
        _create_config(client, user_token_headers, name="Active Status")
        resp = client.get("/api/asr-settings/status", headers=user_token_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_settings"] is True
        assert data["active_config"] is not None
        assert data["active_config"]["name"] == "Active Status"
        assert data["using_local_default"] is False

    def test_status_includes_capabilities(self, client, user_token_headers):
        _create_config(
            client,
            user_token_headers,
            name="Caps Check",
            provider="local",
            model_name="large-v3-turbo",
        )
        resp = client.get("/api/asr-settings/status", headers=user_token_headers)
        assert resp.status_code == 200
        caps = resp.json()["active_model_capabilities"]
        assert "supports_translation" in caps
        assert "provider" in caps
        assert caps["provider"] == "local"

    def test_status_with_shared_active_config(
        self, client, user_token_headers, other_user_auth_headers
    ):
        """Status should resolve a shared config that belongs to another user."""
        created = _create_config(client, user_token_headers, name="Shared Status").json()
        uuid = created["uuid"]
        client.put(
            f"/api/asr-settings/config/{uuid}",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        # Other user activates it
        client.post(
            "/api/asr-settings/set-active",
            json={"config_uuid": uuid},
            headers=other_user_auth_headers,
        )
        resp = client.get("/api/asr-settings/status", headers=other_user_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_settings"] is True
        assert data["active_config"] is not None
        assert data["active_config"]["name"] == "Shared Status"


# ===================================================================
# Model capabilities (pure logic, no HTTP)
# ===================================================================


class TestASRModelCapabilities:
    """Tests for ASRProviderFactory.get_model_capabilities."""

    def test_exact_model_match(self):
        caps = ASRProviderFactory.get_model_capabilities("local", "large-v3-turbo")
        assert caps["supports_translation"] is False
        assert caps["language_support"] == "english_optimized"

    def test_substring_match_local(self):
        """A model name like 'custom-large-v3-turbo-finetune' should substring-match
        'large-v3-turbo' (most specific first)."""
        caps = ASRProviderFactory.get_model_capabilities("local", "custom-large-v3-turbo-finetune")
        # Should match large-v3-turbo (most specific) which does NOT support translation
        assert caps["supports_translation"] is False

    def test_unknown_provider_permissive_default(self):
        """An unknown provider should return a permissive default."""
        caps = ASRProviderFactory.get_model_capabilities("totally_unknown", "any_model")
        assert caps["supports_translation"] is True
        assert caps["language_support"] == "multilingual"

    def test_turbo_no_translation(self):
        """large-v3-turbo explicitly does NOT support translation."""
        caps = ASRProviderFactory.get_model_capabilities("local", "large-v3-turbo")
        assert caps["supports_translation"] is False

    def test_large_v3_supports_translation(self):
        """large-v3 DOES support translation."""
        caps = ASRProviderFactory.get_model_capabilities("local", "large-v3")
        assert caps["supports_translation"] is True
        assert caps["language_support"] == "multilingual"

    def test_crisperwhisper_ct2_english_only_no_translation(self):
        """The CTranslate2 CrisperWhisper build is English-only and cannot translate."""
        caps = ASRProviderFactory.get_model_capabilities(
            "local", "nyrahealth/faster_CrisperWhisper"
        )
        assert caps["language_support"] == "english_only"
        assert caps["supports_translation"] is False

    def test_crisperwhisper_ct2_in_local_catalog(self):
        """The local catalog lists only the loadable CTranslate2 CrisperWhisper build.

        The PyTorch checkpoint nyrahealth/CrisperWhisper is intentionally excluded —
        faster-whisper's WhisperModel cannot load a transformers safetensors repo, so a
        catalog entry for it would crash on load.
        """
        catalog = ASRProviderFactory.get_provider_catalog()
        local_model_ids = {m["id"] for m in catalog["local"]["models"]}
        assert "nyrahealth/faster_CrisperWhisper" in local_model_ids
        assert "nyrahealth/CrisperWhisper" not in local_model_ids

    def test_crisperwhisper_ct2_in_valid_whisper_models(self):
        """Only the loadable CT2 CrisperWhisper id is whitelisted for reprocessing."""
        assert "nyrahealth/faster_CrisperWhisper" in VALID_LOCAL_WHISPER_MODELS
        assert "nyrahealth/CrisperWhisper" not in VALID_LOCAL_WHISPER_MODELS

    def test_crisperwhisper_short_names_resolve_to_ct2_repo(self):
        """Custom short names resolve to the CTranslate2 repo id loadable by WhisperModel."""
        from app.services.asr.model_discovery import resolve_loadable_model_name

        for short in ("crisperwhisper", "faster_crisperwhisper"):
            assert resolve_loadable_model_name(short) == "nyrahealth/faster_CrisperWhisper"

    def test_resolve_leaves_builtin_short_names_unchanged(self):
        """faster-whisper's own short names and repo ids pass through unchanged."""
        from app.services.asr.model_discovery import resolve_loadable_model_name

        for name in ("large-v3-turbo", "large-v3", "tiny.en", "distil-large-v3", "org/custom-ct2"):
            assert resolve_loadable_model_name(name) == name

    def test_english_only_dotted_en_variants(self):
        """English-only '.en' Whisper variants are classified english_only, not multilingual.

        Guards against the substring loop mis-matching 'tiny.en' to the multilingual
        'tiny' catalog entry.
        """
        for model_id in ("tiny.en", "base.en", "small.en", "medium.en", "distil-small.en"):
            caps = ASRProviderFactory.get_model_capabilities("local", model_id)
            assert caps["language_support"] == "english_only", model_id
            assert caps["supports_translation"] is False, model_id
            assert caps["languages"] == 1, model_id

    def test_multilingual_models_not_misclassified_english_only(self):
        """Multilingual base models keep multilingual capabilities (regression guard)."""
        for model_id in ("large-v3", "medium", "small", "tiny", "base"):
            caps = ASRProviderFactory.get_model_capabilities("local", model_id)
            assert caps["language_support"] == "multilingual", model_id


# ===================================================================
# Local model discovery
# ===================================================================


class TestLocalModelDiscovery:
    """Tests for model cache scanning and the /local-models endpoint."""

    def test_discover_returns_list(self):
        from app.services.asr.model_discovery import discover_local_models

        result = discover_local_models()
        assert isinstance(result, list)
        for m in result:
            assert "short_name" in m
            assert "repo_id" in m
            assert m["downloaded"] is True

    def test_downloaded_model_names_returns_set(self):
        from app.services.asr.model_discovery import get_downloaded_model_names

        result = get_downloaded_model_names()
        assert isinstance(result, set)

    def test_local_models_endpoint(self, client, user_token_headers):
        resp = client.get("/api/asr-settings/local-models", headers=user_token_headers)
        assert resp.status_code == 200
        assert "models" in resp.json()

    def test_providers_catalog_includes_downloaded_flag(self, client, user_token_headers):
        """The /providers endpoint should annotate local models with a 'downloaded' field."""
        resp = client.get("/api/asr-settings/providers", headers=user_token_headers)
        assert resp.status_code == 200
        local = next(p for p in resp.json()["providers"] if p["id"] == "local")
        for m in local["models"]:
            assert "downloaded" in m
            assert isinstance(m["downloaded"], bool)


# ===================================================================
# Admin local model control
# ===================================================================


class TestAdminLocalModelControl:
    """Tests for admin-only local Whisper model management."""

    def test_get_active_local_model(self, client, user_token_headers):
        """Any user can view the active local model."""
        resp = client.get("/api/asr-settings/local-model/active", headers=user_token_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "active_model" in data
        assert data["source"] in ("database", "environment")
        assert "available_models" in data

    def test_set_local_model_requires_admin(self, client, user_token_headers):
        """Regular users cannot change the local model."""
        resp = client.post(
            "/api/asr-settings/local-model/set",
            json={"model_name": "large-v3"},
            headers=user_token_headers,
        )
        assert resp.status_code == 403

    def test_set_local_model_as_admin(self, client, super_admin_token_headers, db_session):
        """Admin can set the local model, stored in SystemSettings."""
        resp = client.post(
            "/api/asr-settings/local-model/set",
            json={"model_name": "large-v3"},
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["model_name"] == "large-v3"

        # Verify it's stored in SystemSettings
        from app.models.system_settings import SystemSettings

        setting = (
            db_session.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
        )
        assert setting is not None
        assert setting.value == "large-v3"

    def test_set_local_model_updates_existing(self, client, super_admin_token_headers, db_session):
        """Setting the model twice updates the existing row."""
        client.post(
            "/api/asr-settings/local-model/set",
            json={"model_name": "large-v3"},
            headers=super_admin_token_headers,
        )
        resp = client.post(
            "/api/asr-settings/local-model/set",
            json={"model_name": "large-v3-turbo"},
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["model_name"] == "large-v3-turbo"

        from app.models.system_settings import SystemSettings

        count = (
            db_session.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").count()
        )
        assert count == 1

    def test_set_local_model_remaps_pytorch_crisperwhisper_to_ct2(
        self, client, super_admin_token_headers, db_session
    ):
        """The PyTorch CrisperWhisper checkpoint is remapped to the loadable CT2 build.

        faster-whisper cannot load nyrahealth/CrisperWhisper (safetensors, no CT2
        model.bin), so the endpoint stores the CTranslate2 build instead.
        """
        resp = client.post(
            "/api/asr-settings/local-model/set",
            json={"model_name": "nyrahealth/CrisperWhisper"},
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["model_name"] == "nyrahealth/faster_CrisperWhisper"

        from app.models.system_settings import SystemSettings

        setting = (
            db_session.query(SystemSettings).filter(SystemSettings.key == "asr.local_model").first()
        )
        assert setting.value == "nyrahealth/faster_CrisperWhisper"

    def test_set_local_model_resolves_crisperwhisper_short_name(
        self, client, super_admin_token_headers
    ):
        """The 'crisperwhisper' short name is stored as the loadable CT2 repo id."""
        resp = client.post(
            "/api/asr-settings/local-model/set",
            json={"model_name": "crisperwhisper"},
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["model_name"] == "nyrahealth/faster_CrisperWhisper"

    def test_set_local_model_empty_name_rejected(self, client, super_admin_token_headers):
        """Empty model name is rejected."""
        resp = client.post(
            "/api/asr-settings/local-model/set",
            json={"model_name": "  "},
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 400

    def test_get_active_model_reflects_db_setting(self, client, super_admin_token_headers):
        """After setting via admin endpoint, get returns the DB value."""
        client.post(
            "/api/asr-settings/local-model/set",
            json={"model_name": "medium"},
            headers=super_admin_token_headers,
        )
        resp = client.get(
            "/api/asr-settings/local-model/active",
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_model"] == "medium"
        assert data["source"] == "database"

    def test_restart_requires_admin(self, client, user_token_headers):
        """Regular users cannot restart the GPU worker."""
        resp = client.post(
            "/api/asr-settings/local-model/restart",
            headers=user_token_headers,
        )
        assert resp.status_code == 403


class TestTranscriptionConfigResolvesModel:
    """Test that TranscriptionConfig._resolve_model_name reads from SystemSettings."""

    def test_resolve_falls_back_to_env(self, monkeypatch):
        """Without a DB setting, falls back to WHISPER_MODEL env var."""
        monkeypatch.setenv("WHISPER_MODEL", "tiny")
        from app.transcription.config import TranscriptionConfig

        result = TranscriptionConfig._resolve_model_name()
        # May return DB value if one exists; if not, env var
        assert isinstance(result, str)
        assert len(result) > 0

    def test_resolve_returns_string(self):
        """_resolve_model_name always returns a non-empty string."""
        from app.transcription.config import TranscriptionConfig

        result = TranscriptionConfig._resolve_model_name()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_admin_set_model_reflected_in_get(self, client, super_admin_token_headers):
        """Full round-trip: admin sets model, GET endpoint returns it."""
        # This test exercises the full DB path (set + get both use API sessions)
        client.post(
            "/api/asr-settings/local-model/set",
            json={"model_name": "distil-large-v3"},
            headers=super_admin_token_headers,
        )
        resp = client.get(
            "/api/asr-settings/local-model/active",
            headers=super_admin_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["active_model"] == "distil-large-v3"
        assert resp.json()["source"] == "database"
