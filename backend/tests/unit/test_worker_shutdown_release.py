"""Graceful GPU worker shutdown — the release logic itself (issue #782).

`./opentr.sh stop` reached a live CUDA process with docker's bare 10s SIGKILL, with no
signal handler to release the transcriber/diarizer/CUDA context on the way out. This file
covers `app.core.worker_shutdown` — the release logic — kept in a module of its own so it
is unit-testable **without importing the celery app** (`app/core/celery.py` is already
~950 lines and pulls in torch at import time). `app/core/celery.py` wires three thin
`@signal.connect` shims that delegate into it; those are covered here too, by driving the
REAL celery signals rather than grepping the source for `@x.connect`.

Three premises from the issue text are WRONG and this file pins the corrected behaviour:

- P3: `worker_shutting_down` fires **inside** celery's signal handler, before the pool has
  drained the in-flight task — releasing models there would free VRAM under a live CUDA
  kernel. Its handler must do NOTHING but arm a flag. `test_worker_shutting_down_handler_
  calls_no_release_function` is the anti-regression: an edit that follows the issue text
  literally (wire the release into `worker_shutting_down`) fails here.
- P5: `release_transcriber()` frees only the transcriber. The real release must call
  `ModelManager.release_all()`, which also frees the diarizer.
- The `sys.modules` gate in every release step is LOAD-BEARING, not an optimisation: on a
  worker that never imported torch (celery-nlp-worker, celery-download-worker, a lite
  deployment), an `import` here would drag in a multi-second dependency on a shutdown path
  that has no business touching it. `test_never_loaded_subsystems_return_without_importing`
  proves this by making any import attempt fail outright.
"""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from app.core import worker_shutdown as ws


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Every test gets a fresh `_SHUTDOWN`/`_RELEASED` — these are process-wide singletons."""
    ws._SHUTDOWN.clear()
    ws._RELEASED.clear()
    yield
    ws._SHUTDOWN.clear()
    ws._RELEASED.clear()


def _fake_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


# ---------------------------------------------------------------------------------------
# The release logic itself
# ---------------------------------------------------------------------------------------


@pytest.mark.unit
class TestReleaseWorkerResources:
    def test_never_loaded_subsystems_return_without_importing(self, monkeypatch):
        """Fast path (issue #782's `sys.modules` gate): a worker that never touched
        pii_pool/model_manager/speaker_embedding_service must not import them just to
        find out they are irrelevant — that would drag torch onto a CPU-only worker's
        shutdown path. Proven by making `__import__` itself fail: if the release logic
        ever falls back to a real import, this test errors instead of passing quietly.
        """
        monkeypatch.delitem(sys.modules, "app.transcription.model_manager", raising=False)
        monkeypatch.delitem(sys.modules, "app.services.redaction.pii_pool", raising=False)
        monkeypatch.delitem(sys.modules, "app.services.speaker_embedding_service", raising=False)

        import builtins

        def _raise(*args, **kwargs):
            raise AssertionError(
                "release_worker_resources attempted a real import — the sys.modules "
                "gate must use .get(), never `import`"
            )

        monkeypatch.setattr(builtins, "__import__", _raise)

        started = time.monotonic()
        ws.release_worker_resources()
        elapsed = time.monotonic() - started

        assert elapsed < 0.05, (
            f"release took {elapsed:.4f}s with nothing loaded — should be near-instant "
            "dict lookups only"
        )

    def test_exception_in_one_step_does_not_skip_the_others(self, monkeypatch):
        """Each release step is exception-isolated: pii_pool raising must not prevent
        the model manager or embedding cache from being released too."""
        calls: list[str] = []

        def _pii_shutdown():
            calls.append("pii")
            raise RuntimeError("boom-pii")

        class _FakeManager:
            _instance = object()  # sentinel: "an instance exists"

            @classmethod
            def get_instance(cls):
                calls.append("models")
                raise RuntimeError("boom-models")

        def _clear_embedding_cache():
            calls.append("embed")
            raise RuntimeError("boom-embed")

        monkeypatch.setitem(
            sys.modules,
            "app.services.redaction.pii_pool",
            _fake_module("app.services.redaction.pii_pool", shutdown=_pii_shutdown),
        )
        monkeypatch.setitem(
            sys.modules,
            "app.transcription.model_manager",
            _fake_module("app.transcription.model_manager", ModelManager=_FakeManager),
        )
        monkeypatch.setitem(
            sys.modules,
            "app.services.speaker_embedding_service",
            _fake_module(
                "app.services.speaker_embedding_service",
                _cached_embedding_service=object(),
                clear_embedding_cache=_clear_embedding_cache,
            ),
        )

        # Must not raise out of release_worker_resources itself.
        ws.release_worker_resources()

        assert set(calls) == {"pii", "models", "embed"}, (
            f"not every subsystem was invoked despite each being isolated: {calls}"
        )

    def test_a_worker_with_no_models_loaded_is_a_fast_noop(self, monkeypatch):
        """Edge case: module present (imported), but no instance was ever created."""

        get_instance_calls: list[bool] = []

        class _IdleManager:
            _instance = None

            @classmethod
            def get_instance(cls):
                get_instance_calls.append(True)
                raise AssertionError("get_instance() must not be called on an idle manager")

        monkeypatch.setitem(
            sys.modules,
            "app.transcription.model_manager",
            _fake_module("app.transcription.model_manager", ModelManager=_IdleManager),
        )
        monkeypatch.delitem(sys.modules, "app.services.redaction.pii_pool", raising=False)
        monkeypatch.delitem(sys.modules, "app.services.speaker_embedding_service", raising=False)

        ws.release_worker_resources()

        assert not get_instance_calls, (
            "release_worker_resources() called ModelManager.get_instance() despite "
            "_instance being None -- the fast path must check _instance first"
        )
        assert ws._RELEASED.is_set(), "release_worker_resources() did not mark itself released"

    def test_idempotent_second_call_is_a_noop(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setitem(
            sys.modules,
            "app.services.redaction.pii_pool",
            _fake_module(
                "app.services.redaction.pii_pool",
                shutdown=lambda: calls.append("pii"),
            ),
        )
        monkeypatch.delitem(sys.modules, "app.transcription.model_manager", raising=False)
        monkeypatch.delitem(sys.modules, "app.services.speaker_embedding_service", raising=False)

        ws.release_worker_resources()
        ws.release_worker_resources()

        assert calls == ["pii"], f"release ran more than once: {calls}"

    def test_budget_exceeded_forces_exit(self, monkeypatch):
        """The watchdog: a release that hangs must not be left to docker's own SIGKILL."""
        forced: list[int] = []
        monkeypatch.setattr(ws.os, "_exit", lambda code: forced.append(code))

        def _slow_shutdown():
            # The injected fault IS the subject under test: this stands in for a release
            # step that outlives its budget, which is what test_budget_exceeded_forces_exit
            # exists to prove the watchdog catches. There is no poll-based equivalent --
            # the watchdog itself is a real threading.Timer racing the wall clock, so
            # proving it fires needs a real duration to race against (same shape as
            # unit/test_celery_fork_init.py's `_stalling_model`, ACCEPTED in
            # tests/audit-allowlist.txt for the identical reason).
            time.sleep(0.3)

        monkeypatch.setitem(
            sys.modules,
            "app.services.redaction.pii_pool",
            _fake_module("app.services.redaction.pii_pool", shutdown=_slow_shutdown),
        )
        monkeypatch.delitem(sys.modules, "app.transcription.model_manager", raising=False)
        monkeypatch.delitem(sys.modules, "app.services.speaker_embedding_service", raising=False)

        ws.release_worker_resources(budget_s=0.05)

        assert forced == [0], f"expected exactly one forced os._exit(0) call, got {forced}"

    def test_a_healthy_release_cancels_the_watchdog(self, monkeypatch):
        """Control: the watchdog must not fire on the ordinary fast path."""
        forced: list[int] = []
        monkeypatch.setattr(ws.os, "_exit", lambda code: forced.append(code))
        monkeypatch.delitem(sys.modules, "app.transcription.model_manager", raising=False)
        monkeypatch.delitem(sys.modules, "app.services.redaction.pii_pool", raising=False)
        monkeypatch.delitem(sys.modules, "app.services.speaker_embedding_service", raising=False)

        ws.release_worker_resources(budget_s=5.0)
        # No sleep needed: watchdog.cancel() runs synchronously in release_worker_resources'
        # `finally` block, before this call returns -- by the time control comes back here,
        # the Timer is already cancelled (Timer.cancel() sets the internal event a waiting
        # `run()` checks) and can never invoke _force_exit, regardless of wall-clock timing.

        assert forced == [], "the watchdog fired despite a fast, successful release"

    def test_emits_a_greppable_summary_line(self, monkeypatch, caplog):
        import logging

        monkeypatch.delitem(sys.modules, "app.transcription.model_manager", raising=False)
        monkeypatch.delitem(sys.modules, "app.services.redaction.pii_pool", raising=False)
        monkeypatch.delitem(sys.modules, "app.services.speaker_embedding_service", raising=False)

        with caplog.at_level(logging.INFO, logger="app.core.worker_shutdown"):
            ws.release_worker_resources()

        messages = [r.getMessage() for r in caplog.records]
        assert any("worker shutdown" in m and "elapsed=" in m for m in messages), messages


@pytest.mark.unit
class TestShutdownFlag:
    def test_mark_shutting_down_sets_the_flag(self):
        assert ws.shutdown_requested() is False
        ws.mark_shutting_down()
        assert ws.shutdown_requested() is True

    def test_mark_shutting_down_releases_nothing(self, monkeypatch):
        """Arming the flag is the ONLY thing this function may do (P3)."""
        released = []
        monkeypatch.setattr(ws, "release_worker_resources", lambda **kw: released.append(True))
        ws.mark_shutting_down()
        assert not released, "mark_shutting_down() must not release anything"


# ---------------------------------------------------------------------------------------
# Registration: via the REAL celery signals, not a grep of the source
# ---------------------------------------------------------------------------------------


@pytest.mark.unit
class TestCelerySignalWiring:
    """Importing app.core.celery registers three real celery.signals receivers. Proven by
    firing the actual signal and observing the effect, not by searching for `@x.connect`
    in the source (a decorator that is never reached by any code path would still match
    a grep)."""

    @staticmethod
    def _import_celery_module():
        pytest.importorskip("celery")
        import celery.signals as signals

        from app.core import celery as celery_module  # noqa: F401

        return signals

    def test_worker_shutdown_signal_invokes_release_worker_resources(self, monkeypatch):
        signals = self._import_celery_module()
        called = []
        monkeypatch.setattr(ws, "release_worker_resources", lambda **kw: called.append(kw))

        signals.worker_shutdown.send(sender=None)

        assert len(called) == 1, (
            "celery.signals.worker_shutdown fired but app.core.worker_shutdown."
            f"release_worker_resources was called {len(called)} times (expected 1) — "
            "check the @worker_shutdown.connect wiring in app/core/celery.py"
        )

    def test_worker_shutting_down_signal_invokes_mark_shutting_down_only(self, monkeypatch):
        """The P3 anti-regression, exercised through the real signal: the handler wired to
        `worker_shutting_down` must call `mark_shutting_down()` and NOTHING that releases
        a model — an edit that follows the issue text literally (wiring the release into
        this signal instead of `worker_shutdown`) fails this test."""
        signals = self._import_celery_module()
        marked = []
        released = []
        monkeypatch.setattr(ws, "mark_shutting_down", lambda: marked.append(True))
        monkeypatch.setattr(ws, "release_worker_resources", lambda **kw: released.append(True))

        signals.worker_shutting_down.send(sender=None)

        assert marked, "worker_shutting_down fired but mark_shutting_down() was never called"
        assert not released, (
            "worker_shutting_down's handler called release_worker_resources() — it fires "
            "BEFORE the task pool has drained (P3), so this frees VRAM under a live CUDA "
            "kernel. Release belongs on worker_shutdown, not worker_shutting_down."
        )

    def test_worker_process_shutdown_signal_invokes_embedding_cache_release_only(self, monkeypatch):
        signals = self._import_celery_module()
        embed_calls = []
        release_calls = []
        monkeypatch.setattr(ws, "release_embedding_cache_only", lambda: embed_calls.append(True))
        monkeypatch.setattr(ws, "release_worker_resources", lambda **kw: release_calls.append(True))

        signals.worker_process_shutdown.send(sender=None)

        assert embed_calls, (
            "celery.signals.worker_process_shutdown fired but release_embedding_cache_only "
            "was never called"
        )
        assert not release_calls, (
            "worker_process_shutdown triggered the full release — it fires per forked "
            "prefork child and must do the embedding-cache-only cleanup, nothing else"
        )


@pytest.mark.unit
class TestReleaseEmbeddingCacheOnly:
    def test_releases_only_the_embedding_cache(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setitem(
            sys.modules,
            "app.services.speaker_embedding_service",
            _fake_module(
                "app.services.speaker_embedding_service",
                _cached_embedding_service=object(),
                clear_embedding_cache=lambda: calls.append("embed"),
            ),
        )

        ws.release_embedding_cache_only()

        assert calls == ["embed"]

    def test_swallows_an_exception_rather_than_crashing_the_reaper(self, monkeypatch):
        raise_calls: list[bool] = []

        def _raise():
            raise_calls.append(True)
            raise RuntimeError("boom")

        monkeypatch.setitem(
            sys.modules,
            "app.services.speaker_embedding_service",
            _fake_module(
                "app.services.speaker_embedding_service",
                _cached_embedding_service=object(),
                clear_embedding_cache=_raise,
            ),
        )

        ws.release_embedding_cache_only()  # must not raise

        assert raise_calls == [True], (
            "the fault was never actually exercised -- the test proved nothing about "
            "exception isolation"
        )

    def test_never_loaded_is_a_noop(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "app.services.speaker_embedding_service", raising=False)

        import builtins

        import_attempts: list[bool] = []

        def _raise_on_import(*args, **kwargs):
            import_attempts.append(True)
            raise AssertionError(
                "release_embedding_cache_only() attempted a real import when the module "
                "was never loaded -- it must use sys.modules.get(), never `import`"
            )

        monkeypatch.setattr(builtins, "__import__", _raise_on_import)

        ws.release_embedding_cache_only()  # must not raise

        assert import_attempts == [], (
            "release_embedding_cache_only() attempted a real import despite the module "
            "being absent from sys.modules"
        )


@pytest.mark.unit
def test_budget_env_var_is_read_at_import_time_with_a_sane_default(monkeypatch):
    """Documents the contract `OT_WORKER_SHUTDOWN_BUDGET_S` sits on: a garbage or absent
    value must not crash a worker's shutdown path."""
    assert ws._BUDGET_S > 0
    assert ws._BUDGET_S < 300, "budget must stay finite -- a genuinely hung release still exits"


@pytest.mark.unit
def test_watchdog_uses_a_thread_timer_not_sigalrm():
    """SIGALRM is documented (core/celery.py) as unreliable under --pool=threads, which is
    every GPU worker here -- a Timer thread is pool-agnostic."""
    import ast
    import inspect
    import textwrap

    source = inspect.getsource(ws.release_worker_resources)
    tree = ast.parse(textwrap.dedent(source))
    calls = [ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert any(call.endswith("threading.Timer") for call in calls), calls
    assert not any("alarm" in call for call in calls), (
        f"release_worker_resources calls signal.alarm somewhere: {calls}"
    )
    assert isinstance(threading.Timer(0, lambda: None), threading.Timer)
