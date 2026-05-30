"""
Pydantic schemas for admin settings
"""

import re
from typing import Optional

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator


class RetryConfig(BaseModel):
    """Response schema for retry configuration"""

    max_retries: int
    retry_limit_enabled: bool


class RetryConfigUpdate(BaseModel):
    """Schema for updating retry configuration"""

    max_retries: Optional[int] = None
    retry_limit_enabled: Optional[bool] = None

    @field_validator("max_retries")
    @classmethod
    def validate_max_retries(cls, v):
        if v is not None and (v < 0 or v > 99):
            raise ValueError("max_retries must be between 0 and 99 (0 = unlimited)")
        return v


class SystemSettingResponse(BaseModel):
    """Response schema for a single system setting"""

    key: str
    value: Optional[str]
    description: Optional[str]
    updated_at: Optional[str]


class AllSettingsResponse(BaseModel):
    """Response schema for all system settings"""

    settings: dict[str, dict]


class GarbageCleanupConfig(BaseModel):
    """Response schema for garbage cleanup configuration"""

    garbage_cleanup_enabled: bool
    max_word_length: int


class GarbageCleanupConfigUpdate(BaseModel):
    """Schema for updating garbage cleanup configuration"""

    garbage_cleanup_enabled: Optional[bool] = None
    max_word_length: Optional[int] = None

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
    last_run: Optional[str] = None
    last_run_deleted: int = 0


class RetentionConfigUpdate(BaseModel):
    """Schema for updating file retention configuration"""

    retention_enabled: Optional[bool] = None
    retention_days: Optional[int] = Field(None, ge=1, le=3650)
    delete_error_files: Optional[bool] = None
    run_time: Optional[str] = None
    timezone: Optional[str] = None

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
    completed_at: Optional[str]
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

    hostname: Optional[str] = None
    provider_type: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    verify_ssl: Optional[bool] = None
    label: Optional[str] = None

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
