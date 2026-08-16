"""The neural INGEST pipeline must self-heal on config drift, not just on model change (#401).

``_check_existing_pipeline_config`` used to compare ``model_id`` and nothing else, so a
release that repointed ``field_map`` — exactly what #383 Phase 3 does — took effect on
fresh installs only. An upgraded deployment kept the old pipeline and went on embedding
the old field: no error, no log, no metric, just different retrieval quality between two
deployments of the same version.

The search pipeline has always self-healed (``ensure_search_pipeline_exists`` compares
``rank_constant``); these tests hold the ingest pipeline to the same standard, and pin
the one case that must NOT recreate — a pipeline written without ``batch_size`` by the
fallback path, which would otherwise be recreated on every boot forever.

The OpenSearch client is a stand-in that records what was PUT; recreation is asserted
from those recorded bodies, not from a call count on the function under test.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.services.search import indexing_service as svc

MODEL_ID = "abc123-model"


class _FakeIngest:
    """The ``client.ingest`` namespace, holding one live pipeline definition."""

    def __init__(self, live: dict[str, Any] | None) -> None:
        self.live = live
        self.puts: list[dict[str, Any]] = []

    def get_pipeline(self, id: str) -> dict[str, Any]:  # noqa: A002 - opensearch-py's kwarg
        if self.live is None:
            raise LookupError(f"pipeline {id} not found")
        return {id: self.live}

    def put_pipeline(self, id: str, body: dict[str, Any]) -> None:  # noqa: A002
        self.puts.append(body)
        self.live = body


class _FakeClient:
    def __init__(self, live: dict[str, Any] | None) -> None:
        self.ingest = _FakeIngest(live)


#: What the pipeline must embed **today** — index v6 repointed it from ``content``
#: (#403 Stage 3), which is the drift case below. Spelled out rather than read back
#: from ``_build_neural_ingest_pipeline``: deriving the expectation from the code
#: under test would make every assertion here true by construction, and this file
#: exists because a field_map change reaching only fresh installs is invisible.
CURRENT_FIELD_MAP = {"embedding_text": "embedding"}

#: The pre-v6 field map, i.e. what an upgraded deployment's live pipeline still has.
PRE_V6_FIELD_MAP = {"content": "embedding"}


def _live_pipeline(**overrides: Any) -> dict[str, Any]:
    """A pipeline body as OpenSearch would return it, with fields overridden."""
    processor: dict[str, Any] = {
        "model_id": MODEL_ID,
        "field_map": dict(CURRENT_FIELD_MAP),
        "batch_size": settings.SEARCH_NEURAL_BATCH_SIZE,
        "ignore_failure": False,
    }
    for key, value in overrides.items():
        if value is _ABSENT:
            processor.pop(key, None)
        else:
            processor[key] = value
    return {
        "description": f"Neural embedding pipeline for transcript search (model: {MODEL_ID})",
        "processors": [{"text_embedding": processor}],
    }


_ABSENT = object()


@pytest.fixture
def client(monkeypatch):
    """Install a fake OpenSearch client; the caller sets its live pipeline."""

    def _install(live: dict[str, Any] | None) -> _FakeClient:
        fake = _FakeClient(live)
        monkeypatch.setattr(svc, "opensearch_client", fake)
        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", True)
        svc.reset_neural_pipeline_state()
        return fake

    return _install


def _written_processor(fake: _FakeClient) -> dict[str, Any]:
    assert fake.ingest.puts, "expected the pipeline to be recreated, but nothing was PUT"
    processor: dict[str, Any] = fake.ingest.puts[-1]["processors"][0]["text_embedding"]
    return processor


# ---------------------------------------------------------------------------
# Drift must trigger recreation
# ---------------------------------------------------------------------------


def test_field_map_change_recreates_the_pipeline(client):
    """The #383 Phase 3 case, now the real one: same model, different source field.

    An upgraded deployment's live pipeline still embeds ``content``; index v6
    embeds ``embedding_text``. Without recreation that deployment keeps embedding
    the chunk body and never sees the contextualization header, silently — the
    version is identical, only retrieval quality differs.
    """
    fake = client(_live_pipeline(field_map=dict(PRE_V6_FIELD_MAP)))

    assert svc.ensure_neural_ingest_pipeline(model_id=MODEL_ID) is True

    assert _written_processor(fake)["field_map"] == CURRENT_FIELD_MAP


def test_batch_size_change_recreates_the_pipeline(client):
    """Same model, same field_map, a batch size that no longer matches config."""
    fake = client(_live_pipeline(batch_size=settings.SEARCH_NEURAL_BATCH_SIZE + 17))

    assert svc.ensure_neural_ingest_pipeline(model_id=MODEL_ID) is True

    assert _written_processor(fake)["batch_size"] == settings.SEARCH_NEURAL_BATCH_SIZE


def test_model_change_still_recreates_the_pipeline(client):
    """The one drift the old code did catch — it must keep working."""
    fake = client(_live_pipeline(model_id="a-previous-model"))

    assert svc.ensure_neural_ingest_pipeline(model_id=MODEL_ID) is True

    assert _written_processor(fake)["model_id"] == MODEL_ID


def test_absent_pipeline_is_created(client):
    fake = client(None)

    assert svc.ensure_neural_ingest_pipeline(model_id=MODEL_ID) is True

    assert _written_processor(fake)["model_id"] == MODEL_ID


# ---------------------------------------------------------------------------
# ...and a matching pipeline must be left alone
# ---------------------------------------------------------------------------


def test_matching_pipeline_is_left_alone(client):
    """The control: without this, a test that always recreates would look correct."""
    fake = client(_live_pipeline())

    assert svc.ensure_neural_ingest_pipeline(model_id=MODEL_ID) is True

    assert fake.ingest.puts == []


def test_pipeline_written_without_batch_size_is_not_recreated_every_boot(client):
    """The creation path drops ``batch_size`` when OpenSearch rejects it.

    Treating that absence as drift would recreate the pipeline on every startup,
    forever, on exactly the deployments that already needed a fallback.
    """
    fake = client(_live_pipeline(batch_size=_ABSENT))

    assert svc.ensure_neural_ingest_pipeline(model_id=MODEL_ID) is True

    assert fake.ingest.puts == []
