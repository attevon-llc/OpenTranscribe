"""Pydantic schemas for ASR provider settings."""

import re
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel
from pydantic import field_validator
from pydantic import model_validator


class ASRProvider(str, Enum):
    LOCAL = "local"
    DEEPGRAM = "deepgram"
    ASSEMBLYAI = "assemblyai"
    OPENAI = "openai"
    GOOGLE = "google"
    AZURE = "azure"
    AWS = "aws"
    SPEECHMATICS = "speechmatics"
    GLADIA = "gladia"
    PYANNOTE = "pyannote"


# Maximum length for an API key. Cloud provider keys are typically < 256 characters;
# a generous cap of 8192 bytes blocks resource-exhaustion attacks.
MAX_API_KEY_LEN = 8192

# Azure and AWS regions are curated allowlists — unknown values are rejected.
VALID_AZURE_REGIONS = frozenset(
    {
        "westus",
        "westus2",
        "eastus",
        "eastus2",
        "centralus",
        "northcentralus",
        "southcentralus",
        "westeurope",
        "northeurope",
        "uksouth",
        "ukwest",
        "francecentral",
        "germanywestcentral",
        "switzerlandnorth",
        "australiaeast",
        "australiasoutheast",
        "southeastasia",
        "eastasia",
        "japaneast",
        "japanwest",
        "koreacentral",
        "koreasouth",
        "canadacentral",
        "canadaeast",
        "brazilsouth",
        "southafricanorth",
        "uaenorth",
    }
)
VALID_AWS_REGIONS = frozenset(
    {
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "ca-central-1",
        "ca-west-1",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "eu-central-1",
        "eu-north-1",
        "eu-south-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-southeast-3",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-south-1",
        "ap-east-1",
        "sa-east-1",
        "me-south-1",
        "af-south-1",
    }
)


def _validate_base_url_value(base_url: str | None) -> str | None:
    """Validate that base_url uses http/https scheme (SSRF protection)."""
    if not base_url:
        return base_url
    stripped = base_url.strip()
    if not re.match(r"^https?://", stripped, re.IGNORECASE):
        raise ValueError("base_url must begin with http:// or https://")
    return stripped


def _validate_api_key_length_value(api_key: str | None) -> str | None:
    """Validate that API key does not exceed the safe maximum length."""
    if api_key and len(api_key) > MAX_API_KEY_LEN:
        raise ValueError(f"api_key must not exceed {MAX_API_KEY_LEN} characters")
    return api_key


def _validate_region_for_provider(provider: str | None, region: str | None) -> None:
    """Validate that region is on the allowlist for providers that require one."""
    if not region:
        return
    if provider == "azure" and region not in VALID_AZURE_REGIONS:
        raise ValueError(f"Unknown Azure region '{region}'")
    if provider == "aws" and region not in VALID_AWS_REGIONS:
        raise ValueError(f"Unknown AWS region '{region}'")


class ASRModelInfo(BaseModel):
    id: str
    display_name: str
    description: str
    price_per_min_batch: float | None = None
    price_per_min_stream: float | None = None
    languages: int | None = None
    is_default: bool = False
    is_medical: bool = False
    supports_diarization: bool = False
    supports_vocabulary: bool = False
    supports_translation: bool = False
    supports_confidence: bool = False
    max_file_size_mb: int | None = None


class ASRProviderInfo(BaseModel):
    id: str
    display_name: str
    requires_api_key: bool
    requires_access_key_id: bool = False  # AWS-only: needs an Access Key ID + Secret
    requires_region: bool = False
    supports_custom_url: bool = False
    supports_diarization: bool
    supports_vocabulary: bool
    supports_translation: bool
    description: str = ""
    status: str = "experimental"  # "tested", "experimental", or "coming_soon"
    status_note: str = ""  # Shown as warning when experimental
    diarization_quality: str = ""  # Quality note about built-in diarization
    models: list[ASRModelInfo]


class ASRProviderCatalog(BaseModel):
    providers: list[ASRProviderInfo]


class UserASRSettingsBase(BaseModel):
    name: str
    provider: ASRProvider
    model_name: str
    base_url: str | None = None
    region: str | None = None
    access_key_id: str | None = None  # AWS access key ID (an identifier, not a secret)
    is_active: bool = True
    is_shared: bool = False

    @field_validator("access_key_id")
    @classmethod
    def validate_access_key_id(cls, v: str | None) -> str | None:
        if v and len(v.strip()) > 200:
            raise ValueError("access_key_id must not exceed 200 characters")
        return v.strip() if v else v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be empty")
        if len(v) > 100:
            raise ValueError("Name must be 100 characters or less")
        return v

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str | None) -> str | None:
        return _validate_base_url_value(v)


class UserASRSettingsCreate(UserASRSettingsBase):
    api_key: str | None = None

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str | None) -> str | None:
        return _validate_api_key_length_value(v)

    @model_validator(mode="after")
    def validate_region_for_provider(self) -> "UserASRSettingsCreate":
        _validate_region_for_provider(self.provider.value, self.region)
        return self


class UserASRSettingsUpdate(BaseModel):
    name: str | None = None
    provider: ASRProvider | None = None
    model_name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    is_active: bool | None = None
    is_shared: bool | None = None

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str | None) -> str | None:
        return _validate_api_key_length_value(v)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str | None) -> str | None:
        return _validate_base_url_value(v)


class UserASRSettingsResponse(UserASRSettingsBase):
    id: int
    uuid: UUID
    user_id: int
    has_api_key: bool
    last_tested: datetime | None = None
    test_status: str | None = None
    test_message: str | None = None
    is_shared: bool = False
    shared_at: datetime | None = None
    owner_name: str | None = None
    owner_role: str | None = None
    is_own: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ASRSettingsList(BaseModel):
    configs: list[UserASRSettingsResponse]
    shared_configs: list[UserASRSettingsResponse] = []
    active_config_id: int | None = None
    active_config_uuid: UUID | None = None


class ASRConnectionTestRequest(BaseModel):
    provider: ASRProvider
    api_key: str | None = None
    access_key_id: str | None = None  # AWS access key ID (Secret arrives via api_key)
    base_url: str | None = None
    region: str | None = None
    model_name: str | None = None

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str | None) -> str | None:
        return _validate_api_key_length_value(v)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str | None) -> str | None:
        return _validate_base_url_value(v)

    @model_validator(mode="after")
    def validate_region_for_provider(self) -> "ASRConnectionTestRequest":
        _validate_region_for_provider(self.provider.value, self.region)
        return self


class ASRConnectionTestResult(BaseModel):
    success: bool
    message: str
    # response_time_ms is the canonical field name used internally
    response_time_ms: float | None = None

    # latency_ms is an alias for API consumers that expect the spec name
    @property
    def latency_ms(self) -> float | None:
        return self.response_time_ms


class SetActiveASRConfigRequest(BaseModel):
    # Both fields are optional so callers can send either name.
    # At least one must be provided; endpoint validation enforces this.
    config_uuid: UUID | None = None  # canonical field name
    uuid: UUID | None = None  # spec-compatibility alias

    def resolved_uuid(self) -> UUID | None:
        """Return whichever of config_uuid / uuid was provided, preferring config_uuid."""
        return self.config_uuid or self.uuid


class ASRStatusResponse(BaseModel):
    has_settings: bool
    active_config: UserASRSettingsResponse | None = None
    using_local_default: bool
    deployment_mode: str
    asr_configured: bool
    # Convenience fields so callers don't have to unpack active_config
    active_provider: str | None = None
    active_model: str | None = None
    active_config_uuid: UUID | None = None
    is_cloud_provider: bool = False
