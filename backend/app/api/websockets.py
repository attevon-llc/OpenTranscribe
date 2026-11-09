import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis
from fastapi import APIRouter
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.endpoints.auth.lifecycle_gates import admitted_on_optional_auth
from app.auth.roles import ELEVATED_ROLES
from app.auth.token_service import token_service
from app.core.capabilities import capability_enabled
from app.core.db_overload import OVERLOAD_EXCEPTION_CLASSES
from app.core.db_overload import overload_cause
from app.core.db_overload import reraise_db_overload
from app.db.session_utils import session_scope
from app.services import ws_admission
from app.services.user_lookup import get_user_by_uuid
from app.utils.client_ip import resolve_client_ip
from app.utils.websocket_notify import SESSION_REVOKED_EVENT

from ..core.config import settings
from ..core.exceptions import AuthenticationError
from ..core.security import verify_token
from ..models.user import User

# Set up logging
logger = logging.getLogger(__name__)

router = APIRouter()


#: Sent when an open socket is closed because its user's sessions were revoked, or because
#: the access token that authenticated it expired. The SPA re-checks the session over HTTP
#: (refreshing it when it can) before reconnecting rather than retrying blindly.
SESSION_REVOKED_CLOSE = 4004


@dataclass(frozen=True)
class SocketAuth:
    """Who authenticated a socket, and with which access token.

    ``jti`` / ``issued_at`` / ``expires_at`` come from a local access token's claims and
    are ``None`` for a socket an external verifier authenticated.
    """

    user_uuid: str | None
    authenticated_at: int
    jti: str | None = None
    issued_at: int | None = None
    expires_at: int | None = None

    def predates(self, epoch: int) -> bool:
        """Whether a revocation epoch outdates this socket.

        Compares the token's own ``iat`` with the epoch, as the HTTP verdict does
        (``TokenService._issued_before_user_epoch``), so the answer does not depend on this
        host's clock agreeing with the host that stamped the epoch. ``<=`` rather than the
        verdict's ``<``: a token minted in the epoch's own second is closed too, which costs
        one reconnect that its handshake then accepts. Without an ``iat``, falls back to
        when this process accepted the socket.
        """
        if self.issued_at is not None:
            return self.issued_at <= epoch
        return self.authenticated_at <= epoch


def _socket_auth(user_uuid: str | None, claims: dict[str, Any] | None) -> SocketAuth:
    """Build the :class:`SocketAuth` record for a freshly authenticated socket."""

    def _int(name: str) -> int | None:
        value = (claims or {}).get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    jti = (claims or {}).get("jti")
    return SocketAuth(
        user_uuid=user_uuid,
        authenticated_at=int(time.time()),
        jti=jti if isinstance(jti, str) else None,
        issued_at=_int("iat"),
        expires_at=_int("exp"),
    )


#: Upper bound on one fan-out send. A client that cannot take a frame in this long is
#: dropped and closed (it reconnects), instead of delaying everyone else's notifications.
WS_SEND_TIMEOUT_S = 5.0


@dataclass
class _Outbox:
    """One socket's bounded queue of frames and the task that writes them, in order."""

    queue: asyncio.Queue[str]
    writer: asyncio.Task[None] | None = None


def _new_send_queue() -> asyncio.Queue[str]:
    return asyncio.Queue(maxsize=max(1, settings.WS_SEND_QUEUE_SIZE))


async def _close_quietly(websocket: WebSocket) -> None:
    """Close a stalled socket without waiting on it: the close frame can stall too."""
    with contextlib.suppress(Exception):
        async with asyncio.timeout(WS_SEND_TIMEOUT_S):
            await websocket.close(code=1013, reason="Client too slow")


