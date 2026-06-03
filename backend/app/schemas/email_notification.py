"""Pydantic schemas for watch-source email notification configs."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator


class EmailProvider(str, Enum):
    """Supported email notification providers."""

    SMTP = "smtp"
    M365 = "m365"
    EXCHANGE = "exchange"


class EmailConfigCreate(BaseModel):
    """Create an email notification config (secrets plaintext on write)."""

    name: str = Field(..., min_length=1, max_length=200)
    provider: EmailProvider
    is_enabled: bool = True
    from_address: Optional[str] = None
    default_recipients: Optional[str] = None  # CSV

    # SMTP
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = Field(default=None, ge=1, le=65535)
    smtp_use_tls: bool = True
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None

    # M365
    m365_tenant_id: Optional[str] = None
    m365_client_id: Optional[str] = None
    m365_client_secret: Optional[str] = None

    # Exchange
    exchange_server: Optional[str] = None
    exchange_domain: Optional[str] = None
    exchange_username: Optional[str] = None
    exchange_password: Optional[str] = None

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

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    is_enabled: Optional[bool] = None
    from_address: Optional[str] = None
    default_recipients: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = Field(default=None, ge=1, le=65535)
    smtp_use_tls: Optional[bool] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    m365_tenant_id: Optional[str] = None
    m365_client_id: Optional[str] = None
    m365_client_secret: Optional[str] = None
    exchange_server: Optional[str] = None
    exchange_domain: Optional[str] = None
    exchange_username: Optional[str] = None
    exchange_password: Optional[str] = None


class EmailConfigResponse(BaseModel):
    """Email config as returned by the API — never includes secrets."""

    uuid: str
    name: str
    provider: str
    is_enabled: bool = True
    from_address: Optional[str] = None
    default_recipients: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_use_tls: bool = True
    smtp_username: Optional[str] = None
    has_smtp_password: bool = False
    m365_tenant_id: Optional[str] = None
    m365_client_id: Optional[str] = None
    has_m365_secret: bool = False
    exchange_server: Optional[str] = None
    exchange_domain: Optional[str] = None
    exchange_username: Optional[str] = None
    has_exchange_password: bool = False
    last_tested_at: Optional[datetime] = None
    test_status: Optional[str] = None
    test_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class EmailConfigsList(BaseModel):
    """List of email notification configs."""

    configs: list[EmailConfigResponse] = []


class EmailTestResponse(BaseModel):
    """Result of testing an email config (connection or send)."""

    success: bool
    message: str
