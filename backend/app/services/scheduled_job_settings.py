"""Plumbing shared by the services that keep scheduled-job settings in ``SystemSettings``.

``backup_service``, ``directory_sync_service`` and ``watch_settings_service`` each read a
batch of ``<prefix>.*`` keys with one ``get_settings_map`` SELECT, then turn the strings
into typed values with the same rules. Those rules live here once; the key names, coded
defaults and per-service validation stay in each service.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.services import system_settings_service as sss
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

_TRUTHY = ("true", "1", "yes", "on")


@contextlib.contextmanager
def session_scope(db: Session | None) -> Iterator[Session]:
    """Yield the passed session, or a short-lived one that is closed after use."""
    if db is not None:
        yield db
        return
    from app.db.base import SessionLocal

    own = SessionLocal()
    try:
        yield own
    finally:
        own.close()


def bool_value(vals: Mapping[str, str | None], key: str, default: bool) -> bool:
    """``key`` read as a bool; ``default`` when the key is absent or ``None``."""
    v = vals.get(key)
    return v.lower() in _TRUTHY if v is not None else default


def int_value(vals: Mapping[str, str | None], key: str, default: int) -> int:
    """``key`` read as an int; ``default`` when absent or not an integer string."""
    v = vals.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def json_value(vals: Mapping[str, str | None], key: str) -> Any:
    """``key`` decoded as JSON; ``None`` when absent, empty or not valid JSON."""
    raw = vals.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def store_encrypted_secret(db: Session, key: str, secret: str, description: str) -> None:
    """Encrypt ``secret`` and persist it under ``key``; an empty string clears the key."""
    if not secret:
        sss.set_setting(db, key, "", description)
        return
    encrypted = encrypt_api_key(secret)
    if not encrypted:
        raise ValueError("Failed to encrypt S3 secret key")
    sss.set_setting(db, key, encrypted, description)


def read_encrypted_secret(db: Session | None, key: str) -> str | None:
    """Decrypt and return the secret stored under ``key`` (runtime-only, never via an API)."""
    with session_scope(db) as s:
        raw = sss.get_setting(s, key)
    if not raw:
        return None
    return decrypt_api_key(raw)
