"""Speaker/title renames must reach the chunk plane (issue #405) — real OpenSearch.

``update_by_query`` semantics are the whole fix: whether OpenSearch executes the
painless script as written, whether ``conflicts=proceed`` and the ``noop`` guard
behave, whether the rewrite is visible after the refresh. A stand-in index can
only confirm we *believe* those things, so every test here runs against a real
cluster and skips loudly when none is reachable.

Each test carries its own negative control — the pre-#405 state, asserted live
against the cluster — so a rewrite that silently matched nothing would fail on
the "after" assertion rather than pass on an empty index.

Point the suite at an isolated stack (never the shared dev one, whose live index
these must not touch)::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_rename_propagation_chunks.py -m integration
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and "
            "export OPENSEARCH_PORT — a stand-in cannot validate update_by_query."
        ),
    ),
]

OLD_NAME = "SPEAKER_01"
NEW_NAME = "Dana"
OTHER_NAME = "Ravi"
USER_ID = 4051


def _doc(
    file_uuid: str,
    chunk_index: int,
    speaker: str,
    speakers: list[str],
    content: str,
    title: str = "Q3 pricing sync",
) -> dict[str, Any]:
    """One chunk document shaped exactly like the indexing service writes them."""
    return {
        "file_id": 405,
        "file_uuid": file_uuid,
        "user_id": USER_ID,
        "chunk_index": chunk_index,
        "content": content,
        "title": title,
        "speaker": speaker,
        "speakers": speakers,
        "tags": [],
        "content_type": "audio/wav",
        "accessible_user_ids": [USER_ID],
        "upload_time": "2026-08-01T00:00:00+00:00",
        "language": "en",
        "start_time": float(chunk_index * 10),
        "end_time": float(chunk_index * 10 + 9),
        "indexed_at": "2026-08-01T00:00:00+00:00",
    }


@pytest.fixture
def chunk_index(monkeypatch):
    """A throwaway chunks index with the REAL mapping, wired in via settings.

    The real mapping matters: ``speaker`` is a ``keyword`` with eager global
    ordinals, and the whole bug is an exact ``terms`` match against it.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    name = f"test_rename_chunks_{uuid.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    try:
        yield client
    finally:
        client.indices.delete(index=name, ignore=[404])


def _index_docs(client, docs: list[dict[str, Any]]) -> None:
    from app.core.config import settings

    for doc in docs:
        client.index(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            id=f"{doc['file_uuid']}_{doc['chunk_index']}",
            body=doc,
        )
    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)


def _sources(client, file_uuid: str) -> list[dict[str, Any]]:
    from app.core.config import settings

    response = client.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={"size": 50, "query": {"term": {"file_uuid": file_uuid}}, "sort": ["chunk_index"]},
    )
    return [hit["_source"] for hit in response["hits"]["hits"]]


def test_speaker_rename_rewrites_chunk_speaker_and_speakers_array(chunk_index):
    """The rename rewrites the per-chunk ``speaker`` AND the file-level array."""
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    file_uuid = str(uuid.uuid4())
    _index_docs(
        chunk_index,
        [
            _doc(file_uuid, 0, OLD_NAME, [OLD_NAME, OTHER_NAME], "we should raise pricing"),
            _doc(file_uuid, 1, OTHER_NAME, [OLD_NAME, OTHER_NAME], "agreed on the pricing tier"),
        ],
    )

    before = _sources(chunk_index, file_uuid)
    assert [d["speaker"] for d in before] == [OLD_NAME, OTHER_NAME]
    assert all(OLD_NAME in d["speakers"] for d in before), "control: the stale name is indexed"

    result = propagate_speaker_rename(file_uuid, [OLD_NAME], NEW_NAME)

    assert result["status"] == "success"
    assert result["updated"] == 2, "both chunks carry the file-level speakers array"

    after = _sources(chunk_index, file_uuid)
    assert [d["speaker"] for d in after] == [NEW_NAME, OTHER_NAME]
    for doc in after:
        assert OLD_NAME not in doc["speakers"]
        assert sorted(doc["speakers"]) == sorted([NEW_NAME, OTHER_NAME])


