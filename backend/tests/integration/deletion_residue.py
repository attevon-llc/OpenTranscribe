"""Find every copy of a media file's data that survives in Postgres, OpenSearch, MinIO, Redis.

Used by ``test_deletion_residue_live.py``. The scans are GENERIC on purpose — derived
from the live schema and the live index list, never from a hand-kept inventory — because
a hand-kept list is exactly what a new table or a new index plane silently falls out of.

* **Postgres** — the dependent closure of a ``media_file`` row, followed through every
  foreign key whose ``ON DELETE`` is not ``SET NULL`` (derived from ``pg_constraint``).
  Every row in that closure must be gone after a delete. ``SET NULL`` children are
  reported separately: their *row* is retained by design, but they must no longer point
  at the file. JSON columns outside the closure are text-scanned for the file's UUID,
  which is how a copy with no foreign key at all (a chat citation's snippet) is found.
* **OpenSearch** — every non-system index, queried for any document naming the file,
  its speakers, its cluster or its owner.
* **MinIO** — every bucket, listed in full, for keys under the file's prefixes.
* **Redis** — databases 0 and 1, scanned for the file's UUID and the seeded keys.

:func:`scan_residue` returns a flat list of human-readable findings; empty means clean.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Text
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.sql import column as sa_column
from sqlalchemy.sql import select as select_
from sqlalchemy.sql import table as sa_table

from tests.integration.deletion_seed import SeededFile
from tests.integration.deletion_seed import redis_client

logger = logging.getLogger(__name__)
#: JSON columns that legitimately keep a deleted file's UUID, with the reason. Anything
#: else a text scan finds is residue.
JSON_REFERENCE_EXEMPT: dict[tuple[str, str], str] = {
    # A conversation's pinned retrieval scope is a POINTER, not content. Removing a
    # deleted UUID from it is actively dangerous: an emptied scope means "every
    # transcript I can access", so a scrub would WIDEN the conversation, while a
    # dangling UUID simply matches nothing.
    ("chat_conversation", "context"): "scope pointer; emptying it widens retrieval",
    ("chat_project", "scope"): "scope pointer; emptying it widens retrieval",
}

#: Index name prefixes the residue scan skips, with the reason.
INDEX_EXEMPT_PREFIXES: dict[str, str] = {
    ".": "OpenSearch system indices",
    "audit-logs-": "append-only compliance trail; it must outlive the data it records",
    "top_queries-": "OpenSearch query-insights log of search requests, not file content",
}


#: A centroid rebuilt from the remaining members matches their mean to float32 precision
#: (~1.0); the two-member average the old code left behind measures ~0.95 for the seeded
#: vectors, so this sits between the two with room on both sides.
CENTROID_MATCH_COSINE = 0.99999


@dataclass(frozen=True)
class FkEdge:
    child: str
    child_col: str
    parent: str
    parent_col: str
    on_delete: str  # a=no action, r=restrict, c=cascade, n=set null, d=set default


def fk_edges(conn: Connection) -> list[FkEdge]:
    """Every single-column foreign key in the ``public`` schema, by bare table name."""
    rows = conn.execute(
        text(
            """
            SELECT ct.relname::text, a.attname::text,
                   pt.relname::text, af.attname::text, c.confdeltype::text
            FROM pg_constraint c
            JOIN pg_class ct ON ct.oid = c.conrelid
            JOIN pg_class pt ON pt.oid = c.confrelid
            JOIN pg_namespace n ON n.oid = ct.relnamespace AND n.nspname = 'public'
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
            JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = c.confkey[1]
            WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 1
            """
        )
    ).all()
    return [FkEdge(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4])) for r in rows]


def _primary_key(conn: Connection, table: str) -> str | None:
    row = conn.execute(
        text(
            """
            SELECT a.attname::text FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = to_regclass(quote_ident(:t)) AND i.indisprimary
            """
        ),
        {"t": table},
    ).all()
    return row[0][0] if len(row) == 1 else None


def _rows_where_in(
    conn: Connection, table: str, col: str, ids: list[Any], select: str | None
) -> list[Any]:
    """``SELECT <select or 1> FROM <table> WHERE <col> = ANY(ids)``, identifiers quoted by
    SQLAlchemy — the names come from ``pg_catalog``, never from input."""
    tbl = sa_table(table, sa_column(col), *([sa_column(select)] if select else []))
    target = tbl.c[select] if select else literal(1)
    return list(conn.execute(select_(target).where(tbl.c[col].in_(ids))).all())


def media_file_closure(
    conn: Connection, file_ids: list[int]
) -> tuple[dict[str, set[Any]], dict[FkEdge, int]]:
    """The rows that exist BECAUSE of these files, and how many rows each edge holds.

    Returns:
        ``(reached, edge_rows)``. ``reached`` maps table -> primary keys reached through
        non-``SET NULL`` edges (``media_file`` included). ``edge_rows`` counts, for every
        edge whose parent is in the closure, the rows referencing a reached parent — the
        coverage guard reads it to prove the seed populated every such table.
    """
    edges = fk_edges(conn)
    reached: dict[str, set[Any]] = defaultdict(set)
    reached["media_file"] = set(file_ids)
    edge_rows: dict[FkEdge, int] = {}
    frontier = ["media_file"]
    while frontier:
        parent = frontier.pop()
        for edge in edges:
            if edge.parent != parent or edge.parent_col != _primary_key(conn, parent):
                continue
            pk = _primary_key(conn, edge.child)
            rows = _rows_where_in(conn, edge.child, edge.child_col, list(reached[parent]), pk)
            edge_rows[edge] = edge_rows.get(edge, 0) + len(rows)
            if edge.on_delete == "n" or pk is None:
                continue
            new = {r[0] for r in rows} - reached[edge.child]
            if new:
                reached[edge.child] |= new
                frontier.append(edge.child)
    return dict(reached), edge_rows


def postgres_residue(
    conn: Connection,
    reached: dict[str, set[Any]],
    edge_rows: dict[FkEdge, int],
    sf: SeededFile,
) -> list[str]:
    """Rows of the pre-delete closure that still exist, plus non-FK copies."""
    findings: list[str] = []
    for table, ids in reached.items():
        pk = _primary_key(conn, table)
        if pk is None or not ids:
            continue
        left = len(_rows_where_in(conn, table, pk, list(ids), None))
        if left:
            findings.append(f"postgres: {left} row(s) of {table} survive")
    for edge in edge_rows:
        if edge.on_delete != "n" or edge.parent not in reached:
            continue
        left = len(
            _rows_where_in(conn, edge.child, edge.child_col, list(reached[edge.parent]), None)
        )
        if left:
            findings.append(f"postgres: {left} {edge.child}.{edge.child_col} still point at it")
    if sf.cluster_id is not None and _rows_where_in(
        conn, "speaker_cluster", "id", [sf.cluster_id], None
    ):
        findings.append("postgres: the emptied, unpromoted speaker_cluster survives")
    findings.extend(_json_references(conn, sf.file_uuid, skip_tables=set(reached)))
    return findings


def _json_references(conn: Connection, needle: str, *, skip_tables: set[str]) -> list[str]:
    cols = conn.execute(
        text(
            """
            SELECT table_name::text, column_name::text FROM information_schema.columns
            WHERE table_schema = 'public' AND data_type IN ('json', 'jsonb')
            """
        )
    ).all()
    findings = []
    for row in cols:
        table, column = str(row[0]), str(row[1])
        if table in skip_tables or (table, column) in JSON_REFERENCE_EXEMPT:
            continue
        tbl = sa_table(table, sa_column(column))
        hits = conn.execute(
            select_(func.count()).where(cast(tbl.c[column], Text).like(f"%{needle}%"))
        ).scalar_one()
        if hits:
            findings.append(f"postgres: {hits} {table}.{column} JSON value(s) still name the file")
    return findings


def scannable_indices(client: Any) -> list[str]:
    """Every index the residue scan covers: all of them minus :data:`INDEX_EXEMPT_PREFIXES`."""
    return [
        row["index"]
        for row in client.cat.indices(format="json")
        if not any(row["index"].startswith(p) for p in INDEX_EXEMPT_PREFIXES)
    ]


def residue_query(sf: SeededFile, *, include_owner: bool) -> dict[str, Any]:
    """A query matching any document that names the file, its speakers or its cluster.

    ``include_owner`` widens it to everything the OWNER holds (``user_id``, the profile
    embedding) — only ever for a throwaway account, never the shared admin, whose real
    documents the same term would match.
    """
    should: list[dict[str, Any]] = [
        {"term": {"file_uuid": sf.file_uuid}},
        {"term": {"file_id": sf.file_id}},
        {"term": {"media_file_id": sf.file_id}},
        {"terms": {"speaker_uuid": sf.speaker_uuids}},
        {"ids": {"values": [sf.file_uuid, *sf.speaker_uuids]}},
    ]
    if sf.cluster_uuid:
        should += [
            {"term": {"cluster_uuid": sf.cluster_uuid}},
            {"ids": {"values": [f"cluster_{sf.cluster_uuid}"]}},
        ]
    for extra in sf.extra_cluster_uuids:
        should += [{"term": {"cluster_uuid": extra}}, {"ids": {"values": [f"cluster_{extra}"]}}]
    if include_owner:
        should.append({"term": {"user_id": sf.owner_id}})
        if sf.profile_uuid:
            should.append({"ids": {"values": [f"profile_{sf.profile_uuid}"]}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def opensearch_residue(sf: SeededFile, *, include_owner: bool) -> list[str]:
    """Documents in any non-exempt index that name the file, its speakers, cluster or owner."""
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    indices = scannable_indices(client)
    client.indices.refresh(index=",".join(indices), ignore_unavailable=True)
    resp = client.search(
        index=",".join(indices),
        body={
            "size": 0,
            "query": residue_query(sf, include_owner=include_owner),
            "aggs": {"idx": {"terms": {"field": "_index", "size": 100}}},
        },
        params={"ignore_unavailable": "true"},
    )
    return [
        f"opensearch: {b['doc_count']} doc(s) in {b['key']}"
        for b in resp["aggregations"]["idx"]["buckets"]
    ]


def opensearch_planes(file_uuid: str) -> dict[str, int]:
    """``transcript_chunks`` documents per ``doc_type`` for one file (coverage guard)."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    resp = client.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={
            "size": 0,
            "query": {"term": {"file_uuid": file_uuid}},
            "aggs": {"t": {"terms": {"field": "doc_type", "missing": "chunk", "size": 10}}},
        },
    )
    return {b["key"]: b["doc_count"] for b in resp["aggregations"]["t"]["buckets"]}


