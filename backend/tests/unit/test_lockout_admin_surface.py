"""Characterization tests for the lockout admin surface that had no test at all.

``list_locked_accounts``, ``iter_lockout_records``, ``current_store_kind`` and
``reset_lockout_state`` (issue #570) back ``GET /admin/locked-accounts`` and
``POST /admin/locked-accounts/{identifier}/reset``. Nothing in the suite called any of them,
so a reset that skipped the compare-and-set, a listing that leaked unlocked records into the
default queue view, or a cursor that skipped a record would all have shipped green.

The store is installed through ``lockout_store``'s own state globals (the production path), not
by patching ``_get_store``, so these tests read and write through the same store the counting
path uses.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.auth import lockout
from app.auth import lockout_admin
from app.auth import lockout_store
from app.auth.lockout import LockoutRecord
from app.core.process_auth_settings import publish_process_auth_setting


class _CasRedis:
    """Redis stand-in: GET/SET/SCAN/MGET plus the ``_CAS_LUA`` compare-and-set contract.

    ``conflicts`` makes the next N script calls lose to a simulated concurrent writer that
    bumps ``failed_attempts`` between the caller's read and its write.
    """

    def __init__(self, conflicts: int = 0):
        self.data: dict[str, str] = {}
        self.conflicts = conflicts
        self.cas_calls = 0

    def pipeline(self, transaction: bool = True):
        raise AssertionError("the lockout paths must not use pipeline()")

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.data.get(k) for k in keys]

    def scan(self, cursor: int = 0, match: str = "*", count: int = 10):
        keys = sorted(k for k in self.data if k.startswith(match.rstrip("*")))
        page = keys[cursor : cursor + count]
        nxt = cursor + count
        return (0 if nxt >= len(keys) else nxt), [k.encode() for k in page]

    def register_script(self, src: str):
        def run(keys, args):
            self.cas_calls += 1
            key = keys[0]
            expected, new_value, _ttl, expect_missing = args
            if self.conflicts > 0:
                self.conflicts -= 1
                rec = json.loads(self.data[key])
                rec["failed_attempts"] += 1
                self.data[key] = json.dumps(rec)
            current = self.data.get(key)
            if expect_missing == "1" and current:
                return [0, current]
            if expect_missing != "1" and current != expected:
                return [0, current or ""]
            self.data[key] = new_value
            return [1, ""]

        return run


@pytest.fixture(autouse=True)
def _lockout_on(monkeypatch):
    publish_process_auth_setting("account_lockout_enabled", True)
    monkeypatch.setattr(lockout, "_cas_script", None)
    monkeypatch.setattr(lockout, "_cas_script_client", None)
    monkeypatch.setattr(lockout_store, "_last_redis_probe", 0.0)


@pytest.fixture
def memory(monkeypatch) -> lockout_store.InMemoryLockoutStore:
    store = lockout_store.InMemoryLockoutStore()
    monkeypatch.setattr(lockout_store, "_redis_client", None)
    monkeypatch.setattr(lockout_store, "_in_memory_store", store)
    monkeypatch.setattr(lockout_store, "_store_initialized", True)
    return store


def _redis(monkeypatch, client: _CasRedis) -> _CasRedis:
    monkeypatch.setattr(lockout_store, "_redis_client", client)
    monkeypatch.setattr(lockout_store, "_in_memory_store", None)
    monkeypatch.setattr(lockout_store, "_store_initialized", True)
    return client


def _put(store, identifier: str, *, locked_for: timedelta | None, count: int = 2) -> None:
    now = datetime.now(UTC)
    record = LockoutRecord(
        identifier=identifier,
        failed_attempts=4,
        lockout_count=count,
        locked_until=(now + locked_for).isoformat() if locked_for is not None else None,
        first_failed_attempt=(now - timedelta(minutes=5)).isoformat(),
        last_failed_attempt=now.isoformat(),
    )
    store.set(lockout._lockout_key(identifier), json.dumps(record.to_dict()))


def _get(store, identifier: str) -> dict:
    raw = store.get(lockout._lockout_key(identifier))
    assert raw is not None, f"no record for {identifier}"
    record: dict = json.loads(raw)
    return record


# ── current_store_kind ─────────────────────────────────────────────────────────────


def test_store_kind_reports_memory_on_the_fallback(memory) -> None:
    assert lockout_admin.current_store_kind() == "memory"


def test_store_kind_reports_redis_when_a_client_is_in_use(monkeypatch) -> None:
    _redis(monkeypatch, _CasRedis())
    assert lockout_admin.current_store_kind() == "redis"


# ── iter_lockout_records / list_locked_accounts ────────────────────────────────────


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_paging_visits_every_record_once_in_key_order(monkeypatch, memory, backend) -> None:
    store = memory if backend == "memory" else _redis(monkeypatch, _CasRedis())
    for ident in ("c@example.com", "a@example.com", "b@example.com"):
        _put(store, ident, locked_for=timedelta(minutes=10))
    store.set("unrelated:key", "{}")

    seen: list[str] = []
    cursor = None
    for _ in range(4):
        page, cursor = lockout_admin.iter_lockout_records(cursor, 2)
        seen.extend(r.identifier for r in page)
        if cursor is None:
            break

    assert seen == ["a@example.com", "b@example.com", "c@example.com"]
    assert cursor is None, "the scan must report exhaustion, not loop forever"


def test_listing_hides_unlocked_records_unless_asked(memory) -> None:
    _put(memory, "locked@example.com", locked_for=timedelta(minutes=10))
    _put(memory, "expired@example.com", locked_for=timedelta(minutes=-1))
    _put(memory, "never@example.com", locked_for=None)

    accounts, next_cursor, enabled = lockout_admin.list_locked_accounts()
    assert [a["identifier"] for a in accounts] == ["locked@example.com"]
    assert accounts[0]["is_locked"] is True
    assert (next_cursor, enabled) == (None, True)

    everything, _, _ = lockout_admin.list_locked_accounts(include_unlocked=True)
    assert sorted(a["identifier"] for a in everything) == [
        "expired@example.com",
        "locked@example.com",
        "never@example.com",
    ]
    assert {a["identifier"]: a["is_locked"] for a in everything} == {
        "expired@example.com": False,
        "locked@example.com": True,
        "never@example.com": False,
    }


def test_listing_reports_nothing_locked_while_lockout_is_disabled(memory) -> None:
    _put(memory, "locked@example.com", locked_for=timedelta(minutes=10))
    publish_process_auth_setting("account_lockout_enabled", False)

    accounts, _, enabled = lockout_admin.list_locked_accounts()
    assert (accounts, enabled) == ([], False)


# ── reset_lockout_state ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_reset_of_an_unknown_account_writes_nothing(monkeypatch, memory, backend) -> None:
    store = memory if backend == "memory" else _redis(monkeypatch, _CasRedis())

    outcome = lockout_admin.reset_lockout_state("ghost@example.com")

    assert outcome == {
        "found": False,
        "written": False,
        "previous_lockout_count": 0,
        "was_locked": False,
        "unlocked": False,
    }
    assert store.get(lockout._lockout_key("ghost@example.com")) is None


@pytest.mark.parametrize("backend", ["memory", "redis"])
@pytest.mark.parametrize("clear_lock", [False, True])
def test_reset_clears_counters_and_only_unlocks_when_asked(
    monkeypatch, memory, backend, clear_lock
) -> None:
    store = memory if backend == "memory" else _redis(monkeypatch, _CasRedis())
    _put(store, "victim@example.com", locked_for=timedelta(minutes=10), count=3)

    outcome = lockout_admin.reset_lockout_state("  Victim@Example.com ", clear_lock=clear_lock)

    assert outcome == {
        "found": True,
        "written": True,
        "previous_lockout_count": 3,
        "was_locked": True,
        "unlocked": clear_lock,
    }
    stored = _get(store, "victim@example.com")
    assert (stored["lockout_count"], stored["failed_attempts"]) == (0, 0)
    assert stored["admin_unlocked_at"] is not None
    assert (stored["locked_until"] is None) is clear_lock, (
        "locked_until must survive a reset unless clear_lock was requested"
    )


def test_reset_recomputes_after_losing_a_cas_race(monkeypatch) -> None:
    client = _redis(monkeypatch, _CasRedis(conflicts=1))
    _put(client, "raced@example.com", locked_for=timedelta(minutes=10), count=2)

    outcome = lockout_admin.reset_lockout_state("raced@example.com")

    assert outcome["written"] is True
    assert client.cas_calls == 2, "one lost race, then a successful retry"
    assert _get(client, "raced@example.com")["failed_attempts"] == 0


def test_reset_reports_unwritten_when_every_cas_attempt_loses(monkeypatch) -> None:
    client = _redis(monkeypatch, _CasRedis(conflicts=lockout.CAS_MAX_RETRIES))
    _put(client, "contended@example.com", locked_for=timedelta(minutes=10), count=2)

    outcome = lockout_admin.reset_lockout_state("contended@example.com", clear_lock=True)

    assert outcome == {
        "found": True,
        "written": False,
        "previous_lockout_count": 0,
        "was_locked": False,
        "unlocked": False,
    }
    assert client.cas_calls == lockout.CAS_MAX_RETRIES
    stored = _get(client, "contended@example.com")
    assert stored["lockout_count"] == 2, "a lost reset must not partially apply"
    assert stored["locked_until"] is not None