def test_rename_leaves_other_files_and_other_speakers_alone(chunk_index):
    """A rename is scoped to one file — a same-named speaker elsewhere is untouched."""
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    renamed_uuid = str(uuid.uuid4())
    untouched_uuid = str(uuid.uuid4())
    _index_docs(
        chunk_index,
        [
            _doc(renamed_uuid, 0, OLD_NAME, [OLD_NAME], "budget review"),
            _doc(untouched_uuid, 0, OLD_NAME, [OLD_NAME], "unrelated recording"),
        ],
    )

    propagate_speaker_rename(renamed_uuid, [OLD_NAME], NEW_NAME)

    assert [d["speaker"] for d in _sources(chunk_index, renamed_uuid)] == [NEW_NAME]
    assert [d["speaker"] for d in _sources(chunk_index, untouched_uuid)] == [OLD_NAME]


def test_chat_speaker_scope_finds_pre_rename_content_under_the_new_name(chunk_index):
    """The headline bug: chat scopes by the CURRENT name, the index holds the OLD one.

    Chat resolves display names from Postgres and filters with an exact ``terms``
    match on ``speaker`` (``hybrid_search_service._build_filters``). Before the
    propagation runs, asking about ``Dana`` returns nothing from anything indexed
    before the rename — and the model answers from the remainder. The first
    assertion here IS the pre-#405 behaviour, measured against a real cluster.
    """
    from app.services.search.chunk_retrieval import retrieve_chunks
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    file_uuid = str(uuid.uuid4())
    _index_docs(
        chunk_index,
        [
            _doc(
                file_uuid, 0, OLD_NAME, [OLD_NAME, OTHER_NAME], "pricing should go up ten percent"
            ),
            _doc(file_uuid, 1, OTHER_NAME, [OLD_NAME, OTHER_NAME], "pricing is fine as it stands"),
        ],
    )

    def _scoped(speaker: str):
        return retrieve_chunks(
            "pricing",
            user_id=USER_ID,
            file_uuids=[file_uuid],
            speakers=[speaker],
            search_mode="keyword",
        )

    assert _scoped(OLD_NAME), "control: the content is retrievable under the stale name"
    assert _scoped(NEW_NAME) == [], "the bug: the renamed speaker's words are unreachable"

    propagate_speaker_rename(file_uuid, [OLD_NAME], NEW_NAME)

    hits = _scoped(NEW_NAME)
    assert len(hits) == 1
    assert hits[0].speaker == NEW_NAME
    assert "ten percent" in hits[0].content
    assert _scoped(OLD_NAME) == [], "and the stale name no longer resolves"


def test_batch_accept_collapses_several_speakers_in_one_pass(chunk_index):
    """A batch accept maps several diarized labels onto one person, in one query."""
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    file_uuid = str(uuid.uuid4())
    _index_docs(
        chunk_index,
        [
            _doc(file_uuid, 0, "SPEAKER_01", ["SPEAKER_01", "SPEAKER_02", OTHER_NAME], "part one"),
            _doc(file_uuid, 1, "SPEAKER_02", ["SPEAKER_01", "SPEAKER_02", OTHER_NAME], "part two"),
            _doc(file_uuid, 2, OTHER_NAME, ["SPEAKER_01", "SPEAKER_02", OTHER_NAME], "part three"),
        ],
    )

    result = propagate_speaker_rename(file_uuid, ["SPEAKER_01", "SPEAKER_02"], NEW_NAME)

    assert result["updated"] == 3
    after = _sources(chunk_index, file_uuid)
    assert [d["speaker"] for d in after] == [NEW_NAME, NEW_NAME, OTHER_NAME]
    for doc in after:
        # De-duplicated: two labels collapsing onto one name must not produce
        # ["Dana", "Dana", "Ravi"], which would double-count the facet.
        assert sorted(doc["speakers"]) == sorted([NEW_NAME, OTHER_NAME])


def test_rename_is_a_noop_when_no_chunk_carries_the_old_name(chunk_index):
    """Nothing to rewrite must not rewrite everything (or bump versions for free)."""
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    file_uuid = str(uuid.uuid4())
    _index_docs(chunk_index, [_doc(file_uuid, 0, OTHER_NAME, [OTHER_NAME], "nothing to do")])

    result = propagate_speaker_rename(file_uuid, ["Someone Else"], NEW_NAME)

    assert result["updated"] == 0
    assert [d["speaker"] for d in _sources(chunk_index, file_uuid)] == [OTHER_NAME]


