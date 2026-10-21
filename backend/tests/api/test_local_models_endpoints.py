"""``/api/admin/local-models``: list, download, cancel (#1246).

The Redis lock/state is a real throwaway Redis (see ``fixtures.throwaway_redis``) under a
per-test namespace; the Celery boundary (``apply_async``, ``revoke``) is replaced with recorders,
and the assertions are on what was SENT. The hub cache is a temporary directory. Nothing here
starts a download or touches the network.
"""

from __future__ import annotations

import json

import pytest
from fixtures.hf_cache import make_complete_repo
from fixtures.hf_cache import make_partial_repo

from app.api.endpoints import local_models
from app.core import local_whisper_catalog as catalog
from app.core.config import settings
from app.services.local_models import cache
from app.services.local_models.state import DownloadStore
from app.tasks import local_model_download

URL = "/api/admin/local-models"
DOWNLOAD = f"{URL}/download"
CANCEL = f"{URL}/download/cancel"
TOKEN = "hf_" + "Z" * 34
GIB = 1024**3


def _entry(model_id: str) -> catalog.LocalWhisperModel:
    entry = catalog.get(model_id)
    assert entry is not None
    return entry


pytestmark = [pytest.mark.xdist_group("local_model_redis"), pytest.mark.usefixtures("pipeline_env")]


@pytest.fixture
def store(local_model_redis, lm_namespace, monkeypatch):
    s = DownloadStore(local_model_redis, lm_namespace)
    monkeypatch.setattr(local_models, "_store", lambda: s)
    return s


@pytest.fixture
def dispatched(monkeypatch):
    sent: list[dict] = []

    def apply_async(args=None, kwargs=None, **options):
        sent.append({"args": args, "options": options})

    monkeypatch.setattr(local_model_download.download_local_model_task, "apply_async", apply_async)
    return sent


@pytest.fixture
def revoked(monkeypatch):
    from app.core.celery import celery_app

    calls: list[tuple] = []
    monkeypatch.setattr(
        celery_app.control, "revoke", lambda task_id, **kw: calls.append((task_id, kw))
    )
    return calls


