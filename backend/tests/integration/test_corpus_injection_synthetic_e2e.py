"""Synthetic tier end to end — real Postgres, real OpenSearch (issue #403).

The unit suite proves the adapter parses the generator's format and that gold
turn ranges map onto chunks the production *chunker* produces. It cannot prove
the link that actually matters for a measurement: that those chunks are the ones
**OpenSearch has**, read back through the harness's own reader, and that a gold
span therefore becomes a judgement against a document a retriever can return.

So this file runs the whole chain — adapter -> rows -> the production indexing
task -> ``index_reader.fetch_chunks`` -> ``QrelsBuilder`` — and **skips loudly**
when no cluster is reachable rather than substituting a stand-in under the same
test id.

Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 MINIO_PORT=5278 \\
        pytest backend/tests/integration/test_corpus_injection_synthetic_e2e.py -m integration

Every artefact is written under a per-run random ``seed``, so this cannot
collide with — or clean up — a real injection sitting on the same stack.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fixtures.synthetic_corpus import write_fixture_corpus
from sqlalchemy import func
from sqlalchemy import select

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.scripts.corpus_injection import ids
from app.scripts.corpus_injection.adapters.synthetic import SyntheticAdapter
from app.scripts.corpus_injection.injector import dispatch_indexing
from app.scripts.corpus_injection.injector import inject_meeting

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT/POSTGRES_PORT — a stand-in index cannot show that a synthetic "
            "gold span lands on a chunk the engine actually holds."
        ),
    ),
]

SEED = f"pytest-synth-{uuid.uuid4().hex[:10]}"
CORPUS = "synthetic"


@pytest.fixture(scope="module")
def engine():
    from app.db.base import engine as app_engine

    return app_engine


@pytest.fixture(scope="module")
def owner_id(engine) -> int:
    from sqlalchemy.orm import Session

    from app.models.user import User

    with Session(engine) as db:
        user_id = db.scalar(select(User.id).order_by(User.id).limit(1))
    if user_id is None:
        raise pytest.skip("No user rows on the target stack; nothing can own an injected file.")
    return int(user_id)


@pytest.fixture
def session(engine):
    """A real, committing session — the indexing task opens its own."""
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        yield db


@pytest.fixture
def opensearch():
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if client is None:
        pytest.skip("OpenSearch client unavailable despite SKIP_OPENSEARCH=False.")
    return client


@pytest.fixture
def adapter(tmp_path) -> SyntheticAdapter:
    """The real adapter over a miniature corpus in the generator's format."""
    return SyntheticAdapter(write_fixture_corpus(tmp_path / "synthetic"), meetings=0)


@pytest.fixture
def injected(session, owner_id, opensearch, adapter):
    """Inject and index every fixture meeting; delete everything afterwards.

    Registered before anything is written and torn down unconditionally, so a
    failing assertion mid-test still cleans up the shared eval stack.
    """
    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService

    records = {}
    turns_by_file = {}
    for meeting_id in adapter.meeting_ids():
        record, turn_rows = inject_meeting(
            session, adapter.load(meeting_id), owner_id, seed=SEED, tool_version="pytest"
        )
        session.commit()
        dispatch_indexing(record, owner_id, mode="eager")
        records[meeting_id] = record
        turns_by_file[record.file_uuid] = turn_rows

    yield records, turns_by_file

    # Refresh BEFORE deleting: the indexer bulk-loads with refresh=false and
    # delete_by_query refreshes *after* it runs, so freshly indexed chunks are
    # otherwise invisible to the delete and survive as orphans.
    opensearch.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
    indexer = TranscriptIndexingService()
    for record in records.values():
        media_file = session.get(MediaFile, record.media_file_id)
        if media_file is not None:
            session.query(TranscriptSegment).filter(
                TranscriptSegment.media_file_id == media_file.id
            ).delete()
            session.query(Speaker).filter(Speaker.media_file_id == media_file.id).delete()
            session.delete(media_file)
            session.commit()
        indexer.delete_transcript_chunks(record.file_uuid)


def _fetch_chunks(client, file_uuids):
    """Read chunks back the way the harness does — refresh, merge, then read."""
    from app.core.config import settings
    from tests.eval.harness import index_reader

    index_reader.prepare_index(client, settings.OPENSEARCH_CHUNKS_INDEX)
    return index_reader.fetch_chunks(client, settings.OPENSEARCH_CHUNKS_INDEX, sorted(file_uuids))