# Connection manager to handle WebSocket connections
class ConnectionManager:
    def __init__(self) -> None:
        # user_id -> List[WebSocket]
        self.active_connections: dict[int, list[WebSocket]] = {}
        # Sockets whose user held an admin tier when the connection was opened.
        self.admin_connections: set[WebSocket] = set()
        # socket -> who authenticated it and with which token, for the revocation closes.
        self.socket_owners: dict[WebSocket, SocketAuth] = {}
        # socket -> its bounded send queue and writer task (see _fan_out).
        self.outboxes: dict[WebSocket, _Outbox] = {}
        # Close tasks for dropped sockets; referenced so they are not garbage collected.
        self._closers: set[asyncio.Task[None]] = set()

    def open_sockets(self, user_id: int | None = None) -> int:
        """Sockets open in this process, or for ``user_id`` only."""
        if user_id is not None:
            return len(self.active_connections.get(user_id, []))
        return len(self.socket_owners)

    def connect(
        self,
        websocket: WebSocket,
        user_id: int,
        host_telemetry: bool = False,
        user_uuid: str | None = None,
        claims: dict[str, Any] | None = None,
    ) -> None:
        """Register a WebSocket connection for a user (accept() must be called before this).

        Bookkeeping only — no I/O — so it is a plain function, mirroring ``disconnect``.
        It is still owned by the connection's own coroutine; nothing outside this
        process may touch ``manager`` (publish via ``utils/websocket_notify.py``).
        """
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        if host_telemetry:
            self.admin_connections.add(websocket)
        self.socket_owners[websocket] = _socket_auth(user_uuid, claims)
        self.outboxes[websocket] = _Outbox(_new_send_queue())
        logger.info(f"User {user_id} connected. Total connections: {len(self.active_connections)}")

    async def drain(self, code: int = 1001) -> int:
        """Close every open socket so shutdown does not hang to SIGKILL.

        Nothing closed WebSockets on shutdown (issue #284 A1.21): the lifespan cancelled
        its background tasks but left sockets open, so the process waited on them until
        the orchestrator's grace period expired and SIGKILLed it — dropping every stream
        mid-message instead of letting clients reconnect cleanly.

        1001 ("going away") is the correct code here: it tells the client this endpoint
        is shutting down normally, which the frontend's reconnect logic treats as a
        retry rather than an error.

        Args:
            code: WebSocket close code to send.

        Returns:
            Number of sockets closed.
        """
        import contextlib

        sockets = [ws for conns in self.active_connections.values() for ws in conns]
        for box in self.outboxes.values():
            self._retire(box)
        self.outboxes.clear()
        for ws in sockets:
            # A socket already torn down by the peer raises here; that is a successful
            # drain, not a failure.
            with contextlib.suppress(Exception):
                await ws.close(code=code)
        self.active_connections.clear()
        self.admin_connections.clear()
        self.socket_owners.clear()
        if sockets:
            logger.info("Drained %d WebSocket connection(s) on shutdown", len(sockets))
        return len(sockets)

    async def close_revoked(self, user_uuid: str, epoch: int) -> int:
        """Close *user_uuid*'s sockets whose token the revocation ``epoch`` outdates.

        See :meth:`SocketAuth.predates`. Sockets whose token is newer than the epoch
        passed the epoch check at their handshake and are left alone. Bookkeeping is
        removed by each connection's own coroutine when its receive loop sees the close.

        Returns:
            Number of sockets closed.
        """
        return await self._close_where(
            lambda auth: auth.user_uuid == user_uuid and auth.predates(epoch)
        )

    async def close_token(self, user_uuid: str, jti: str) -> int:
        """Close *user_uuid*'s sockets authenticated with the access token ``jti``.

        Used when one session logs out: its access token is blacklisted, so a socket it
        opened must not outlive it. A socket the same session opened with an earlier
        access token is bounded by that token's expiry instead (``websocket_endpoint``).

        Returns:
            Number of sockets closed.
        """
        return await self._close_where(lambda auth: auth.user_uuid == user_uuid and auth.jti == jti)

    async def _close_where(self, doomed_if: Any) -> int:
        doomed = [ws for ws, auth in self.socket_owners.items() if doomed_if(auth)]
        for ws in doomed:
            with contextlib.suppress(Exception):
                await ws.close(code=SESSION_REVOKED_CLOSE, reason="Session revoked")
        if doomed:
            logger.info("Closed %d WebSocket(s) after a session revocation", len(doomed))
        return len(doomed)

    def disconnect(self, websocket: WebSocket, user_id: int) -> None:
        # Tolerates a socket the fan-out already dropped for stalling: its own coroutine
        # still runs this when its receive loop ends.
        self.admin_connections.discard(websocket)
        self.socket_owners.pop(websocket, None)
        box = self.outboxes.pop(websocket, None)
        if box is not None:
            self._retire(box)
        sockets = self.active_connections.get(user_id)
        if sockets is not None and websocket in sockets:
            sockets.remove(websocket)
            if not sockets:
                del self.active_connections[user_id]
            logger.info(
                f"User {user_id} disconnected. Total connections: {len(self.active_connections)}"
            )

    async def send_personal_message(self, user_id: int, message: dict[str, Any]) -> None:
        # A copy: each send yields to the loop, and a tab that connects or closes
        # meanwhile mutates the live list (removing one skipped the next tab).
        targets = [(ws, user_id) for ws in self.active_connections.get(user_id, [])]
        await self._fan_out(targets, json.dumps(message))

    async def broadcast(self, message: dict[str, Any]) -> None:
        # A snapshot, for the same reason: a connect or disconnect during the fan-out
        # raised "dictionary changed size during iteration", which the subscriber took
        # for a lost connection, dropping every notification published while it resubscribed.
        targets = [
            (ws, user_id)
            for user_id, sockets in list(self.active_connections.items())
            for ws in list(sockets)
        ]
        await self._fan_out(targets, json.dumps(message))

    async def broadcast_to_admins(self, message: dict[str, Any]) -> None:
        """Send to administrator sockets only (e.g. host GPU telemetry)."""
        targets = [(ws, self._owner_id(ws)) for ws in list(self.admin_connections)]
        await self._fan_out(targets, json.dumps(message))

    def _owner_id(self, websocket: WebSocket) -> int | None:
        for user_id, sockets in self.active_connections.items():
            if websocket in sockets:
                return user_id
        return None

    async def _fan_out(self, targets: Sequence[tuple[WebSocket, int | None]], payload: str) -> None:
        """Queue ``payload`` for every target and return without waiting for any send.

        One subscriber task delivers every notification for the process. Awaiting the sends
        (even concurrently, each bounded by ``WS_SEND_TIMEOUT_S``) held that task for as
        long as the slowest client took, so one stalled tab delayed every later
        notification for everyone and let Redis's pub/sub output buffer fill (concurrency
        audit M7). Each socket now has a bounded queue (``WS_SEND_QUEUE_SIZE``) drained in
        order by its own writer task, so the subscriber only enqueues. A socket whose queue
        is full, or whose send exceeds ``WS_SEND_TIMEOUT_S``, is dropped and closed; the
        client reconnects. A socket that left after the snapshot was taken is skipped.
        """
        for websocket, user_id in targets:
            box = self.outboxes.get(websocket)
            if box is None or websocket not in self.socket_owners:
                continue
            if box.writer is None or box.writer.done():
                # A writer only ends when its socket is dropped (and the box removed) or
                # its event loop stopped; start a fresh queue on the running loop with it.
                box.queue = _new_send_queue()
                box.writer = asyncio.create_task(self._write(websocket, user_id, box))
            try:
                box.queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._drop(websocket, user_id, "its send queue is full")

    async def _write(self, websocket: WebSocket, user_id: int | None, box: _Outbox) -> None:
        """Send one socket's queued frames in order until it is dropped or disconnects."""
        while True:
            payload = await box.queue.get()
            try:
                async with asyncio.timeout(WS_SEND_TIMEOUT_S):
                    await websocket.send_text(payload)
            except TimeoutError:
                self._drop(websocket, user_id, f"a send took longer than {WS_SEND_TIMEOUT_S} s")
                return
            except (WebSocketDisconnect, RuntimeError, OSError):
                # A failed send ends the socket; keeping the writer would repeat it for every frame.
                logger.error(f"Error sending WebSocket message to user {user_id}", exc_info=True)
                self._drop(websocket, user_id, "a send failed")
                return
            finally:
                box.queue.task_done()

    def _drop(self, websocket: WebSocket, user_id: int | None, why: str) -> None:
        logger.warning("Dropping a WebSocket of user %s: %s", user_id, why)
        self._forget(websocket, user_id)
        closer = asyncio.create_task(_close_quietly(websocket))
        self._closers.add(closer)
        closer.add_done_callback(self._closers.discard)

    @staticmethod
    def _retire(box: _Outbox) -> None:
        """Discard a removed socket's unsent frames and stop its writer."""
        while not box.queue.empty():
            box.queue.get_nowait()
            box.queue.task_done()
        if box.writer is not None and box.writer is not asyncio.current_task():
            box.writer.cancel()

    async def flush(self) -> None:
        """Wait until every queued frame is sent or its socket dropped, and drops closed."""
        async with asyncio.TaskGroup() as group:
            for box in list(self.outboxes.values()):
                group.create_task(box.queue.join())
        if self._closers:
            await asyncio.wait(set(self._closers))

    def _forget(self, websocket: WebSocket, user_id: int | None) -> None:
        if user_id is None:
            user_id = self._owner_id(websocket)
        if user_id is not None:
            self.disconnect(websocket, user_id)
        else:
            self.admin_connections.discard(websocket)
            self.socket_owners.pop(websocket, None)


