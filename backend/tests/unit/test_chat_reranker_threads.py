"""The reranker's torch thread cap is scoped to its scoring call (issue #1270).

``CrossEncoder.predict`` ran on the request thread at torch's default intra-op thread
count, which oversubscribes a busy host. ``chat/reranker_threads.py`` runs it on a small
pool whose threads latch the deployment's cap (``CHAT_RERANKER_TORCH_THREADS``) and then
restores the process default from a helper thread.

Almost every test drives the real ``rerank``/``score_pairs`` code and replaces only
``torch`` (a recording stand-in in ``sys.modules``) and the loaded model, so the process's
real torch thread default is never touched. The one test that does use real torch runs in
a subprocess, because ``set_num_threads`` is process-wide and the suite shares a process.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from app.core import constants as C  # noqa: N812
from app.core.config import settings
from app.services.chat import reranker as reranker_mod

_BACKEND = Path(__file__).resolve().parents[2]
_PREFIX = "chat-rerank-score"
_PROCESS_DEFAULT = 24


class _FakeTorch:
    """Records ``set_num_threads`` per thread and mimics torch's latch semantics.

    A thread that has set or read a count keeps it; an untouched thread sees the process
    default, which ``set_num_threads`` from any thread rewrites. That is exactly the
    property (measured with real torch by ``probe_torch_thread_scope.py``) the pool relies
    on, so a bug in the order of latch and restore shows up here.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.default = _PROCESS_DEFAULT
        self.calls: list[tuple[str, int]] = []
        self.fail = fail
        self._latched: dict[threading.Thread, int] = {}

    def set_num_threads(self, n: int) -> None:
        if self.fail:
            raise RuntimeError("torch refused")
        self.calls.append((threading.current_thread().name, n))
        self.default = n
        self._latched[threading.current_thread()] = n

    def get_num_threads(self) -> int:
        return self._latched.setdefault(threading.current_thread(), self.default)


class _RecordingModel:
    """Scores a pair by passage length and records where ``predict`` ran."""

    def __init__(self, hold: threading.Event | None = None) -> None:
        self.threads: list[str] = []
        self.element_types: set[type] = set()
        self.hold = hold
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def predict(self, pairs):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.threads.append(threading.current_thread().name)
            for pair in pairs:
                assert isinstance(pair, tuple)
                self.element_types.update(type(item) for item in pair)
            if self.hold is not None:
                assert self.hold.wait(timeout=30), "the test never released predict"
            return [float(len(passage)) for _query, passage in pairs]
        finally:
            with self._lock:
                self.active -= 1


class _Hit:
    def __init__(self, content: str) -> None:
        self.content = content
        self.score = 0.0
        self.language = "en"


def _hits(*contents: str) -> list[_Hit]:
    return [_Hit(c) for c in contents]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for var in ("OMP_NUM_THREADS", "TORCH_NUM_THREADS"):
        monkeypatch.delenv(var, raising=False)
    reranker_mod.reset_reranker_state()
    yield
    reranker_mod.reset_reranker_state()


@pytest.fixture
def fake_torch(monkeypatch):
    fake = _FakeTorch()
    monkeypatch.setitem(sys.modules, "torch", fake)
    return fake


def _configure(monkeypatch, value: str) -> None:
    # Pydantic v2 keeps field values in __dict__; setting it there works whether or not the
    # field exists, so this test file fails on an assertion, not an AttributeError, on a tree
    # without the setting.
    monkeypatch.setitem(settings.__dict__, "CHAT_RERANKER_TORCH_THREADS", value)


@pytest.fixture
def model():
    loaded = _RecordingModel()
    reranker_mod._rerankers[C.CHAT_RERANKER_MODEL] = loaded
    return loaded


def test_rerank_scores_on_the_capped_scoring_thread(monkeypatch, fake_torch, model):
    _configure(monkeypatch, "3")

    reranker_mod.rerank("q", _hits("a", "bbb", "cc"))

    assert model.threads, "the model was never asked to score"
    assert all(name.startswith(_PREFIX) for name in model.threads), model.threads


