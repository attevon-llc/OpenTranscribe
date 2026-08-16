"""OIDC state-store behaviour that no test asserted (issue #446, mutation survivors).

Written from the 73 surviving mutants of ``app/auth/session.py`` (measured
2026-08-14, 76% coverage of the module by ``MODULE_TESTS[session]`` -- exactly the
module's coverage FLOOR, per the handoff: "no headroom, so any test you add should
raise it"). This is the fourth and last module of the issue #446 handoff; `lockout`,
`dependencies` and `security` are already triaged and committed on this branch.

The stale baseline note ("77 -> 73 after the Redis re-probe tests... Census: 31 log
strings + 2 log-only branches; 40 other") was a partial pass and, per the handoff's
own instruction, was NOT trusted here -- the 73 survivors were re-read from a fresh
run rather than assumed. The actual split is **27 real, 13 equivalent, 33 noise**,
not the stale census's "40 other" (some of that residue turned out to be equivalent,
not merely untested).

This module owns the OIDC login state store (``OIDCStateStore``, PKCE verifiers +
CSRF ``state``) and the shared Redis-or-in-memory backend both it and (indirectly,
via the imported reprobe constant) ``lockout`` sit on. Every real gap below sits on
an **unauthenticated** endpoint (the OIDC login/callback routes), so a caller-visible
bug here is either a state-exhaustion/DoS vector, a TTL/replay-window weakening, or a
crash that takes the login route down while Redis is degraded -- classification is
biased toward ``real`` for the same reason the other three modules' notes give.

Full triage of the 73:

* **27 real.** Five families:

  - **``_get_store``'s cold-boot and reprobe-crash edges (4).** ``_store_initialized``
    inverted (``not X`` -> ``X``) makes the whole "first ever call" branch
    permanently unreachable -- it can only run once already-True, which it never is
    -- so every call instead falls through the reprobe-gated ``elif``. That elif
    gates its OWN probe attempt on ``now - _last_redis_probe >= REDIS_REPROBE_SECONDS``,
    and ``_last_redis_probe`` starts at the SAME ``0.0`` sentinel value
    ``time.monotonic()`` is compared against -- so with the clock patched to also
    read ``0.0`` (simulating a process that has been up for less than the reprobe
    interval, e.g. right after a container start), the mutant never attempts a
    connection AT ALL on the very first call, silently handing out the in-memory
    fallback with zero probes. A second mutant (``_last_redis_probe = now`` ->
    ``= None`` inside the reprobe branch) crashes the *next* call outright: the
    following call's ``now - _last_redis_probe`` becomes ``now - None``, an
    unconditional ``TypeError`` raised on the unauthenticated login path -- the same
    failure class ``TestGetStoreReprobe`` in ``test_auth_state_degradation.py``
    already pins for ``lockout``, ported here for ``session``. A third
    (``_in_memory_store = InMemoryStore()`` -> ``= None`` at the module's SECOND,
    bottom-of-function safety-net site -- not the first site, which is the
    already-proven-equivalent one below) can return a bare ``None`` from
    ``_get_store()`` instead of a usable store when Redis goes down AFTER having
    been healthy (``_store_initialized`` already ``True``, so the first branch is
    skipped and only the bottom check is reachable). A fourth
    (``_redis_client is None`` -> ``is not None`` on the healthy-first-call path)
    fires the misleading "falling back to in-memory" warning even while Redis is
    healthy; kept as ``real`` rather than folded into equivalence (see the
    docstring on ``TestGetStoreDoesNotWarnWhenRedisIsHealthy`` for why the line is
    biased toward a test rather than a proof, even though the RETURN value and the
    ``_record_degradation`` metric are provably unaffected).
  - **``_record_degradation``'s label values actually reaching the metric (4).**
    Nothing called the REAL function and inspected what reached
    ``security_state_degraded_total.labels(...)`` -- every existing caller mocks
    ``_record_degradation`` itself (``test_auth_state_degradation.py``'s
    ``degraded.call_args.args == (...)`` assertions), so the function's OWN
    ``control=``/``fallback=`` kwargs wiring had no test. A corrupted or dropped
    label silently mis-labels (or, for the real Prometheus client, likely raises
    inside the function's own swallow-everything ``except``, silently discarding
    the whole metric) every degradation event this module and ``lockout`` both
    depend on for "is our shared auth state actually shared" visibility.
  - **``InMemoryStore``'s expiry boundary and prefix isolation (6).** The exact
    expiry-instant boundary (``>`` not ``>=`` in ``get``, ``<=`` not ``<`` in
    ``keys``) mirrors the boundary convention every other module in this handoff
    pins; ``keys``'s ``and``->``or`` on ``k.startswith(prefix) and (not expired)``
    would let a value from a COMPLETELY DIFFERENT key namespace leak into a
    prefix-scoped scan (this store's only consumer is Redis-keyspace-scoped OIDC
    state, but the class itself is general and ``token_service.py`` uses it too);
    the SAME line's other mutant (``not expire_at or ...`` -> ``expire_at or
    ...``) both leaks an already-expired key back into ``keys()`` (short-circuits
    true on any nonzero ``expire_at``, valid or not) and crashes with ``TypeError``
    on a key stored with no TTL at all (``None or now <= None``); and
    ``rstrip("*")`` -> ``rstrip("XX*XX")`` over-strips a prefix ending in the
    literal character ``X`` -- the exact bug class ``lockout``'s own
    ``InMemoryLockoutStore.keys`` test already documents, ported here. ``set``'s
    ``expire_at = datetime...`` -> ``= None`` (when ``ex`` IS given) silently drops
    the TTL of an explicitly time-limited value, making it immortal.
  - **``OIDCStateStore.__init__``'s falsy-but-not-``None`` sentinel (1).**
    ``self._store: Any = None`` -> ``= ""`` breaks the lazy-load contract: the
    ``store`` property checks ``self._store is None`` (identity, not truthiness),
    so a fresh ``OIDCStateStore()`` -- the real module-level singleton
    ``oidc_state_store`` included -- would permanently return the empty string
    instead of ever calling ``_get_store()``, breaking every operation.
  - **The Redis SCAN calls' ``match=`` pattern actually reaching Redis (5), and
    ``_scan_keys`` collecting the real key (1).** ``_count_states`` and
    ``_scan_keys`` both exist, per their own docstrings, specifically to avoid
    ``KEYS`` and scan ONLY the ``oidc:state:*`` namespace on a Redis instance that
    is *also* the lockout store and the Celery broker. Dropping or nulling
    ``match=`` scans the WHOLE keyspace instead: ``_count_states`` would count
    foreign keys toward the exhaustion cap (false-positive "state limit exceeded"
    refusals off OTHER subsystems' key volume), and ``_scan_keys`` -- which
    ``_cleanup_oldest_states`` feeds straight into ``self.store.delete(key)`` --
    would delete lockout counters or Celery task-result keys under load, a
    cross-subsystem data-loss bug wearing an OIDC-cleanup costume. A ``count=None``
    on the SAME calls is a separate, EQUIVALENT mutant -- see below.
    ``_scan_keys``'s ``collected.append(key ...)`` -> ``append(None)`` corrupts
    every collected key to ``None``, so cleanup deletes nothing (an attacker who
    fills the state store would find the exhaustion-cap cleanup a permanent no-op).
  - **``store_state``'s documented defaults, its cleanup call, its own cap
    boundary, and the TTL it hands to the backing store (7).** The docstring
    promises "default: 10 minutes" -- nothing pinned that number, so
    ``expires_seconds: int = 600`` -> ``= 601`` survived; the cleanup-batch-size
    call ``self._cleanup_oldest_states(100)`` had its literal argument dropped to
    ``None`` (crashes ``_scan_keys``'s ``len(collected) >= limit`` with a
    ``TypeError`` the instant Redis's exhaustion-cleanup path is reached under
    real load -- i.e. exactly when an attacker is filling the store) or bumped to
    ``101`` (an unasserted constant, same shape as ``lockout``'s
    ``_record_ttl_seconds`` pin); the POST-cleanup recap boundary
    (``current_count >= self._max_states`` -> ``> self._max_states``) is an
    off-by-one cap bypass -- at EXACTLY the cap after cleanup did nothing, the
    mutant accepts one more state instead of refusing; and, worse, the SAME
    refusal branch's ``return False`` -> ``return True`` reports success while
    silently NOT calling ``self.store.set(...)`` at all, handing the OIDC login
    flow a state it can never redeem. Finally ``self.store.set(key, value,
    ex=expires_seconds)`` -> ``ex=None`` (or the kwarg dropped outright) makes the
    stored state immortal at the ``OIDCStateStore`` call site too -- the
    higher-level counterpart to ``InMemoryStore.set``'s own TTL-drop mutant above.

* **13 equivalent**, every one verified by reading the exact code path (three by
  direct interpreter experiment) rather than guessed at:

  - **``_get_store``'s first-site ``_in_memory_store = InMemoryStore()`` -> ``None``**
    (``mutmut_11``, inside the ``if not _store_initialized:`` branch). Already
    proven and documented in ``test_auth_state_degradation.py::
    test_the_first_fallback_call_returns_a_real_store``: the function's own SECOND,
    bottom-of-function ``if _in_memory_store is None: _in_memory_store =
    InMemoryStore()`` unconditionally re-creates it before the function returns, so
    no observable difference survives to the caller. (Contrast with the SECOND
    site's own mutant above, which IS real -- there is no third check left to save
    it.)
  - **Three ``datetime.now(UTC)`` -> ``datetime.now(None)`` mutants**
    (``InMemoryStore.get``, ``.keys``, ``.set``). Verified by direct interpreter
    experiment (not the docs): a naive ``datetime.now(None)`` represents the
    process's LOCAL wall-clock instant, and ``.timestamp()`` on a naive datetime
    assumes local time and converts to the correct POSIX epoch second regardless --
    the exact same epoch value ``datetime.now(UTC).timestamp()`` produces, up to
    microsecond call-time jitter (measured: ``1.6e-05`` seconds apart, not a
    systematic timezone offset). Every read site in this module compares two
    ``.timestamp()`` outputs against each other, never the ``datetime`` object's
    tzinfo directly, so the aware/naive distinction never surfaces.
  - **``InMemoryStore.delete``'s ``return 1`` -> ``return 2``.** Every caller in
    the codebase (``_cleanup_oldest_states``'s ``if self.store.delete(key):``,
    ``get_state``'s unconditional call with the return value discarded,
    ``delete_state``'s ``bool(deleted)``) reads the return value ONLY as a
    truthiness check, never the exact count -- confirmed by grepping every call
    site of ``.delete(`` against this class in ``app/`` (three sites, all
    boolean-coerced or discarded). This also matches the class's own documented
    contract ("mirrors Redis API") for a single-key delete, where real Redis
    itself only ever returns 0 or 1.
  - **``InMemoryStore.set``'s ``expire_at = None`` -> ``= ""`` (the ``ex``-not-given
    initial value, NOT the ``ex``-given computed value -- that one is real, see
    above).** Grepped every read of ``expire_at`` in this module: both
    ``get`` (``if expire_at and ...``) and ``keys`` (``not expire_at or ...``) use
    plain truthiness, never ``is None``, and ``""`` is exactly as falsy as ``None``
    in both. No third read site exists.
  - **Four Redis ``count=500`` mutants** (``_count_states`` and ``_scan_keys``,
    one ``count=None`` and one dropped-kwarg-defaults-to-None each, plus two
    ``count=500`` -> ``count=501`` constant tweaks). Verified against the
    installed ``redis-py``: ``Redis.scan_iter``'s own signature default for
    ``count`` is already ``None`` (confirmed via ``inspect.signature``), so
    passing ``count=None`` explicitly, or omitting the kwarg, produces the
    IDENTICAL call redis-py would make either way. ``COUNT`` is SCAN's per-round-trip
    batch-size hint only -- it changes how many round trips the cursor takes to
    walk the keyspace, never the final aggregated result set -- so 500 vs 501 is
    an unobservable tuning constant with the same property (this is the
    ``match=`` mutants' direct opposite: ``match`` changes WHICH keys come back,
    ``count`` only changes how many round trips it costs to get them).
  - **``_cleanup_oldest_states``'s default parameter, ``count: int = 100`` ->
    ``= 101``.** Grepped every call site in ``app/`` and ``tests/``: the sole
    production caller (``store_state``) always passes ``100`` explicitly, and
    every existing test (``test_auth_state_degradation.py``) passes an explicit
    ``count=`` too -- the default is dead code, unreachable by any current caller.

* **33 noise** -- a log string (``logger.debug``/``.warning``/``.info``/``.error``)
  no caller observes, or a condition/argument that feeds *only* a log call: the
  ``_get_store`` fallback/recovery messages (11: casing/``XX``-wrap variants plus
  two ``None``-body swaps), ``_record_degradation``'s own except-block debug
  message and its ``exc_info`` kwarg (7 -- the swallow-everything CONTRACT itself
  is exercised via ``test_a_broken_metrics_backend_cannot_break_the_login_flow``;
  only the log's own text/kwargs are untested, and asserting them would break on
  every reword), ``_cleanup_oldest_states``'s ``if removed > 0:`` log-only guard
  and its message (3), ``delete_state``/``get_state``'s debug/warning text and
  the ``state[:8]`` vs ``state[:9]`` truncation length inside a LOG line only (6),
  and ``store_state``'s exhaustion-error message text plus its two purely-cosmetic
  debug-log lines (6).

No production bug required a characterization-test-then-fix loop here in the
"pin wrong behaviour, then flip it" sense -- every real gap above is EXISTING,
CORRECT behaviour with no assertion, matching ``dependencies`` and ``security``'s
own findings. The two most operationally significant gaps (the SCAN ``match=``
cross-subsystem leak, and ``store_state``'s ``return True`` on a state that was
never stored) are real risks worth having caught, but the code as WRITTEN already
does the right thing; the tests below are what was missing.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.auth import session as session_module
from app.auth.session import OIDC_STATE_PREFIX
from app.auth.session import InMemoryStore
from app.auth.session import OIDCStateStore
from app.core import metrics as metrics_module

STATE = "d4c1a9e2-mutant-state"
STATE_DATA = {"code_verifier": "mutant-verifier-value", "next": "/gallery"}


class _CountingProbe:
    """A real stand-in for ``get_redis_client`` that counts how often it fired.

    Same shape as ``test_auth_state_degradation.py``'s own ``_CountingProbe`` --
    the probe count IS the behaviour under test (a connection setup plus a PING on
    an unauthenticated path), so it is counted on a real object rather than mock
    bookkeeping.
    """

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._result


def _reset_store_module_state():
    session_module._redis_client = None
    session_module._in_memory_store = None
    session_module._store_initialized = False
    session_module._last_redis_probe = 0.0


# ── _get_store: cold-boot probing, reprobe-crash, and the second safety net ──────


@pytest.mark.unit
class TestGetStoreColdBoot:
    def teardown_method(self):
        _reset_store_module_state()

    def test_the_very_first_call_probes_redis_even_within_the_reprobe_window(self):
        """``if not _store_initialized`` must run unconditionally on the FIRST
        call -- it is not, and must not become, gated by the reprobe interval.

        ``_last_redis_probe`` starts at the same ``0.0`` sentinel
        ``time.monotonic()`` is compared against, so patching the clock to also
        read a small value (simulating a process barely past boot) makes
        ``now - _last_redis_probe >= REDIS_REPROBE_SECONDS`` false. A mutant that
        skips straight to the reprobe-gated ``elif`` branch (inverting the
        ``_store_initialized`` guard makes the true first-call branch permanently
        unreachable -- it can only run once already True) would then never even
        attempt a connection on a fresh process, silently handing out the
        in-memory fallback having never asked Redis at all.
        """
        _reset_store_module_state()
        probe = _CountingProbe(result=None)

        with (
            patch.object(session_module.time, "monotonic", return_value=1.0),
            patch.object(session_module, "get_redis_client", probe),
        ):
            store = session_module._get_store()

        assert probe.calls == 1, (
            "the first-ever call must always attempt Redis, regardless of how "
            "little monotonic time has elapsed since the sentinel"
        )
        assert isinstance(store, session_module.InMemoryStore)

    def test_a_failed_reprobe_leaves_the_clock_usable_for_the_next_call(self):
        """``_last_redis_probe = now`` (inside the reprobe-gated ``elif``, NOT
        the initial-branch's own separate stamp) must store a real float, not
        ``None``. A ``None`` here does not crash the call that sets it -- it
        crashes the NEXT one, whose ``now - _last_redis_probe`` becomes
        ``now - None``, an unconditional ``TypeError`` on the unauthenticated
        OIDC login path. Ported from ``lockout``'s own ``TestGetStoreReprobe``
        in ``test_auth_state_degradation.py``.

        Three calls are needed to actually reach the mutated line: the FIRST
        call takes the ``if not _store_initialized`` branch, which stamps the
        clock through its OWN, different, unmutated assignment. Only a SECOND
        call that is forced past the reprobe interval reaches the ``elif``'s
        nested ``if`` and executes the mutated line; a THIRD call is what then
        reads the corrupted value back and would raise.
        """
        _reset_store_module_state()

        with patch.object(session_module, "get_redis_client", return_value=None):
            first = session_module._get_store()  # call 1: initial branch

        assert isinstance(first, session_module.InMemoryStore)

        # Force call 2 down the reprobe-gated `elif` branch (not the initial
        # branch, already exercised above) by rewinding the clock past the
        # interval -- this is the call that executes the mutated line.
        session_module._last_redis_probe -= 10_000
        with patch.object(session_module, "get_redis_client", return_value=None):
            second = session_module._get_store()  # call 2

        assert isinstance(second, session_module.InMemoryStore)
        assert isinstance(session_module._last_redis_probe, float), (
            "a None clock here crashes the THIRD call's `now - _last_redis_probe`"
        )

        third = session_module._get_store()  # call 3: must not raise TypeError

        assert isinstance(third, session_module.InMemoryStore)

    def test_a_later_outage_after_being_healthy_still_returns_a_real_store(self):
        """The SECOND, bottom-of-function safety net
        (``if _in_memory_store is None: _in_memory_store = InMemoryStore()``) is
        reachable on its own: with ``_store_initialized`` already ``True`` (Redis
        was healthy before), the FIRST branch's own construction site is skipped
        entirely, so only this second site can ever populate the fallback.
        Returning bare ``None`` here crashes every caller of ``store``/``get``/
        ``set`` on the object, not just this function.
        """
        _reset_store_module_state()
        session_module._store_initialized = True
        session_module._redis_client = None
        session_module._in_memory_store = None
        session_module._last_redis_probe = 0.0

        with patch.object(session_module, "get_redis_client", return_value=None):
            store = session_module._get_store()

        assert store is not None
        assert isinstance(store, session_module.InMemoryStore)

    def test_does_not_warn_or_fall_back_when_redis_is_healthy_on_first_connect(self):
        """``if _redis_client is None:`` inverted would log the "falling back to
        in-memory" warning, and construct a wasted ``InMemoryStore()``, even while
        Redis IS reachable -- a false operational signal an operator would read as
        "OIDC logins are per-replica" when they are not. The RETURN value is
        provably unaffected either way (this function still returns the real
        Redis client), so this test targets the misleading signal directly rather
        than relying on a return-value assertion that cannot distinguish the two.
        """
        _reset_store_module_state()
        healthy = object()

        with (
            patch.object(session_module, "get_redis_client", return_value=healthy),
            patch.object(session_module.logger, "warning") as warning,
        ):
            store = session_module._get_store()

        assert store is healthy
        warning.assert_not_called()


# ── _record_degradation: the metric's own label wiring ───────────────────────────


@pytest.mark.unit
class TestRecordDegradationLabels:
    """Nothing calls the REAL function and inspects the metric -- every existing
    caller mocks ``_record_degradation`` itself, so its own ``.labels(...)`` call
    had no test. The lazy ``from app.core.metrics import
    security_state_degraded_total`` re-resolves at call time, so patching the
    module-level object's identity (not just an attribute) is picked up.
    """

    class _FakeCounter:
        def __init__(self):
            self.captured: dict[str, object] = {}
            self.inc_calls = 0

        def labels(self, **kwargs):
            self.captured = kwargs
            return self

        def inc(self):
            self.inc_calls += 1

    def test_the_real_control_and_fallback_values_reach_the_metric(self, monkeypatch):
        fake = self._FakeCounter()
        monkeypatch.setattr(metrics_module, "security_state_degraded_total", fake)

        session_module._record_degradation("oidc_state", "local")

        assert fake.captured == {"control": "oidc_state", "fallback": "local"}
        assert fake.inc_calls == 1

    def test_a_different_call_carries_its_own_values_not_a_stale_default(self, monkeypatch):
        """The control for the test above: two different calls must not collapse
        onto the same labels (e.g. a mutant hardcoding one argument)."""
        fake = self._FakeCounter()
        monkeypatch.setattr(metrics_module, "security_state_degraded_total", fake)

        session_module._record_degradation("rate_limit", "memory")

        assert fake.captured == {"control": "rate_limit", "fallback": "memory"}


# ── InMemoryStore: expiry boundary, prefix isolation, TTL propagation ────────────


@pytest.mark.unit
class TestInMemoryStoreExpiryBoundary:
    """``>`` not ``>=`` in ``get``, ``<=`` not ``<`` in ``keys`` -- at the EXACT
    expiry instant a value is still valid, matching the boundary convention this
    handoff's other three modules already pin (an entry present since before the
    deadline is still current AT the deadline, not one tick early).
    """

    def _frozen(self, instant: datetime) -> type[datetime]:
        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003
                return instant

        return _Frozen

    def test_get_does_not_expire_a_key_at_the_exact_instant(self, monkeypatch):
        store = InMemoryStore()
        instant = datetime.now(UTC)
        store._data["k"] = ("v", instant.timestamp())

        monkeypatch.setattr(session_module, "datetime", self._frozen(instant))

        assert store.get("k") == "v"

    def test_get_expires_a_key_one_tick_after_its_instant(self, monkeypatch):
        """The control: without it, a mutant treating every value as unconditionally
        valid would pass the test above too."""
        store = InMemoryStore()
        instant = datetime.now(UTC)
        store._data["k"] = ("v", instant.timestamp())

        monkeypatch.setattr(
            session_module, "datetime", self._frozen(instant + timedelta(seconds=1))
        )

        assert store.get("k") is None

    def test_keys_includes_a_key_at_the_exact_expiry_instant(self, monkeypatch):
        store = InMemoryStore()
        instant = datetime.now(UTC)
        key = f"{OIDC_STATE_PREFIX}boundary"
        store._data[key] = ("v", instant.timestamp())

        monkeypatch.setattr(session_module, "datetime", self._frozen(instant))

        assert store.keys(f"{OIDC_STATE_PREFIX}*") == [key]

    def test_keys_excludes_a_key_one_tick_after_its_expiry_instant(self, monkeypatch):
        store = InMemoryStore()
        instant = datetime.now(UTC)
        key = f"{OIDC_STATE_PREFIX}boundary"
        store._data[key] = ("v", instant.timestamp())

        monkeypatch.setattr(
            session_module, "datetime", self._frozen(instant + timedelta(seconds=1))
        )

        assert store.keys(f"{OIDC_STATE_PREFIX}*") == []


@pytest.mark.unit
class TestInMemoryStoreKeysScoping:
    """``k.startswith(prefix) and (not expired)`` -- both halves are load-bearing,
    and the two halves fail differently when weakened."""

    def test_keys_only_returns_entries_matching_the_prefix(self):
        """``and`` -> ``or``: a value from an unrelated namespace (no TTL, so its
        own half of the ``or`` is trivially true) would leak into a prefix-scoped
        scan regardless of the prefix check."""
        store = InMemoryStore()
        store.set(f"{OIDC_STATE_PREFIX}mine", "v")
        store.set("unrelated:key", "v")

        assert store.keys(f"{OIDC_STATE_PREFIX}*") == [f"{OIDC_STATE_PREFIX}mine"]

    def test_keys_excludes_an_already_expired_key(self):
        """``not expire_at or ...`` -> ``expire_at or ...``: an expired (but
        still-present) entry's ``expire_at`` is a truthy float, so the mutant's
        short-circuit would include it without ever checking it has passed."""
        store = InMemoryStore()
        past = (datetime.now(UTC) - timedelta(seconds=10)).timestamp()
        store._data[f"{OIDC_STATE_PREFIX}old"] = ("v", past)

        assert store.keys(f"{OIDC_STATE_PREFIX}*") == []

    def test_keys_includes_a_key_with_no_ttl_at_all_without_crashing(self):
        """The other half of the same mutant: with ``expire_at`` falsy (``None``,
        no TTL), the mutant's ``expire_at or (now <= expire_at)`` evaluates the
        second operand -- ``now <= None`` -- an unconditional ``TypeError`` on
        every ``keys()`` call touching a no-TTL entry."""
        store = InMemoryStore()
        store.set(f"{OIDC_STATE_PREFIX}permanent", "v")  # no ex= -> no TTL

        assert store.keys(f"{OIDC_STATE_PREFIX}*") == [f"{OIDC_STATE_PREFIX}permanent"]

    def test_keys_still_includes_a_fresh_still_valid_entry(self):
        """Positive control for the two tests above."""
        store = InMemoryStore()
        store.set(f"{OIDC_STATE_PREFIX}fresh", "v", ex=600)

        assert store.keys(f"{OIDC_STATE_PREFIX}*") == [f"{OIDC_STATE_PREFIX}fresh"]

    def test_keys_strips_only_the_trailing_wildcard_not_arbitrary_characters(self):
        """``rstrip("*")`` -> ``rstrip("XX*XX")``: the exact bug class already
        pinned for ``lockout.InMemoryLockoutStore.keys`` -- a mutant widening the
        strip set to include the literal character ``X`` over-strips a prefix
        that happens to end in one."""
        store = InMemoryStore()
        store.set("prefixX", "keep")
        store.set("prefix999", "must-not-match")  # starts with "prefix", not "prefixX"

        assert store.keys("prefixX*") == ["prefixX"]


@pytest.mark.unit
class TestInMemoryStoreSetTtl:
    def test_set_computes_a_real_future_expiry_when_ex_is_given(self):
        """``expire_at = datetime.now(UTC).timestamp() + ex`` -> ``= None``: an
        explicitly time-limited value would become immortal."""
        store = InMemoryStore()

        store.set("k", "v", ex=100)

        _, expire_at = store._data["k"]
        assert expire_at is not None
        assert expire_at > datetime.now(UTC).timestamp()


# ── OIDCStateStore.__init__: the lazy-load sentinel must be None, not falsy ──────


@pytest.mark.unit
class TestOidcStateStoreInitSentinel:
    def test_the_store_property_lazy_loads_from_a_fresh_instance(self, monkeypatch):
        """``self._store: Any = None`` -> ``= ""``: the ``store`` property checks
        ``self._store is None`` by IDENTITY, so a falsy-but-not-``None`` sentinel
        would never trigger ``_get_store()`` -- a fresh ``OIDCStateStore()``
        (including the real module-level singleton) would permanently return the
        empty string instead of ever getting a real backend."""
        sentinel = object()
        monkeypatch.setattr(session_module, "_get_store", lambda: sentinel)

        instance = OIDCStateStore()

        assert instance.store is sentinel


# ── _count_states / _scan_keys: the SCAN match= pattern actually reaching Redis ──


class _PatternSensitiveScanStore:
    """A Redis stand-in whose ``scan_iter`` only honours the CALLER'S pattern.

    Proves the code passes its own ``match=`` argument through to SCAN, rather
    than (accidentally) scanning the WHOLE keyspace -- which, on the real Redis
    instance this module shares with ``lockout`` and the Celery broker, would mean
    counting or collecting keys that belong to an entirely different subsystem.
    """

    def __init__(self, matching: list[str], foreign: list[str]) -> None:
        self._matching = list(matching)
        self._foreign = list(foreign)
        self.calls: list[tuple[object, object]] = []

    def scan_iter(self, match=None, count=None):
        self.calls.append((match, count))
        if match == f"{OIDC_STATE_PREFIX}*":
            yield from self._matching
        else:
            # No filter (None) or the wrong one: everything, foreign keys included.
            yield from (self._matching + self._foreign)

    def delete(self, key):
        return 1

    def keys(self, pattern):  # pragma: no cover - only the scan_iter path is exercised
        raise AssertionError("this stand-in has scan_iter; KEYS must never be used")


@pytest.mark.unit
class TestCountStatesScopedToItsOwnPrefix:
    def test_count_states_ignores_keys_outside_the_oidc_namespace(self):
        """``match=pattern`` dropped or nulled: the count would include Celery
        broker keys and lockout counters living on the SAME Redis instance,
        producing a false-positive "state limit exceeded" refusal driven by
        unrelated subsystems' key volume."""
        store = OIDCStateStore(max_states=1000)
        fake = _PatternSensitiveScanStore(
            matching=[f"{OIDC_STATE_PREFIX}a", f"{OIDC_STATE_PREFIX}b"],
            foreign=["lockout:someone@example.com", "celery-task-meta-1", "lockout:another"],
        )
        store._store = fake

        assert store._count_states() == 2


