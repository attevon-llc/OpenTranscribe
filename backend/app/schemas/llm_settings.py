"""
Pydantic schemas for user LLM settings
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel
from pydantic import field_validator

from app.core.enums import ReasoningOffSwitch
from app.schemas.base import UUIDBaseSchema


class LLMProvider(StrEnum):
    """Supported LLM providers"""

    OPENAI = "openai"
    VLLM = "vllm"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    CUSTOM = "custom"
    # Legacy - kept for backward compatibility with existing database records
    CLAUDE = "claude"  # Deprecated: use ANTHROPIC instead


class ConnectionStatus(StrEnum):
    """Connection test status"""

    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"
    UNTESTED = "untested"


class UserLLMSettingsBase(BaseModel):
    """Base schema for user LLM settings"""

    name: str
    provider: LLMProvider
    model_name: str
    base_url: str | None = None
    max_tokens: int = 8192
    temperature: str = "0.3"
    is_active: bool = True
    is_shared: bool = False

    @field_validator("max_tokens")
    @classmethod
    def validate_max_tokens(cls, v):
        if v < 512 or v > 2000000:
            raise ValueError("max_tokens must be between 512 and 2,000,000")
        return v

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, v):
        try:
            temp_float = float(v)
        except ValueError as e:
            raise ValueError("temperature must be a valid number") from e
        if temp_float < 0.0 or temp_float > 2.0:
            raise ValueError("temperature must be between 0.0 and 2.0")
        return v


class UserLLMSettingsCreate(UserLLMSettingsBase):
    """Schema for creating user LLM settings"""

    api_key: str | None = None


class UserLLMSettingsUpdate(BaseModel):
    """Schema for updating user LLM settings"""

    name: str | None = None
    provider: LLMProvider | None = None
    model_name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int | None = None
    temperature: str | None = None
    is_active: bool | None = None
    is_shared: bool | None = None

    @field_validator("max_tokens")
    @classmethod
    def validate_max_tokens(cls, v):
        if v is not None and (v < 512 or v > 2000000):
            raise ValueError("max_tokens must be between 512 and 2,000,000")
        return v

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, v):
        if v is not None:
            try:
                temp_float = float(v)
            except ValueError as e:
                raise ValueError("temperature must be a valid number") from e
            if temp_float < 0.0 or temp_float > 2.0:
                raise ValueError("temperature must be between 0.0 and 2.0")
        return v


class UserLLMSettings(UserLLMSettingsBase, UUIDBaseSchema):
    """Schema for returning user LLM settings with UUID"""

    user_id: UUID
    last_tested: datetime | None = None
    test_status: ConnectionStatus | None = None
    test_message: str | None = None
    created_at: datetime
    updated_at: datetime


class UserLLMSettingsPublic(UUIDBaseSchema):
    """Public schema that excludes sensitive information"""

    user_id: UUID
    name: str
    provider: LLMProvider
    model_name: str
    base_url: str | None = None
    max_tokens: int
    temperature: str
    is_active: bool
    last_tested: datetime | None = None
    test_status: ConnectionStatus | None = None
    test_message: str | None = None
    has_api_key: bool = False
    is_shared: bool = False
    shared_at: datetime | None = None
    owner_name: str | None = None
    owner_role: str | None = None
    is_own: bool = True
    created_at: datetime
    updated_at: datetime


class ReasoningCapability(BaseModel):
    """A model's MEASURED reasoning off-switch capability (issue #64).

    ``off_switch`` is the only field a client should branch on, and only
    ``"works"`` may render a control. The character counts are the evidence and
    exist so an operator can see *why* a toggle is absent; they are diagnostics,
    not a contract.

    ``probed_at`` is None when this model has never been probed — which is the
    default for every deployment, and renders no control.
    """

    off_switch: ReasoningOffSwitch = ReasoningOffSwitch.UNKNOWN
    probeable: bool = False
    probed_at: datetime | None = None
    reasoning_chars_on: int = 0
    reasoning_chars_off: int = 0
    reasoning_chars_omitted: int = 0
    detail: str = ""


class ConnectionTestRequest(BaseModel):
    """Schema for connection test requests"""

    provider: LLMProvider
    model_name: str
    api_key: str | None = None
    base_url: str | None = None
    config_id: UUID | None = None  # For edit mode - uses stored API key


class ConnectionTestResponse(BaseModel):
    """Schema for connection test results"""

    success: bool
    status: ConnectionStatus
    message: str
    response_time_ms: int | None = None
    model_info: dict | None = None


class ProviderDefaults(BaseModel):
    """Default configuration for a provider"""

    provider: LLMProvider
    default_model: str
    default_base_url: str | None = None
    requires_api_key: bool = True
    supports_custom_url: bool = True
    max_context_length: int | None = None
    description: str


class SupportedProvidersResponse(BaseModel):
    """Response containing all supported providers with their defaults"""

    providers: list[ProviderDefaults]


class UserLLMConfigurationsList(BaseModel):
    """Response containing all user's LLM configurations"""

    configurations: list[UserLLMSettingsPublic]
    shared_configurations: list[UserLLMSettingsPublic] = []
    active_configuration_id: UUID | None = None
    total: int
    #: Measured reasoning off-switch verdict per configuration uuid (issue #64).
    #: A side map rather than a field on the configuration, because the fact
    #: belongs to the (provider, endpoint, model) triple and not to the row —
    #: two configurations naming the same vLLM model share one measurement, and
    #: editing a row's model silently changes which measurement applies.
    #: **A uuid absent from this map has never been probed**; treat it as
    #: "unknown", which renders no control.
    reasoning_off_switch: dict[str, ReasoningOffSwitch] = {}


class SetActiveConfigRequest(BaseModel):
    """Request to set active LLM configuration"""

    configuration_id: UUID


class LLMSettingsStatus(BaseModel):
    """Status information about user's LLM settings"""

    has_settings: bool = False
    active_configuration: UserLLMSettingsPublic | None = None
    total_configurations: int = 0
    using_system_default: bool = True
