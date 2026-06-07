"""
Service for managing system-wide settings.

Provides a clean interface for getting and setting system configuration
with type conversion and caching support.
"""

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.system_settings import SystemSettings

logger = logging.getLogger(__name__)


def get_setting(db: Session, key: str, default: Any = None) -> str | None:
    """
    Get a system setting by key.

    Args:
        db: Database session
        key: Setting key (e.g., 'transcription.max_retries')
        default: Default value if setting doesn't exist

    Returns:
        Setting value as string, or default if not found
    """
    setting = db.query(SystemSettings).filter(SystemSettings.key == key).first()
    if setting is None:
        return default  # type: ignore[no-any-return]
    return str(setting.value) if setting.value is not None else None


def get_setting_int(db: Session, key: str, default: int = 0) -> int:
    """
    Get a system setting as an integer.

    Args:
        db: Database session
        key: Setting key
        default: Default value if setting doesn't exist or can't be converted

    Returns:
        Setting value as integer
    """
    value = get_setting(db, key)
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        logger.warning(
            f"Could not convert setting '{key}' value '{value}' to int, using default {default}"
        )
        return default


def get_setting_bool(db: Session, key: str, default: bool = False) -> bool:
    """
    Get a system setting as a boolean.

    Args:
        db: Database session
        key: Setting key
        default: Default value if setting doesn't exist

    Returns:
        Setting value as boolean
    """
    value = get_setting(db, key)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


def get_settings_map(db: Session, keys: list[str]) -> dict[str, str | None]:
    """Fetch multiple settings in a single query. Returns {key: value_str | None}.

    Only keys that exist in the table are present in the result; callers should
    use ``.get(key)`` with their own default. Prefer this over multiple
    ``get_setting`` calls whenever a code path reads two or more keys — it
    collapses N round-trips into one SELECT.
    """
    rows = db.query(SystemSettings).filter(SystemSettings.key.in_(keys)).all()
    return {str(row.key): (str(row.value) if row.value is not None else None) for row in rows}


# Backwards-compatible private alias (internal callers predate the public name).
_get_settings_map = get_settings_map


def _bool_val(v: str | None, default: bool) -> bool:
    """Parse a settings string as bool."""
    return v.lower() in ("true", "1", "yes", "on") if v is not None else default


def _int_val(v: str | None, default: int) -> int:
    """Parse a settings string as int."""
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def set_setting(
    db: Session, key: str, value: Any, description: str | None = None
) -> SystemSettings:
    """
    Set a system setting.

    Args:
        db: Database session
        key: Setting key
        value: Setting value (will be converted to string)
        description: Optional description for the setting

    Returns:
        Updated or created SystemSettings object
    """
    setting = db.query(SystemSettings).filter(SystemSettings.key == key).first()

    str_value = str(value).lower() if isinstance(value, bool) else str(value)

    if setting is None:
        setting = SystemSettings(key=key, value=str_value, description=description)
        db.add(setting)
    else:
        setting.value = str_value  # type: ignore[assignment]
        if description is not None:
            setting.description = description  # type: ignore[assignment]

    db.commit()
    db.refresh(setting)
    return setting  # type: ignore[no-any-return]


def _set_settings_batch(db: Session, updates: dict[str, tuple[Any, str | None]]) -> None:
    """
    Update multiple settings in a single round-trip.

    Fetches all affected keys in one SELECT, applies changes in memory,
    then commits once.

    Args:
        updates: Mapping of {key: (value, description)} to write.
    """
    keys = list(updates.keys())
    existing = {
        str(row.key): row
        for row in db.query(SystemSettings).filter(SystemSettings.key.in_(keys)).all()
    }
    for key, (value, description) in updates.items():
        str_value = str(value).lower() if isinstance(value, bool) else str(value)
        if key in existing:
            existing[key].value = str_value  # type: ignore[assignment]
            if description is not None:
                existing[key].description = description  # type: ignore[assignment]
        else:
            db.add(SystemSettings(key=key, value=str_value, description=description))
    db.commit()


