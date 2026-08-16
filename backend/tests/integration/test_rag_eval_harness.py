"""The eval harness against a REAL OpenSearch holding a REAL injected corpus.

The metric and qrels tests are pure logic and need nothing running. These cover
the parts that only exist against a cluster: force-merge, reading chunk documents
back, and driving the production chat retriever over them.

There is no stand-in. Without a reachable cluster **that has an eval corpus
injected**, every test here skips with the reason — a fake index would prove the
fake behaves, and the specific failures this guards (an id shape that no longer
matches the qrels, a gold span that maps to no chunk) can only happen against
documents the production indexer wrote.

    ./opentr.sh start dev --fresh rag403 --port-offset 100
    ./scripts/inject-eval-corpus.sh --fresh rag403 --corpus qmsum --limit 3
    OPENSEARCH_PORT=5280 pytest tests/integration/test_rag_eval_harness.py -m integration
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip(
    "pytrec_eval",
    reason="pytrec_eval_terrier is eval-only (licence); pip install -r requirements-eval.txt",
)

MANIFEST_DIR = Path(__file__).resolve().parents[3] / ".rag-403" / "injections" / "qmsum"


@pytest.fixture(scope="module")
def injected():
    """Manifest + live chunk documents, or an explicit skip naming what is missing."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from tests.eval.harness import corpora
    from tests.eval.harness import index_reader

    if not (MANIFEST_DIR / "manifest.json").is_file():
        pytest.skip(f"no injection manifest at {MANIFEST_DIR} — inject a corpus first")

    client = get_opensearch_client()
    if client is None:
        pytest.skip("no OpenSearch client — the stack is not reachable")

    corpus = corpora.load_manifest(MANIFEST_DIR)
    turns = corpora.load_turns(MANIFEST_DIR)
    index_reader.prepare_index(client, settings.OPENSEARCH_CHUNKS_INDEX)
    chunks = index_reader.fetch_chunks(client, settings.OPENSEARCH_CHUNKS_INDEX, corpus.file_uuids)
    if not chunks:
        pytest.skip(
            "the manifest's files are not in this cluster's chunk index — "
            "this stack was not the injection target"
        )
    return corpus, turns, chunks


def test_indexed_chunks_carry_what_the_qrels_join_on(injected):
    """Chunk ids, speakers and times are the join keys; a shape change breaks scoring."""
    _corpus, _turns, chunks = injected
    sample = next(iter(chunks.values()))

    assert sample, "a file is present in the index with zero chunks"
    for chunk in sample[:20]:
        assert chunk.doc_id == f"{chunk.file_uuid}_{chunk.chunk_index}"
        assert chunk.speaker, "chunk has no speaker — the qrels' speaker restriction is dead"
        assert chunk.end_time >= chunk.start_time


def test_gold_spans_map_onto_indexed_chunks(injected):
    """The end-to-end claim of the qrels builder, against real chunk boundaries."""
    from tests.eval.harness import corpora
    from tests.eval.harness.qrels import QrelsBuilder

    corpus, turns, chunks = injected
    queries = [
        query
        for query in corpora.load_qmsum_queries(corpus)
        if all(span.file_uuid in chunks for span in query.spans)
    ]
    if not queries:
        pytest.skip("no QMSum queries whose gold files are all present in this index")

    builder = QrelsBuilder(turns, chunks)
    judged = {query.query_id: builder.judgements(list(query.spans)) for query in queries[:25]}
    non_empty = {qid: docs for qid, docs in judged.items() if docs}

    assert non_empty, "every gold span mapped to zero chunks — the turn->chunk join is broken"
    for docs in non_empty.values():
        assert set(docs.values()) <= {1, 2}
        for doc_id in docs:
            file_uuid, _, index = doc_id.rpartition("_")
            assert file_uuid in chunks
            assert index.isdigit()


def test_the_chat_retriever_and_the_metric_engine_meet(injected):
    """One end-to-end pass: retrieve -> normalise -> score, on real documents."""
    from tests.eval.harness import corpora
    from tests.eval.harness import metrics as metrics_mod
    from tests.eval.harness import runner as runner_mod
    from tests.eval.harness.qrels import QrelsBuilder

    corpus, turns, chunks = injected
    builder = QrelsBuilder(turns, chunks)
    queries = []
    qrels = {}
    for query in corpora.load_qmsum_queries(corpus):
        if not all(span.file_uuid in chunks for span in query.spans):
            continue
        judged = builder.judgements(list(query.spans))
        if judged:
            queries.append(query)
            qrels[query.query_id] = judged
        if len(queries) == 5:
            break
    if not queries:
        pytest.skip("no judgeable QMSum queries against this index")

    run = runner_mod.execute(
        queries,
        user_id=_owner_id(corpus),
        config=runner_mod.RetrievalConfig(size=20, workers=2),
        progress_every=0,
    )
    assert set(run) == {query.query_id for query in queries}

    result = metrics_mod.evaluate(qrels, run)
    assert result.query_count == len(qrels)
    assert set(result.per_query) == set(qrels)
    for row in result.per_query.values():
        assert set(row) == set(metrics_mod.MEASURES)
        assert all(0.0 <= value <= 1.0 for value in row.values())


def _owner_id(corpus) -> int:
    """The user the corpus was injected as — retrieval is filtered by it.

    Skips rather than returns None: a retrieval run under the wrong user id
    returns nothing and would score a clean, meaningless zero.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.db.base import engine
    from app.models.media import MediaFile

    with Session(engine) as db:
        rows = db.execute(
            select(MediaFile.user_id).where(MediaFile.uuid.in_(corpus.file_uuids[:5]))
        ).all()
    if not rows:
        pytest.skip("the corpus owner is not resolvable on this stack's database")
    return int(rows[0][0])
