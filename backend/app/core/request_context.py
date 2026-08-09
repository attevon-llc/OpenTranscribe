"""The per-request correlation id, owned in exactly one place.

There used to be **two** ``ContextVar`` objects, both named ``"request_id"``:
``app.middleware.audit._request_id_var`` (set by the middleware) and
``app.auth.audit.request_id_var`` (read by every audit event). The display name a
``ContextVar`` is constructed with is documentation, not identity — two objects with
the same name are two independent slots. So the middleware set one and the audit
logger read the other, always found it empty, and fell back to
``str(uuid.uuid4())``: every audit event carried a fresh random id and a multi-event
flow (login -> MFA challenge -> session created, or request -> failure -> lockout)
could not be reconstructed at all. Correlation is the entire point of AU-3's
"where did this come from" field.

This module is stdlib-only and imports nothing from ``app``, so it can be imported
from ``middleware``, ``auth``, ``core`` and the Celery hooks without any risk of an
import cycle (``app.core`` must never import ``app.api`` or ``app.services`` at
module scope).
"""

from contextvars import ContextVar

#: The single request-correlation slot. Set by ``middleware.audit.AuditMiddleware``
#: (and by the Celery pre-run hook for task-side propagation); read by
#: ``auth.audit.AuditLogger.log`` and the logging filter.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Return the current request's correlation id.

    Returns:
        The request id for the current context, or an empty string when no request
        (or task) has set one.
    """
    return request_id_var.get()


def set_request_id(request_id: str) -> None:
    """Bind a correlation id to the current context.

    Args:
        request_id: The id to bind. Pass ``""`` to clear it.
    """
    request_id_var.set(request_id)