def centroid_residue(cluster_uuid: str, remaining: list[list[float]]) -> list[str]:
    """Findings when a SURVIVING cluster's centroid is not the mean of ``remaining`` voiceprints.

    After a delete, a cluster that still has members must carry a centroid built from
    those members alone. Cosine against the expected mean is used (the stored vector is
    L2-normalised, the expectation is normalised here), so a centroid that still averages
    in a deleted voiceprint reads as a measurable shortfall rather than as noise.
    """
    import numpy as np

    from app.services.opensearch_service import get_opensearch_client
    from app.services.opensearch_service.aliases import get_active_speaker_index

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    doc_id = f"cluster_{cluster_uuid}"
    index = get_active_speaker_index()
    if not client.exists(index=index, id=doc_id):
        return [f"opensearch: surviving cluster {doc_id} has no centroid document"]
    stored = np.array(client.get(index=index, id=doc_id)["_source"]["embedding"], dtype=np.float64)
    expected = np.mean(np.array(remaining, dtype=np.float64), axis=0)
    cosine = float(stored @ expected / (np.linalg.norm(stored) * np.linalg.norm(expected)))
    if cosine < CENTROID_MATCH_COSINE:
        return [
            f"opensearch: surviving cluster {doc_id} centroid is not the mean of its remaining "
            f"members (cosine {cosine:.6f} < {CENTROID_MATCH_COSINE})"
        ]
    return []


