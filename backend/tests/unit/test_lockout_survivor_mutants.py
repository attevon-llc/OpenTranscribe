"""Account-lockout behaviour that no test asserted (issue #446, mutation survivors).

Written from the 149 surviving mutants of ``app/auth/lockout.py`` (measured
2026-08-14, 85%→86% coverage of the module by ``MODULE_TESTS[lockout]``). Full triage,
corrected by MEASURING (a first pass classified one mutant "real" that turned out
equivalent -- see below):

* **77 real** — a predicate, a constant, a stored field, or a dict key returned to a
  caller (``get_lockout_info`` is the admin API's wire shape; its 31 survivors were
  every field name and the ``is_locked`` boundary). This file targets all 77.
* **70 noise** — a log/error string, or a condition that guards *only* a log call
  (the ``failed_attempts > 0 or locked_until_dt`` checks in
  ``_apply_successful_login`` and its ``_check_and_record_attempt_memory`` duplicate
  gate nothing but ``logger.info``; the resets below them run unconditionally
  either way). Not tested here — asserting log text breaks on every reword.
* **2 equivalent**:
  - ``_cas_write``'s ``expected or ""`` → ``expected or "XXXX"``. ``_decode_stored``
    (this file's ``TestDecodeStored``) only ever returns ``None`` or a non-empty
    string, so ``expected`` is never a falsy-but-not-``None`` value; the mutated
    fallback literal is dead on arrival and no test can reach it.
  - ``_check_and_record_attempt_memory``'s expiry-reset
    ``record.first_failed_attempt = now.isoformat()`` → ``None``
    (``x__check_and_record_attempt_memory__mutmut_18``). First classified real and
    given a boundary test that did not kill it (measured: 149→77, not the predicted
    149→71) — the reset branch's write is unconditionally OVERWRITTEN by whichever
    of the two branches runs immediately afterward in the SAME call: the success
    branch sets ``first_failed_attempt = None`` unconditionally regardless, and the
    failure branch's own ``if record.first_failed_attempt is None:`` guard re-stamps
    it with the SAME ``now`` the reset branch used. Both branches are exhaustive (an
    `if/else` on `success`), so every call converges on the identical final value
    whether or not the mutation fired. Confirmed with ``--verify`` (still SURVIVED
    after the two field-update tests below were added) before writing this proof
    rather than after — the trap this file's own docstring warns about elsewhere.

Two near-duplicate code paths run through this module — Redis-backed
(``_check_and_record_attempt_redis``) and in-memory
(``_check_and_record_attempt_memory``), the fallback used while Redis is down. A
mutant's diff can look like it belongs to the other path; every test below names
its function so the path is unambiguous. ``tests/unit/test_lockout_atomicity.py``
already covers ``failed_attempts`` counting and the core CAS race on both paths —
this file covers the fields and boundaries that atomicity file's tests happened
not to touch: ``last_failed_attempt``/``first_failed_attempt``/``admin_unlocked_at``
resets, the exact-expiry-instant boundary (``<`` vs ``<=``, ``>=`` vs ``>``), the
Redis-degradation fallback's per-identifier isolation, and ``get_lockout_info``'s
full wire shape.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.auth import lockout
from app.auth.lockout import InMemoryLockoutStore
from app.core.config import settings


@pytest.fixture(autouse=True)
def lockout_settings(monkeypatch):
    """Pin lockout tunables so dev/CI env overrides cannot change the expectations.

    Mirrors ``test_lockout_atomicity.py``'s fixture of the same name/shape.
    """
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_ENABLED", True)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_THRESHOLD", 3)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_DURATION_MINUTES", 15)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_MAX_DURATION_MINUTES", 1440)
    monkeypatch.setattr(settings, "ACCOUNT_LOCKOUT_PROGRESSIVE", True)


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """Clear the module singletons so each test starts on a known store."""
    monkeypatch.setattr(lockout, "_redis_client", None)
    monkeypatch.setattr(lockout, "_in_memory_store", None)
    monkeypatch.setattr(lockout, "_store_initialized", False)
    monkeypatch.setattr(lockout, "_cas_script", None)
    monkeypatch.setattr(lockout, "_cas_script_client", None)


@pytest.fixture
def memory_store(monkeypatch) -> InMemoryLockoutStore:
    """Route every ``_get_store()`` call to a fresh, real in-memory store.

    Bypasses the Redis-probing machinery entirely -- ``check_and_record_attempt``'s
    ``hasattr(store, "pipeline")`` dispatch correctly sends everything through the
    MEMORY path, since ``InMemoryLockoutStore`` has no ``pipeline`` attribute.
    """
    store = lockout.InMemoryLockoutStore()
    monkeypatch.setattr(lockout, "_get_store", lambda: store)
    return store


class _BrokenCas:
    """A Redis stand-in whose CAS script registration always fails.

    Drives ``check_and_record_attempt`` down the degradation path in
    ``_check_and_record_attempt_redis``'s ``except Exception`` handler, which falls
    back to ``_check_and_record_attempt_memory(_get_memory_fallback_store(), ...)``
    -- the exact call site three of this file's mutants (63/64/65) sit in.
    """

    def get(self, key: str) -> str | None:
        return None

    def register_script(self, src: str):
        raise ConnectionError("redis down")

    def pipeline(self, transaction: bool = True):
        raise AssertionError("must not be reached -- registration fails first")


@pytest.fixture
def broken_redis(monkeypatch) -> _BrokenCas:
    fake = _BrokenCas()
    monkeypatch.setattr(lockout, "_get_store", lambda: fake)
    return fake


# ── _decode_stored: normalising "empty" Redis values ──────────────────────────────


class TestDecodeStored:
    """Consequence prevented: an un-normalised empty value reaching ``_cas_write``,
    which uses ``expected is None`` to choose the CAS script's "expect missing" vs
    "expect exact match" branch (``ARGV[4]``). A wrongly-non-None ``""`` picks the
    wrong branch and a first write can be rejected as a conflict, or a stale
    zero-length value can be treated as a real record.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("", None),
            (b"", None),
            ("real-value", "real-value"),
            (b"real-bytes", "real-bytes"),
        ],
        ids=["none", "empty-str", "empty-bytes", "str-passthrough", "bytes-decoded"],
    )
    def test_normalizes_empty_values_to_none(self, raw, expected) -> None:
        assert lockout._decode_stored(raw) == expected


