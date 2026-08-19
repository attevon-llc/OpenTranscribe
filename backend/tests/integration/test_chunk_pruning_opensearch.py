"""The stale-chunk-tail prune (issue #400), executed by a real OpenSearch.

``tests/unit/test_search_chunk_pruning.py`` covers the same fix against an in-memory
stand-in. That stand-in is careful — it evaluates the real query bodies and *raises* on
any clause it does not understand — but it can only prove that we believe OpenSearch
executes a ``bool``/``filter``/``range`` ``delete_by_query`` the way the fix assumes. It
was written that way because no cluster was reachable at the time. This module closes
that gap: same service, same query builder, a real engine underneath.

What is proven here and nowhere else:

* a shrinking re-chunk really does leave no ``{file_uuid}_{n}`` tail behind;
* the gate really finds nothing on a first index, so the hot path issues **no**
  ``delete_by_query`` — asserted from the engine's own index stats, not from a spy. It
  issues exactly one search, and that search is the ceiling lookup rather than the gate:
  it runs only after an empty probe window and can only widen the walk, never skip a
  prune (see ``test_first_index_probes_by_id_and_issues_no_delete``);
* the prune's ``term`` on ``file_uuid`` really is scoped to one file;
* the index-v6 plane split behaves on OpenSearch exactly as it does against the stand-in —
  the chunk-plane prune spares a digest, the per-file delete takes it, and a bare
  ``doc_type`` term (the mistake) stops pruning a pre-v6 corpus entirely;
* the #435 refresh-window race is **closed**, and closed by the mechanism claimed rather
  than by timing — see ``test_reindex_inside_the_refresh_window_still_prunes_the_tail``,
  which freezes the searcher outright and asserts that the searcher is blind to the tail
  while an id lookup is not. That test used to assert the opposite (the leak), and the
  11-of-12 measurement that justified changing it is quoted in its docstring.

Two things this module got wrong on its first execution, both worth keeping in front of
whoever edits it next:

* **It reasons about the chunk plane, and it must own every input that decides what is in
  the index.** It shipped with a positive ``FILE_ID`` literal, which since index v6 is fed
  to a digest generator that resolves it against Postgres — and on the Stage-3 stack that
  id was a real recording, so a stranger's five-section digest landed in this module's
  index and broke six of its assertions. The sharper half of the finding is the other
  direction: **on a machine without that row the whole suite would have passed while
  proving less.** A suite whose outcome depends on database content it does not own is not
  a gate, and it fails *green* for everyone else. Hence the negative id at :data:`FILE_ID`;
  do not "simplify" it back to a positive one.
* **Anything that reads back what a bulk load just wrote must refresh first.** The bulk
  load uses ``refresh=False`` and this module's own
  ``test_reindex_inside_the_refresh_window_leaves_the_tail_behind`` is *about* that — yet
  ``_strip_doc_type_from_chunks`` selected through an unrefreshed searcher, so the "pre-v6
  corpus" it claims to construct sometimes never existed. Measured: 1 failure in 8 runs
  without the refresh, 13 of 13 clean with it.

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

#: **Negative on purpose, and it is a correctness property of this module.**
#:
#: ``index_transcript_chunks`` also writes the digest plane, and ``_index_digest_plane``
#: resolves this id against Postgres in its own session. A positive literal is a bet that
#: no ``media_file`` has that id — and the bet was lost: this suite shipped with
#: ``FILE_ID = 400``, and on the Stage-3 stack row 400 is a real 690-segment recording, so
#: every ``_index()`` here read a stranger's transcript, built its five-section digest and
#: wrote it into this module's throwaway index. ``_docs`` filters on ``file_uuid`` alone, so
#: the chunk-index assertions saw ``[-5, -4, -3, -2, -1, 0, 1, …]`` and the count-gate test
#: saw a second search (the digest plane's own prune gate). Six of this module's tests
#: failed for that reason and none of them for the reason they were written to check —
#: and on any stack without that row they would all have passed while proving less.
#:
#: ``media_file.id`` is a positive serial, so a negative id CANNOT resolve:
#: ``generate_file_artifacts`` returns ``None``, the digest plane is empty by construction
#: rather than by luck, and this module's outcome no longer depends on what happens to be
#: in the database. Nothing is mocked to achieve it. ``_index`` asserts the emptiness so the
#: precondition is checked rather than assumed, and the digest plane's own behaviour is
#: covered by ``test_digest_plane_opensearch.py``, which owns a real file.
FILE_ID = -400


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
    assert result["digest_sections"] == 0, (
        "a digest was generated for FILE_ID — this module reasons about the chunk plane "
        "only, and its chunk_index and search-counter assertions are wrong if the digest "
        "plane is also being written. See the FILE_ID comment."
    )
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


def _plant(client, file_uuid: str, indexes: list[int], *, marker: str) -> None:
    """Write documents at exactly *indexes*, so a HOLE can be constructed on purpose.

    ``_index`` can only produce a contiguous ``0..n-1``; a partially failed bulk
    load cannot be provoked through the service, so the state it leaves is built
    directly here. ``_extract_failed_docs`` drops permanently-failed documents, so
    a real hole looks exactly like this.
    """
    from app.core.config import settings

    for n in indexes:
        client.index(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            id=f"{file_uuid}_{n}",
            body={
                "file_id": FILE_ID,
                "file_uuid": file_uuid,
                "user_id": USER_ID,
                "chunk_index": n,
                "content": f"{marker} chunk {n}",
                "title": "Quarterly planning",
                "speaker": "Speaker 0",
                "speakers": ["Speaker 0"],
                "doc_type": "chunk",
                "accessible_user_ids": [USER_ID],
                "start_time": float(n * 10),
                "end_time": float(n * 10 + 10),
            },
        )
    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)


def _counters(client) -> dict[str, int]:
    """Engine-side counters that reveal what the service actually issued.

    The three are chosen so the gate's *mechanism* is observable, not just its
    outcome, and without patching anything on the client:

    * ``search.query_total`` — searcher-dependent work. The prune gate used to be a
      ``count``, which is a search, and a search sees only what the last refresh
      made visible. That is issue #435. After the fix the hot path must issue
      **none**.
    * ``get.total`` — realtime, translog-reading work, one per id in an ``mget``
      probe. This is what "the gate still ran" looks like, so "no search" cannot be
      confused with "no gate".
    * ``indexing.delete_total`` — documents actually removed.
    """
    from app.core.config import settings

    primaries = client.indices.stats(index=settings.OPENSEARCH_CHUNKS_INDEX)["indices"][
        settings.OPENSEARCH_CHUNKS_INDEX
    ]["primaries"]
    return {
        "searches": int(primaries["search"]["query_total"]),
        "gets": int(primaries["get"]["total"]),
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


def test_first_index_probes_by_id_and_issues_no_delete(chunk_index):
    """Nothing to prune: one realtime id probe, one ceiling lookup, and no delete.

    Read from the engine's own counters rather than a spy on the client, because the
    claim under test is about what OpenSearch was asked to do — and since #435 the
    *kind* of request is the point, not only the count of them. The GATE used to be a
    ``count``; a count is a search, and a search sees only what the last refresh made
    visible, which is the whole bug. It is now an ``mget``, which reads the translog.

    ⚠️ This asserted **zero** searches until the #400 follow-up, and the one search it
    now allows is deliberately NOT a gate. ``_probe_ceiling`` runs only *after* the id
    probe came back empty, and its answer can only make the walk look **further** —
    it can never cause a prune to be skipped, which is the failure mode #435 is about.
    It exists because an empty window did not mean the tail had ended: a partially
    failed bulk load leaves a hole, and stopping at the first empty window stranded
    everything above a hole of 64 or more, permanently.

    Cost, measured rather than assumed: the ceiling aggregation is **2.74 ms median /
    2.97 ms p95** against the live 11,430-document index, on the branch where the
    first window is empty. That is ~1.2 s added to a 432-file reindex that takes
    182 s. The alternative the gate still avoids — refreshing before the gate — was
    measured at **95 ms median per file**, i.e. +41 to +79 s on the same run.

    ``delete_by_query`` remains the expensive thing the gate exists to avoid on the
    path every completed transcription takes — it forces a whole-index refresh.
    """
    from app.services.search.indexing_service import _ORPHAN_PROBE_WINDOW
    from app.services.search.indexing_service import chunk_plane_query

    file_uuid = str(uuid.uuid4())

    before = _counters(chunk_index)
    first = _index(_segments(5, marker="FIRST"), file_uuid=file_uuid)
    after = _index_counters_delta(chunk_index, before)

    assert first["stale_removed"] == 0
    assert after["searches"] == 1, (
        "the GATE must stay an id probe; the one permitted search is the ceiling "
        "lookup, which runs only after an empty window and can only widen the walk. "
        "More than one means something searcher-dependent crept back into the gate"
    )
    assert after["gets"] == _ORPHAN_PROBE_WINDOW, (
        "...and the gate DID run: one probe window of realtime id lookups. Without "
        "this, the search count alone would also be satisfied by a deleted gate"
    )
    assert after["deleted"] == 0

    # There is genuinely no tail — asserted through the searcher, which agrees with
    # the probe here because a refresh has landed. The two disagreeing is #435, and
    # the test below is where that is pinned.
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
    assert delta["searches"] == 1, "one ceiling lookup, not a searcher-dependent gate"
    assert delta["gets"] == _ORPHAN_PROBE_WINDOW
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
    """Make this file's chunk documents look the way a pre-v6 index holds them.

    The refresh is load-bearing, not defensive. ``update_by_query`` selects through a
    **search**, and the bulk load that wrote these chunks used ``refresh=False`` — so
    without it this helper matches zero documents, strips nothing, and silently leaves a
    fully v6 corpus behind. That is exactly how it shipped, and it made
    ``test_a_bare_doc_type_term_silently_stops_pruning_the_existing_corpus`` *intermittent*
    rather than simply broken — measured at 1 failure in 8 runs. When it fires, the bare
    term is being measured against documents that all still carry ``doc_type: chunk``: the
    term matches them, four chunks are pruned, and the hazard the test exists to
    characterise was never constructed at all. The same searcher-visibility mechanic this
    module characterises in
    ``test_reindex_inside_the_refresh_window_leaves_the_tail_behind``.
    """
    from app.core.config import settings

    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
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
# The race that used to be here — closed, and pinned closed
# ---------------------------------------------------------------------------


def test_reindex_inside_the_refresh_window_still_prunes_the_tail(chunk_index):
    """The #435 fix: the gate is realtime, so a frozen searcher does not hide the tail.

    **What this test used to assert, and why it changed.** ``_prune_stale_chunks``
    gated its delete on a ``count``. A count is a search against the last refreshed
    segment, and the bulk load before it uses ``refresh=False`` — so when the same
    file was indexed twice before a refresh landed, the second index saw an empty
    tail, skipped the delete, and the orphans stayed **permanently**: nothing later
    re-examined them, and a subsequent refresh triggered no prune. This module
    characterised that as a passing test, and the service's docstring called it
    unreachable ("which no pipeline path does").

    Both parts of that were wrong, and measured rather than argued. At this stack's
    production ``refresh_interval`` a back-to-back pair of ``index_transcript_chunks``
    calls completes in 125–224 ms and **the stale tail survived 11 of 12 pairs** —
    the window is not narrow. And nothing serialises the callers: five dispatch sites
    reach the single-file indexing task, including a user-triggered reprocess with no
    in-flight check and a recovery sweep that branches on the index record being
    *missing* rather than on the original task having stopped. Issue #435.

    **What closed it.** The gate is now an ``mget`` over the ids at or above
    ``keep_count``. Id lookups are realtime — they read the translog — so the probe
    sees the tail the instant the bulk load returns, refresh or no refresh. The
    ``delete_by_query`` that follows still needs a searcher, so the prune refreshes
    first; that refresh sits behind the gate and a full reindex therefore pays none
    of it (measured: ~95 ms median per file if it were unconditional, +41 s on a
    432-file run).

    This test pins ``refresh_interval`` open rather than racing a 1 s clock, which
    makes it a **harsher** condition than production, not a weaker one: the searcher
    is frozen for the whole test instead of for a few hundred milliseconds.
    """
    from app.core.config import settings
    from app.services.search.indexing_service import chunk_plane_query

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    chunk_index.indices.put_settings(index=index_name, body={"index": {"refresh_interval": "-1"}})

    file_uuid = str(uuid.uuid4())
    first = _index(_segments(8, marker="ORIGINAL"), file_uuid=file_uuid)
    assert first["chunk_count"] == 8

    # THE CONTROL, and the reason this test can still fail. The searcher must be
    # genuinely blind right here, or everything below passes for the wrong reason.
    # This is the old gate's own predicate, evaluated by the real searcher, at the
    # exact moment the old gate would have evaluated it: it finds nothing, which is
    # why it skipped the delete and leaked. The id plane disagrees, and that
    # disagreement is the entire fix.
    blind = chunk_index.count(
        index=index_name,
        body={"query": chunk_plane_query(file_uuid, from_chunk_index=3)},
    )
    assert blind["count"] == 0, (
        "the searcher must be blind to the tail here, or the refresh window is not "
        "being held open and this test is not exercising #435 at all"
    )
    assert chunk_index.exists(index=index_name, id=f"{file_uuid}_7"), (
        "...while the same documents ARE addressable by id. Realtime vs searcher is "
        "the whole mechanism; if this ever fails, mget is not realtime and the fix "
        "rests on nothing"
    )

    second = _index(_segments(3, marker="EDITED"), file_uuid=file_uuid)

    assert second["chunk_count"] == 3
    assert second["stale_removed"] == 5, (
        "the realtime probe saw the tail the searcher could not, and the prune ran"
    )

    surviving = _docs(chunk_index, file_uuid)  # _docs refreshes before reading
    assert [int(doc["chunk_index"]) for doc in surviving] == [0, 1, 2]
    assert all("ORIGINAL" not in doc["content"] for doc in surviving), (
        "no stale text survives — there is nothing left to heal later"
    )

    # Control: the path that already worked before #435 — a refresh in between —
    # still prunes exactly the same way, so the fix did not trade one case for another.
    clean_uuid = str(uuid.uuid4())
    _index(_segments(8, marker="ORIGINAL"), file_uuid=clean_uuid)
    chunk_index.indices.refresh(index=index_name)
    controlled = _index(_segments(3, marker="EDITED"), file_uuid=clean_uuid)

    assert controlled["stale_removed"] == 5
    assert _chunk_indexes(chunk_index, clean_uuid) == [0, 1, 2]


# ---------------------------------------------------------------------------
# The probe walk must not stop at the first empty window (#400 follow-up)
# ---------------------------------------------------------------------------


def test_a_hole_larger_than_the_probe_window_does_not_strand_the_tail(chunk_index):
    """A partially failed bulk load leaves a HOLE, and the walk used to stop in it.

    ``_orphaned_document_ids`` walked in windows of ``_ORPHAN_PROBE_WINDOW`` and
    returned at the first window that found nothing. So a previous generation of
    200 chunks whose bulk load permanently failed for 10..100 — which
    ``_extract_failed_docs`` drops, by design — left documents at 0..9 and
    101..199. A re-chunk to 10 chunks then probed 10..73, found nothing, skipped
    the delete entirely, and stranded 101..199 **permanently**: nothing later
    re-examines them. That is precisely the #435 failure class, reintroduced by
    the walk's own stop condition rather than by visibility.

    The probe is only a boolean gate — the delete itself is a range over
    ``chunk_index >= keep_count`` — so finding *any* orphan above the hole is
    enough to remove all of them.
    """
    from app.services.search.indexing_service import _ORPHAN_PROBE_WINDOW

    file_uuid = str(uuid.uuid4())
    survivors = list(range(_ORPHAN_PROBE_WINDOW + 37, _ORPHAN_PROBE_WINDOW + 60))
    _plant(chunk_index, file_uuid, [*range(10), *survivors], marker="PREVIOUS")

    assert survivors[0] - 10 > _ORPHAN_PROBE_WINDOW, (
        "control: the hole must exceed one probe window, or the walk never stops in it"
    )
    assert set(_chunk_indexes(chunk_index, file_uuid)) == {*range(10), *survivors}

    result = _index(_segments(10, marker="CURRENT"), file_uuid=file_uuid)

    assert result["stale_removed"] == len(survivors), (
        "the walk stopped inside the hole, so everything above it was never pruned"
    )
    assert _chunk_indexes(chunk_index, file_uuid) == list(range(10))


def test_a_tail_longer_than_one_probe_window_is_pruned_whole(chunk_index):
    """The walk must cover a tail bigger than a single window.

    Every existing case in this module has a tail of 5 or 6 — well inside one
    window — so a walk that only ever probed once would have passed all of them.
    """
    from app.services.search.indexing_service import _ORPHAN_PROBE_WINDOW

    file_uuid = str(uuid.uuid4())
    previous = _ORPHAN_PROBE_WINDOW * 2 + 5
    _plant(chunk_index, file_uuid, list(range(previous)), marker="PREVIOUS")

    result = _index(_segments(3, marker="CURRENT"), file_uuid=file_uuid)

    assert result["stale_removed"] == previous - 3
    assert _chunk_indexes(chunk_index, file_uuid) == [0, 1, 2]


def test_a_transcript_that_now_chunks_to_nothing_loses_its_old_chunks(chunk_index):
    """Segments that all chunk away must not leave the previous generation behind.

    ``index_transcript_chunks`` returned early on ``not chunks`` without pruning,
    and ``reindex_transcript`` — which deletes first — is NOT the primary path:
    ``tasks/search_indexing_task`` calls this method directly. The ``not segments``
    guard above it does not catch this, because the segments exist; they just
    produce no chunk.
    """
    from app.services.search.indexing_service import TranscriptIndexingService

    file_uuid = str(uuid.uuid4())
    _plant(chunk_index, file_uuid, list(range(6)), marker="PREVIOUS")

    # Called directly rather than through `_index`: this branch returns the int 0
    # rather than the success dict, which is exactly the early return under test.
    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=FILE_ID,
        file_uuid=file_uuid,
        user_id=USER_ID,
        segments=[{"start": 0.0, "end": 1.0, "text": "   ", "speaker": "Speaker 0"}],
        title="Quarterly planning",
        speakers=["Speaker 0"],
        tags=[],
    )

    # The control, and it now asserts the REASON as well as the count (issue #495).
    # `index_transcript_chunks` used to answer a bare `0` here — and also on a dead
    # OpenSearch, and on any swallowed exception, since it caught everything and
    # returned the same `0`. `result == 0` therefore passed whether this input really
    # chunked to nothing or the indexer had simply broken. Naming the reason is what
    # makes it a control rather than a coincidence.
    assert result["chunk_count"] == 0, "control: this input really does chunk to nothing"
    assert result["reason"] == "no_chunks_generated", (
        f"zero chunks for the wrong reason — the indexer may have failed: {result}"
    )
    assert _chunk_indexes(chunk_index, file_uuid) == [], (
        "the old chunks are still searchable for a transcript that no longer has any"
    )
