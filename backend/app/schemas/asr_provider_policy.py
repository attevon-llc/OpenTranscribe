"""Wire shapes of ``/api/admin/asr-providers`` (issue #1246, WP6)."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field


class SharedCredential(BaseModel):
    """An admin-shared ASR configuration. Flags only: a key is never returned."""

    config_uuid: str
    name: str
    has_api_key: bool
    has_access_key_id: bool
    owner_name: str | None = None


class ProviderPolicyEntry(BaseModel):
    id: str
    display_name: str
    allowed_for_users: bool
    shared_credentials: list[SharedCredential] = Field(default_factory=list)


class ProviderPolicyResponse(BaseModel):
    providers: list[ProviderPolicyEntry]
    users_may_add_own_keys: bool


class ProviderPolicyUpdate(BaseModel):
    allowed_for_users: list[str] | None = Field(
        default=None,
        description="Cloud provider ids users may choose. null allows every provider.",
        max_length=64,
    )
    users_may_add_own_keys: bool = True