@pytest.fixture
def audit_events(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(local_models.audit_logger, "log", lambda **kw: captured.append(kw))
    return captured


@pytest.fixture(autouse=True)
def _roomy_disk_and_active_model(hf_cache, monkeypatch):
    monkeypatch.setattr(cache, "free_bytes", lambda: 500 * GIB)
    monkeypatch.setattr(local_models, "_active_model", lambda: "base")


def _post(client, headers, model_id="tiny"):
    return client.post(DOWNLOAD, json={"model_id": model_id}, headers=headers)


class TestWhoMayCall:
    @pytest.mark.parametrize(
        ("method", "path", "kwargs"),
        [
            ("get", URL, {}),
            ("post", DOWNLOAD, {"json": {"model_id": "tiny"}}),
            ("post", CANCEL, {}),
        ],
    )
    def test_a_plain_admin_and_a_user_are_refused(
        self, client, admin_token_headers, user_token_headers, store, method, path, kwargs
    ):
        for headers in (admin_token_headers, user_token_headers):
            assert getattr(client, method)(path, headers=headers, **kwargs).status_code == 403

    @pytest.mark.parametrize(
        ("method", "path", "kwargs"),
        [
            ("get", URL, {}),
            ("post", DOWNLOAD, {"json": {"model_id": "tiny"}}),
            ("post", CANCEL, {}),
        ],
    )
    def test_anonymous_is_refused(self, client, store, method, path, kwargs):
        assert getattr(client, method)(path, **kwargs).status_code == 401

    def test_a_refused_caller_starts_nothing(self, client, admin_token_headers, store, dispatched):
        _post(client, admin_token_headers)

        assert dispatched == []
        assert store.current_lock() is None

    @pytest.mark.parametrize(
        ("method", "path", "kwargs"),
        [
            ("get", URL, {}),
            ("post", DOWNLOAD, {"json": {"model_id": "tiny"}}),
            ("post", CANCEL, {}),
        ],
    )
    def test_lite_has_no_local_models(
        self, client, super_admin_token_headers, store, pipeline_env, method, path, kwargs
    ):
        pipeline_env("lite")

        r = getattr(client, method)(path, headers=super_admin_token_headers, **kwargs)

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "local_asr_unavailable"


class TestCatalog:
    def test_lists_every_catalog_model_with_its_disk_state(
        self, client, super_admin_token_headers, store, hf_cache
    ):
        make_complete_repo(hf_cache, _entry("tiny").repo_id)
        make_partial_repo(hf_cache, _entry("small").repo_id, incomplete_bytes=300)

        r = client.get(URL, headers=super_admin_token_headers)

        assert r.status_code == 200
        body = r.json()
        by_id = {m["id"]: m for m in body["models"]}
        assert list(by_id) == [m.id for m in catalog.LOCAL_WHISPER_MODELS]
        assert (by_id["tiny"]["downloaded"], by_id["tiny"]["partial"]) == (True, False)
        assert (by_id["small"]["downloaded"], by_id["small"]["partial"]) == (False, True)
        assert (by_id["large-v3"]["downloaded"], by_id["large-v3"]["partial"]) == (False, False)
        assert by_id["tiny"]["present_bytes"] > 0
        assert by_id["base"]["is_active"] is True
        assert [m["id"] for m in body["models"] if m["is_active"]] == ["base"]
        assert body["active_model"] == "base"
        assert body["free_bytes"] == 500 * GIB
        assert body["cache_writable"] is True
        assert body["download"] is None

    def test_reports_the_download_in_flight(self, client, super_admin_token_headers, store):
        store.try_acquire("tiny", "t-1", 1)
        store.write_state(
            replace=True, model_id="tiny", status="downloading", bytes_done=40, bytes_total=100,
            percent=40.0, task_id="t-1", user_id=1,
        )  # fmt: skip

        download = client.get(URL, headers=super_admin_token_headers).json()["download"]

        assert download["status"] == "downloading"
        assert (download["bytes_done"], download["bytes_total"], download["percent"]) == (
            40,
            100,
            40.0,
        )
        assert "task_id" not in download
        assert "user_id" not in download

    def test_an_active_state_whose_lock_expired_reads_as_failed(
        self, client, super_admin_token_headers, store
    ):
        store.write_state(replace=True, model_id="tiny", status="downloading", bytes_done=1)

        download = client.get(URL, headers=super_admin_token_headers).json()["download"]

        assert (download["status"], download["error_code"]) == ("failed", "download_failed")

    def test_a_dead_redis_does_not_break_the_list(
        self, client, super_admin_token_headers, monkeypatch
    ):
        def boom():
            raise ConnectionError("redis is down")

        monkeypatch.setattr(local_models, "_store", boom)

        r = client.get(URL, headers=super_admin_token_headers)

        assert r.status_code == 200
        assert r.json()["download"] is None


class TestStartDownload:
    def test_accepts_a_catalog_model_and_dispatches_it_once(
        self, client, super_admin_token_headers, store, dispatched, audit_events, super_admin_user
    ):
        r = _post(client, super_admin_token_headers)

        assert r.status_code == 202
        body = r.json()
        assert (body["model_id"], body["status"]) == ("tiny", "queued")
        lock = store.current_lock()
        assert lock is not None
        assert lock["model_id"] == "tiny"
        assert len(dispatched) == 1
        assert dispatched[0]["args"] == ["tiny", super_admin_user.id]
        assert dispatched[0]["options"]["task_id"] == lock["task_id"]
        assert [(e["details"]["action"], e["details"]["model"]) for e in audit_events] == [
            ("download_started", "tiny")
        ]

    def test_a_catalog_alias_resolves_to_its_id(
        self, client, super_admin_token_headers, store, dispatched
    ):
        r = _post(client, super_admin_token_headers, "crisperwhisper")

        assert r.status_code == 202
        assert dispatched[0]["args"][0] == "nyrahealth/faster_CrisperWhisper"

    @pytest.mark.parametrize("model_id", ["someone/else-ct2", "distil-large-v3", "", "../etc"])
    def test_refuses_anything_outside_the_catalog(
        self, client, super_admin_token_headers, store, dispatched, model_id
    ):
        r = _post(client, super_admin_token_headers, model_id)

        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "unknown_local_model"
        assert dispatched == []
        assert store.current_lock() is None

    def test_a_second_start_while_one_runs_is_refused(
        self, client, super_admin_token_headers, store, dispatched
    ):
        assert _post(client, super_admin_token_headers, "tiny").status_code == 202

        r = _post(client, super_admin_token_headers, "base")

        assert r.status_code == 409
        detail = r.json()["detail"]
        assert (detail["code"], detail["model_id"]) == ("model_download_in_progress", "tiny")
        assert len(dispatched) == 1

    def test_a_downloaded_model_is_refused(
        self, client, super_admin_token_headers, store, dispatched, hf_cache
    ):
        make_complete_repo(hf_cache, _entry("tiny").repo_id)

        r = _post(client, super_admin_token_headers)

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "local_model_already_downloaded"
        assert dispatched == []

    def test_too_little_disk_is_refused_with_the_numbers(
        self, client, super_admin_token_headers, store, dispatched, monkeypatch
    ):
        monkeypatch.setattr(cache, "free_bytes", lambda: 2048)

        r = _post(client, super_admin_token_headers, "large-v3")

        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "insufficient_disk_space"
        assert detail["free_bytes"] == 2048
        assert detail["required_bytes"] >= _entry("large-v3").approx_bytes
        assert dispatched == []
        assert store.current_lock() is None

    def test_bytes_already_on_disk_count_towards_the_requirement(
        self, client, super_admin_token_headers, store, dispatched, hf_cache, monkeypatch
    ):
        entry = _entry("tiny")
        make_partial_repo(hf_cache, entry.repo_id)
        blobs = cache.repo_dir(entry.repo_id) / "blobs"
        (blobs / "etag-done").write_bytes(b"x" * 4096)
        monkeypatch.setattr(cache, "free_bytes", lambda: entry.approx_bytes + GIB - 4096)

        r = _post(client, super_admin_token_headers)

        assert r.status_code == 202

    def test_an_unwritable_cache_is_refused(
        self, client, super_admin_token_headers, store, dispatched, monkeypatch
    ):
        monkeypatch.setattr(cache, "cache_writable", lambda: False)

        r = _post(client, super_admin_token_headers)

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "cache_not_writable"
        assert dispatched == []

    def test_an_unreachable_lock_store_fails_closed(
        self, client, super_admin_token_headers, dispatched, monkeypatch
    ):
        def boom():
            raise ConnectionError("redis is down")

        monkeypatch.setattr(local_models, "_store", boom)

        r = _post(client, super_admin_token_headers)

        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "model_download_unavailable"
        assert dispatched == []

    def test_a_dispatch_failure_releases_the_lock(
        self, client, super_admin_token_headers, store, monkeypatch
    ):
        def broken(args=None, kwargs=None, **options):
            raise RuntimeError("broker is down")

        monkeypatch.setattr(local_model_download.download_local_model_task, "apply_async", broken)

        r = _post(client, super_admin_token_headers)

        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "model_download_unavailable"
        assert store.current_lock() is None

    def test_no_response_carries_the_token(
        self, client, super_admin_token_headers, store, dispatched, revoked, audit_events,
        monkeypatch,
    ):  # fmt: skip
        monkeypatch.setattr(settings, "HUGGINGFACE_TOKEN", TOKEN)
        monkeypatch.setenv("HF_TOKEN", TOKEN)

        bodies = [
            client.get(URL, headers=super_admin_token_headers).text,
            _post(client, super_admin_token_headers).text,
            _post(client, super_admin_token_headers, "base").text,
            client.post(CANCEL, headers=super_admin_token_headers).text,
        ]

        assert all(TOKEN not in b for b in bodies)
        assert TOKEN not in json.dumps(audit_events, default=str)
        assert TOKEN not in json.dumps(dispatched, default=str)


class TestCancel:
    def test_with_nothing_running_it_is_a_404(self, client, super_admin_token_headers, store):
        r = client.post(CANCEL, headers=super_admin_token_headers)

        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "no_model_download"

    def test_a_queued_download_is_revoked_and_settled_here(
        self, client, super_admin_token_headers, store, dispatched, revoked, audit_events
    ):
        _post(client, super_admin_token_headers)
        task_id = store.current_lock()["task_id"]

        r = client.post(CANCEL, headers=super_admin_token_headers)

        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        assert revoked == [(task_id, {})]  # no terminate: nothing is running to terminate
        assert store.cancel_requested(task_id) is True
        assert store.current_lock() is None
        state = store.read_state()
        assert state is not None
        assert state["status"] == "cancelled"
        assert audit_events[-1]["details"]["action"] == "download_cancelled"
        # The slot is free again.
        assert _post(client, super_admin_token_headers, "base").status_code == 202

    def test_a_running_download_is_asked_to_stop_and_keeps_its_lock(
        self, client, super_admin_token_headers, store, revoked
    ):
        store.try_acquire("tiny", "t-run", 1)
        store.write_state(replace=True, model_id="tiny", status="downloading", task_id="t-run")

        r = client.post(CANCEL, headers=super_admin_token_headers)

        assert r.status_code == 200
        assert r.json()["status"] == "downloading"  # the task confirms with its own event
        assert store.cancel_requested("t-run") is True
        assert revoked == []
        assert store.current_lock() is not None  # the task releases it after cleaning up
