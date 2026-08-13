"""The stale-chunk-tail prune (issue #400), executed by a real OpenSearch.

``tests/unit/test_search_chunk_pruning.py`` covers the same fix against an in-memory
stand-in. That stand-in is careful — it evaluates the real query bodies and *raises* on
any clause it does not understand — but it can only prove that we believe OpenSearch
executes a ``bool``/``filter``/``range`` ``delete_by_query`` the way the fix assumes. It
was written that way because no cluster was reachable at the time. This module closes
that gap: same service, same query builder, a real engine underneath.

What is proven here and nowhere else:

* a shrinking re-chunk really does leave no ``{file_uuid}_{n}`` tail behind;
* the count-gate really returns 0 on a first index, so the hot path issues **no**
  ``delete_by_query`` — asserted from the engine's own index stats, not from a spy;
* the prune's ``term`` on ``file_uuid`` really is scoped to one file;
* the index-v6 plane split behaves on OpenSearch exactly as it does against the stand-in —
  the chunk-plane prune spares a digest, the per-file delete takes it, and a bare
  ``doc_type`` term (the mistake) stops pruning a pre-v6 corpus entirely;
* the refresh-window race the fix's author documented is **reachable** — see
  ``test_reindex_inside_the_refresh_window_leaves_the_tail_behind``.

Point the suite at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 MINIO_PORT=5278 \\
        pytest backend/tests/integration/test_chunk_pruning_opensearch.py -m integration
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
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT — a stand-in index cannot validate delete_by_query semantics."
        ),
    ),
]

USER_ID = 4001
FILE_ID = 400


@pytest.fixture
def chunk_index(monkeypatch):
    """A throwaway chunks index with the REAL mapping, wired in via settings.

    The index NAME is monkeypatched, never the client: the whole point is that the
    service talks to a real cluster. The real mapping matters here because
    ``file_uuid`` must be a ``keyword`` for the prune's ``term`` and ``chunk_index`` an
    ``integer`` for its ``range`` — a dynamically mapped index would answer differently.

    Neural embedding is switched off: the prune is orthogonal to it, and requiring a
    deployed ML model would make this suite skip on exactly the clusters it should run on.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    name = f"test_chunk_prune_{uuid.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    svc.reset_neural_pipeline_state()
    try:
        yield client
    finally:
        client.indices.delete(index=name, ignore=[404])
        svc.reset_neural_pipeline_state()


def _segments(count: int, *, marker: str) -> list[dict[str, Any]]:
    """``count`` segments with alternating speakers → exactly ``count`` chunks.

    Alternating the speaker prevents speaker-turn merging and each segment is well under
    ``SEARCH_CHUNK_TARGET_WORDS``, so the real chunker emits one chunk per segment. The
    marker makes the *generation* of a document visible in its text.
    """
    return [
        {
            "start": float(i * 10),
            "end": float(i * 10 + 10),
            "text": f"{marker} statement number {i} about quarterly planning and staffing.",
            "speaker": f"Speaker {i % 2}",
        }
        for i in range(count)
    ]


def _index(segments: list[dict[str, Any]], *, file_uuid: str) -> dict[str, Any]:
    from app.services.search.indexing_service import TranscriptIndexingService

    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=FILE_ID,
        file_uuid=file_uuid,
        user_id=USER_ID,
        segments=segments,
        title="Quarterly planning",
        speakers=["Speaker 0", "Speaker 1"],
        tags=[],
    )
    assert isinstance(result, dict), f"indexing returned a failure sentinel: {result!r}"
    return result


def _docs(client, file_uuid: str) -> list[dict[str, Any]]:
    """Every document of one file, read back from the cluster after a refresh."""
    from app.core.config import settings

    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
    response = client.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={"size": 100, "query": {"term": {"file_uuid": file_uuid}}, "sort": ["chunk_index"]},
    )
    return [hit["_source"] for hit in response["hits"]["hits"]]


def _chunk_indexes(client, file_uuid: str) -> list[int]:
    return [int(doc["chunk_index"]) for doc in _docs(client, file_uuid)]


def _counters(client) -> dict[str, int]:
    """Engine-side counters that reveal what the service actually issued.

    ``search.query_total`` distinguishes "the count gate ran and stopped" (one search)
    from "a ``delete_by_query`` was issued as well" (the gate plus the delete's own
    searches) without patching anything on the client. ``indexing.delete_total`` is the
    number of documents actually removed.
    """
    from app.core.config import settings

    primaries = client.indices.stats(index=settings.OPENSEARCH_CHUNKS_INDEX)["indices"][
        settings.OPENSEARCH_CHUNKS_INDEX
    ]["primaries"]
    return {
        "searches": int(primaries["search"]["query_total"]),
        "deleted": int(primaries["indexing"]["delete_total"]),
    }