def minio_residue(sf: SeededFile, *, include_owner: bool) -> list[str]:
    """Objects in any bucket under the file's prefixes (or the owner's avatar prefix)."""
    from app.services.minio_service import minio_client

    needles = [f"file_{sf.file_id}/", f"derived/{sf.file_id}_", sf.file_uuid]
    if include_owner:
        needles.append(f"avatars/{sf.owner_id}/")
    findings = []
    for bucket in minio_client.list_buckets():
        for obj in minio_client.list_objects(bucket.name, recursive=True):
            name = obj.object_name or ""
            if any(n in name for n in needles):
                findings.append(f"minio: {bucket.name}/{name}")
    return findings


def redis_residue(sf: SeededFile) -> list[str]:
    """Seeded keys that survive, plus any key in db 0/1 naming the file's UUID."""
    findings = [f"redis: db{db} {key}" for db, key in sf.redis_keys if redis_client(db).exists(key)]
    for db in (0, 1):
        for key in redis_client(db).scan_iter(match=f"*{sf.file_uuid}*", count=1000):
            findings.append(f"redis: db{db} {key}")
    return findings


def scan_residue(
    conn: Connection,
    sf: SeededFile,
    reached: dict[str, set[Any]],
    edge_rows: dict[FkEdge, int],
    *,
    include_owner: bool = False,
) -> list[str]:
    """Every store's residue for one seeded file. Empty means the delete left nothing."""
    return [
        *postgres_residue(conn, reached, edge_rows, sf),
        *opensearch_residue(sf, include_owner=include_owner),
        *minio_residue(sf, include_owner=include_owner),
        *redis_residue(sf),
    ]


