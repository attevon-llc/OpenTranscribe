"""One-off backfill: stamp ``organization_id`` onto already-indexed OpenSearch docs.

Tenant isolation (sub-step 1.2) added an ``organization_id`` field to the
transcript-chunks index and the speaker/voiceprint indices, and gates every
search/kNN query on it. Docs indexed BEFORE that change have no
``organization_id`` field, so on a populated cloud cluster they would be treated
as personal (org-less) and become invisible to org-scoped searches — and, worse,
the per-user reindex tasks are user-scoped, so they never re-stamp org rows.

This task closes that gap with an ``update_by_query`` per org file: it reads each
org's (organization_id -> file_uuids / file_ids / profile_ids) mapping from the
DB and stamps the field onto the matching docs in place — no full reindex, no
embedding recompute.

Speaker-plane scope rules (must match the query gates in
``speaker_matching_service`` / ``smart_speaker_suggestion_service`` /
``speaker_clustering_service``):

* **Per-file speaker docs** carry ``media_file_id`` — their org is the FILE's
  ``organization_id`` (never "the owner is an org member": a member's personal
  files stay personal).
* **Profile docs** (``document_type: profile``) mirror the
  ``SpeakerProfile.organization_id`` row value.
* **Cluster docs** (``document_type: cluster``) are personal-plane (the write
  path has no tenant field) — any stray ``organization_id`` is removed.

A repair pass also strips ``organization_id`` from members' personal file /
profile / cluster docs, undoing the earlier (buggy) backfill that stamped ALL
of a member's speaker docs by ``user_id``.

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
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import CPUPriority

logger = logging.getLogger(__name__)


@dataclass
class SpeakerScopeMaps:
    """DB-derived tenant-scope mappings for the speaker-plane backfill.

    ``org_to_file_ids``: org -> ids of its media files (per-file speaker docs).
    ``org_to_profile_ids``: org -> ids of its speaker profiles (profile docs).
    ``member_user_ids``: every user holding any org membership — the population
        the old user_id-wide backfill could have mislabeled.
    ``personal_file_ids`` / ``personal_profile_ids``: members' org-NULL files /
        profiles whose docs must NOT carry an ``organization_id``.
    """

    org_to_file_ids: dict[int, list[int]] = field(default_factory=dict)
    org_to_profile_ids: dict[int, list[int]] = field(default_factory=dict)
    member_user_ids: list[int] = field(default_factory=list)
    personal_file_ids: list[int] = field(default_factory=list)
    personal_profile_ids: list[int] = field(default_factory=list)


def _build_speaker_scope_maps(db: Any) -> SpeakerScopeMaps:
    """Read the speaker-plane tenant-scope mappings from the DB."""
    from app.models.media import MediaFile
    from app.models.media import SpeakerProfile
    from app.models.organization import OrganizationMembership

    scope = SpeakerScopeMaps()

    member_ids = {int(uid) for (uid,) in db.query(OrganizationMembership.user_id).distinct().all()}
    scope.member_user_ids = sorted(member_ids)

    for org_id, file_id in (
        db.query(MediaFile.organization_id, MediaFile.id)
        .filter(MediaFile.organization_id.isnot(None))
        .all()
    ):
        scope.org_to_file_ids.setdefault(int(org_id), []).append(int(file_id))

    for org_id, profile_id in (
        db.query(SpeakerProfile.organization_id, SpeakerProfile.id)
        .filter(SpeakerProfile.organization_id.isnot(None))
        .all()
    ):
        scope.org_to_profile_ids.setdefault(int(org_id), []).append(int(profile_id))

    if member_ids:
        scope.personal_file_ids = [
            int(fid)
            for (fid,) in db.query(MediaFile.id)
            .filter(
                MediaFile.organization_id.is_(None),
                MediaFile.user_id.in_(member_ids),
            )
            .all()
        ]
        scope.personal_profile_ids = [
            int(pid)
            for (pid,) in db.query(SpeakerProfile.id)
            .filter(
                SpeakerProfile.organization_id.is_(None),
                SpeakerProfile.user_id.in_(member_ids),
            )
            .all()
        ]

    return scope


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


# Max ids per update_by_query terms clause (well under OpenSearch's
# index.max_terms_count default of 65536).
_TERMS_CHUNK = 5000

_STAMP_ORG_SCRIPT_SRC = "ctx._source.organization_id = params.org_id"
_REMOVE_ORG_SCRIPT = {
    "source": "ctx._source.remove('organization_id')",
    "lang": "painless",
}


def _chunked(ids: list[int], size: int = _TERMS_CHUNK) -> list[list[int]]:
    """Split an id list into terms-query-safe chunks."""
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _speaker_indices(client: Any) -> list[str]:
    """Existing speaker indices to backfill (active alias + versioned)."""
    from app.core.constants import get_speaker_index
    from app.core.constants import get_speaker_index_v3
    from app.core.constants import get_speaker_index_v4

    indices = []
    for index_name in {get_speaker_index(), get_speaker_index_v3(), get_speaker_index_v4()}:
        try:
            if client.indices.exists(index=index_name):
                indices.append(index_name)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Skipping index {index_name} (existence check failed): {e}")
    return indices


def _update_by_query(client: Any, index_name: str, query: dict, script: dict, label: str) -> int:
    """Run one update_by_query, returning the updated-doc count (0 on error)."""
    try:
        resp = client.update_by_query(
            index=index_name,
            refresh=True,
            conflicts="proceed",
            body={"query": query, "script": script},
        )
        return int(resp.get("updated", 0))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Speaker backfill ({label}) failed in {index_name}: {e}")
        return 0


def _stamp_script(org_id: int) -> dict:
    return {"source": _STAMP_ORG_SCRIPT_SRC, "lang": "painless", "params": {"org_id": org_id}}


def _backfill_speaker_docs(client: Any, scope: SpeakerScopeMaps) -> int:
    """Stamp/repair organization_id on speaker-plane docs across speaker indices.

    Tenant scope follows the DB row that anchors each doc type — NOT doc
    ownership (a member's personal voiceprints must stay org-less):

    * per-file speaker docs -> the file's org (``terms media_file_id``, like
      the per-file-uuid chunk backfill);
    * profile docs -> ``SpeakerProfile.organization_id``;
    * cluster docs -> always personal (org field removed — the cluster write
      path has no tenant field).

    Idempotent: stamp queries skip docs already carrying the correct org, and
    repair queries only match docs that still carry a stray org field. Targets
    the active alias plus the versioned indices so both v3/v4 are covered.
    """
    updated = 0
    for index_name in _speaker_indices(client):
        # 1) Per-file speaker docs: org = the file's organization_id.
        for org_id, file_ids in scope.org_to_file_ids.items():
            for chunk in _chunked(file_ids):
                query = {
                    "bool": {
                        "filter": [{"terms": {"media_file_id": chunk}}],
                        "must_not": [{"term": {"organization_id": org_id}}],
                    }
                }
                updated += _update_by_query(
                    client, index_name, query, _stamp_script(org_id), f"file-docs org {org_id}"
                )

        # 2) Profile docs: org mirrors SpeakerProfile.organization_id.
        for org_id, profile_ids in scope.org_to_profile_ids.items():
            for chunk in _chunked(profile_ids):
                query = {
                    "bool": {
                        "filter": [
                            {"term": {"document_type": "profile"}},
                            {"terms": {"profile_id": chunk}},
                        ],
                        "must_not": [{"term": {"organization_id": org_id}}],
                    }
                }
                updated += _update_by_query(
                    client, index_name, query, _stamp_script(org_id), f"profile-docs org {org_id}"
                )

        # 3) Repair: members' PERSONAL file docs must not carry an org
        #    (undoes the old user_id-wide stamping).
        for chunk in _chunked(scope.personal_file_ids):
            query = {
                "bool": {
                    "filter": [
                        {"terms": {"media_file_id": chunk}},
                        {"exists": {"field": "organization_id"}},
                    ]
                }
            }
            updated += _update_by_query(
                client, index_name, query, _REMOVE_ORG_SCRIPT, "personal file-docs repair"
            )

        # 4) Repair: personal profile docs must not carry an org.
        for chunk in _chunked(scope.personal_profile_ids):
            query = {
                "bool": {
                    "filter": [
                        {"term": {"document_type": "profile"}},
                        {"terms": {"profile_id": chunk}},
                        {"exists": {"field": "organization_id"}},
                    ]
                }
            }
            updated += _update_by_query(
                client, index_name, query, _REMOVE_ORG_SCRIPT, "personal profile-docs repair"
            )

        # 5) Repair: cluster docs are personal-plane — strip any stray org.
        for chunk in _chunked(scope.member_user_ids):
            query = {
                "bool": {
                    "filter": [
                        {"term": {"document_type": "cluster"}},
                        {"terms": {"user_id": chunk}},
                        {"exists": {"field": "organization_id"}},
                    ]
                }
            }
            updated += _update_by_query(
                client, index_name, query, _REMOVE_ORG_SCRIPT, "cluster-docs repair"
            )
    return updated


def run_tenant_backfill() -> dict[str, int]:
    """Stamp organization_id onto existing OpenSearch docs from the DB.

    Returns a summary dict. No-op (all zeros) in the community edition where no
    rows carry an organization_id.
    """
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.services.opensearch_service import opensearch_client

    if opensearch_client is None:
        logger.warning("Tenant backfill skipped: OpenSearch client not initialized")
        return {"orgs": 0, "chunk_docs": 0, "speaker_docs": 0}

    org_to_file_uuids: dict[int, list[str]] = {}

    with session_scope() as db:
        # Org files -> chunk doc scope
        rows = (
            db.query(MediaFile.organization_id, MediaFile.uuid)
            .filter(MediaFile.organization_id.isnot(None))
            .all()
        )
        for org_id, file_uuid in rows:
            org_to_file_uuids.setdefault(int(org_id), []).append(str(file_uuid))

        # Speaker plane: per-file / per-profile scope + personal repair lists
        scope = _build_speaker_scope_maps(db)

    org_count = len(
        set(org_to_file_uuids) | set(scope.org_to_file_ids) | set(scope.org_to_profile_ids)
    )
    if org_count == 0 and not scope.member_user_ids:
        logger.info("Tenant backfill: 0 orgs found (community edition) — nothing to do")
        return {"orgs": 0, "chunk_docs": 0, "speaker_docs": 0}

    chunk_docs = _backfill_transcript_chunks(opensearch_client, org_to_file_uuids)
    speaker_docs = _backfill_speaker_docs(opensearch_client, scope)
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
