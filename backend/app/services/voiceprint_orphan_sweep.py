"""Find and remove orphaned voiceprints: speaker/profile embeddings with no database row.

A voiceprint is biometric data. When the row it belongs to is gone — the recording was
purged, the speaker merged away, the profile deleted, the database restored from an older
dump — the embedding must go too. Every delete path tries to do that inline; this module
is the backstop for whatever an earlier path missed, and the tool an operator runs to
clean up after one.

What it compares, per document in every speaker index (``speaker_embedding_indices()``:
the alias, v3, v4 and the legacy v3 backup):

* a **speaker** document (no ``document_type``, id = ``speaker.uuid``) is an orphan when
  no ``speaker`` row has that uuid;
* a **profile** document (``document_type: profile``, id = ``profile_<uuid>``) is an
  orphan when no ``speaker_profile`` row has that uuid;
* a **cluster centroid** document (``document_type: cluster``, id = ``cluster_<uuid>``) is
  an orphan when no ``speaker_cluster`` row has that uuid (what a purge interrupted before
  ``privacy_purge_pending`` existed left behind);
* anything else (future document types) is never touched.

It is a **dry run by default** and counts before it deletes. Unlike the scheduled
``opensearch_orphan_cleanup`` sweep, it also covers profile documents and the v3 / v3
backup indices, which that sweep does not read.

Guards, both stated in the report:

* **Empty database.** With no speaker AND no profile rows, every voiceprint is an
  "orphan" — and a lost or empty-restored database looks exactly like that. The sweep
  refuses unless ``force=True``.
* The deletion is verified: after ``delete_by_query`` (with ``refresh``) each index is
  re-counted for the ids it tried to delete, and any survivor is reported.
* **Writes during the scan.** The row snapshot is taken before the scan, so a speaker whose row
  commits while the scan runs would look orphaned. Before deleting, the rows are read again and
  any candidate that now has one is dropped, and a document written after the sweep started
  (``created_at``) is never deleted: its row may not have committed yet. Both are counted in
  ``skipped_recent``.

Run it inside a backend container::

    python -m app.scripts.sweep_orphan_voiceprints            # dry run: counts only
    python -m app.scripts.sweep_orphan_voiceprints --apply    # delete the orphans
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from app.services.opensearch_service.client import CLUSTER_UNAVAILABLE_ERRORS

logger = logging.getLogger(__name__)

#: ids per ``delete_by_query`` — well under ``index.max_terms_count`` / ids-query limits.
_DELETE_BATCH = 500

_PROFILE_ID_PREFIX = "profile_"
_CLUSTER_ID_PREFIX = "cluster_"


def _db_speaker_uuids() -> set[str]:
    from app.db.session_utils import session_scope
    from app.models.speaker import Speaker

    with session_scope() as db:
        return {str(u) for (u,) in db.query(Speaker.uuid).all()}


def _db_profile_uuids() -> set[str]:
    from app.db.session_utils import session_scope
    from app.models.speaker import SpeakerProfile

    with session_scope() as db:
        return {str(u) for (u,) in db.query(SpeakerProfile.uuid).all()}


def _db_cluster_uuids() -> set[str]:
    from app.db.session_utils import session_scope
    from app.models.speaker import SpeakerCluster

    with session_scope() as db:
        return {str(u) for (u,) in db.query(SpeakerCluster.uuid).all()}


def _classify(
    hit: dict[str, Any], speakers: set[str], profiles: set[str], clusters: set[str]
) -> str | None:
    """``"speaker"`` / ``"profile"`` / ``"cluster"`` when ``hit`` is an orphan, else None."""
    doc_id = str(hit["_id"])
    source = hit.get("_source") or {}
    doc_type = source.get("document_type")
    if doc_type == "cluster":
        cluster_uuid = source.get("cluster_uuid") or (
            doc_id[len(_CLUSTER_ID_PREFIX) :] if doc_id.startswith(_CLUSTER_ID_PREFIX) else None
        )
        return "cluster" if cluster_uuid and str(cluster_uuid) not in clusters else None
    if doc_type == "profile":
        profile_uuid = source.get("profile_uuid") or (
            doc_id[len(_PROFILE_ID_PREFIX) :] if doc_id.startswith(_PROFILE_ID_PREFIX) else None
        )
        return "profile" if profile_uuid and str(profile_uuid) not in profiles else None
    if doc_type is None:
        return "speaker" if doc_id not in speakers else None
    return None


def _written_since(hit: dict[str, Any], started: datetime) -> bool:
    """The document's ``created_at`` is at or after ``started`` (absent or unreadable: no)."""
    raw = (hit.get("_source") or {}).get("created_at")
    if not raw:
        return False
    try:
        written = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if written.tzinfo is None:
        written = written.replace(tzinfo=UTC)
    return written >= started


def _concrete_indices(client: Any, names: list[str]) -> list[str]:
    """Resolve aliases so each physical index is scanned exactly once."""
    concrete: list[str] = []
    for name in names:
        if not client.indices.exists(index=name):
            continue
        resolved = list(client.indices.get(index=name).keys())
        for idx in resolved:
            if idx not in concrete:
                concrete.append(idx)
    return concrete


