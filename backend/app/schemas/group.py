"""Pydantic schemas for user groups."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.schemas.base import UUIDBaseSchema
from app.schemas.user import UserBrief


class GroupBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)


class GroupCreate(GroupBase):
    pass


class GroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)


class GroupMemberAdd(BaseModel):
    user_uuid: UUID
    role: str = Field("member", pattern="^(admin|member)$")


class GroupMemberUpdate(BaseModel):
    role: str = Field(..., pattern="^(admin|member)$")


class GroupMember(BaseModel):
    """Group member with user info."""

    uuid: UUID  # member record UUID
    user_uuid: UUID
    email: str
    full_name: str | None = None
    role: str
    joined_at: datetime

    model_config = ConfigDict(from_attributes=True)


class Group(UUIDBaseSchema):
    """Group summary for list views."""

    name: str
    description: str | None = None
    member_count: int = 0
    my_role: str = "member"
    owner: UserBrief
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GroupDetail(Group):
    """Group with full member list."""

    members: list[GroupMember] = []
    updated_at: datetime