class TeardownIncompleteError(AssertionError):
    """At least one store kept seeded data; every failed step is listed, not just the first."""


#: Attempts per OpenSearch sweep. A ``delete_by_query`` is a scroll search, and a scroll
#: context reaped mid-sweep (``search_context_missing``) fails a perfectly retryable call;
#: that one transient error used to abort the whole teardown and leak ``e2e-delres-*`` rows.
_OPENSEARCH_SWEEP_ATTEMPTS = 3


class _Teardown:
    """Runs every teardown step whatever an earlier one did, and remembers what failed."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.failures: list[str] = []

    def attempt(self, step: str, action: Any) -> None:
        try:
            action()
        except Exception as exc:  # recorded; raised together with every other failure
            logger.exception("deletion-residue teardown step failed: %s", step)
            self.failures.append(f"{step}: {type(exc).__name__}: {exc}")

    def sql(self, statements: list[tuple[str, dict[str, Any]]]) -> None:
        """One transaction per step, so one failure cannot roll another step back."""
        with self.engine.begin() as conn:
            for statement, params in statements:
                conn.execute(text(statement), params)

    def purge(self, sf: SeededFile) -> None:
        from sqlalchemy.orm import Session

        from app.models.media import MediaFile
        from app.services.file_cleanup_service import purge_media_file

        with Session(self.engine) as db:
            row = db.query(MediaFile).filter(MediaFile.id == sf.file_id).one_or_none()
            if row is not None:
                purge_media_file(db, row)
            db.commit()

    def sweep_opensearch(self, client: Any, sf: SeededFile, *, throwaway: bool) -> None:
        for remaining in range(_OPENSEARCH_SWEEP_ATTEMPTS - 1, -1, -1):
            try:
                client.delete_by_query(
                    index=",".join(scannable_indices(client)),
                    body={"query": residue_query(sf, include_owner=throwaway)},
                    params={
                        "ignore_unavailable": "true",
                        "refresh": "true",
                        "conflicts": "proceed",
                    },
                )
                return
            except Exception:
                if remaining == 0:
                    raise
                logger.warning("Transient OpenSearch sweep failure for %s; retrying", sf.file_uuid)

    def sweep_minio(self, sf: SeededFile, *, throwaway: bool) -> None:
        from app.services.minio_service import minio_client

        for finding in minio_residue(sf, include_owner=throwaway):
            bucket, _, key = finding.removeprefix("minio: ").partition("/")
            minio_client.remove_object(bucket, key)

    def seeded_rows(self, sf: SeededFile) -> None:
        ids = {
            "cl": sf.cluster_id,
            "tag": sf.tag_id,
            "col": sf.collection_id,
            "prof": sf.profile_id,
            "ws": sf.watch_source_id,
            "ue": sf.usage_event_id,
        }
        self.sql(
            [
                ("DELETE FROM speaker_cluster WHERE id = :cl", ids),
                *[
                    ("DELETE FROM speaker_cluster WHERE id = :cl", {"cl": extra})
                    for extra in sf.extra_cluster_ids
                ],
                ("DELETE FROM tag WHERE id = :tag", ids),
                ("DELETE FROM collection_member WHERE collection_id = :col", ids),
                ("DELETE FROM collection WHERE id = :col", ids),
                ("DELETE FROM speaker_profile WHERE id = :prof", ids),
                ("DELETE FROM watch_source_file WHERE watch_source_id = :ws", ids),
                ("DELETE FROM watch_source WHERE id = :ws", ids),
                ("DELETE FROM usage_event WHERE id = CAST(:ue AS uuid)", ids),
            ]
        )

    def raise_if_failed(self) -> None:
        if self.failures:
            raise TeardownIncompleteError(
                f"teardown left seeded data in {len(self.failures)} place(s); every step "
                "was still attempted:\n  " + "\n  ".join(self.failures)
            )


def teardown_seed(
    engine: Any,
    files: list[SeededFile],
    *,
    throwaway_user_ids: list[int],
    chat_conversation_ids: list[int],
    api_profile_uuids: list[str] | None = None,
) -> None:
    """Remove everything the seed wrote that the delete under test did not. Idempotent.

    Runs on every exit path, including a failed assertion and a red run against old
    code, so it never trusts the code under test to have finished: whatever survives
    is swept store by store. The scope is strictly the seeded identifiers — the shared
    admin's real documents are never addressed (``include_owner`` is only set for a
    throwaway owner).

    **Every step runs whatever an earlier one did** (issue #1233): a step that raises is
    logged with its traceback, recorded, and the sweep continues, each SQL step in its
    own transaction. Once everything has been attempted,
    :class:`TeardownIncompleteError` lists every failed step. A teardown that stopped at
    the first error (a transient ``search_context_missing``) leaked the rows of every
    later step and named only one.

    Args:
        api_profile_uuids: Profiles a test created through the API (a promotion) rather
            than through the seed; their rows and embeddings are removed too.
    """
    from app.services.opensearch_service import get_opensearch_client
    from app.services.opensearch_service import remove_profile_embedding

    td = _Teardown(engine)
    for profile_uuid in api_profile_uuids or []:
        td.attempt(
            f"opensearch profile {profile_uuid}", lambda u=profile_uuid: remove_profile_embedding(u)
        )
        td.attempt(
            f"postgres profile {profile_uuid}",
            lambda u=profile_uuid: td.sql(
                [("DELETE FROM speaker_profile WHERE uuid = :p", {"p": u})]
            ),
        )
    for sf in files:
        td.attempt(f"purge file {sf.file_uuid}", lambda sf=sf: td.purge(sf))

    client = get_opensearch_client()
    if client is None:
        td.failures.append("opensearch: SKIP_OPENSEARCH said a cluster was reachable but it is not")
    for sf in files:
        throwaway = sf.owner_id in throwaway_user_ids
        if client is not None:
            td.attempt(
                f"opensearch documents of {sf.file_uuid}",
                lambda sf=sf, t=throwaway: td.sweep_opensearch(client, sf, throwaway=t),
            )
        if sf.profile_uuid:
            td.attempt(
                f"opensearch profile {sf.profile_uuid}",
                lambda u=sf.profile_uuid: remove_profile_embedding(u),
            )
        td.attempt(
            f"minio objects of {sf.file_uuid}",
            lambda sf=sf, t=throwaway: td.sweep_minio(sf, throwaway=t),
        )
        for db_index, key in sf.redis_keys:
            td.attempt(
                f"redis {db_index}:{key}", lambda i=db_index, k=key: redis_client(i).delete(k)
            )

    for cid in chat_conversation_ids:
        td.attempt(
            f"postgres chat {cid}",
            lambda c=cid: td.sql(
                [
                    ("DELETE FROM chat_message WHERE conversation_id = :c", {"c": c}),
                    ("DELETE FROM chat_conversation WHERE id = :c", {"c": c}),
                ]
            ),
        )
    for sf in files:
        td.attempt(f"postgres rows of {sf.file_uuid}", lambda sf=sf: td.seeded_rows(sf))
    for uid in throwaway_user_ids:
        td.attempt(f"throwaway user {uid}", lambda u=uid: _delete_throwaway_user(engine, u))
    td.raise_if_failed()


def _delete_throwaway_user(engine: Any, user_id: int) -> None:
    """Delete a throwaway account the way ``DELETE /api/users/{uuid}`` does, if it exists."""
    from sqlalchemy.orm import Session

    from app.api.endpoints.admin import _delete_user_media_files
    from app.api.endpoints.admin import _delete_user_owned_records
    from app.api.endpoints.admin import _delete_user_speakers
    from app.models.user import User
    from app.services.file_cleanup_service import load_account_purge_plans
    from app.services.file_cleanup_service import purge_account_external_copies

    with Session(engine) as db:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if user is None:
            return
        plan = load_account_purge_plans(db, user_id)
        try:
            _delete_user_owned_records(db, user_id)
            _delete_user_speakers(db, user_id)
            _delete_user_media_files(db, user_id)
            db.delete(user)
            db.commit()
        except Exception:  # noqa: BLE001 — teardown must finish against broken code too
            # A red run executes the very defect under test (the ORM refusing to delete
            # an account); fall back to SQL so the throwaway account never outlives it.
            db.rollback()
            with engine.begin() as conn:
                for statement in (
                    "DELETE FROM speaker_profile WHERE user_id = :u",
                    "DELETE FROM collection WHERE user_id = :u",
                    "DELETE FROM tag WHERE user_id = :u",
                    "DELETE FROM comment WHERE user_id = :u",
                    "DELETE FROM task WHERE user_id = :u",
                    'DELETE FROM "user" WHERE id = :u',
                ):
                    conn.execute(text(statement), {"u": user_id})
    purge_account_external_copies(plan)