#: Event types that expose host/infrastructure detail and are never sent to non-admins,
#: even if a publisher forgets to flag them ``admin_only``.
ADMIN_ONLY_EVENT_TYPES = frozenset({"gpu_stats_update"})

# Create connection manager instance
manager = ConnectionManager()

# Redis client for pub/sub
redis_client: redis.Redis | None = None
_subscriber_task: asyncio.Task[None] | None = None


def setup_redis() -> None:
    """Initialize Redis connection for pub/sub notifications.

    ``redis.asyncio.from_url`` only builds a lazily-connecting pool and
    ``asyncio.create_task`` needs a running loop, not an ``await`` — so nothing here
    yields. Declaring it ``async def`` claimed otherwise (issue #320); callers must
    still invoke it from a coroutine so ``create_task`` has a loop to attach to.
    """
    global redis_client, _subscriber_task
    if not redis_client:
        # health_check_interval + keepalive keep the long-lived pub/sub connection
        # from being reaped while idle (the root cause of the subscriber silently
        # dying) and let redis-py detect a dead socket proactively.
        #
        # A connect timeout, but deliberately no socket_timeout: the subscription waits
        # in get_message(timeout=...) for as long as nothing is published, and a read
        # timeout would turn that idle wait into an error (redis-py "Timeouts" docs).
        redis_client = redis.from_url(
            settings.REDIS_URL,
            health_check_interval=30,
            socket_keepalive=True,
            socket_connect_timeout=settings.REDIS_REQUEST_CONNECT_TIMEOUT_SECONDS,
        )
        # Start Redis subscriber in background. The reference is kept: the event loop
        # holds only a weak one, so an unreferenced task can be collected mid-run
        # (asyncio.create_task docs).
        _subscriber_task = asyncio.create_task(redis_subscriber())


