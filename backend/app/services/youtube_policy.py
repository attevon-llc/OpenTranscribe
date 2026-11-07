"""Admin-tunable YouTube ingestion policy, stored as ``youtube.*`` SystemSettings.

Replaces five environment variables (issue #566). The environment is read exactly once, by
:func:`seed_from_env`, when a key has no stored value yet; from then on the stored value wins
and the variable is ignored. The seed is removed in v0.8.0.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from app.core.env import env_bool
from app.core.env import env_int
from app.core.env import env_is_set
from app.core.youtube_defaults import DEFAULT_YOUTUBE_AUTO_RETRY_ENABLED
from app.core.youtube_defaults import DEFAULT_YOUTUBE_RECOVERY_BATCH_SIZE
from app.core.youtube_defaults import DEFAULT_YOUTUBE_USER_RATE_LIMIT_ENABLED
from app.core.youtube_defaults import DEFAULT_YOUTUBE_USER_RATE_LIMIT_PER_DAY
from app.core.youtube_defaults import DEFAULT_YOUTUBE_USER_RATE_LIMIT_PER_HOUR
from app.core.youtube_defaults import YOUTUBE_RATE_LIMIT_MAX
from app.core.youtube_defaults import YOUTUBE_RATE_LIMIT_MIN
from app.core.youtube_defaults import YOUTUBE_RECOVERY_BATCH_MAX
from app.core.youtube_defaults import YOUTUBE_RECOVERY_BATCH_MIN
from app.services.system_settings_service import get_setting_bool
from app.services.system_settings_service import get_setting_int
from app.services.system_settings_service import set_setting

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: field -> (SystemSettings key, deprecated env var, description)
POLICY_KEYS: dict[str, tuple[str, str, str]] = {
    "user_rate_limit_enabled": (
        "youtube.user_rate_limit_enabled",
        "YOUTUBE_USER_RATE_LIMIT_ENABLED",
        "Enforce per-user YouTube download quotas",
    ),
    "user_rate_limit_per_hour": (
        "youtube.user_rate_limit_per_hour",
        "YOUTUBE_USER_RATE_LIMIT_PER_HOUR",
        "Per-user YouTube downloads allowed per hour",
    ),
    "user_rate_limit_per_day": (
        "youtube.user_rate_limit_per_day",
        "YOUTUBE_USER_RATE_LIMIT_PER_DAY",
        "Per-user YouTube downloads allowed per day",
    ),
    "auto_retry_enabled": (
        "youtube.auto_retry_enabled",
        "YOUTUBE_AUTO_RETRY_ENABLED",
        "Automatically retry failed YouTube downloads",
    ),
    "recovery_batch_size": (
        "youtube.recovery_batch_size",
        "YOUTUBE_RECOVERY_BATCH_SIZE",
        "YouTube downloads re-queued per recovery cycle",
    ),
}

#: field -> (minimum, maximum) for the integer fields.
_BOUNDS: dict[str, tuple[int, int]] = {
    "user_rate_limit_per_hour": (YOUTUBE_RATE_LIMIT_MIN, YOUTUBE_RATE_LIMIT_MAX),
    "user_rate_limit_per_day": (YOUTUBE_RATE_LIMIT_MIN, YOUTUBE_RATE_LIMIT_MAX),
    "recovery_batch_size": (YOUTUBE_RECOVERY_BATCH_MIN, YOUTUBE_RECOVERY_BATCH_MAX),
}


@dataclass(frozen=True)
class YoutubePolicy:
    """The effective YouTube ingestion policy."""

    user_rate_limit_enabled: bool
    user_rate_limit_per_hour: int
    user_rate_limit_per_day: int
    auto_retry_enabled: bool
    recovery_batch_size: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_POLICY = YoutubePolicy(
    user_rate_limit_enabled=DEFAULT_YOUTUBE_USER_RATE_LIMIT_ENABLED,
    user_rate_limit_per_hour=DEFAULT_YOUTUBE_USER_RATE_LIMIT_PER_HOUR,
    user_rate_limit_per_day=DEFAULT_YOUTUBE_USER_RATE_LIMIT_PER_DAY,
    auto_retry_enabled=DEFAULT_YOUTUBE_AUTO_RETRY_ENABLED,
    recovery_batch_size=DEFAULT_YOUTUBE_RECOVERY_BATCH_SIZE,
)
DEFAULTS = DEFAULT_POLICY.as_dict()


def get_youtube_policy(db: Session) -> YoutubePolicy:
    """Read the policy (cached per key), falling back to the coded defaults."""

    def _int(field: str) -> int:
        low, high = _BOUNDS[field]
        return min(max(get_setting_int(db, POLICY_KEYS[field][0], int(DEFAULTS[field])), low), high)

    def _bool(field: str) -> bool:
        return get_setting_bool(db, POLICY_KEYS[field][0], bool(DEFAULTS[field]))

    return YoutubePolicy(
        user_rate_limit_enabled=_bool("user_rate_limit_enabled"),
        user_rate_limit_per_hour=_int("user_rate_limit_per_hour"),
        user_rate_limit_per_day=_int("user_rate_limit_per_day"),
        auto_retry_enabled=_bool("auto_retry_enabled"),
        recovery_batch_size=_int("recovery_batch_size"),
    )


def current_youtube_policy() -> YoutubePolicy:
    """The policy for callers that hold no session (rate limiter, Celery tasks).

    A database outage yields the coded defaults, which are the conservative policy, rather
    than turning the outage into an error in a download path that does not otherwise need it.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.db.base import SessionLocal

    try:
        db = SessionLocal()
        try:
            return get_youtube_policy(db)
        finally:
            db.close()
    except SQLAlchemyError:
        logger.warning("YouTube policy unreadable; using the coded defaults", exc_info=True)
        return DEFAULT_POLICY


def update_youtube_policy(db: Session, updates: dict[str, bool | int]) -> YoutubePolicy:
    """Persist the given fields (already bounds-checked by the schema) and return the policy."""
    for field, value in updates.items():
        key, _env, description = POLICY_KEYS[field]
        if field in _BOUNDS:
            low, high = _BOUNDS[field]
            if not low <= int(value) <= high:
                raise ValueError(f"{field} must be between {low} and {high}")
        set_setting(db, key, value, description)
    return get_youtube_policy(db)


def seed_from_env(db: Session) -> list[str]:
    """Copy each explicitly-set legacy variable into an unset key, once, with a warning.

    Returns:
        The environment variable names that were copied.
    """
    from app.models.system_settings import SystemSettings

    seeded: list[str] = []
    for field, (key, env_name, description) in POLICY_KEYS.items():
        if not env_is_set(env_name):
            continue
        if db.query(SystemSettings).filter(SystemSettings.key == key).first() is not None:
            continue
        default = DEFAULTS[field]
        if field in _BOUNDS:
            low, high = _BOUNDS[field]
            value: bool | int = env_int(env_name, int(default), minimum=low, maximum=high)
        else:
            value = env_bool(env_name, bool(default))
        set_setting(db, key, value, description)
        seeded.append(env_name)
        logger.warning(
            "%s is deprecated: its value (%s) was copied into the admin setting %s once. "
            "Change it under Settings > Recovery from now on and remove it from .env; "
            "reading it is removed in v0.8.0.",
            env_name,
            value,
            key,
        )
    return seeded
