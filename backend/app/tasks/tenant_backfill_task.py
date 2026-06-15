"""One-off backfill: stamp ``organization_id`` onto already-indexed OpenSearch docs.

Tenant isolation (sub-step 1.2) added an ``organization_id`` field to the
transcript-chunks index and the speaker/voiceprint indices, and gates every
search/kNN query on it. Docs indexed BEFORE that change have no
``organization_id`` field, so on a populated cloud cluster they would be treated
as personal (org-less) and become invisible to org-scoped searches — and, worse,
the per-user reindex tasks are user-scoped, so they never re-stamp org rows.

This task closes that gap with an ``update_by_query`` per org file: it reads each
org's (organization_id -> file_uuids / speaker user scope) mapping from the DB and
stamps the field onto the matching docs in place — no full reindex, no embedding
recompute.

COMMUNITY EDITION: every ``media_file.organization_id`` is NULL, so the DB query
returns no org rows and this task is a no-op (it logs "0 orgs" and exits).

POPULATED-CLUSTER RUNBOOK
-------------------------
Run ONCE after deploying the 1.2 isolation change to a cluster that already holds
indexed transcripts/voiceprints (cloud only):

    # in the backend container (or any celery worker host):
    python -m app.tasks.tenant_backfill_task            # synchronous, prints a summary
    # or enqueue it:
    from app.tasks.tenant_backfill_task import backfill_organization_id_task
    backfill_organization_id_task.delay()

Idempotent — safe to re-run. It only touches docs whose ``organization_id`` is
missing or differs from the DB value, so a second run reports ~0 updates. A fresh
/ empty dev cluster needs nothing here (a normal reindex already stamps the field
from ``MediaFile.organization_id``).

Verify afterwards (should return 0 once complete):

    GET transcript_search/_count
    {"query": {"bool": {
        "must":     [{"exists": {"field": "file_uuid"}}],
        "must_not": [{"exists": {"field": "organization_id"}}],
        "filter":   [{"terms": {"file_uuid": [<an org file's uuid>]}}]
    }}}
"""

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import CPUPriority

logger = logging.getLogger(__name__)


def _backfill_transcript_chunks(client: Any, org_to_file_uuids: dict[int, list[str]]) -> int:
    """Stamp organization_id on transcript-chunk docs, one update_by_query per org."""
    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    updated = 0
    if not client.indices.exists(index=index_name):
        return 0
    for org_id, file_uuids in org_to_file_uuids.items():
        if not file_uuids:
            continue
        try:
            resp = client.update_by_query(
                index=index_name,
                refresh=True,
                conflicts="proceed",
                body={
                    "query": {"terms": {"file_uuid": file_uuids}},
                    "script": {
                        "source": "ctx._source.organization_id = params.org_id",
                        "lang": "painless",
                        "params": {"org_id": org_id},
                    },
                },
            )
            updated += int(resp.get("updated", 0))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Chunk backfill failed for org {org_id}: {e}")
    return updated


def _backfill_speaker_docs(client: Any, org_to_user_ids: dict[int, list[int]]) -> int:
    """Stamp organization_id on speaker/profile/cluster docs across speaker indices.

    Speaker docs carry ``user_id`` (not file_uuid); within an org each member's
    docs belong to that org, so we stamp by (org -> member user_ids). Targets the
    active alias plus the versioned indices so both v3/v4 are covered.
    """
    from app.core.constants import get_speaker_index
    from app.core.constants import get_speaker_index_v3
    from app.core.constants import get_speaker_index_v4

    indices = {get_speaker_index(), get_speaker_index_v3(), get_speaker_index_v4()}
    updated = 0
    for index_name in indices:
        try:
            if not client.indices.exists(index=index_name):
                continue
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Skipping index {index_name} (existence check failed): {e}")
            continue
        for org_id, user_ids in org_to_user_ids.items():
            if not user_ids:
                continue
            try:
                resp = client.update_by_query(
                    index=index_name,
                    refresh=True,
                    conflicts="proceed",
                    body={
                        "query": {"terms": {"user_id": user_ids}},
                        "script": {
                            "source": "ctx._source.organization_id = params.org_id",
                            "lang": "painless",
                            "params": {"org_id": org_id},
                        },
                    },
                )
                updated += int(resp.get("updated", 0))
            except Exception as e:  # noqa: BLE001
                logger.error(f"Speaker backfill failed for org {org_id} in {index_name}: {e}")
    return updated


def run_tenant_backfill() -> dict[str, int]:
    """Stamp organization_id onto existing OpenSearch docs from the DB.

    Returns a summary dict. No-op (all zeros) in the community edition where no
    rows carry an organization_id.
    """
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.models.organization import OrganizationMembership
    from app.services.opensearch_service import opensearch_client

    if opensearch_client is None:
        logger.warning("Tenant backfill skipped: OpenSearch client not initialized")
        return {"orgs": 0, "chunk_docs": 0, "speaker_docs": 0}

    org_to_file_uuids: dict[int, list[str]] = {}
    org_to_user_ids: dict[int, list[int]] = {}

    with session_scope() as db:
        # Org files -> chunk doc scope
        rows = (
            db.query(MediaFile.organization_id, MediaFile.uuid)
            .filter(MediaFile.organization_id.isnot(None))
            .all()
        )
        for org_id, file_uuid in rows:
            org_to_file_uuids.setdefault(int(org_id), []).append(str(file_uuid))

        # Org members -> speaker/profile/cluster doc scope
        memberships = db.query(
            OrganizationMembership.organization_id, OrganizationMembership.user_id
        ).all()
        for org_id, user_id in memberships:
            org_to_user_ids.setdefault(int(org_id), []).append(int(user_id))

    org_count = len(set(org_to_file_uuids) | set(org_to_user_ids))
    if org_count == 0:
        logger.info("Tenant backfill: 0 orgs found (community edition) — nothing to do")
        return {"orgs": 0, "chunk_docs": 0, "speaker_docs": 0}

    chunk_docs = _backfill_transcript_chunks(opensearch_client, org_to_file_uuids)
    speaker_docs = _backfill_speaker_docs(opensearch_client, org_to_user_ids)
    summary = {"orgs": org_count, "chunk_docs": chunk_docs, "speaker_docs": speaker_docs}
    logger.info(f"Tenant backfill complete: {summary}")
    return summary


@celery_app.task(name="tenant_backfill_organization_id", priority=CPUPriority.MAINTENANCE)
def backfill_organization_id_task() -> dict[str, int]:
    """Celery entrypoint for the one-off org_id backfill (see module runbook)."""
    return run_tenant_backfill()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_tenant_backfill())  # noqa: T201