async def _dispatch_notification(message: dict) -> None:
    """Parse one pub/sub message and forward it to the right WebSocket client(s)."""
    if message.get("type") != "message":
        return
    try:
        notification_data = json.loads(message["data"])
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"Error decoding Redis notification: {e}", exc_info=True)
        return

    user_id = notification_data.get("user_id")
    notification_type = notification_data.get("type")

    if notification_type == SESSION_REVOKED_EVENT:
        user_uuid = notification_data.get("user_uuid")
        revoked = notification_data.get("data", {})
        epoch, jti = revoked.get("epoch"), revoked.get("jti")
        if isinstance(user_uuid, str) and isinstance(epoch, int) and not isinstance(epoch, bool):
            await manager.close_revoked(user_uuid, epoch)
        elif isinstance(user_uuid, str) and isinstance(jti, str) and jti:
            await manager.close_token(user_uuid, jti)
        else:
            logger.warning("Ignoring malformed session revocation message")
        return
    is_broadcast = notification_data.get("broadcast", False)
    data = notification_data.get("data", {})

    admin_only = bool(notification_data.get("admin_only")) or (
        notification_type in ADMIN_ONLY_EVENT_TYPES
    )

    if admin_only and notification_type:
        await manager.broadcast_to_admins({"type": notification_type, "data": data})
    elif is_broadcast and notification_type:
        await manager.broadcast({"type": notification_type, "data": data})
    elif user_id and notification_type:
        await manager.send_personal_message(user_id, {"type": notification_type, "data": data})
    else:
        logger.warning(f"Invalid notification data: {notification_data}")


