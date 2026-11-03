"""Wire schemas for a file's voiceprint consent (issue #1356)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

ConsentState = Literal["unspecified", "granted", "declined"]


class FileVoiceprintConsentUpdate(BaseModel):
    """``PATCH /files/{uuid}/voiceprint-consent``."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["granted", "declined"] = Field(
        description=(
            "'declined' stops voiceprints for this file and deletes the ones it has. "
            "'granted' records that the uploader states the speakers agreed; it creates nothing "
            "by itself (reprocess speakers to create voiceprints)."
        )
    )


class FileVoiceprintConsent(BaseModel):
    """The file's recorded state and what the change did."""

    state: ConsentState
    previous_state: ConsentState
    changed: bool
    purge_task_id: str | None = Field(
        default=None,
        description="Set when voiceprints are being deleted; poll GET /privacy/purge/{id}.",
    )
    reprocess_to_create_voiceprints: bool = Field(
        default=False, description="True after a move to 'granted' from 'declined'."
    )