def test_scoring_threads_latch_the_resolved_cap(monkeypatch, fake_torch, model):
    _configure(monkeypatch, "3")

    reranker_mod.rerank("q", _hits("a", "bbb"))

    scoring = [(name, n) for name, n in fake_torch.calls if name.startswith(_PREFIX)]
    assert scoring, "no scoring thread ever set a cap"
    assert len({name for name, _ in scoring}) == C.CHAT_RERANKER_SCORING_WORKERS
    assert {n for _, n in scoring} == {3}


def test_process_default_is_restored_from_a_non_scoring_thread(monkeypatch, fake_torch, model):
    _configure(monkeypatch, "3")

    reranker_mod.rerank("q", _hits("a", "bbb"))

    assert fake_torch.calls, "set_num_threads was never called"
    last_thread, last_value = fake_torch.calls[-1]
    assert last_value == _PROCESS_DEFAULT
    assert not last_thread.startswith(_PREFIX), "the restore must not run on a scoring thread"
    assert fake_torch.default == _PROCESS_DEFAULT
    # A thread nothing has latched sees the default again, and a scoring thread still has the cap.
    seen: dict[str, int] = {}
    fresh = threading.Thread(target=lambda: seen.update(n=fake_torch.get_num_threads()))
    fresh.start()
    fresh.join()
    assert seen["n"] == _PROCESS_DEFAULT


def test_explicit_omp_num_threads_skips_auto_but_not_an_explicit_integer(
    monkeypatch, fake_torch, model
):
    from app.services.chat import reranker_threads

    monkeypatch.setenv("OMP_NUM_THREADS", "5")

    _configure(monkeypatch, "auto")
    assert reranker_threads.resolve_thread_cap().threads is None
    reranker_mod.rerank("q", _hits("a", "bbb"))
    assert model.threads == ["MainThread"], "auto must defer to the operator's torch env"
    assert fake_torch.calls == []

    reranker_mod.reset_reranker_state()
    reranker_mod._rerankers[C.CHAT_RERANKER_MODEL] = model
    model.threads.clear()
    _configure(monkeypatch, "2")
    assert reranker_threads.resolve_thread_cap().threads == 2
    reranker_mod.rerank("q", _hits("a", "bbb"))
    assert all(name.startswith(_PREFIX) for name in model.threads)


@pytest.mark.parametrize(("cpus", "expected"), [(48, "ceiling"), (8, 4), (4, 2), (2, 1), (1, 1)])
def test_auto_cap_follows_the_cpu_budget_up_to_the_ceiling(monkeypatch, cpus, expected):
    from app.services.chat import reranker_threads

    monkeypatch.setattr(reranker_threads, "effective_cpu_count", lambda: cpus)
    want = C.CHAT_RERANKER_TORCH_THREADS_AUTO_MAX if expected == "ceiling" else expected

    cap = reranker_threads.resolve_thread_cap("auto")

    assert (cap.threads, cap.source) == (want, "auto")


def test_an_explicit_integer_is_used_as_given(monkeypatch):
    from app.services.chat import reranker_threads

    monkeypatch.setattr(reranker_threads, "effective_cpu_count", lambda: 48)

    cap = reranker_threads.resolve_thread_cap("32")

    assert (cap.threads, cap.source) == (32, "configured")


@pytest.mark.parametrize("raw", ["0", "-2", "abc", "2.5", "all"])
def test_a_malformed_setting_is_refused_loudly(raw):
    from app.core.config import Settings
    from app.utils.cpu_budget import parse_thread_count_setting

    with pytest.raises(ValueError, match="positive integer"):
        parse_thread_count_setting(raw)
    with pytest.raises(ValueError, match="CHAT_RERANKER_TORCH_THREADS"):
        Settings(CHAT_RERANKER_TORCH_THREADS=raw)


@pytest.mark.parametrize(("raw", "expected"), [(" Auto ", "auto"), ("6", "6"), (" 12 ", "12")])
def test_a_valid_setting_is_normalized(raw, expected):
    from app.core.config import Settings

    normalized = Settings(CHAT_RERANKER_TORCH_THREADS=raw).CHAT_RERANKER_TORCH_THREADS
    assert normalized == expected