async def redis_subscriber() -> None:
    """Forward Redis pub/sub notifications to WebSocket connections.

    Wrapped in a supervised reconnect loop: a transient Redis read timeout or
    dropped connection must never permanently kill notification delivery (that
    would silently break transcription progress, downloads, etc. for the whole
    backend process). On any error we log, back off, and resubscribe. Polling
    via ``get_message`` with a short timeout means idle periods don't raise.
    """
    backoff = 1
    while True:
        try:
            assert redis_client is not None
            pubsub = redis_client.pubsub()
            await pubsub.subscribe("websocket_notifications")
            logger.info("Started Redis subscriber for WebSocket notifications")
            backoff = 1  # healthy subscription resets the backoff

            while True:
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                except (TimeoutError, RedisTimeoutError):
                    # Idle read timeout — benign keepalive, NOT a connection loss.
                    continue
                if message is None:
                    continue
                await _dispatch_notification(message)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await pubsub.close()
            raise
        except (RedisError, OSError) as e:
            # Real connection loss — resubscribe with exponential backoff.
            logger.warning(f"Redis subscriber connection lost: {e}; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except Exception:  # subscriber-loop boundary: this task must outlive any one failure
            logger.exception(f"Unexpected Redis subscriber error; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


# Function to publish notification to Redis (for use from other processes)
async def publish_notification(user_id: int, notification_type: str, data: dict[str, Any]) -> None:
    """Publish notification via Redis pub/sub."""
    if not redis_client:
        setup_redis()

    notification = {"user_id": user_id, "type": notification_type, "data": data}

    if redis_client is None:
        logger.error("Cannot publish notification: the Redis client is not initialised")
        return
    try:
        await redis_client.publish("websocket_notifications", json.dumps(notification))
        logger.info(f"Published notification to Redis for user {user_id}: {notification_type}")
    except (RedisError, OSError, TypeError, ValueError) as e:
        logger.error(f"Failed to publish notification to Redis: {e}", exc_info=True)


def _try_authenticate_token(token: str, db: Session, websocket: WebSocket) -> User | None:
    """Validate a token (local JWT or external provider) and return the User, or None.

    Cloud-edition seam: an external session token authenticates the socket via the
    registered external verifier before the local-JWT path, mirroring
    ``get_current_user``. The community edition registers no verifier, so the
    external branch is skipped and behavior is identical (local JWT only). On any
    failure (no match, invalid token, or an account any lifecycle gate refuses, as on
    optional-auth routes) the caller closes with 4003.
    """
    # Cloud-edition seam: offer the token to registered external verifiers first.
    try:
        from app.auth.provider_registry import has_verifiers

        if has_verifiers():
            from app.auth.external_session import external_session_expired
            from app.auth.external_sync import sync_external_user_to_db
            from app.auth.provider_registry import verify_external_token

            # TokenVerifier takes an HTTPConnection, the shared base of Request and
            # WebSocket, so the handshake's headers/cookies reach the verifier.
            external_identity = verify_external_token(token, websocket)
            if external_identity is not None:
                if external_session_expired(external_identity):
                    return None
                external_user = sync_external_user_to_db(db, external_identity)
                if not external_user.is_active or not admitted_on_optional_auth(
                    external_user, websocket, db
                ):
                    return None
                return external_user
    except SQLAlchemyError as e:
        reraise_db_overload(e)
        # Never let an external-auth problem break local WS authentication;
        # fall through to the local-JWT path.
        logger.warning(f"WebSocket external token authentication error: {e}", exc_info=True)

    try:
        payload = verify_token(token)
        user_identifier = payload.get("sub")  # UUID string
        if not user_identifier:
            return None

        # Same revocation verdict as the HTTP dependencies — one function, not a copy:
        # a copy here once dropped `iat`, so logout-all never reached the handshake.
        if token_service.access_token_revoked(payload, db):
            logger.warning(
                "Rejected revoked token on WebSocket (jti=%s...)", str(payload.get("jti"))[:8]
            )
            return None

        user = get_user_by_uuid(db, user_identifier)
        if user is None or not user.is_active or not admitted_on_optional_auth(user, websocket, db):
            return None
        return user
    except (AuthenticationError, SQLAlchemyError) as e:
        reraise_db_overload(e)
        logger.error(f"WebSocket token authentication error: {str(e)}", exc_info=True)
        return None


def _local_claims(token: str) -> dict[str, Any] | None:
    """The claims of a local access token that just authenticated a socket, else ``None``.

    ``None`` for a token an external verifier accepted: it carries no local ``jti``/``exp``
    and its lifetime is that verifier's business.
    """
    try:
        claims: dict[str, Any] = verify_token(token)
    except AuthenticationError:
        # An external verifier accepted this socket, so there is no local claim set to bound it.
        logger.debug("Socket token is not a local access token", exc_info=True)
        return None
    return claims


async def _receive_until(websocket: WebSocket, expires_at: int | None) -> str | None:
    """Next client frame, or ``None`` once the token that opened the socket has expired.

    A socket must not outlive its access token: revocation reaches an open socket only
    through a best-effort broadcast, so the expiry is what bounds a socket that missed one
    (a single-session logout of an earlier token, a replica that missed an outage-time
    revocation). The SPA treats the 4004 close as "re-check the session" and reconnects
    with its refreshed token.
    """
    if expires_at is None:
        return await websocket.receive_text()
    remaining = expires_at - time.time()
    if remaining > 0:
        try:
            return await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
        except TimeoutError:
            pass
    with contextlib.suppress(Exception):
        await websocket.close(code=SESSION_REVOKED_CLOSE, reason="Session expired")
    return None


def _normalize_origin(origin: str) -> str:
    """Lower-case an origin and drop a trailing slash, for exact comparison."""
    return origin.strip().rstrip("/").lower()


def _request_origin(websocket: WebSocket) -> str | None:
    """The origin the handshake was addressed TO, for a same-origin comparison.

    Reconstructed from ``Host`` plus the forwarded scheme, which is what nginx
    supplies for ``/api/ws`` (``proxy_set_header Host $http_host`` +
    ``X-Forwarded-Proto $scheme``). Falls back to the connection's own scheme,
    mapping the WebSocket schemes onto the HTTP ones an ``Origin`` header uses.
    """
    host = websocket.headers.get("host")
    if not host:
        return None
    forwarded = websocket.headers.get("x-forwarded-proto")
    if forwarded:
        # A chain of proxies appends, so the client-facing scheme is the first.
        scheme = forwarded.split(",")[0].strip()
    else:
        scheme = {"ws": "http", "wss": "https"}.get(websocket.url.scheme, websocket.url.scheme)
    return _normalize_origin(f"{scheme}://{host}")


def _may_receive_host_telemetry(user: Any) -> bool:
    """Whether this user's socket may receive host/infrastructure events (``gpu_stats_update``).

    Mirrors ``GET /api/system/stats``, which is gated by the ``system.hardware_stats``
    capability: on a deployment where that capability is hidden from tenants (platform-audience
    capability, hidden for organization users) only platform staff (``is_superuser``) pass, so a
    tenant's own admin never sees another tenant's or the host's telemetry. Where the capability
    is on (community edition) an admin or super admin passes.
    """
    if bool(getattr(user, "is_superuser", False)):
        return True
    return user.role in ELEVATED_ROLES and capability_enabled("system.hardware_stats")


def _origin_is_allowed(websocket: WebSocket) -> bool:
    """Whether this handshake's ``Origin`` may open a socket (issue #903).

    The WebSocket handshake is **not** subject to the same-origin policy and
    browsers attach cookies to it, while this app's CORS and CSRF middleware are
    both ``BaseHTTPMiddleware``-based and do not run on the WebSocket path. So
    without this check, a page on any origin could open a socket in a visiting
    authenticated user's browser, the ``access_token`` cookie would go along, and
    that page would receive the user's live event stream — cross-site WebSocket
    hijacking, needing no prior access.

    Three cases pass:

    * **No ``Origin`` header** — a non-browser client (curl, a cron job, an
      agent). This app's non-UI API is a deliberate feature, and such a client
      carries no ambient cookie for another site to abuse, so refusing it would
      cost real functionality and buy nothing.
    * **Same origin as the request itself.** ``CORS_ORIGINS`` is set by no shipped
      compose file — it defaults to the two Vite dev URLs — because a same-origin
      SPA behind nginx never needs CORS. Keying only off the allowlist would
      therefore refuse the WebSocket in every production deployment.
    * **An origin in ``settings.CORS_ORIGINS``** — the same list ``main.py`` hands
      ``CORSMiddleware``, deliberately reused rather than duplicated, since a
      second allowlist would drift from the first.

    Comparison is exact (after case/trailing-slash normalisation), never a prefix
    test: ``http://localhost:5173.evil.example`` starts with an allowed origin.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True

    allowed = {_normalize_origin(o) for o in settings.CORS_ORIGINS if o}
    if "*" in allowed:
        # Parity with CORSMiddleware. main.py already refuses to boot with a
        # wildcard in a hardened environment, so this only reaches a dev stack.
        return True

    normalized = _normalize_origin(origin)
    return normalized in allowed or normalized == _request_origin(websocket)


def _authenticate_in_session(token: str, websocket: WebSocket) -> User | None:
    """Verify ``token`` in a short-lived session; return the user detached, or None.

    Synchronous on purpose and always called through ``run_in_threadpool``: the pool
    checkout (which waits up to the pool timeout when the pool is busy), the user query and
    the token verification would otherwise run on the event loop that every other request
    and socket in the process shares, and a reconnect storm would stall all of them.
    """
    with session_scope() as db:
        user = _try_authenticate_token(token, db, websocket)
        # session_scope() commits on exit, which expires every attribute on `user`
        # (SQLAlchemy's default expire_on_commit); the caller reads loaded columns after
        # the session is gone. Expunge BEFORE the block exits/commits so they stay
        # readable on the detached instance; after the commit would be too late.
        if user is not None:
            db.expunge(user)
        return user


async def _close_busy(websocket: WebSocket) -> None:
    """Answer a handshake the database could not serve with 1013, not an auth failure.

    RFC 6455 section 7.4.1 / IANA close code 1013 "Try Again Later": the client backs off and
    retries instead of treating the refusal as bad credentials (4003).
    """
    logger.warning("Refused a WebSocket handshake: the database is overloaded")
    with contextlib.suppress(Exception):
        await websocket.close(code=ws_admission.TRY_AGAIN_LATER_CLOSE, reason="Server busy")


async def _authenticate_in_pool(token: str, websocket: WebSocket) -> User | None:
    """:func:`_authenticate_in_session` off the loop; an overload closes 1013 and raises ``_BusyError``."""
    try:
        return await run_in_threadpool(_authenticate_in_session, token, websocket)
    except OVERLOAD_EXCEPTION_CLASSES as exc:
        if overload_cause(exc) is None:
            raise
        await _close_busy(websocket)
        raise _BusyError from exc


class _BusyError(Exception):
    """The handshake was answered 1013 because the database could not serve it."""


async def _authenticate_handshake(websocket: WebSocket) -> tuple[User, str | None] | None:
    """Authenticate an accepted socket by cookie, else by a first ``authenticate`` frame.

    Returns ``(user, token)``, or ``None`` after closing the socket with the refusal's code.
    """
    token = websocket.cookies.get("access_token")
    try:
        user = await _authenticate_in_pool(token, websocket) if token else None
        if user:
            return user, token
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        data = json.loads(raw)
        if data.get("type") != "authenticate" or not data.get("token"):
            await websocket.close(code=4001, reason="Authentication required")
            return None
        auth_token = data["token"]
        user = await _authenticate_in_pool(auth_token, websocket)
        if not user:
            await websocket.close(code=4003, reason="Invalid token")
            return None
        return user, auth_token
    except _BusyError:
        return None
    except TimeoutError:
        await websocket.close(code=4001, reason="Authentication timeout")
    except (json.JSONDecodeError, WebSocketDisconnect):
        with contextlib.suppress(Exception):
            await websocket.close(code=4002, reason="Invalid message")
    except Exception:  # per-connection boundary: any handshake failure closes this socket only
        logger.exception("WebSocket authentication failed unexpectedly")
        with contextlib.suppress(Exception):
            await websocket.close(code=4003, reason="Authentication error")
    return None


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint with first-message authentication.

    Authentication flow:
    0. Reject the handshake outright if ``Origin`` is not allowed
       (:func:`_origin_is_allowed`) — before ``accept()``, so a foreign page never
       holds a server-side connection object at all.
    1. Accept the raw WebSocket connection.
    2. Try cookie-based auth (``access_token`` cookie).
    3. If no cookie, wait up to 10 s for a first-message ``authenticate`` frame.
    4. On success, register the connection and proceed with the normal
       message loop; on failure, close with an appropriate error code.

    Deliberately does NOT take ``db: Session = Depends(get_db)``: a WebSocket route's
    injected dependencies stay open for the *whole connection*, not just one
    request/response like HTTP — and this connection can live for hours. DB access is
    only needed for the few-millisecond auth check below, so each attempt opens its own
    short-lived ``session_scope()`` instead. Holding a session open for the connection's
    full lifetime left an idle, uncommitted transaction checked out of the pool for as
    long as the socket stayed connected: wasted a pool slot, silently discarded any
    write from the external-auth user-sync path (``get_db``'s cleanup only closes, which
    rolls back — it never commits), and could block a schema migration waiting on the
    same table for as long as any client stayed connected.
    """
    # Origin gate FIRST — before setup_redis(), before accept(). Closing a
    # WebSocket that was never accepted rejects the handshake itself, so an
    # unauthorized peer never reaches a message loop and never costs a connection.
    # Note the two faces of that refusal: on the wire uvicorn turns a pre-accept
    # close into an **HTTP 403** on the handshake (verified against a real
    # uvicorn), because there is no WebSocket yet to carry a close code; the 4403
    # travels in the ASGI message and is what an ASGI-level client (Starlette's
    # TestClient) observes. Both mean the same thing: no socket was opened.
    if not _origin_is_allowed(websocket):
        logger.warning(
            "Rejected WebSocket handshake from disallowed origin %r (host=%r)",
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
        )
        await websocket.close(code=4403, reason="Origin not allowed")
        return

    # Initialize Redis subscriber if not already running
    setup_redis()

    # Admission limits, still before accept(): a refused handshake never holds a socket.
    if not ws_admission.process_has_room(manager.open_sockets()):
        logger.warning("Refused a WebSocket handshake: this process is at its socket limit")
        await websocket.close(code=ws_admission.TRY_AGAIN_LATER_CLOSE, reason="Server busy")
        return
    if not await ws_admission.handshake_allowed(redis_client, resolve_client_ip(websocket)):
        logger.warning("Refused a WebSocket handshake: per-IP handshake rate limit reached")
        await websocket.close(
            code=ws_admission.TRY_AGAIN_LATER_CLOSE, reason="Too many connections"
        )
        return

    # Accept the connection, then authenticate
    await websocket.accept()

    authenticated = await _authenticate_handshake(websocket)
    if authenticated is None:
        return
    user, auth_token = authenticated

    if not ws_admission.user_has_room(manager.open_sockets(user.id)):
        logger.warning("Refused a WebSocket for user %s: per-user socket limit reached", user.id)
        await websocket.close(
            code=ws_admission.TRY_AGAIN_LATER_CLOSE, reason="Too many connections"
        )
        return

    # Register the authenticated connection
    manager.connect(
        websocket,
        user.id,
        host_telemetry=_may_receive_host_telemetry(user),
        user_uuid=str(user.uuid),
        claims=_local_claims(auth_token) if auth_token else None,
    )
    # Send initial connection status
    await websocket.send_text(
        json.dumps(
            {
                "type": "connection_established",
                "message": "Connected to WebSocket server",
            }
        )
    )

    await _serve_authenticated(websocket, user.id)


async def _serve_authenticated(websocket: WebSocket, user_id: int) -> None:
    """Message loop of a registered socket, until the peer leaves or its token expires."""
    owner = manager.socket_owners.get(websocket)
    expires_at = owner.expires_at if owner else None
    try:
        while True:
            # Wait for messages (keep the connection alive)
            data = await _receive_until(websocket, expires_at)
            if data is None:
                manager.disconnect(websocket, user_id)
                return
            # Silently drop auth messages that arrive in the echo loop
            # (e.g. from cookie-auth clients that also send first-message auth)
            try:
                msg = json.loads(data)
                if isinstance(msg, dict) and msg.get("type") == "authenticate":
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            # Echo back for debugging/heartbeat
            await websocket.send_text(json.dumps({"type": "echo", "data": data}))
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
    except Exception:  # per-connection boundary: one socket's failure must not reach the others
        logger.exception("WebSocket error")
        manager.disconnect(websocket, user_id)
