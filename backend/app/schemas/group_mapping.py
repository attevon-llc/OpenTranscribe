"""Pydantic schemas for IdP group mappings (``v376``).

Wire contract for ``/api/admin/group-mappings``. Two rules are enforced here as
well as in the service and the database, because each layer is reachable without
the others:

* ``grants_role`` is limited to ``user`` / ``admin`` — ``super_admin`` is not a
  value any identity provider may assert.
* A mapping must grant *something*: a group, a role, or both.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

#: Mirrors ``ck_group_mapping_source_valid`` / ``ck_group_mapping_role_capped``.
SOURCE_PATTERN = "^(ldap|oidc)$"
ROLE_PATTERN = "^(user|admin)$"


class GroupMappingBase(BaseModel):
    """Fields common to create and update."""

    source: str = Field(..., pattern=SOURCE_PATTERN)
    claim_value: str = Field(..., min_length=1, max_length=1024)
    group_uuid: UUID | None = None
    grants_role: str | None = Field(None, pattern=ROLE_PATTERN)
    description: str | None = Field(None, max_length=2000)


class GroupMappingCreate(GroupMappingBase):
    @model_validator(mode="after")
    def _grants_something(self) -> GroupMappingCreate:
        if self.group_uuid is None and self.grants_role is None:
            raise ValueError("A mapping must set group_uuid, grants_role, or both")
        return self


class GroupMappingUpdate(BaseModel):
    """All-optional patch. ``group_uuid``/``grants_role`` can only be cleared to
    ``None`` while the other half remains set — the service re-checks the pair."""

    claim_value: str | None = Field(None, min_length=1, max_length=1024)
    group_uuid: UUID | None = None
    grants_role: str | None = Field(None, pattern=ROLE_PATTERN)
    description: str | None = Field(None, max_length=2000)


class GroupMapping(BaseModel):
    """A mapping as served. ``group_name`` saves the UI a second lookup."""

    uuid: UUID
    source: str
    claim_value: str
    group_uuid: UUID | None = None
    group_name: str | None = None
    grants_role: str | None = None
    description: str | None = None
    member_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MappingTestRequest(BaseModel):
    """Ask what a claim list — or a real directory user — would resolve to.

    Exactly one of ``claim_values`` and ``username`` is used. ``username`` is LDAP
    only: OIDC group membership is asserted inside a token, and there is no
    provider-neutral way to look it up for an arbitrary user without one.
    """

    source: str = Field(..., pattern=SOURCE_PATTERN)
    claim_values: list[str] | None = None
    username: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _one_input(self) -> MappingTestRequest:
        if bool(self.claim_values) == bool(self.username):
            raise ValueError("Provide exactly one of claim_values or username")
        return self


class MappingTestGroup(BaseModel):
    """One group a test subject would land in."""

    uuid: UUID
    name: str


class MappingTestResponse(BaseModel):
    """What the subject resolves to, plus the claims that did not match anything."""

    source: str
    claim_values: list[str]
    matched_claims: list[str]
    unmatched_claims: list[str]
    groups: list[MappingTestGroup]
    grants_role: str | None = None
    legacy_admin: bool = False
    effective_role: str
