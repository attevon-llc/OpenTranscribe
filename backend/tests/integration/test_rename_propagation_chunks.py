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


def test_speaker_rename_is_built_through_chunk_plane_query(chunk_index, monkeypatch):
    """Stage 3's ``doc_type`` predicate goes in ONE place and must reach this rewrite.

    ``chunk_plane_query`` is the single point where the chunk-plane predicate is
    built (issue #400). A rename that hand-rolled its own body would keep working
    today and silently rewrite per-file digests.

    Speakers stay chunk-plane-only on purpose: a digest carries no ``speaker``
    field at all (``build_digest_documents`` pops it, so digests stay out of the
    speaker facet and out of chat's speaker-scoped ``terms`` filter), and its
    prose bakes the old name where no ``update_by_query`` can reach. Rewriting
    the roster alone would half-correct the document and hide that regeneration
    (#383 addendum G1) is the real fix.
    """
    from app.services.search import indexing_service as svc
    from app.tasks import rename_propagation_task as task_module

    file_uuid = str(uuid.uuid4())
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

    by_index = {d["chunk_index"]: d for d in _sources(chunk_index, file_uuid)}
    assert by_index[0]["speaker"] == NEW_NAME
    assert by_index[99]["speaker"] == OLD_NAME, "the digest plane was excluded, not rewritten"


def test_title_rename_covers_the_digest_plane_too(chunk_index):
    """A digest inherits ``title`` and renders it as a citation, so it must follow.

    ``build_digest_documents`` copies ``base_metadata`` (which carries ``title``)
    onto every digest section; ``chunk_retrieval._digest_hit`` reads it and
    ``chat/citations`` renders it. Scoping the title rewrite to the chunk plane
    therefore let a single answer cite the same recording under **two different
    names** — the new title from a chunk, the old one from a digest.

    Unlike the speaker roster, a title is metadata rather than derived prose, so
    rewriting it leaves the digest internally consistent. That asymmetry is why
    this task uses ``file_plane_query`` and its sibling above does not.
    """
    from app.tasks import rename_propagation_task as task_module

    file_uuid = str(uuid.uuid4())
    _index_docs(
        chunk_index,
        [
            {**_doc(file_uuid, 0, OLD_NAME, [OLD_NAME], "chunk plane"), "doc_type": "chunk"},
            {**_doc(file_uuid, 99, OLD_NAME, [OLD_NAME], "digest prose"), "doc_type": "digest"},
        ],
    )

    result = task_module.propagate_title_rename(file_uuid, "New title")

    assert result["updated"] == 2, (
        "the title rewrite reached only one plane; a digest citation keeps the old title"
    )
    titles = {doc["doc_type"]: doc["title"] for doc in _sources(chunk_index, file_uuid)}
    assert titles == {"chunk": "New title", "digest": "New title"}


# --------------------------------------------------------------------------- #
# The roster must stay SORTED (issue #455 via #405 follow-up)                  #
# --------------------------------------------------------------------------- #


def test_the_speakers_roster_is_still_sorted_after_a_rename(chunk_index):
    """``search_indexing_task`` writes ``speakers`` sorted, deliberately.

    #455 established that an unsorted roster changed the EMBEDDINGS, because the
    list is part of every chunk document. The painless rebuild preserves
    positional order, so renaming Bob -> Zoe turned ``["Bob", "Zed"]`` into
    ``["Zoe", "Zed"]`` — a document a reindex would never produce, and the
    invariant #455 exists to hold silently broken by the rename path.
    """
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    file_uuid = str(uuid.uuid4())
    _index_docs(chunk_index, [_doc(file_uuid, 0, "Bob", ["Bob", "Zed"], "sorted roster")])

    propagate_speaker_rename(file_uuid, ["Bob"], "Zoe")

    roster = _sources(chunk_index, file_uuid)[0]["speakers"]
    assert roster == ["Zed", "Zoe"], (
        f"the roster is no longer sorted, so a reindex would produce a different document: {roster}"
    )


# --------------------------------------------------------------------------- #
# Two renames, no ordering guarantee (the inversion this dispatch model allows) #
# --------------------------------------------------------------------------- #


