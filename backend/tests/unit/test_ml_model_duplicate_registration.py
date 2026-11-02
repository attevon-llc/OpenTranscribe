"""A registration that outlived its wait must not be followed by a duplicate model.

Found at stack start: the local registration task ran past ``_wait_for_registration``'s ceiling,
``register_model_from_local`` returned ``None``, and ``ensure_model_deployed`` went on to
register the same model again from the remote registry. The first registration then completed
too, leaving two models of one name -- one DEPLOYED, one not -- and ``find_model_by_name``
returned whichever the search listed first.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

NAME = "huggingface/sentence-transformers/all-MiniLM-L6-v2"


def _records(calls: list[str], result: Any):
    """A stand-in that records its argument and returns ``result``."""

    def _call(arg: str) -> Any:
        calls.append(arg)
        return result

    return _call


def _model(model_id: str, state: str) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "name": NAME,
        "state": state,
        "deployed": state == "DEPLOYED",
    }


@pytest.fixture
def service(monkeypatch):
    from pathlib import Path

    from app.services.search import ml_model_service

    svc = ml_model_service.OpenSearchMLModelService.__new__(
        ml_model_service.OpenSearchMLModelService
    )
    svc._client = MagicMock()
    monkeypatch.setattr(svc, "_ensure_client", lambda: True)
    monkeypatch.setattr(svc, "get_local_model_path", lambda name: Path("/ml-models/x.zip"))
    monkeypatch.setattr(svc, "_verified_or_none", lambda name, model_id: model_id)
    return svc


def test_the_deployed_model_wins_over_an_undeployed_duplicate(service, monkeypatch):
    monkeypatch.setattr(
        service,
        "list_models",
        lambda deployed_only=False: [
            _model("dup-undeployed", "REGISTERED"),
            _model("ok", "DEPLOYED"),
        ],
    )

    assert service.find_model_by_name(NAME) == "ok"


def test_an_in_flight_model_beats_a_failed_one_and_ties_keep_search_order(service, monkeypatch):
    monkeypatch.setattr(
        service,
        "list_models",
        lambda deployed_only=False: [
            _model("failed", "REGISTER_FAILED"),
            _model("busy", "REGISTERING"),
            _model("busy2", "DEPLOYING"),
        ],
    )

    assert service.find_model_by_name(NAME) == "busy"
    assert service.find_model_by_name("nothing-by-this-name") is None


def test_a_local_registration_that_landed_late_is_adopted_not_duplicated(service, monkeypatch):
    registered: list[str] = []
    listings = iter([[], [_model("late", "REGISTERED")]])  # empty at first, there on re-check
    monkeypatch.setattr(service, "list_models", lambda deployed_only=False: next(listings))
    monkeypatch.setattr(service, "register_model_from_local", lambda name: None)
    monkeypatch.setattr(service, "register_model", _records(registered, "dup"))
    monkeypatch.setattr(
        service, "get_model_status", lambda model_id: {"state": "REGISTERED", "deployed": False}
    )
    deployed: list[str] = []
    monkeypatch.setattr(service, "deploy_model", _records(deployed, True))

    assert service.ensure_model_deployed(NAME) == "late"

    assert registered == [], "a second model was registered while the first existed"
    assert deployed == ["late"]


def test_remote_registration_still_happens_when_nothing_was_registered(service, monkeypatch):
    """Control: without it, 'never registers a duplicate' would pass by never registering."""
    monkeypatch.setattr(service, "list_models", lambda deployed_only=False: [])
    monkeypatch.setattr(service, "register_model_from_local", lambda name: None)
    registered: list[str] = []
    monkeypatch.setattr(service, "register_model", _records(registered, "fresh"))
    monkeypatch.setattr(service, "deploy_model", lambda model_id: True)

    assert service.ensure_model_deployed(NAME) == "fresh"
    assert registered == [NAME]