def sweep_orphan_voiceprints(dry_run: bool = True, force: bool = False) -> dict[str, Any]:
    """Count (and, unless ``dry_run``, delete) voiceprint documents with no DB row.

    Idempotent: a second run over the same state finds nothing.

    Args:
        dry_run: Count only (the default). Nothing is written.
        force: Proceed even when the database holds no speaker and no profile rows.

    Returns:
        ``{"dry_run", "force", "refused", "db_speakers", "db_profiles", "orphans_found",
        "deleted", "surviving_orphans", "indices": {index: {"docs_scanned",
        "orphan_speaker_docs", "orphan_profile_docs", "orphan_cluster_docs", "deleted", "surviving"}}}``.

    Raises:
        RuntimeError: OpenSearch is not configured/reachable. Never reported as "no
            orphans" — that would be the answer an operator acts on.
    """
    from opensearchpy import helpers

    from app.services.opensearch_service import get_opensearch_client
    from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices

    client = get_opensearch_client()
    if client is None:
        raise RuntimeError("OpenSearch client unavailable — cannot sweep voiceprints")

    started = datetime.now(UTC)
    speakers = _db_speaker_uuids()
    profiles = _db_profile_uuids()
    # A failed read raises here, before any scan or delete: an unreadable table must never
    # look like "no clusters", which would make every centroid an orphan.
    clusters = _db_cluster_uuids()
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "force": force,
        "refused": None,
        "db_speakers": len(speakers),
        "db_profiles": len(profiles),
        "orphans_found": 0,
        "deleted": 0,
        "surviving_orphans": 0,
        "skipped_recent": 0,
        "indices": {},
    }

    orphans_by_index: dict[str, list[dict[str, Any]]] = {}
    for idx in _concrete_indices(client, speaker_embedding_indices()):
        stats = {
            "docs_scanned": 0,
            "orphan_speaker_docs": 0,
            "orphan_profile_docs": 0,
            "orphan_cluster_docs": 0,
            "deleted": 0,
            "surviving": 0,
        }
        # A deleted-but-unrefreshed doc would otherwise be "found" and re-deleted (harmless)
        # while a just-written one would be missed (also harmless); refresh keeps the counts
        # an operator reads honest.
        client.indices.refresh(index=idx)
        orphan_hits: list[dict[str, Any]] = []
        for hit in helpers.scan(
            client,
            index=idx,
            query={"query": {"match_all": {}}},
            _source=["document_type", "profile_uuid", "cluster_uuid", "created_at"],
            size=1000,
        ):
            stats["docs_scanned"] += 1
            kind = _classify(hit, speakers, profiles, clusters)
            if kind:
                stats[f"orphan_{kind}_docs"] += 1
                orphan_hits.append(hit)
        report["indices"][idx] = stats
        report["orphans_found"] += len(orphan_hits)
        if orphan_hits:
            orphans_by_index[idx] = orphan_hits

    if not speakers and not profiles and report["orphans_found"] and not force:
        report["refused"] = "empty_database"
        logger.error(
            "Voiceprint sweep REFUSED: the database has no speaker and no profile rows, so "
            "all %d voiceprint document(s) look orphaned. An empty database and a lost one "
            "are indistinguishable from here; re-run with force once Postgres is verified.",
            report["orphans_found"],
        )
        return report

    if dry_run:
        logger.info("Voiceprint sweep (dry run): %d orphan(s) found", report["orphans_found"])
        return report

    # The first snapshot predates the scan: read the rows again now, after it, and keep only the
    # candidates that are still orphans and were written before the sweep began.
    speakers, profiles, clusters = _db_speaker_uuids(), _db_profile_uuids(), _db_cluster_uuids()
    for idx, hits in orphans_by_index.items():
        stats = report["indices"][idx]
        ids = [
            str(hit["_id"])
            for hit in hits
            if _classify(hit, speakers, profiles, clusters) and not _written_since(hit, started)
        ]
        report["skipped_recent"] += len(hits) - len(ids)
        for i in range(0, len(ids), _DELETE_BATCH):
            batch = ids[i : i + _DELETE_BATCH]
            try:
                resp = client.delete_by_query(
                    index=idx,
                    body={"query": {"ids": {"values": batch}}},
                    refresh=True,
                    conflicts="proceed",
                )
                stats["deleted"] += int(resp.get("deleted", 0))
                if resp.get("failures"):
                    logger.error(
                        "Voiceprint sweep: %d delete failure(s) in %s",
                        len(resp["failures"]),
                        idx,
                    )
            except CLUSTER_UNAVAILABLE_ERRORS as e:  # counted as survivors below
                logger.exception("Voiceprint sweep: delete batch failed in %s: %s", idx, e)
        client.indices.refresh(index=idx)
        stats["surviving"] = sum(
            int(
                client.count(
                    index=idx, body={"query": {"ids": {"values": ids[i : i + _DELETE_BATCH]}}}
                )["count"]
            )
            for i in range(0, len(ids), _DELETE_BATCH)
        )
        report["deleted"] += stats["deleted"]
        report["surviving_orphans"] += stats["surviving"]

    level = logging.ERROR if report["surviving_orphans"] else logging.INFO
    logger.log(
        level,
        "Voiceprint sweep: deleted %d of %d orphan(s); %d survive",
        report["deleted"],
        report["orphans_found"],
        report["surviving_orphans"],
    )
    return report
