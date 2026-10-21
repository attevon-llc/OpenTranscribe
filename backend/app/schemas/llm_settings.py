"""
Pydantic schemas for user LLM settings
"""

import re
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from app.core.enums import ContextWindowStatus
from app.core.enums import ReasoningOffSwitch
from app.schemas.base import UUIDBaseSchema


class LLMProvider(StrEnum):
    """Supported LLM providers"""

    OPENAI = "openai"
    VLLM = "vllm"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    #: AWS-native, reached through boto3's Converse API (see
    #: ``services/llm_bedrock.py``). No ``base_url``. A configuration may carry its own
    #: region and AWS credentials (write-only, stored encrypted); blank means the
    #: server's region and boto3 credential chain (``services/bedrock_settings.py``).
    BEDROCK = "bedrock"
    CUSTOM = "custom"
    # Legacy - kept for backward compatibility with existing database records
    CLAUDE = "claude"  # Deprecated: use ANTHROPIC instead


class ConnectionStatus(StrEnum):
    """Connection test status"""

    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"
    UNTESTED = "untested"


#: AWS region names: ``us-east-1``, ``eu-central-1``, ``us-gov-west-1``, ``ap-southeast-2``.
AWS_REGION_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
AWS_ACCESS_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{16,128}$")
AWS_SECRET_PATTERN = re.compile(r"^\S{16,256}$")
AWS_SESSION_TOKEN_MAX = 4096


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class BedrockCredentialFields(BaseModel):
    """Write-only AWS fields for a ``bedrock`` configuration.

    None of these is ever returned by an endpoint: the public schema carries only
    ``aws_region`` and the ``has_aws_credentials`` flag. ``repr=False`` keeps them out of
    any logged model.
    """

    aws_region: str | None = None
    aws_access_key_id: str | None = Field(default=None, repr=False)
    aws_secret_access_key: str | None = Field(default=None, repr=False)
    aws_session_token: str | None = Field(default=None, repr=False)

    @field_validator("aws_region", mode="before")
    @classmethod
    def _valid_region(cls, v):
        v = _blank_to_none(v)
        if v is not None and not AWS_REGION_PATTERN.fullmatch(v):
            raise ValueError("aws_region must look like us-east-1")
        return v

    @field_validator("aws_access_key_id", mode="before")
    @classmethod
    def _valid_key_id(cls, v):
        v = _blank_to_none(v)
        if v is not None and not AWS_ACCESS_KEY_ID_PATTERN.fullmatch(v):
            raise ValueError("aws_access_key_id is not a valid access key id")
        return v

    @field_validator("aws_secret_access_key", mode="before")
    @classmethod
    def _valid_secret(cls, v):
        v = _blank_to_none(v)
        if v is not None and not AWS_SECRET_PATTERN.fullmatch(v):
            raise ValueError("aws_secret_access_key is not a valid secret access key")
        return v

    @field_validator("aws_session_token", mode="before")
    @classmethod
    def _valid_token(cls, v):
        v = _blank_to_none(v)
        if v is not None and (len(v) > AWS_SESSION_TOKEN_MAX or any(c.isspace() for c in v)):
            raise ValueError("aws_session_token is not a valid session token")
        return v

    @model_validator(mode="after")
    def _key_pair_together(self):
        if bool(self.aws_access_key_id) != bool(self.aws_secret_access_key):
            raise ValueError("aws_access_key_id and aws_secret_access_key must be given together")
        if self.aws_session_token and not self.aws_access_key_id:
            raise ValueError("aws_session_token needs an access key id and secret access key")
        return self

    @property
    def has_aws_fields(self) -> bool:
        """True when the request carries any AWS value to store."""
        return bool(
            self.aws_region
            or self.aws_access_key_id
            or self.aws_secret_access_key
            or self.aws_session_token
        )


class UserLLMSettingsBase(BaseModel):
    """Base schema for user LLM settings"""

    name: str
    provider: LLMProvider
    model_name: str
    base_url: str | None = None
    #: ⚠️ This is the model's CONTEXT WINDOW, not a response cap (issue #533).
    #: `LLMService.__init__` reads it as `user_context_window`, derives the
    #: response budget from a quarter of it, and the chat excerpt budget — how
    #: much transcript the model ever sees — is computed against it. The name
    #: survives for wire compatibility; the misreading it invites is exactly
    #: what let this 8192 default silently drive 60k/256k models at 8k. The
    #: measured ceiling comes from `services/llm_context_window.probe`.
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


