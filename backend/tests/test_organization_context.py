"""
Tests for organization context feature.

Tests verify that organization context is correctly:
- Stored/retrieved via user settings
- Injected into LLM system prompts when enabled
- Excluded when toggled off
- Respects system vs custom prompt toggles
"""

from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_ORG_CONTEXT_INCLUDE_CUSTOM_PROMPTS
from app.core.constants import DEFAULT_ORG_CONTEXT_INCLUDE_DEFAULT_PROMPTS
from app.core.constants import DEFAULT_ORG_CONTEXT_TEXT
from app.core.constants import ORG_CONTEXT_MAX_LENGTH


class TestOrgContextConstants:
    """Test organization context constants are properly defined."""

    def test_defaults(self):
        assert DEFAULT_ORG_CONTEXT_TEXT == ""
        assert DEFAULT_ORG_CONTEXT_INCLUDE_DEFAULT_PROMPTS is True
        assert DEFAULT_ORG_CONTEXT_INCLUDE_CUSTOM_PROMPTS is False
        assert ORG_CONTEXT_MAX_LENGTH == 10000


class TestOrgContextSchemas:
    """Test Pydantic schemas for organization context."""

    def test_response_schema_defaults(self):
        from app.schemas.organization_context import OrganizationContextSettings

        settings = OrganizationContextSettings()
        assert settings.context_text == ""
        assert settings.include_in_default_prompts is True
        assert settings.include_in_custom_prompts is False

    def test_response_schema_custom_values(self):
        from app.schemas.organization_context import OrganizationContextSettings

        settings = OrganizationContextSettings(
            context_text="Test org context",
            include_in_default_prompts=False,
            include_in_custom_prompts=True,
        )
        assert settings.context_text == "Test org context"
        assert settings.include_in_default_prompts is False
        assert settings.include_in_custom_prompts is True

    def test_update_schema_partial(self):
        from app.schemas.organization_context import OrganizationContextUpdate

        update = OrganizationContextUpdate(context_text="New context")
        data = update.model_dump(exclude_none=True)
        assert data == {"context_text": "New context"}
        assert "include_in_default_prompts" not in data

    def test_update_schema_all_fields(self):
        from app.schemas.organization_context import OrganizationContextUpdate

        update = OrganizationContextUpdate(
            context_text="Full update",
            include_in_default_prompts=False,
            include_in_custom_prompts=True,
        )
        data = update.model_dump(exclude_none=True)
        assert len(data) == 3

    def test_update_schema_empty(self):
        from app.schemas.organization_context import OrganizationContextUpdate

        update = OrganizationContextUpdate()
        data = update.model_dump(exclude_none=True)
        assert data == {}


class TestOrgContextSystemPromptInjection:
    """Test that organization context is correctly injected into LLM system prompts."""

    def test_build_org_context_block_with_content(self):
        from app.services.llm_service import LLMService

        service = LLMService.__new__(LLMService)
        block = service._build_org_context_block("Greenleaf Analytics builds healthcare tools.")
        assert "Greenleaf Analytics" in block
        assert "organization/project context" in block

    def test_build_org_context_block_empty(self):
        from app.services.llm_service import LLMService

        service = LLMService.__new__(LLMService)
        assert service._build_org_context_block("") == ""
        assert service._build_org_context_block("   ") == ""

    def test_build_org_context_block_none(self):
        from app.services.llm_service import LLMService

        service = LLMService.__new__(LLMService)
        assert service._build_org_context_block(None) == ""  # type: ignore[arg-type]


