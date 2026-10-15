"""The Celery task that downloads a catalog model: routing, wiring, token and events (#1246).

The supervisor itself is covered in ``test_local_model_supervisor.py``; this module proves the
task hands it the right things (the saved token, the initiating admin, a store) and that its
progress reaches that admin's WebSocket as ``local_model_download``.
"""

from __future__ import annotations

import pytest
from fixtures.mock_hf_hub import default_repo

from app.core import local_whisper_catalog as catalog
from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_LOCAL_MODEL_DOWNLOAD
from app.core.constants import CeleryQueues
from app.services.local_models import cache
from app.services.local_models.state import DownloadStore
from app.tasks import local_model_download as task_module

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]


def _entry(model_id: str) -> catalog.LocalWhisperModel:
    entry = catalog.get(model_id)
    assert entry is not None
    return entry


TINY = _entry("tiny")
TOKEN = "hf_task_secret_token"
ADMIN_ID = 42


@pytest.fixture(autouse=True)
def _never_reach_the_real_hub(loopback_only):
    """Every test here runs behind the loopback-only network guard."""


@pytest.fixture
def wired(mock_hf_hub, local_model_redis, lm_namespace, monkeypatch):
    """The task bound to the mock hub and a namespaced store, recording sent events."""
    mock_hf_hub.add_repo(default_repo(TINY.repo_id))
    store = DownloadStore(local_model_redis, lm_namespace)
    sent: list[tuple] = []
    seen: dict = {}
    real_run = task_module.run_download

    def run_against_mock(entry, **kwargs):
        seen.update(kwargs)
        return real_run(entry, endpoint=mock_hf_hub.url, poll_seconds=0.05, **kwargs)

    monkeypatch.setattr(task_module, "DownloadStore", lambda _redis: store)
    monkeypatch.setattr(task_module, "get_redis", lambda: local_model_redis)
    monkeypatch.setattr(task_module, "run_download", run_against_mock)

    def record_event(*args) -> bool:
        sent.append(args)
        return True

    monkeypatch.setattr(task_module, "send_ws_event", record_event)
    monkeypatch.setattr("app.services.huggingface_token.get_hf_token", lambda: TOKEN)
    return store, sent, seen, mock_hf_hub


def test_the_task_is_registered_and_routed_to_the_download_queue():
    assert "asr.download_local_model" in celery_app.tasks
    route = celery_app.conf.task_routes["asr.download_local_model"]
    assert route == {"queue": CeleryQueues.DOWNLOAD}


def test_the_task_retries_nothing_on_its_own():
    t = task_module.download_local_model_task
    assert not getattr(t, "autoretry_for", ())
    assert t.acks_late is not True
    assert t.soft_time_limit and t.time_limit > t.soft_time_limit


def test_the_task_downloads_with_the_saved_token_and_reports_to_the_initiator(wired):
    store, sent, seen, hub = wired
    store.try_acquire(TINY.id, "task-xyz", ADMIN_ID)

    result = task_module.download_local_model_task.apply(
        args=[TINY.id, ADMIN_ID], task_id="task-xyz"
    ).get()

    assert result == {"model_id": TINY.id, "status": "completed"}
    assert seen["token"] == TOKEN
    assert seen["user_id"] == ADMIN_ID
    assert seen["task_id"] == "task-xyz"
    assert f"Bearer {TOKEN}" in hub.authorizations()
    assert cache.is_complete(TINY.repo_id) is True
    assert sent, "no progress event was sent"
    assert {(uid, kind) for uid, kind, _ in sent} == {
        (ADMIN_ID, NOTIFICATION_TYPE_LOCAL_MODEL_DOWNLOAD)
    }
    assert sent[-1][2]["status"] == "completed"
    assert TOKEN not in repr(sent)
    assert store.current_lock() is None


def test_an_unknown_model_fails_without_a_download_and_frees_the_lock(wired):
    store, sent, _seen, hub = wired
    store.try_acquire("someone/else", "task-bad", ADMIN_ID)

    result = task_module.download_local_model_task.apply(
        args=["someone/else", ADMIN_ID], task_id="task-bad"
    ).get()

    assert result == {"model_id": "someone/else", "status": "failed"}
    assert hub.requests == []
    assert store.current_lock() is None
    state = store.read_state()
    assert state is not None
    assert state["error_code"] == "unknown_local_model"
