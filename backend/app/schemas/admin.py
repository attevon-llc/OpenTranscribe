"""
Pydantic schemas for admin settings
"""

import re

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator


class RetryConfig(BaseModel):
    """Response schema for retry configuration"""

    max_retries: int
    retry_limit_enabled: bool


class RetryConfigUpdate(BaseModel):
    """Schema for updating retry configuration"""

    max_retries: int | None = None
    retry_limit_enabled: bool | None = None

    @field_validator("max_retries")
    @classmethod
    def validate_max_retries(cls, v):
        if v is not None and (v < 0 or v > 99):
            raise ValueError("max_retries must be between 0 and 99 (0 = unlimited)")
        return v


class GarbageCleanupConfig(BaseModel):
    """Response schema for garbage cleanup configuration"""

    garbage_cleanup_enabled: bool
    max_word_length: int


class GarbageCleanupConfigUpdate(BaseModel):
    """Schema for updating garbage cleanup configuration"""

    garbage_cleanup_enabled: bool | None = None
    max_word_length: int | None = None

    @field_validator("max_word_length")
    @classmethod
    def validate_max_word_length(cls, v):
        if v is not None and (v < 20 or v > 200):
            raise ValueError("max_word_length must be between 20 and 200")
        return v


class RetentionConfig(BaseModel):
    """Response schema for file retention configuration"""

    retention_enabled: bool
    retention_days: int
    delete_error_files: bool
    run_time: str
    timezone: str
    last_run: str | None = None
    last_run_deleted: int = 0


class RetentionConfigUpdate(BaseModel):
    """Schema for updating file retention configuration"""

    retention_enabled: bool | None = None
    retention_days: int | None = Field(None, ge=1, le=3650)
    delete_error_files: bool | None = None
    run_time: str | None = None
    timezone: str | None = None

    @field_validator("run_time")
    @classmethod
    def validate_run_time(cls, v):
        if v is not None:
            if not re.match(r"^\d{2}:\d{2}$", v):
                raise ValueError("run_time must be in HH:MM format")
            hour, minute = map(int, v.split(":"))
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError("run_time must be a valid time (00:00–23:59)")
        return v

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v):
        if v is not None:
            try:
                import zoneinfo

                zoneinfo.ZoneInfo(v)
            except (KeyError, zoneinfo.ZoneInfoNotFoundError):
                raise ValueError(f"'{v}' is not a valid IANA timezone") from None
        return v


class RetentionPreviewFile(BaseModel):
    """A single file entry in the retention preview response"""

    uuid: str
    title: str
    owner_email: str
    completed_at: str | None
    age_days: int
    size_bytes: int
    status: str


class RetentionPreviewResponse(BaseModel):
    """Response schema for retention preview (dry-run)"""

    file_count: int
    total_size_bytes: int
    files: list[RetentionPreviewFile]


class RetentionRunResponse(BaseModel):
    """Response schema for a manual retention run trigger"""

    task_id: str
    status: str
    message: str


# ---------------------------------------------------------------------------
# Protected Media Sources
# ---------------------------------------------------------------------------


class MediaSource(BaseModel):
    """A single protected media source configuration."""

    id: str = Field(..., description="Unique identifier for this source")
    hostname: str = Field(..., min_length=1, description="Hostname (e.g. media.example.com)")
    provider_type: str = Field(
        default="mediacms", description="Provider plugin type (mediacms, etc.)"
    )
    username: str = Field(default="", description="Default username for this source")
    password: str = Field(default="", description="Default password for this source")
    verify_ssl: bool = Field(default=True, description="Verify SSL certificates")
    label: str = Field(default="", description="Optional display label")

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$", v):
            raise ValueError("Invalid hostname format")
        return v


class MediaSourceCreate(BaseModel):
    """Schema for creating a new media source."""

    hostname: str = Field(..., min_length=1)
    provider_type: str = Field(default="mediacms")
    username: str = Field(default="")
    password: str = Field(default="")
    verify_ssl: bool = Field(default=True)
    label: str = Field(default="")

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$", v):
            raise ValueError("Invalid hostname format")
        return v


