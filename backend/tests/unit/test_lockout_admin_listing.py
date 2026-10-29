"""The admin locked-accounts plane of ``app.auth.lockout`` (issue #570).

``iter_lockout_records`` / ``list_locked_accounts`` / ``reset_lockout_state`` /
``current_store_kind`` / ``InMemoryLockoutStore.delete`` shipped with no direct tests:
the admin panel is the only thing that calls them, and it renders what they return, so
a wrong page boundary, a leaked foreign key, a mislabelled lock state or a reset that
quietly keeps the counter is what an operator would act on.

Both backends are exercised. The in-memory path uses the real ``InMemoryLockoutStore``;
the Redis path uses a structural stand-in implementing the exact operations the module
calls (``scan``/``mget``/``get`` and the compare-and-set script contract), so the
cursor loop and the CAS retry budget run for real.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Self

import pytest

from app.auth import lockout
from app.auth.lockout import LOCKOUT_PREFIX
from app.auth.lockout import InMemoryLockoutStore
from app.auth.lockout import LockoutRecord
from app.core.auth_settings import publish_process_auth_setting


def _key(identifier: str) -> str:
    return f"{LOCKOUT_PREFIX}{identifier}"


def _dump(record: LockoutRecord) -> str:
    return json.dumps(record.to_dict())


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


@pytest.fixture(autouse=True)
def _lockout_on():
    publish_process_auth_setting("account_lockout_enabled", True)
    yield
    publish_process_auth_setting("account_lockout_enabled", True)


@pytest.fixture
def memory(monkeypatch) -> InMemoryLockoutStore:
    store = InMemoryLockoutStore()
    monkeypatch.setattr(lockout, "_get_store", lambda **_kw: store)
    return store


class _FakeRedis:
    """Only the calls the admin plane makes; deliberately NOT an InMemoryLockoutStore."""

    def __init__(self, page_size: int | None = None):
        self.data: dict[str, Any] = {}
        self.scan_calls: list[dict[str, Any]] = []
        self.mget_calls: list[list[str]] = []
        self.script_calls: list[dict[str, Any]] = []
        self.always_lose = False
        self.interleave: Any = None
        self._page_size = page_size

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value

    def scan(self, cursor: int = 0, match: str = "*", count: int = 10) -> tuple[int, list[Any]]:
        self.scan_calls.append({"cursor": cursor, "match": match, "count": count})
        prefix = match.rstrip("*")
        keys = sorted(k for k in self.data if k.startswith(prefix))
        size = self._page_size or count
        batch = keys[cursor : cursor + size]
        nxt = cursor + size
        return (nxt if nxt < len(keys) else 0), batch

    def mget(self, keys: list[str]) -> list[Any]:
        self.mget_calls.append(list(keys))
        return [self.data.get(k) for k in keys]

    def register_script(self, _src: str):
        def run(keys, args):
            self.script_calls.append({"keys": keys, "args": args})
            if self.interleave is not None:
                hook, self.interleave = self.interleave, None
                hook(self)
            key = keys[0]
            expected, new_value, _ttl, expect_missing = args
            current = self.data.get(key)
            if isinstance(current, bytes):
                current = current.decode()
            if self.always_lose:
                return [0, current or ""]
            if expect_missing == "1":
                if current is not None:
                    return [0, current]
            elif current != expected:
                return [0, current or ""]
            self.data[key] = new_value
            return [1, ""]

        return run


@pytest.fixture
def redis_store(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(lockout, "_get_store", lambda **_kw: fake)
    monkeypatch.setattr(lockout, "_cas_script", None)
    monkeypatch.setattr(lockout, "_cas_script_client", None)
    return fake


# ── InMemoryLockoutStore.delete / current_store_kind ──────────────────────────


class TestInMemoryDelete:
    def test_deleting_a_present_key_returns_one_and_removes_only_it(self):
        store = InMemoryLockoutStore()
        store.set("a", "1")
        store.set("b", "2")

        assert store.delete("a") == 1
        assert store.get("a") is None
        assert store.get("b") == "2"

    def test_deleting_a_missing_key_returns_zero_and_changes_nothing(self):
        store = InMemoryLockoutStore()
        store.set("a", "1")

        assert store.delete("nope") == 0
        assert store.get("a") == "1"


class TestCurrentStoreKind:
    def test_memory_fallback_is_reported_as_memory(self, memory):
        assert lockout.current_store_kind() == "memory"

    def test_a_redis_client_is_reported_as_redis(self, redis_store):
        assert lockout.current_store_kind() == "redis"


# ── iter_lockout_records ──────────────────────────────────────────────────────


def _seed(store: Any, *identifiers: str) -> None:
    for identifier in identifiers:
        store.set(_key(identifier), _dump(LockoutRecord(identifier=identifier, failed_attempts=1)))


class TestIterLockoutRecordsMemory:
    def test_pages_are_sorted_bounded_and_chained_by_cursor(self, memory):
        _seed(memory, "c@x", "a@x", "e@x", "b@x", "d@x")

        page1, cur1 = lockout.iter_lockout_records(None, 2)
        page2, cur2 = lockout.iter_lockout_records(cur1, 2)
        page3, cur3 = lockout.iter_lockout_records(cur2, 2)

        assert [r.identifier for r in page1] == ["a@x", "b@x"]
        assert cur1 == "2"
        assert [r.identifier for r in page2] == ["c@x", "d@x"]
        assert cur2 == "4"
        assert [r.identifier for r in page3] == ["e@x"]
        assert cur3 is None

    def test_a_page_that_exactly_exhausts_the_keyspace_has_no_next_cursor(self, memory):
        _seed(memory, "a@x", "b@x")

        records, cursor = lockout.iter_lockout_records(None, 2)

        assert len(records) == 2
        assert cursor is None

    def test_cursor_zero_string_starts_from_the_beginning(self, memory):
        _seed(memory, "a@x", "b@x")

        records, _ = lockout.iter_lockout_records("0", 5)

        assert [r.identifier for r in records] == ["a@x", "b@x"]

    def test_foreign_keys_are_never_listed(self, memory):
        _seed(memory, "a@x")
        memory.set("celery-task-meta-1", "{}")
        memory.set("oidc_state:abc", "{}")

        records, cursor = lockout.iter_lockout_records(None, 10)

        assert [r.identifier for r in records] == ["a@x"]
        assert cursor is None

    def test_an_empty_store_yields_nothing(self, memory):
        assert lockout.iter_lockout_records(None, 10) == ([], None)

    def test_a_blank_value_is_skipped_but_still_consumes_its_slot(self, memory):
        _seed(memory, "a@x", "c@x")
        memory.set(_key("b@x"), "")

        records, cursor = lockout.iter_lockout_records(None, 2)

        assert [r.identifier for r in records] == ["a@x"]
        assert cursor == "2"


class TestIterLockoutRecordsRedis:
    def test_scan_is_scoped_to_the_lockout_prefix_and_starts_at_zero(self, redis_store):
        _seed(redis_store, "a@x")
        redis_store.data["celery-task-meta-1"] = "{}"

        records, cursor = lockout.iter_lockout_records(None, 10)

        assert [r.identifier for r in records] == ["a@x"]
        assert cursor is None
        assert redis_store.scan_calls[0]["match"] == f"{LOCKOUT_PREFIX}*"
        assert redis_store.scan_calls[0]["cursor"] == 0
        assert redis_store.scan_calls[0]["count"] == 10

    def test_a_supplied_cursor_resumes_the_scan_and_the_next_cursor_is_returned(self, redis_store):
        _seed(redis_store, "a@x", "b@x", "c@x", "d@x", "e@x")

        page1, cur1 = lockout.iter_lockout_records(None, 2)
        page2, cur2 = lockout.iter_lockout_records(cur1, 2)
        page3, cur3 = lockout.iter_lockout_records(cur2, 2)

        assert [r.identifier for r in page1] == ["a@x", "b@x"]
        assert cur1 == "2"
        assert redis_store.scan_calls[1]["cursor"] == 2
        assert [r.identifier for r in page2] == ["c@x", "d@x"]
        assert [r.identifier for r in page3] == ["e@x"]
        assert cur3 is None

    def test_the_page_is_fetched_with_one_mget_of_exactly_the_page_keys(self, redis_store):
        _seed(redis_store, "a@x", "b@x", "c@x")

        lockout.iter_lockout_records(None, 2)

        assert redis_store.mget_calls == [[_key("a@x"), _key("b@x")]]

    def test_bytes_keys_and_values_are_decoded(self, redis_store):
        raw = _dump(LockoutRecord(identifier="a@x", failed_attempts=2)).encode()
        redis_store.data[_key("a@x")] = raw

        records, _ = lockout.iter_lockout_records(None, 5)

        assert [(r.identifier, r.failed_attempts) for r in records] == [("a@x", 2)]

    def test_a_key_that_vanished_before_mget_is_skipped(self, redis_store, monkeypatch):
        _seed(redis_store, "a@x", "b@x")
        real_mget = redis_store.mget

        def racing_mget(keys: list[str]) -> list[Any]:
            redis_store.data.pop(_key("a@x"), None)
            result: list[Any] = real_mget(keys)
            return result

        monkeypatch.setattr(redis_store, "mget", racing_mget)

        records, _ = lockout.iter_lockout_records(None, 5)

        assert [r.identifier for r in records] == ["b@x"]

    def test_the_scan_loop_is_bounded_when_batches_come_back_empty(self, redis_store, monkeypatch):
        calls: list[int] = []

        def endless_scan(cursor: int = 0, match: str = "*", count: int = 10):
            calls.append(cursor)
            return cursor + 1, []

        monkeypatch.setattr(redis_store, "scan", endless_scan)

        records, cursor = lockout.iter_lockout_records(None, 5)

        assert records == []
        assert len(calls) == lockout._SCAN_BATCH_MAX_ROUNDS
        assert cursor == str(lockout._SCAN_BATCH_MAX_ROUNDS)

    def test_the_loop_gathers_across_several_short_batches(self, monkeypatch):
        fake = _FakeRedis(page_size=1)
        monkeypatch.setattr(lockout, "_get_store", lambda **_kw: fake)
        monkeypatch.setattr(lockout, "_cas_script", None)
        monkeypatch.setattr(lockout, "_cas_script_client", None)
        _seed(fake, "a@x", "b@x", "c@x")

        records, cursor = lockout.iter_lockout_records(None, 2)

        assert [r.identifier for r in records] == ["a@x", "b@x"]
        assert len(fake.scan_calls) == 2
        assert cursor == "2"


# ── list_locked_accounts ──────────────────────────────────────────────────────


def _locked(identifier: str) -> LockoutRecord:
    return LockoutRecord(
        identifier=identifier,
        failed_attempts=5,
        lockout_count=2,
        locked_until=_iso(timedelta(hours=1)),
        first_failed_attempt=_iso(timedelta(minutes=-30)),
        last_failed_attempt=_iso(timedelta(minutes=-1)),
    )


def _expired(identifier: str) -> LockoutRecord:
    return LockoutRecord(
        identifier=identifier, failed_attempts=1, locked_until=_iso(timedelta(hours=-1))
    )


def _never(identifier: str) -> LockoutRecord:
    return LockoutRecord(identifier=identifier, failed_attempts=2)


class TestListLockedAccounts:
    @pytest.fixture
    def populated(self, memory: InMemoryLockoutStore) -> InMemoryLockoutStore:
        for record in (_locked("locked@x"), _expired("expired@x"), _never("never@x")):
            memory.set(_key(record.identifier), _dump(record))
        return memory

    def test_by_default_only_currently_locked_accounts_are_listed(self, populated):
        accounts, cursor, enabled = lockout.list_locked_accounts()

        assert [a["identifier"] for a in accounts] == ["locked@x"]
        assert cursor is None
        assert enabled is True

    def test_a_listed_account_carries_every_field_the_panel_renders(self, populated):
        record = _locked("locked@x")
        populated.set(_key("locked@x"), _dump(record))

        (info,), _, _ = lockout.list_locked_accounts()

        assert info == {
            "identifier": "locked@x",
            "is_locked": True,
            "failed_attempts": 5,
            "lockout_count": 2,
            "locked_until": record.locked_until,
            "first_failed_attempt": record.first_failed_attempt,
            "last_failed_attempt": record.last_failed_attempt,
            "admin_unlocked_at": None,
            "lockout_enabled": True,
        }

    def test_include_unlocked_lists_every_record_with_the_right_lock_flag(self, populated):
        accounts, _, _ = lockout.list_locked_accounts(include_unlocked=True)

        assert {a["identifier"]: a["is_locked"] for a in accounts} == {
            "expired@x": False,
            "locked@x": True,
            "never@x": False,
        }

    def test_with_enforcement_off_nobody_is_reported_locked(self, populated):
        publish_process_auth_setting("account_lockout_enabled", False)

        default, _, enabled = lockout.list_locked_accounts()
        everything, _, _ = lockout.list_locked_accounts(include_unlocked=True)

        assert enabled is False
        assert default == []
        assert all(a["is_locked"] is False for a in everything)
        assert all(a["lockout_enabled"] is False for a in everything)

    def test_cursor_and_limit_are_passed_through_to_the_scan(self, memory):
        for name in ("a@x", "b@x", "c@x"):
            memory.set(_key(name), _dump(_locked(name)))

        first, cursor, _ = lockout.list_locked_accounts(limit=2)
        second, cursor2, _ = lockout.list_locked_accounts(cursor=cursor, limit=2)

        assert [a["identifier"] for a in first] == ["a@x", "b@x"]
        assert cursor == "2"
        assert [a["identifier"] for a in second] == ["c@x"]
        assert cursor2 is None

    def test_the_default_page_size_is_one_hundred(self, memory):
        for i in range(101):
            memory.set(_key(f"u{i:03d}@x"), _dump(_locked(f"u{i:03d}@x")))

        accounts, cursor, _ = lockout.list_locked_accounts()

        assert len(accounts) == 100
        assert cursor == "100"


# ── reset_lockout_state ───────────────────────────────────────────────────────

UNKNOWN = {
    "found": False,
    "written": False,
    "previous_lockout_count": 0,
    "was_locked": False,
    "unlocked": False,
}


def _stored(store: Any, identifier: str) -> LockoutRecord:
    raw = store.get(_key(identifier))
    if isinstance(raw, bytes):
        raw = raw.decode()
    return LockoutRecord.from_dict(json.loads(raw))


@pytest.fixture(params=["memory", "redis"])
def any_store(request, monkeypatch) -> Any:
    """Run each reset scenario against both backends."""
    if request.param == "memory":
        store: Any = InMemoryLockoutStore()
    else:
        store = _FakeRedis()
        monkeypatch.setattr(lockout, "_cas_script", None)
        monkeypatch.setattr(lockout, "_cas_script_client", None)
    monkeypatch.setattr(lockout, "_get_store", lambda **_kw: store)
    return store


class TestResetLockoutState:
    def test_an_unknown_identifier_reports_not_found_and_writes_nothing(self, any_store):
        assert lockout.reset_lockout_state("ghost@x") == UNKNOWN
        assert any_store.get(_key("ghost@x")) is None

    def test_reset_zeroes_counters_but_keeps_the_lock_by_default(self, any_store):
        record = _locked("u@x")
        any_store.set(_key("u@x"), _dump(record))
        before = datetime.now(UTC)

        outcome = lockout.reset_lockout_state("u@x")

        assert outcome == {
            "found": True,
            "written": True,
            "previous_lockout_count": 2,
            "was_locked": True,
            "unlocked": False,
        }
        after = _stored(any_store, "u@x")
        assert after.lockout_count == 0
        assert after.failed_attempts == 0
        assert after.locked_until == record.locked_until
        assert after.first_failed_attempt == record.first_failed_attempt
        assert after.last_failed_attempt == record.last_failed_attempt
        assert after.admin_unlocked_at is not None
        stamped = datetime.fromisoformat(after.admin_unlocked_at)
        assert before <= stamped <= datetime.now(UTC)

    def test_clear_lock_also_lifts_a_live_lock(self, any_store):
        any_store.set(_key("u@x"), _dump(_locked("u@x")))

        outcome = lockout.reset_lockout_state("u@x", clear_lock=True)

        assert outcome["was_locked"] is True
        assert outcome["unlocked"] is True
        assert _stored(any_store, "u@x").locked_until is None

    def test_clear_lock_on_an_already_expired_lock_does_not_claim_an_unlock(self, any_store):
        any_store.set(_key("u@x"), _dump(_expired("u@x")))

        outcome = lockout.reset_lockout_state("u@x", clear_lock=True)

        assert outcome["found"] is True
        assert outcome["was_locked"] is False
        assert outcome["unlocked"] is False
        assert _stored(any_store, "u@x").locked_until is None

    def test_without_clear_lock_an_expired_record_keeps_its_timestamp(self, any_store):
        record = _expired("u@x")
        any_store.set(_key("u@x"), _dump(record))

        outcome = lockout.reset_lockout_state("u@x")

        assert outcome["was_locked"] is False
        assert outcome["unlocked"] is False
        assert _stored(any_store, "u@x").locked_until == record.locked_until

    def test_the_identifier_is_normalised_before_lookup(self, any_store):
        any_store.set(_key("user@example.com"), _dump(_locked("user@example.com")))

        outcome = lockout.reset_lockout_state("  USER@Example.COM ")

        assert outcome["found"] is True
        assert _stored(any_store, "user@example.com").lockout_count == 0

    def test_only_the_named_account_is_touched(self, any_store):
        any_store.set(_key("a@x"), _dump(_locked("a@x")))
        any_store.set(_key("b@x"), _dump(_locked("b@x")))

        lockout.reset_lockout_state("a@x")

        assert _stored(any_store, "b@x").lockout_count == 2
        assert _stored(any_store, "b@x").admin_unlocked_at is None


class TestResetLockoutStateRedisCas:
    def test_it_writes_through_the_compare_and_set_script(self, redis_store):
        record = _locked("u@x")
        redis_store.data[_key("u@x")] = _dump(record)

        lockout.reset_lockout_state("u@x")

        assert len(redis_store.script_calls) == 1
        call = redis_store.script_calls[0]
        assert call["keys"] == [_key("u@x")]
        assert call["args"][0] == _dump(record)
        assert call["args"][3] == "0"

    def test_bytes_from_an_undecoded_client_are_handled(self, redis_store):
        redis_store.data[_key("u@x")] = _dump(_locked("u@x")).encode()

        outcome = lockout.reset_lockout_state("u@x")

        assert outcome["found"] is True
        assert outcome["previous_lockout_count"] == 2

    def test_a_lost_race_recomputes_from_the_winning_record(self, redis_store):
        redis_store.data[_key("u@x")] = _dump(_locked("u@x"))

        def concurrent_writer(fake: _FakeRedis) -> None:
            winner = _locked("u@x")
            winner.lockout_count = 7
            fake.data[_key("u@x")] = _dump(winner)

        redis_store.interleave = concurrent_writer

        outcome = lockout.reset_lockout_state("u@x")

        assert outcome["written"] is True
        assert outcome["previous_lockout_count"] == 7
        assert len(redis_store.script_calls) == 2
        assert _stored(redis_store, "u@x").lockout_count == 0

    def test_exhausting_the_retry_budget_reports_unwritten_rather_than_a_partial_write(
        self, redis_store, caplog
    ):
        record = _locked("u@x")
        redis_store.data[_key("u@x")] = _dump(record)
        redis_store.always_lose = True

        with caplog.at_level(logging.WARNING, logger=lockout.logger.name):
            outcome = lockout.reset_lockout_state("u@x")

        # The warning is the only operator-visible signal that an unlock silently did not land,
        # so its content (masked identity, the budget, the cause) is a contract, not noise.
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [
            f"Lockout reset for {lockout._mask_identifier('u@x')} lost "
            f"{lockout.CAS_MAX_RETRIES} compare-and-set races in a row"
        ]
        assert "u@x" not in warnings[0]

        assert outcome == {
            "found": True,
            "written": False,
            "previous_lockout_count": 0,
            "was_locked": False,
            "unlocked": False,
        }
        assert len(redis_store.script_calls) == lockout.CAS_MAX_RETRIES
        assert redis_store.data[_key("u@x")] == _dump(record)

    def test_an_unknown_identifier_never_reaches_the_script(self, redis_store):
        assert lockout.reset_lockout_state("ghost@x") == UNKNOWN
        assert redis_store.script_calls == []


# ── the lock-expiry instant, pinned with a frozen clock ───────────────────────

_INSTANT = datetime(2031, 5, 17, 12, 0, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now`` is a fixed instant, so ``now == locked_until`` is exact."""

    @classmethod
    def now(cls, tz: Any = None) -> Self:
        return cls.combine(_INSTANT.date(), _INSTANT.timetz())


