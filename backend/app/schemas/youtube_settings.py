"""Wire contract for the admin YouTube ingestion policy (``/admin/youtube-settings``)."""

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.core.youtube_defaults import YOUTUBE_RATE_LIMIT_MAX
from app.core.youtube_defaults import YOUTUBE_RATE_LIMIT_MIN
from app.core.youtube_defaults import YOUTUBE_RECOVERY_BATCH_MAX
from app.core.youtube_defaults import YOUTUBE_RECOVERY_BATCH_MIN


class YoutubeSettings(BaseModel):
    """The effective policy."""

    user_rate_limit_enabled: bool
    user_rate_limit_per_hour: int
    user_rate_limit_per_day: int
    auto_retry_enabled: bool
    recovery_batch_size: int


class YoutubeSettingsUpdate(BaseModel):
    """Fields to change; omitted fields keep their stored value."""

    model_config = ConfigDict(extra="forbid")

    user_rate_limit_enabled: bool | None = None
    user_rate_limit_per_hour: int | None = Field(
        default=None, ge=YOUTUBE_RATE_LIMIT_MIN, le=YOUTUBE_RATE_LIMIT_MAX
    )
    user_rate_limit_per_day: int | None = Field(
        default=None, ge=YOUTUBE_RATE_LIMIT_MIN, le=YOUTUBE_RATE_LIMIT_MAX
    )
    auto_retry_enabled: bool | None = None
    recovery_batch_size: int | None = Field(
        default=None, ge=YOUTUBE_RECOVERY_BATCH_MIN, le=YOUTUBE_RECOVERY_BATCH_MAX
    )