class UserLLMSettingsCreate(UserLLMSettingsBase, BedrockCredentialFields):
    """Schema for creating user LLM settings"""

    api_key: str | None = None


class UserLLMSettingsUpdate(BedrockCredentialFields):
    """Schema for updating user LLM settings"""

    #: Drop the configuration's own AWS credentials so it uses the server's credential chain.
    clear_aws_credentials: bool = False

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
    #: Bedrock only: the configuration's region (not a secret) and whether it stores its own
    #: AWS credentials. The credentials themselves are never returned.
    aws_region: str | None = None
    has_aws_credentials: bool = False
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


class ContextWindowCapability(BaseModel):
    """A model's MEASURED maximum context window (issue #533).

    ``status`` is the field to branch on; only ``"measured"`` carries a
    ``context_window``. ``relation`` compares the measurement to the row's
    configured ``max_tokens`` — which **is** the context window the app drives
    the model at (see ``LLMService.__init__``), the misreading that let 8192
    look reasonable:

    * ``"below"`` — configured under the measured maximum: every chat turn is
      leaving context (and therefore transcript excerpts) unused.
    * ``"above"`` — configured over it: requests will 400 or silently truncate.
    * ``"match"`` — configured exactly at the measured maximum.

    ``relation`` is ``None`` whenever there is no measurement to compare to.
    """

    status: ContextWindowStatus = ContextWindowStatus.UNKNOWN
    discoverable: bool = False
    context_window: int | None = None
    configured_max_tokens: int | None = None
    relation: str | None = None
    probed_at: datetime | None = None
    detail: str = ""


class ConnectionTestRequest(BedrockCredentialFields):
    """Schema for connection test requests"""

    provider: LLMProvider
    model_name: str
    api_key: str | None = None
    base_url: str | None = None
    config_id: UUID | None = None  # For edit mode - uses stored API key
    # Bedrock: the entered (unsaved) aws_* values come from BedrockCredentialFields; blank
    # ones fall back to the stored configuration's (edit mode), then to the server's chain.


class ConnectionTestResponse(BaseModel):
    """Schema for connection test results"""

    success: bool
    status: ConnectionStatus
    message: str
    response_time_ms: int | None = None
    model_info: dict | None = None


class BedrockServerCredentialStatus(BaseModel):
    """Whether the SERVER's own AWS credential chain resolves, and the kind of source.

    Never a value: no key, secret or token is read, only which provider of the chain answered.
    """

    found: bool
    source: str | None = None
    region: str | None = None


class ProviderDefaults(BaseModel):
    """Default configuration for a provider"""

    provider: LLMProvider
    default_model: str
    default_base_url: str | None = None
    requires_api_key: bool = True
    supports_custom_url: bool = True
    max_context_length: int | None = None
    description: str
    #: Suggestions for a provider's free-text fields (Bedrock: AWS regions, model ids).
    suggested_regions: list[str] = []
    suggested_models: list[str] = []


class SupportedProvidersResponse(BaseModel):
    """Response containing all supported providers with their defaults"""

    providers: list[ProviderDefaults]


class LocalEndpointStatus(BaseModel):
    """One row of the fixed local-endpoint discovery table (issue #644).

    ``base_url`` is always a container-network address (a compose service name
    like ``llm-test-vllm``), never a host-side ``127.0.0.1:<port>``: inside the
    backend container ``127.0.0.1`` is the backend itself, and
    ``redaction.llm_guard.is_local_provider`` would classify it "local" for
    masking/quota purposes while the config actually points nowhere useful.
    """

    id: str
    label: str
    base_url: str
    provider: LLMProvider
    reachable: bool
    models: list[str] = []
    start_command: str


class LocalEndpointsResponse(BaseModel):
    """Response for ``GET /llm-settings/local-endpoints``."""

    endpoints: list[LocalEndpointStatus]
    #: Mirrors this deployment's OWN ``LLM_ALLOW_PRIVATE_ENDPOINTS`` setting — it
    #: says nothing about any specific target's actual reachability. The discovery
    #: UI uses it to warn that a reachable local server will still be refused at
    #: save/dial time until this is set (issue #644 / #284 A0.1).
    private_endpoints_allowed: bool


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
