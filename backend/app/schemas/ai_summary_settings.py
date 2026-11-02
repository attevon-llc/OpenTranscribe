"""Wire shape of ``GET /user-settings/ai-summary``."""

from pydantic import BaseModel
from pydantic import Field


class AiSummaryPreference(BaseModel):
    """The caller's auto-summary preference and the deployment-wide switch above it."""

    ai_summary_enabled: bool = Field(
        description="The caller's own preference for automatic summaries."
    )
    system_enabled: bool = Field(
        description=(
            "The deployment-wide switch an admin controls (``ai.summary_enabled``). When false, "
            "no summary is generated whatever the caller's preference says. A plain user cannot "
            "read the admin route, so this is how the settings panel learns of it."
        )
    )
