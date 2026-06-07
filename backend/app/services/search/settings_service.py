"""Service for persisting search settings to the database."""

import logging
from typing import Optional

from app.core.constants import OPENSEARCH_DEFAULT_MODEL
from app.core.constants import OPENSEARCH_EMBEDDING_MODELS

logger = logging.getLogger(__name__)

# DB keys
_KEY_EMBEDDING_MODEL = "search.embedding_model"
_KEY_EMBEDDING_DIMENSION = "search.embedding_dimension"

# Default dimension from the default model
_default_model_info = OPENSEARCH_EMBEDDING_MODELS.get(OPENSEARCH_DEFAULT_MODEL, {})
_DEFAULT_DIMENSION: int = _default_model_info.get("dimension", 384)  # type: ignore[assignment]


def get_search_embedding_model() -> str:
    """Get the current embedding model ID from DB, falling back to default."""
    value = _get_setting(_KEY_EMBEDDING_MODEL)
    if not value:
        return OPENSEARCH_DEFAULT_MODEL

    # If already a valid full model ID, return as-is
    if value in OPENSEARCH_EMBEDDING_MODELS:
        return value

    # Try to find a matching model by short name (e.g., "all-MiniLM-L6-v2")
    for full_id in OPENSEARCH_EMBEDDING_MODELS:
        if full_id.endswith(f"/{value}"):
            logger.info(f"Normalized short model name '{value}' to '{full_id}'")
            return full_id

    # Fallback to default if not found
    logger.warning(f"Unknown model '{value}', falling back to default")
    return OPENSEARCH_DEFAULT_MODEL


def get_search_embedding_dimension() -> int:
    """Get the current embedding dimension from DB, falling back to default."""
    value = _get_setting(_KEY_EMBEDDING_DIMENSION)
    try:
        return int(value) if value else _DEFAULT_DIMENSION
    except (ValueError, TypeError):
        return _DEFAULT_DIMENSION


def save_search_embedding_model(model_id: str, dimension: int) -> None:
    """Persist the embedding model selection to the database."""
    _set_setting(
        _KEY_EMBEDDING_MODEL,
        model_id,
        "Search embedding model ID",
    )
    _set_setting(
        _KEY_EMBEDDING_DIMENSION,
        str(dimension),
        "Search embedding vector dimension",
    )
    logger.info(f"Saved search model setting: {model_id} ({dimension}d)")


def get_search_embedding_settings() -> tuple[str, int]:
    """Get both embedding model and dimension in a single DB query.

    Returns:
        Tuple of (model_id, dimension).
    """
    try:
        from app.db.session_utils import session_scope
        from app.models.system_settings import SystemSettings

        with session_scope() as db:
            rows = (
                db.query(SystemSettings)
                .filter(SystemSettings.key.in_([_KEY_EMBEDDING_MODEL, _KEY_EMBEDDING_DIMENSION]))
                .all()
            )
            values = {row.key: row.value for row in rows}
    except Exception as e:
        logger.warning(f"Could not read search settings: {e}")
        values = {}

    # Resolve model
    model_value = values.get(_KEY_EMBEDDING_MODEL)
    if model_value and model_value in OPENSEARCH_EMBEDDING_MODELS:
        model_id = model_value
    elif model_value:
        model_id = next(
            (fid for fid in OPENSEARCH_EMBEDDING_MODELS if fid.endswith(f"/{model_value}")),
            OPENSEARCH_DEFAULT_MODEL,
        )
    else:
        model_id = OPENSEARCH_DEFAULT_MODEL

    # Resolve dimension
    dim_value = values.get(_KEY_EMBEDDING_DIMENSION)
    try:
        dimension = int(dim_value) if dim_value else _DEFAULT_DIMENSION
    except (ValueError, TypeError):
        dimension = _DEFAULT_DIMENSION

    return model_id, dimension


def _get_setting(key: str) -> Optional[str]:
    """Read a single setting from the database."""
    try:
        from app.db.session_utils import session_scope
        from app.models.system_settings import SystemSettings

        with session_scope() as db:
            row = db.query(SystemSettings).filter(SystemSettings.key == key).first()
            return row.value if row else None
    except Exception as e:
        logger.warning(f"Could not read setting '{key}': {e}")
        return None


def _set_setting(key: str, value: str, description: str = "") -> None:
    """Write a single setting to the database (upsert)."""
    try:
        from app.db.session_utils import session_scope
        from app.models.system_settings import SystemSettings

        with session_scope() as db:
            row = db.query(SystemSettings).filter(SystemSettings.key == key).first()
            if row:
                row.value = value
                if description:
                    row.description = description
            else:
                db.add(SystemSettings(key=key, value=value, description=description))
            db.commit()
        # Bust the in-process settings cache — this writes the same
        # SystemSettings table that system_settings_service.get_setting reads.
        from app.core import settings_cache

        settings_cache.invalidate(key)
    except Exception as e:
        logger.error(f"Could not save setting '{key}': {e}")
