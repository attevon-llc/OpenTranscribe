"""Tests for global sharing of LLM/ASR configs and prompts.

Verifies that shared configurations and prompts are served correctly
to the right users while enforcing security boundaries (API keys never
exposed to non-owners, edit/delete restricted to owners).
"""

import uuid

from app.models.prompt import SummaryPrompt
from app.models.prompt import UserSetting
from app.models.user_asr_settings import UserASRSettings
from app.models.user_llm_settings import UserLLMSettings
from app.utils.encryption import encrypt_api_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_llm_config(db, user, *, name=None, shared=False, api_key="sk-test-key-1234"):
    """Create an LLM config directly in the DB and return it."""
    cfg = UserLLMSettings(
        user_id=user.id,
        name=name or f"llm-{uuid.uuid4().hex[:6]}",
        provider="openai",
        model_name="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        api_key=encrypt_api_key(api_key) if api_key else None,
        max_tokens=16000,
        temperature="0.3",
        is_active=True,
        is_shared=shared,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def _create_asr_config(db, user, *, name=None, shared=False, api_key="sk-asr-key-5678"):
    """Create an ASR config directly in the DB and return it."""
    cfg = UserASRSettings(
        user_id=user.id,
        name=name or f"asr-{uuid.uuid4().hex[:6]}",
        provider="deepgram",
        model_name="nova-2",
        api_key=encrypt_api_key(api_key) if api_key else None,
        is_active=True,
        is_shared=shared,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def _create_prompt(db, user, *, name=None, shared=False, tags=None, shared_by=None):
    """Create a custom prompt directly in the DB and return it."""
    p = SummaryPrompt(
        user_id=user.id,
        name=name or f"prompt-{uuid.uuid4().hex[:6]}",
        prompt_text="Summarise {transcript}.",
        content_type="general",
        is_system_default=False,
        is_active=True,
        is_shared=shared,
        shared_by=shared_by.id if shared_by else None,
        tags=tags or [],
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _get_system_prompt(db):
    """Return an existing active system default prompt (seeded in the live DB)."""
    return (
        db.query(SummaryPrompt)
        .filter(SummaryPrompt.is_system_default == True, SummaryPrompt.is_active == True)  # noqa: E712
        .first()
    )


def _set_active_llm(db, user, config):
    """Set the active LLM config for a user via UserSetting."""
    setting = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.setting_key == "active_llm_config_id")
        .first()
    )
    if setting:
        setting.setting_value = str(config.id)
    else:
        setting = UserSetting(
            user_id=user.id, setting_key="active_llm_config_id", setting_value=str(config.id)
        )
        db.add(setting)
    db.commit()


def _set_active_asr(db, user, config):
    """Set the active ASR config for a user via UserSetting."""
    setting = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.setting_key == "active_asr_config_id")
        .first()
    )
    if setting:
        setting.setting_value = str(config.id)
    else:
        setting = UserSetting(
            user_id=user.id, setting_key="active_asr_config_id", setting_value=str(config.id)
        )
        db.add(setting)
    db.commit()


# ===================================================================
# LLM Config Sharing
# ===================================================================


