"""Broker connection parameters, derived without importing the application.

``app.core.config`` assembles the broker URL and ``app.core.celery`` adds the TLS options,
but importing either costs about a second of CPU (pydantic-settings, then the whole Celery
app). The worker healthcheck (``scripts/celery_pool_healthcheck.py``) runs every 30 s in
every worker container and only needs to reach the broker, so it resolves the URL here,
from the process environment, with the same code those two modules use. Keep this module
stdlib-only: anything heavy imported here is paid by every probe.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Mapping

from app.core.env import is_truthy

# kombu refuses to connect over `rediss://` unless an SSL context is supplied: it does NOT
# fall back to a default (issue #284 A1.17). CERT_REQUIRED (the default) verifies the server
# certificate; CELERY_REDIS_SSL_CERT_REQS=optional|none is only for a self-signed broker you
# control.
_REDIS_SSL_MODES = {
    "required": ssl.CERT_REQUIRED,
    "optional": ssl.CERT_OPTIONAL,
    "none": ssl.CERT_NONE,
}


def assemble_redis_url(*, use_tls: bool, host: str, port: str, password: str) -> str:
    """The Redis URL ``Settings`` builds when ``REDIS_URL`` is not given explicitly."""
    scheme = "rediss" if use_tls else "redis"
    auth = f":{password}@" if password else ""
    return f"{scheme}://{auth}{host}:{port}/0"


def broker_url_from_env(environ: Mapping[str, str] | None = None) -> str:
    """``settings.CELERY_BROKER_URL``, resolved from the environment alone.

    Same precedence as ``Settings.validate_auth_settings``: an explicit non-empty
    ``CELERY_BROKER_URL``, else ``REDIS_URL``, else one assembled from ``REDIS_*``. Empty
    values count as unset for the two URLs (``env_ignore_empty``), while the ``REDIS_*``
    parts keep their ``os.getenv`` class defaults, which return an empty string as given.
    A ``.env`` file is not read: inside a container the worker's own settings see only
    the environment too, so the two agree there, which is the only place the probe runs.
    """
    env = os.environ if environ is None else environ
    explicit = env.get("CELERY_BROKER_URL") or env.get("REDIS_URL")
    if explicit:
        return explicit
    return assemble_redis_url(
        use_tls=is_truthy(env.get("REDIS_USE_TLS")),
        host=env.get("REDIS_HOST", "localhost"),
        port=env.get("REDIS_PORT", "6379"),
        password=env.get("REDIS_PASSWORD", ""),
    )


def redis_ssl_options(
    url: str, environ: Mapping[str, str] | None = None
) -> dict[str, ssl.VerifyMode] | None:
    """``broker_use_ssl`` for *url*: an SSL mode for ``rediss://``, None for plain Redis."""
    if not url.startswith("rediss://"):
        return None
    env = os.environ if environ is None else environ
    mode = env.get("CELERY_REDIS_SSL_CERT_REQS", "required").lower()
    return {"ssl_cert_reqs": _REDIS_SSL_MODES.get(mode, ssl.CERT_REQUIRED)}
