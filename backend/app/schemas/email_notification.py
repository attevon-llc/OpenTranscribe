"""Pydantic schemas for watch-source email notification configs."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator


class EmailProvider(StrEnum):
    """Supported email notification providers."""

    SMTP = "smtp"
    M365 = "m365"
    EXCHANGE = "exchange"


class EmailConfigCreate(BaseModel):
    """Create an email notification config (secrets plaintext on write)."""

    name: str = Field(..., min_length=1, max_length=200)
    provider: EmailProvider
    is_enabled: bool = True
    from_address: str | None = None
    default_recipients: str | None = None  # CSV

    # SMTP
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_use_tls: bool = True
    smtp_username: str | None = None
    smtp_password: str | None = None

    # M365
    m365_tenant_id: str | None = None
    m365_client_id: str | None = None
    m365_client_secret: str | None = None

    # Exchange
    exchange_server: str | None = None
    exchange_domain: str | None = None
    exchange_username: str | None = None
    exchange_password: str | None = None

    @model_validator(mode="after")
    def _validate_per_provider(self) -> "EmailConfigCreate":
        if self.provider == EmailProvider.SMTP:
            if not self.smtp_host:
                raise ValueError("SMTP provider requires smtp_host")
        elif self.provider == EmailProvider.M365:
            missing = [f for f in ("m365_tenant_id", "m365_client_id") if not getattr(self, f)]
            if missing:
                raise ValueError(f"M365 provider requires: {', '.join(missing)}")
        elif self.provider == EmailProvider.EXCHANGE and not self.exchange_server:
            raise ValueError("Exchange provider requires exchange_server")
        if not self.from_address:
            raise ValueError("from_address is required")
        return self


class EmailConfigUpdate(BaseModel):
    """Update an email config. All fields optional; provider is immutable."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_enabled: bool | None = None
    from_address: str | None = None
    default_recipients: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_use_tls: bool | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    m365_tenant_id: str | None = None
    m365_client_id: str | None = None
    m365_client_secret: str | None = None
    exchange_server: str | None = None
    exchange_domain: str | None = None
    exchange_username: str | None = None
    exchange_password: str | None = None


class EmailConfigResponse(BaseModel):
    """Email config as returned by the API — never includes secrets."""

    uuid: str
    name: str
    provider: str
    is_enabled: bool = True
    from_address: str | None = None
    default_recipients: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_use_tls: bool = True
    smtp_username: str | None = None
    has_smtp_password: bool = False
    m365_tenant_id: str | None = None
    m365_client_id: str | None = None
    has_m365_secret: bool = False
    exchange_server: str | None = None
    exchange_domain: str | None = None
    exchange_username: str | None = None
    has_exchange_password: bool = False
    last_tested_at: datetime | None = None
    test_status: str | None = None
    test_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class EmailConfigsList(BaseModel):
    """List of email notification configs."""

    configs: list[EmailConfigResponse] = []


class EmailTestResponse(BaseModel):
    """Result of testing an email config (connection or send)."""

    success: bool
    message: str


class AuthMailDesignationUpdate(BaseModel):
    """Designate the config that carries transactional auth mail.

    An empty string (or ``null``) clears the designation, which is a legitimate
    choice meaning "use the ``SMTP_*`` env transport".
    """

    config_uuid: str | None = Field(
        default=None,
        description="UUID of an existing, enabled email config; empty clears the designation",
    )


class AuthMailDesignationResponse(BaseModel):
    """The designation plus whether it still resolves.

    ``config_uuid`` alone cannot tell the UI whether auth mail works: the row may
    have been deleted or disabled after it was designated. ``status`` /
    ``resolves`` carry that, and ``env_smtp_configured`` says whether the
    fallback transport exists at all.
    """

    config_uuid: str | None = None
    config_name: str | None = None
    provider: str | None = None
    is_enabled: bool | None = None
    resolves: bool = False
    status: Literal["not_designated", "active", "missing", "disabled"]
    env_smtp_configured: bool = False