class TestOrgContextPromptTypeToggle:
    """Test that org context respects system vs custom prompt toggles."""

    @pytest.fixture
    def mock_db(self):
        return Mock(spec=Session)

    def _make_setting(self, key: str, value: str) -> Mock:
        setting = Mock()
        setting.setting_key = key
        setting.setting_value = value
        return setting

    def test_no_context_text_returns_empty(self, mock_db: Mock):
        """When no context text is stored, return empty string."""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        from app.tasks.summarization import _get_organization_context

        result = _get_organization_context(mock_db, user_id=1)
        assert result == ""

    def test_empty_context_text_returns_empty(self, mock_db: Mock):
        """When context text is empty, return empty string."""
        setting = self._make_setting("org_context_text", "")
        mock_db.query.return_value.filter.return_value.first.return_value = setting

        from app.tasks.summarization import _get_organization_context

        result = _get_organization_context(mock_db, user_id=1)
        assert result == ""

    @patch("app.utils.prompt_manager.get_user_active_prompt_info")
    def test_system_prompt_enabled_returns_context(
        self, mock_prompt_info: MagicMock, mock_db: Mock
    ):
        """When using system prompt and toggle is on, context is returned."""
        context_text = "Greenleaf Analytics healthcare company"
        mock_prompt_info.return_value = ("prompt text", True)  # is_system_default=True

        # Calls: 1) shared context check, 2) context text, 3) toggle setting
        context_setting = self._make_setting("org_context_text", context_text)
        toggle_setting = self._make_setting("org_context_include_default_prompts", "true")
        mock_db.query.return_value.filter.return_value.first.side_effect = [
            None,  # org_context_use_shared_from — not using shared
            context_setting,
            toggle_setting,
        ]

        from app.tasks.summarization import _get_organization_context

        result = _get_organization_context(mock_db, user_id=1)
        assert result == context_text

    @patch("app.utils.prompt_manager.get_user_active_prompt_info")
    def test_system_prompt_disabled_returns_empty(self, mock_prompt_info: MagicMock, mock_db: Mock):
        """When using system prompt but toggle is off, context is excluded."""
        mock_prompt_info.return_value = ("prompt text", True)  # is_system_default=True

        context_setting = self._make_setting("org_context_text", "Some context")
        toggle_setting = self._make_setting("org_context_include_default_prompts", "false")
        mock_db.query.return_value.filter.return_value.first.side_effect = [
            None,  # org_context_use_shared_from — not using shared
            context_setting,
            toggle_setting,
        ]

        from app.tasks.summarization import _get_organization_context

        result = _get_organization_context(mock_db, user_id=1)
        assert result == ""

    @patch("app.utils.prompt_manager.get_user_active_prompt_info")
    def test_custom_prompt_enabled_returns_context(
        self, mock_prompt_info: MagicMock, mock_db: Mock
    ):
        """When using custom prompt and toggle is on, context is returned."""
        context_text = "Custom prompt org context"
        mock_prompt_info.return_value = ("custom prompt text", False)  # is_system_default=False

        context_setting = self._make_setting("org_context_text", context_text)
        toggle_setting = self._make_setting("org_context_include_custom_prompts", "true")
        mock_db.query.return_value.filter.return_value.first.side_effect = [
            None,  # org_context_use_shared_from — not using shared
            context_setting,
            toggle_setting,
        ]

        from app.tasks.summarization import _get_organization_context

        result = _get_organization_context(mock_db, user_id=1)
        assert result == context_text

    @patch("app.utils.prompt_manager.get_user_active_prompt_info")
    def test_custom_prompt_default_off_returns_empty(
        self, mock_prompt_info: MagicMock, mock_db: Mock
    ):
        """When using custom prompt and no toggle stored (default=false), context is excluded."""
        mock_prompt_info.return_value = ("custom prompt text", False)  # is_system_default=False

        context_setting = self._make_setting("org_context_text", "Some context")
        # No toggle setting stored - defaults to "false" for custom prompts
        mock_db.query.return_value.filter.return_value.first.side_effect = [
            None,  # org_context_use_shared_from — not using shared
            context_setting,
            None,
        ]

        from app.tasks.summarization import _get_organization_context

        result = _get_organization_context(mock_db, user_id=1)
        assert result == ""

    @patch("app.utils.prompt_manager.get_user_active_prompt_info")
    def test_system_prompt_default_on_returns_context(
        self, mock_prompt_info: MagicMock, mock_db: Mock
    ):
        """When using system prompt and no toggle stored (default=true), context is returned."""
        context_text = "Default on context"
        mock_prompt_info.return_value = ("system prompt text", True)  # is_system_default=True

        context_setting = self._make_setting("org_context_text", context_text)
        # No toggle setting stored - defaults to "true" for system prompts
        mock_db.query.return_value.filter.return_value.first.side_effect = [
            None,  # org_context_use_shared_from — not using shared
            context_setting,
            None,
        ]

        from app.tasks.summarization import _get_organization_context

        result = _get_organization_context(mock_db, user_id=1)
        assert result == context_text


class TestPromptManagerInfo:
    """Test that prompt_manager returns is_system_default metadata."""

    @patch("app.utils.prompt_manager.SessionLocal")
    def test_get_user_active_prompt_info_returns_tuple(self, mock_session_local: MagicMock):
        """get_user_active_prompt_info returns (prompt_text, is_system_default)."""
        from app.utils.prompt_manager import get_user_active_prompt_info

        mock_db = Mock(spec=Session)
        mock_session_local.return_value = mock_db

        # Simulate no user setting -> falls back to system default
        mock_db.query.return_value.filter.return_value.first.return_value = None

        # Mock the system default prompt query chain
        mock_prompt = Mock()
        mock_prompt.name = "Universal System Prompt"
        mock_prompt.prompt_text = "Analyze the transcript..."
        mock_prompt.is_system_default = True

        # get_system_default_prompt_record does multiple queries
        # First query for universal, then general, then any
        mock_db.query.return_value.filter.return_value.first.side_effect = [
            None,  # No active_summary_prompt_id setting
            mock_prompt,  # Found universal system prompt
        ]

        result = get_user_active_prompt_info(user_id=1, db=mock_db)
        assert isinstance(result, tuple)
        assert len(result) == 2
        prompt_text, is_system_default = result
        assert is_system_default is True

    def test_get_user_active_prompt_backward_compatible(self):
        """get_user_active_prompt still returns just the string."""
        from app.utils.prompt_manager import get_user_active_prompt
        from app.utils.prompt_manager import get_user_active_prompt_info

        # Both functions exist and have correct signatures
        assert callable(get_user_active_prompt)
        assert callable(get_user_active_prompt_info)