def _index_counters_delta(client, before: dict[str, int]) -> dict[str, int]:
    after = _counters(client)
    return {key: after[key] - before[key] for key in before}


# ---------------------------------------------------------------------------
# The decisive case
# ---------------------------------------------------------------------------


def test_shrinking_rechunk_leaves_no_stale_tail_on_a_real_cluster(chunk_index):
    """8 chunks re-indexed as 3: OpenSearch must hold 3 documents, none of them stale.

    This is issue #400 itself. Doc ids are ``{file_uuid}_{chunk_index}``, so the bulk
    load overwrites 0..2 and cannot touch 3..7 — only the ``delete_by_query`` can.
    """
    file_uuid = str(uuid.uuid4())

    first = _index(_segments(8, marker="ORIGINAL"), file_uuid=file_uuid)
    assert first["chunk_count"] == 8
    assert _chunk_indexes(chunk_index, file_uuid) == list(range(8)), "control: the tail exists"

    second = _index(_segments(3, marker="EDITED"), file_uuid=file_uuid)

    assert second["chunk_count"] == 3
    assert second["stale_removed"] == 5
    surviving = _docs(chunk_index, file_uuid)
    assert [int(doc["chunk_index"]) for doc in surviving] == [0, 1, 2]
    assert all("ORIGINAL" not in doc["content"] for doc in surviving)


def test_prune_touches_only_the_file_being_reindexed(chunk_index):
    """The ``term`` on ``file_uuid`` scopes the delete — a sibling recording is untouched."""
    shrinking_uuid = str(uuid.uuid4())
    sibling_uuid = str(uuid.uuid4())

    _index(_segments(6, marker="SIBLING"), file_uuid=sibling_uuid)
    _index(_segments(6, marker="ORIGINAL"), file_uuid=shrinking_uuid)
    assert _chunk_indexes(chunk_index, sibling_uuid) == list(range(6))

    result = _index(_segments(2, marker="EDITED"), file_uuid=shrinking_uuid)

    assert result["stale_removed"] == 4
    assert _chunk_indexes(chunk_index, shrinking_uuid) == [0, 1]
    sibling = _docs(chunk_index, sibling_uuid)
    assert [int(doc["chunk_index"]) for doc in sibling] == list(range(6))
    assert all("SIBLING" in doc["content"] for doc in sibling)


# ---------------------------------------------------------------------------
# The hot path must not pay for a delete it does not need
# ---------------------------------------------------------------------------


def test_first_index_issues_the_count_gate_and_no_delete_by_query(chunk_index):
    """Nothing indexed yet, and nothing indexed above the new count: one search, no delete.

    Read from the engine's own counters rather than a spy on the client, because the
    claim under test is about what OpenSearch was asked to do. ``delete_by_query`` forces
    a whole-index refresh, which is the cost the count gate exists to avoid on the path
    every completed transcription takes.
    """
    from app.services.search.indexing_service import chunk_plane_query

    file_uuid = str(uuid.uuid4())

    before = _counters(chunk_index)
    first = _index(_segments(5, marker="FIRST"), file_uuid=file_uuid)
    after = _index_counters_delta(chunk_index, before)

    assert first["stale_removed"] == 0
    assert after["searches"] == 1, "the count gate, and nothing after it"
    assert after["deleted"] == 0

    # The gate's own predicate, evaluated by the real searcher: genuinely empty.
    from app.core.config import settings

    chunk_index.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
    gate = chunk_index.count(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={"query": chunk_plane_query(file_uuid, from_chunk_index=5)},
    )
    assert gate["count"] == 0

    # A re-chunk that GREW has no orphans either, and must not pay for a delete.
    before = _counters(chunk_index)
    grown = _index(_segments(9, marker="EXPANDED"), file_uuid=file_uuid)
    delta = _index_counters_delta(chunk_index, before)

    assert grown["stale_removed"] == 0
    assert delta["searches"] == 1
    assert delta["deleted"] == 0
    assert _chunk_indexes(chunk_index, file_uuid) == list(range(9))


# ---------------------------------------------------------------------------
# The index-v6 plane split, on the engine rather than against the stand-in
# ---------------------------------------------------------------------------


#: The mistake, spelled out here rather than imported: this is what a bare ``term``
#: looks like, and it is the shape the shipped predicate must never collapse to.
_BARE_CHUNK_CLAUSE: dict[str, Any] = {"term": {"doc_type": "chunk"}}


