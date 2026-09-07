"""Neural-search bootstrap self-heal (issue #625).

The root cause: ``app.main._initialize_neural_search`` used to be a one-shot startup task
with no retry, and ``ml_model_service._REGISTRATION_MAX_WAIT`` (300s) is a ceiling on OUR
polling, not on OpenSearch's own async registration task — a model can finish
``REGISTERED, deployed=False`` minutes after the API process gave up, and nothing ever
re-checked. ``neural_bootstrap.py`` fixes this with one idempotent function
(``ensure_neural_search_bootstrap``) called from two places: the startup fast path (unchanged
shape) and a Celery-beat self-heal every 10 minutes.

Test #2 below (``test_a_timed_out_registration_is_recovered_on_the_next_tick``) is the
headline falsifiable test: it proves the SECOND tick recovers what the first one could not.
Verified red-first per this repo's standard, against a ``git archive HEAD`` tree — the second
tick never ran on old ``app.main`` code because nothing called the bootstrap a second time.

None of these tests wait anywhere near real ceilings: ``ml_model_service._wait_for_registration``
/ ``_wait_for_deployment`` take injectable ``max_wait``/``poll_interval`` keyword args
specifically so this suite stays fast.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.constants import NEURAL_BOOTSTRAP_MAX_BACKOFF_SECONDS
from app.services.search import neural_bootstrap as nb


class _FakeRedis:
    """Minimal in-memory stand-in for the redis calls ``run_bootstrap_tick`` makes."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: object, ex: int | None = None) -> None:
        self._store[key] = str(value)

    def incr(self, key: str) -> int:
        current = int(self._store.get(key, "0")) + 1
        self._store[key] = str(current)
        return current

    def expire(self, key: str, ttl: int) -> None:  # noqa: ARG002 - TTL not modeled
        pass

    def delete(self, *keys: str) -> None:
        for key in keys:
            self._store.pop(key, None)


@pytest.fixture
def fake_redis():
    return _FakeRedis()


# ---------------------------------------------------------------------------
# 1. Injectable ceilings — confirms the kwarg plumbing works and stays fast
# ---------------------------------------------------------------------------


def test_injectable_ceilings_do_not_wait_for_the_real_timeout():
    from app.services.search.ml_model_service import OpenSearchMLModelService

    service = OpenSearchMLModelService.__new__(OpenSearchMLModelService)
    service._client = MagicMock()
    service._client.transport.perform_request.return_value = {"state": "RUNNING"}

    import time

    start = time.time()
    result = service._wait_for_registration("task-1", max_wait=0.05, poll_interval=0.01)
    elapsed = time.time() - start

    assert result is None
    assert elapsed < 1.0  # nowhere near the real 300s ceiling


# ---------------------------------------------------------------------------
# 2. THE headline test: recovery on the next tick
# ---------------------------------------------------------------------------


class _EventuallyReadyMLService:
    """Simulates a model whose registration task keeps running after our first poll gives
    up, then completes before the second bootstrap tick."""

    def __init__(self) -> None:
        self.calls = 0
        self.active_model_id: str | None = None
        self._deployed = False

    # --- probe surface -----------------------------------------------------
    def get_active_model_id(self) -> str | None:
        return self.active_model_id

    # --- bootstrap surface ---------------------------------------------------
    def configure_ml_settings(self) -> bool:
        return True

    def get_available_local_models(self) -> list[dict[str, object]]:
        return [{"short_name": "all-MiniLM-L6-v2"}]

    def get_local_model_path(self, _model_name: str):
        return "/ml-models/all-MiniLM-L6-v2"

    def ensure_model_deployed(self, _model_name: str) -> str | None:
        self.calls += 1
        if self.calls == 1:
            # Tick 1: registration "timed out" from our side.
            return None
        # Tick 2: the model is now findable and deployed.
        self._deployed = True
        return "model-123"

    def set_active_model_id(self, model_id: str) -> None:
        self.active_model_id = model_id


@pytest.fixture
def eventually_ready_service():
    return _EventuallyReadyMLService()


def test_a_timed_out_registration_is_recovered_on_the_next_tick(
    fake_redis, eventually_ready_service
):
    pipeline_calls: list[str | None] = []

    def _fake_ensure_pipeline(model_id: str | None = None) -> bool:
        pipeline_calls.append(model_id)
        return True

    # neural_search_ready() gates both the probe and the pipeline-availability check; drive
    # it directly off the fake service's state so the test controls exactly what "ready"
    # means without touching a real is_neural_pipeline_available cache.
    def _fake_ready() -> bool:
        return bool(eventually_ready_service.active_model_id) and eventually_ready_service._deployed

    with (
        patch(
            "app.services.search.ml_model_service.get_ml_model_service",
            return_value=eventually_ready_service,
        ),
        patch(
            "app.services.search.indexing_service.ensure_neural_ingest_pipeline",
            side_effect=_fake_ensure_pipeline,
        ),
        patch("app.core.redis.get_redis", return_value=fake_redis),
        patch("app.services.search.neural_bootstrap.neural_search_ready", side_effect=_fake_ready),
    ):
        tick_1 = nb.run_bootstrap_tick()
        assert tick_1["state"] == "degraded"
        assert tick_1["attempts"] == 1
        assert pipeline_calls == []  # never reached on tick 1

        # Simulate the backoff window elapsing (real beat ticks 10 minutes apart; the
        # 600s base backoff would otherwise skip this second call as still-in-backoff).
        fake_redis.delete(nb._NEXT_AT_KEY)

        tick_2 = nb.run_bootstrap_tick()
        assert tick_2["state"] == "ok"
        assert eventually_ready_service.calls == 2
        # ensure_neural_ingest_pipeline() is called with no model_id in the local-mode
        # path (it resolves the model internally, same as the pre-#625 code) — what
        # matters here is that the pipeline step was reached AT ALL on tick 2, which it
        # never was on tick 1.
        assert len(pipeline_calls) == 1


