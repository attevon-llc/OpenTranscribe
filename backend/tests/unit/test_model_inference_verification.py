"""A deployed model must prove it can embed before it is activated (#503).

⚠️ **"Deployed" is not "working", and that is measured, not theoretical.**
``all-mpnet-base-v2`` on a 1 GB heap reports ``DEPLOY: COMPLETED`` and then fails to
produce an embedding. ``_wait_for_deployment`` returns True on that state, so the
cluster was reported healthy while it could not embed a single document.

The consequence is silent and total: the caller stores the id as the active model, the
neural ingest pipeline points at it, and every document indexed afterwards carries no
vector. Neural search degrades to BM25 with nothing in any log saying so.

The check therefore runs a REAL prediction. Reading a status field is exactly what
failed. It also catches, one layer earlier, the #504 shape — a model named in our
registries that OpenSearch does not actually provide.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def service(monkeypatch):
    """The service with its transport recorded and its client stubbed."""
    from app.services.search import ml_model_service

    calls: list[dict[str, Any]] = []
    responses: dict[str, Any] = {}

    class _Transport:
        def perform_request(self, method: str, path: str, body: dict | None = None):
            calls.append({"method": method, "path": path, "body": body})
            if "_predict" in path:
                return responses.get("predict", {})
            return {}

    class _Client:
        transport = _Transport()

    monkeypatch.setattr(ml_model_service, "get_opensearch_client", lambda: _Client())
    svc = ml_model_service.OpenSearchMLModelService()
    svc.calls = calls  # type: ignore[attr-defined]
    svc.responses = responses  # type: ignore[attr-defined]
    return svc


def _embedding(dimension: int) -> dict[str, Any]:
    return {"inference_results": [{"output": [{"data": [0.01] * dimension}]}]}


def test_a_model_that_returns_a_vector_passes(service) -> None:
    """The control. Without it, "rejects broken models" would pass if it rejected all."""
    service.responses["predict"] = _embedding(384)

    ok, detail = service.verify_model_can_embed("model-1", expected_dimension=384)

    assert ok is True
    assert "384" in detail
    predicts = [c for c in service.calls if "_predict" in c["path"]]
    assert len(predicts) == 1, "the check must actually call the model, not read its status"
    assert predicts[0]["body"]["text_docs"], "no text was sent to embed"


def test_a_model_that_deploys_but_cannot_embed_is_rejected(service) -> None:
    """The measured defect: DEPLOY COMPLETED, then no embedding."""
    service.responses["predict"] = {"error": "Memory Circuit Breaker is open"}

    ok, detail = service.verify_model_can_embed("model-1", expected_dimension=384)

    assert ok is False
    assert "no embedding" in detail.lower()


def test_an_empty_vector_is_rejected(service) -> None:
    """A present-but-empty `data` array is still a model that cannot embed."""
    service.responses["predict"] = {"inference_results": [{"output": [{"data": []}]}]}

    ok, detail = service.verify_model_can_embed("model-1", expected_dimension=384)

    assert ok is False
    assert "empty" in detail.lower()


def test_a_dimension_mismatch_is_rejected(service) -> None:
    """768-d vectors into a 384-d knn_vector are rejected per document.

    That surfaces as an index that simply stays empty — no exception anyone reads —
    so it has to be caught here, where the number is still in hand.
    """
    service.responses["predict"] = _embedding(768)

    ok, detail = service.verify_model_can_embed("model-1", expected_dimension=384)

    assert ok is False
    assert "768" in detail and "384" in detail


def test_a_request_failure_is_reported_not_raised(service, monkeypatch) -> None:
    """A dead cluster must fail the check, not crash startup."""
    from app.services.search import ml_model_service

    class _Boom:
        def perform_request(self, *a, **kw):
            raise RuntimeError("connection refused")

    class _Client:
        transport = _Boom()

    monkeypatch.setattr(ml_model_service, "get_opensearch_client", lambda: _Client())
    svc = ml_model_service.OpenSearchMLModelService()

    ok, detail = svc.verify_model_can_embed("model-1")

    assert ok is False
    assert "connection refused" in detail


def test_ensure_model_deployed_refuses_a_model_that_cannot_embed(service, monkeypatch) -> None:
    """The wiring: every return path of ensure_model_deployed goes through the check.

    Returning the id anyway is the dangerous outcome — the caller stores it as the
    active model and the ingest pipeline points at something that produces no vectors.
    """
    monkeypatch.setattr(service, "find_model_by_name", lambda name: "model-1")
    monkeypatch.setattr(service, "get_model_status", lambda mid: {"deployed": True})
    monkeypatch.setattr(
        service, "verify_model_can_embed", lambda mid, expected_dimension=None: (False, "nope")
    )

    assert service.ensure_model_deployed("some/model") is None


def test_ensure_model_deployed_returns_a_model_that_can_embed(service, monkeypatch) -> None:
    """The positive control for the wiring above."""
    monkeypatch.setattr(service, "find_model_by_name", lambda name: "model-1")
    monkeypatch.setattr(service, "get_model_status", lambda mid: {"deployed": True})
    monkeypatch.setattr(
        service, "verify_model_can_embed", lambda mid, expected_dimension=None: (True, "384-dim")
    )

    assert service.ensure_model_deployed("some/model") == "model-1"


def test_a_settings_failure_does_not_break_deployment(service, monkeypatch) -> None:
    """The dimension is a nice-to-have; an unreadable setting must not block startup.

    Without this the check would be strictly worse than no check on a cluster whose
    settings table is briefly unavailable.
    """
    from app.services.search import settings_service

    def _boom() -> int:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_service, "get_search_embedding_dimension", _boom)
    monkeypatch.setattr(service, "find_model_by_name", lambda name: "model-1")
    monkeypatch.setattr(service, "get_model_status", lambda mid: {"deployed": True})

    seen: dict[str, Any] = {}

    def _verify(mid, expected_dimension=None):
        seen["expected"] = expected_dimension
        return True, "384-dim"

    monkeypatch.setattr(service, "verify_model_can_embed", _verify)

    assert service.ensure_model_deployed("some/model") == "model-1"
    assert seen["expected"] is None, "an unreadable setting must degrade to no dimension check"
