"""The trash's index stamp: what ``availability_stamp`` sends to OpenSearch (#1101).

Against a recording client, so the request bodies themselves are the evidence: which indices,
which documents (only those not already carrying the value), the script, the options
(``refresh=True`` so a read right after a trash sees it; ``conflicts="proceed"`` so a
concurrent write is counted, not fatal), and the chunking that keeps a 10,001-file trash under
``index.max_terms_count``. The reconcile half reads real rows (Postgres via ``db_session``).
The live twin, against real OpenSearch, is
``tests/integration/test_trash_index_lifecycle_opensearch.py``.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest

from app.core.config import settings
from app.models.media import MediaFile
from app.services.search import availability_stamp
from app.services.search.availability_field import AVAILABLE
from app.services.search.availability_field import TRASHED


class _RecordingClient:
    """An OpenSearch stand-in that records ``update_by_query`` calls."""

    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        response: dict[str, Any] | None = None,
        trashed_buckets: dict[str, list[int]] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.searches: list[tuple[str, dict[str, Any]]] = []
        self._existing = existing
        self._response = response or {"updated": 1, "version_conflicts": 0, "failures": []}
        self._buckets = trashed_buckets or {}
        outer = self

        class _Indices:
            def exists(self, index: str) -> bool:
                return outer._existing is None or index in outer._existing

        self.indices = _Indices()

    def update_by_query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return dict(self._response)

    def search(self, index: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.searches.append((index, body))
        composite = body["aggs"]["files"]["composite"]
        if "after" in composite:
            return {"aggregations": {"files": {"buckets": []}}}
        ids = self._buckets.get(index, [])
        buckets = [{"key": {"file": i}, "doc_count": 1} for i in ids]
        after = {"file": ids[-1]} if ids else None
        return {"aggregations": {"files": {"buckets": buckets, "after_key": after}}}


def _targets(monkeypatch) -> None:
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", "chunks_t")
    monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", "transcripts_t")
    monkeypatch.setattr(
        "app.services.opensearch_service.speaker_maintenance.speaker_embedding_indices",
        lambda: ["speakers_t_v4"],
    )


def test_the_stamp_rewrites_only_the_files_documents_that_differ(monkeypatch):
    _targets(monkeypatch)
    client = _RecordingClient()

    outcome = availability_stamp.stamp_files([5, 3, 5], TRASHED, client=client)

    assert outcome.complete and outcome.updated == 3
    assert [c["index"] for c in client.calls] == ["chunks_t", "transcripts_t", "speakers_t_v4"]
    for call, id_field in zip(client.calls, ("file_id", "file_id", "media_file_id"), strict=True):
        assert call["refresh"] is True
        assert call["conflicts"] == "proceed"
        assert call["body"]["query"] == {
            "bool": {
                "filter": [{"terms": {id_field: [3, 5]}}],
                "must_not": [{"term": {"availability": "trashed"}}],
            }
        }
        assert call["body"]["script"] == {
            "source": "ctx._source.availability = params.value",
            "lang": "painless",
            "params": {"value": "trashed"},
        }


def test_the_unstamp_writes_available_through_the_same_call(monkeypatch):
    _targets(monkeypatch)
    client = _RecordingClient()

    availability_stamp.stamp_files([9], AVAILABLE, client=client)

    first = client.calls[0]["body"]
    assert first["script"]["params"] == {"value": "available"}
    assert first["query"]["bool"]["must_not"] == [{"term": {"availability": "available"}}]


def test_a_large_trash_is_chunked_under_the_terms_limit(monkeypatch):
    _targets(monkeypatch)
    client = _RecordingClient(existing={"chunks_t"})

    availability_stamp.stamp_files(range(1, 10_002), TRASHED, client=client)

    sizes = [len(c["body"]["query"]["bool"]["filter"][0]["terms"]["file_id"]) for c in client.calls]
    assert sizes == [5000, 5000, 1]


def test_an_absent_index_is_skipped_and_no_client_is_a_no_op(monkeypatch):
    _targets(monkeypatch)
    client = _RecordingClient(existing={"transcripts_t"})

    availability_stamp.stamp_files([1], TRASHED, client=client)
    assert [c["index"] for c in client.calls] == ["transcripts_t"]

    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: None)
    assert availability_stamp.stamp_files([1], TRASHED).complete


@pytest.mark.parametrize(
    "response",
    [
        {"updated": 0, "version_conflicts": 2, "failures": []},
        {"updated": 0, "version_conflicts": 0, "failures": [{"cause": "x"}]},
    ],
)
def test_a_conflict_or_failure_marks_the_stamp_incomplete(monkeypatch, response):
    _targets(monkeypatch)
    client = _RecordingClient(existing={"chunks_t"}, response=response)

    outcome = availability_stamp.stamp_files([1], TRASHED, client=client)

    assert not outcome.complete
    assert outcome.incomplete == ["chunks_t"]


def test_the_script_refuses_an_unknown_value():
    with pytest.raises(ValueError):
        availability_stamp.stamp_script("deleted")


# --- reconcile (real rows) -------------------------------------------------------------------


@pytest.fixture
def bridged(monkeypatch, db_session):
    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)
    return db_session


def _file(db, owner, *, trashed: bool) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"stamp_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1,
    )
    if trashed:
        media_file.deleted_at = datetime.now(UTC)
        media_file.purge_after = datetime.now(UTC) + timedelta(days=30)
        media_file.trash_origin = "user"
    db.add(media_file)
    db.flush()
    return media_file


def _stamped(client: _RecordingClient) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for call in client.calls:
        value = call["body"]["script"]["params"]["value"]
        ids = next(iter(call["body"]["query"]["bool"]["filter"][0]["terms"].values()))
        out.setdefault(value, set()).update(ids)
    return out


def test_a_scoped_reconcile_stamps_each_file_from_its_current_row(
    monkeypatch, bridged, normal_user
):
    _targets(monkeypatch)
    trashed = _file(bridged, normal_user, trashed=True)
    live = _file(bridged, normal_user, trashed=False)
    client = _RecordingClient(existing={"chunks_t"})

    availability_stamp.reconcile([trashed.id, live.id, 987_654_321], client=client)

    assert _stamped(client) == {"trashed": {trashed.id}, "available": {live.id}}


def test_the_beat_reconcile_stamps_the_trash_and_unstamps_restored_files(
    monkeypatch, bridged, normal_user
):
    """Every trashed row is stamped trashed; a file the index still reads as trashed but whose
    row is live (a restore whose stamp failed) is stamped available; a purged id is left."""
    _targets(monkeypatch)
    trashed = _file(bridged, normal_user, trashed=True)
    restored = _file(bridged, normal_user, trashed=False)
    client = _RecordingClient(
        existing={"chunks_t", "speakers_t_v4"},
        trashed_buckets={"chunks_t": [restored.id, 987_654_321], "speakers_t_v4": [trashed.id]},
    )

    outcome = availability_stamp.reconcile(client=client)

    stamped = _stamped(client)
    assert trashed.id in stamped["trashed"]
    assert stamped["available"] == {restored.id}
    assert outcome.as_dict()["incomplete"] == []
    (_index, body) = client.searches[0]
    assert body["query"] == {"terms": {"availability": ["trashed", "quarantined"]}}
    assert body["aggs"]["files"]["composite"]["sources"] == [
        {"file": {"terms": {"field": "file_id"}}}
    ]
