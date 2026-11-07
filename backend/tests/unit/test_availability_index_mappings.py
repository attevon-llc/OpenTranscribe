"""The ``availability`` field is mapped on every index that carries it, additively (#1101).

Deploying the trash must need no reindex: a NEW index is created with the field, an EXISTING
one gets it through an additive ``put_mapping`` on startup (the chunks index through its
additive step 4, the transcripts and speaker indices through ``ensure_field_mapping``). Without
the explicit mapping the first stamped write would create it by dynamic mapping as ``text``,
which happens to match ``term: trashed`` today but is not the declared contract.
"""

from __future__ import annotations

from typing import Any

KEYWORD = {"type": "keyword"}


def test_the_chunks_index_adds_availability_as_additive_step_four():
    from app.services.search.index_schema import ADDITIVE_MAPPING_STEPS
    from app.services.search.index_schema import TRANSCRIPT_CHUNKS_INDEX_BODY

    step4 = next(s for s in ADDITIVE_MAPPING_STEPS if s.version == 4)
    assert step4.properties == {"availability": KEYWORD}
    mappings = TRANSCRIPT_CHUNKS_INDEX_BODY["mappings"]
    assert isinstance(mappings, dict)
    assert mappings["properties"]["availability"] == KEYWORD


def test_a_new_transcripts_index_maps_availability():
    from app.services.opensearch_service.indices import transcript_index_body

    assert transcript_index_body()["mappings"]["properties"]["availability"] == KEYWORD


class _MappingClient:
    """Records index creations and ``put_mapping`` calls; reports a given existing mapping."""

    def __init__(self, existing: dict[str, dict[str, Any]] | None = None) -> None:
        self.created: dict[str, dict[str, Any]] = {}
        self.put: list[tuple[str, dict[str, Any]]] = []
        self.existing = existing or {}
        outer = self

        class _Indices:
            def exists(self, index: str) -> bool:
                return index in outer.existing

            def create(self, index: str, body: dict[str, Any]) -> None:
                outer.created[index] = body

            def get_mapping(self, index: str) -> dict[str, Any]:
                return {index: {"mappings": {"properties": outer.existing[index]}}}

            def put_mapping(self, index: str, body: dict[str, Any]) -> None:
                outer.put.append((index, body))

        self.indices = _Indices()


def test_every_new_speaker_index_maps_availability(monkeypatch):
    from app.services.opensearch_service import client as os_client
    from app.services.opensearch_service import indices

    client = _MappingClient()
    monkeypatch.setattr(os_client, "opensearch_client", client)

    indices._ensure_versioned_speaker_index("speakers_t_v3", 512)
    indices.create_speaker_index_v4("speakers_t_v4")

    for name in ("speakers_t_v3", "speakers_t_v4"):
        assert client.created[name]["mappings"]["properties"]["availability"] == KEYWORD, name


def test_the_speaker_rebuild_carries_availability_only_when_present():
    """The corruption rebuild re-creates every document: it must keep the trash stamp."""
    from app.services.opensearch_service.repair import _rebuild_document

    base = {"speaker_uuid": "s-1", "embedding": [0.1], "media_file_id": 3}

    assert _rebuild_document({**base, "availability": "trashed"})["availability"] == "trashed"
    assert "availability" not in _rebuild_document(base)


def test_an_existing_index_gets_the_field_by_put_mapping_once():
    from app.services.opensearch_service.indices import ensure_field_mapping

    client = _MappingClient(existing={"transcripts": {"file_id": {"type": "integer"}}})

    assert ensure_field_mapping(client, "transcripts", "availability", KEYWORD) == "added"
    assert client.put == [("transcripts", {"properties": {"availability": KEYWORD}})]

    client.existing["transcripts"]["availability"] = KEYWORD
    assert ensure_field_mapping(client, "transcripts", "availability", KEYWORD) == "present"
    assert ensure_field_mapping(client, "absent_index", "availability", KEYWORD) == "absent"
    assert len(client.put) == 1


def test_a_dynamically_mapped_field_is_left_alone():
    from app.services.opensearch_service.indices import ensure_field_mapping

    client = _MappingClient(existing={"speakers_v4": {"availability": {"type": "text"}}})

    assert ensure_field_mapping(client, "speakers_v4", "availability", KEYWORD) == (
        "present_as_text"
    )
    assert client.put == []


def test_startup_ensures_the_field_on_the_transcripts_and_every_speaker_index(monkeypatch):
    from app.core.config import settings
    from app.services.opensearch_service import indices

    monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", "transcripts_t")
    monkeypatch.setattr(
        "app.services.opensearch_service.speaker_maintenance.speaker_embedding_indices",
        lambda: ["speakers_t", "speakers_t_v3", "speakers_t_v4"],
    )
    monkeypatch.setattr(indices, "_availability_mapping_checked", set())
    client = _MappingClient(
        existing={name: {} for name in ("transcripts_t", "speakers_t_v3", "speakers_t_v4")}
    )

    indices._ensure_availability_mappings(client)
    indices._ensure_availability_mappings(client)  # verified once per process

    assert sorted(index for index, _ in client.put) == [
        "speakers_t_v3",
        "speakers_t_v4",
        "transcripts_t",
    ]
