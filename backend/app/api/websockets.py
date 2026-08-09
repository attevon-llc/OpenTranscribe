import asyncio
import contextlib
import json
import logging

import redis.asyncio as redis
from fastapi import APIRouter
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.orm import Session

from app.auth.token_service import token_service
from app.db.session_utils import session_scope

from ..core.config import settings
from ..core.security import verify_token
from ..models.user import User

# Set up logging
logger = logging.getLogger(__name__)

router = APIRouter()


# Connection manager to handle WebSocket connections
class ConnectionManager:
    def __init__(self):
        # user_id -> List[WebSocket]
        self.active_connections: dict[int, list[WebSocket]] = {}

    def connect(self, websocket: WebSocket, user_id: int):
        """Register a WebSocket connection for a user (accept() must be called before this).

        Bookkeeping only — no I/O — so it is a plain function, mirroring ``disconnect``.
        It is still owned by the connection's own coroutine; nothing outside this
        process may touch ``manager`` (publish via ``utils/websocket_notify.py``).
        """
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
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
        for ws in sockets:
            # A socket already torn down by the peer raises here; that is a successful
            # drain, not a failure.
            with contextlib.suppress(Exception):
                await ws.close(code=code)
        self.active_connections.clear()
        if sockets:
            logger.info("Drained %d WebSocket connection(s) on shutdown", len(sockets))
        return len(sockets)

    def disconnect(self, websocket: WebSocket, user_id: int):
        if user_id in self.active_connections:
            self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
            logger.info(
                f"User {user_id} disconnected. Total connections: {len(self.active_connections)}"
            )

    async def send_personal_message(self, user_id: int, message: dict):
        if user_id in self.active_connections:
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_text(json.dumps(message))
                except Exception as e:
                    logger.error(f"Error sending message to user {user_id}: {str(e)}")

    async def broadcast(self, message: dict):
        for user_id in self.active_connections:
            await self.send_personal_message(user_id, message)


# Create connection manager instance
manager = ConnectionManager()

# Redis client for pub/sub
redis_client: redis.Redis | None = None


def setup_redis():
    """Initialize Redis connection for pub/sub notifications.

    ``redis.asyncio.from_url`` only builds a lazily-connecting pool and
    ``asyncio.create_task`` needs a running loop, not an ``await`` — so nothing here
    yields. Declaring it ``async def`` claimed otherwise (issue #320); callers must
    still invoke it from a coroutine so ``create_task`` has a loop to attach to.
    """
    global redis_client
    if not redis_client:
        # health_check_interval + keepalive keep the long-lived pub/sub connection
        # from being reaped while idle (the root cause of the subscriber silently
        # dying) and let redis-py detect a dead socket proactively.
        redis_client = redis.from_url(
            settings.REDIS_URL,
            health_check_interval=30,
            socket_keepalive=True,
        )
        # Start Redis subscriber in background
        asyncio.create_task(redis_subscriber())


async def _dispatch_notification(message: dict) -> None:
    """Parse one pub/sub message and forward it to the right WebSocket client(s)."""
    if message.get("type") != "message":
        return
    try:
        notification_data = json.loads(message["data"])
    except Exception as e:
        logger.error(f"Error decoding Redis notification: {e}")
        return

    user_id = notification_data.get("user_id")
    notification_type = notification_data.get("type")
    is_broadcast = notification_data.get("broadcast", False)
    data = notification_data.get("data", {})

    if is_broadcast and notification_type:
        await manager.broadcast({"type": notification_type, "data": data})
    elif user_id and notification_type:
        await manager.send_personal_message(user_id, {"type": notification_type, "data": data})
    else:
        logger.warning(f"Invalid notification data: {notification_data}")


