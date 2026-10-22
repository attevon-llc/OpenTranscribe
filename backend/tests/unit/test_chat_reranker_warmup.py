"""Startup warm-up of the chat reranker (issue #1269).

The first chat in each fresh API process paid the whole cold load of the CPU
cross-encoder. ``chat/reranker_warmup.py`` loads it on a daemon thread at startup.
These tests drive the real ``get_reranker``/``rerank``/warm-up code and replace only
``sentence_transformers.CrossEncoder`` (a stub module in ``sys.modules``, as
``test_chat_reranker_retry.py`` does), so torch is never imported.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from app.core import constants as C  # noqa: N812
from app.services.chat import reranker as reranker_mod
from app.services.chat import reranker_warmup
from app.services.system_settings_service import set_setting

_APP_DIR = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture(autouse=True)
def _clean_state():
    reranker_mod.reset_reranker_state()
    reranker_warmup._warmup_thread = None
    yield
    running = reranker_warmup._warmup_thread
    if running is not None:
        running.join(timeout=10)
    reranker_warmup._warmup_thread = None
    reranker_mod.reset_reranker_state()


class _FakeEncoder:
    """Scores a pair by the length of its passage, so the order is predictable."""

    def predict(self, pairs):
        return [float(len(passage)) for _query, passage in pairs]


class _Loader:
    """A CrossEncoder stand-in that counts constructions and can be held open."""

    def __init__(self, outcomes=None, *, gate: threading.Event | None = None):
        self.outcomes = outcomes or [_FakeEncoder()]
        self.gate = gate
        self.calls = 0
        self.entered = threading.Event()
        self.models: list[str] = []

    def __call__(self, model_name, *args, **kwargs):
        self.models.append(model_name)
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=10), "test never released the loader"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _ObservedLock:
    """``reranker._lock`` stand-in that reports when a second thread is waiting on it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.second_caller_waiting = threading.Event()

    def __enter__(self):
        if self._lock.locked():
            self.second_caller_waiting.set()
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def _install(monkeypatch, loader: _Loader) -> _Loader:
    stub = types.ModuleType("sentence_transformers")
    stub.CrossEncoder = loader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)
    return loader


class _Hit:
    def __init__(self, content: str):
        self.content = content
        self.score = 0.0
        self.language = "en"


def _factory(db_session):
    @contextlib.contextmanager
    def _scope():
        yield db_session

    return _scope


def test_a_chat_arriving_mid_warmup_waits_for_the_one_load(monkeypatch):
    """The warm-up and a concurrent first chat must load the weights exactly once."""
    gate = threading.Event()
    loader = _install(monkeypatch, _Loader(gate=gate))
    lock = _ObservedLock()
    monkeypatch.setattr(reranker_mod, "_lock", lock)

    warm_result: dict[str, bool] = {}
    warm = threading.Thread(target=lambda: warm_result.update(ok=reranker_warmup.warm_reranker()))
    warm.start()
    assert loader.entered.wait(timeout=10), "warm-up never started loading"

    chat_result: dict[str, list] = {}
    hits = [_Hit("short"), _Hit("a much longer passage"), _Hit("mid length")]
    chat = threading.Thread(
        target=lambda: chat_result.update(order=reranker_mod.rerank("q", hits)), daemon=True
    )
    chat.start()
    assert lock.second_caller_waiting.wait(timeout=10), "the chat never reached the load lock"
    assert chat.is_alive(), "the chat must wait for the in-flight load, not skip reranking"

    gate.set()
    warm.join(timeout=10)
    chat.join(timeout=10)

    assert loader.calls == 1, "warm-up and first chat must share one load"
    assert loader.models == [C.CHAT_RERANKER_MODEL], "only the default model is warmed"
    assert warm_result["ok"] is True
    # The chat was actually reranked by the model the warm-up loaded.
    assert [h.content for h in chat_result["order"]] == [
        "a much longer passage",
        "mid length",
        "short",
    ]


def test_a_failed_warmup_leaves_the_next_request_free_to_load(monkeypatch):
    """A failed warm-up must not start the cooldown that would skip reranking for 5 min."""
    encoder = _FakeEncoder()
    loader = _install(monkeypatch, _Loader([RuntimeError("cache not mounted yet"), encoder]))

    assert reranker_warmup.warm_reranker() is False
    assert reranker_mod.get_reranker() is encoder
    assert loader.calls == 2


