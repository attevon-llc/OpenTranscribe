"""Stamp legacy speaker profiles, speaker collections and vocabulary terms with a tenant.

Revision ``v430_per_tenant_speaker_and_vocab_names`` runs the stamping once, at
upgrade time. This module is the operator's tool around it (issue #1110):

* it **reports** the rows the rules cannot stamp (their evidence spans tenants), with
  the candidate tenants, so an administrator can decide instead of the code guessing;
* it can **re-apply** the same rules to rows created before an explicit cutoff — for
  rows an older worker wrote while the deploy was rolling out;
* it can **sync voiceprints**: a profile's consolidated embedding document in
  OpenSearch carries ``organization_id`` only for organization profiles, so a profile
  stamped by the backfill is invisible to its organization's voice matching until its
  document carries the stamp too.

The SQL is the revision's own (``STAMP_STATEMENTS`` / ``AMBIGUOUS_REPORT_SQL``), loaded
from the migration file, so the rules exist in one place.

Run it inside a backend container::

    python -m app.scripts.backfill_tenant_stamps                    # dry run + report
    python -m app.scripts.backfill_tenant_stamps --apply --created-before 2026-10-01T00:00:00Z
    python -m app.scripts.backfill_tenant_stamps --sync-voiceprints
"""

from __future__ import annotations

import importlib.util
import logging
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

REVISION = "v430_per_tenant_speaker_and_vocab_names"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

#: Target table of each statement in the revision's ``STAMP_STATEMENTS``, in order.
_STATEMENT_TABLES = (
    "speaker_profile",
    "speaker_profile",
    "speaker_collection",
    "speaker_collection",
    "custom_vocabulary",
)

#: ids per ``update_by_query`` — well under the ids-query limits.
_SYNC_BATCH = 500

_SET_ORG_SCRIPT = "ctx._source.organization_id = params.org"


@lru_cache(maxsize=1)
def _revision() -> ModuleType:
    """Load the revision by path — ``alembic/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_REVISION_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if len(module.STAMP_STATEMENTS) != len(_STATEMENT_TABLES):
        raise RuntimeError(f"{REVISION}.STAMP_STATEMENTS no longer matches this module")
    return module


def backfill_tenant_stamps(
    conn: Any, *, apply: bool, created_before: datetime | None
) -> dict[str, Any]:
    """Run (or rehearse) the stamping rules and report what they cannot decide.

    Args:
        conn: SQLAlchemy connection. With ``apply`` the caller commits; a dry run
            leaves no trace (its statements run inside a rolled-back savepoint).
        apply: Write the stamps. Requires ``created_before``.
        created_before: Only rows created before this instant are considered, so a
            re-run never moves a row created in the personal workspace after the
            deploy. ``None`` (dry run only) considers every row.

    Returns:
        ``applied``, ``cutoff``, ``stamped`` (rows per table that were — or, in a dry
        run, would be — stamped), ``ambiguous`` (one dict per unstamped row whose
        evidence spans tenants) and ``ambiguous_count``.
    """
    if apply and created_before is None:
        raise ValueError("apply=True needs created_before: re-stamping must be bounded")
    revision = _revision()
    cutoff: Any = created_before if created_before is not None else "infinity"

    stamped: dict[str, int] = dict.fromkeys(_STATEMENT_TABLES, 0)
    savepoint = None if apply else conn.begin_nested()
    try:
        for table, statement in zip(_STATEMENT_TABLES, revision.STAMP_STATEMENTS, strict=True):
            stamped[table] += conn.execute(text(statement), {"cutoff": cutoff}).rowcount
    finally:
        if savepoint is not None:
            savepoint.rollback()

    ambiguous = [
        {
            "kind": row["kind"],
            "id": int(row["id"]),
            "user_id": int(row["user_id"]) if row["user_id"] is not None else None,
            "label": row["label"],
            "reason": row["reason"],
            "candidates": list(row["candidates"] or []),
        }
        for row in conn.execute(text(revision.AMBIGUOUS_REPORT_SQL), {"cutoff": cutoff}).mappings()
    ]
    return {
        "applied": apply,
        "cutoff": created_before.isoformat() if created_before is not None else None,
        "stamped": stamped,
        "ambiguous": ambiguous,
        "ambiguous_count": len(ambiguous),
    }


def org_profile_uuids(conn: Any) -> dict[str, int]:
    """``{profile uuid: organization_id}`` for every organization profile with a voiceprint."""
    rows = conn.execute(
        text(
            "SELECT uuid, organization_id FROM speaker_profile "
            "WHERE organization_id IS NOT NULL AND COALESCE(embedding_count, 0) > 0"
        )
    )
    return {str(r[0]): int(r[1]) for r in rows}


def sync_profile_voiceprint_tenants(
    profile_orgs: dict[str, int],
    *,
    client: Any = None,
    indices: list[str] | None = None,
) -> dict[str, Any]:
    """Write ``organization_id`` onto the consolidated-embedding document of each profile.

    Idempotent: a document that already carries the stamp is rewritten with the same
    value; a profile without a document is simply not matched.

    Args:
        profile_orgs: ``{profile uuid: organization_id}``.
        client: OpenSearch client (default: the application's).
        indices: Indices to update (default: every speaker-embedding index).

    Returns:
        ``updated`` (documents changed, summed over indices) and ``indices``.
    """
    if client is None:
        from app.services.opensearch_service import client as os_client

        client = os_client.get_opensearch_client()
        if client is None:
            raise RuntimeError("OpenSearch is not reachable")
    if indices is None:
        from app.services.opensearch_service.speaker_maintenance import speaker_embedding_indices

        indices = speaker_embedding_indices()

    by_org: dict[int, list[str]] = defaultdict(list)
    for profile_uuid, org_id in profile_orgs.items():
        by_org[org_id].append(f"profile_{profile_uuid}")

    updated = 0
    for index in indices:
        for org_id, doc_ids in by_org.items():
            for start in range(0, len(doc_ids), _SYNC_BATCH):
                batch = doc_ids[start : start + _SYNC_BATCH]
                response = client.update_by_query(
                    index=index,
                    body={
                        "query": {"ids": {"values": batch}},
                        "script": {
                            "source": _SET_ORG_SCRIPT,
                            "lang": "painless",
                            "params": {"org": org_id},
                        },
                    },
                    params={"ignore_unavailable": "true", "refresh": "true"},
                )
                updated += int(response.get("updated", 0))
    logger.info("Voiceprint tenant sync: %d documents updated", updated)
    return {"updated": updated, "indices": list(indices)}
