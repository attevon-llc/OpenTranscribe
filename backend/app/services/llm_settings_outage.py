"""A failed read of a user's LLM settings is an outage, not "no LLM configured".

``LLMService``'s factories let a database error through, because ``None`` already means
"nothing is configured". A request handler that resolves the caller's provider maps that
error here to a 503 with a fixed sentence: the exception text carries SQL and connection
details, so it never reaches the client.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceUnavailableError

logger = logging.getLogger(__name__)

LLM_SETTINGS_UNREADABLE_MESSAGE: Final = (
    "AI provider settings could not be read. Try again in a moment."
)


@contextmanager
def llm_settings_outage_as_503() -> Iterator[None]:
    """Raise a 503 with a fixed sentence for a database error inside the block.

    Raises:
        ServiceUnavailableError: the block raised ``SQLAlchemyError``.
    """
    try:
        yield
    except SQLAlchemyError as exc:
        logger.error("LLM provider settings could not be read", exc_info=True)
        raise ServiceUnavailableError(
            LLM_SETTINGS_UNREADABLE_MESSAGE, headers={"Retry-After": "5"}
        ) from exc
