"""Application logging configuration (text or structured JSON).

``configure_logging`` is the single entry point, called from ``app.main`` (web)
and ``@setup_logging.connect`` in ``app.core.celery`` (workers). It is
idempotent — safe to call more than once.

- ``LOG_FORMAT="text"`` (default): the historical human-readable format
  (equivalent to ``logging.basicConfig(level=INFO)``).
- ``LOG_FORMAT="json"``: ``pythonjsonlogger.json.JsonFormatter`` with
  ``asctime`` renamed to ``timestamp`` and ``levelname`` to ``level``; ``extra``
  fields (e.g. the access log's ``route``/``db_query_count``) are emitted as
  structured keys. A :class:`RequestIdFilter` injects ``request_id`` onto EVERY
  record (read from the audit ContextVar) so all JSON lines — not just access
  logs — are correlatable across the HTTP→Celery boundary.
"""

import logging

from app.core.config import settings

_JSON_FMT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_TEXT_FMT = "%(asctime)s %(levelname)s:%(name)s:%(message)s"

_configured = False


class RequestIdFilter(logging.Filter):
    """Attach the current request id (or ``"-"``) to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Lazy import keeps this module importable before middleware is wired.
        from app.middleware.audit import get_request_id

        record.request_id = get_request_id() or "-"
        return True


def _build_handler() -> logging.Handler:
    """Create the root stream handler with the format selected by settings."""
    handler = logging.StreamHandler()
    if settings.LOG_FORMAT.lower() == "json":
        from pythonjsonlogger.json import JsonFormatter

        handler.setFormatter(
            JsonFormatter(
                _JSON_FMT,
                rename_fields={"asctime": "timestamp", "levelname": "level"},
            )
        )
        handler.addFilter(RequestIdFilter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FMT))
    return handler


def configure_logging() -> None:
    """Configure the root logger handler/formatter (idempotent)."""
    global _configured

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # This function is the single authority for the root handler. Remove ALL
    # existing root handlers — ours from a prior call AND any default handler a
    # module-level ``logging.basicConfig`` installed during imports (imports run
    # before we are called, so a leftover default handler would double every
    # log line: one bare ``INFO:name:msg`` + one timestamped).
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = _build_handler()
    handler._obs_managed = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    _configured = True
