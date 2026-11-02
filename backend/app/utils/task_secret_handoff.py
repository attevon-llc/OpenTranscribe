"""Short-lived, encrypted hand-off of secrets to Celery tasks.

A Celery argument is written to the broker, kept across retries and shown by monitoring
tools, so a credential must never be one. The API process stores the secret here and passes
the task only an opaque reference; the task fetches it when it runs and discards it when
it is finished.

The stored value is encrypted with the application's AES-GCM helper, bound (as associated
data) to a caller-supplied context such as the user and file, carries its own reference and
expires after ``TASK_SECRET_HANDOFF_TTL_SECONDS``. A reference replayed into another
context therefore fails to decrypt.
"""

import json
import logging
import uuid

from app.core.constants import TASK_SECRET_HANDOFF_TTL_SECONDS
from app.core.redis import get_redis
from app.utils.encryption import decrypt_value
from app.utils.encryption import encrypt_value

logger = logging.getLogger(__name__)

_KEY_PREFIX = "task_secret:"


class TaskSecretUnavailableError(Exception):
    """The handed-off secret expired, was already discarded, or cannot be read."""


def _key(ref: str) -> str:
    return f"{_KEY_PREFIX}{ref}"


def _aad(context: str) -> bytes:
    return f"task-secret:{context}".encode()


def stash_task_secret(
    values: dict[str, str | None], *, context: str, ttl: int | None = None
) -> str | None:
    """Store ``values`` encrypted with a TTL and return the opaque reference.

    Returns None when every value is empty, so callers pass no reference at all.

    Raises:
        TaskSecretUnavailableError: If the values cannot be encrypted or stored.
    """
    present = {k: v for k, v in values.items() if v}
    if not present:
        return None
    ref = uuid.uuid4().hex
    token = encrypt_value(json.dumps({"ref": ref, "values": present}), aad=_aad(context))
    if token is None:
        raise TaskSecretUnavailableError("Could not protect the credentials for this request")
    try:
        get_redis().setex(_key(ref), ttl or TASK_SECRET_HANDOFF_TTL_SECONDS, token)
    except Exception as exc:
        raise TaskSecretUnavailableError(
            "Could not store the credentials for this request"
        ) from exc
    return ref


def fetch_task_secret(ref: str, *, context: str) -> dict[str, str]:
    """Return the values stored under ``ref`` (does not consume them, so retries work).

    Raises:
        TaskSecretUnavailableError: With a message that never contains secret material.
    """
    try:
        token = get_redis().get(_key(ref))
    except Exception as exc:
        raise TaskSecretUnavailableError("The credential store is unavailable") from exc
    if not token:
        raise TaskSecretUnavailableError(
            "The credentials for this request expired before the task ran; submit it again"
        )
    plain = decrypt_value(token.decode() if isinstance(token, bytes) else token, aad=_aad(context))
    try:
        data = json.loads(plain) if plain else None
    except ValueError:
        data = None
    if (
        not isinstance(data, dict)
        or data.get("ref") != ref
        or not isinstance(data.get("values"), dict)
    ):
        raise TaskSecretUnavailableError("The stored credentials could not be read")
    return {str(k): str(v) for k, v in data["values"].items()}


def discard_task_secret(ref: str | None) -> None:
    """Delete the stored secret; never raises (it expires on its own regardless)."""
    if not ref:
        return
    try:
        get_redis().delete(_key(ref))
    except Exception:
        logger.warning("Could not discard a task credential entry; it will expire by TTL")


def media_credentials_context(user_id: int, file_uuid: str) -> str:
    """Binding context for a protected-media import's credentials: the user and the file."""
    return f"media-cred:{user_id}:{file_uuid}"
