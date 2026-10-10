"""Reconciling a LARGE orphan set, against a real OpenSearch, on a throwaway index.

The beat sweep refuses to delete more than 10% of an index (``ratio_guard``) because a
Postgres restored empty looks exactly like a corpus that is mostly garbage. That left an
operator with real orphans — a restore, rows removed out-of-band — no supported way to
finish the job. The path is now: dry run (``orphan_keys`` names what would go), then a
forced run. Two properties have to hold and both are proven here against the real
cluster and the real database:

* the dry run names every orphan and nothing else;
* a FORCED run still never deletes the documents of a file that exists — including one
  that was created after the sweep took its Postgres snapshot, which is the window the
  pre-delete re-check closes. Without it, that file's documents were judged orphans
  and deleted.

Every index name ``run_orphan_cleanup`` would touch is redirected to a uuid-named
throwaway (four of them never created), so the live indices are never read or written.
"""

from __future__ import annotations

import os
import uuid as uuid_pkg
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text

from tests.integration.deletion_seed import DELRES_PREFIX
from tests.integration.deletion_seed import new_tag

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true",
        reason="needs a reachable OpenSearch (export OPENSEARCH_PORT)",
    ),
]

_ORPHANS = 30  # above the 25-document floor, and 30/33 is far above the 10% ratio


@pytest.fixture
def engine():
    from app.core.config import settings

    eng = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def existing_file(engine) -> Iterator[str]:
    """A real, committed ``media_file`` row (no media, no index documents of its own)."""
    with engine.begin() as conn:
        owner = conn.execute(text('SELECT id FROM "user" ORDER BY id LIMIT 1')).scalar_one()
        row = conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type, status) VALUES (gen_random_uuid(), :u, :f, 'none', 1, "
                "'audio/wav', 'completed') RETURNING id, uuid"
            ),
            {"u": owner, "f": f"{DELRES_PREFIX}{new_tag()}.wav"},
        ).one()
    try:
        yield str(row[1])
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM media_file WHERE id = :i"), {"i": row[0]})


@pytest.fixture
def sandbox(monkeypatch) -> Iterator[tuple[object, str]]:
    """Point every index the sweep scans at throwaway names; create only the chunks one."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.tasks import opensearch_integrity_task as sweeper

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    tag = uuid_pkg.uuid4().hex[:12]
    chunks = f"delres-reconcile-{tag}"
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", chunks)
    monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", f"delres-absent-t-{tag}")
    monkeypatch.setattr(settings, "OPENSEARCH_SUMMARY_INDEX", f"delres-absent-s-{tag}")
    monkeypatch.setattr(sweeper, "get_speaker_index", lambda: f"delres-absent-sp-{tag}")
    monkeypatch.setattr(sweeper, "get_speaker_index_v4", lambda: f"delres-absent-v4-{tag}")
    client.indices.create(
        index=chunks, body={"mappings": {"properties": {"file_uuid": {"type": "keyword"}}}}
    )
    try:
        yield client, chunks
    finally:
        client.indices.delete(index=chunks, ignore=[404])


def _index_docs(client, index: str, file_uuid: str, n: int) -> None:
    for _ in range(n):
        client.index(index=index, body={"file_uuid": file_uuid})


def _uuids_in(client, index: str) -> dict[str, int]:
    client.indices.refresh(index=index)
    resp = client.search(
        index=index,
        body={"size": 0, "aggs": {"u": {"terms": {"field": "file_uuid", "size": 1000}}}},
    )
    return {b["key"]: b["doc_count"] for b in resp["aggregations"]["u"]["buckets"]}


def test_dry_run_lists_the_orphans_and_a_forced_run_spares_a_file_that_exists(
    engine, existing_file, sandbox, monkeypatch
):
    from app.tasks import opensearch_integrity_task as sweeper

    client, chunks = sandbox
    orphans = sorted(str(uuid_pkg.uuid4()) for _ in range(_ORPHANS))
    _index_docs(client, chunks, existing_file, 3)
    for orphan in orphans:
        _index_docs(client, chunks, orphan, 1)
    client.indices.refresh(index=chunks)

    # The snapshot was taken BEFORE `existing_file` was created: it holds other rows
    # (so the empty-set refusal does not fire) but not this one.
    real_snapshot = sweeper._get_all_file_uuids_from_db()
    stale_snapshot = real_snapshot - {existing_file}
    assert stale_snapshot, "the database must hold at least one other file for this test"
    monkeypatch.setattr(sweeper, "_get_all_file_uuids_from_db", lambda: stale_snapshot)

    unforced = sweeper.run_orphan_cleanup(dry_run=False)[chunks]
    assert unforced["refused"] == "ratio_guard"
    assert unforced["deleted_docs"] == 0

    listing = sweeper.run_orphan_cleanup(dry_run=True)[chunks]
    assert listing["orphan_keys"] == orphans
    assert listing["orphan_keys_truncated"] is False
    assert listing["orphaned_docs"] == _ORPHANS
    assert listing["rechecked_present"] == 1
    assert _uuids_in(client, chunks)[existing_file] == 3, "a dry run deleted something"

    forced = sweeper.run_orphan_cleanup(dry_run=False, force=True)[chunks]
    assert forced["refused"] is None
    assert forced["deleted_docs"] == _ORPHANS
    assert _uuids_in(client, chunks) == {existing_file: 3}


def test_force_never_overrides_an_empty_postgres_side(sandbox, monkeypatch):
    """The other refusal: with NO files in the comparison, every document is an orphan."""
    from app.tasks import opensearch_integrity_task as sweeper

    client, chunks = sandbox
    _index_docs(client, chunks, str(uuid_pkg.uuid4()), 40)
    client.indices.refresh(index=chunks)
    monkeypatch.setattr(sweeper, "_get_all_file_uuids_from_db", set)

    forced = sweeper.run_orphan_cleanup(dry_run=False, force=True)[chunks]
    assert forced["refused"] == "empty_valid_set"
    assert sum(_uuids_in(client, chunks).values()) == 40
