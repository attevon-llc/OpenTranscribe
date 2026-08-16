"""The API process pays a ~10 s Presidio build on the first masked answer (issue #74).

``celery-redaction`` preloads the detectors; the API process never did, yet it runs
three inline maskers — chat's fail-closed fallback, output redaction, and segment-edit
re-detection. Measured in a fresh process: ~9.9 s to build the ``AnalyzerEngine``,
~0.2 s for the first ``analyze()``, ~0.01 s warm. So the singleton was already correct;
what was missing was warming it off the request path.

Two independent things are under test, and the second is the one that is easy to miss:

1. **The gate.** Redaction is opt-out, so a deployment where nobody enabled it must not
   be charged ~500 MB and a busy core at boot. ``redaction_is_in_use`` is the whole
   decision, and it has to say *no* by default or the warm-up is just an unconditional
   tax.
2. **The build lock.** A warm-up thread runs concurrently with inbound requests **by
   design**, so a request landing inside the build window is the normal case. Without a
   lock ``_get_analyzer`` had no mutual exclusion at all: measured, two concurrent
   callers built two separate engines (~11.6 s each against 10.1 s solo, because they
   contend), so the warm-up made the very case it exists to help *slower* and doubled
   peak RAM. A warm-up without the lock is a pessimisation.

Presidio itself is never built here — ``_build_analyzer`` is replaced by a slow
sentinel, because what is being tested is the caching and locking around it, not spaCy.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.services.redaction import warmup
from app.services.redaction.config import redaction_is_in_use
from app.services.redaction.detectors import pii_presidio


class _FakeAnalyzer:
    """Stands in for a built ``AnalyzerEngine``; identity is what the tests read."""

    def analyze(self, text: str, language: str):  # noqa: ARG002
        return []


@pytest.fixture
def controlled_builder(monkeypatch):
    """A build that BLOCKS until the test releases it, and records every entry.

    Deliberately not a ``time.sleep`` stand-in for a slow build. A fixed sleep makes
    the interesting window a race against the scheduler: too short and the second
    caller arrives after the build finished, which is a green test that exercised
    nothing. ``entered`` reports that a build is genuinely in flight and ``release``
    decides when it ends, so "a caller arrived mid-build" is a fact rather than a
    hope.

    ``builds`` is the discriminator throughout: a caller that started its OWN build
    appends to it, whether or not it later blocks.
    """
    state = SimpleNamespace(
        builds=[],
        entered=threading.Event(),
        release=threading.Event(),
    )

    def _build(_use_gliner: bool):
        state.builds.append(threading.current_thread().name)
        state.entered.set()
        assert state.release.wait(timeout=10), "test never released the build"
        return _FakeAnalyzer()

    monkeypatch.setattr(pii_presidio, "_build_analyzer", _build)
    return state


@pytest.fixture(autouse=True)
def reset_analyzer_singleton():
    """Clear the process-wide analyzer around every test in this module.

    The singleton outlives a test by design, so without this the second test to run
    would hit a warm cache and assert nothing.
    """
    pii_presidio._analyzer = None
    pii_presidio._analyzer_gliner = None
    pii_presidio._load_failed = False
    yield
    pii_presidio._analyzer = None
    pii_presidio._analyzer_gliner = None
    pii_presidio._load_failed = False


@pytest.fixture
def no_redaction_anywhere(db_session):
    """Make "nobody redacts" an actual precondition, not an assumption.

    ``db_session`` is a savepoint that is always rolled back, so deleting these rows
    is confined to the test. Asserting the default against whatever the shared dev
    database happens to contain would be a test that passes for the wrong reason.
    """
    from app import models

    db_session.query(models.UserSetting).filter(
        models.UserSetting.setting_key == "redaction_enabled"
    ).delete(synchronize_session=False)
    db_session.query(models.SystemSettings).filter(
        models.SystemSettings.key.in_(
            ["redaction.force_pii", "redaction.force_toxicity", "redaction.force_profanity"]
        )
    ).delete(synchronize_session=False)
    db_session.flush()


# --------------------------------------------------------------------- the gate


def test_a_deployment_where_nobody_redacts_does_not_warm(db_session, no_redaction_anywhere):
    """The opt-out default: no users, no floor, no reason to spend 500 MB."""
    assert redaction_is_in_use(db_session) is False


def test_one_user_enabling_redaction_is_enough_to_warm(
    db_session, normal_user, no_redaction_anywhere
):
    """A single opted-in user makes Presidio load on their first masked answer.

    The gate is deliberately not "does anyone mask the pii *category*": every inline
    masker runs ``detection_config_for_all()``, which runs all detectors whatever the
    user's categories are. This user enables redaction and sets NO categories, which
    is exactly the case a category-based gate would wrongly skip.
    """
    from app import models

    db_session.add(
        models.UserSetting(
            user_id=normal_user.id, setting_key="redaction_enabled", setting_value="true"
        )
    )
    db_session.flush()

    assert redaction_is_in_use(db_session) is True


def test_a_user_who_turned_redaction_off_does_not_trigger_a_warm_up(
    db_session, normal_user, no_redaction_anywhere
):
    """Control: a present row is not the same as an enabled one.

    Without this, a gate implemented as "does a redaction_enabled row exist" would
    pass the test above and warm on every deployment that ever rendered the settings
    page.
    """
    from app import models

    db_session.add(
        models.UserSetting(
            user_id=normal_user.id, setting_key="redaction_enabled", setting_value="false"
        )
    )
    db_session.flush()

    assert redaction_is_in_use(db_session) is False


def test_an_admin_force_floor_warms_even_with_every_user_opted_out(
    db_session, normal_user, no_redaction_anywhere
):
    """The floor turns masking on for everyone, so it must warm on its own.

    ``resolve_effective_config`` computes ``enabled = user_enabled or
    bool(forced_categories)``, so a forced category overrides the user rows the gate
    would otherwise be reading — which is why the floor is checked separately rather
    than inferred from user preferences.
    """
    from app import models
    from app.services import system_settings_service

    db_session.add(
        models.UserSetting(
            user_id=normal_user.id, setting_key="redaction_enabled", setting_value="false"
        )
    )
    system_settings_service.set_setting(db_session, "redaction.force_pii", True)
    db_session.flush()

    assert redaction_is_in_use(db_session) is True


# ------------------------------------------------------- the build lock (issue #74)


def test_two_concurrent_callers_build_exactly_one_analyzer(controlled_builder):
    """A request landing inside the warm-up window must not start a second build.

    This is the must-fire test for the lock. Without it both callers pass the
    ``_analyzer is None`` check and each calls ``_build_analyzer``: measured on the
    real detector as 2 distinct engines at ~11.6 s apiece (vs 10.1 s solo, because
    they contend for CPU) and ~2x the peak RAM — the warm-up actively harming the
    case it exists to fix.
    """
    engines: list[object] = []
    barrier = threading.Barrier(2)

    def caller():
        barrier.wait()
        engines.append(pii_presidio._get_analyzer(False))

    threads = [threading.Thread(target=caller, name=f"caller-{i}") for i in range(2)]
    for t in threads:
        t.start()

    assert controlled_builder.entered.wait(timeout=5), "no build ever started"
    controlled_builder.release.set()
    for t in threads:
        t.join(timeout=10)

    assert len(controlled_builder.builds) == 1, "the analyzer was built more than once"
    assert len(engines) == 2
    assert engines[0] is engines[1], "concurrent callers got different analyzer instances"


def test_a_caller_arriving_mid_build_waits_for_it_rather_than_starting_its_own(
    controlled_builder,
):
    """The second caller BLOCKS on the build in flight rather than starting its own.

    Blocking is the intended outcome: it is the same engine, sooner, for less CPU,
    and the caller was going to block for a cold build either way. Measured on the
    real detector, a request arriving 3 s into a ~10 s build now waits 7.0 s (the
    remainder) instead of 8.8 s building a duplicate.
    """
    first: list[object] = []
    warm = threading.Thread(
        target=lambda: first.append(pii_presidio._get_analyzer(False)), name="warmup"
    )
    warm.start()
    assert controlled_builder.entered.wait(timeout=5), "the warm-up build never started"

    # A build is now provably in flight and cannot finish until released.
    second: list[object] = []
    done = threading.Event()

    def late_caller():
        second.append(pii_presidio._get_analyzer(False))
        done.set()

    threading.Thread(target=late_caller, name="request").start()

    # It must not complete, and — the discriminator — must not have begun a build of
    # its own. Without the lock `builds` reaches 2 here.
    assert not done.wait(timeout=0.5), "the late caller returned before the build finished"
    assert len(controlled_builder.builds) == 1, "a second build started while one was in flight"

    controlled_builder.release.set()
    assert done.wait(timeout=10), "the late caller never unblocked after the build finished"
    warm.join(timeout=10)

    assert second[0] is first[0], "the late caller got a different engine"
    assert len(controlled_builder.builds) == 1


def test_the_warm_cache_hit_does_not_take_the_lock(controlled_builder):
    """Control: the fast path stays lock-free, since it runs on every masked segment.

    Measured at ~2 us. Holding the lock on every call would serialize every detection
    in the process behind a mutex for no reason — so this asserts a cache hit
    completes while another thread is *holding* the build lock.
    """
    controlled_builder.release.set()
    pii_presidio._get_analyzer(False)
    assert len(controlled_builder.builds) == 1

    hit: list[object] = []
    done = threading.Event()

    def cache_reader():
        hit.append(pii_presidio._get_analyzer(False))
        done.set()

    with pii_presidio._analyzer_lock:
        threading.Thread(target=cache_reader, name="cache-reader").start()
        completed = done.wait(timeout=5)

    assert completed, "a warm cache hit blocked on the build lock"
    assert hit[0] is not None
    assert len(controlled_builder.builds) == 1


# --------------------------------------------------------- warm-up is never fatal


def test_a_failing_build_does_not_raise_out_of_the_warm_up(monkeypatch):
    """Presidio is optional; an absent one must leave startup and masking untouched.

    ``_get_analyzer`` still returns ``None`` afterwards, which is the seam every
    caller's fail-closed handling is already built on.
    """

    def _explode(_use_gliner: bool):
        raise RuntimeError("no spaCy model on this box")

    monkeypatch.setattr(pii_presidio, "_build_analyzer", _explode)

    assert warmup.warm_pii_analyzer() is False
    assert pii_presidio._get_analyzer(False) is None


def test_the_warm_up_probe_runs_a_real_detection_pass(monkeypatch):
    """The throwaway ``analyze()`` is paid on the thread, not on the first request.

    The first ``analyze()`` costs ~0.2 s against ~0.01 s warm, so skipping it would
    leave a visible fraction of the stall on the user's request.
    """
    analyzed: list[str] = []

    class _Recording(_FakeAnalyzer):
        def analyze(self, text: str, language: str):  # noqa: ARG002
            analyzed.append(text)
            return []

    monkeypatch.setattr(pii_presidio, "_build_analyzer", lambda _g: _Recording())

    assert warmup.warm_pii_analyzer() is True
    assert analyzed, "the warm-up built the analyzer but never exercised it"


def test_the_warm_up_thread_does_not_block_the_caller(monkeypatch):
    """``start_pii_warmup`` must return immediately — the lifespan calls it.

    Measured at 0.53 ms against a real database. A synchronous build here would add
    ~10 s to startup, inside the backend's healthcheck window and ahead of every
    service ordered behind it.
    """
    released = threading.Event()
    entered = threading.Event()

    def _slow_gate():
        entered.set()
        released.wait(timeout=10)

    monkeypatch.setattr(warmup, "_warm_if_in_use", _slow_gate)

    started = time.perf_counter()
    thread = warmup.start_pii_warmup()
    elapsed = time.perf_counter() - started

    try:
        assert entered.wait(timeout=5), "the warm-up thread never started"
        assert thread.is_alive()
        assert elapsed < 0.5, "start_pii_warmup blocked on the warm-up"
    finally:
        released.set()
        thread.join(timeout=5)


def test_the_warm_up_skips_the_build_when_nothing_redacts(monkeypatch):
    """The gate is wired to the build, not merely defined beside it.

    Without this, ``redaction_is_in_use`` could be correct and unread — every
    deployment would still pay the load. Paired with its opposite below, because
    "never builds" is also satisfied by a warm-up that does nothing at all.
    """
    builds: list[bool] = []
    monkeypatch.setattr("app.services.redaction.config.redaction_is_in_use", lambda _db: False)
    monkeypatch.setattr(warmup, "warm_pii_analyzer", lambda: builds.append(True))

    warmup._warm_if_in_use()

    assert builds == [], "the analyzer was built on a deployment that never redacts"


def test_the_warm_up_builds_when_the_deployment_does_redact(monkeypatch):
    """The other half of the gate: a redacting deployment actually gets warmed."""
    builds: list[bool] = []
    monkeypatch.setattr("app.services.redaction.config.redaction_is_in_use", lambda _db: True)
    monkeypatch.setattr(warmup, "warm_pii_analyzer", lambda: builds.append(True))

    warmup._warm_if_in_use()

    assert builds == [True], "a redacting deployment was left cold"