def _unarmed_chunk_plane_query(
    file_uuid: str,
    *,
    from_chunk_index: int | None = None,
    extra_filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The pre-v6 predicate: file, optional range, nothing about ``doc_type``.

    Reproduced here rather than imported, because the shipped function no longer has
    this shape — and reconstructing it is what lets the hazard below be demonstrated
    against the engine instead of asserted from a docstring.
    """
    filters: list[dict[str, Any]] = [{"term": {"file_uuid": file_uuid}}]
    if from_chunk_index is not None:
        filters.append({"range": {"chunk_index": {"gte": from_chunk_index}}})
    if extra_filters:
        filters.extend(extra_filters)
    return {"bool": {"filter": filters}}


def _patch_chunk_plane_query(monkeypatch, base, svc, clause: dict[str, Any]) -> None:
    """Swap the shipped predicate for *base* + *clause*.

    *base* is passed in rather than read off the module so that re-patching within one
    test replaces the simulated clause instead of stacking a second one on top of it.
    """

    def _with_doc_type(file_uuid_arg: str, **kwargs: Any) -> dict[str, Any]:
        query: dict[str, Any] = base(file_uuid_arg, **kwargs)
        query["bool"]["filter"].append(clause)
        return query

    monkeypatch.setattr(svc, "chunk_plane_query", _with_doc_type)


def _strip_doc_type_from_chunks(client, file_uuid: str) -> None:
    """Make this file's chunk documents look the way a pre-v6 index holds them."""
    from app.core.config import settings

    client.update_by_query(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={
            "query": {
                "bool": {
                    "filter": [{"term": {"file_uuid": file_uuid}}],
                    "must_not": [{"term": {"doc_type": "digest"}}],
                }
            },
            "script": {"source": "ctx._source.remove('doc_type')", "lang": "painless"},
        },
        refresh=True,
        conflicts="proceed",
    )


def _seed_chunks_and_a_digest(client, file_uuid: str, *, chunks: int) -> None:
    """``chunks`` real chunk documents plus one digest-shaped document at index 99.

    99 is inside the range any shrinking re-chunk dooms, so if the digest survives it is
    the ``doc_type`` clause that spared it and not the ``range``. ``doc_type`` is mapped
    explicitly as ``keyword`` first, matching the v6 mapping addition — the
    index is not ``dynamic: strict``, so leaving it unmapped would make the field's
    behaviour depend on dynamic-mapping rules rather than on the declared mapping.
    """
    from app.core.config import settings

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    client.indices.put_mapping(
        index=index_name, body={"properties": {"doc_type": {"type": "keyword"}}}
    )
    _index(_segments(chunks, marker="ORIGINAL"), file_uuid=file_uuid)
    client.index(
        index=index_name,
        id=f"{file_uuid}_digest_0",
        body={
            "file_id": FILE_ID,
            "file_uuid": file_uuid,
            "user_id": USER_ID,
            "chunk_index": 99,
            "doc_type": "digest",
            "content": "digest prose summarising the meeting",
            "title": "Quarterly planning",
            "accessible_user_ids": [USER_ID],
        },
    )


def test_the_chunk_plane_predicate_spares_digests_and_the_file_plane_takes_them(chunk_index):
    """Index v6 put two kinds of document in one index; the predicates must part them.

    Until Stage 3 this test *simulated* the ``doc_type`` addition by monkeypatching
    ``chunk_plane_query``. It now drives the real predicates, and the split is the point:
    the tail prune and the ordinary chunk rewrite must leave a digest alone, while the
    per-file delete — used by file deletion and by a full rebuild — must take it. Getting
    that backwards either strands a readable summary of a deleted recording or destroys
    the digest tier on every reindex.

    The control is the last block: the same delete on the same corpus removes everything,
    so "the digest survived" is not "the delete matched nothing".
    """
    from app.services.search import indexing_service as svc

    file_uuid = str(uuid.uuid4())
    _seed_chunks_and_a_digest(chunk_index, file_uuid, chunks=6)
    assert _chunk_indexes(chunk_index, file_uuid) == [*range(6), 99], "control: both planes"

    shrunk = _index(_segments(2, marker="EDITED"), file_uuid=file_uuid)

    assert shrunk["stale_removed"] == 4, "chunks 2..5 pruned"
    assert _chunk_indexes(chunk_index, file_uuid) == [0, 1, 99], "the digest plane survived"

    svc.TranscriptIndexingService().delete_transcript_chunks(file_uuid)
    assert _chunk_indexes(chunk_index, file_uuid) == [], (
        "the per-file delete must clear EVERY plane — a digest that outlives its file is "
        "a readable summary of deleted content"
    )


def test_a_bare_doc_type_term_silently_stops_pruning_the_existing_corpus(chunk_index, monkeypatch):
    """Why the predicate carries a compat arm, on the engine, after v6 shipped.

    Every chunk written **before** v6 carries no ``doc_type``. A predicate that appends a
    bare ``{"term": {"doc_type": "chunk"}}`` matches none of them: the count gate returns
    0, the prune is skipped, and issue #400 comes back silently for the whole installed
    corpus until a full reindex has stamped the field. An explicit keyword mapping does
    nothing about it — the field is absent, not mistyped.

    The corpus here is seeded the way a pre-v6 deployment holds it (``doc_type`` stripped
    after indexing), because the current indexer stamps the field and would make the
    hazard unreachable. Failure mode, not a fix: the assertion records what a bare term
    does, and the control shows the shipped clause prunes the identical corpus correctly.
    """
    from app.services.search import indexing_service as svc

    hazard_uuid = str(uuid.uuid4())
    control_uuid = str(uuid.uuid4())
    for file_uuid in (hazard_uuid, control_uuid):
        _seed_chunks_and_a_digest(chunk_index, file_uuid, chunks=6)
        _strip_doc_type_from_chunks(chunk_index, file_uuid)

    real_query = svc.chunk_plane_query
    _patch_chunk_plane_query(monkeypatch, _unarmed_chunk_plane_query, svc, _BARE_CHUNK_CLAUSE)
    shrunk = _index(_segments(2, marker="EDITED"), file_uuid=hazard_uuid)

    assert shrunk["stale_removed"] == 0, "the bare term matched no pre-v6 chunk"
    assert _chunk_indexes(chunk_index, hazard_uuid) == [*range(6), 99], "the tail is still there"

    # Control: same corpus, same shrink, the clause actually shipped — the tail goes.
    monkeypatch.setattr(svc, "chunk_plane_query", real_query)
    _strip_doc_type_from_chunks(chunk_index, control_uuid)
    controlled = _index(_segments(2, marker="EDITED"), file_uuid=control_uuid)

    assert controlled["stale_removed"] == 4
    assert _chunk_indexes(chunk_index, control_uuid) == [0, 1, 99]


# ---------------------------------------------------------------------------
# The documented race — reachable, and it does not heal
# ---------------------------------------------------------------------------


def test_reindex_inside_the_refresh_window_leaves_the_tail_behind(chunk_index):
    """CHARACTERISATION: the count gate reads the searcher, so it can miss the tail.

    ``_prune_stale_chunks`` gates the delete on a ``count``, and a count is a search
    against the last refreshed segment. The bulk load before it uses ``refresh=False``.
    So when the same file is indexed twice before a refresh lands, the second index sees
    an empty tail, skips the delete, and the orphans stay — **permanently**: nothing
    later re-examines them, and a subsequent refresh does not trigger a prune.

    The service's docstring calls this out and judges it unreachable ("which no pipeline
    path does"). Measured on OpenSearch 3.4 at the production default
    ``refresh_interval`` of 1 s, two back-to-back ``index_transcript_chunks`` calls take
    ~150 ms and the leak reproduced in 5 of 6 attempts. This test pins
    ``refresh_interval`` open instead of racing a 1 s clock, so it states the mechanism
    rather than the timing — and the control below proves the mechanism *is* visibility,
    not the predicate.

    If a future change closes the race (refreshing before the gate, or dropping the gate),
    this test fails. That is the intended signal: update it, and delete the caveat in
    ``_prune_stale_chunks``'s docstring.
    """
    from app.core.config import settings

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    chunk_index.indices.put_settings(index=index_name, body={"index": {"refresh_interval": "-1"}})

    leaked_uuid = str(uuid.uuid4())
    first = _index(_segments(8, marker="ORIGINAL"), file_uuid=leaked_uuid)
    second = _index(_segments(3, marker="EDITED"), file_uuid=leaked_uuid)

    assert first["chunk_count"] == 8
    assert second["chunk_count"] == 3
    assert second["stale_removed"] == 0, "the gate saw an unrefreshed, empty tail"

    surviving = _docs(chunk_index, leaked_uuid)  # _docs refreshes before reading
    assert [int(doc["chunk_index"]) for doc in surviving] == list(range(8))
    assert any("ORIGINAL" in doc["content"] for doc in surviving), "stale text is searchable"

    # It does not heal: the refresh above made the tail visible, but nothing prunes it
    # until the file is indexed again.
    assert _chunk_indexes(chunk_index, leaked_uuid) == list(range(8))
    healed = _index(_segments(3, marker="EDITED"), file_uuid=leaked_uuid)
    assert healed["stale_removed"] == 5
    assert _chunk_indexes(chunk_index, leaked_uuid) == [0, 1, 2]

    # Control: identical calls with a refresh in between prune correctly, so the cause
    # is searcher visibility and nothing else about the second index.
    clean_uuid = str(uuid.uuid4())
    _index(_segments(8, marker="ORIGINAL"), file_uuid=clean_uuid)
    chunk_index.indices.refresh(index=index_name)
    controlled = _index(_segments(3, marker="EDITED"), file_uuid=clean_uuid)

    assert controlled["stale_removed"] == 5
    assert _chunk_indexes(chunk_index, clean_uuid) == [0, 1, 2]