def test_title_rename_rewrites_every_chunk_of_the_file(chunk_index):
    """``update_transcript_title`` only touches the full-doc index; chunks need this."""
    from app.tasks.rename_propagation_task import propagate_title_rename

    file_uuid = str(uuid.uuid4())
    other_uuid = str(uuid.uuid4())
    _index_docs(
        chunk_index,
        [
            _doc(file_uuid, 0, OLD_NAME, [OLD_NAME], "one", title="Old title"),
            _doc(file_uuid, 1, OLD_NAME, [OLD_NAME], "two", title="Old title"),
            _doc(other_uuid, 0, OLD_NAME, [OLD_NAME], "three", title="Old title"),
        ],
    )

    assert {d["title"] for d in _sources(chunk_index, file_uuid)} == {"Old title"}

    result = propagate_title_rename(file_uuid, "Renamed retro")

    assert result["updated"] == 2
    assert {d["title"] for d in _sources(chunk_index, file_uuid)} == {"Renamed retro"}
    assert {d["title"] for d in _sources(chunk_index, other_uuid)} == {"Old title"}


def test_propagation_bumps_the_chat_corpus_version(chunk_index, monkeypatch):
    """Otherwise chat serves the pre-rename retrieval for the length of the cache TTL."""
    from app.services.chat import retrieval_cache
    from app.tasks.rename_propagation_task import propagate_speaker_rename
    from app.tasks.rename_propagation_task import propagate_title_rename

    bumps: list[int] = []
    monkeypatch.setattr(retrieval_cache, "bump_corpus_version", lambda: bumps.append(1))

    file_uuid = str(uuid.uuid4())
    _index_docs(chunk_index, [_doc(file_uuid, 0, OLD_NAME, [OLD_NAME], "pricing")])

    propagate_speaker_rename(file_uuid, [OLD_NAME], NEW_NAME)
    assert len(bumps) == 1, "a speaker rename must invalidate cached retrievals"

    propagate_title_rename(file_uuid, "A new title")
    assert len(bumps) == 2, "a title rename must too — citations carry the title"

    propagate_title_rename(file_uuid, "A new title")
    assert len(bumps) == 2, "an unchanged title rewrote nothing, so nothing to invalidate"


def test_rename_query_is_built_through_chunk_plane_query(chunk_index, monkeypatch):
    """Stage 3's ``doc_type`` predicate goes in ONE place and must reach these rewrites.

    ``chunk_plane_query`` is the single point where the chunk-plane predicate is
    built (issue #400). A rename that hand-rolled its own body would keep working
    today and silently rewrite per-file digests once #383 Phase 3 lands.
    """
    from app.services.search import indexing_service as svc
    from app.tasks import rename_propagation_task as task_module

    file_uuid = str(uuid.uuid4())
    digest_uuid = f"{file_uuid}_digest"
    _index_docs(
        chunk_index,
        [
            {**_doc(file_uuid, 0, OLD_NAME, [OLD_NAME], "chunk plane"), "doc_type": "chunk"},
            # Stand-in for a Phase 3 digest doc: same file, different plane.
            {**_doc(file_uuid, 99, OLD_NAME, [OLD_NAME], "digest prose"), "doc_type": "digest"},
        ],
    )

    real_query = svc.chunk_plane_query

    def _with_doc_type(file_uuid_arg: str, **kwargs):
        query = real_query(file_uuid_arg, **kwargs)
        query["bool"]["filter"].append({"term": {"doc_type": "chunk"}})
        return query

    monkeypatch.setattr(svc, "chunk_plane_query", _with_doc_type)
    assert task_module.propagate_speaker_rename(file_uuid, [OLD_NAME], NEW_NAME)["updated"] == 1
    assert task_module.propagate_title_rename(file_uuid, "New title")["updated"] == 1

    by_index = {d["chunk_index"]: d for d in _sources(chunk_index, file_uuid)}
    assert by_index[0]["speaker"] == NEW_NAME
    assert by_index[0]["title"] == "New title"
    assert by_index[99]["speaker"] == OLD_NAME, "the digest plane was excluded, not rewritten"
    assert by_index[99]["title"] == "Q3 pricing sync"
