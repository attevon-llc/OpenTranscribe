"""Pydantic schemas for collection sharing."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.schemas.base import UUIDBaseSchema
from app.schemas.user import UserBrief


class ShareCreate(BaseModel):
    target_type: str = Field(..., pattern="^(user|group)$")
    target_uuid: UUID
    permission: str = Field("viewer", pattern="^(viewer|editor)$")


class ShareUpdate(BaseModel):
    permission: str = Field(..., pattern="^(viewer|editor)$")


class Share(UUIDBaseSchema):
    """Share record for display."""

    target_type: str
    target_uuid: UUID
    target_name: str
    target_email: str | None = None  # only for user targets
    member_count: int | None = None  # only for group targets
    permission: str
    shared_by: UserBrief
    created_at: datetime


class SharedCollectionInfo(BaseModel):
    """Collection info from the perspective of someone it's shared with."""

    uuid: UUID
    name: str
    description: str | None = None
    media_count: int = 0
    my_permission: str
    shared_by: UserBrief
    shared_at: datetime

    model_config = ConfigDict(from_attributes=True)