@pytest.mark.unit
class TestScanKeysScopedToItsOwnPattern:
    def test_scan_keys_ignores_keys_outside_its_pattern(self):
        """The same ``match=`` gap in ``_scan_keys`` is more dangerous: its result
        feeds straight into ``self.store.delete(key)`` inside
        ``_cleanup_oldest_states``, so scanning the whole keyspace here means the
        exhaustion-cap cleanup can delete a LOCKOUT COUNTER or a CELERY RESULT KEY
        instead of an OIDC state -- cross-subsystem data loss under an
        OIDC-cleanup label."""
        store = OIDCStateStore(max_states=1000)
        fake = _PatternSensitiveScanStore(
            matching=[f"{OIDC_STATE_PREFIX}a"],
            foreign=["lockout:danger", "celery-task-meta-1"],
        )
        store._store = fake

        collected = store._scan_keys(f"{OIDC_STATE_PREFIX}*", limit=10)

        assert collected == [f"{OIDC_STATE_PREFIX}a"]

    def test_scan_keys_collects_the_real_key_not_a_null_placeholder(self):
        """``collected.append(key ...)`` -> ``append(None)``: every collected
        "key" becomes ``None``, so ``_cleanup_oldest_states``'s
        ``self.store.delete(key)`` deletes nothing -- the exhaustion-cap cleanup
        silently becomes a permanent no-op under an active attack."""

        class _Store:
            def scan_iter(self, match=None, count=None):
                yield f"{OIDC_STATE_PREFIX}a"
                yield f"{OIDC_STATE_PREFIX}b"

        store = OIDCStateStore(max_states=1000)
        store._store = _Store()

        collected = store._scan_keys(f"{OIDC_STATE_PREFIX}*", limit=10)

        assert collected == [f"{OIDC_STATE_PREFIX}a", f"{OIDC_STATE_PREFIX}b"]
        assert None not in collected


