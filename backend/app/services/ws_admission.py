"""WebSocket admission limits: per-IP handshake rate, per-user and per-process socket caps.

The ``/ws`` route accepts a connection before it authenticates (first-message auth waits
up to 10 s), so nothing upstream bounded how many sockets one client could hold open or how
fast it could open them (concurrency audit M7). Three limits, all configurable in
``core/settings_concurrency.py``:

* ``WS_HANDSHAKES_PER_IP_PER_MINUTE``: handshakes per client IP per minute, counted in
  Redis so every API process shares one budget. Checked before ``accept()``.
* ``WS_MAX_SOCKETS_PER_PROCESS``: open sockets in this process. Checked before ``accept()``.
* ``WS_MAX_SOCKETS_PER_USER``: open sockets per user in this process (each browser tab holds
  one). Checked after authentication, since only then is the user known.

0 disables a limit. The rate limit fails OPEN when Redis is unreachable, matching the HTTP
limiter's degradation: a limiter outage must not take live updates down for everyone.
"""

from __future__ import annotations

import logging
import time

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Close code for a refused admission: RFC 6455 "Try Again Later". The SPA reconnects with
#: its jittered backoff (``connectionPolicy.closeAction``), which is the wanted behaviour.
TRY_AGAIN_LATER_CLOSE = 1013

_WINDOW_SECONDS = 60


async def handshake_allowed(client: aioredis.Redis | None, client_ip: str) -> bool:
    """Count this handshake against its IP's per-minute budget; False when it is spent.

    ``client_ip`` comes from ``utils.client_ip.resolve_client_ip``, which honours the
    trusted-proxy list, so every client behind a reverse proxy is not one bucket.

    INCR and EXPIRE run in one MULTI, so a crash between them cannot leave a counter with
    no expiry that refuses that IP forever.
    """
    limit = settings.WS_HANDSHAKES_PER_IP_PER_MINUTE
    if limit <= 0 or client is None:
        return True
    window = int(time.time()) // _WINDOW_SECONDS
    key = f"ws_handshake:{client_ip}:{window}"
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, _WINDOW_SECONDS * 2)
            count, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        logger.warning("WebSocket handshake rate limit unavailable (%s); admitting", exc)
        return True
    return int(count) <= limit


def process_has_room(open_sockets: int) -> bool:
    """Whether this process may accept one more socket."""
    cap = settings.WS_MAX_SOCKETS_PER_PROCESS
    return cap <= 0 or open_sockets < cap


def user_has_room(open_for_user: int) -> bool:
    """Whether this user may open one more socket in this process."""
    cap = settings.WS_MAX_SOCKETS_PER_USER
    return cap <= 0 or open_for_user < cap
