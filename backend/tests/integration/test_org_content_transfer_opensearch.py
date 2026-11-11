"""The transfer's restamp on a real OpenSearch (#1104).

Runs only when OpenSearch is reachable (the conftest probe on ``OPENSEARCH_PORT``, default 5180).
It works on two throwaway indices it creates and deletes, never on the live ones::

    OPENSEARCH_PORT=5280 ./scripts/run-backend-tests.sh --throwaway-db \\
        tests/integration/test_org_content_transfer_opensearch.py -m integration -n0
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.services.org_content_transfer import restamp_documents
from app.services.search.availability_field import AVAILABILITY_FIELD
from app.services.search.availability_field import exclude_unavailable_clause
from tests.fixtures.opensearch_test_index import create_test_index
from tests.fixtures.opensearch_test_index import delete_test_index

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true",
        reason="No OpenSearch reachable (SKIP_OPENSEARCH); set OPENSEARCH_PORT.",
    ),
]

OLD, NEW = 910001, 910002
LIVE, HELD, QUARANTINED_FILE = 9100001, 9100002, 9100003
MAPPING = {
    "mappings": {
        "properties": {
            "file_id": {"type": "integer"},
            "media_file_id": {"type": "integer"},
            "user_id": {"type": "integer"},
            "organization_id": {"type": "integer"},
            AVAILABILITY_FIELD: {"type": "keyword"},
        }
    }
}


@pytest.fixture()
def indices(monkeypatch):
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    assert client is not None
    tag = uuid.uuid4().hex[:8]
    chunks, transcripts = f"test_oct_chunks_{tag}", f"test_oct_transcripts_{tag}"
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", chunks)
    monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", transcripts)
    try:
        for name in (chunks, transcripts):
            create_test_index(client, name, MAPPING)
        for file_id, availability in (
            (LIVE, "available"),
            (HELD, "available"),
            (QUARANTINED_FILE, "quarantined"),
        ):
            for name in (chunks, transcripts):
                client.index(
                    index=name,
                    body={
                        "file_id": file_id,
                        "user_id": OLD,
                        "organization_id": 7,
                        AVAILABILITY_FIELD: availability,
                    },
                )
        client.indices.refresh(index=f"{chunks},{transcripts}")
        yield client, chunks, transcripts
    finally:
        for name in (chunks, transcripts):
            delete_test_index(client, name)


def _owners(client, index: str) -> dict[int, int]:
    client.indices.refresh(index=index)
    hits = client.search(index=index, body={"size": 50, "query": {"match_all": {}}})["hits"]["hits"]
    return {h["_source"]["file_id"]: h["_source"]["user_id"] for h in hits}


def test_restamp_moves_only_the_named_files_and_is_idempotent(indices):
    client, chunks, transcripts = indices
    moved = [LIVE, QUARANTINED_FILE]  # HELD stays: it was not in the moved ids

    first = restamp_documents(to_user_id=NEW, file_ids=moved, profile_ids=[], cluster_uuids=[])
    again = restamp_documents(to_user_id=NEW, file_ids=moved, profile_ids=[], cluster_uuids=[])

    assert first == 4  # two files x two indices
    assert again == 0
    for index in (chunks, transcripts):
        assert _owners(client, index) == {LIVE: NEW, QUARANTINED_FILE: NEW, HELD: OLD}


def test_a_quarantined_file_stays_hidden_for_the_new_owner(indices):
    client, chunks, _ = indices
    restamp_documents(
        to_user_id=NEW, file_ids=[LIVE, QUARANTINED_FILE], profile_ids=[], cluster_uuids=[]
    )
    client.indices.refresh(index=chunks)
    visible = client.search(
        index=chunks,
        body={
            "size": 50,
            "query": {
                "bool": {"filter": [{"term": {"user_id": NEW}}, exclude_unavailable_clause()]}
            },
        },
    )["hits"]["hits"]
    assert {h["_source"]["file_id"] for h in visible} == {LIVE}
    stored = client.search(index=chunks, body={"query": {"term": {"file_id": QUARANTINED_FILE}}})
    assert stored["hits"]["hits"][0]["_source"][AVAILABILITY_FIELD] == "quarantined"
