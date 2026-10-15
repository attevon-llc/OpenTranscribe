"""The one place the Hugging Face access token is resolved, stored and checked.

The token downloads gated or private models (the pyannote speaker models, private repos).
Precedence: the value a super_admin saved in Settings (stored encrypted in
``SystemSettings``) > the server environment (``HUGGINGFACE_TOKEN``) > none.

Rules every caller relies on:

* :func:`get_hf_token` needs no request. It opens a SHORT session of its own, closes it
  before returning, and caches the result for :data:`CACHE_TTL_SECONDS`, so a model load does
  not hit the database and a token changed in the UI reaches a running worker at its next
  model load (within the TTL) without a restart.
* **Never call it from ``init_worker_process`` or any fork/init hook** - no DB, no network,
  no locks there (issue #631). That hook uses :func:`env_token`, which is a dict read.
* A database failure falls back to the environment value (and is not cached), so a degraded
  database cannot turn into a stuck "no token" answer.
* This is the only module allowed to read the environment variable; a structural test
  (``tests/unit/test_hf_token_single_reader.py``) fails on any other reader.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.system_settings import SystemSettings
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

logger = logging.getLogger(__name__)

SETTING_KEY = "transcription.hf_access_token_encrypted"
CACHE_TTL_SECONDS = 60.0
#: Upper bound on the resolver's own DB read; it can run on a worker's main thread.
_DB_STATEMENT_TIMEOUT_MS = 3000

#: Real Hugging Face tokens are ``hf_`` plus ~34 alphanumerics; allow headroom both ways.
TOKEN_SHAPE = re.compile(r"^hf_[A-Za-z0-9]{20,}$")
TOKEN_MAX_LEN = 256

TokenSource = Literal["app", "environment", "none"]

#: Fixed outcomes of the Test action. Never an exception text or account detail.
TEST_VALID = "The token is valid."
TEST_INVALID = "The token was rejected. It is invalid or has expired."
TEST_UNREACHABLE = "Could not reach Hugging Face."

_lock = threading.Lock()
_cache: tuple[float, str | None] | None = None
_clock = time.monotonic
#: True while the HF_TOKEN env var holds a value this module put there (so we may replace it).
_published_by_us = False


def env_token() -> str | None:
    """The server-environment token. A plain read: safe in fork/init hooks."""
    return settings.HUGGINGFACE_TOKEN or None


def validate_token(value: str) -> str:
    """Trim, then require the ``hf_`` shape. The ValueError message never holds the value."""
    token = value.strip()
    if len(token) > TOKEN_MAX_LEN or not TOKEN_SHAPE.match(token):
        raise ValueError("invalid token shape")
    return token


def invalidate_cache() -> None:
    """Drop the cached answer in this process (called after a save/remove)."""
    global _cache
    with _lock:
        _cache = None


def _read_stored(db: Session) -> str | None:
    row = db.query(SystemSettings).filter(SystemSettings.key == SETTING_KEY).first()
    if row is None or not row.value:
        return None
    return decrypt_api_key(str(row.value))


def _resolve_uncached() -> tuple[str | None, bool]:
    """(token, cacheable). Not cacheable when the DB could not be read."""
    from app.db.base import SessionLocal

    db = SessionLocal()
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text(f"SET LOCAL statement_timeout = {_DB_STATEMENT_TIMEOUT_MS}"))
        stored = _read_stored(db)
    except Exception:  # noqa: BLE001 - degrade to the environment value, never raise to a model load
        logger.warning("Could not read the saved Hugging Face token; using the environment value")
        return env_token(), False
    finally:
        db.close()
    return (stored or env_token()), True


def get_hf_token() -> str | None:
    """Saved token, else the environment token, else None. Cached for a short TTL."""
    global _cache
    with _lock:
        if _cache is not None and _clock() - _cache[0] < CACHE_TTL_SECONDS:
            return _cache[1]
    token, cacheable = _resolve_uncached()
    if cacheable:
        with _lock:
            _cache = (_clock(), token)
    _publish(token)
    return token


def _publish(token: str | None) -> None:
    """Export to ``HF_TOKEN`` so un-parameterised Hub calls use the same credential.

    A dict write only. An operator-supplied ``HF_TOKEN`` / ``HUGGING_FACE_HUB_TOKEN`` wins,
    except one this module published earlier, which a newer value replaces.
    """
    global _published_by_us
    import os

    if not token:
        return
    foreign = os.environ.get("HUGGING_FACE_HUB_TOKEN") or (
        os.environ.get("HF_TOKEN") if not _published_by_us else None
    )
    if foreign:
        return
    os.environ["HF_TOKEN"] = token
    _published_by_us = True


def publish_env_token_for_fork() -> bool:
    """Network- and DB-free export of the environment token (``init_worker_process``)."""
    token = env_token()
    if not token:
        return False
    import os

    if os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"):
        return False
    _publish(token)
    return True


def get_status(db: Session) -> tuple[bool, TokenSource]:
    """(is_set, source) for the admin UI. Reads the DB row directly: never cached."""
    if _read_stored(db):
        return True, "app"
    if env_token():
        return True, "environment"
    return False, "none"


def save_token(db: Session, token: str) -> None:
    """Store ``token`` (already validated) encrypted. Raises RuntimeError if encryption fails."""
    encrypted = encrypt_api_key(token)
    if not encrypted:
        raise RuntimeError("encryption failed")
    row = db.query(SystemSettings).filter(SystemSettings.key == SETTING_KEY).first()
    if row is None:
        db.add(
            SystemSettings(
                key=SETTING_KEY,
                value=encrypted,
                description="Hugging Face access token (encrypted, write-only)",
            )
        )
    else:
        row.value = encrypted  # type: ignore[assignment]
    db.commit()
    invalidate_cache()


def clear_token(db: Session) -> bool:
    """Remove the saved token. True when there was one."""
    row = db.query(SystemSettings).filter(SystemSettings.key == SETTING_KEY).first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    invalidate_cache()
    return True


#: Seconds to wait for Hugging Face. ``HfApi.whoami`` takes no timeout, so the request is
#: made directly.
_TEST_TIMEOUT_S = 8.0


def check_token(token: str) -> str:
    """Ask Hugging Face whether ``token`` is accepted. Returns one of the fixed sentences."""
    import requests
    from huggingface_hub import constants

    try:
        response = requests.get(
            f"{constants.ENDPOINT}/api/whoami-v2",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TEST_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - the exception text can carry the URL/headers; never surface it
        return TEST_UNREACHABLE
    if response.status_code == 200:
        return TEST_VALID
    if response.status_code in (401, 403):
        return TEST_INVALID
    return TEST_UNREACHABLE
