"""Index-level resurrection check for completed erasures (issue #1355, work package X).

An OpenSearch-only restore (a snapshot restore, ``POST /search/repair-indices``, the speaker index
rebuilt from its backup) brings erased subjects' documents back while Postgres is untouched, so the
Postgres check in ``tasks/erasure_reconciliation`` cannot see it. This module asks the indices.

* **Which indices** come from the erasure receipt's registry (``erasure_receipt.stores_external``:
  ``classify_index`` and ``INDEX_PATTERNS``), not a second enumeration. ``os.audit`` is retained by
  design and never touched. The speaker indices are deleted through
  ``gdpr_erasure_service.delete_speaker_docs_by_query``, the single place they are enumerated.
* **One batched query per index** for a page of entries: a ``filters`` aggregation with one bucket
  per ledger entry, so the cost is bounded by the number of indices, not entries times indices.
* **Never clean on a failed look.** A cluster that cannot be asked leaves every cursor where it was
  and counts ``index_unverified``; an index whose mapping lacks the owner field is counted too (it
  cannot be checked, which is not the same as empty) but does not stall the cursor, because that is
  structural and would otherwise hold the head of the queue forever.
* **Time-bounded for org members**: only documents created at or before the erasure are deleted. A
  document with no creation field cannot be bounded, is left, and is reported in
  ``needs_review`` by store key.
* **Ambiguity is not deleted**: if a live user row carries the id (any uuid), the documents might
  belong to a legitimate account that was issued the id, so the subject is skipped and counted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.erasure import ErasureLedgerEntry
from app.models.organization import Organization
from app.models.user import User
from app.services import erasure_ledger_service as ledger

logger = logging.getLogger(__name__)

#: Entries examined per tick. Bounds the size of the ``filters`` aggregation (one bucket each) and
#: cycles the whole ledger in ``ledger size / 500`` ticks.
MAX_INDEX_CHECK_ENTRIES = 500

#: Logical store key -> the field holding a document's creation time, for the org-member cutoff.
CREATED_FIELD: dict[str, str] = {
    "os.transcripts": "upload_time",
    "os.transcript_chunks": "upload_time",
    "os.transcript_summaries": "created_at",
    "os.speakers": "created_at",
}


@dataclass(frozen=True)
class IndexSubject:
    """Plain data for one completed ledger entry (no ORM instance leaves the read session)."""

    entry_id: int
    subject_type: str
    user_id: int | None
    organization_id: int | None
    completed_at: datetime | None


@dataclass
class IndexCheckResult:
    redeleted: int = 0
    #: ``(entry id, store key)`` for every entry whose documents were found back.
    resurrections: list[tuple[int, str]] = field(default_factory=list)
    unverified: int = 0
    #: Store key -> number of entries with documents that could not be bounded by time.
    needs_review: dict[str, int] = field(default_factory=dict)
    #: Entries whose check finished and may be stamped ``index_checked_at``.
    checked_entry_ids: list[int] = field(default_factory=list)


def _stamp(subject: IndexSubject, props: dict[str, Any]) -> list | None:
    """Filter clauses selecting the subject's documents, or None when this index cannot say."""
    if subject.subject_type == "user" and subject.user_id is not None:
        return [{"term": {"user_id": subject.user_id}}]
    if subject.subject_type == "organization" and subject.organization_id is not None:
        if "organization_id" not in props:
            return None
        return [{"term": {"organization_id": subject.organization_id}}]
    if (
        subject.subject_type == "org_member"
        and subject.user_id is not None
        and subject.organization_id is not None
        and "organization_id" in props
    ):
        return [
            {"term": {"user_id": subject.user_id}},
            {"term": {"organization_id": subject.organization_id}},
        ]
    return None


def _bounded(
    clauses: list, subject: IndexSubject, created_field: str | None, props: dict
) -> list | None:
    """The deletable selection: org members only up to the erasure time, others unbounded."""
    if subject.subject_type != "org_member":
        return clauses
    if created_field is None or created_field not in props or subject.completed_at is None:
        return None
    bound = subject.completed_at.astimezone(UTC).isoformat()
    return [*clauses, {"range": {created_field: {"lte": bound}}}]


def _client() -> Any:
    from app.services.erasure_receipt.stores_external import _os_client

    return _os_client()


def _indices(client: Any) -> list[tuple[str, str]]:
    """``(physical name, logical key)`` of every index a subject's documents can live in."""
    from app.services.erasure_receipt.stores_external import _configured_indices
    from app.services.erasure_receipt.stores_external import classify_index

    configured = _configured_indices()
    found: list[tuple[str, str]] = []
    for row in client.cat.indices(format="json"):
        name = str(row["index"])
        if name.startswith("."):
            continue
        key = classify_index(name, configured)
        if key in CREATED_FIELD:
            found.append((name, key))
    return sorted(found)


