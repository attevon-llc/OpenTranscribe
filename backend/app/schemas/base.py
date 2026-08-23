"""
Base Pydantic schemas for hybrid ID system.

Provides automatic UUID exposure for all response schemas.
Internal database uses integer IDs for fast queries, external API uses UUIDs.
"""

import builtins
import contextlib
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import model_validator


class UUIDBaseSchema(BaseModel):
    """
    Base schema for models with UUID public identifiers.

    All response schemas should inherit from this to get automatic
    UUID exposure to the external API.

    The SQLAlchemy model must have both:
    - id: Integer (internal primary key, NOT exposed to API)
    - uuid: UUID (public identifier, exposed to API)

    The schema exposes 'uuid' as the public identifier.
    Internal integer 'id' is never sent to the frontend.
    """

    uuid: UUID  # Public UUID identifier

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def _convert_to_dict(cls, data: Any) -> dict[str, Any]:
        """Convert SQLAlchemy model to dict, excluding internal id."""
        if isinstance(data.__dict__, dict):
            # Exclude internal 'id' field - only expose 'uuid'
            return {k: v for k, v in data.__dict__.items() if not k.startswith("_") and k != "id"}

        data_dict = {}
        for attr in dir(data):
            if not attr.startswith("_") and attr != "id":
                with contextlib.suppress(builtins.BaseException):
                    data_dict[attr] = getattr(data, attr)
        return data_dict

    @classmethod
    def _map_foreign_key_uuids(cls, data: Any, data_dict: dict[str, Any]) -> None:
        """Map foreign key UUIDs from related objects to the data dict."""
        # Map user relationship
        if hasattr(data, "user") and hasattr(data.user, "uuid"):
            data_dict["user_id"] = data.user.uuid

        # Map sharing-attribution relationship (shared_by FK -> user uuid)
        shared_by_user = getattr(data, "shared_by_user", None)
        if shared_by_user is not None and hasattr(shared_by_user, "uuid"):
            data_dict["shared_by"] = shared_by_user.uuid

        # Map media_file relationship
        if hasattr(data, "media_file") and hasattr(data.media_file, "uuid"):
            data_dict["media_file_id"] = data.media_file.uuid

        # Map speaker relationship
        if hasattr(data, "speaker") and hasattr(data.speaker, "uuid"):
            data_dict["speaker_id"] = data.speaker.uuid

        # Map profile relationship
        if hasattr(data, "profile") and hasattr(data.profile, "uuid"):
            data_dict["profile_id"] = data.profile.uuid

    @model_validator(mode="before")
    @classmethod
    def prepare_uuid_response(cls, data: Any) -> Any:
        """
        Prepare model data for API response, ensuring uuid is properly exposed.
        Also maps foreign key UUIDs (user_id, media_file_id, etc.) from related models.

        This runs before Pydantic validation, allowing us to transform
        the SQLAlchemy model's data structure.
        """
        # Handle SQLAlchemy models
        if hasattr(data, "uuid") and hasattr(data, "__dict__"):
            data_dict = cls._convert_to_dict(data)

            # Include computed properties that aren't in __dict__
            # has_api_key is a property on UserLLMSettings model
            if hasattr(data, "has_api_key") and "has_api_key" not in data_dict:
                data_dict["has_api_key"] = data.has_api_key

            # Map foreign key UUIDs from related objects
            cls._map_foreign_key_uuids(data, data_dict)

            return data_dict

        return data