# ── store_state: documented defaults, the cleanup call, the cap boundary, TTL ────


@pytest.mark.unit
class TestStoreStateDefaults:
    def test_the_documented_ten_minute_default_is_exactly_600_seconds(self):
        """The docstring promises "default: 10 minutes" -- nothing pinned the
        number, so ``600`` -> ``601`` survived."""
        store = OIDCStateStore()
        store._store = InMemoryStore()

        store.store_state(STATE, STATE_DATA)  # no expires_seconds -> the default

        _, expire_at = store.store._data[f"{OIDC_STATE_PREFIX}{STATE}"]
        expected = datetime.now(UTC).timestamp() + 600
        # A tight tolerance is deliberate: 600 vs. 601 is a 1-second difference,
        # and a loose window (e.g. seconds of slack for "test wall-clock jitter")
        # would absorb it and never actually distinguish the two.
        assert abs(expire_at - expected) < 0.5


@pytest.mark.unit
class TestStoreStateCleanupInvocation:
    def test_cleanup_is_invoked_with_the_documented_batch_size(self, monkeypatch):
        """``self._cleanup_oldest_states(100)`` -> ``(None)`` crashes
        ``_scan_keys``'s ``len(collected) >= limit`` with a ``TypeError`` the
        instant Redis's exhaustion-cleanup path runs -- i.e. exactly when an
        attacker is filling the store. ``-> (101)`` is a silently-changed,
        unasserted constant. One spy kills both."""
        store = OIDCStateStore(max_states=1)
        store._store = InMemoryStore()
        store.store_state("seed-state", {"code_verifier": "seed"})  # now at the cap

        calls: list[object] = []

        def _spy(count=100):
            calls.append(count)
            return 0

        monkeypatch.setattr(store, "_cleanup_oldest_states", _spy)

        store.store_state("trigger-state", {"code_verifier": "trigger"})

        assert calls == [100]