@pytest.fixture
def frozen_clock(monkeypatch) -> None:
    monkeypatch.setattr(lockout, "datetime", _FrozenDatetime)


def _expiring_exactly_now(identifier: str) -> LockoutRecord:
    return LockoutRecord(
        identifier=identifier,
        failed_attempts=5,
        lockout_count=2,
        locked_until=_INSTANT.isoformat(),
    )


class TestALockExpiringExactlyNowIsNotLocked:
    """``now < locked_until``: at the very instant of expiry the lock is over, so the
    panel must not list the account as locked and a reset must not claim it unlocked one."""

    def test_the_listing_does_not_report_it_locked(self, memory, frozen_clock):
        memory.set(_key("u@x"), _dump(_expiring_exactly_now("u@x")))

        default, _, _ = lockout.list_locked_accounts()
        everything, _, _ = lockout.list_locked_accounts(include_unlocked=True)

        assert default == []
        assert [a["is_locked"] for a in everything] == [False]

    def test_a_reset_does_not_report_it_was_locked(self, any_store, frozen_clock):
        any_store.set(_key("u@x"), _dump(_expiring_exactly_now("u@x")))

        outcome = lockout.reset_lockout_state("u@x", clear_lock=True)

        assert outcome["found"] is True
        assert outcome["was_locked"] is False
        assert outcome["unlocked"] is False


class TestIterLockoutRecordsEmptyScanKey:
    def test_an_empty_key_from_the_scan_is_passed_to_mget_as_empty(self, redis_store, monkeypatch):
        _seed(redis_store, "a@x")
        real_scan = redis_store.scan

        def scan_with_blank(cursor: int = 0, match: str = "*", count: int = 10):
            nxt, batch = real_scan(cursor=cursor, match=match, count=count)
            return nxt, ["", *batch]

        monkeypatch.setattr(redis_store, "scan", scan_with_blank)

        records, _ = lockout.iter_lockout_records(None, 5)

        assert redis_store.mget_calls == [["", _key("a@x")]]
        assert [r.identifier for r in records] == ["a@x"]