def test_a_blank_env_var_means_auto(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("CHAT_RERANKER_TORCH_THREADS", "")

    assert Settings().CHAT_RERANKER_TORCH_THREADS == "auto"


def test_a_broken_pool_still_reranks_inline(monkeypatch, caplog, model):
    broken = _FakeTorch(fail=True)
    monkeypatch.setitem(sys.modules, "torch", broken)
    _configure(monkeypatch, "3")
    caplog.set_level(logging.WARNING)

    first = reranker_mod.rerank("q", _hits("a", "bbb", "cc"))
    second = reranker_mod.rerank("q", _hits("a", "bbb", "cc"))

    assert [h.content for h in first] == ["bbb", "cc", "a"]
    assert [h.content for h in second] == ["bbb", "cc", "a"]
    assert model.threads == ["MainThread", "MainThread"]
    unavailable = [r for r in caplog.records if "scoring pool unavailable" in r.getMessage()]
    assert len(unavailable) == 1, "the failure is reported once, not on every turn"


def test_a_missing_torch_still_reranks_inline(monkeypatch, model):
    monkeypatch.setitem(sys.modules, "torch", None)  # `import torch` raises ImportError
    _configure(monkeypatch, "3")

    ranked = reranker_mod.rerank("q", _hits("a", "bbb", "cc"))

    assert [h.content for h in ranked] == ["bbb", "cc", "a"]
    assert model.threads == ["MainThread"]


def test_warmup_starts_the_scoring_pool(monkeypatch, fake_torch):
    from app.services.chat import reranker_warmup

    _configure(monkeypatch, "3")
    loaded = _RecordingModel()
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(CrossEncoder=lambda *_a, **_k: loaded),
    )

    assert reranker_warmup.warm_reranker() is True

    assert loaded.threads and all(n.startswith(_PREFIX) for n in loaded.threads)
    assert {n for name, n in fake_torch.calls if name.startswith(_PREFIX)} == {3}


def test_scoring_pool_never_receives_hit_objects(monkeypatch, fake_torch, model):
    _configure(monkeypatch, "3")

    reranker_mod.rerank("q", _hits("a", "bbb"))

    assert model.threads and all(n.startswith(_PREFIX) for n in model.threads)
    assert model.element_types == {str}