class MediaSourceUpdate(BaseModel):
    """Schema for updating a media source."""

    hostname: str | None = None
    provider_type: str | None = None
    username: str | None = None
    password: str | None = None
    verify_ssl: bool | None = None
    label: str | None = None

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip().lower()
            if not re.match(
                r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$", v
            ):
                raise ValueError("Invalid hostname format")
        return v


class MediaSourcesList(BaseModel):
    """Response schema for the list of media sources."""

    sources: list[MediaSource]


class CacheConfig(BaseModel):
    """Derived-asset cache configuration and current usage."""

    retention_days: int = Field(
        ..., description="Days before derived assets auto-expire (0 = keep forever)"
    )
    bucket: str
    prefix: str
    object_count: int = Field(..., description="Number of cached derived objects")
    total_bytes: int = Field(..., description="Total size of the derived cache in bytes")


class CacheConfigUpdate(BaseModel):
    """Request to change the derived-cache retention window."""

    retention_days: int = Field(
        ..., ge=0, le=3650, description="Days before derived assets auto-expire (0 = keep forever)"
    )


class CacheClearResponse(BaseModel):
    """Result of a manual derived-cache purge."""

    deleted: int = Field(..., description="Number of derived objects removed")


# ===== Abuse / DMCA / safe-harbor takedown =====


class QuarantineRequest(BaseModel):
    """Request to quarantine (take down) a media file for abuse/DMCA."""

    reason: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Takedown reason (DMCA notice reference, AUP clause, abuse report id).",
    )
    legal_hold: bool = Field(
        True,
        description="Also place a legal-hold on the row + S3 object so it can't be deleted.",
    )


class ReleaseRequest(BaseModel):
    """Request to release a quarantined media file."""

    clear_legal_hold: bool = Field(True, description="Also lift the legal-hold when releasing.")


class QuarantinedFile(BaseModel):
    """A taken-down file in the admin review list."""

    uuid: str
    filename: str | None = None
    user_id: int
    organization_id: int | None = None
    quarantine_reason: str | None = None
    quarantined_at: str | None = None
    quarantined_by: int | None = None
    legal_hold: bool = False


class QuarantinedFilesList(BaseModel):
    """Paginated list of quarantined files for admin review."""

    files: list[QuarantinedFile]
    total: int


class QuarantineActionResponse(BaseModel):
    """Result of a quarantine/release action."""

    uuid: str
    is_quarantined: bool
    legal_hold: bool
    status: str


class LinkExternalIdentityRequest(BaseModel):
    """Deliberately link an existing account to an external identity (P1.3).

    The operator remedy referenced by ``auth/account_linking.py``: when a source
    cannot assert ``email_verified`` (Authentik hardcodes it false for every
    account) or an address simply collides, the automatic email-match link is
    refused and the login fails rather than guessing. This is the explicit
    alternative — an administrator sets the provider's own identifier on the
    account, so the *next* login matches by that identifier first and never
    reaches the email-match branch at all.
    """

    provider: str = Field(..., description="'oidc', 'ldap', or 'pki'")
    identifier: str = Field(..., min_length=1, description="The provider's subject/uid/DN to link")

    @field_validator("provider")
    @classmethod
    def _provider_is_linkable(cls, value: str) -> str:
        from app.auth.constants import AUTH_TYPE_LDAP
        from app.auth.constants import AUTH_TYPE_OIDC
        from app.auth.constants import AUTH_TYPE_PKI

        linkable = {AUTH_TYPE_OIDC, AUTH_TYPE_LDAP, AUTH_TYPE_PKI}
        if value not in linkable:
            raise ValueError(f"provider must be one of {sorted(linkable)}")
        return value

    @field_validator("identifier")
    @classmethod
    def _identifier_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("identifier must not be blank")
        return stripped


class LinkExternalIdentityResponse(BaseModel):
    """Result of linking an external identity to an account."""

    success: bool
    provider: str
    identifier: str