async def redis_subscriber():
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
        except Exception as e:
            logger.error(f"Unexpected Redis subscriber error: {e}; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


# Function to publish notification to Redis (for use from other processes)
async def publish_notification(user_id: int, notification_type: str, data: dict):
    """Publish notification via Redis pub/sub."""
    if not redis_client:
        setup_redis()

    notification = {"user_id": user_id, "type": notification_type, "data": data}

    try:
        assert redis_client is not None
        await redis_client.publish("websocket_notifications", json.dumps(notification))
        logger.info(f"Published notification to Redis for user {user_id}: {notification_type}")
    except Exception as e:
        logger.error(f"Failed to publish notification to Redis: {e}")


def _try_authenticate_token(
    token: str, db: Session, websocket: WebSocket | None = None
) -> User | None:
    """Validate a token (local JWT or external provider) and return the User, or None.

    Cloud-edition seam: an external session token authenticates the socket via the
    registered external verifier before the local-JWT path, mirroring
    ``get_current_user``. The community edition registers no verifier, so the
    external branch is skipped and behavior is identical (local JWT only). On any
    failure (no match, invalid token, inactive user) the caller closes with 4003.
    """
    # Cloud-edition seam: offer the token to registered external verifiers first.
    try:
        from app.auth.provider_registry import has_verifiers

        if has_verifiers():
            from app.auth.external_sync import sync_external_user_to_db
            from app.auth.provider_registry import verify_external_token

            # The TokenVerifier protocol only needs the token to verify; the WS
            # object exposes the same headers/cookies a verifier might read, so
            # it is a safe stand-in for the HTTP Request on this path.
            external_identity = verify_external_token(token, websocket)  # type: ignore[arg-type]
            if external_identity is not None:
                external_user = sync_external_user_to_db(db, external_identity)
                if not external_user.is_active:
                    return None
                return external_user
    except Exception as e:
        # Never let an external-auth problem break local WS authentication;
        # fall through to the local-JWT path.
        logger.debug(f"WebSocket external token authentication error: {e}")

    try:
        payload = verify_token(token)
        user_identifier = payload.get("sub")  # UUID string
        if not user_identifier:
            return None

        # Mirror the HTTP path's checks (endpoints/auth.py) — this branch used to skip
        # BOTH, so a revoked token or a deactivated account still opened a socket and
        # kept receiving that user's events (issue #284 A0.7).
        token_jti = payload.get("jti")
        if (
            settings.TOKEN_REVOCATION_ENABLED
            and token_jti
            and token_service.is_token_revoked(token_jti, db=db, user_uuid=user_identifier)
        ):
            logger.warning("Rejected revoked token on WebSocket (jti=%s...)", token_jti[:8])
            return None

        user = db.query(User).filter(User.uuid == user_identifier).first()
        if user is None or not user.is_active:
            return None
        return user  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(f"WebSocket token authentication error: {str(e)}")
        return None


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint with first-message authentication.

    Authentication flow:
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
    # Initialize Redis subscriber if not already running
    setup_redis()

    # Accept the connection first, then authenticate
    await websocket.accept()

    user: User | None = None

    # 1. Try cookie-based auth
    token = websocket.cookies.get("access_token")
    if token:
        with session_scope() as db:
            user = _try_authenticate_token(token, db, websocket)
            # session_scope() commits on exit, which expires every attribute on `user`
            # (SQLAlchemy's default expire_on_commit) — the only attribute touched after
            # this block is user.id, which must survive the session closing. Expunge
            # BEFORE the block exits/commits so the already-loaded columns stay readable
            # on the now-detached instance; expunging after commit would be too late,
            # since expiry already happened.
            if user is not None:
                db.expunge(user)

    # 2. If no cookie auth, wait for first-message authentication
    if not user:
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            data = json.loads(raw)
            if data.get("type") != "authenticate" or not data.get("token"):
                await websocket.close(code=4001, reason="Authentication required")
                return
            with session_scope() as db:
                user = _try_authenticate_token(data["token"], db, websocket)
                if user is not None:
                    db.expunge(user)
            if not user:
                await websocket.close(code=4003, reason="Invalid token")
                return
        except TimeoutError:
            await websocket.close(code=4001, reason="Authentication timeout")
            return
        except (json.JSONDecodeError, WebSocketDisconnect):
            with contextlib.suppress(Exception):
                await websocket.close(code=4002, reason="Invalid message")
            return
        except Exception:
            with contextlib.suppress(Exception):
                await websocket.close(code=4003, reason="Authentication error")
            return

    # Register the authenticated connection
    manager.connect(websocket, user.id)

    # Send initial connection status
    await websocket.send_text(
        json.dumps(
            {
                "type": "connection_established",
                "message": "Connected to WebSocket server",
            }
        )
    )

    try:
        while True:
            # Wait for messages (keep the connection alive)
            data = await websocket.receive_text()
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
        manager.disconnect(websocket, user.id)
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}")
        manager.disconnect(websocket, user.id)


# Function to send notification to a user
async def send_notification(user_id: int, notification_type: str, data: dict):
    message = {"type": notification_type, "data": data}
    await manager.send_personal_message(user_id, message)


# Function to broadcast a notification to all connected users
async def broadcast_notification(notification_type: str, data: dict):
    message = {"type": notification_type, "data": data}
    await manager.broadcast(message)