def test_out_of_order_renames_converge_on_the_current_name(chunk_index, db_session, monkeypatch):
    """``A -> B`` then ``B -> C`` must not leave the index on B.

    Both are independent tasks on an 8-way cpu queue with no ordering control. If
    ``B -> C`` runs first it matches nothing and succeeds; the ``A -> B`` task
    then used to write **B**, so Postgres said C while the index said B and chat's
    exact ``terms`` filter on C returned zero chunks — #405's own bug, recreated
    by #405's dispatch model.

    The task now re-reads the speaker's current name at run time, so either order
    converges. This drives the worse order deliberately.
    """
    from app.tasks import rename_propagation_task as task_module

    file_uuid = str(uuid.uuid4())
    _index_docs(chunk_index, [_doc(file_uuid, 0, "A", ["A"], "spoken by A")])

    # Postgres has already been renamed twice; only the propagation is in flight.
    monkeypatch.setattr(task_module, "_current_speaker_name", lambda _sid: "C")

    # The LOSER runs first and legitimately matches nothing.
    late = task_module.propagate_speaker_rename(file_uuid, ["B"], "C", speaker_id=7)
    assert late["updated"] == 0, "control: B -> C cannot match an index still holding A"

    # The stale task now arrives carrying the superseded 'B'.
    task_module.propagate_speaker_rename(file_uuid, ["A"], "B", speaker_id=7)

    speaker = _sources(chunk_index, file_uuid)[0]["speaker"]
    assert speaker == "C", (
        f"the index settled on the superseded name {speaker!r}; chat's terms filter on 'C' "
        "would return zero chunks for this file"
    )


def test_without_a_speaker_id_the_dispatched_name_is_still_used(chunk_index):
    """A task queued by an older build has no ``speaker_id`` and must still work.

    The re-resolution is an improvement, not a precondition — making it mandatory
    would strand every task already on the queue at deploy time.
    """
    from app.tasks.rename_propagation_task import propagate_speaker_rename

    file_uuid = str(uuid.uuid4())
    _index_docs(chunk_index, [_doc(file_uuid, 0, "A", ["A"], "spoken by A")])

    propagate_speaker_rename(file_uuid, ["A"], "B")

    assert _sources(chunk_index, file_uuid)[0]["speaker"] == "B"


def test_a_deleted_speaker_falls_back_to_the_dispatched_name(chunk_index, monkeypatch):
    """A stale rewrite beats a stale index.

    If the speaker row is gone by the time the task runs, re-resolution returns
    ``None``. Skipping the rewrite would leave the pre-rename name in the index
    forever; the dispatched name is at least closer to the truth.
    """
    from app.tasks import rename_propagation_task as task_module

    file_uuid = str(uuid.uuid4())
    _index_docs(chunk_index, [_doc(file_uuid, 0, "A", ["A"], "spoken by A")])

    monkeypatch.setattr(task_module, "_current_speaker_name", lambda _sid: None)
    task_module.propagate_speaker_rename(file_uuid, ["A"], "B", speaker_id=999_999)

    assert _sources(chunk_index, file_uuid)[0]["speaker"] == "B"


def test_a_title_rename_also_re_resolves_the_current_title(chunk_index, monkeypatch):
    """Same inversion, same fix, on the title path."""
    from app.tasks import rename_propagation_task as task_module

    file_uuid = str(uuid.uuid4())
    _index_docs(chunk_index, [_doc(file_uuid, 0, OLD_NAME, [OLD_NAME], "body")])

    monkeypatch.setattr(task_module, "_current_file_title", lambda _uuid: "Third title")
    task_module.propagate_title_rename(file_uuid, "Second title")

    assert _sources(chunk_index, file_uuid)[0]["title"] == "Third title"


# --------------------------------------------------------------------------- #
# Version conflicts are counted and retried, not silently dropped              #
# --------------------------------------------------------------------------- #


def test_version_conflicts_are_retried_rather_than_reported_as_success(chunk_index, monkeypatch):
    """``conflicts="proceed"`` does not mean the skipped documents were handled.

    It means "do not abort the whole update_by_query". Nothing re-examines the
    skipped documents, so a concurrent title+speaker rename over one file left a
    SUBSET of its chunks carrying the old value while the task returned
    ``status: success``. The count was never read.
    """
    from app.tasks import rename_propagation_task as task_module

    file_uuid = str(uuid.uuid4())
    _index_docs(chunk_index, [_doc(file_uuid, 0, OLD_NAME, [OLD_NAME], "body")])

    retries: list[str] = []

    def _record_retry(*args, **kwargs):
        retries.append("retried")
        raise task_module.propagate_speaker_rename.MaxRetriesExceededError()

    monkeypatch.setattr(task_module.propagate_speaker_rename, "retry", _record_retry)

    real_ubq = chunk_index.update_by_query

    def _with_conflicts(*args, **kwargs):
        response = real_ubq(*args, **kwargs)
        response["version_conflicts"] = 3
        return response

    monkeypatch.setattr(chunk_index, "update_by_query", _with_conflicts)

    result = task_module.propagate_speaker_rename(file_uuid, [OLD_NAME], NEW_NAME)

    assert retries == ["retried"], "a version conflict did not trigger a retry"
    assert result["version_conflicts"] == 3, (
        "the conflict count is not reported, so an operator cannot tell a partial "
        "rewrite from a complete one"
    )