# ── _get_cas_script: the SHA cache, keyed on the client ───────────────────────────


class _ScriptStore:
    """A minimal store recording how many times its script was (re)registered."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.register_calls = 0

    def register_script(self, src: str) -> str:
        self.register_calls += 1
        return f"script-for-{self.name}-call-{self.register_calls}"


class TestGetCasScript:
    """Consequence prevented: reusing a script registered against a DIFFERENT Redis
    client (a silent write to the wrong connection), or re-registering on every
    single call (defeating the whole point of caching the SHA, per the module's own
    docstring: "caching keeps the SHA stable so repeated calls hit EVALSHA")."""

    def test_repeated_calls_with_the_same_store_reuse_the_registered_script(self) -> None:
        store = _ScriptStore("a")

        first = lockout._get_cas_script(store)
        second = lockout._get_cas_script(store)

        assert store.register_calls == 1, "caching must avoid a round trip on every call"
        assert first is second

    def test_a_different_store_forces_a_fresh_registration(self) -> None:
        store_a = _ScriptStore("a")
        store_b = _ScriptStore("b")

        script_a = lockout._get_cas_script(store_a)
        script_b = lockout._get_cas_script(store_b)

        assert store_b.register_calls == 1, "the new store's own script must be registered"
        assert script_a != script_b, (
            "reusing store A's script for store B would write through the wrong client"
        )


# ── InMemoryLockoutStore.keys: prefix matching, not a character class ─────────────


class TestInMemoryStoreKeys:
    def test_keys_strips_only_the_trailing_wildcard_not_arbitrary_characters(self) -> None:
        """``rstrip("*")`` must treat ``"*"`` as a literal suffix marker.

        ``str.rstrip`` strips every trailing character IN its argument's character
        set. A mutant widening the argument to ``"XX*XX"`` still strips the ``*``
        (still in the set) but ALSO strips any trailing ``X`` from the prefix
        itself, silently broadening the match to keys that were never asked for.
        """
        store = lockout.InMemoryLockoutStore()
        store.set("lockout:userX", "keep")
        store.set("lockout:user999", "must-not-match")

        matched = store.keys("lockout:userX*")

        assert matched == ["lockout:userX"], (
            "a pattern ending in 'X*' must strip only the '*', not the prefix's own 'X'"
        )


# ── _record_ttl_seconds / _save_record: the TTL that keeps a lockout durable ──────


class TestRecordTtlSeconds:
    def test_ttl_is_exactly_the_max_duration_plus_one_day(self) -> None:
        """The +1440/*60 constants, pinned exactly (not just "> the max duration",
        which the existing ``test_ttl_outlives_the_maximum_lockout`` already checks
        and which a +1 or *61 mutant still satisfies)."""
        assert lockout._record_ttl_seconds() == (1440 + 1440) * 60


class _SpyStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.calls.append({"key": key, "value": value, "ex": ex})

    def get(self, key: str) -> str | None:
        return None


class TestSaveRecord:
    def test_save_record_always_sets_a_positive_ttl(self, monkeypatch) -> None:
        """A dropped or ``None`` TTL means the Redis key never expires -- every
        record accumulates forever instead of being reclaimed, defeating the
        rationale in ``_record_ttl_seconds``'s own docstring."""
        spy = _SpyStore()
        monkeypatch.setattr(lockout, "_get_store", lambda: spy)
        record = lockout.LockoutRecord(identifier="ttl-check@example.com", failed_attempts=1)

        lockout._save_record(record)

        assert len(spy.calls) == 1
        ex = spy.calls[0]["ex"]
        assert isinstance(ex, int) and ex > 0, "a dropped/None TTL means the record never expires"
        assert ex == (1440 + 1440) * 60


# ── _handle_expired_lockout: the shared reset both paths call ─────────────────────


class TestHandleExpiredLockout:
    def test_resets_attempts_and_restarts_the_first_attempt_clock(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        record = lockout.LockoutRecord(
            identifier="expired@example.com",
            failed_attempts=5,
            lockout_count=2,
            locked_until=(now - timedelta(minutes=1)).isoformat(),
            first_failed_attempt=(now - timedelta(hours=1)).isoformat(),
        )

        lockout._handle_expired_lockout(record, now)

        assert record.failed_attempts == 0
        assert record.get_locked_until_datetime() is None
        assert record.first_failed_attempt == now.isoformat()
        assert record.lockout_count == 2, "progressive count must survive expiry"


# ── _apply_failed_login: fields beyond failed_attempts (redis path) ───────────────


class TestApplyFailedLoginFieldUpdates:
    """``_apply_failed_login`` mutates the record BOTH storage paths eventually
    persist. ``test_lockout_atomicity.py`` covers ``failed_attempts`` and the
    threshold/lock outcome; every other field it touches had no assertion."""

    def test_last_failed_attempt_is_stamped_with_the_callers_clock(self) -> None:
        record = lockout.LockoutRecord(identifier="x@example.com")
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        lockout._apply_failed_login(record, "x@example.com", now)

        assert record.last_failed_attempt == now.isoformat()

    def test_a_prior_admin_unlock_marker_is_cleared_by_the_next_failure(self) -> None:
        record = lockout.LockoutRecord(
            identifier="x@example.com",
            admin_unlocked_at=datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)

        lockout._apply_failed_login(record, "x@example.com", now)

        assert record.admin_unlocked_at is None, "must be None, not an empty-but-truthy sentinel"

    def test_first_failed_attempt_is_set_once_and_never_overwritten(self) -> None:
        record = lockout.LockoutRecord(identifier="x@example.com")
        first = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        second = first + timedelta(minutes=1)

        lockout._apply_failed_login(record, "x@example.com", first)
        assert record.first_failed_attempt == first.isoformat(), (
            "the FIRST failure must stamp a real timestamp, not leave it unset"
        )

        lockout._apply_failed_login(record, "x@example.com", second)
        assert record.first_failed_attempt == first.isoformat(), (
            "a LATER failure must not move the window's start"
        )


# ── _apply_successful_login: the reset must use None, not a truthy sentinel ───────


class TestApplySuccessfulLoginFieldUpdates:
    def test_every_tracking_field_resets_to_none_not_an_empty_string(self) -> None:
        record = lockout.LockoutRecord(
            identifier="y@example.com",
            failed_attempts=3,
            first_failed_attempt=datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
            last_failed_attempt=datetime(2026, 1, 1, 12, tzinfo=UTC).isoformat(),
            admin_unlocked_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        )

        result = lockout._apply_successful_login(record, locked_until_dt=None)

        assert result == (False, None)
        assert record.failed_attempts == 0
        assert record.first_failed_attempt is None
        assert record.last_failed_attempt is None
        assert record.admin_unlocked_at is None


# ── _record_attempt_audit_only: the super-admin exempt path ───────────────────────


class TestRecordAttemptAuditOnly:
    def test_the_clock_is_utc_aware_not_naive_local_time(self, memory_store) -> None:
        """``datetime.now(UTC)`` -> ``datetime.now(None)`` returns naive LOCAL time.
        Every reader in this module (``cleanup_expired_lockouts`` in particular)
        compares timestamps against a tz-AWARE clock, which raises ``TypeError``
        the instant it reads a naive one back."""
        identifier = "audit-clock@example.com"

        lockout._record_attempt_audit_only(identifier)

        info = lockout.get_lockout_info(identifier)
        last_failed_attempt = info["last_failed_attempt"]
        assert last_failed_attempt is not None
        parsed = datetime.fromisoformat(last_failed_attempt)
        assert parsed.utcoffset() == timedelta(0), (
            "a naive timestamp (utcoffset() is None) breaks every aware comparison downstream; "
            f"a NON-UTC local offset (e.g. host TZ) is wrong by that many hours: got {parsed}"
        )

    def test_last_failed_attempt_is_stamped_close_to_the_real_clock(self, memory_store) -> None:
        identifier = "audit-last@example.com"
        before = datetime.now(UTC)

        lockout._record_attempt_audit_only(identifier)

        after = datetime.now(UTC)
        last_failed_attempt = lockout.get_lockout_info(identifier)["last_failed_attempt"]
        assert last_failed_attempt is not None
        parsed = datetime.fromisoformat(last_failed_attempt)
        assert before <= parsed <= after, (
            f"stamped {parsed} is not within the call's own time window [{before}, {after}]"
        )

    def test_first_failed_attempt_is_set_once_and_never_moved(self, memory_store) -> None:
        identifier = "audit-first@example.com"

        lockout._record_attempt_audit_only(identifier)
        first = lockout.get_lockout_info(identifier)["first_failed_attempt"]
        assert first is not None

        lockout._record_attempt_audit_only(identifier)
        second = lockout.get_lockout_info(identifier)["first_failed_attempt"]
        assert second == first, "a later exempt attempt must not move the window's start"


# ── _get_redis_client: must actually use the configured URL ───────────────────────


class TestGetRedisClient:
    def test_connects_to_the_configured_url(self, monkeypatch) -> None:
        """Dropping ``settings.REDIS_URL`` to ``None`` would make every connection
        attempt fail (or connect to whatever redis-py defaults to), silently
        disabling distributed lockout storage for the life of the process."""
        import redis

        calls: list[object] = []

        class _FakePinger:
            def ping(self) -> bool:
                return True

        def fake_from_url(cls, url, **kwargs):
            calls.append(url)
            return _FakePinger()

        monkeypatch.setattr(redis.Redis, "from_url", classmethod(fake_from_url))

        client = lockout._get_redis_client()

        assert calls == [settings.REDIS_URL]
        assert client is not None


# ── _get_store: the reprobe timestamp must stay usable ─────────────────────────────


@pytest.fixture
def store_on_fallback_after_a_failed_reprobe(monkeypatch) -> None:
    """Put ``_get_store()`` through exactly one failed reprobe attempt.

    Common setup for both tests below, so neither has to repeat the four
    ``monkeypatch.setattr`` calls that put the module in "just probed, still on
    the fallback" state.
    """
    monkeypatch.setattr(lockout, "_store_initialized", True)
    monkeypatch.setattr(lockout, "_redis_client", None)
    monkeypatch.setattr(lockout, "_in_memory_store", lockout.InMemoryLockoutStore())
    monkeypatch.setattr(lockout, "_last_redis_probe", 0.0)  # force the window open

    with patch.object(lockout, "_get_redis_client", return_value=None):
        lockout._get_store()


class TestGetStoreReprobe:
    """``_last_redis_probe = None`` would make the NEXT call's
    ``now - _last_redis_probe`` raise ``TypeError`` -- crashing every login while
    Redis stays down, instead of just skipping the reprobe."""

    def test_a_failed_reprobe_records_a_real_timestamp(
        self, store_on_fallback_after_a_failed_reprobe
    ) -> None:
        assert isinstance(lockout._last_redis_probe, float)
        assert lockout._last_redis_probe > 0, "0.0 is the pre-probe sentinel, not a real clock read"

    def test_a_second_call_inside_the_window_does_not_crash_or_reprobe(
        self, store_on_fallback_after_a_failed_reprobe
    ) -> None:
        probe_calls: list[int] = []

        def _count_and_return_none():
            probe_calls.append(1)
            return None

        with patch.object(lockout, "_get_redis_client", side_effect=_count_and_return_none):
            store = lockout._get_store()  # raises TypeError under the mutant if this crashes

        assert probe_calls == [], "still inside the reprobe window -- must not probe again"
        assert isinstance(store, lockout.InMemoryLockoutStore)


def _stored(store: InMemoryLockoutStore, key: str) -> dict:
    """Read and parse a stored record, failing loudly if the key is missing."""
    raw = store.get(key)
    assert raw is not None, f"no record stored under {key!r}"
    record: dict = json.loads(raw)
    return record


# ── The memory path: boundaries and fields _check_and_record_attempt_memory owns ──


class TestMemoryPathBoundariesAndFields:
    """Everything here calls ``_check_and_record_attempt_memory`` directly with an
    explicit ``now`` (it is a normal parameter -- no clock freezing needed), the
    same style ``test_progressive_durations_and_sticky_lockout_count`` already uses
    for the Redis path in ``test_lockout_atomicity.py``."""

    def test_unlocks_at_the_exact_expiry_instant_and_restarts_the_window(self) -> None:
        store = lockout.InMemoryLockoutStore()
        identifier = "boundary-mem@example.com"
        frozen = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        record = lockout.LockoutRecord(
            identifier=identifier,
            failed_attempts=5,
            lockout_count=1,
            locked_until=frozen.isoformat(),
            first_failed_attempt=(frozen - timedelta(hours=1)).isoformat(),
            last_failed_attempt=(frozen - timedelta(minutes=1)).isoformat(),
        )
        store.set(lockout._lockout_key(identifier), json.dumps(record.to_dict()))

        is_locked, _ = lockout._check_and_record_attempt_memory(
            store, lockout._lockout_key(identifier), identifier, False, frozen
        )

        assert is_locked is False, "the lockout must end AT its expiry instant, not after it"
        stored = _stored(store, lockout._lockout_key(identifier))
        assert stored["failed_attempts"] == 1, (
            "the expired lockout must reset the counter before counting THIS attempt"
        )
        # NOTE: this does not kill the reset branch's OWN `first_failed_attempt =
        # now.isoformat()` mutant (mutmut_18: -> None) in isolation -- see the
        # module docstring's equivalence proof. It is asserted anyway because it
        # is real, observable behaviour a caller (get_lockout_info) reads.
        assert stored["first_failed_attempt"] == frozen.isoformat(), (
            "expiry must restart the tracking window, not leave it unset"
        )

    def test_progressive_lockout_count_increments_not_resets(self) -> None:
        store = lockout.InMemoryLockoutStore()
        identifier = "progressive-mem@example.com"
        key = lockout._lockout_key(identifier)
        now = datetime(2026, 1, 1, tzinfo=UTC)

        unlock_time: datetime | None = None
        for _ in range(settings.ACCOUNT_LOCKOUT_THRESHOLD):
            is_locked, unlock_time = lockout._check_and_record_attempt_memory(
                store, key, identifier, False, now
            )
        assert is_locked is True
        assert unlock_time is not None
        assert _stored(store, key)["lockout_count"] == 1

        now2 = unlock_time + timedelta(minutes=1)
        for _ in range(settings.ACCOUNT_LOCKOUT_THRESHOLD):
            is_locked, unlock_time = lockout._check_and_record_attempt_memory(
                store, key, identifier, False, now2
            )
        assert _stored(store, key)["lockout_count"] == 2, (
            "lockout_count must ESCALATE across cycles, not reset to 1 on every lockout"
        )

    def test_a_brand_new_record_gets_its_own_identifier_field(self) -> None:
        store = lockout.InMemoryLockoutStore()
        identifier = "new-mem@example.com"

        lockout._check_and_record_attempt_memory(
            store, lockout._lockout_key(identifier), identifier, False, datetime.now(UTC)
        )

        assert _stored(store, lockout._lockout_key(identifier))["identifier"] == identifier

    def test_a_successful_login_resets_the_admin_unlock_marker_to_none(self) -> None:
        """The memory path's success branch is an INLINE duplicate of
        ``_apply_successful_login`` -- ``TestApplySuccessfulLoginFieldUpdates``
        exercises the shared helper the REDIS path calls, but does not reach this
        copy at all."""
        store = lockout.InMemoryLockoutStore()
        identifier = "mem-success-fields@example.com"
        key = lockout._lockout_key(identifier)
        record = lockout.LockoutRecord(
            identifier=identifier,
            failed_attempts=2,
            admin_unlocked_at=datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
        )
        store.set(key, json.dumps(record.to_dict()))

        lockout._check_and_record_attempt_memory(store, key, identifier, True, datetime.now(UTC))

        assert _stored(store, key)["admin_unlocked_at"] is None, (
            "must be None, not an empty-but-truthy sentinel"
        )

    def test_a_failed_login_stamps_last_failed_attempt_and_clears_the_unlock_marker(
        self,
    ) -> None:
        """The memory path's failure branch is an INLINE duplicate of
        ``_apply_failed_login`` -- same reasoning as the success-branch test above."""
        store = lockout.InMemoryLockoutStore()
        identifier = "mem-failed-fields@example.com"
        key = lockout._lockout_key(identifier)
        record = lockout.LockoutRecord(
            identifier=identifier,
            admin_unlocked_at=datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
        )
        store.set(key, json.dumps(record.to_dict()))
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        lockout._check_and_record_attempt_memory(store, key, identifier, False, now)

        stored = _stored(store, key)
        assert stored["last_failed_attempt"] == now.isoformat()
        assert stored["admin_unlocked_at"] is None, "must be None, not an empty-but-truthy sentinel"

    def test_a_failed_login_sets_first_failed_attempt_once_and_never_overwrites_it(
        self,
    ) -> None:
        store = lockout.InMemoryLockoutStore()
        identifier = "mem-first-attempt@example.com"
        key = lockout._lockout_key(identifier)
        first = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        second = first + timedelta(minutes=1)

        lockout._check_and_record_attempt_memory(store, key, identifier, False, first)
        assert _stored(store, key)["first_failed_attempt"] == first.isoformat(), (
            "the FIRST failure must stamp a real timestamp, not leave it unset"
        )

        lockout._check_and_record_attempt_memory(store, key, identifier, False, second)
        assert _stored(store, key)["first_failed_attempt"] == first.isoformat(), (
            "a LATER failure must not move the window's start"
        )


class TestMemoryDispatch:
    """The public entry point, ``check_and_record_attempt`` -- distinct from the
    tests above, which call the internal function directly and so cannot see a
    mutation at the DISPATCH call site itself."""

    def test_dispatch_stamps_the_correct_identifier_on_a_brand_new_record(
        self, memory_store
    ) -> None:
        identifier = "brand-new-mem@example.com"

        lockout.check_and_record_attempt(identifier, success=False)

        assert lockout.get_lockout_info(identifier)["identifier"] == identifier


# ── The Redis path: the same boundary, plus the degradation fallback ──────────────


class _CasRedis:
    """Minimal CAS-capable Redis stand-in for direct calls to the redis-path
    function (bypassing ``_get_store()``'s probing), tailored to boundary tests."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value

    def register_script(self, src: str):
        def _run(keys, args):
            key = keys[0]
            expected, new_value, ttl, expect_missing = args
            current = self.data.get(key)
            if expect_missing == "1":
                if current is not None:
                    return [0, current]
            elif current != expected:
                return [0, current or ""]
            self.data[key] = new_value
            return [1, ""]

        return _run

    def pipeline(self, transaction: bool = True):
        raise AssertionError("not used by these tests")


class TestRedisPathBoundary:
    def test_unlocks_at_the_exact_expiry_instant_and_restarts_the_window(self) -> None:
        fake = _CasRedis()
        identifier = "boundary-redis@example.com"
        frozen = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        record = lockout.LockoutRecord(
            identifier=identifier,
            failed_attempts=5,
            lockout_count=1,
            locked_until=frozen.isoformat(),
            first_failed_attempt=(frozen - timedelta(hours=1)).isoformat(),
            last_failed_attempt=(frozen - timedelta(minutes=1)).isoformat(),
        )
        fake.data[lockout._lockout_key(identifier)] = json.dumps(record.to_dict())

        is_locked, _ = lockout._check_and_record_attempt_redis(
            fake, lockout._lockout_key(identifier), identifier, False, frozen
        )

        assert is_locked is False, "the lockout must end AT its expiry instant, not after it"
        stored = json.loads(fake.data[lockout._lockout_key(identifier)])
        assert stored["failed_attempts"] == 1
        assert stored["first_failed_attempt"] == frozen.isoformat()


class TestRedisDegradationFallback:
    """``_check_and_record_attempt_redis``'s ``except`` handler falls back to
    ``_check_and_record_attempt_memory(_get_memory_fallback_store(), key,
    identifier, success, now)``. Every existing Redis-failure test in
    ``test_lockout_atomicity.py`` uses a single identifier and ``success=False``,
    so none of them could see the KEY, the IDENTIFIER, or the SUCCESS flag being
    dropped at that specific call site -- three different arguments, three
    different mutants.

    Assertions read ``_get_memory_fallback_store()`` directly rather than through
    ``get_lockout_info()``: reads go through ``_get_store()``, which prefers
    ``_redis_client`` whenever it is still set (a CAS-script failure alone does not
    clear it) -- so with ``broken_redis`` active, ``get_lockout_info`` reads through
    the STILL-BROKEN client, not the fallback store the write actually landed in.
    That read/write split is a pre-existing property of the degradation design
    (the same one the module's ``_record_degradation`` metric exists to surface,
    not something these three mutants touch), so it is worked around here rather
    than re-litigated.
    """

    def _fallback_record(self, identifier: str) -> dict:
        store = lockout._get_memory_fallback_store()
        raw = store.get(lockout._lockout_key(identifier))
        assert raw is not None, f"no fallback record was written for {identifier}"
        record: dict = json.loads(raw)
        return record

    def test_separate_identifiers_keep_separate_counters(self, broken_redis) -> None:
        """Dropping the KEY to ``None`` would collapse every identifier's fallback
        record onto the SAME storage key -- an attacker on one account inflating
        (or clearing) a completely different account's lockout counter."""
        lockout.check_and_record_attempt("alice@example.com", success=False)
        lockout.check_and_record_attempt("bob@example.com", success=False)
        lockout.check_and_record_attempt("bob@example.com", success=False)

        assert self._fallback_record("alice@example.com")["failed_attempts"] == 1
        assert self._fallback_record("bob@example.com")["failed_attempts"] == 2

    def test_a_new_record_gets_the_correct_identifier(self, broken_redis) -> None:
        lockout.check_and_record_attempt("carol@example.com", success=False)

        assert self._fallback_record("carol@example.com")["identifier"] == "carol@example.com"

    def test_a_successful_login_still_clears_the_counter_during_an_outage(
        self, broken_redis
    ) -> None:
        """Dropping SUCCESS to ``None`` makes ``if success:`` false regardless of
        what the caller passed -- a user who just authenticated correctly would be
        recorded as ANOTHER FAILURE the moment Redis is unavailable."""
        identifier = "dave@example.com"
        lockout.check_and_record_attempt(identifier, success=False)
        lockout.check_and_record_attempt(identifier, success=False)
        assert self._fallback_record(identifier)["failed_attempts"] == 2

        is_locked, unlock_time = lockout.check_and_record_attempt(identifier, success=True)

        assert (is_locked, unlock_time) == (False, None)
        assert self._fallback_record(identifier)["failed_attempts"] == 0, (
            "a successful login during a Redis outage must clear the counter, "
            "not be silently recorded as another failure"
        )


# ── unlock_account: the boundary ───────────────────────────────────────────────────


class TestUnlockAccountBoundary:
    def test_at_the_exact_expiry_instant_is_a_no_op(self, memory_store) -> None:
        """``now >= locked_until_dt`` -> ``now > locked_until_dt``: at the exact
        boundary the account has already effectively unlocked itself, so an admin
        "unlock" must report False and touch nothing -- not stamp an audit marker
        for an action that did not do anything."""
        identifier = "unlock-boundary@example.com"
        frozen = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        record = lockout.LockoutRecord(
            identifier=identifier,
            failed_attempts=3,
            lockout_count=1,
            locked_until=frozen.isoformat(),
        )
        memory_store.set(lockout._lockout_key(identifier), json.dumps(record.to_dict()))

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003 - signature must match
                return frozen

        with patch.object(lockout, "datetime", _Frozen):
            result = lockout.unlock_account(identifier)

        assert result is False
        stored = _stored(memory_store, lockout._lockout_key(identifier))
        assert stored["admin_unlocked_at"] is None, "a no-op unlock must not stamp an audit marker"


# ── get_lockout_info: the full wire shape the admin API returns ───────────────────


class TestGetLockoutInfoShape:
    """Every key here is returned verbatim as JSON to the admin API. A renamed or
    dropped key breaks any caller reading it by name; the existing suite only ever
    asserted the one or two keys it happened to need for its own purpose, leaving
    the rest -- and the ``is_locked`` boundary -- unchecked."""

    def test_an_unknown_identifier_returns_the_full_default_shape(self, memory_store) -> None:
        info = lockout.get_lockout_info("nobody-here@example.com")

        assert info == {
            "identifier": "nobody-here@example.com",
            "is_locked": False,
            "failed_attempts": 0,
            "lockout_count": 0,
            "locked_until": None,
            "first_failed_attempt": None,
            "last_failed_attempt": None,
            "admin_unlocked_at": None,
            "lockout_enabled": True,
        }

    def test_a_known_locked_record_returns_the_full_shape(self, memory_store) -> None:
        identifier = "known-locked@example.com"
        # Relative to "now" (not a fixed date) so this never rots as the calendar moves.
        locked_until = datetime.now(UTC) + timedelta(hours=1)
        record = lockout.LockoutRecord(
            identifier=identifier,
            failed_attempts=5,
            lockout_count=1,
            locked_until=locked_until.isoformat(),
            first_failed_attempt=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            last_failed_attempt=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        memory_store.set(lockout._lockout_key(identifier), json.dumps(record.to_dict()))

        info = lockout.get_lockout_info(identifier)

        assert info == {
            "identifier": identifier,
            "is_locked": True,
            "failed_attempts": 5,
            "lockout_count": 1,
            "locked_until": locked_until.isoformat(),
            "first_failed_attempt": record.first_failed_attempt,
            "last_failed_attempt": record.last_failed_attempt,
            "admin_unlocked_at": None,
            "lockout_enabled": True,
        }

    def test_is_locked_is_false_at_the_exact_expiry_instant(self, memory_store) -> None:
        identifier = "info-boundary@example.com"
        frozen = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        record = lockout.LockoutRecord(
            identifier=identifier,
            failed_attempts=3,
            lockout_count=1,
            locked_until=frozen.isoformat(),
        )
        memory_store.set(lockout._lockout_key(identifier), json.dumps(record.to_dict()))

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003 - signature must match
                return frozen

        with patch.object(lockout, "datetime", _Frozen):
            info = lockout.get_lockout_info(identifier)

        assert info["is_locked"] is False


# ── cleanup_expired_lockouts: a record shape the existing suite never wrote ───────


class TestCleanupHandlesFirstOnlyRecords:
    def test_does_not_crash_on_a_record_with_only_a_first_attempt_timestamp(
        self, memory_store
    ) -> None:
        """``last_activity``'s fallback branch reads ``first_failed_attempt`` only
        when ``last_failed_attempt`` is falsy. A mutant nulling that argument turns
        this reachable-but-rare record shape into an unhandled ``TypeError`` inside
        the periodic Celery-beat sweep -- crashing the whole sweep, not just
        skipping one record."""
        identifier = "first-only@example.com"
        record = lockout.LockoutRecord(
            identifier=identifier,
            failed_attempts=1,
            first_failed_attempt=datetime.now(UTC).isoformat(),
            last_failed_attempt=None,
        )
        memory_store.set(lockout._lockout_key(identifier), json.dumps(record.to_dict()))

        cleaned = lockout.cleanup_expired_lockouts()  # must not raise

        assert cleaned == 0, "a record active moments ago must not be swept"
