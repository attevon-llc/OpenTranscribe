"""Offline model registration must send the config OpenSearch requires (#491 follow-on).

MEASURED against ``opensearchproject/opensearch:3.4.0`` — the image this repo ships —
in a throwaway container with the real artifact mounted at ``/ml-models``:

    POST /_plugins/_ml/models/_register
    {"name", "version", "model_format", "url": "file:///ml-models/..."}
    -> 400 {"type": "illegal_argument_exception", "reason": "model config is null"}

That was **exactly** the body `register_model_from_url` sent, so the offline path —
the whole reason `/ml-models` is mounted and `scripts/download-models.sh` exists —
could not register anything. Every airgapped deployment silently fell through to the
remote HuggingFace download, which is the one thing an airgapped install cannot do.

Adding `model_config` (+ `model_content_hash_value`) makes it work, verified end to
end in the same container: REGISTER COMPLETED -> DEPLOY COMPLETED -> inference
returned a 384-dimension embedding.

⚠️ **The values must not be hardcoded per model.** They come from the ``config.json``
OpenSearch publishes beside every artifact, which `download-models.py` now saves. A
table in our source would be a guess that drifts from upstream, and the real types
are not guessable: ``all-mpnet-base-v2`` is ``mpnet``, ``all-distilroberta-v1`` is
``roberta``, the MiniLMs are ``bert``.

Note the second, distinct 400: a `model_config` present but lacking `model_type`
gives ``"model type is null"``. Both are covered below.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

MODEL = "huggingface/sentence-transformers/all-MiniLM-L6-v2"

#: The shape OpenSearch publishes, trimmed to what registration reads.
PUBLISHED_CONFIG = {
    "name": "sentence-transformers/all-MiniLM-L6-v2",
    "version": "1.0.1",
    "model_format": "TORCH_SCRIPT",
    "model_content_size_in_bytes": 91790008,
    "model_content_hash_value": "c15f0d2e62d872be5b5bc6c84d2e0f4921541e29fefbef51d59cc10a8ae30e0f",
    "model_config": {
        "model_type": "bert",
        "embedding_dimension": 384,
        "framework_type": "sentence_transformers",
        "all_config": '{"architectures":["BertModel"]}',
    },
}


@pytest.fixture
def service(monkeypatch):
    """The service with its transport recorded and cluster settings stubbed."""
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
    monkeypatch.setattr(svc, "_wait_for_registration", lambda task_id: "model-1")
    svc.sent = sent  # type: ignore[attr-defined]
    return svc


def test_registration_without_a_model_config_is_refused_before_the_request(service):
    """The defect: this body is what OpenSearch answers 400 to.

    Refused client-side rather than sent, because a 400 here is indistinguishable in
    the logs from the network being down — which is the very condition the offline
    path exists to handle.
    """
    result = service.register_model_from_url(model_name=MODEL, url="file:///ml-models/x.zip")

    assert result is None
    assert service.sent == [], (
        "the request was sent anyway; OpenSearch will answer 400 'model config is null' "
        f"and the offline path will look like a network failure: {service.sent}"
    )


def test_a_supplied_model_config_reaches_the_register_body(service):
    """The fix. Both fields must be on the wire, not just accepted as arguments."""
    result = service.register_model_from_url(
        model_name=MODEL,
        url="file:///ml-models/x.zip",
        model_config=PUBLISHED_CONFIG["model_config"],
        model_content_hash_value=PUBLISHED_CONFIG["model_content_hash_value"],
    )

    assert result == "model-1"
    assert len(service.sent) == 1
    body = service.sent[0]["body"]
    assert body["model_config"] == PUBLISHED_CONFIG["model_config"]
    assert body["model_content_hash_value"] == PUBLISHED_CONFIG["model_content_hash_value"]
    assert body["url"] == "file:///ml-models/x.zip"
    # model_type specifically — its absence is a DIFFERENT 400 ("model type is null").
    assert body["model_config"]["model_type"] == "bert"


def _write_artifact(tmp_path: Path, config: dict[str, Any] | None) -> Path:
    zip_path = tmp_path / "model.zip"
    zip_path.write_bytes(b"not a real zip")
    if config is not None:
        (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return zip_path


def test_the_published_config_is_read_from_beside_the_artifact(tmp_path, service):
    """`download-models.py` saves config.json next to the zip; this reads it."""
    zip_path = _write_artifact(tmp_path, PUBLISHED_CONFIG)

    model_config, content_hash = service._read_local_model_config(zip_path)

    assert model_config == PUBLISHED_CONFIG["model_config"]
    assert content_hash == PUBLISHED_CONFIG["model_content_hash_value"]


def test_a_cache_holding_only_the_zip_is_reported_not_attempted(tmp_path, service):
    """An older cache has the artifact and no config.json.

    That cache is genuinely unusable offline, and saying so beats a 400 from a
    registration that could never have succeeded.
    """
    zip_path = _write_artifact(tmp_path, None)

    assert service._read_local_model_config(zip_path) == (None, None)


def test_a_config_without_model_type_is_rejected(tmp_path, service):
    """The second 400: `model_config` present, `model_type` missing."""
    broken = {**PUBLISHED_CONFIG, "model_config": {"embedding_dimension": 384}}
    zip_path = _write_artifact(tmp_path, broken)

    assert service._read_local_model_config(zip_path) == (None, None)


def test_register_from_local_refuses_when_the_config_is_missing(tmp_path, service, monkeypatch):
    """End to end through the caller that builds the file:// URL."""
    zip_path = _write_artifact(tmp_path, None)
    monkeypatch.setattr(service, "get_local_model_path", lambda name: zip_path)

    assert service.register_model_from_local(MODEL) is None
    assert service.sent == []


def test_register_from_local_passes_the_config_through(tmp_path, service, monkeypatch):
    """The positive control — without it, "refuses" would pass if it always refused."""
    zip_path = _write_artifact(tmp_path, PUBLISHED_CONFIG)
    monkeypatch.setattr(service, "get_local_model_path", lambda name: zip_path)

    assert service.register_model_from_local(MODEL) == "model-1"
    body = service.sent[0]["body"]
    assert body["model_config"]["model_type"] == "bert"
    assert body["url"] == f"file://{zip_path}"
