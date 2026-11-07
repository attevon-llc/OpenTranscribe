"""Wire contract of ``/admin/telemetry`` (issue #1019), the opt-in anonymous usage report."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

TelemetryOffReason = Literal["do_not_track", "env_disabled", "demo", "no_receiver"]


class TelemetryStatus(BaseModel):
    """What the admin card shows. ``preview`` is the exact request body that would be sent."""

    enabled: bool = Field(description="The administrator's choice (off by default).")
    sending: bool = Field(description="True only when enabled and nothing forces it off.")
    off_reasons: list[TelemetryOffReason] = Field(
        description="Deployment settings that force the report off, whatever the choice."
    )
    last_sent_at: datetime | None = None
    endpoint: str = Field(description="Where the report goes.")
    preview: dict[str, Any] = Field(
        description="The request body, built by the same function the sender uses."
    )


class TelemetryUpdate(BaseModel):
    enabled: bool