def _delete(client: Any, name: str, key: str, query: dict[str, Any]) -> int:
    if key == "os.speakers":
        from app.services.gdpr_erasure_service import delete_speaker_docs_by_query

        errors: list[dict[str, Any]] = []
        deleted = delete_speaker_docs_by_query(
            {"query": query}, errors=errors, label="resurrection"
        )
        if errors:
            raise RuntimeError("speaker documents could not be deleted")
        return deleted
    response = client.delete_by_query(
        index=name, body={"query": query}, refresh=True, conflicts="proceed"
    )
    if response.get("failures"):
        raise RuntimeError("documents could not be deleted")
    return int(response.get("deleted", 0))


def _check_index(
    client: Any, name: str, key: str, subjects: list[IndexSubject], result: IndexCheckResult
) -> None:
    created_field = CREATED_FIELD[key]
    mapping = client.indices.get_mapping(index=name)
    props = next(iter(mapping.values()))["mappings"].get("properties", {})
    if "user_id" not in props:
        result.unverified += 1
        return
    selections: dict[str, dict[str, Any]] = {}
    untimed: dict[str, dict[str, Any]] = {}
    for subject in subjects:
        clauses = _stamp(subject, props)
        if clauses is None:
            result.unverified += 1
            continue
        bounded = _bounded(clauses, subject, created_field, props)
        if bounded is not None:
            selections[str(subject.entry_id)] = {"bool": {"filter": bounded}}
        if subject.subject_type == "org_member":
            untimed[f"nc{subject.entry_id}"] = (
                {"bool": {"filter": clauses, "must_not": [{"exists": {"field": created_field}}]}}
                if created_field in props
                else {"bool": {"filter": clauses}}
            )
    filters = {**selections, **untimed}
    if not filters:
        return
    client.indices.refresh(index=name)
    response = client.search(
        index=name,
        body={"size": 0, "aggs": {"by_entry": {"filters": {"filters": filters}}}},
    )
    buckets = response["aggregations"]["by_entry"]["buckets"]
    for entry_key, query in selections.items():
        if int(buckets[entry_key]["doc_count"]) > 0:
            result.redeleted += _delete(client, name, key, query)
            result.resurrections.append((int(entry_key), key))
    flagged = sum(1 for k in untimed if int(buckets[k]["doc_count"]) > 0)
    if flagged:
        result.needs_review[key] = result.needs_review.get(key, 0) + flagged


def check_subjects(subjects: list[IndexSubject]) -> IndexCheckResult:
    """Check every index for the subjects' documents. Never raises; a failed look is ``unverified``."""
    result = IndexCheckResult()
    if not subjects:
        return result
    try:
        client = _client()
        for name, key in _indices(client):
            _check_index(client, name, key, subjects, result)
    except Exception as e:  # noqa: BLE001 - an unreachable cluster is a reported state
        logger.warning("erasure index check could not complete (%s)", type(e).__name__)
        result.unverified += 1
        result.checked_entry_ids = []
        return result
    result.checked_entry_ids = [s.entry_id for s in subjects]
    return result


def select_subjects(
    db: Session, *, limit: int = MAX_INDEX_CHECK_ENTRIES
) -> tuple[list[IndexSubject], list[int]]:
    """Plain subjects for the next page of completed entries, minus the ambiguous ones.

    Returns ``(subjects, ambiguous entry ids)``. A user or organization row that is alive again
    (any uuid) is left to the Postgres check, or is ambiguous about whose documents these are;
    either way this check does not delete by that id.
    """
    subjects: list[IndexSubject] = []
    ambiguous: list[int] = []
    for entry in ledger.index_check_entries(db, limit=limit):
        if _row_alive(db, entry):
            ambiguous.append(int(entry.id))
            continue
        subjects.append(
            IndexSubject(
                int(entry.id),
                str(entry.subject_type),
                entry.subject_user_id,
                entry.subject_organization_id,
                entry.completed_at,
            )
        )
    return subjects, ambiguous


def _row_alive(db: Session, entry: ErasureLedgerEntry) -> bool:
    if entry.subject_type == "user" and entry.subject_user_id is not None:
        return db.query(User.id).filter(User.id == entry.subject_user_id).first() is not None
    if entry.subject_type == "organization" and entry.subject_organization_id is not None:
        return (
            db.query(Organization.id)
            .filter(Organization.id == entry.subject_organization_id)
            .first()
            is not None
        )
    return False


def run_index_phase() -> dict[str, Any]:
    """The reconcile task's index phase: short read session, no session during OpenSearch, short write."""
    from app.db.session_utils import session_scope

    with session_scope() as db:
        subjects, ambiguous = select_subjects(db)
    result = check_subjects(subjects)
    now = datetime.now(UTC)
    with session_scope() as db:
        for entry_id, store_key in result.resurrections:
            entry = db.get(ErasureLedgerEntry, entry_id)
            if entry is not None:
                ledger.record_resurrection(db, entry, store_key=store_key)
        # Ambiguous entries are stamped too, or they would sit at the head of the queue forever.
        for entry_id in [*result.checked_entry_ids, *ambiguous]:
            entry = db.get(ErasureLedgerEntry, entry_id)
            if entry is not None:
                entry.index_checked_at = now
        db.commit()
    return {
        "index_resurrections_redeleted": result.redeleted,
        "index_resurrected_entries": len(result.resurrections),
        "index_unverified": result.unverified + len(ambiguous),
        "org_member_needs_review": sum(result.needs_review.values()),
    }
