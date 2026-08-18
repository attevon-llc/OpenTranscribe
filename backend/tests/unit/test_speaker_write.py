"""``opensearch_service.speaker_write`` — real writes against a live OpenSearch.

Every test here hits a genuinely throwaway pair of speaker indices
(``test_speakers_write_<uuid>_v3`` / ``_v4``, aliased exactly the way
``ensure_indices_exist`` wires the real ``speakers`` alias) on the dev
cluster, created and torn down per test via ``settings.OPENSEARCH_SPEAKER_INDEX``
monkeypatching — the same technique
``tests/integration/test_speaker_rename_service_chunks.py`` uses for the
chunks index. Nothing here touches the real ``speakers``/``speakers_v3``/
``speakers_v4`` indices.

Per the repo-wide cosinesimil gotcha (``app/services/search/CLAUDE.md``):
this module never reads a kNN ``_score`` — every operation here is a plain
``index``/``bulk`` write, so there is no score-conversion site to check. The
11 existing conversion sites are enumerated in that CLAUDE.md and none of
them live in this file.
"""

from __future__ import annotations

import os
import uuid as uuid_pkg
from types import SimpleNamespace

import pytest
from opensearchpy.exceptions import NotFoundError

from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V3
from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.services.opensearch_service import speaker_write

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"


# Serialised against the other live-cluster speaker suites (issue #486). All three create and
# delete throwaway OpenSearch indices; under the default `-n auto` (24 workers here) that
# index churn overloads the single dev cluster and reads start timing out at 10 s, which
# surfaces as an unrelated-looking assertion failure in whichever test lost the race.
# Measured: 1 failure in 4 concurrent runs, on a different test each time.
pytestmark = [
    pytest.mark.xdist_group("opensearch_speaker_indices"),
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). These tests verify real "
            "document writes and cannot be meaningfully mocked."
        ),
    ),
]


def _embedding(dimension: int, offset: float = 0.0) -> list[float]:
    """A deterministic, non-degenerate embedding vector of the given dimension."""
    return [round(((i + 1) * 0.001 + offset) % 1.0, 6) for i in range(dimension)]


@pytest.fixture
def speaker_indices(monkeypatch):
    """A throwaway v3/v4 speaker index pair with a real 'speakers'-shaped alias.

    Mirrors what ``ensure_indices_exist`` builds in production: two versioned
    concrete indices plus an alias pointing at v4 (the "fresh install"
    default), all under a unique name so nothing here can collide with, or
    leak into, a real deployment's indices.
    """
    from app.core.config import settings
    from app.core.constants import get_speaker_index
    from app.core.constants import get_speaker_index_v3
    from app.core.constants import get_speaker_index_v4
    from app.services.opensearch_service import client as _client
    from app.services.opensearch_service.indices import _ensure_versioned_speaker_index

    client = _client.opensearch_client
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    base_name = f"test_speakers_write_{uuid_pkg.uuid4().hex[:10]}"
    monkeypatch.setattr(settings, "OPENSEARCH_SPEAKER_INDEX", base_name)

    v3 = get_speaker_index_v3()
    v4 = get_speaker_index_v4()
    _ensure_versioned_speaker_index(v3, PYANNOTE_EMBEDDING_DIMENSION_V3)
    _ensure_versioned_speaker_index(v4, PYANNOTE_EMBEDDING_DIMENSION_V4)
    alias = get_speaker_index()
    client.indices.put_alias(index=v4, name=alias)

    try:
        yield SimpleNamespace(client=client, v3=v3, v4=v4, alias=alias)
    finally:
        for idx in (v3, v4):
            client.indices.delete(index=idx, ignore=[404])


# --------------------------------------------------------------------------- #
# add_speaker_embedding_v4
# --------------------------------------------------------------------------- #


def test_add_speaker_embedding_v4_writes_a_readable_document(speaker_indices):
    speaker_uuid = str(uuid_pkg.uuid4())
    embedding = _embedding(PYANNOTE_EMBEDDING_DIMENSION_V4)

    response = speaker_write.add_speaker_embedding_v4(
        speaker_id=101,
        speaker_uuid=speaker_uuid,
        user_id=42,
        name="SPEAKER_00",
        embedding=embedding,
        display_name="Alice",
        segment_count=3,
    )

    assert response is not None
    assert response["result"] == "created"

    doc = speaker_indices.client.get(index=speaker_indices.v4, id=speaker_uuid)["_source"]
    assert doc["speaker_id"] == 101
    assert doc["user_id"] == 42
    assert doc["name"] == "SPEAKER_00"
    assert doc["display_name"] == "Alice"
    assert doc["segment_count"] == 3
    assert doc["embedding"] == pytest.approx(embedding, abs=1e-4)
    # organization_id was never passed — must be entirely absent, not null,
    # to match the personal-scope search gate's must_not-exists filter.
    assert "organization_id" not in doc


