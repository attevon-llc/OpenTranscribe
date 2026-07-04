"""Pydantic schemas for user media source settings."""

import re
from datetime import datetime

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

ALLOWED_PROVIDER_TYPES = {"mediacms"}

# Bare hostnames that map to internal Docker/Kubernetes services
_RESERVED_HOSTNAMES = {
    "redis",
    "postgres",
    "postgresql",
    "minio",
    "opensearch",
    "elasticsearch",
    "celery",
    "flower",
    "nginx",
    "backend",
    "frontend",
    "localhost",
    "kibana",
    "grafana",
    "prometheus",
    "vault",
    "consul",
    "etcd",
    "kubernetes",
    "docker",
    "host",
    "gateway",
}


def _validate_hostname_value(v: str) -> str:
    """Shared hostname validation logic. Accepts host or host:port."""
    v = v.strip().lower()
    # Split off optional port
    host_part = v
    if ":" in v:
        parts = v.rsplit(":", 1)
        host_part = parts[0]
        port_str = parts[1]
        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            raise ValueError("Invalid port number (must be 1-65535)")
    if not re.match(
        r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$",
        host_part,
    ):
        raise ValueError("Invalid hostname format")
    # SSRF protection: require at least one dot (reject bare service names)
    if "." not in host_part:
        raise ValueError("Hostname must be a fully qualified domain name (e.g., media.example.com)")
    # Block known internal service names even with dots (e.g., kubernetes.default.svc)
    first_label = host_part.split(".")[0]
    if first_label in _RESERVED_HOSTNAMES:
        raise ValueError(f"Hostname '{v}' is not allowed (reserved internal name)")
    return v


def _validate_provider_type_value(v: str) -> str:
    """Shared provider_type validation logic."""
    if v not in ALLOWED_PROVIDER_TYPES:
        raise ValueError(
            f"Unsupported provider type. Must be one of: {', '.join(sorted(ALLOWED_PROVIDER_TYPES))}"
        )
    return v


class UserMediaSourceCreate(BaseModel):
    """Schema for creating a new user media source."""

    hostname: str = Field(..., min_length=1, max_length=255)
    provider_type: str = Field(default="mediacms", max_length=50)
    username: str = Field(default="")
    password: str = Field(default="")
    verify_ssl: bool = True
    label: str = Field(default="", max_length=200)

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str) -> str:
        return _validate_hostname_value(v)

    @field_validator("provider_type")
    @classmethod
    def validate_provider_type(cls, v: str) -> str:
        return _validate_provider_type_value(v)


class UserMediaSourceUpdate(BaseModel):
    """Schema for updating a media source."""

    hostname: str | None = None
    provider_type: str | None = None
    username: str | None = None
    password: str | None = None
    verify_ssl: bool | None = None
    label: str | None = None
    is_shared: bool | None = None

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_hostname_value(v)
        return v

    @field_validator("provider_type")
    @classmethod
    def validate_provider_type(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_provider_type_value(v)
        return v


class UserMediaSourceResponse(BaseModel):
    """Response schema for a user media source."""

    uuid: str
    hostname: str
    provider_type: str
    username: str = ""
    has_credentials: bool = False
    verify_ssl: bool = True
    label: str = ""
    is_active: bool = True
    is_shared: bool = False
    shared_at: datetime | None = None
    owner_name: str | None = None
    owner_role: str | None = None
    is_own: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserMediaSourcesList(BaseModel):
    """Response schema for the list of user media sources."""

    sources: list[UserMediaSourceResponse] = []
    shared_sources: list[UserMediaSourceResponse] = []