def test_control_a_failed_request_load_still_starts_the_cooldown(monkeypatch):
    """Control: the request path keeps its cooldown, so the test above is not vacuous."""
    loader = _install(monkeypatch, _Loader([RuntimeError("no model"), _FakeEncoder()]))

    assert reranker_mod.get_reranker() is None
    assert reranker_mod.get_reranker() is None
    assert loader.calls == 1


def test_a_failing_probe_still_counts_as_warmed(monkeypatch):
    class _BrokenPredict:
        def predict(self, pairs):
            raise RuntimeError("probe failed")

    model = _BrokenPredict()
    _install(monkeypatch, _Loader([model]))

    assert reranker_warmup.warm_reranker() is True
    assert reranker_mod.get_reranker() is model


def test_warmup_is_wanted_on_default_settings(db_session):
    """Control for the two skips below: no rows means warm."""
    assert reranker_warmup.warmup_wanted(_factory(db_session)) is True


def test_warmup_skipped_when_admin_turned_reranking_off(monkeypatch, db_session):
    loader = _install(monkeypatch, _Loader())
    set_setting(db_session, "chat.rag.rerank_enabled", "false")

    assert reranker_warmup.warmup_wanted(_factory(db_session)) is False
    reranker_warmup._warm_if_wanted(_factory(db_session))
    assert loader.calls == 0


def test_warmup_skipped_when_opted_out(monkeypatch, db_session):
    loader = _install(monkeypatch, _Loader())
    set_setting(db_session, reranker_warmup.WARMUP_SETTING_KEY, "false")

    assert reranker_warmup.warmup_wanted(_factory(db_session)) is False
    reranker_warmup._warm_if_wanted(_factory(db_session))
    assert loader.calls == 0


def test_unreadable_settings_skip_the_warmup_without_raising(monkeypatch):
    loader = _install(monkeypatch, _Loader())

    @contextlib.contextmanager
    def _broken():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    assert reranker_warmup.warmup_wanted(_broken) is False
    reranker_warmup._warm_if_wanted(_broken)
    assert loader.calls == 0


def test_starting_the_warmup_does_not_wait_for_the_load(monkeypatch, db_session):
    gate = threading.Event()
    loader = _install(monkeypatch, _Loader(gate=gate))

    started = time.perf_counter()
    thread = reranker_warmup.start_reranker_warmup(_factory(db_session))
    elapsed = time.perf_counter() - started

    assert loader.entered.wait(timeout=10), "the warm-up thread never reached the load"
    assert thread.is_alive(), "the load is still blocked, so the start must have returned"
    assert thread.daemon
    assert elapsed < 1.0
    # A second start while running returns the same thread rather than a second load.
    assert reranker_warmup.start_reranker_warmup(_factory(db_session)) is thread

    gate.set()
    thread.join(timeout=10)
    assert loader.calls == 1
    assert reranker_mod.get_reranker() is not None


def test_lifespan_in_test_mode_does_not_start_the_warmup(monkeypatch):
    """TESTING=true returns before every startup task, this one included."""
    from app import main

    calls: list[object] = []
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setattr(reranker_warmup, "start_reranker_warmup", lambda *a: calls.append(a))

    async def _run():
        async with main.lifespan(main.app):
            pass

    asyncio.run(_run())
    assert calls == []


def test_control_the_lifespan_hook_does_start_the_warmup(monkeypatch):
    """Control for the test above: the hook the lifespan calls reaches the starter."""
    from app import main

    calls: list[object] = []
    monkeypatch.setattr(reranker_warmup, "start_reranker_warmup", lambda *a: calls.append(a))

    main._start_reranker_warmup()
    assert len(calls) == 1


def test_only_the_api_lifespan_starts_the_warmup():
    """Celery workers import ``app.main`` but never run its lifespan.

    So the warm-up stays out of worker processes only as long as nothing else
    starts it. Fails if a second caller appears anywhere under ``app/``.
    """
    callers = sorted(
        str(path.relative_to(_APP_DIR))
        for path in _APP_DIR.rglob("*.py")
        if "start_reranker_warmup(" in path.read_text(encoding="utf-8")
        and path.name != "reranker_warmup.py"
    )
    assert callers == ["main.py"]
    source = (_APP_DIR / "main.py").read_text(encoding="utf-8")
    lifespan = source[source.index("async def lifespan(") :]
    assert lifespan.index('"Test mode: skipping startup tasks"') < lifespan.index(
        "_start_reranker_warmup()"
    )