def test_add_speaker_embedding_v4_writes_organization_id_when_present(speaker_indices):
    speaker_uuid = str(uuid_pkg.uuid4())

    speaker_write.add_speaker_embedding_v4(
        speaker_id=102,
        speaker_uuid=speaker_uuid,
        user_id=42,
        name="SPEAKER_01",
        embedding=_embedding(PYANNOTE_EMBEDDING_DIMENSION_V4),
        organization_id=77,
    )

    doc = speaker_indices.client.get(index=speaker_indices.v4, id=speaker_uuid)["_source"]
    assert doc["organization_id"] == 77


def test_add_speaker_embedding_v4_rejects_wrong_dimension(speaker_indices):
    speaker_uuid = str(uuid_pkg.uuid4())

    response = speaker_write.add_speaker_embedding_v4(
        speaker_id=103,
        speaker_uuid=speaker_uuid,
        user_id=42,
        name="SPEAKER_02",
        embedding=[0.1, 0.2, 0.3],  # not 256-dim
    )

    assert response is None
    with pytest.raises(NotFoundError):
        speaker_indices.client.get(index=speaker_indices.v4, id=speaker_uuid)


def test_add_speaker_embedding_v4_rejects_none_embedding(speaker_indices):
    speaker_uuid = str(uuid_pkg.uuid4())

    response = speaker_write.add_speaker_embedding_v4(
        speaker_id=104,
        speaker_uuid=speaker_uuid,
        user_id=42,
        name="SPEAKER_03",
        embedding=None,  # type: ignore[arg-type]
    )

    assert response is None
    with pytest.raises(NotFoundError):
        speaker_indices.client.get(index=speaker_indices.v4, id=speaker_uuid)


# --------------------------------------------------------------------------- #
# bulk_add_speaker_embeddings_v4
# --------------------------------------------------------------------------- #


def test_bulk_add_speaker_embeddings_v4_indexes_good_entries_and_skips_bad_ones(speaker_indices):
    good_a = str(uuid_pkg.uuid4())
    good_b = str(uuid_pkg.uuid4())
    bad_dim = str(uuid_pkg.uuid4())

    response = speaker_write.bulk_add_speaker_embeddings_v4(
        [
            {
                "speaker_uuid": good_a,
                "speaker_id": 201,
                "user_id": 42,
                "name": "SPEAKER_A",
                "embedding": _embedding(PYANNOTE_EMBEDDING_DIMENSION_V4, offset=0.1),
            },
            {
                "speaker_uuid": bad_dim,
                "speaker_id": 202,
                "user_id": 42,
                "name": "SPEAKER_BAD",
                "embedding": [0.1, 0.2],  # wrong dimension
            },
            {
                "speaker_uuid": good_b,
                "speaker_id": 203,
                "user_id": 42,
                "name": "SPEAKER_B",
                "embedding": _embedding(PYANNOTE_EMBEDDING_DIMENSION_V4, offset=0.2),
                "organization_id": 55,
            },
        ]
    )

    assert response is not None
    assert response.get("errors") is False
    speaker_indices.client.indices.refresh(index=speaker_indices.v4)

    count = speaker_indices.client.count(index=speaker_indices.v4)["count"]
    assert count == 2

    doc_a = speaker_indices.client.get(index=speaker_indices.v4, id=good_a)["_source"]
    assert doc_a["name"] == "SPEAKER_A"
    assert "organization_id" not in doc_a

    doc_b = speaker_indices.client.get(index=speaker_indices.v4, id=good_b)["_source"]
    assert doc_b["organization_id"] == 55

    with pytest.raises(NotFoundError):
        speaker_indices.client.get(index=speaker_indices.v4, id=bad_dim)


def test_bulk_add_speaker_embeddings_v4_empty_input_returns_none(speaker_indices):
    assert speaker_write.bulk_add_speaker_embeddings_v4([]) is None


