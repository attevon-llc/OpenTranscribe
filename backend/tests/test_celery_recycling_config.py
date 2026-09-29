"""Worker recycling is app config, env-driven (issue #1070)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
PROBE = (
    "import json; from app.core.celery import celery_app as a; "
    "print(json.dumps([a.conf.worker_max_tasks_per_child, a.conf.worker_max_memory_per_child]))"
)


def _conf(**env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("CELERY_WORKER_MAX")}
    base.update(SKIP_CELERY="true", **env)
    out = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=BACKEND, env=base, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_defaults_unset():
    assert _conf() == [None, None]


def test_values_from_env():
    got = _conf(
        CELERY_WORKER_MAX_TASKS_PER_CHILD="25", CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB="1500000"
    )
    assert got == [25, 1500000]


def test_garbage_falls_back_to_unset():
    assert _conf(CELERY_WORKER_MAX_TASKS_PER_CHILD="lots") == [None, None]


@pytest.fixture
def threads_worker(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["celery", "worker", "--pool=threads"])


def test_threads_pool_warns_for_memory_and_tasks(threads_worker, monkeypatch, caplog):
    from app.core import celery as c

    monkeypatch.setitem(c.celery_app.conf, "worker_max_memory_per_child", 1500000)
    monkeypatch.setitem(c.celery_app.conf, "worker_max_tasks_per_child", 20)
    with caplog.at_level("WARNING"):
        c.warn_inert_max_tasks_per_child()
    text = caplog.text
    assert "max-memory-per-child" in text
    assert "max-tasks-per-child=20" in text


def test_threads_pool_silent_when_unset(threads_worker, caplog):
    from app.core import celery as c

    with caplog.at_level("WARNING"):
        c.warn_inert_max_tasks_per_child()
    assert "max-" not in caplog.text
