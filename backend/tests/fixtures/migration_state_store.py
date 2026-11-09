"""An in-memory Redis for the migration lock and progress records.

The migration endpoints refuse to start when Redis cannot be read, because an unreadable lock is
not a released one. A suite that exercises their other behaviour therefore needs a store that
answers; this one backs ``get``/``set``/``delete``/``incr``/``decr``/``expire``/``ping`` with a
dict. A test of the outage itself installs a client that raises ``redis.ConnectionError``.
"""

from __future__ import annotations

import pytest


class InMemoryMigrationRedis:
    """The Redis operations ``MigrationLockService`` and ``MigrationProgressService`` use."""

    def __init__(self) -> None:
        self.store: dict[str, str | int] = {}

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> str | int | None:
        return self.store.get(key)

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.store.pop(key, None) is not None)

    def incr(self, key: str) -> int:
        self.store[key] = int(self.store.get(key, 0)) + 1
        return int(self.store[key])

    def decr(self, key: str) -> int:
        self.store[key] = int(self.store.get(key, 0)) - 1
        return int(self.store[key])

    def expire(self, key: str, seconds: int) -> bool:
        return key in self.store


@pytest.fixture
def migration_state_store(monkeypatch) -> InMemoryMigrationRedis:
    """Point the migration lock and progress services at an in-memory Redis."""
    from app.services.migration_lock_service import MigrationLockService
    from app.services.migration_progress_service import MigrationProgressService

    client = InMemoryMigrationRedis()
    # The services cache the client they resolved, and the module-level instances outlive a
    # test, so the class-level accessors are what is replaced (undone on teardown).
    monkeypatch.setattr(MigrationLockService, "_redis", property(lambda _self: client))
    monkeypatch.setattr(MigrationProgressService, "redis_client", property(lambda _self: client))
    return client