def test_bulk_add_speaker_embeddings_v4_all_bad_entries_returns_none_and_writes_nothing(
    speaker_indices,
):
    response = speaker_write.bulk_add_speaker_embeddings_v4(
        [
            {
                "speaker_uuid": str(uuid_pkg.uuid4()),
                "speaker_id": 301,
                "user_id": 42,
                "name": "SPEAKER_BAD",
                "embedding": [0.1],
            }
        ]
    )

    assert response is None
    speaker_indices.client.indices.refresh(index=speaker_indices.v4)
    assert speaker_indices.client.count(index=speaker_indices.v4)["count"] == 0


# --------------------------------------------------------------------------- #
# add_speaker_embedding (v3/v4 dual-dimension, default-index)
# --------------------------------------------------------------------------- #


def test_add_speaker_embedding_default_target_lands_in_the_alias(speaker_indices):
    """No ``target_index`` -> the alias, which in this fixture resolves to v4."""
    speaker_uuid = str(uuid_pkg.uuid4())

    response = speaker_write.add_speaker_embedding(
        speaker_id=401,
        speaker_uuid=speaker_uuid,
        user_id=42,
        name="SPEAKER_ALIAS",
        embedding=_embedding(PYANNOTE_EMBEDDING_DIMENSION_V4),
    )

    assert response is not None
    doc = speaker_indices.client.get(index=speaker_indices.alias, id=speaker_uuid)["_source"]
    assert doc["name"] == "SPEAKER_ALIAS"
    # It really landed in the concrete v4 index, not merely readable via alias.
    v4_doc = speaker_indices.client.get(index=speaker_indices.v4, id=speaker_uuid)["_source"]
    assert v4_doc["name"] == "SPEAKER_ALIAS"


def test_add_speaker_embedding_explicit_v3_target(speaker_indices):
    speaker_uuid = str(uuid_pkg.uuid4())

    speaker_write.add_speaker_embedding(
        speaker_id=402,
        speaker_uuid=speaker_uuid,
        user_id=42,
        name="SPEAKER_V3",
        embedding=_embedding(PYANNOTE_EMBEDDING_DIMENSION_V3),
        target_index=speaker_indices.v3,
    )

    doc = speaker_indices.client.get(index=speaker_indices.v3, id=speaker_uuid)["_source"]
    assert doc["name"] == "SPEAKER_V3"
    with pytest.raises(NotFoundError):
        speaker_indices.client.get(index=speaker_indices.v4, id=speaker_uuid)


def test_add_speaker_embedding_rejects_unexpected_dimension(speaker_indices):
    speaker_uuid = str(uuid_pkg.uuid4())

    response = speaker_write.add_speaker_embedding(
        speaker_id=403,
        speaker_uuid=speaker_uuid,
        user_id=42,
        name="SPEAKER_BAD_DIM",
        embedding=[0.1] * 17,  # neither 512 nor 256
        target_index=speaker_indices.v3,
    )

    assert response is None
    with pytest.raises(NotFoundError):
        speaker_indices.client.get(index=speaker_indices.v3, id=speaker_uuid)


def test_add_speaker_embedding_retries_once_on_a_transient_connection_error(
    speaker_indices, monkeypatch
):
    """The retry path must actually deliver the document, not just swallow the error.

    Raises ``opensearchpy.exceptions.ConnectionError`` — the REAL exception type
    the client raises on a connectivity blip — not the Python builtin
    ``ConnectionError``, which opensearchpy's does NOT subclass (issue #474: the
    retry catch used to check ``isinstance(e, (ConnectionError, OSError))`` with
    the builtin, so it never actually fired against a real client error).
    """
    from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

    speaker_uuid = str(uuid_pkg.uuid4())
    real_index = speaker_indices.client.index
    calls = {"n": 0}

    def flaky_index(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OpenSearchConnectionError("N/A", "simulated transient network failure", {})
        return real_index(*args, **kwargs)

    monkeypatch.setattr(speaker_indices.client, "index", flaky_index)

    response = speaker_write.add_speaker_embedding(
        speaker_id=404,
        speaker_uuid=speaker_uuid,
        user_id=42,
        name="SPEAKER_RETRY",
        embedding=_embedding(PYANNOTE_EMBEDDING_DIMENSION_V3),
        target_index=speaker_indices.v3,
    )

    assert calls["n"] == 2
    assert response is not None
    doc = speaker_indices.client.get(index=speaker_indices.v3, id=speaker_uuid)["_source"]
    assert doc["name"] == "SPEAKER_RETRY"
