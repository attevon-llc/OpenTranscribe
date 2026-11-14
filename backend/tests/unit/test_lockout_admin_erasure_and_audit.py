"""Erasure helpers and unlock audit lines of ``lockout_admin`` (issue #1373).

``erase_identifier`` / ``identifier_has_record`` (GDPR erasure and its post-check) had no
covering test in the module's mutation run, and the three ``unlock_account`` log lines are the
operator-visible admin audit trail: each must keep its level, logger and masked identifier.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.auth import lockout
from app.auth import lockout_admin
from app.auth import lockout_store
from app.auth.lockout import LockoutRecord

LOGGER = "app.auth.lockout_admin"
ALICE = "alice@example.com"
ALICE_MASKED = "a***@example.com"


@pytest.fixture
def memory(monkeypatch) -> lockout_store.InMemoryLockoutStore:
    store = lockout_store.InMemoryLockoutStore()
    monkeypatch.setattr(lockout_store, "_redis_client", None)
    monkeypatch.setattr(lockout_store, "_get_redis_client", lambda: None)
    monkeypatch.setattr(lockout_store, "_in_memory_store", store)
    monkeypatch.setattr(lockout_store, "_store_initialized", True)
    return store


class _RedisKV:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0


def _put(store, identifier: str, locked_for: timedelta | None) -> None:
    now = datetime.now(UTC)
    rec = LockoutRecord(
        identifier=identifier,
        failed_attempts=4,
        lockout_count=2,
        locked_until=(now + locked_for).isoformat() if locked_for is not None else None,
        first_failed_attempt=now.isoformat(),
        last_failed_attempt=now.isoformat(),
    )
    store.set(lockout._lockout_key(identifier), json.dumps(rec.to_dict()))


def _redis_with_fallback(monkeypatch):
    redis = _RedisKV()
    fallback = lockout_store.InMemoryLockoutStore()
    monkeypatch.setattr(lockout_store, "_redis_client", redis)
    monkeypatch.setattr(lockout_store, "_in_memory_store", fallback)
    monkeypatch.setattr(lockout_store, "_store_initialized", True)
    return redis, fallback


def test_erase_removes_the_record_and_reports_one(memory) -> None:
    _put(memory, ALICE, None)
    assert lockout_admin.erase_identifier(ALICE) == 1
    assert memory.get(lockout._lockout_key(ALICE)) is None


def test_erase_of_absent_identifier_reports_zero(memory) -> None:
    assert lockout_admin.erase_identifier(ALICE) == 0


def test_erase_normalizes_the_identifier(memory) -> None:
    _put(memory, ALICE, None)
    assert lockout_admin.erase_identifier("  Alice@Example.com ") == 1
    assert memory.get(lockout._lockout_key(ALICE)) is None


def test_erase_clears_redis_and_the_in_memory_fallback(monkeypatch) -> None:
    redis, fallback = _redis_with_fallback(monkeypatch)
    _put(redis, ALICE, None)
    _put(fallback, ALICE, None)
    assert lockout_admin.erase_identifier(ALICE) == 2
    assert redis.get(lockout._lockout_key(ALICE)) is None
    assert fallback.get(lockout._lockout_key(ALICE)) is None


def test_erase_with_only_a_fallback_copy_removes_it(monkeypatch) -> None:
    redis, fallback = _redis_with_fallback(monkeypatch)
    _put(fallback, ALICE, None)
    assert lockout_admin.erase_identifier(ALICE) == 1
    assert fallback.get(lockout._lockout_key(ALICE)) is None


def test_has_record_false_then_true_then_false_after_erase(memory) -> None:
    assert lockout_admin.identifier_has_record(ALICE) is False
    _put(memory, ALICE, None)
    assert lockout_admin.identifier_has_record(ALICE) is True
    assert lockout_admin.identifier_has_record("  ALICE@example.com") is True
    lockout_admin.erase_identifier(ALICE)
    assert lockout_admin.identifier_has_record(ALICE) is False


def test_has_record_sees_a_fallback_only_record(monkeypatch) -> None:
    redis, fallback = _redis_with_fallback(monkeypatch)
    assert lockout_admin.identifier_has_record(ALICE) is False
    _put(fallback, ALICE, None)
    assert lockout_admin.identifier_has_record(ALICE) is True


def test_has_record_sees_a_redis_only_record(monkeypatch) -> None:
    redis, _fallback = _redis_with_fallback(monkeypatch)
    _put(redis, ALICE, None)
    assert lockout_admin.identifier_has_record(ALICE) is True


def _records(caplog, level):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == level]


def test_unlock_unknown_account_logs_masked_identifier(memory, caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert lockout_admin.unlock_account(ALICE) is False
    (rec,) = _records(caplog, logging.INFO)
    assert "unknown account" in rec.getMessage()
    assert ALICE_MASKED in rec.getMessage()


def test_unlock_non_locked_account_logs_masked_identifier(memory, caplog) -> None:
    _put(memory, ALICE, None)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert lockout_admin.unlock_account(ALICE) is False
    (rec,) = _records(caplog, logging.INFO)
    assert "non-locked account" in rec.getMessage()
    assert ALICE_MASKED in rec.getMessage()


def test_unlock_locked_account_warns_with_identity_deadline_and_count(memory, caplog) -> None:
    _put(memory, ALICE, timedelta(minutes=10))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert lockout_admin.unlock_account(ALICE) is True
    (rec,) = _records(caplog, logging.WARNING)
    msg = rec.getMessage()
    assert "Admin unlocking account" in msg
    assert ALICE_MASKED in msg
    assert "was locked until 20" in msg
    assert "lockout #2" in msg
    assert ALICE not in msg