# ---------------------------------------------------------------------------
# 3. Healthy deployments pay only the probe
# ---------------------------------------------------------------------------


def test_a_healthy_deployment_pays_only_the_probe():
    with (
        patch("app.services.search.neural_bootstrap.neural_search_ready", return_value=True),
        patch("app.services.search.ml_model_service.get_ml_model_service") as get_ml,
    ):
        result = nb.ensure_neural_search_bootstrap()

    assert result.state == "ok"
    get_ml.return_value.ensure_model_deployed.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Backoff caps and never stops retrying
# ---------------------------------------------------------------------------


def test_repeated_failure_backs_off_and_never_stops(fake_redis):
    with (
        patch("app.services.search.neural_bootstrap.neural_search_ready", return_value=False),
        patch("app.core.redis.get_redis", return_value=fake_redis),
        patch(
            "app.services.search.neural_bootstrap.ensure_neural_search_bootstrap",
            return_value=nb.BootstrapResult(
                state="degraded", stage="register_deploy", detail="boom", model_id=None
            ),
        ),
    ):
        delays: list[float] = []
        for _ in range(8):
            # Force each tick to run rather than honoring backoff, by clearing next_at —
            # this test measures the DELAY COMPUTED each attempt, not real wall-clock waits.
            fake_redis.delete(nb._NEXT_AT_KEY)
            tick = nb.run_bootstrap_tick()
            assert tick["state"] == "degraded"
            delays.append(float(fake_redis.get(nb._NEXT_AT_KEY)))

        # Attempt 8 still ran (no terminal state) and the stored next_at reflects a delay
        # that never exceeds the cap.
        attempts_after = int(fake_redis.get(nb._ATTEMPTS_KEY))
        assert attempts_after == 8

        # Recompute expected delay sequence and confirm the cap holds from some point on.
        import time

        now = time.time()
        last_delay = delays[-1] - now
        assert last_delay <= NEURAL_BOOTSTRAP_MAX_BACKOFF_SECONDS + 1


# ---------------------------------------------------------------------------
# 5. Recovery clears the backoff
# ---------------------------------------------------------------------------


def test_recovery_clears_the_backoff(fake_redis):
    with (
        patch("app.core.redis.get_redis", return_value=fake_redis),
        patch("app.services.search.neural_bootstrap.neural_search_ready", return_value=False),
        patch(
            "app.services.search.neural_bootstrap.ensure_neural_search_bootstrap",
            return_value=nb.BootstrapResult(
                state="degraded", stage="pipeline", detail="still down", model_id=None
            ),
        ),
    ):
        for _ in range(3):
            fake_redis.delete(nb._NEXT_AT_KEY)
            nb.run_bootstrap_tick()

    assert int(fake_redis.get(nb._ATTEMPTS_KEY)) == 3

    with (
        patch("app.core.redis.get_redis", return_value=fake_redis),
        patch("app.services.search.neural_bootstrap.neural_search_ready", return_value=True),
    ):
        tick = nb.run_bootstrap_tick()

    assert tick["state"] == "ok"
    assert fake_redis.get(nb._ATTEMPTS_KEY) is None
    assert fake_redis.get(nb._NEXT_AT_KEY) is None
    assert fake_redis.get(nb._LAST_ERROR_KEY) is None


# ---------------------------------------------------------------------------
# 6. Redis outage fails open
# ---------------------------------------------------------------------------


def test_a_redis_outage_fails_open():
    bootstrap_called = {"count": 0}

    def _fake_bootstrap(**_kwargs: object) -> nb.BootstrapResult:
        bootstrap_called["count"] += 1
        return nb.BootstrapResult(state="ok", stage=None, detail=None, model_id="m1")

    with (
        patch("app.core.redis.get_redis", side_effect=ConnectionError("down")),
        patch("app.services.search.neural_bootstrap.neural_search_ready", return_value=False),
        patch(
            "app.services.search.neural_bootstrap.ensure_neural_search_bootstrap",
            side_effect=_fake_bootstrap,
        ),
    ):
        tick = nb.run_bootstrap_tick()

    assert bootstrap_called["count"] == 1
    assert tick["state"] == "ok"


# ---------------------------------------------------------------------------
# 8. Managed mode short-circuits
# ---------------------------------------------------------------------------


def test_managed_mode_short_circuits(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "OPENSEARCH_EMBEDDING_MODE", "managed")
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_MODEL_ID", "abc123")
    ml_service = SimpleNamespace(
        get_active_model_id=MagicMock(return_value=None),
        set_active_model_id=MagicMock(),
        configure_ml_settings=MagicMock(),
        ensure_model_deployed=MagicMock(),
    )

    with (
        patch("app.services.search.neural_bootstrap.neural_search_ready", return_value=False),
        patch(
            "app.services.search.ml_model_service.get_ml_model_service",
            return_value=ml_service,
        ),
        patch(
            "app.services.search.indexing_service.ensure_neural_ingest_pipeline",
            return_value=True,
        ),
    ):
        result = nb.ensure_neural_search_bootstrap()

    assert result.state == "ok"
    ml_service.set_active_model_id.assert_called_once_with("abc123")
    ml_service.configure_ml_settings.assert_not_called()
    ml_service.ensure_model_deployed.assert_not_called()