class TestLLMSharingVisibility:
    """Owner's shared LLM configs appear in other users' lists."""

    def test_shared_config_appears_for_other_user(
        self,
        client,
        db_session,
        normal_user,
        other_user,
        user_token_headers,
        other_user_auth_headers,
    ):
        """A shared config created by normal_user should appear in other_user's shared list."""
        _create_llm_config(db_session, normal_user, shared=True, name="Shared GPT")

        resp = client.get("/api/llm-settings", headers=other_user_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        shared = data.get("shared_configurations", [])
        names = [c["name"] for c in shared]
        assert "Shared GPT" in names

    def test_unshared_config_hidden_from_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """An unshared config should NOT appear in other_user's lists."""
        _create_llm_config(db_session, normal_user, shared=False, name="Private GPT")

        resp = client.get("/api/llm-settings", headers=other_user_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        shared = data.get("shared_configurations", [])
        own = data.get("configurations", [])
        all_names = [c["name"] for c in shared + own]
        assert "Private GPT" not in all_names

    def test_own_shared_config_not_in_shared_list(
        self, client, db_session, normal_user, user_token_headers
    ):
        """Owner's shared config should appear in their own configs, not in shared_configurations."""
        cfg = _create_llm_config(db_session, normal_user, shared=True, name="My Shared")

        resp = client.get("/api/llm-settings", headers=user_token_headers)
        assert resp.status_code == 200
        data = resp.json()
        own_names = [c["name"] for c in data["configurations"]]
        shared_names = [c["name"] for c in data.get("shared_configurations", [])]
        assert "My Shared" in own_names
        assert "My Shared" not in shared_names

    def test_shared_config_shows_owner_attribution(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Shared configs should include the owner's name and role."""
        _create_llm_config(db_session, normal_user, shared=True, name="Attributed")

        resp = client.get("/api/llm-settings", headers=other_user_auth_headers)
        data = resp.json()
        shared = data.get("shared_configurations", [])
        cfg = next(c for c in shared if c["name"] == "Attributed")
        assert cfg["owner_name"] == normal_user.full_name
        assert cfg["is_own"] is False


class TestLLMSharingAPIKeySecurity:
    """API keys must NEVER be exposed to non-owners."""

    def test_shared_config_has_no_api_key_in_list(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Shared configs in list response should have has_api_key=True but no actual key."""
        _create_llm_config(db_session, normal_user, shared=True, api_key="sk-secret-key")

        resp = client.get("/api/llm-settings", headers=other_user_auth_headers)
        shared = resp.json().get("shared_configurations", [])
        assert len(shared) >= 1
        cfg = shared[0]
        assert cfg["has_api_key"] is True
        assert "api_key" not in cfg

    def test_non_owner_cannot_get_api_key(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner requesting GET /config/{uuid}/api-key should get 403."""
        cfg = _create_llm_config(db_session, normal_user, shared=True)
        resp = client.get(
            f"/api/llm-settings/config/{cfg.uuid}/api-key", headers=other_user_auth_headers
        )
        assert resp.status_code == 403

    def test_owner_can_get_api_key(self, client, db_session, normal_user, user_token_headers):
        """Owner requesting GET /config/{uuid}/api-key should succeed."""
        cfg = _create_llm_config(db_session, normal_user, shared=True, api_key="sk-owner-key")
        resp = client.get(
            f"/api/llm-settings/config/{cfg.uuid}/api-key", headers=user_token_headers
        )
        assert resp.status_code == 200
        assert resp.json()["api_key"] is not None


class TestLLMSharingAccessControl:
    """Edit/delete restricted to owner; activate allowed for shared."""

    def test_non_owner_cannot_edit_shared_config(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner should get 403 when trying to update a shared config."""
        cfg = _create_llm_config(db_session, normal_user, shared=True)
        resp = client.put(
            f"/api/llm-settings/config/{cfg.uuid}",
            json={"name": "Hacked Name"},
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 403

    def test_non_owner_cannot_delete_shared_config(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner should get 403 when trying to delete a shared config."""
        cfg = _create_llm_config(db_session, normal_user, shared=True)
        resp = client.delete(
            f"/api/llm-settings/config/{cfg.uuid}", headers=other_user_auth_headers
        )
        assert resp.status_code == 403

    def test_non_owner_can_activate_shared_config(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner should be able to set a shared config as their active config."""
        cfg = _create_llm_config(db_session, normal_user, shared=True)
        resp = client.post(
            "/api/llm-settings/set-active",
            json={"configuration_id": str(cfg.uuid)},
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 200

    def test_non_owner_can_view_shared_config(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner can GET a shared config by UUID."""
        cfg = _create_llm_config(db_session, normal_user, shared=True, name="ViewMe")
        resp = client.get(f"/api/llm-settings/config/{cfg.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "ViewMe"

    def test_non_owner_cannot_view_private_config(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner should get 403 when trying to view a private config."""
        cfg = _create_llm_config(db_session, normal_user, shared=False)
        resp = client.get(f"/api/llm-settings/config/{cfg.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403


class TestLLMShareToggle:
    """Owner can toggle sharing on/off via the update endpoint."""

    def test_owner_can_share_config(self, client, db_session, normal_user, user_token_headers):
        """Owner toggles is_shared to True."""
        cfg = _create_llm_config(db_session, normal_user, shared=False)
        resp = client.put(
            f"/api/llm-settings/config/{cfg.uuid}",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_shared"] is True

    def test_owner_can_unshare_config(self, client, db_session, normal_user, user_token_headers):
        """Owner toggles is_shared to False."""
        cfg = _create_llm_config(db_session, normal_user, shared=True)
        resp = client.put(
            f"/api/llm-settings/config/{cfg.uuid}",
            json={"is_shared": False},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_shared"] is False


class TestLLMSharedStatusEndpoint:
    """GET /llm-settings/status resolves shared configs for the active user."""

    def test_status_with_shared_active_config(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """When other_user has a shared config set active, status should reflect it."""
        cfg = _create_llm_config(db_session, normal_user, shared=True, name="SharedActive")
        _set_active_llm(db_session, other_user, cfg)

        resp = client.get("/api/llm-settings/status", headers=other_user_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_settings"] is True
        active = data.get("active_configuration")
        assert active is not None
        assert active["name"] == "SharedActive"


# ===================================================================
# ASR Config Sharing
# ===================================================================


class TestASRSharingVisibility:
    """Owner's shared ASR configs appear in other users' lists."""

    def test_shared_asr_config_appears_for_other_user(
        self,
        client,
        db_session,
        normal_user,
        other_user,
        user_token_headers,
        other_user_auth_headers,
    ):
        """A shared ASR config should appear in other_user's shared_configs list."""
        _create_asr_config(db_session, normal_user, shared=True, name="Shared Deepgram")

        resp = client.get("/api/asr-settings", headers=other_user_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        shared = data.get("shared_configs", [])
        names = [c["name"] for c in shared]
        assert "Shared Deepgram" in names

    def test_unshared_asr_config_hidden(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """An unshared ASR config should NOT appear for other users."""
        _create_asr_config(db_session, normal_user, shared=False, name="Private Deepgram")

        resp = client.get("/api/asr-settings", headers=other_user_auth_headers)
        data = resp.json()
        shared = data.get("shared_configs", [])
        own = data.get("configs", data.get("configurations", []))
        all_names = [c["name"] for c in shared + own]
        assert "Private Deepgram" not in all_names

    def test_shared_asr_has_owner_attribution(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Shared ASR configs should carry owner info for non-owners."""
        _create_asr_config(db_session, normal_user, shared=True, name="Attributed ASR")

        resp = client.get("/api/asr-settings", headers=other_user_auth_headers)
        shared = resp.json().get("shared_configs", [])
        cfg = next(c for c in shared if c["name"] == "Attributed ASR")
        assert cfg["owner_name"] == normal_user.full_name
        assert cfg["is_own"] is False


class TestASRSharingAPIKeySecurity:
    """API keys must NEVER be exposed to non-owners for ASR configs."""

    def test_non_owner_cannot_get_asr_api_key(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner requesting GET /config/{uuid}/api-key should be denied (404 hides resource)."""
        cfg = _create_asr_config(db_session, normal_user, shared=True)
        resp = client.get(
            f"/api/asr-settings/config/{cfg.uuid}/api-key", headers=other_user_auth_headers
        )
        assert resp.status_code == 404, resp.text  # a non-owner must not learn the config exists

    def test_owner_can_get_asr_api_key(self, client, db_session, normal_user, user_token_headers):
        """Owner can retrieve their own ASR API key."""
        cfg = _create_asr_config(db_session, normal_user, shared=True, api_key="sk-asr-owner")
        resp = client.get(
            f"/api/asr-settings/config/{cfg.uuid}/api-key", headers=user_token_headers
        )
        assert resp.status_code == 200
        assert resp.json()["api_key"] is not None


class TestASRSharingAccessControl:
    """Edit/delete restricted to owner; activate allowed for shared ASR configs."""

    def test_non_owner_cannot_edit_shared_asr(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner cannot update a shared ASR config."""
        cfg = _create_asr_config(db_session, normal_user, shared=True)
        resp = client.put(
            f"/api/asr-settings/config/{cfg.uuid}",
            json={"name": "Hacked"},
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 404, resp.text  # a non-owner must not learn the config exists

    def test_non_owner_cannot_delete_shared_asr(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner cannot delete a shared ASR config."""
        cfg = _create_asr_config(db_session, normal_user, shared=True)
        resp = client.delete(
            f"/api/asr-settings/config/{cfg.uuid}", headers=other_user_auth_headers
        )
        assert resp.status_code == 404, resp.text  # a non-owner must not learn the config exists

    def test_non_owner_can_activate_shared_asr(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner can set a shared ASR config as active."""
        cfg = _create_asr_config(db_session, normal_user, shared=True)
        resp = client.post(
            "/api/asr-settings/set-active",
            json={"config_uuid": str(cfg.uuid)},
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 200

    def test_non_owner_can_view_shared_asr(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner can GET a shared ASR config by UUID."""
        cfg = _create_asr_config(db_session, normal_user, shared=True, name="ViewASR")
        resp = client.get(f"/api/asr-settings/config/{cfg.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "ViewASR"

    def test_non_owner_cannot_view_private_asr(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner gets 404 for a private ASR config."""
        cfg = _create_asr_config(db_session, normal_user, shared=False)
        resp = client.get(f"/api/asr-settings/config/{cfg.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 404


# ===================================================================
# Prompt Sharing
# ===================================================================


class TestPromptSharingVisibility:
    """Shared prompts appear for other users in the prompts list."""

    def test_shared_prompt_appears_for_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """A shared prompt should appear in other_user's prompt list."""
        _create_prompt(db_session, normal_user, shared=True, name="Shared Summary")

        resp = client.get("/api/prompts", headers=other_user_auth_headers)
        assert resp.status_code == 200
        prompts = resp.json().get("prompts", [])
        names = [p["name"] for p in prompts]
        assert "Shared Summary" in names

    def test_unshared_prompt_hidden_from_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """An unshared prompt should NOT appear for other users."""
        _create_prompt(db_session, normal_user, shared=False, name="Private Summary")

        resp = client.get("/api/prompts", headers=other_user_auth_headers)
        prompts = resp.json().get("prompts", [])
        names = [p["name"] for p in prompts if not p.get("is_system_default")]
        assert "Private Summary" not in names

    def test_shared_prompt_has_author_attribution(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Shared prompts should carry author_name and is_owner=False for non-owners."""
        _create_prompt(db_session, normal_user, shared=True, name="Attributed Prompt")

        resp = client.get("/api/prompts", headers=other_user_auth_headers)
        prompts = resp.json().get("prompts", [])
        p = next(p for p in prompts if p["name"] == "Attributed Prompt")
        assert p["author_name"] == normal_user.full_name
        assert p["is_owner"] is False

    def test_own_prompt_has_is_owner_true(
        self, client, db_session, normal_user, user_token_headers
    ):
        """Owner's own prompt should have is_owner=True."""
        _create_prompt(db_session, normal_user, shared=True, name="My Prompt")

        resp = client.get("/api/prompts", headers=user_token_headers)
        prompts = resp.json().get("prompts", [])
        p = next(p for p in prompts if p["name"] == "My Prompt")
        assert p["is_owner"] is True


class TestPromptSharingAccessControl:
    """Edit/delete restricted to owner; activate allowed for shared prompts."""

    def test_non_owner_cannot_edit_shared_prompt(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner should get 403 when trying to update a shared prompt."""
        p = _create_prompt(db_session, normal_user, shared=True)
        resp = client.put(
            f"/api/prompts/{p.uuid}",
            json={"name": "Hacked Name"},
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 403

    def test_non_owner_cannot_delete_shared_prompt(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner should get 403 when trying to delete a shared prompt."""
        p = _create_prompt(db_session, normal_user, shared=True)
        resp = client.delete(f"/api/prompts/{p.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403

    def test_non_owner_can_activate_shared_prompt(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner should be able to set a shared prompt as their active prompt."""
        p = _create_prompt(db_session, normal_user, shared=True)
        resp = client.post(
            "/api/prompts/active/set",
            json={"prompt_id": str(p.uuid)},
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 200

    def test_non_owner_can_view_shared_prompt(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner can GET a shared prompt by UUID."""
        p = _create_prompt(db_session, normal_user, shared=True, name="ViewPrompt")
        resp = client.get(f"/api/prompts/{p.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "ViewPrompt"

    def test_non_owner_cannot_view_private_prompt(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner should get 403 for a private (unshared) prompt."""
        p = _create_prompt(db_session, normal_user, shared=False)
        resp = client.get(f"/api/prompts/{p.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403


class TestPromptShareToggle:
    """Owner can toggle sharing on/off via the dedicated endpoint."""

    def test_owner_can_share_prompt(self, client, db_session, normal_user, user_token_headers):
        """Owner shares a prompt via POST /prompts/shared/{uuid}/toggle."""
        p = _create_prompt(db_session, normal_user, shared=False)
        resp = client.post(
            f"/api/prompts/shared/{p.uuid}/toggle",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_shared"] is True

    def test_owner_can_unshare_prompt(self, client, db_session, normal_user, user_token_headers):
        """Owner unshares a prompt."""
        p = _create_prompt(db_session, normal_user, shared=True)
        resp = client.post(
            f"/api/prompts/shared/{p.uuid}/toggle",
            json={"is_shared": False},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_shared"] is False

    def test_non_owner_cannot_toggle_share(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner should get 403 when trying to toggle sharing."""
        p = _create_prompt(db_session, normal_user, shared=True)
        resp = client.post(
            f"/api/prompts/shared/{p.uuid}/toggle",
            json={"is_shared": False},
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 403


class TestPromptTags:
    """Tags are stored and returned correctly."""

    def test_create_prompt_with_tags(self, client, db_session, normal_user, user_token_headers):
        """Creating a prompt with tags should store them."""
        resp = client.post(
            "/api/prompts",
            json={
                "name": f"Tagged-{uuid.uuid4().hex[:6]}",
                "prompt_text": "Summarise {transcript}.",
                "tags": ["meeting", "action-items"],
            },
            headers=user_token_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "meeting" in data["tags"]
        assert "action-items" in data["tags"]

    def test_update_prompt_tags(self, client, db_session, normal_user, user_token_headers):
        """Updating a prompt's tags should replace them."""
        p = _create_prompt(db_session, normal_user, tags=["old-tag"])
        resp = client.put(
            f"/api/prompts/{p.uuid}",
            json={"tags": ["new-tag", "updated"]},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert "new-tag" in resp.json()["tags"]
        assert "old-tag" not in resp.json()["tags"]


class TestSharedPromptLibrary:
    """GET /prompts/shared/library endpoint with filtering."""

    def test_shared_library_returns_shared_prompts(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Shared library should list shared prompts."""
        _create_prompt(db_session, normal_user, shared=True, name="LibraryPrompt", tags=["test"])

        resp = client.get("/api/prompts/shared/library", headers=other_user_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        names = [p["name"] for p in data["prompts"]]
        assert "LibraryPrompt" in names

    def test_shared_library_excludes_unshared(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Private prompts should not appear in the shared library."""
        _create_prompt(db_session, normal_user, shared=False, name="HiddenPrompt")

        resp = client.get("/api/prompts/shared/library", headers=other_user_auth_headers)
        data = resp.json()
        names = [p["name"] for p in data["prompts"]]
        assert "HiddenPrompt" not in names

    def test_shared_library_filter_by_tag(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Filtering by tag should only return prompts with that tag."""
        _create_prompt(db_session, normal_user, shared=True, name="TagA", tags=["alpha"])
        _create_prompt(db_session, normal_user, shared=True, name="TagB", tags=["beta"])

        resp = client.get("/api/prompts/shared/library?tags=alpha", headers=other_user_auth_headers)
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["prompts"]]
        assert "TagA" in names
        assert "TagB" not in names

    def test_shared_library_search(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Search should filter by name/description."""
        unique = uuid.uuid4().hex[:8]
        _create_prompt(db_session, normal_user, shared=True, name=f"UniqueSearch-{unique}")

        resp = client.get(
            f"/api/prompts/shared/library?search={unique}", headers=other_user_auth_headers
        )
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["prompts"]]
        assert any(unique in n for n in names)


# ===================================================================
# Cleanup / Edge Cases
# ===================================================================


class TestSharedConfigDeletionCleanup:
    """When a shared config is deleted, other users' active references are cleaned up."""

    def test_delete_shared_llm_clears_others_active(
        self,
        client,
        db_session,
        normal_user,
        other_user,
        user_token_headers,
        other_user_auth_headers,
    ):
        """Deleting a shared LLM config should remove active references for non-owners."""
        cfg = _create_llm_config(db_session, normal_user, shared=True)
        _set_active_llm(db_session, other_user, cfg)

        # Verify other_user has it active
        setting = (
            db_session.query(UserSetting)
            .filter(
                UserSetting.user_id == other_user.id,
                UserSetting.setting_key == "active_llm_config_id",
            )
            .first()
        )
        assert setting is not None

        # Owner deletes
        resp = client.delete(f"/api/llm-settings/config/{cfg.uuid}", headers=user_token_headers)
        assert resp.status_code == 200, resp.text

        # Verify other_user's active reference is cleaned up
        db_session.expire_all()
        setting = (
            db_session.query(UserSetting)
            .filter(
                UserSetting.user_id == other_user.id,
                UserSetting.setting_key == "active_llm_config_id",
            )
            .first()
        )
        assert setting is None

    def test_unshare_llm_clears_others_active(
        self, client, db_session, normal_user, other_user, user_token_headers
    ):
        """Unsharing a config should remove other users' active references."""
        cfg = _create_llm_config(db_session, normal_user, shared=True)
        _set_active_llm(db_session, other_user, cfg)

        # Owner unshares
        resp = client.put(
            f"/api/llm-settings/config/{cfg.uuid}",
            json={"is_shared": False},
            headers=user_token_headers,
        )
        assert resp.status_code == 200

        # Other user's reference should be gone
        db_session.expire_all()
        setting = (
            db_session.query(UserSetting)
            .filter(
                UserSetting.user_id == other_user.id,
                UserSetting.setting_key == "active_llm_config_id",
            )
            .first()
        )
        assert setting is None


class TestUnauthenticatedAccess:
    """Unauthenticated requests should be rejected."""

    def test_llm_list_requires_auth(self, client):
        """GET /llm-settings without auth returns 401."""
        resp = client.get("/api/llm-settings")
        assert resp.status_code == 401

    def test_asr_list_requires_auth(self, client):
        """GET /asr-settings without auth returns 401."""
        resp = client.get("/api/asr-settings")
        assert resp.status_code == 401

    def test_prompts_list_requires_auth(self, client):
        """GET /prompts without auth returns 401."""
        resp = client.get("/api/prompts")
        assert resp.status_code == 401

    def test_shared_library_requires_auth(self, client):
        """GET /prompts/shared/library without auth returns 401."""
        resp = client.get("/api/prompts/shared/library")
        assert resp.status_code == 401


# ===================================================================
# Prompt Clone (issue #78)
# ===================================================================


class TestPromptClone:
    """Cloning a prompt into the current user's editable library."""

    def test_clone_shared_prompt_creates_owned_copy(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Cloning a shared prompt yields a private, owned copy named '(copy)'."""
        src = _create_prompt(db_session, normal_user, shared=True, name="Great Prompt")

        resp = client.post(f"/api/prompts/{src.uuid}/clone", headers=other_user_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Great Prompt (copy)"
        assert data["is_owner"] is True
        assert data["is_shared"] is False
        assert data["prompt_text"] == src.prompt_text
        assert data["uuid"] != str(src.uuid)

        # The clone now appears in the cloner's own prompt list.
        list_resp = client.get("/api/prompts", headers=other_user_auth_headers)
        names = [p["name"] for p in list_resp.json()["prompts"]]
        assert "Great Prompt (copy)" in names

    def test_clone_system_prompt(self, client, db_session, other_user, other_user_auth_headers):
        """A system prompt can be cloned into a user's library."""
        sys_prompt = _get_system_prompt(db_session)
        if sys_prompt is None:
            # Fresh databases (CI) have no startup-seeded prompts — create one
            # inside the savepoint so the test is self-sufficient.
            sys_prompt = SummaryPrompt(
                name="CI System Prompt",
                prompt_text="Summarize the transcript.",
                is_system_default=True,
                is_active=True,
                user_id=None,
                content_type="general",
            )
            db_session.add(sys_prompt)
            db_session.commit()
            db_session.refresh(sys_prompt)

        resp = client.post(f"/api/prompts/{sys_prompt.uuid}/clone", headers=other_user_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_owner"] is True
        assert data["is_system_default"] is False
        assert data["name"] == f"{sys_prompt.name} (copy)"

    def test_cannot_clone_private_prompt_of_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Cloning another user's private prompt is forbidden (403)."""
        src = _create_prompt(db_session, normal_user, shared=False, name="Secret")

        resp = client.post(f"/api/prompts/{src.uuid}/clone", headers=other_user_auth_headers)
        assert resp.status_code == 403

    def test_clone_counts_toward_prompt_limit(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """A clone is rejected (400) when the cloner is already at the 50-prompt cap."""
        # Fill the cloner's library to the cap.
        for i in range(50):
            _create_prompt(db_session, other_user, name=f"filler-{i}-{uuid.uuid4().hex[:4]}")

        src = _create_prompt(db_session, normal_user, shared=True, name="Capped")
        resp = client.post(f"/api/prompts/{src.uuid}/clone", headers=other_user_auth_headers)
        assert resp.status_code == 400
        assert "Maximum number" in resp.json()["detail"]


class TestPromptShareAttribution:
    """shared_by attribution is recorded and surfaced distinctly from the creator."""

    def test_admin_share_sets_shared_by_name(
        self,
        client,
        db_session,
        normal_user,
        admin_user,
        admin_token_headers,
        other_user,
        other_user_auth_headers,
    ):
        """When an admin shares another user's prompt, shared_by_name is the admin's name."""
        prompt = _create_prompt(db_session, normal_user, shared=False, name="Curated")

        share_resp = client.post(
            f"/api/prompts/shared/{prompt.uuid}/toggle",
            json={"is_shared": True},
            headers=admin_token_headers,
        )
        assert share_resp.status_code == 200

        resp = client.get("/api/prompts", headers=other_user_auth_headers)
        p = next(p for p in resp.json()["prompts"] if p["name"] == "Curated")
        assert p["author_name"] == normal_user.full_name
        assert p["shared_by_name"] == admin_user.full_name
        assert p["is_owner"] is False

    def test_unshare_clears_shared_by(self, client, db_session, normal_user, user_token_headers):
        """Unsharing clears the shared_by attribution."""
        prompt = _create_prompt(db_session, normal_user, shared=True, shared_by=normal_user)

        resp = client.post(
            f"/api/prompts/shared/{prompt.uuid}/toggle",
            json={"is_shared": False},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        db_session.expire_all()
        db_session.refresh(prompt)
        assert prompt.shared_by is None
        assert prompt.shared_at is None


class TestPromptShareAudit:
    """Share / unshare / clone emit audit events."""

    def test_share_emits_audit_event(
        self, client, db_session, normal_user, user_token_headers, monkeypatch
    ):
        """Sharing a prompt logs a PROMPT_SHARE audit event."""
        from app.api.endpoints import prompts as prompts_module
        from app.auth.audit import AuditEventType

        events = []
        monkeypatch.setattr(
            prompts_module.audit_logger,
            "log",
            lambda **kw: events.append(kw),
        )

        prompt = _create_prompt(db_session, normal_user, shared=False)
        resp = client.post(
            f"/api/prompts/shared/{prompt.uuid}/toggle",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert any(e["event_type"] == AuditEventType.PROMPT_SHARE for e in events)
        evt = next(e for e in events if e["event_type"] == AuditEventType.PROMPT_SHARE)
        assert evt["details"]["prompt_uuid"] == str(prompt.uuid)

    def test_clone_emits_audit_event(
        self, client, db_session, normal_user, other_user, other_user_auth_headers, monkeypatch
    ):
        """Cloning a prompt logs a PROMPT_CLONE audit event referencing the source."""
        from app.api.endpoints import prompts as prompts_module
        from app.auth.audit import AuditEventType

        events = []
        monkeypatch.setattr(
            prompts_module.audit_logger,
            "log",
            lambda **kw: events.append(kw),
        )

        src = _create_prompt(db_session, normal_user, shared=True)
        resp = client.post(f"/api/prompts/{src.uuid}/clone", headers=other_user_auth_headers)
        assert resp.status_code == 200
        clone_uuid = resp.json()["uuid"]
        evt = next(e for e in events if e["event_type"] == AuditEventType.PROMPT_CLONE)
        assert evt["details"]["source_prompt_uuid"] == str(src.uuid)
        assert evt["details"]["prompt_uuid"] == clone_uuid


class TestPromptUsageCount:
    """usage_count is incremented on real use and drives 'popular' ordering."""

    def test_increment_prompt_usage_bumps_count(self, db_session, normal_user):
        """increment_prompt_usage(+1) raises usage_count by one."""
        from app.utils.prompt_manager import increment_prompt_usage

        p = _create_prompt(db_session, normal_user, shared=True)
        assert p.usage_count == 0

        increment_prompt_usage(db_session, p.id)
        db_session.refresh(p)
        assert p.usage_count == 1

        increment_prompt_usage(db_session, p.id)
        db_session.refresh(p)
        assert p.usage_count == 2

    def test_increment_prompt_usage_by_uuid(self, db_session, normal_user):
        """increment_prompt_usage accepts a UUID as well as an int id."""
        from app.utils.prompt_manager import increment_prompt_usage

        p = _create_prompt(db_session, normal_user, shared=True)
        increment_prompt_usage(db_session, str(p.uuid))
        db_session.refresh(p)
        assert p.usage_count == 1

    def test_increment_missing_prompt_is_noop(self, db_session):
        """Incrementing a non-existent prompt does not raise."""
        from app.utils.prompt_manager import increment_prompt_usage

        increment_prompt_usage(db_session, 999_999_999)  # no row; must not raise

    def test_shared_library_popular_sort_reflects_usage(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """The 'popular' sort orders a heavily-used prompt above a fresh one."""
        from app.utils.prompt_manager import increment_prompt_usage

        unique = uuid.uuid4().hex[:8]
        low = _create_prompt(db_session, normal_user, shared=True, name=f"low-{unique}")
        high = _create_prompt(db_session, normal_user, shared=True, name=f"high-{unique}")
        for _ in range(5):
            increment_prompt_usage(db_session, high.id)

        resp = client.get(
            "/api/prompts/shared/library?sort_by=popular", headers=other_user_auth_headers
        )
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["prompts"]]
        assert f"high-{unique}" in names
        assert f"low-{unique}" in names
        assert names.index(f"high-{unique}") < names.index(f"low-{unique}")


class TestSharedPromptDeletionCleanup:
    """Deleting a shared prompt clears every user's active-prompt pointer."""

    def test_delete_shared_prompt_clears_others_active(
        self,
        client,
        db_session,
        normal_user,
        other_user,
        user_token_headers,
        other_user_auth_headers,
    ):
        """Owner deleting a shared prompt removes non-owners' active references."""
        prompt = _create_prompt(db_session, normal_user, shared=True)

        # other_user activates the shared prompt.
        activate = client.post(
            "/api/prompts/active/set",
            json={"prompt_id": str(prompt.uuid)},
            headers=other_user_auth_headers,
        )
        assert activate.status_code == 200

        setting = (
            db_session.query(UserSetting)
            .filter(
                UserSetting.user_id == other_user.id,
                UserSetting.setting_key == "active_summary_prompt_id",
                UserSetting.setting_value == str(prompt.id),
            )
            .first()
        )
        assert setting is not None

        # Owner deletes the prompt.
        resp = client.delete(f"/api/prompts/{prompt.uuid}", headers=user_token_headers)
        assert resp.status_code == 200

        db_session.expire_all()
        setting = (
            db_session.query(UserSetting)
            .filter(
                UserSetting.user_id == other_user.id,
                UserSetting.setting_key == "active_summary_prompt_id",
                UserSetting.setting_value == str(prompt.id),
            )
            .first()
        )
        assert setting is None
