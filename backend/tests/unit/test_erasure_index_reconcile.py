"""Index-level resurrection check against an in-memory OpenSearch stand-in (issue #1355, WP-X).

The fake implements exactly the query shapes the module issues (``term``, ``range``, ``exists``,
``bool`` with ``filter``/``must_not``, and a ``filters`` aggregation), so the assertions are about
what the module deletes and what it leaves, not about a mock being called. The live-cluster
version of the same scenario runs on the isolated stack (``integration`` marker).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest

from app.services import erasure_index_reconcile as reconcile
from app.services.erasure_index_reconcile import IndexSubject

ERASED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BEFORE = (ERASED_AT - timedelta(days=5)).isoformat()
AFTER = (ERASED_AT + timedelta(days=5)).isoformat()

MAPPING = {
    "user_id": {"type": "integer"},
    "organization_id": {"type": "integer"},
    "upload_time": {"type": "date"},
}


def _match(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    if "term" in query:
        ((field, value),) = query["term"].items()
        return bool(doc.get(field) == value)
    if "range" in query:
        ((field, bounds),) = query["range"].items()
        return field in doc and bool(doc[field] <= bounds["lte"])
    if "exists" in query:
        return query["exists"]["field"] in doc
    inner = query["bool"]
    return all(_match(doc, q) for q in inner.get("filter", [])) and not any(
        _match(doc, q) for q in inner.get("must_not", [])
    )


class _Indices:
    def __init__(self, owner: FakeOpenSearch):
        self.owner = owner

    def get_mapping(self, index: str):
        return {index: {"mappings": {"properties": self.owner.mappings[index]}}}

    def refresh(self, index: str):
        return {}


class _Cat:
    def __init__(self, owner: FakeOpenSearch):
        self.owner = owner

    def indices(self, format: str):  # noqa: A002 - mirrors the client signature
        return [{"index": name} for name in self.owner.docs]


class FakeOpenSearch:
    def __init__(self, docs: dict[str, list[dict[str, Any]]], mapping=None):
        self.docs = docs
        self.mappings = {name: dict(mapping or MAPPING) for name in docs}
        self.searches: list[str] = []
        self.fail_search = False
        self.indices = _Indices(self)
        self.cat = _Cat(self)

    def search(self, index: str, body: dict[str, Any]):
        if self.fail_search:
            raise ConnectionError("down")
        self.searches.append(index)
        filters = body["aggs"]["by_entry"]["filters"]["filters"]
        buckets = {
            key: {"doc_count": sum(1 for d in self.docs[index] if _match(d, q))}
            for key, q in filters.items()
        }
        return {"aggregations": {"by_entry": {"buckets": buckets}}}

    def delete_by_query(self, index: str, body: dict[str, Any], **_: Any):
        keep = [d for d in self.docs[index] if not _match(d, body["query"])]
        deleted = len(self.docs[index]) - len(keep)
        self.docs[index] = keep
        return {"deleted": deleted, "failures": []}


@pytest.fixture()
def opensearch(monkeypatch):
    def install(docs, **kwargs):
        fake = FakeOpenSearch(docs, **kwargs)
        monkeypatch.setattr(reconcile, "_client", lambda: fake)
        return fake

    return install


def _user(entry_id=1, user_id=7):
    return IndexSubject(entry_id, "user", user_id, None, ERASED_AT)


def _member(entry_id=2, user_id=7, org=3):
    return IndexSubject(entry_id, "org_member", user_id, org, ERASED_AT)


def test_opensearch_restore_of_erased_user_is_redeleted(opensearch):
    fake = opensearch(
        {
            "transcripts": [{"user_id": 7}, {"user_id": 7}, {"user_id": 9}],
            "transcript_chunks": [{"user_id": 7}, {"user_id": 9}],
        }
    )
    result = reconcile.check_subjects([_user()])

    assert result.redeleted == 3
    assert sorted(result.resurrections) == [(1, "os.transcript_chunks"), (1, "os.transcripts")]
    assert fake.docs == {"transcripts": [{"user_id": 9}], "transcript_chunks": [{"user_id": 9}]}
    assert result.checked_entry_ids == [1]
    assert result.unverified == 0


def test_a_clean_index_reports_nothing_and_stamps_the_entry(opensearch):
    opensearch({"transcripts": [{"user_id": 9}]})
    result = reconcile.check_subjects([_user()])
    assert (result.redeleted, result.resurrections, result.unverified) == (0, [], 0)
    assert result.checked_entry_ids == [1]


def test_opensearch_unavailable_is_unverified_not_clean(opensearch):
    fake = opensearch({"transcripts": [{"user_id": 7}]})
    fake.fail_search = True
    result = reconcile.check_subjects([_user()])
    assert result.unverified == 1
    assert result.checked_entry_ids == []  # the cursor does not advance
    assert result.redeleted == 0


def test_no_client_at_all_is_unverified(monkeypatch):
    def unavailable():
        raise RuntimeError("OpenSearch client unavailable")

    monkeypatch.setattr(reconcile, "_client", unavailable)
    result = reconcile.check_subjects([_user()])
    assert result.unverified == 1
    assert result.checked_entry_ids == []


def test_index_check_is_one_query_per_index(opensearch):
    fake = opensearch({"transcripts": [{"user_id": 1}], "transcript_chunks": [{"user_id": 1}]})
    subjects = [_user(entry_id=i, user_id=100 + i) for i in range(1, 51)]
    reconcile.check_subjects(subjects)
    assert sorted(fake.searches) == ["transcript_chunks", "transcripts"]


def test_org_member_documents_after_the_erasure_survive(opensearch):
    fake = opensearch(
        {
            "transcripts": [
                {"user_id": 7, "organization_id": 3, "upload_time": BEFORE},
                {"user_id": 7, "organization_id": 3, "upload_time": AFTER},
                {"user_id": 7, "organization_id": 4, "upload_time": BEFORE},
            ]
        }
    )
    result = reconcile.check_subjects([_member()])

    assert result.redeleted == 1
    assert fake.docs["transcripts"] == [
        {"user_id": 7, "organization_id": 3, "upload_time": AFTER},
        {"user_id": 7, "organization_id": 4, "upload_time": BEFORE},
    ]


def test_org_member_documents_without_a_creation_time_are_reported_not_deleted(opensearch):
    fake = opensearch({"transcripts": [{"user_id": 7, "organization_id": 3}]})
    result = reconcile.check_subjects([_member()])
    assert result.redeleted == 0
    assert result.needs_review == {"os.transcripts": 1}
    assert len(fake.docs["transcripts"]) == 1


def test_an_index_whose_mapping_lacks_the_owner_field_is_unverified(opensearch):
    opensearch({"transcripts": [{"user_id": 7}]}, mapping={"title": {"type": "text"}})
    result = reconcile.check_subjects([_user()])
    assert result.unverified == 1
    assert result.checked_entry_ids == [1]  # structural gap: do not stall the queue


def test_the_audit_index_is_never_touched(opensearch):
    fake = opensearch({"audit-logs-2026.10": [{"user_id": 7}], "transcripts": []})
    reconcile.check_subjects([_user()])
    assert fake.docs["audit-logs-2026.10"] == [{"user_id": 7}]
    assert "audit-logs-2026.10" not in fake.searches


def test_a_live_user_with_the_same_id_is_ambiguous_not_deleted(db_session, normal_user):
    from app.services import erasure_ledger_service as ledger

    entry = ledger.record_request(
        db_session,
        subject_type="user",
        subject_user_id=normal_user.id,
        subject_user_uuid=None,
        actor_kind="super_admin",
    )
    assert entry is not None
    entry.status = "complete"
    entry.completed_at = ERASED_AT
    db_session.commit()

    subjects, ambiguous = reconcile.select_subjects(db_session)

    assert entry.id in ambiguous
    assert entry.id not in [s.entry_id for s in subjects]
