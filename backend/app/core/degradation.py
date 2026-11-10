"""The metric a security control bumps when it runs without its shared state store.

Lives in core, beside the metrics registry it writes to, so both the auth package and the
core auth-settings loader can call it without crossing the layer contract.
"""

import logging

logger = logging.getLogger(__name__)


def record_degradation(control: str, fallback: str) -> None:
    """Count a security control running without its shared state store.

    Imported lazily and never allowed to raise: a broken metrics backend must not be
    able to break an authentication path. Prometheus is optional in some deployments,
    and this runs on the request path (every revocation check while Redis is down).

    Args:
        control: The security control that degraded.
        fallback: What it used instead (``local`` = per-process approximation).
    """
    try:
        from app.core.metrics import security_state_degraded_total

        security_state_degraded_total.labels(control=control, fallback=fallback).inc()
    except Exception:  # boundary: metrics must never break auth
        # Swallowed on purpose, but never silently: PR #322 removed the blanket
        # S110 exemption precisely because silent handlers hide real faults. A
        # broken counter must not turn into a failed login, so this degrades to a
        # log line instead of propagating.
        logger.debug("Could not record security degradation metric", exc_info=True)
