"""Deploying a REGISTERING model destroys its own registration (issue #625 follow-up).

Root cause, found live in a v0.5.0 fresh-install rehearsal: issue #625 gave the neural-search
bootstrap TWO independent callers -- the one-shot startup path
(``app.main._initialize_neural_search``) and a beat self-heal every 10 minutes
(``app.tasks.search_maintenance_task.neural_search_bootstrap_task``). ``find_model_by_name``
matches a model the instant OpenSearch ML Commons creates its meta document (state
``REGISTERING``), long before it is deployable. ``ensure_model_deployed`` used to see
"registered but ``deployed`` is False" and call ``deploy_model`` unconditionally -- but a deploy
issued into a ``REGISTERING`` model does not just fail: OpenSearch's own deploy-failure cleanup
runs ``ModelHelper.deleteFileCache(modelId)``, which recursively deletes
``ml_cache/models_cache/register/<modelId>/`` -- the exact directory the OTHER caller's in-flight
download is writing into. That turned a registration that would have COMPLETED in ~12-18s into
``REGISTER_MODEL FAILED: <path>.zip (No such file or directory)`` in about 6 seconds -- byte
for byte the error the rehearsal hit, and no poll duration could have saved it, because there
was nothing left to poll for once the files were gone.

Verified red-first per this repo's standard: reverting ``ensure_model_deployed`` to call
``deploy_model`` unconditionally on any non-deployed match makes
``test_a_registering_model_is_never_deployed_into`` fail (it calls deploy) and
``test_the_race_reproduction_matches_the_live_failure_shape`` fail identically.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def service(monkeypatch):
    """The service with its transport recorded and cluster settings stubbed.

    Same shape as ``test_offline_model_registration.py``'s fixture of the same name.
    """
    from app.services.search import ml_model_service

    sent: list[dict[str, Any]] = []

    class _Transport:
        def perform_request(self, method: str, path: str, body: dict[str, Any] | None = None):
            sent.append({"method": method, "path": path, "body": body})
            return {"task_id": "task-1"}

    class _Client:
        transport = _Transport()

    monkeypatch.setattr(ml_model_service, "get_opensearch_client", lambda: _Client())
    svc = ml_model_service.OpenSearchMLModelService()
    monkeypatch.setattr(svc, "configure_ml_settings", lambda: True)
    svc.sent = sent  # type: ignore[attr-defined]
    return svc


def _status(state: str, *, deployed: bool | None = None) -> dict[str, Any]:
    return {
        "model_id": "model-1",
        "name": "huggingface/sentence-transformers/all-MiniLM-L6-v2",
        "state": state,
        "deployed": (state == "DEPLOYED") if deployed is None else deployed,
    }


def test_a_registering_model_is_never_deployed_into(service, monkeypatch):
    """The defect, isolated: REGISTERING must never reach deploy_model."""
    deploy_calls: list[str] = []
    monkeypatch.setattr(service, "find_model_by_name", lambda name: "model-1")
    monkeypatch.setattr(service, "get_model_status", lambda model_id: _status("REGISTERING"))
    monkeypatch.setattr(service, "deploy_model", lambda model_id: deploy_calls.append(model_id))
    # Keep the wait itself fast without touching real time/sleep.
    monkeypatch.setattr(
        service,
        "_await_deployable_state",
        lambda model_id, **kw: "REGISTERING",  # never resolves within the (fake) wait
    )

    result = service.ensure_model_deployed("huggingface/sentence-transformers/all-MiniLM-L6-v2")

    assert result is None
    assert deploy_calls == [], (
        f"deploy_model was called on a model still REGISTERING: {deploy_calls} -- this is "
        "exactly what deletes the in-flight registration's cache directory on OpenSearch's side"
    )


def test_a_registered_model_is_deployed_normally(service, monkeypatch):
    """The positive control -- without it, 'never deploys' would pass by always refusing."""
    deploy_calls: list[str] = []

    def fake_deploy_model(model_id: str) -> bool:
        deploy_calls.append(model_id)
        return True

    monkeypatch.setattr(service, "find_model_by_name", lambda name: "model-1")
    monkeypatch.setattr(service, "get_model_status", lambda model_id: _status("REGISTERED"))
    monkeypatch.setattr(service, "deploy_model", fake_deploy_model)
    monkeypatch.setattr(service, "_await_deployable_state", lambda model_id, **kw: "REGISTERED")
    monkeypatch.setattr(service, "verify_model_can_embed", lambda model_id, **kw: (True, "ok"))

    result = service.ensure_model_deployed("huggingface/sentence-transformers/all-MiniLM-L6-v2")

    assert result == "model-1"
    assert deploy_calls == ["model-1"]


def test_a_model_that_finishes_registering_while_we_wait_is_deployed(service, monkeypatch):
    """The wait resolves to DEPLOYED (another caller finished the whole sequence)."""
    monkeypatch.setattr(service, "find_model_by_name", lambda name: "model-1")
    monkeypatch.setattr(service, "get_model_status", lambda model_id: _status("REGISTERING"))
    monkeypatch.setattr(service, "_await_deployable_state", lambda model_id, **kw: "DEPLOYED")
    monkeypatch.setattr(service, "verify_model_can_embed", lambda model_id, **kw: (True, "ok"))

    deploy_calls: list[str] = []
    monkeypatch.setattr(service, "deploy_model", lambda model_id: deploy_calls.append(model_id))

    result = service.ensure_model_deployed("huggingface/sentence-transformers/all-MiniLM-L6-v2")

    assert result == "model-1"
    assert deploy_calls == [], "already DEPLOYED by the time we waited -- must not deploy again"


def test_await_deployable_state_polls_until_the_model_leaves_registering(service, monkeypatch):
    """The real wait loop, not a stubbed replacement -- proves it actually polls and resolves."""
    states = iter(["REGISTERING", "REGISTERING", "REGISTERED"])
    monkeypatch.setattr(
        service, "get_model_status", lambda model_id: _status(next(states, "REGISTERED"))
    )
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    result = service._await_deployable_state("model-1", max_wait=10.0, poll_interval=0.01)

    assert result == "REGISTERED"
    assert len(sleeps) == 2, "should poll exactly twice before seeing REGISTERED"


def test_await_deployable_state_gives_up_at_max_wait_without_ever_returning_deployable(
    service, monkeypatch
):
    """A model stuck REGISTERING forever must not be silently treated as safe to deploy."""
    monkeypatch.setattr(service, "get_model_status", lambda model_id: _status("REGISTERING"))
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = service._await_deployable_state("model-1", max_wait=0.05, poll_interval=0.01)

    assert result == "REGISTERING"


def test_the_race_reproduction_matches_the_live_failure_shape(service, monkeypatch):
    """End to end through ensure_model_deployed with a fake OpenSearch that models the race.

    Models the exact sequence the Opus investigation reproduced on a real opensearch:3.4.0
    container: caller A is mid-registration (state REGISTERING); caller B calls
    ensure_model_deployed and must wait rather than deploy. When A's registration lands
    (REGISTERED), B may then deploy -- and only then.
    """
    call_log: list[str] = []
    # Simulates OpenSearch's real state machine: REGISTERING for two polls, then REGISTERED.
    # `last_state` is mutated by every read, so `fake_deploy_model` can check what the
    # state ACTUALLY WAS at the moment deploy was called -- not just whether deploy
    # happened at all, which the pre-fix code also does (on its very first, and only,
    # status read).
    states = iter(["REGISTERING", "REGISTERING", "REGISTERED"])
    last_state: dict[str, str | None] = {"value": None}

    def fake_get_model_status(model_id):
        current = next(states, "REGISTERED")
        last_state["value"] = current
        return _status(current)

    def fake_deploy_model(model_id):
        call_log.append(f"deploy:{model_id} (state was {last_state['value']})")
        if last_state["value"] in ("REGISTERING", "DEPLOYING"):
            raise AssertionError(
                f"deploy_model called while model was still {last_state['value']!r} -- "
                "this is exactly what deletes the in-flight registration's cache "
                "directory on OpenSearch's side"
            )
        return True

    monkeypatch.setattr(service, "find_model_by_name", lambda name: "model-1")
    monkeypatch.setattr(service, "get_model_status", fake_get_model_status)
    monkeypatch.setattr(service, "deploy_model", fake_deploy_model)
    monkeypatch.setattr(service, "verify_model_can_embed", lambda model_id, **kw: (True, "ok"))
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = service.ensure_model_deployed("huggingface/sentence-transformers/all-MiniLM-L6-v2")

    assert result == "model-1"
    assert call_log == ["deploy:model-1 (state was REGISTERED)"], (
        f"deploy_model must be called exactly once, only after REGISTERED: {call_log}"
    )