@pytest.mark.unit
class TestStoreStateCapBoundaryAfterCleanup:
    """``current_count >= self._max_states`` -> ``> self._max_states``, the
    POST-cleanup recheck. To hit the exact boundary the count after cleanup must
    still equal the cap (cleanup found nothing to remove), which is forced here by
    stubbing ``_count_states``/``_cleanup_oldest_states`` directly rather than
    trying to land a real in-memory store exactly on the boundary by chance.
    """

    def test_a_state_at_exactly_the_cap_after_cleanup_is_refused(self, monkeypatch):
        store = OIDCStateStore(max_states=5)
        store._store = InMemoryStore()
        monkeypatch.setattr(store, "_count_states", lambda: 5)
        monkeypatch.setattr(store, "_cleanup_oldest_states", lambda count=100: 0)

        result = store.store_state(STATE, STATE_DATA)

        assert result is False, (
            "at exactly the cap even after cleanup, the state must be refused -- "
            "an inverted `>` would accept it (cap bypass), and a `return True` "
            "typo in the SAME branch would report success while never calling "
            "store.set(), handing the login flow an unredeemable state"
        )
        assert store.store.get(f"{OIDC_STATE_PREFIX}{STATE}") is None, (
            "a refused state must never actually be written"
        )

    def test_a_state_below_the_cap_is_still_accepted(self, monkeypatch):
        """Positive control: the boundary logic must not refuse everything."""
        store = OIDCStateStore(max_states=5)
        store._store = InMemoryStore()
        monkeypatch.setattr(store, "_count_states", lambda: 4)

        assert store.store_state(STATE, STATE_DATA) is True
        assert store.store.get(f"{OIDC_STATE_PREFIX}{STATE}") is not None


@pytest.mark.unit
class TestStoreStatePropagatesItsOwnTtl:
    def test_an_explicit_expiry_reaches_the_backing_store(self):
        """``self.store.set(key, value, ex=expires_seconds)`` -> ``ex=None`` (or
        the kwarg dropped): the caller-requested expiry never reaches the backend,
        so the state is immortal regardless of what the caller asked for."""
        store = OIDCStateStore()
        store._store = InMemoryStore()

        store.store_state(STATE, STATE_DATA, expires_seconds=5)

        _, expire_at = store.store._data[f"{OIDC_STATE_PREFIX}{STATE}"]
        assert expire_at is not None
        expected = datetime.now(UTC).timestamp() + 5
        assert abs(expire_at - expected) < 2