def _wait_until(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_at_most_the_scoring_workers_run_at_once(monkeypatch, fake_torch):
    release = threading.Event()
    slow = _RecordingModel(hold=release)
    reranker_mod._rerankers[C.CHAT_RERANKER_MODEL] = slow
    _configure(monkeypatch, "3")
    callers_total = C.CHAT_RERANKER_SCORING_WORKERS * 3
    arrived = []

    def caller() -> None:
        arrived.append(1)
        reranker_mod.rerank("q", _hits("a", "bbb"))

    callers = [threading.Thread(target=caller) for _ in range(callers_total)]
    for t in callers:
        t.start()
    try:
        # Every caller is in flight and the predicts that can run are blocked on `release`:
        # an unbounded implementation has all of them inside predict by now, a bounded one
        # only the scoring workers.
        assert _wait_until(lambda: len(arrived) == callers_total)
        assert _wait_until(lambda: slow.active >= C.CHAT_RERANKER_SCORING_WORKERS)
    finally:
        release.set()
        for t in callers:
            t.join(timeout=30)

    assert slow.max_active == C.CHAT_RERANKER_SCORING_WORKERS


def test_rerank_order_matches_score_order(model):
    """Control: passes before and after, and pins that scoring moved without re-sorting."""
    ranked = reranker_mod.rerank("q", _hits("a", "bbbb", "cc", "ddd"))

    assert [h.content for h in ranked] == ["bbbb", "ddd", "cc", "a"]
    assert [h.score for h in ranked] == [4.0, 3.0, 2.0, 1.0]


def test_the_admin_status_reports_the_effective_cap(monkeypatch):
    from app.services.chat import reranker_threads

    monkeypatch.setattr(reranker_threads, "effective_cpu_count", lambda: 16)
    _configure(monkeypatch, "auto")
    assert reranker_threads.describe_thread_cap() == {
        "threads": 8,
        "source": "auto",
        "setting": "auto",
    }
    _configure(monkeypatch, "3")
    assert reranker_threads.describe_thread_cap() == {
        "threads": 3,
        "source": "configured",
        "setting": "3",
    }
    monkeypatch.setenv("TORCH_NUM_THREADS", "4")
    _configure(monkeypatch, "auto")
    assert reranker_threads.describe_thread_cap() == {
        "threads": None,
        "source": "torch_env",
        "setting": "auto",
    }


# ---------------------------------------------------------------------------
# The benchmark's pre-registered choice rule and its fixture.
# ---------------------------------------------------------------------------


def test_pick_cap_takes_the_smallest_n_inside_both_tolerances():
    from app.scripts.benchmark_reranker_threads import pick_cap

    idle = {2: 3.0, 4: 1.8, 8: 1.27, 16: 1.26}
    loaded = {2: 8.6, 4: 4.6, 8: 4.2, 16: 5.3}

    # 4 is inside the loaded 10% band (4.6 <= 4.62) but 1.8 > 1.26 * 1.25 idle, so 8.
    assert pick_cap(idle, loaded) == 8


def test_pick_cap_prefers_a_smaller_n_when_it_qualifies_on_both():
    from app.scripts.benchmark_reranker_threads import pick_cap

    assert pick_cap({2: 1.0, 4: 0.9}, {2: 2.0, 4: 1.95}) == 2


def test_pick_cap_reports_none_when_no_n_satisfies_both():
    from app.scripts.benchmark_reranker_threads import pick_cap

    # N=2 is best idle but far off loaded, N=4 the reverse.
    assert pick_cap({2: 1.0, 4: 5.0}, {2: 5.0, 4: 1.0}) is None


def test_synthetic_passages_are_deterministic_and_sized():
    from app.scripts.benchmark_reranker_threads import synthetic_passages

    first = synthetic_passages(5)

    assert first == synthetic_passages(5)
    assert len(set(first)) == 5
    assert all(len(p.split()) == 150 for p in first)


# ---------------------------------------------------------------------------
# Real torch, in a subprocess (set_num_threads is process-wide).
# ---------------------------------------------------------------------------

_REAL_POOL_SCRIPT = """
import json, sys, threading
import torch
from app.services.chat import reranker_threads

class Model:
    def predict(self, pairs):
        return [(threading.current_thread().name, torch.get_num_threads())]

box = {}
latched = threading.Event(); release = threading.Event()
def pre_latched():
    box["pre"] = torch.get_num_threads()   # latched BEFORE the pool exists
    latched.set(); release.wait(60)
    box["pre_after"] = torch.get_num_threads()
t = threading.Thread(target=pre_latched); t.start(); latched.wait(60)

scored = reranker_threads.score_pairs(Model(), [("q", "p")])
fresh = threading.Thread(target=lambda: box.update(fresh=torch.get_num_threads()))
fresh.start(); fresh.join()
release.set(); t.join()
print(json.dumps({"scored": scored, **box}))
"""


def _subprocess_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("OMP_NUM_THREADS", "TORCH_NUM_THREADS")}
    env.update(
        TEMP_DIR=str(tmp_path / "t"),
        DATA_DIR=str(tmp_path / "d"),
        MODELS_DIR=str(tmp_path / "m"),
        CUDA_VISIBLE_DEVICES="",
        PYTHONPATH=str(_BACKEND),
        **extra,
    )
    return env


def test_real_torch_cap_is_thread_scoped(tmp_path):
    pytest.importorskip("torch", reason="needs real torch; requirements-ci.txt installs it")
    cap = 2
    probe = subprocess.run(
        [sys.executable, "-m", "app.scripts.probe_torch_thread_scope", "--cap", str(cap)],
        capture_output=True,
        text=True,
        cwd=_BACKEND,
        env=_subprocess_env(tmp_path),
        timeout=600,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr[-1500:]
    verdicts = json.loads(probe.stdout)["verdicts"]
    assert all(verdicts.values()), verdicts

    pool = subprocess.run(
        [sys.executable, "-c", _REAL_POOL_SCRIPT],
        capture_output=True,
        text=True,
        cwd=_BACKEND,
        env=_subprocess_env(tmp_path, CHAT_RERANKER_TORCH_THREADS=str(cap)),
        timeout=600,
        check=False,
    )
    assert pool.returncode == 0, pool.stderr[-1500:]
    seen = json.loads(pool.stdout.strip().splitlines()[-1])
    name, scoring_threads = seen["scored"][0]
    assert name.startswith(_PREFIX)
    assert scoring_threads == cap, "predict must run at the cap"
    assert seen["pre"] == seen["pre_after"] != cap, "a pre-latched thread keeps its own count"
    assert seen["fresh"] == seen["pre"], "a thread started after the pool sees the default"