def get_all_settings(db: Session) -> dict[str, dict]:
    """
    Get all system settings.

    Returns:
        Dictionary of all settings with their values and descriptions
    """
    settings = db.query(SystemSettings).all()
    return {
        str(s.key): {
            "value": s.value,
            "description": s.description,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in settings
    }


def get_retry_config(db: Session) -> dict:
    """
    Get retry configuration as a structured dict.

    Returns:
        Dictionary with retry configuration:
        - max_retries: int (0 = unlimited)
        - retry_limit_enabled: bool
    """
    return {
        "max_retries": get_setting_int(db, "transcription.max_retries", 3),
        "retry_limit_enabled": get_setting_bool(db, "transcription.retry_limit_enabled", True),
    }


def should_retry_file(db: Session, retry_count: int) -> bool:
    """
    Check if a file should be retried based on system settings.

    Args:
        db: Database session
        retry_count: Current retry count for the file

    Returns:
        True if the file should be retried, False otherwise
    """
    config = get_retry_config(db)

    # If retry limit is disabled, always allow retries
    if not config["retry_limit_enabled"]:
        return True

    # Check against the system-wide max retries
    max_retries = config["max_retries"]
    return bool(retry_count < max_retries)


def update_retry_config(
    db: Session, max_retries: int | None = None, retry_limit_enabled: bool | None = None
) -> dict:
    """
    Update retry configuration.

    Args:
        db: Database session
        max_retries: New max retries value (None to keep current)
        retry_limit_enabled: New enabled state (None to keep current)

    Returns:
        Updated retry configuration
    """
    if max_retries is not None:
        set_setting(
            db,
            "transcription.max_retries",
            max_retries,
            "Maximum number of retry attempts for failed transcriptions (0 = unlimited)",
        )

    if retry_limit_enabled is not None:
        set_setting(
            db,
            "transcription.retry_limit_enabled",
            retry_limit_enabled,
            "Whether to enforce retry limits on transcription processing",
        )

    return get_retry_config(db)


def get_garbage_cleanup_config(db: Session) -> dict:
    """
    Get garbage cleanup configuration as a structured dict.

    Returns:
        Dictionary with garbage cleanup configuration:
        - garbage_cleanup_enabled: bool
        - max_word_length: int (default: 50)
    """
    return {
        "garbage_cleanup_enabled": get_setting_bool(
            db, "transcription.garbage_cleanup_enabled", True
        ),
        "max_word_length": get_setting_int(db, "transcription.max_word_length", 50),
    }


def update_garbage_cleanup_config(
    db: Session,
    garbage_cleanup_enabled: bool | None = None,
    max_word_length: int | None = None,
) -> dict:
    """
    Update garbage cleanup configuration.

    Args:
        db: Database session
        garbage_cleanup_enabled: New enabled state (None to keep current)
        max_word_length: New max word length threshold (None to keep current)

    Returns:
        Updated garbage cleanup configuration
    """
    if garbage_cleanup_enabled is not None:
        set_setting(
            db,
            "transcription.garbage_cleanup_enabled",
            garbage_cleanup_enabled,
            "Whether to clean up garbage words (very long words with no spaces) during transcription",
        )

    if max_word_length is not None:
        set_setting(
            db,
            "transcription.max_word_length",
            max_word_length,
            "Maximum word length threshold for garbage detection (words longer than this with no spaces are replaced)",
        )

    return get_garbage_cleanup_config(db)


def get_retention_config(db: Session) -> dict:
    """
    Get file retention configuration as a structured dict.

    Fetches all seven retention keys in a single database query.

    Returns:
        Dictionary with file retention configuration:
        - retention_enabled: bool (whether auto-deletion is active)
        - retention_days: int (files older than this are deleted)
        - delete_error_files: bool (whether to also delete error-status files)
        - run_time: str (HH:MM daily schedule time)
        - timezone: str (IANA timezone string)
        - last_run: str|None (ISO UTC timestamp of last run)
        - last_run_deleted: int (files deleted in last run)
    """
    row = _get_settings_map(
        db,
        [
            "files.retention_enabled",
            "files.retention_days",
            "files.delete_error_files",
            "files.retention_run_time",
            "files.retention_timezone",
            "files.retention_last_run",
            "files.retention_last_run_deleted",
        ],
    )
    return {
        "retention_enabled": _bool_val(row.get("files.retention_enabled"), False),
        "retention_days": _int_val(row.get("files.retention_days"), 90),
        "delete_error_files": _bool_val(row.get("files.delete_error_files"), False),
        "run_time": row.get("files.retention_run_time") or "02:00",
        "timezone": row.get("files.retention_timezone") or "UTC",
        "last_run": row.get("files.retention_last_run"),
        "last_run_deleted": _int_val(row.get("files.retention_last_run_deleted"), 0),
    }


def update_retention_config(
    db: Session,
    retention_enabled: bool | None = None,
    retention_days: int | None = None,
    delete_error_files: bool | None = None,
    run_time: str | None = None,
    timezone: str | None = None,
) -> dict:
    """
    Update file retention configuration.

    All provided (non-None) fields are written in a single database round-trip.

    Args:
        db: Database session
        retention_enabled: New enabled state (None to keep current)
        retention_days: New retention window in days (None to keep current)
        delete_error_files: Whether to also delete error files (None to keep current)
        run_time: New HH:MM daily schedule time (None to keep current)
        timezone: New IANA timezone string (None to keep current)

    Returns:
        Updated retention configuration
    """
    updates: dict[str, tuple[Any, str | None]] = {}

    if retention_enabled is not None:
        updates["files.retention_enabled"] = (
            retention_enabled,
            "Enable automatic deletion of old completed transcription files",
        )
    if retention_days is not None:
        updates["files.retention_days"] = (
            retention_days,
            "Delete completed files older than this many days (requires retention_enabled=true)",
        )
    if delete_error_files is not None:
        updates["files.delete_error_files"] = (
            delete_error_files,
            "Also delete files in error status during retention runs",
        )
    if run_time is not None:
        updates["files.retention_run_time"] = (
            run_time,
            "Daily scheduled run time in HH:MM format",
        )
    if timezone is not None:
        updates["files.retention_timezone"] = (
            timezone,
            "IANA timezone for the scheduled run (e.g. America/New_York)",
        )

    if updates:
        _set_settings_batch(db, updates)

    return get_retention_config(db)


# ---------------------------------------------------------------------------
# Protected Media Sources Configuration
# ---------------------------------------------------------------------------
# Stores media source hosts, credentials, and provider type in DB.
# Replaces the old MEDIACMS_ALLOWED_HOSTS env variable approach.
# ---------------------------------------------------------------------------


def get_media_sources(db: Session) -> list[dict]:
    """Get all configured protected media sources.

    Each source is stored as a JSON object with:
    - id: unique identifier (auto-generated)
    - hostname: the host that triggers this provider
    - provider_type: "mediacms" (extensible to future providers)
    - username: stored credential username (may be empty)
    - password: stored credential password (may be empty)
    - verify_ssl: whether to verify SSL certificates
    - label: optional display name
    """
    raw = get_setting(db, "media_sources.list")
    if not raw:
        return []
    try:
        sources = json.loads(raw)
        return sources if isinstance(sources, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def set_media_sources(db: Session, sources: list[dict]) -> list[dict]:
    """Replace the full list of protected media sources."""
    set_setting(
        db,
        "media_sources.list",
        json.dumps(sources),
        "Protected media sources configuration (hosts, credentials, providers)",
    )
    return sources


def get_media_source_hosts(db: Session) -> set[str]:
    """Return set of configured hostnames from DB media sources."""
    return {s["hostname"] for s in get_media_sources(db) if s.get("hostname")}


def get_media_source_for_host(db: Session, hostname: str) -> dict | None:
    """Look up media source config for a specific hostname."""
    for source in get_media_sources(db):
        if source.get("hostname") == hostname:
            return source
    return None