class TestTheSyntheticCorpusReachesTheIndex:
    def test_every_injected_meeting_produces_chunks(self, injected, opensearch, adapter):
        records, _ = injected
        chunks = _fetch_chunks(opensearch, [r.file_uuid for r in records.values()])

        assert len(records) == 8
        assert sorted(chunks) == sorted(r.file_uuid for r in records.values())
        empty = sorted(uuid_ for uuid_, docs in chunks.items() if not docs)
        assert empty == []

    def test_the_row_declares_its_timestamps_are_not_measurements(self, injected, session):
        records, _ = injected
        media_file = session.get(MediaFile, records["T000-S0-0000"].media_file_id)
        block = media_file.metadata_important["rag_eval"]

        assert block["timings_are_measurements"] is False
        assert block["timing_source"] == "synthetic"
        assert block["synthetic_timing_params"] == {"generator": "corpus_supplied_v1"}
        assert block["corpus_file_uuid"] == "corpusuuid-T000-S0-0000"

        words = session.execute(
            select(TranscriptSegment.words).where(TranscriptSegment.media_file_id == media_file.id)
        ).all()
        assert words, "no segments were written at all"
        assert all(row[0] is None for row in words)

    def test_the_segments_survive_the_ddl_only_uniqueness_constraint(self, injected, session):
        """``UNIQUE(media_file_id, start_time, end_time, md5(text))``, for real.

        A turn-per-segment corpus is the shape that collides on it; the insert
        aborting is the only way this is ever observed.
        """
        records, _ = injected
        counts = [
            session.scalar(
                select(func.count(TranscriptSegment.id)).where(
                    TranscriptSegment.media_file_id == record.media_file_id
                )
            )
            for record in records.values()
        ]
        assert counts == [8] * 8


class TestGoldSpansBecomeJudgementsOverRealChunks:
    def test_a_multi_file_gold_set_judges_chunks_in_every_gold_file(
        self, injected, opensearch, adapter
    ):
        """The multi-file class, end to end. Nothing else in #403 measures it."""
        from tests.eval.harness import corpora
        from tests.eval.harness.qrels import GoldSpan
        from tests.eval.harness.qrels import QrelsBuilder

        records, turns_by_file = injected
        builder = QrelsBuilder(_turn_rows(turns_by_file), _fetch_chunks(opensearch, turns_by_file))

        spans = [
            GoldSpan(str(ids.file_uuid(CORPUS, "T000-S0-0000", SEED)), 2, 3),
            GoldSpan(str(ids.file_uuid(CORPUS, "T000-S0-0001", SEED)), 4, 4),
        ]
        judged = builder.judgements(spans)

        assert judged, "a gold turn range produced no judgement over real chunks"
        assert {doc_id.rsplit("_", 1)[0] for doc_id in judged} == {span.file_uuid for span in spans}
        assert max(judged.values()) == 2
        assert corpora.MULTI_FILE == "multi_file"

    def test_a_gold_span_outside_the_corpus_judges_nothing(self, injected, opensearch):
        """The control: without it, "non-empty" would pass on any judgement at all."""
        from tests.eval.harness.qrels import GoldSpan
        from tests.eval.harness.qrels import QrelsBuilder

        _, turns_by_file = injected
        builder = QrelsBuilder(_turn_rows(turns_by_file), _fetch_chunks(opensearch, turns_by_file))
        absent = str(ids.file_uuid(CORPUS, "T099-S9-9999", SEED))

        assert builder.judgements([GoldSpan(absent, 0, 3)]) == {}

    def test_the_indexed_chunk_text_is_the_generators_own_turn_content(self, injected, opensearch):
        from app.core.config import settings

        records, _ = injected
        file_uuid = records["T001-S0-0002"].file_uuid
        opensearch.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
        hits = opensearch.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body={"query": {"term": {"file_uuid": file_uuid}}, "size": 100},
        )["hits"]["hits"]
        text = " ".join(hit["_source"]["content"] for hit in hits)

        assert hits
        assert "T001-S0-0002 turn 3" in text
        assert {hit["_source"]["speaker"] for hit in hits} >= {"Ada Vance", "Bo Ruiz"}


class TestIdempotency:
    def test_a_second_run_creates_nothing_and_re_indexes_nothing_new(
        self, injected, session, owner_id, opensearch, adapter
    ):
        records, _ = injected
        before = _fetch_chunks(opensearch, [r.file_uuid for r in records.values()])

        second = {}
        for meeting_id in adapter.meeting_ids():
            record, _ = inject_meeting(
                session, adapter.load(meeting_id), owner_id, seed=SEED, tool_version="pytest"
            )
            session.commit()
            second[meeting_id] = record

        assert [r.action for r in second.values()] == ["skipped"] * 8
        assert [r.file_uuid for r in second.values()] == [r.file_uuid for r in records.values()]
        assert [r.media_file_id for r in second.values()] == [
            r.media_file_id for r in records.values()
        ]

        file_rows = session.scalar(
            select(func.count(MediaFile.id)).where(
                MediaFile.uuid.in_([uuid.UUID(r.file_uuid) for r in records.values()])
            )
        )
        after = _fetch_chunks(opensearch, [r.file_uuid for r in records.values()])
        assert file_rows == 8
        assert {k: len(v) for k, v in after.items()} == {k: len(v) for k, v in before.items()}


def _turn_rows(turns_by_file):
    """Manifest turn rows -> the harness's ``TurnRow`` objects."""
    from tests.eval.harness.qrels import TurnRow

    return {
        file_uuid: [
            TurnRow(
                file_uuid=file_uuid,
                turn_index=int(row["turn_index"]),
                speaker=str(row["speaker"]),
                start=float(row["start"]),
                end=float(row["end"]),
                word_count=int(row["word_count"]),
            )
            for row in rows
        ]
        for file_uuid, rows in turns_by_file.items()
    }
